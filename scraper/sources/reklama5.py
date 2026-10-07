"""Reklama5.mk (mobile site) — list and detail page parsing.

The desktop site sits behind a bot challenge; the mobile site serves plain
HTML and its robots.txt allows general crawling.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from bs4 import BeautifulSoup

from ..text import KeywordSet, clean, norm, parse_area, parse_price, parse_rooms

_AGENCY = KeywordSet(["dooel", "doo", "agencij", "nedviznin", "real estate", "realestate", "estate", "properties",
                      "imot", "home", "invest", "provizij", "agent"])


def is_agency(seller: str, desc: str) -> bool:
    return _AGENCY.any(norm(seller)) or bool(re.search(r"агенција|agencija|провизија|provizija", desc or "", re.I))

BASE = "https://m.reklama5.mk"
SOURCE = "reklama5"

CITIES = {1: "Скопје", 2: "Битола", 3: "Куманово", 4: "Прилеп", 5: "Тетово", 6: "Велес", 7: "Штип", 8: "Охрид",
          9: "Гостивар", 10: "Струмица", 11: "Кавадарци", 12: "Кочани", 13: "Кичево", 14: "Струга", 15: "Радовиш",
          16: "Гевгелија", 17: "Дебар", 18: "Крива Паланка", 19: "Свети Николе", 20: "Неготино", 21: "Делчево",
          22: "Виница", 23: "Ресен", 24: "Пробиштип", 25: "Берово", 26: "Кратово", 28: "Крушево",
          29: "Македонски Брод", 30: "Валандово", 34: "Демир Хисар"}

# Coordinates the site fills in when the seller doesn't move the map pin.
DEFAULT_PINS = [(41.98707, 21.45193)]

KIND_BY_CAT = {159: "apartment", 158: "house", 173: "land", 161: "weekend_house"}

_MONTHS = {"јан": 1, "фев": 2, "мар": 3, "апр": 4, "мај": 5, "јун": 6, "јул": 7, "авг": 8,
           "сеп": 9, "окт": 10, "ное": 11, "дек": 12}


def search_url(cat: int, city: int, page: int) -> str:
    q = f"cat={cat}" + (f"&city={city}" if city else "")
    return f"{BASE}/Search?{q}&page={page}"


def detail_url(ad_id: str) -> str:
    return f"{BASE}/AdDetails?ad={ad_id}"


def _posted(text: str, today: date) -> str | None:
    t = text.lower()
    if "денес" in t:
        return today.isoformat()
    if "вчера" in t:
        return (today - timedelta(days=1)).isoformat()
    m = re.search(r"(\d{1,2})\s+([^\W\d_]{3})", t)
    if m and m.group(2) in _MONTHS:
        month, day = _MONTHS[m.group(2)], int(m.group(1))
        year = today.year if (month, day) <= (today.month, today.day) else today.year - 1
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None
    return None


def _card_area(specs: str) -> float | None:
    """House cards read "Парцела: 1300 m² • Изградена: 100 m²" — the house is
    the built area, never the plot."""
    m = re.search(r"Изградена:\s*([\d.,]+)\s*m", specs)
    if m:
        return parse_area(m.group(1) + " m2")
    if "Парцела" in specs:
        return None
    return parse_area(specs)


def parse_list(html: str, cat: int, mkd_per_eur: float = 61.5, today: date | None = None) -> list[dict]:
    today = today or date.today()
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for card in soup.select("div.OglasResults"):
        a = card.select_one("a.SearchAdTitle")
        if not a:
            continue
        m = re.search(r"ad=(\d+)", a.get("href", ""))
        if not m:
            continue
        p = a.find("p")
        specs = clean(p.get_text(" ")) if p else ""
        if p:
            p.extract()
        title = clean(a.get_text(" "))

        price_box = card.select_one(".price-mobile-box")
        old_price = None
        if price_box:
            disc = price_box.select_one(".oglasDiscount")
            if disc:
                s = disc.find("s")
                old_price = parse_price(s.get_text(" ") if s else "", mkd_per_eur)
                disc.extract()
            price_text = clean(price_box.get_text(" "))
        else:
            price_text = ""
        price = parse_price(price_text, mkd_per_eur)

        loc_el = card.select_one(".adDate")
        loc_raw = clean(loc_el.get_text(" ")) if loc_el else ""
        # "Скопје > Карпош 23 сеп 12:32" / "Охрид Денес 13:00"
        loc_part = re.split(r"\s(?:Денес|Вчера|\d{1,2}\s+[^\W\d_]{3})", loc_raw)[0].strip()
        city, _, district = (x.strip() for x in loc_part.partition(">"))

        img = card.select_one("img.profilethumbs")
        img_src = img.get("src") if img else None
        if img_src and img_src.startswith("//"):
            img_src = "https:" + img_src

        out.append({
            "source": SOURCE,
            "source_id": m.group(1),
            "url": detail_url(m.group(1)),
            "cat": cat,
            "kind": KIND_BY_CAT.get(cat, "other"),
            "title": title,
            "price_eur": price,
            "price_text": price_text,
            "old_price_eur": old_price,
            "area_m2": _card_area(specs) or parse_area(title),
            "rooms": parse_rooms(specs),
            "city": city or None,
            "district": district or None,
            "posted": _posted(loc_raw, today),      # the card shows the last renewal date
            "renewed": _posted(loc_raw, today),
            "image": img_src,
            "promoted": "topAdResults" in (card.get("class") or []),
        })
    return out


def parse_detail(html: str, mkd_per_eur: float = 61.5) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    fields = {}
    for item in soup.select(".itemDescription"):
        k, v = item.select_one(".itemStatic"), item.select_one(".itemDinamic")
        if k and v:
            fields[clean(k.get_text(" "))] = clean(v.get_text(" "))

    desc_el = soup.select_one("p.oglasDescription")
    desc = ""
    if desc_el:
        for br in desc_el.find_all("br"):
            br.replace_with("\n")
        desc = "\n".join(clean(x) for x in desc_el.get_text().splitlines() if x.strip())

    title_el = soup.select_one(".adDetailsMobile.title h2") or soup.find("h2")
    price_el = soup.select_one(".adDetailPrice")

    lat = lng = None
    m = re.search(r"showMap\(\s*([\d.]+)\s*,\s*([\d.]+)\s*\)", html)
    if m:
        lat, lng = float(m.group(1)), float(m.group(2))
        if not (40.8 <= lat <= 42.4 and 20.4 <= lng <= 23.1):   # outside North Macedonia
            lat = lng = None
        elif any(abs(lat - a) < 1e-4 and abs(lng - b) < 1e-4 for a, b in DEFAULT_PINS):
            lat = lng = None        # the form's default pin, not the property

    place_el = soup.select_one(".show-map .place")
    city = district = None
    if place_el:
        parts = [clean(x) for x in place_el.get_text("|").split("|") if clean(x) and clean(x) != "/"]
        city = parts[0] if parts else None
        district = parts[1] if len(parts) > 1 else None

    posted = None
    m = re.search(r"Објавен на:\s*(\d{2}\.\d{2}\.\d{4})", html)
    if m:
        posted = datetime.strptime(m.group(1), "%d.%m.%Y").date().isoformat()

    title = clean(title_el.get_text(" ")) if title_el else ""
    area = None
    # Houses: the built area, not the plot ("Површина" is often the plot).
    for key in ("Изградена површина (m²)", "Изградена површина", "Површина (m²)", "Квадратура", "Површина"):
        if key in fields:
            area = parse_area(fields[key])
            if area:
                break
    if not area or area < 10:   # sellers often type "1 m²" in the form; trust the text instead
        area = parse_area(title) or parse_area(desc) or area

    seller_el = soup.select_one(".user-name")
    seller = next(iter(seller_el.stripped_strings), "") if seller_el else ""
    return {
        "title": title or None,
        "price_eur": parse_price(price_el.get_text(" "), mkd_per_eur) if price_el else None,
        "description": desc,
        "fields": fields,
        "area_m2": area,
        "rooms": parse_rooms(fields.get("Број на соби", "") + " соби") if fields.get("Број на соби") else None,
        "lat": lat,
        "lng": lng,
        "city": city,
        "district": district,
        "address": fields.get("Адреса"),
        "posted": posted,
        "seller": seller or None,
        "agency": is_agency(seller, desc),
    }
