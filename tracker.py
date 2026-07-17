#!/usr/bin/env python3
"""
Fiyat Takipçisi — Amazon.com.tr (Depo/ikinci el dahil) + diğer mağazalar.
Hedef fiyatın altına düşünce Telegram'dan anında bildirim gönderir.

Kullanım:
    python tracker.py              # normal çalıştırma
    python tracker.py --test       # Telegram bağlantısını test et
    python tracker.py --dry-run    # bildirim gönderme, sadece ekrana yaz
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# .env dosyası varsa yükle (opsiyonel — python-dotenv kuruluysa)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    # python-dotenv kurulu değilse .env'i elle oku
    _env_file = Path(__file__).parent / ".env"
    if _env_file.exists():
        for _line in _env_file.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip("'\""))

# --------------------------------------------------------------------------
# Ayarlar
# --------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
HISTORY_PATH = BASE_DIR / "history.csv"

TR_TZ = timezone(timedelta(hours=3))  # Türkiye saati

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Amazon bot korumasını aşmak için gerçekçi header'lar
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
]

REQUEST_TIMEOUT = 25


def build_headers(referer: str | None = None) -> dict:
    ua = random.choice(USER_AGENTS)
    is_firefox = "Firefox" in ua
    h = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
        "DNT": "1",
    }
    # Chrome ise Client Hints ekle — 403 yiyen siteler bunu kontrol ediyor
    if not is_firefox:
        h.update({
            "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        })
    if referer:
        h["Referer"] = referer
    return h


# --------------------------------------------------------------------------
# Yardımcılar
# --------------------------------------------------------------------------

def parse_price_tr(text: str) -> float | None:
    """
    '55.999,00 TL' -> 55999.0
    '1.234,56'     -> 1234.56
    '999 TL'       -> 999.0
    """
    if not text:
        return None
    # Sadece rakam, nokta ve virgül bırak
    cleaned = re.sub(r"[^\d.,]", "", text.strip())
    if not cleaned:
        return None

    # Türkçe format: nokta = binlik, virgül = ondalık
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
        # Virgül yoksa: "55.999" gibi -> binlik ayracı olabilir
        parts = cleaned.split(".")
        if len(parts) > 1 and all(len(p) == 3 for p in parts[1:]):
            cleaned = cleaned.replace(".", "")

    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if value > 0 else None


def fmt_tl(value: float) -> str:
    s = f"{value:,.2f}"
    # 55,999.00 -> 55.999,00
    s = s.replace(",", "#").replace(".", ",").replace("#", ".")
    return f"{s} TL"


def extract_asin(url: str) -> str | None:
    m = re.search(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]asin=([A-Z0-9]{10})", url, re.I)
    return m.group(1) if m else None


# --------------------------------------------------------------------------
# Veri modelleri
# --------------------------------------------------------------------------

@dataclass
class Offer:
    price: float
    condition: str          # "Sıfır" | "Amazon Depo" | "İkinci el" | mağaza adı
    seller: str = ""
    url: str = ""


@dataclass
class Target:
    name: str
    url: str
    threshold: float
    kind: str = "generic"           # "amazon" | "generic"
    include_used: bool = True       # Amazon Depo / ikinci el takip edilsin mi
    selectors: list = field(default_factory=list)  # generic için CSS seçiciler

    @classmethod
    def from_dict(cls, d: dict) -> "Target":
        return cls(
            name=d["name"],
            url=d["url"],
            threshold=float(d["threshold"]),
            kind=d.get("kind", "generic"),
            include_used=bool(d.get("include_used", True)),
            selectors=d.get("selectors", []),
        )


# --------------------------------------------------------------------------
# Amazon.com.tr
# --------------------------------------------------------------------------

AMAZON_BUYBOX_SELECTORS = [
    "span.a-price[data-a-color='base'] span.a-offscreen",
    "#corePrice_feature_div span.a-offscreen",
    "#corePriceDisplay_desktop_feature_div span.a-offscreen",
    "#priceblock_ourprice",
    "#priceblock_dealprice",
    "span.a-price span.a-offscreen",
]


def fetch(session: requests.Session, url: str, referer: str | None = None,
          retries: int = 3) -> str | None:
    """Sayfayı çeker. Captcha/403 gelirse farklı User-Agent ile tekrar dener."""
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, headers=build_headers(referer), timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            print(f"    ! istek hatası (deneme {attempt}/{retries}): {e}")
            time.sleep(random.uniform(2, 4))
            continue

        if r.status_code == 200:
            # Amazon captcha kontrolü
            if "api-services-support@amazon.com" in r.text or "Bot Check" in r.text:
                print(f"    · captcha geldi (deneme {attempt}/{retries})")
                if attempt < retries:
                    session.cookies.clear()  # yeni oturum gibi davran
                    time.sleep(random.uniform(4, 8))
                    continue
                print("    ! captcha aşılamadı")
                return None
            return r.text

        if r.status_code in (403, 429, 503):
            print(f"    · HTTP {r.status_code} (deneme {attempt}/{retries})")
            if attempt < retries:
                session.cookies.clear()
                time.sleep(random.uniform(4, 8))
                continue

        print(f"    ! HTTP {r.status_code}")
        return None

    return None


def parse_amazon_buybox(html: str) -> Offer | None:
    soup = BeautifulSoup(html, "html.parser")
    for sel in AMAZON_BUYBOX_SELECTORS:
        el = soup.select_one(sel)
        if el:
            price = parse_price_tr(el.get_text())
            if price:
                return Offer(price=price, condition="Sıfır (Buybox)", seller="Amazon")
    return None


def fetch_amazon_offers(session: requests.Session, asin: str, product_url: str) -> list[Offer]:
    """
    AOD (All Offers Display) — Amazon Depo ve diğer satıcı teklifleri.
    Amazon endpoint'i zaman zaman değiştiği için birkaç varyantı sırayla dener.
    """
    offers: list[Offer] = []

    # Denenecek endpoint'ler (en güncelden eskiye)
    endpoints = [
        f"https://www.amazon.com.tr/gp/product/ajax/ref=dp_aod_ALL_mbc"
        f"?asin={asin}&m=&qid=&smid=&sourcecustomerorglistid=&sourcecustomerorglistitemid="
        f"&sr=&pc=dp&experienceId=aodAjaxMain",

        f"https://www.amazon.com.tr/gp/product/ajax"
        f"?asin={asin}&pc=dp&experienceId=aodAjaxMain",

        f"https://www.amazon.com.tr/gp/aod/ajax/ref=auto_load_aod"
        f"?asin={asin}&pc=dp",

        f"https://www.amazon.com.tr/gp/offer-listing/{asin}/ref=dp_olp_ALL_mbc?condition=all",
    ]

    headers = build_headers(referer=product_url)
    headers.update({
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "text/html,*/*",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    })

    html = None
    for i, url in enumerate(endpoints, 1):
        try:
            r = session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            print(f"    ! AOD#{i} istek hatası: {e}")
            continue

        if r.status_code == 200 and len(r.text) > 500:
            print(f"    ✓ AOD#{i} çalıştı")
            html = r.text
            break
        print(f"    · AOD#{i} → HTTP {r.status_code}")
        time.sleep(random.uniform(0.8, 1.5))

    if not html:
        print("    ! Hiçbir AOD endpoint'i çalışmadı")
        return offers

    soup = BeautifulSoup(html, "html.parser")

    # Pinned (buybox) + normal teklifler + eski offer-listing formatı
    blocks = soup.select(
        "#aod-pinned-offer, #aod-offer, div[id^='aod-offer'], .olpOffer, #aod-offer-list > div"
    )
    for block in blocks:
        price_el = (
            block.select_one("span.a-price span.a-offscreen")
            or block.select_one(".aok-offscreen")
            or block.select_one("span.a-color-price")
            or block.select_one(".olpOfferPrice")
        )
        if not price_el:
            continue
        price = parse_price_tr(price_el.get_text())
        if not price:
            continue

        cond_el = (
            block.select_one("#aod-offer-heading h5")
            or block.select_one("div[id*='aod-offer-heading']")
            or block.select_one("h5")
            or block.select_one(".olpCondition")
            or block.select_one(".a-text-bold")
        )
        condition = cond_el.get_text(strip=True) if cond_el else "Bilinmiyor"

        seller_el = (
            block.select_one("#aod-offer-soldBy a.a-link-normal")
            or block.select_one("#aod-offer-soldBy .a-color-base")
            or block.select_one("div[id*='soldBy'] a")
            or block.select_one(".olpSellerName")
        )
        seller = seller_el.get_text(strip=True) if seller_el else ""

        # Amazon Depo tespiti
        blob = f"{condition} {seller}".lower()
        if "warehouse" in blob or "depo" in blob or "outlet" in blob:
            condition = f"AMAZON DEPO — {condition}"

        offers.append(Offer(price=price, condition=condition, seller=seller, url=product_url))

    if not offers:
        print("    · AOD sayfası geldi ama teklif ayrıştırılamadı")

    return offers


def check_amazon(session: requests.Session, target: Target) -> list[Offer]:
    print(f"  → Amazon sayfası çekiliyor...")
    html = fetch(session, target.url)
    found: list[Offer] = []

    if html:
        bb = parse_amazon_buybox(html)
        if bb:
            bb.url = target.url
            found.append(bb)
            print(f"    Buybox: {fmt_tl(bb.price)}")

    asin = extract_asin(target.url)
    if asin and target.include_used:
        time.sleep(random.uniform(1.5, 3.0))  # nazik ol
        print(f"    → Tüm teklifler (Depo dahil) çekiliyor... [ASIN: {asin}]")
        aod = fetch_amazon_offers(session, asin, target.url)
        for o in aod:
            print(f"    Teklif: {fmt_tl(o.price)} — {o.condition} ({o.seller})")
        found.extend(aod)

    return found


# --------------------------------------------------------------------------
# Genel mağazalar (Hepsiburada, Vatan, incehesap...)
# --------------------------------------------------------------------------

GENERIC_FALLBACK_SELECTORS = [
    "[data-test-id='price-current-price']",   # Hepsiburada
    "span.price-value",
    "div.product-list__price",
    ".product-price",
    "#priceNew",
    ".pricing .price",
    "[itemprop='price']",
    "meta[itemprop='price']",
]


def check_generic(session: requests.Session, target: Target) -> list[Offer]:
    print(f"  → Sayfa çekiliyor...")
    html = fetch(session, target.url)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    selectors = target.selectors or GENERIC_FALLBACK_SELECTORS

    for sel in selectors:
        el = soup.select_one(sel)
        if not el:
            continue
        raw = el.get("content") if el.name == "meta" else el.get_text()
        price = parse_price_tr(raw or "")
        if price:
            print(f"    Fiyat: {fmt_tl(price)}  [seçici: {sel}]")
            return [Offer(price=price, condition=target.name, url=target.url)]

    # Son çare: JSON-LD içinden fiyat ara
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            offers = item.get("offers") if isinstance(item, dict) else None
            if isinstance(offers, dict) and offers.get("price"):
                price = parse_price_tr(str(offers["price"]))
                if price:
                    print(f"    Fiyat: {fmt_tl(price)}  [JSON-LD]")
                    return [Offer(price=price, condition=target.name, url=target.url)]

    print("    ! fiyat bulunamadı — seçiciyi güncellemen gerekebilir")
    return []


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def send_telegram(text: str, dry_run: bool = False) -> bool:
    if dry_run:
        print("\n--- [DRY RUN] Gönderilecek mesaj ---")
        print(text)
        print("------------------------------------\n")
        return True

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("! TELEGRAM_TOKEN veya TELEGRAM_CHAT_ID tanımlı değil")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code == 200:
            print("  ✓ Telegram bildirimi gönderildi")
            return True
        print(f"  ! Telegram hatası: {r.status_code} — {r.text[:200]}")
    except requests.RequestException as e:
        print(f"  ! Telegram istek hatası: {e}")
    return False


# --------------------------------------------------------------------------
# Durum (aynı fırsatı tekrar tekrar bildirmemek için)
# --------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# Fiyat geçmişi (history.csv)
# --------------------------------------------------------------------------

HISTORY_HEADER = ["tarih", "urun", "fiyat", "durum", "satici"]


def log_history(target_name: str, offers: list[Offer]) -> None:
    """Her kontroldeki tüm teklifleri CSV'ye yazar — trend analizi için."""
    if not offers:
        return

    is_new = not HISTORY_PATH.exists()
    now = datetime.now(TR_TZ).strftime("%Y-%m-%d %H:%M:%S")

    with HISTORY_PATH.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(HISTORY_HEADER)
        for o in offers:
            writer.writerow([now, target_name, f"{o.price:.2f}", o.condition, o.seller])


