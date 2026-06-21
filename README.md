<div align="center">

# ☁️ Cloud Link Validator

**Dağınık metinlerden bulut paylaşım linklerini otomatik ayıkla, durumlarını kontrol et, içeriklerini dök ve şık raporlar üret.**

[![Python](https://img.shields.io/badge/Python-3.8%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![CLI](https://img.shields.io/badge/Arayüz-Rich%20Terminal%20UI-magenta)](#)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](#katkıda-bulunma)

Yandex Disk · Google Drive · MEGA · Dropbox · MediaFire

</div>

---

https://github.com/user-attachments/assets/1826ec39-0bf0-4863-b13b-4e5fe83debad

---

## ✨ Özellikler

- 🔎 **Akıllı ayıklama** — Facebook `l.php?u=` yönlendirmeleri, markdown linkleri ve yüzde-kodlanmış (`%3A%2F%2F`) URL'lerin içinden gerçek linkleri çıkarır.
- 🌐 **Çok servisli** — Yandex Disk, Google Drive, MEGA, Dropbox ve MediaFire linklerini tanır ve kontrol eder.
- 🚦 **Nüanslı durum tespiti** — HTTP 200 dönse bile içerik mesajına bakar:
  - ✔ **AKTİF** &nbsp; 🚫 **ENGELLENDİ** (içerik ihlali) &nbsp; ✘ **BULUNAMADI/SİLİNMİŞ** &nbsp; 🔒 **ERİŞİM ENGELLİ** &nbsp; ✘ **HATA**
  - TR / RU / EN hata mesajlarını yakalar.
- 📂 **İçerik dökümü** — Yandex public API ile klasör/dosya ağacını (isim + boyut) çıkarır.
- 📝 **Başlıklı çıktı** — Aktif linkleri `Başlık [öğe · boyut] ⇥ link` formatında `.txt`'ye yazar.
- 📤 **Rapor dışa aktarımı** — `JSON`, `CSV`, `Markdown` ve şık **HTML** raporu.
- ♻️ **Önbellek & devam** — yarıda kesilse bile (`Ctrl+C`) kaldığı yerden devam eder.
- 🔁 **Dayanıklılık** — 429/5xx ve ağ hataları için otomatik retry + exponential backoff.
- 🎨 **Şık terminal arayüzü** — [Rich](https://github.com/Textualize/rich) tabanlı canlı ilerleme, renkli durumlar ve ağaç görünümü.
- ⚡ **Hızlı** — eşzamanlı (multithread) kontrol.

---

## 📦 Kurulum

```bash
git clone https://github.com/<kullanıcı-adın>/cloud-link-validator.git
cd cloud-link-validator
pip install -r requirements.txt
```

> Gereksinimler: Python 3.8+ ve `rich`, `requests`, `fake-useragent`.

---

## 🚀 Kullanım

```bash
# Temel: linkleri ayıkla + kontrol et, başlıklı txt üret
python cloud_link_validator.py liste.txt

# İçerik ağacını terminalde göster ve 2 seviye derine in
python cloud_link_validator.py liste.txt --contents --depth 2

# Tüm rapor formatlarını üret (json + csv + html + md)
python cloud_link_validator.py liste.txt --export all

# Sadece ayıkla (ağ kontrolü yapma)
python cloud_link_validator.py liste.txt --no-check

# Önbelleği yok say, hepsini yeniden kontrol et
python cloud_link_validator.py liste.txt --refresh
```

### Komut satırı seçenekleri

| Seçenek | Açıklama |
|---|---|
| `file` | Taranacak metin dosyası (boş bırakılırsa dosya seçici açılır) |
| `--no-check` | Sadece ayıkla, ağ kontrolü yapma |
| `-o, --output` | Aktif linklerin yazılacağı dosya (varsayılan `active_links.txt`) |
| `-w, --workers` | Eşzamanlı istek sayısı (varsayılan 8) |
| `-d, --depth` | Yandex içerik ağacı derinliği (varsayılan 1) |
| `-r, --retries` | Hata/429/5xx için tekrar sayısı (varsayılan 2) |
| `-e, --export` | Rapor formatları: `json,csv,html,md` veya `all` |
| `-c, --contents` | İçerik ağacını terminalde göster (varsayılan kapalı) |
| `--cache` | Önbellek dosyası yolu |
| `--no-cache` | Önbelleği kullanma/yazma |
| `--refresh` | Önbelleği yok say, hepsini yeniden kontrol et |
| `-V, --version` | Sürümü göster |

---

## 📤 Çıktılar

| Dosya | İçerik |
|---|---|
| `active_links.txt` | `Başlık [öğe · boyut] ⇥ link` (başlıklı) |
| `active_links_urls.txt` | Sadece ham URL listesi |
| `active_links.json` | Tüm sonuçlar + içerik ağaçları |
| `active_links.csv` | servis, durum, http, öğe, boyut, link |
| `active_links.md` | Duruma göre gruplu Markdown raporu |
| `active_links.html` | Renkli rozetli, açılır ağaçlı HTML raporu |

---

## 🧠 Nasıl çalışır?

1. **Ayıklama** — metni hem ham haliyle hem de FB yönlendirmeleri çözülmüş + yüzde-kodu açılmış haliyle tarar, her servis için regex eşleştirir, temizler ve tekilleştirir.
2. **Kontrol** — Yandex için public API'yi sorgular; diğer servislerde HTML gövdesini çekip TR/RU/EN hata kalıplarıyla sınıflandırır.
3. **İçerik** — aktif Yandex linklerinin klasör/dosya ağacını (isteğe bağlı derinlikte) çıkarır.
4. **Rapor** — başlıklı txt + seçilen formatlarda rapor üretir; ilerlemeyi önbelleğe yazar.

---

## 🤝 Katkıda Bulunma

PR'ler memnuniyetle karşılanır! Yeni servis desteği eklemek için `PROVIDER_DEFS` ve `provider_of()`'a bakın.

## 📄 Lisans

[MIT](LICENSE) © 2026

---

<div align="center">
<sub>Eğitim ve kişisel arşiv yönetimi amaçlıdır. Lütfen ilgili servislerin kullanım koşullarına uyun.</sub>
</div>
