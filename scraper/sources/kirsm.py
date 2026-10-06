"""KIRSM — the Chamber of Enforcement Agents' public register of bailiff sales.

https://kirm.mk/објави-и-огласи/пребарување-на-недвижен-имот/
Plain server-rendered HTML, 10 sales per page, newest first. Each sale has a
type, city, starting price (MKD), area, sale date, case number, bailiff and a
link to the official (scanned) notice. No robots.txt rules; public pages.

Bailiff sales are where property really goes under market: the first round
can't go below the appraised value, later rounds start lower.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..text import clean

BASE = "https://kirm.mk/објави-и-огласи/пребарување-на-недвижен-имот/"
SOURCE = "kirsm"

KIND = {"Стан": "apartment", "Куќа": "house", "Земјиште": "land", "Деловен простор": "commercial",
        "Гаража": "garage", "Друго": "other"}
_ROUND = [(re.compile(r"трет|3[\s-]*(?:та)?\s*усна", re.I), 3),
          (re.compile(r"втор|2[\s-]*(?:ра)?\s*усна", re.I), 2),
          (re.compile(r"прв|1[\s-]*(?:ва)?\s*усна", re.I), 1)]


# "1/6 идеален дел", "1/2 (една половина) идеален дел", "идеален дел од ..."
_SHARE = re.compile(r"(?:(\d+)\s*/\s*(\d+)\s*(?:\([^)]{0,40}\))?\s*)?(?:идеален|идеални|ид\.)\s*дел", re.I)


def ownership_share(note: str) -> str | None:
    """'1/6' when only a share of the property is sold, 'partial' if the fraction isn't given."""
    m = _SHARE.search(note or "")
    if not m:
        return None
    return f"{m.group(1)}/{m.group(2)}" if m.group(1) else "partial"


def search_url(page: int) -> str:
    return f"{BASE}?page={page}"


def _number(s: str) -> float | None:
    """'9.424.800' → 9424800; '52.924,00' → 52924.0"""
    s = s.strip()
    if not s:
        return None
    s = s.replace(".", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _round(*texts: str) -> int | None:
    t = " ".join(x for x in texts if x)
    for rx, n in _ROUND:
        if rx.search(t):
            return n
    return None


def parse_list(html: str, mkd_per_eur: float = 61.5) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for box in soup.select(".kirm-listing"):
        meta = {clean(dt.get_text()): clean(dd.get_text()) for dt, dd in zip(box.select("dt"), box.select("dd"))}
        listing_no = meta.get("Број на оглас")
        if not listing_no:
            continue
        type_mk = clean(box.select_one(".kirm-listing-title").get_text()) if box.select_one(".kirm-listing-title") else ""
        city = clean(box.select_one(".kirm-listing-sub").get_text()) if box.select_one(".kirm-listing-sub") else None
        price_box = box.select_one(".kirm-price")
        price_text = clean(price_box.get_text(" ")) if price_box else ""
        m = re.search(r"([\d.]+)\s*ден", price_text)
        price_mkd = _number(m.group(1)) if m else None
        m = re.search(r"([\d.,]+)\s*м²", price_text)
        area = _number(m.group(1)) if m else None
        note_el = box.select_one(".kirm-note")
        note = clean(note_el.get_text(" ")) if note_el else ""
        link = box.select_one("a[href]")
        pdf = urljoin("https://kirm.mk/", link["href"]) if link else BASE

        price_eur = price_mkd / mkd_per_eur if price_mkd else None
        if not price_eur:       # "0 ден." — the notice text often states the value in euros
            m = re.search(r"([\d.]+(?:,\d{1,2})?)\s*(?:евра|евро|еур|eur|€)", note, re.I)
            price_eur = _number(m.group(1)) if m else None
        sale = None
        if meta.get("Дата на продажба"):
            try:
                sale = datetime.strptime(meta["Дата на продажба"], "%d.%m.%Y").date().isoformat()
            except ValueError:
                pass
        kind = KIND.get(type_mk, "other")
        out.append({
            "source": SOURCE,
            "source_id": listing_no,
            "url": pdf,
            "cat": None,
            "kind": kind,
            "deal": "auction",
            "title": f"Bailiff sale: {type_mk or 'property'}" + (f", {int(area)} m²" if area else "") + (f" — {city}" if city else ""),
            "price_eur": round(price_eur) if price_eur else None,
            "price_note": None if price_eur else "placeholder",
            "old_price_eur": None,
            "area_m2": area if area and area > 0 else None,
            "rooms": None,
            "city": city,
            "district": None,
            "posted": None,
            "image": None,
            "promoted": False,
            "abroad": False,
            # auction-only fields
            "auction_date": sale,
            "auction_round": _round(note, pdf),
            "extra": {"case": meta.get("Број на предмет"), "bailiff": meta.get("Извршител"), "note": note[:600],
                      "type_mk": type_mk, "share": ownership_share(note)},
        })
    return dedupe(out)


def dedupe(items: list[dict]) -> list[dict]:
    """The same sale is sometimes posted twice under consecutive listing numbers."""
    seen, out = set(), []
    for it in items:
        key = (it["kind"], it["city"], it["price_eur"], it["area_m2"], it["auction_date"], (it["extra"] or {}).get("case"))
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def all_past(items: list[dict], today: date | None = None) -> bool:
    """True when every sale on the page is already over (pages run newest first)."""
    today = (today or date.today()).isoformat()
    dates = [i["auction_date"] for i in items if i["auction_date"]]
    return bool(dates) and all(d < today for d in dates)
