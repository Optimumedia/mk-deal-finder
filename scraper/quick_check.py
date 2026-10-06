"""Instant verdict on a deal you found yourself (Facebook, Viber, a friend…).

Forward or paste the post — or a Reklama5 link — to the Telegram bot. It
pulls out price, area, type and place, compares the €/m² with what the
platform has seen for similar property, and replies with a verdict plus
what's still unknown. Nothing here is published: leads you forward stay
on your PC (posts from closed groups don't belong on a public website).
"""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

from . import places
from .market import Market, ppm2
from .places import infer_district
from .text import detect_utilities, mentions_price_per_m2, norm, parse_area, parse_price, parse_rooms

ROOT = Path(__file__).resolve().parent.parent
DEALS_DB = ROOT / "data" / "deals.db"
_KIND = [("land", re.compile(r"(?<![a-z])(plac|placot|parcel|zemjist|niva|lozje|livada)", re.I)),
         ("weekend_house", re.compile(r"(?<![a-z])(vikend|vikendic)", re.I)),
         ("house", re.compile(r"(?<![a-z])(kuk|kuka|vila|shtepi|house)", re.I)),
         ("apartment", re.compile(r"(?<![a-z])(stan|stanot|apartman|garsonjer|studio|banes|flat|dupleks|penthaus)", re.I))]
_CITY_WORDS = {
    "Скопје": ["skopje", "скопје"], "Охрид": ["ohrid", "охрид"], "Битола": ["bitola", "битола"],
    "Струга": ["struga", "струга"], "Тетово": ["tetovo", "тетово"], "Куманово": ["kumanovo", "куманово"],
    "Прилеп": ["prilep", "прилеп"], "Велес": ["veles", "велес"], "Штип": ["stip", "штип"],
    "Струмица": ["strumica", "струмица"], "Гевгелија": ["gevgelija", "гевгелија"], "Гостивар": ["gostivar", "гостивар"],
    "Кочани": ["kocani", "кочани"], "Кавадарци": ["kavadarci", "кавадарци"], "Крушево": ["krusevo", "крушево"],
    "Маврово": ["mavrovo", "маврово"], "Берово": ["berovo", "берово"],
}
_market_cache: tuple[float, Market] | None = None


def market(cfg: dict) -> Market:
    """Market built from the scraped listings (cached for an hour)."""
    global _market_cache
    if _market_cache and time.time() - _market_cache[0] < 3600:
        return _market_cache[1]
    rows = []
    if DEALS_DB.exists():
        conn = sqlite3.connect(DEALS_DB.as_uri() + "?mode=ro", uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT kind, deal, city, district, price_eur, price_note, area_m2, title FROM listings "
            "WHERE price_eur IS NOT NULL AND area_m2 IS NOT NULL AND abroad = 0 AND deal IN ('sale', 'rent') "
            "AND COALESCE(price_note, '') != 'placeholder'")]
        conn.close()
    places.fix_districts(rows)
    m = cfg["market"]
    _market_cache = (time.time(), Market(rows, m["min_samples_district"], m["min_samples_city"], m["min_samples_national"]))
    return _market_cache[1]


def extract(text: str) -> dict:
    n = norm(text)
    kind = next((k for k, rx in _KIND if rx.search(n)), "apartment")
    city = next((c for c, words in _CITY_WORDS.items() if any(re.search(rf"(?<![a-z]){norm(w).strip()}", n) for w in words)),
                None)
    district = infer_district(text, city or "Скопје")
    if district and not city:
        city = "Скопје"
    area = parse_area(text)
    from .run import price_and_deal          # same price/deal rules as the scraper
    price, note, deal = price_and_deal(parse_price(text), kind, area, text)
    if price and note is None and price < 6000 and mentions_price_per_m2(text):
        note, deal = "per_m2", "sale"         # "1.500 €/m²" said explicitly
    return {"kind": kind, "city": city, "district": district, "price_eur": price, "price_note": note,
            "area_m2": area, "rooms": parse_rooms(text), "deal": deal,
            "utilities": detect_utilities(text), "title": text[:120]}


def verdict(lead: dict, mkt: Market) -> str:
    k, deal = lead["kind"], lead["deal"] if lead["deal"] in ("sale", "rent") else "sale"
    unit = ppm2({**lead, "deal": deal})
    where = ", ".join(x for x in (lead.get("district"), lead.get("city")) if x)
    lines = [f"🔎 Quick check — {k.replace('_', ' ')} for {deal}" + (f" in {where}" if where else "")]
    total = lead["price_eur"] * lead["area_m2"] if lead.get("price_note") == "per_m2" and lead.get("area_m2") else lead["price_eur"]
    facts = []
    if total:
        facts.append(f"price {total:,.0f} €" + ("/month" if deal == "rent" else ""))
    if lead.get("area_m2"):
        facts.append(f"{lead['area_m2']:.0f} m²")
    if lead.get("rooms"):
        facts.append(f"{lead['rooms']:g} rooms")
    if facts:
        lines.append(" · ".join(facts))
    missing = []
    if not total:
        missing.append("the price")
    if not lead.get("area_m2"):
        missing.append("the area (m²)")
    if not lead.get("city"):
        missing.append("the town / neighbourhood")
    if unit:
        rf = mkt.reference(k, deal, lead.get("city"), lead.get("district"), lead.get("area_m2"))
        ref, n, level = rf
        if ref and level != "national":
            diff = 1 - unit / ref
            where = lead["district"] if level == "district" else lead["city"]
            size = f" ({rf.band})" if rf.band else ""
            lines.append(f"{unit:,.0f} €/m² vs typical {ref:,.0f} €/m² in {where}{size} ({n} listings): "
                         f"{'%d%% below' % round(diff * 100) if diff > 0 else '%d%% above' % round(-diff * 100)}")
            lines.append("💎 Exceptional — verify fast" if diff >= 0.35 else "🔥 Strong deal" if diff >= 0.25
                         else "⭐ Good price" if diff >= 0.15 else "👍 Fair price" if diff >= 0.0
                         else "👀 Above market — negotiate or skip")
        else:
            lines.append("Not enough comparable listings in that area yet to judge the price.")
    if k == "land":
        u = lead["utilities"]
        unknown = [name for key, name in (("electricity", "electricity"), ("water", "water"), ("road", "road access"))
                   if u.get(key) is not True]
        if unknown:
            missing.append(", ".join(unknown) + " (not confirmed)")
    if missing:
        lines.append("❓ Still unknown: " + "; ".join(missing))
    return "\n".join(lines)
