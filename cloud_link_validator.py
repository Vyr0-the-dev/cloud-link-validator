#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cloud Link Validator (Yandex + Google Drive + MEGA + Dropbox + MediaFire)
========================================================================
Herhangi bir metinden (Facebook l.php yönlendirmeleri, markdown linkleri,
yüzde-kodlanmış URL'ler dahil) bulut paylaşım linklerini otomatik ayıklar,
durumlarını kontrol eder, Yandex için içeriği (dosya/klasör ağacı) çeker,
şık bir terminal arayüzünde gösterir ve JSON/CSV/HTML/Markdown rapor üretir.

Özellikler
----------
• Çok-servisli ayıklama: Yandex Disk, Google Drive, MEGA, Dropbox, MediaFire
• Durum sınıflandırma (HTTP 200 olsa bile içerik mesajına bakılır):
    ✔ AKTİF  ·  🚫 ENGELLENDİ (içerik ihlali)  ·  ✘ BULUNAMADI/SİLİNMİŞ
    🔒 ERİŞİM ENGELLİ  ·  ✘ HATA
• Retry + exponential backoff (429/5xx ve ağ hataları için)
• Önbellek / kaldığı yerden devam (.cache json) — yarıda kesilse bile sürür
• Rapor dışa aktarım: --export json,csv,html,md  (veya all)

Kullanım
-------
    python yandex_checker.py <dosya.txt>
    python yandex_checker.py <dosya.txt> --export all --depth 2
    python yandex_checker.py <dosya.txt> --refresh        # önbelleği yok say
    python yandex_checker.py --no-check <dosya.txt>       # sadece ayıkla
"""

import re
import os
import csv
import json
import time
import html as html_lib
import argparse
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, unquote

from rich.console import Console
from rich.progress import (
    Progress, SpinnerColumn, BarColumn, TextColumn,
    TimeElapsedColumn, MofNCompleteColumn,
)
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.tree import Tree
from rich import box

console = Console()

YANDEX_API = "https://cloud-api.yandex.net/v1/disk/public/resources"

# ----------------------------------------------------------------------------
# Servis (provider) tanımları
# ----------------------------------------------------------------------------

PROVIDER_DEFS = [
    ("yandex", re.compile(
        r"https?://(?:yadi\.sk|(?:disk|cloud)\.yandex(?:\.[a-z]{2,4}){1,2}"
        r"|yandex(?:\.[a-z]{2,4}){1,2}/disk)(?:/[^\s\"'<>)\]\*\\]*)?", re.I)),
    ("google", re.compile(
        r"https?://(?:drive|docs)\.google\.com/[^\s\"'<>)\]\*\\]+", re.I)),
    ("mega", re.compile(
        r"https?://mega(?:\.co)?\.nz/[^\s\"'<>)\]\*\\]+", re.I)),
    ("dropbox", re.compile(
        r"https?://(?:www\.)?(?:dropbox\.com|db\.tt)/[^\s\"'<>)\]\*\\]+", re.I)),
    ("mediafire", re.compile(
        r"https?://(?:www\.)?mediafire\.com/[^\s\"'<>)\]\*\\]+", re.I)),
]

PROVIDER_META = {
    "yandex":    ("🟡", "Yandex Disk"),
    "google":    ("🔵", "Google Drive"),
    "mega":      ("🔴", "MEGA"),
    "dropbox":   ("🔷", "Dropbox"),
    "mediafire": ("🟠", "MediaFire"),
    "other":     ("⚪", "Diğer"),
}


def provider_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "yadi.sk" in host or "yandex" in host:
        return "yandex"
    if "google.com" in host:
        return "google"
    if "mega" in host:
        return "mega"
    if "dropbox.com" in host or "db.tt" in host:
        return "dropbox"
    if "mediafire.com" in host:
        return "mediafire"
    return "other"


# ----------------------------------------------------------------------------
# Durum mesajı desenleri (TR / RU / EN, tüm servisler)
# ----------------------------------------------------------------------------

BLOCK_PATTERNS = re.compile(
    r"Bağlantı engellendi|içeri[kğ]\s+yayınlama|yayınlama\s+koşulları|koşulları\s+ihlal"
    r"|Ссылка\s+заблокирована|заблокирован|нарушения?\s+условий"
    r"|content\s+(?:publishing|policy)|violat\w*|removed\s+for\s+violation"
    r"|link\s+(?:has\s+been\s+)?blocked|resource\s+blocked",
    re.IGNORECASE,
)

ACCESS_PATTERNS = re.compile(
    r"need\s+access|request\s+access|you\s+can'?t\s+access\s+this\s+item"
    r"|access\s+denied|don'?t\s+have\s+permission|erişim\s+izni|yetkiniz\s+yok"
    r"|доступ\s+запрещ[её]н",
    re.IGNORECASE,
)

NOTFOUND_PATTERNS = re.compile(
    r"Sonuç\s+bulunamadı|Dosya\s+(?:mevcut\s+değil|silinmiş|bulunamadı|yok)|sayfa\s+bulunamadı"
    r"|Ничего\s+не\s+найдено|Файл\s+не\s+найден|не\s+существует|удал[её]н"
    r"|not\s+found|nothing\s+found|does\s?n'?t\s+exist|no\s+longer\s+(?:available|exists)"
    r"|file\s+you\s+have\s+requested\s+does\s+not\s+exist|invalid\s+or\s+deleted\s+file"
    r"|link\s+(?:doesn'?t|does\s+not)\s+exist|couldn'?t\s+find|has\s+been\s+deleted",
    re.IGNORECASE,
)

KIND_LABEL = {
    "engellendi": "İçerik engellendi",
    "erisim": "Erişim engelli",
    "bulunamadi": "Bulunamadı / silinmiş",
}


def classify_text(text: str):
    if not text:
        return None
    if BLOCK_PATTERNS.search(text):
        return "engellendi"
    if ACCESS_PATTERNS.search(text):
        return "erisim"
    if NOTFOUND_PATTERNS.search(text):
        return "bulunamadi"
    return None


# ----------------------------------------------------------------------------
# Link ayıklama
# ----------------------------------------------------------------------------

_TRAILING_JUNK = ").,;:!*'\"<>]}”’»"


def _clean(url: str) -> str:
    url = url.strip()
    url = url.split(")")[0].split("]")[0].split("*")[0]
    if "?" not in url and "&" in url:
        url = url.split("&")[0]
    url = url.rstrip(_TRAILING_JUNK)
    return url.rstrip("/")


def _unwrap_redirects(content: str) -> list:
    decoded = []
    for raw in re.findall(r"[?&]u=([^&\s\"'<>)\]]+)", content):
        decoded.append(unquote(raw))
    for raw in re.findall(r"[?&][a-z]+=(https?%3[Aa][^&\s\"'<>)\]]+)", content):
        decoded.append(unquote(raw))
    return decoded


def extract_links(content: str) -> list:
    """Tüm servislerden benzersiz linkleri ayıkla."""
    sources = [content]
    sources.extend(_unwrap_redirects(content))
    try:
        sources.append(unquote(content))
    except Exception:
        pass

    found = set()
    for src in sources:
        for _name, pat in PROVIDER_DEFS:
            for match in pat.findall(src):
                clean = _clean(match)
                if clean:
                    found.add(clean)

    normalized = {}
    for link in found:
        p = urlparse(link)
        key = (p.netloc.lower(), p.path.rstrip("/"), p.query, p.fragment)
        normalized[key] = link
    return sorted(normalized.values())


# ----------------------------------------------------------------------------
# Ağ katmanı: retry + backoff
# ----------------------------------------------------------------------------

def _session():
    import requests
    s = requests.Session()
    try:
        from fake_useragent import UserAgent
        ua = UserAgent().random
    except Exception:
        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
    s.headers.update({"User-Agent": ua})
    return s


def _request(sess, method, url, retries=2, **kw):
    """Retry + exponential backoff'lu istek."""
    delay = 1.0
    last_exc = None
    for attempt in range(retries + 1):
        try:
            r = sess.request(method, url, **kw)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            return r
        except Exception as e:  # noqa
            last_exc = e
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    if last_exc:
        raise last_exc
    return r


def fetch_listing(sess, public_key, path=None, limit=1000, retries=2):
    params = {"public_key": public_key, "limit": limit, "sort": "name"}
    if path:
        params["path"] = path
    r = _request(sess, "GET", YANDEX_API, retries=retries, params=params, timeout=20,
                 headers={"Accept": "application/json"})
    if r.status_code == 200:
        try:
            return True, 200, r.json()
        except Exception:
            return False, 200, r.text
    return False, r.status_code, r.text


def fetch_html(sess, link, retries=2):
    try:
        r = _request(sess, "GET", link, retries=retries, timeout=20, allow_redirects=True,
                     headers={"Accept": "text/html,application/xhtml+xml"})
        return r.status_code, r.text
    except Exception as e:
        return None, type(e).__name__


# ----------------------------------------------------------------------------
# Servise göre inceleme
# ----------------------------------------------------------------------------

def inspect_yandex(link, max_depth, retries):
    res = {"link": link, "provider": "yandex", "status": "hata",
           "code": None, "label": "", "data": None}
    sess = _session()
    ok, code, payload = fetch_listing(sess, link, retries=retries)
    res["code"] = code
    if ok and isinstance(payload, dict):
        kind = classify_text(json.dumps(payload, ensure_ascii=False))
        if kind:
            res.update(status=kind, label=KIND_LABEL[kind])
            return res
        _enrich(sess, link, payload, 1, max_depth, retries)
        res.update(status="aktif", label="Erişilebilir", data=payload)
        return res
    # API başarısız → gövde + HTML sınıflandır
    kind = classify_text(payload if isinstance(payload, str) else "")
    if kind is None:
        hs, ht = fetch_html(sess, link, retries=retries)
        kind = classify_text(ht if isinstance(ht, str) else "")
        if hs and not res["code"]:
            res["code"] = hs
    if kind:
        res.update(status=kind, label=KIND_LABEL[kind])
    elif code == 403:
        res.update(status="erisim", label="Erişim engelli")
    elif code in (404, 410):
        res.update(status="bulunamadi", label="Bulunamadı / silinmiş")
    else:
        res.update(status="hata", label=f"Hata ({code})")
    return res


def _enrich(sess, public_key, data, depth, max_depth, retries):
    if depth >= max_depth or not data:
        return
    for item in data.get("_embedded", {}).get("items", []):
        if item.get("type") == "dir":
            ok, _c, sub = fetch_listing(sess, public_key, path=item.get("path"), retries=retries)
            if ok and isinstance(sub, dict):
                item["_children"] = sub
                _enrich(sess, public_key, sub, depth + 1, max_depth, retries)


def inspect_generic(link, provider, retries):
    res = {"link": link, "provider": provider, "status": "hata",
           "code": None, "label": "", "data": None}
    status, text = fetch_html(_session(), link, retries=retries)
    res["code"] = status
    kind = classify_text(text if isinstance(text, str) else "")
    if kind:
        res.update(status=kind, label=KIND_LABEL[kind])
        return res
    if status == 200:
        note = " (sınırlı kontrol)" if provider == "mega" else ""
        res.update(status="aktif", label="Erişilebilir" + note)
    elif status == 403:
        res.update(status="erisim", label="Erişim engelli")
    elif status in (404, 410):
        res.update(status="bulunamadi", label="Bulunamadı / silinmiş")
    elif status is None:
        res.update(status="hata", label=text or "Bağlantı hatası")
    else:
        res.update(status="hata", label=f"Hata ({status})")
    return res


def inspect_link(link, max_depth=1, retries=2):
    try:
        provider = provider_of(link)
        if provider == "yandex":
            return inspect_yandex(link, max_depth, retries)
        return inspect_generic(link, provider, retries)
    except Exception as e:  # noqa
        return {"link": link, "provider": provider_of(link), "status": "hata",
                "code": None, "label": type(e).__name__, "data": None}


# ----------------------------------------------------------------------------
# Yardımcılar
# ----------------------------------------------------------------------------

STATUS_META = {
    "aktif":      ("green",   "✔",  "AKTİF"),
    "engellendi": ("red",     "🚫", "ENGELLENDİ"),
    "bulunamadi": ("yellow",  "✘",  "BULUNAMADI/SİLİNMİŞ"),
    "erisim":     ("magenta", "🔒", "ERİŞİM ENGELLİ"),
    "hata":       ("red",     "✘",  "HATA"),
}
STATUS_ORDER = ["aktif", "engellendi", "bulunamadi", "erisim", "hata"]


def human_size(n):
    if not isinstance(n, (int, float)) or n <= 0:
        return "" if not isinstance(n, (int, float)) else "0 B"
    size = float(n)
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024 or u == "TB":
            return f"{size:.0f} {u}" if u == "B" else f"{size:.1f} {u}"
        size /= 1024
    return f"{n} B"


def aggregate(data):
    """(öğe sayısı, yüklenen toplam boyut) — yalnız çekilmiş ağaç üzerinden."""
    if not data:
        return 0, 0
    if data.get("type") == "file":
        return 1, int(data.get("size") or 0)
    emb = data.get("_embedded", {})
    items = emb.get("items", [])
    count = emb.get("total", len(items))
    size = 0
    for it in items:
        if it.get("type") == "dir":
            _c, s = aggregate(it.get("_children"))
            size += s
        else:
            size += int(it.get("size") or 0)
    return count, size


def read_file(filename):
    with open(filename, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def save_links(links, filename):
    with open(filename, "w", encoding="utf-8") as f:
        f.write("\n".join(links))


def _title_for(r):
    """Aktif link için başlık: Yandex içerik adı → yoksa URL'nin son parçası."""
    data = r.get("data")
    if data and data.get("name"):
        return data["name"]
    path = urlparse(r["link"]).path.rstrip("/")
    seg = unquote(path.split("/")[-1]) if path else ""
    return seg or "(başlıksız)"


def save_active_with_titles(results, filename):
    """Aktif linkleri 'Başlık <TAB> link' formatında yazar (öğe sayısı + boyutla)."""
    lines = []
    for r in results:
        if r["status"] != "aktif":
            continue
        title = _title_for(r)
        cnt, size = aggregate(r.get("data"))
        meta = f" [{cnt} öğe · {human_size(size)}]" if r.get("data") else ""
        lines.append(f"{title}{meta}\t{r['link']}")
    with open(filename, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return len(lines)


def pick_file_interactively():
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        path = filedialog.askopenfilename(
            title="Metin dosyası seçin",
            filetypes=[("Metin dosyaları", "*.txt"), ("Tüm dosyalar", "*.*")])
        root.destroy()
        if path:
            return path
    except Exception:
        pass
    try:
        return console.input("[bold cyan]Dosya yolu girin:[/] ").strip().strip('"') or None
    except (EOFError, KeyboardInterrupt):
        return None


# ----------------------------------------------------------------------------
# Önbellek (resume)
# ----------------------------------------------------------------------------

def load_cache(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(cache, path):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# Terminal arayüzü
# ----------------------------------------------------------------------------

def show_header():
    console.print(Panel(
        Text("☁️  CLOUD LINK VALIDATOR", justify="center", style="bold magenta"),
        subtitle="[dim]Yandex · Google Drive · MEGA · Dropbox · MediaFire[/]",
        border_style="magenta", box=box.DOUBLE))


def show_stats(links):
    by_prov = {}
    for l in links:
        by_prov[provider_of(l)] = by_prov.get(provider_of(l), 0) + 1
    table = Table(box=box.ROUNDED, border_style="blue", header_style="bold blue", expand=True)
    table.add_column("Servis", style="cyan")
    table.add_column("Link sayısı", style="green", justify="right")
    for prov in ["yandex", "google", "mega", "dropbox", "mediafire", "other"]:
        if by_prov.get(prov):
            emoji, name = PROVIDER_META[prov]
            table.add_row(f"{emoji} {name}", str(by_prov[prov]))
    table.add_row("[bold]TOPLAM[/]", f"[bold]{len(links)}[/]")
    console.print(table)


def _add_listing_to_tree(node, data):
    if not data:
        node.add("[dim]içerik alınamadı[/]")
        return
    if data.get("type") == "file":
        node.add(f"📄 {data.get('name','?')} [dim]{human_size(data.get('size'))}[/]")
        return
    emb = data.get("_embedded", {})
    items = emb.get("items", [])
    total = emb.get("total", len(items))
    for it in items:
        if it.get("type") == "dir":
            branch = node.add(f"[bold blue]📁 {it.get('name','?')}[/]")
            if it.get("_children"):
                _add_listing_to_tree(branch, it["_children"])
        else:
            node.add(f"📄 {it.get('name','?')} [dim]{human_size(it.get('size'))}[/]")
    if total > len(items):
        node.add(f"[dim]… +{total - len(items)} öğe daha[/]")


def show_contents(results):
    actives = [r for r in results if r["status"] == "aktif" and r.get("data")]
    if not actives:
        return
    console.print("\n[bold]📂 Link İçerikleri (Yandex)[/]\n")
    for r in actives:
        data, link = r["data"], r["link"]
        title = data.get("name") or link
        cnt, size = aggregate(data)
        label = f"[bold cyan]📁 {title}[/] [green]({cnt} öğe · {human_size(size)})[/]\n[dim]{link}[/]"
        tree = Tree(label, guide_style="dim")
        _add_listing_to_tree(tree, data)
        console.print(Panel(tree, border_style="cyan", box=box.ROUNDED, padding=(0, 1)))


def _live_line(progress, r, cached=False):
    color, icon, label = STATUS_META.get(r["status"], ("white", "•", r["status"]))
    emoji, _name = PROVIDER_META.get(r.get("provider", "other"), ("⚪", ""))
    extra = ""
    if r["status"] == "aktif" and r.get("data"):
        cnt, _s = aggregate(r["data"])
        extra = f" • {cnt} öğe"
    tag = " [dim](cache)[/]" if cached else ""
    progress.console.print(
        f"{emoji} [bold {color}]{icon} {label}[/] {r['link']} [dim]({r['code']}{extra})[/]{tag}")


def run_checks(links, workers, max_depth, retries, cache, cache_path, use_cache, refresh):
    lock = threading.Lock()
    results_map = {} if refresh else dict(cache)
    to_check = [l for l in links if refresh or l not in results_map]

    progress = Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), MofNCompleteColumn(), TextColumn("•"), TimeElapsedColumn(),
        console=console, expand=True)
    with progress:
        task = progress.add_task("[bold green]Kontrol + içerik", total=len(links))
        # önbellekten gelenleri hızlıca yaz
        for l in links:
            if l in results_map and l not in to_check:
                _live_line(progress, results_map[l], cached=True)
                progress.advance(task)
        # kalanları eşzamanlı kontrol et
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(inspect_link, l, max_depth, retries): l for l in to_check}
            try:
                for fut in as_completed(futs):
                    r = fut.result()
                    with lock:
                        results_map[r["link"]] = r
                        if use_cache:
                            save_cache(results_map, cache_path)
                    _live_line(progress, r)
                    progress.advance(task)
            except KeyboardInterrupt:
                console.print("\n[yellow]⚠ Kesildi — ilerleme önbelleğe kaydedildi, tekrar çalıştırınca devam eder.[/]")
                if use_cache:
                    save_cache(results_map, cache_path)
                raise

    return [results_map[l] for l in links if l in results_map]


def show_summary(results, outputs):
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    total_items = sum(aggregate(r.get("data"))[0] for r in results if r["status"] == "aktif")
    total_size = sum(aggregate(r.get("data"))[1] for r in results if r["status"] == "aktif")

    table = Table(box=box.HEAVY_EDGE, border_style="green", show_header=False, expand=True)
    table.add_column("k", style="bold")
    table.add_column("v")
    for status in STATUS_ORDER:
        if counts.get(status):
            color, icon, label = STATUS_META[status]
            table.add_row(f"[{color}]{icon} {label}[/]", f"[bold {color}]{counts[status]}[/]")
    table.add_row("[blue]📄 Aktif öğe / boyut[/]", f"[bold]{total_items} · {human_size(total_size)}[/]")
    for label, path in outputs:
        table.add_row(f"[cyan]{label}[/]", path)
    console.print(Panel(table, title="[bold]🎉 Özet", border_style="green", box=box.DOUBLE))


# ----------------------------------------------------------------------------
# Rapor dışa aktarım: JSON / CSV / Markdown / HTML
# ----------------------------------------------------------------------------

def export_json(results, path):
    payload = {"generated": datetime.now().isoformat(timespec="seconds"),
               "count": len(results), "results": results}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def export_csv(results, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["servis", "durum", "aciklama", "http", "oge", "boyut_byte", "boyut", "link"])
        for r in results:
            cnt, size = aggregate(r.get("data"))
            _c, _i, slabel = STATUS_META.get(r["status"], ("", "", r["status"]))
            _e, pname = PROVIDER_META.get(r.get("provider", "other"), ("", ""))
            w.writerow([pname, slabel, r.get("label", ""), r.get("code", ""),
                        cnt, size, human_size(size), r["link"]])


def _md_tree(data, depth=1):
    out = []
    if not data:
        return out
    ind = "  " * depth
    if data.get("type") == "file":
        out.append(f"{ind}- 📄 {data.get('name','?')} ({human_size(data.get('size'))})")
        return out
    emb = data.get("_embedded", {})
    items = emb.get("items", [])
    for it in items:
        if it.get("type") == "dir":
            out.append(f"{ind}- 📁 **{it.get('name','?')}**")
            if it.get("_children"):
                out += _md_tree(it["_children"], depth + 1)
        else:
            out.append(f"{ind}- 📄 {it.get('name','?')} ({human_size(it.get('size'))})")
    total = emb.get("total", len(items))
    if total > len(items):
        out.append(f"{ind}- … +{total - len(items)} öğe daha")
    return out


def export_markdown(results, path):
    lines = ["# Cloud Link Raporu",
             f"_Oluşturuldu: {datetime.now().strftime('%Y-%m-%d %H:%M')}_", ""]
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    lines.append("## Özet")
    for status in STATUS_ORDER:
        if counts.get(status):
            _c, icon, label = STATUS_META[status]
            lines.append(f"- {icon} **{label}**: {counts[status]}")
    lines.append("")
    for status in STATUS_ORDER:
        group = [r for r in results if r["status"] == status]
        if not group:
            continue
        _c, icon, label = STATUS_META[status]
        lines.append(f"## {icon} {label} ({len(group)})")
        for r in group:
            emoji, pname = PROVIDER_META.get(r.get("provider", "other"), ("", ""))
            head = f"- {emoji} [{pname}] {r['link']}"
            if r.get("label"):
                head += f" — _{r['label']}_"
            lines.append(head)
            if status == "aktif" and r.get("data"):
                lines += _md_tree(r["data"], depth=1)
        lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _html_tree(data):
    if not data:
        return '<span class="muted">içerik alınamadı</span>'
    esc = html_lib.escape
    if data.get("type") == "file":
        return (f'<ul><li class="file">📄 {esc(data.get("name","?"))} '
                f'<span class="sz">{human_size(data.get("size"))}</span></li></ul>')
    emb = data.get("_embedded", {})
    items = emb.get("items", [])
    parts = ["<ul>"]
    for it in items:
        if it.get("type") == "dir":
            child = it.get("_children")
            inner = _html_tree(child) if child else ""
            subs = (child or {}).get("_embedded", {}).get("items", [])
            cnt = f' <span class="cnt">{len(subs)}</span>' if subs else ""
            parts.append(
                f'<li><details><summary>📁 {esc(it.get("name","?"))}{cnt}</summary>{inner}</details></li>')
        else:
            parts.append(
                f'<li class="file">📄 {esc(it.get("name","?"))} '
                f'<span class="sz">{human_size(it.get("size"))}</span></li>')
    total = emb.get("total", len(items))
    if total > len(items):
        parts.append(f'<li class="muted">… +{total - len(items)} öğe daha</li>')
    parts.append("</ul>")
    return "".join(parts)


BADGE_COLORS = {"aktif": "#1a7f37", "engellendi": "#cf222e",
                "bulunamadi": "#9a6700", "erisim": "#8250df", "hata": "#cf222e"}


def export_html(results, path):
    esc = html_lib.escape
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    rows = []
    for status in STATUS_ORDER:
        group = [r for r in results if r["status"] == status]
        if not group:
            continue
        _c, icon, label = STATUS_META[status]
        items_html = []
        for r in group:
            emoji, pname = PROVIDER_META.get(r.get("provider", "other"), ("", ""))
            color = BADGE_COLORS.get(status, "#666")
            badge = f'<span class="badge" style="background:{color}">{esc(label)}</span>'
            link = esc(r["link"])
            title = esc(r.get("label") or r["link"])
            linkline = (f'<div class="linkline"><a href="{link}" target="_blank" '
                        f'rel="noopener">{link}</a></div>')
            note = f'<div class="muted">— {esc(r["label"])}</div>' if r.get("label") else ""
            tree = _html_tree(r["data"]) if (status == "aktif" and r.get("data")) else ""
            summ = (f'<summary class="head">{badge} '
                    f'<span class="prov">{emoji} {esc(pname)}</span> '
                    f'<span class="title">{title}</span></summary>')
            body = f'<div class="body">{linkline}{note}{tree}</div>'
            items_html.append(f'<details class="item">{summ}{body}</details>')
        rows.append(
            f'<details class="group" open><summary class="ghead">{icon} {esc(label)} '
            f'<small>({len(group)})</small></summary>{"".join(items_html)}</details>')
    summary_line = " · ".join(
        f'{STATUS_META[s][1]} {STATUS_META[s][2]}: {counts[s]}'
        for s in STATUS_ORDER if counts.get(s))
    css = """
body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:0;background:#f6f8fa;color:#1f2328;}
header{background:#24292f;color:#fff;padding:24px 32px;}
header h1{margin:0 0 6px;font-size:22px;} header .sum{font-size:13px;opacity:.85;}
main{max-width:1000px;margin:24px auto;padding:0 16px;}
.toolbar{margin:16px 0;display:flex;gap:8px;}
.toolbar button{background:#fff;border:1px solid #d0d7de;border-radius:6px;padding:6px 12px;font-size:13px;cursor:pointer;}
.toolbar button:hover{background:#eef1f4;}
details.group{margin:16px 0;border:1px solid #d0d7de;border-radius:8px;background:#fff;overflow:hidden;}
.ghead{cursor:pointer;font-weight:600;font-size:15px;padding:12px 16px;background:#f6f8fa;list-style:none;}
.ghead small{color:#8c959f;font-weight:500;}
details.group[open] .ghead{border-bottom:1px solid #d0d7de;}
details.item{border-top:1px solid #eaeef2;}
.head{cursor:pointer;display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:14px;padding:10px 16px;list-style:none;}
.head:hover{background:#f6f8fa;}
.head .title{color:#1f2328;font-weight:500;word-break:break-all;}
.badge{color:#fff;font-size:11px;font-weight:600;padding:2px 8px;border-radius:10px;white-space:nowrap;}
.prov{font-size:12px;color:#57606a;}
.body{padding:2px 16px 14px 38px;}
.linkline{margin:2px 0 8px;}
a{color:#0969da;text-decoration:none;word-break:break-all;} a:hover{text-decoration:underline;}
.muted{color:#8c959f;font-size:12px;}
ul{margin:6px 0 0;padding-left:18px;list-style:none;} li{margin:2px 0;font-size:13px;} ul ul{border-left:1px solid #eaeef2;margin-left:4px;}
summary{cursor:pointer;}
summary::-webkit-details-marker{display:none;}
summary::before{content:"▸";display:inline-block;margin-right:6px;color:#8c959f;transition:transform .15s;}
details[open]>summary::before{transform:rotate(90deg);}
.cnt{display:inline-block;background:#eaeef2;color:#57606a;border-radius:10px;font-size:11px;padding:0 7px;margin-left:4px;}
.sz{color:#8c959f;font-size:11px;}
footer{max-width:1000px;margin:24px auto;padding:16px;color:#8c959f;font-size:12px;text-align:center;}
"""
    toolbar = ('<div class="toolbar">'
               '<button onclick="document.querySelectorAll(\'details\')'
               '.forEach(function(d){d.open=true})">Tümünü Aç</button>'
               '<button onclick="document.querySelectorAll(\'details\')'
               '.forEach(function(d){d.open=false})">Tümünü Kapat</button>'
               '</div>')
    page = f"""<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cloud Link Raporu</title><style>{css}</style></head><body>
<header><h1>☁️ Cloud Link Raporu</h1><div class="sum">{esc(summary_line)}</div></header>
<main>{toolbar}{''.join(rows)}</main>
<footer>Oluşturuldu: {datetime.now().strftime('%Y-%m-%d %H:%M')}</footer>
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)


EXPORTERS = {"json": (export_json, ".json"), "csv": (export_csv, ".csv"),
             "md": (export_markdown, ".md"), "html": (export_html, ".html")}


def do_exports(results, fmts, stem):
    written = []
    for fmt in fmts:
        fn, ext = EXPORTERS[fmt]
        path = stem + ext
        fn(results, path)
        written.append((f"📄 {fmt.upper()} rapor", path))
    return written


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def parse_formats(value):
    if not value:
        return []
    parts = [p.strip().lower() for p in value.split(",") if p.strip()]
    if "all" in parts:
        return ["json", "csv", "md", "html"]
    out = []
    for p in parts:
        p = "md" if p in ("markdown", "md") else p
        if p in EXPORTERS and p not in out:
            out.append(p)
    return out


def main():
    ap = argparse.ArgumentParser(description="Cloud link ayıklayıcı / doğrulayıcı / içerik dökücü")
    ap.add_argument("file", nargs="?", help="Taranacak metin dosyası")
    ap.add_argument("--no-check", action="store_true", help="Sadece ayıkla, ağ kontrolü yapma")
    ap.add_argument("-o", "--output", default="active_links.txt", help="Aktif linklerin kaydedileceği dosya")
    ap.add_argument("-w", "--workers", type=int, default=8, help="Eşzamanlı istek sayısı")
    ap.add_argument("-d", "--depth", type=int, default=1, help="İçerik ağacı derinli��i (Yandex)")
    ap.add_argument("-r", "--retries", type=int, default=2, help="Hata/429/5xx için tekrar sayısı")
    ap.add_argument("-e", "--export", default="", help="Rapor formatları: json,csv,html,md veya all")
    ap.add_argument("--cache", default=".cloudlinks_cache.json", help="Önbellek dosyası (resume)")
    ap.add_argument("--no-cache", action="store_true", help="Önbelleği kullanma/yazma")
    ap.add_argument("--refresh", action="store_true", help="Önbelleği yok say, hepsini yeniden kontrol et")
    ap.add_argument("-c", "--contents", action="store_true",
                    help="Aktif Yandex linklerinin içerik ağacını terminalde göster (varsayılan: kapalı)")
    ap.add_argument("-V", "--version", action="version",
                    version="Cloud Link Validator 2.0")
    args = ap.parse_args()

    console.clear()
    show_header()

    input_file = args.file or pick_file_interactively()
    if not input_file:
        console.print("[red]✖ Dosya seçilmedi, çıkılıyor...[/]")
        return
    if not os.path.isfile(input_file):
        console.print(f"[red]✖ Dosya bulunamadı:[/] {input_file}")
        return

    with console.status("[bold green]Dosya analiz ediliyor...[/]", spinner="dots12"):
        content = read_file(input_file)
        links = extract_links(content)

    console.print(f"\n[bold]📊 İstatistikler[/] [dim]({os.path.basename(input_file)})[/]")
    show_stats(links)

    if not links:
        console.print("\n[yellow]Hiç link bulunamadı.[/]")
        return

    if args.no_check:
        save_links(links, args.output)
        console.print(f"\n[green]✔ {len(links)} link ��ıkarıldı →[/] [u]{args.output}[/]")
        return

    use_cache = not args.no_cache
    cache = load_cache(args.cache) if use_cache else {}
    if cache and not args.refresh:
        hit = sum(1 for l in links if l in cache)
        if hit:
            console.print(f"[dim]♻ Önbellekten {hit} link okunacak (yeniden kontrol için --refresh).[/]")

    console.print("\n[bold]🔍 Kontrol + içerik dökümü başlıyor[/]\n")
    results = run_checks(links, workers=args.workers, max_depth=max(1, args.depth),
                         retries=max(0, args.retries), cache=cache, cache_path=args.cache,
                         use_cache=use_cache, refresh=args.refresh)

    outputs = []
    n_active = save_active_with_titles(results, args.output)
    outputs.append((f"💾 Aktif linkler ({n_active}, başlıklı)", args.output))
    # ham link listesi (sadece URL'ler) ayrı bir dosyada da kalır
    stem0 = os.path.splitext(args.output)[0]
    raw_path = stem0 + "_urls.txt"
    save_links([r["link"] for r in results if r["status"] == "aktif"], raw_path)
    outputs.append(("🔗 Ham URL listesi", raw_path))

    fmts = parse_formats(args.export)
    if fmts:
        stem = os.path.splitext(args.output)[0]
        outputs += do_exports(results, fmts, stem)

    if args.contents:
        show_contents(results)
        console.print()
    else:
        has_tree = any(r["status"] == "aktif" and r.get("data") for r in results)
        if has_tree:
            console.print("[dim]ℹ İçerik ağacını terminalde görmek için [bold]--contents[/] (veya derinlik için [bold]--depth 2[/]).[/]")
    show_summary(results, outputs)


if __name__ == "__main__":
    main()
