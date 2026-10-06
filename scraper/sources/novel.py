"""Novel Real Estate (novelestate.com) — a Skopje agency's own listings.

Server-rendered HTML, 12 listings per page; no robots.txt and no terms of use
on the site. Every listing has an agency code (A15579, J4435, L1106, S0489…)
that is stable across list and detail pages.

List:   estate_list.html?serviceType=renting|selling&estateType=apartment|house|lot&page=N
Detail: estate_view.html?id=<code>

Prices are in euros; "0 €" on a card / "Please contact" on the detail page
means price on request. Cards show bedrooms, not rooms, so card `rooms` is
None — the detail page has "Број на соби". The site files almost everything
under "Скопје" (even Маврово); the neighbourhood ("Населба") is what counts.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..places import CENTAR, SKOPJE, infer_district
from ..text import KeywordSet, clean, norm, parse_area, parse_price, redact

BASE = "https://novelestate.com/"
SOURCE = "novel"

SERVICE = {"rent": "renting", "sale": "selling"}
ESTATE_TYPE = {"apartment": "apartment", "house": "house", "land": "lot"}
DEAL_BY_SERVICE = {v: k for k, v in SERVICE.items()}

# The site's type words (alt text / detail heading) → our kinds.
KIND_BY_TYPE = {"стан": "apartment", "кат од куќа": "apartment", "куќа": "house", "земјиште/плац": "land",
                "канцеларија": "commercial", "локал": "commercial", "деловен објект": "commercial",
                "магацин": "commercial"}
KIND_BY_ESTATE_TYPE = {"apartment": "apartment", "house_floor": "apartment", "house": "house", "lot": "land",
                       "office-premises": "commercial", "office-shop": "commercial",
                       "office-building": "commercial", "office-warehouse": "commercial"}

# Neighbourhoods the shared gazetteer doesn't cover (normalised prefix → municipality).
_EXTRA_DISTRICTS = [("centar", CENTAR), ("11ti oktomvri", CENTAR), ("bardovci", "Карпош"),
                    ("vizbegovo", "Бутел"), ("bunardzik", "Илинден")]
# Places outside Skopje the site still files under "Скопје".
_OTHER_TOWNS = {"маврово": "Маврово", "охрид": "Охрид", "струга": "Струга", "дојран": "Дојран",
                "крушево": "Крушево", "попова шапка": "Попова Шапка"}
_OTHER_TOWN_KS = KeywordSet(list(_OTHER_TOWNS), whole_word=True)

_CODE = re.compile(r"\b([A-Z]{1,2}\d{3,6})\b")


def search_url(deal: str, kind: str, page: int) -> str:
    """deal in {"rent", "sale"}, kind in {"apartment", "house", "land"}."""
    return f"{BASE}estate_list.html?serviceType={SERVICE[deal]}&estateType={ESTATE_TYPE[kind]}&page={page}"


def detail_url(code: str) -> str:
    return f"{BASE}estate_view.html?id={code}"


def place(area: str | None) -> tuple[str | None, str | None]:
    """Neighbourhood name → (city, Reklama5-style Skopje municipality or None)."""
    area = clean(area)
    if not area:
        return SKOPJE, None
    n = norm(area)
    if _OTHER_TOWN_KS.any(n):
        town = next(v for k, v in _OTHER_TOWNS.items() if norm(k).strip() in n)
        return town, None
    for prefix, district in _EXTRA_DISTRICTS:
        if n.strip().startswith(prefix):
            return SKOPJE, district
    return SKOPJE, infer_district(area, SKOPJE)


def _price(text: str, mkd_per_eur: float) -> float | None:
    p = parse_price(text, mkd_per_eur)
    return p or None          # "0 €" = price on request


def _kind(type_word: str, fallback: str | None) -> str:
    return KIND_BY_TYPE.get(clean(type_word).lower()) or KIND_BY_ESTATE_TYPE.get(fallback or "", "other")


def _hidden(soup: BeautifulSoup, name: str) -> str:
    el = soup.select_one(f"input#{name}")
    return (el.get("value") or "") if el else ""


def parse_list(html: str, mkd_per_eur: float = 61.5) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    page_deal = DEAL_BY_SERVICE.get(_hidden(soup, "serviceType"))
    page_type = _hidden(soup, "estateType")
    out = []
    for card in soup.select("div.estate-card"):
        link = card.select_one("a.prop-title") or card.select_one("a[href*='estate_view.html?id=']")
        if not link:
            continue
        m = _CODE.search(link.get_text(" ")) or re.search(r"id=([\w-]+)", link.get("href", ""))
        if not m:
            continue
        code = m.group(1)

        img = card.select_one("img.thumb")
        alt = clean(img.get("alt")) if img else ""
        # "Се издава Стан во Тафталиџе 1 - A15579"
        title = re.sub(r"\s*-\s*" + re.escape(code) + r"$", "", alt)
        am = re.match(r"Се (издава|продава)\s+(.+?)\s+во\s+(.+)$", title)
        type_word, area_name = (am.group(2), am.group(3)) if am else ("", None)
        deal = page_deal or ("rent" if am and am.group(1) == "издава" else "sale" if am else None)

        specs = card.select_one("h4 .pull-right")
        specs_html = str(specs) if specs else ""
        mm = re.search(r"([\d.,]+)\s*<span[^>]*>\s*m\s*<sup>\s*2", specs_html)
        area = parse_area(mm.group(1) + " m2") if mm else None

        price_el = card.select_one(".price")
        price_text = clean(price_el.get_text(" ")) if price_el else ""
        city, district = place(area_name)

        out.append({
            "source": SOURCE,
            "source_id": code,
            "url": detail_url(code),
            "cat": None,
            "kind": _kind(type_word, page_type),
            "title": title or f"Novel {code}",
            "price_eur": _price(price_text, mkd_per_eur),
            "price_text": price_text,
            "old_price_eur": None,
            "area_m2": area,
            "rooms": None,          # cards only show bedrooms; the detail page has rooms
            "city": city,
            "district": district,
            "posted": None,         # not shown on cards
            "image": urljoin(BASE, img["src"]) if img and img.get("src") else None,
            "promoted": False,
            "deal": deal,
        })
    return out


def last_page(html: str) -> int | None:
    soup = BeautifulSoup(html, "html.parser")
    ff = soup.select_one(".pagination button[name=page] i.icon-fast-forward")
    if ff and ff.parent.get("value", "").isdigit():
        return int(ff.parent["value"])
    nums = [int(b.get_text(strip=True)) for b in soup.select(".pagination li a, .pagination li button")
            if b.get_text(strip=True).isdigit()]
    return max(nums) if nums else None


def parse_detail(html: str, mkd_per_eur: float = 61.5) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    fields: dict[str, str] = {}
    for tr in soup.select("table.details tr"):
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        label = clean(tds[0].get_text(" ")).rstrip(":").strip()
        value = clean(tds[1].get_text(" "))
        if not value and tds[1].find("img", src=re.compile(r"ok\.png")):
            value = "да"
        if label and value:
            fields[label] = value
    # run.detail_update reads furnishing from "Опрема"; the site calls it "Ентериер".
    if "Ентериер" in fields and "Опрема" not in fields:
        fields["Опрема"] = fields["Ентериер"]

    desc_el = soup.select_one(".estate-description")
    desc = ""
    if desc_el:
        for br in desc_el.find_all("br"):
            br.replace_with("\n")
        desc = "\n".join(clean(x) for x in desc_el.get_text().splitlines() if x.strip())

    title = None
    if soup.title:
        t = clean(soup.title.get_text()).split("|", 1)[-1]
        title = clean(re.sub(r",?\s*Шифра:.*$", "", t)) or None

    city, district = place(fields.get("Населба"))
    rooms = None
    m = re.search(r"\d+(?:[.,]5)?", fields.get("Број на соби", ""))
    if m:
        rooms = float(m.group().replace(",", "."))

    return {
        "title": title,
        "price_eur": _price(fields.get("Цена", ""), mkd_per_eur),
        "description": redact(desc),
        "fields": fields,
        "area_m2": parse_area(fields.get("Површина", "")),
        "rooms": rooms,
        "lat": None,            # the site shows no map coordinates
        "lng": None,
        "city": city,
        "district": district,
        "address": fields.get("Локација") or None,
        "posted": None,         # only "Последна промена" (last change) is shown — kept in fields
        "seller": "Novel Real Estate",
        "agency": True,
    }