def history_stats(target_name: str, current: float) -> str:
    """Mevcut fiyatı geçmişle karşılaştırıp kısa bir yorum döndürür."""
    if not HISTORY_PATH.exists():
        return ""

    prices: list[float] = []
    try:
        with HISTORY_PATH.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["urun"] == target_name:
                    try:
                        prices.append(float(row["fiyat"]))
                    except (ValueError, KeyError):
                        continue
    except OSError:
        return ""

    if len(prices) < 3:
        return ""

    lowest = min(prices)
    avg = sum(prices) / len(prices)

    lines = [f"📊 Ortalama: {fmt_tl(avg)}  |  Dip: {fmt_tl(lowest)}"]
    if current <= lowest:
        lines.append("🏆 <b>TÜM ZAMANLARIN EN DÜŞÜĞÜ!</b>")
    elif current < avg:
        fark = ((avg - current) / avg) * 100
        lines.append(f"📉 Ortalamanın %{fark:.1f} altında")
    return "\n" + "\n".join(lines)


def show_history() -> None:
    """--history ile çağrılır: kaydedilen fiyat geçmişinin özetini yazar."""
    if not HISTORY_PATH.exists():
        print("Henüz fiyat geçmişi yok. Scripti birkaç kez çalıştır.")
        return

    by_product: dict[str, list[tuple[str, float]]] = {}
    with HISTORY_PATH.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                by_product.setdefault(row["urun"], []).append(
                    (row["tarih"], float(row["fiyat"]))
                )
            except (ValueError, KeyError):
                continue

    print(f"\n{'='*60}")
    print("FİYAT GEÇMİŞİ ÖZETİ")
    print(f"{'='*60}\n")

    for name, rows in by_product.items():
        prices = [p for _, p in rows]
        print(f"[{name}]")
        print(f"  Kayıt sayısı : {len(prices)}")
        print(f"  En düşük     : {fmt_tl(min(prices))}")
        print(f"  En yüksek    : {fmt_tl(max(prices))}")
        print(f"  Ortalama     : {fmt_tl(sum(prices)/len(prices))}")
        print(f"  Son görülen  : {fmt_tl(rows[-1][1])}  ({rows[-1][0]})")
        print()


# --------------------------------------------------------------------------
# Ana akış
# --------------------------------------------------------------------------

def load_config() -> list[Target]:
    if not CONFIG_PATH.exists():
        print(f"! {CONFIG_PATH} bulunamadı. config.example.json dosyasını kopyala.")
        sys.exit(1)
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return [Target.from_dict(d) for d in data["targets"]]


def run(dry_run: bool = False) -> int:
    targets = load_config()
    state = load_state()
    session = requests.Session()
    alert_count = 0

    print(f"\n{'='*55}")
    print(f"Fiyat kontrolü başladı — {time.strftime('%d.%m.%Y %H:%M:%S')}")
    print(f"{'='*55}\n")

    for target in targets:
        print(f"[{target.name}]  hedef: {fmt_tl(target.threshold)}")

        if target.kind == "amazon":
            offers = check_amazon(session, target)
        else:
            offers = check_generic(session, target)

        if not offers:
            print("  (fiyat alınamadı)\n")
            continue

        # Fiyat geçmişine yaz — her kontrolde, bildirim olsun olmasın
        if not dry_run:
            log_history(target.name, offers)

        # Eşiğin altındaki teklifler
        hits = [o for o in offers if o.price <= target.threshold]

        if hits:
            best = min(hits, key=lambda o: o.price)
            key = f"{target.name}|{best.condition}"
            last_price = state.get(key, {}).get("price")

            # Aynı fiyatı tekrar bildirme; sadece daha ucuza düşerse tekrar at
            if last_price is not None and best.price >= last_price:
                print(f"  (zaten bildirildi: {fmt_tl(best.price)})\n")
            else:
                fark = target.threshold - best.price
                indirim = f"\n💰 Hedefin <b>{fmt_tl(fark)}</b> altında"
                stats = history_stats(target.name, best.price)

                msg = (
                    f"🔥 <b>FİYAT DÜŞTÜ!</b>\n\n"
                    f"📦 <b>{target.name}</b>\n"
                    f"💵 <b>{fmt_tl(best.price)}</b>\n"
                    f"🏷 {best.condition}\n"
                    + (f"🏪 {best.seller}\n" if best.seller else "")
                    + f"🎯 Hedef: {fmt_tl(target.threshold)}"
                    + indirim
                    + stats
                    + f"\n\n🔗 {best.url or target.url}"
                )
                if send_telegram(msg, dry_run=dry_run):
                    alert_count += 1
                    if not dry_run:
                        state[key] = {"price": best.price, "ts": time.time()}
                print()
        else:
            cheapest = min(offers, key=lambda o: o.price)
            print(f"  En ucuz: {fmt_tl(cheapest.price)} — hedefin üstünde\n")

        time.sleep(random.uniform(2.0, 4.0))  # mağazalar arası bekleme

    if not dry_run:
        save_state(state)

    print(f"{'='*55}")
    print(f"Bitti. {alert_count} bildirim gönderildi.")
    print(f"{'='*55}\n")
    return alert_count


def test_telegram() -> None:
    ok = send_telegram(
        "✅ <b>Fiyat takipçisi bağlandı!</b>\n\n"
        "Bu bir test mesajıdır. Bunu görüyorsan kurulum doğru.\n\n"
        "🎯 RTX 5070 Ti avına hazırız."
    )
    sys.exit(0 if ok else 1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fiyat takipçisi")
    parser.add_argument("--test", action="store_true", help="Telegram bağlantısını test et")
    parser.add_argument("--dry-run", action="store_true", help="Bildirim gönderme, ekrana yaz")
    parser.add_argument("--history", action="store_true", help="Fiyat geçmişi özetini göster")
    args = parser.parse_args()

    if args.test:
        test_telegram()

    if args.history:
        show_history()
        sys.exit(0)

    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()