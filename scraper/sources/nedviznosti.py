"""Nedviznosti.com.mk — a listings portal (agencies and private owners).

robots.txt disallows paginated / query search (`?page=`, `/oglasi?*`), so we
never crawl search results. Instead the listing sitemap (`/oglasi/sitemap.xml`,
~1,000 URLs, each with a <lastmod>) tells us which ads are new or edited, and
only those detail pages are fetched. Each detail page carries a schema.org
`RealEstateListing` JSON-LD block; the rendered HTML fills in the rest (full
description, room count, features, seller type).

Terms of use forbid copying / redistributing the site's content: keep the
descriptions and photos for private analysis, don't republish them.
"""
from __future__ import annotations

import json
import re
import warnings
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from .. import places
from ..text import clean, norm, parse_area, parse_price, parse_rooms, redact
from .reklama5 import CITIES, is_agency

BASE = "https://nedviznosti.com.mk"
SOURCE = "nedviznosti"
SITEMAP_URL = f"{BASE}/oglasi/sitemap.xml"
LISTING_PATH = "/oglasi/oglas/"

# schema.org propertyType → our kind. The site has no weekend-house category.
KIND = {"apartment": "apartment", "house": "house", "land": "land", "commercial": "other", "other": "other"}
# Breadcrumb / URL category slug → kind (fallback when JSON-LD is missing).
KIND_BY_SLUG = {"stanovi": "apartment", "stan": "apartment", "kuki": "house", "kuka": "house",
                "zemjiste": "land", "deloven-prostor": "other", "imoti": "other", "imot": "other"}
_WEEKEND = re.compile(r"викендиц|vikendic", re.I)

# Every city spelling we may see → the Cyrillic name Reklama5 uses.
_CITY_BY_NORM = {norm(c).strip(): c for c in CITIES.values()}
# Municipalities around Skopje that Reklama5 files as "Скопје > <municipality>".
_SKOPJE_MUNICIPALITIES = {norm(d).strip(): d for d in places.NEIGHBOURHOODS}
_CENTAR = {norm("Центар").strip(), norm("Centar").strip()}


# ----------------------------------------------------------------- sitemap
def parse_sitemap(xml: str) -> list[tuple[str, str | None]]:
    """[(listing_url, lastmod_iso)] — category, agency and home pages are skipped."""
    with warnings.catch_warnings():         # no lxml: html.parser reads the flat sitemap fine
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(xml, "html.parser")
    out = []
    for u in soup.find_all("url"):
        loc = u.find("loc")
        if not loc:
            continue
        url = clean(loc.get_text())
        if LISTING_PATH not in url:
            continue
        lm = u.find("lastmod")
        out.append((url, clean(lm.get_text()) or None if lm else None))
    return out


def changed_since(entries: list[tuple[str, str | None]], known: dict[str, str | None]) -> list[str]:
    """URLs that are new, or whose <lastmod> differs from the one stored last run."""
    return [url for url, lastmod in entries if url not in known or (lastmod and known[url] != lastmod)]


def source_id(url: str) -> str | None:
    """'…/oglas/stan-skopje-90m2-se-iznajmuva-4-soben-stan-2060' → '2060'."""
    m = re.search(r"-(\d+)/?$", url.split("?")[0])
    return m.group(1) if m else None


# ----------------------------------------------------------------- detail page
def _jsonld(soup: BeautifulSoup) -> dict:
    for sc in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(sc.string or sc.get_text() or "")
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") == "RealEstateListing":
                return item
    return {}


def _section(soup: BeautifulSoup, heading: str):
    for h2 in soup.find_all("h2"):
        if clean(h2.get_text()) == heading:
            return h2.find_parent("section")
    return None


def _city(locality: str | None) -> str | None:
    if not locality:
        return None
    return _CITY_BY_NORM.get(norm(locality).strip(), clean(locality))


def _district(city: str | None, region: str | None, street: str | None) -> tuple[str | None, str | None]:
    """(city, district) in Reklama5's naming. The site's 'region' is sometimes the
    municipality ("Кисела Вода"), sometimes a neighbourhood ("Козле", "Мичурин")."""
    n_city = norm(city).strip()
    if n_city in _SKOPJE_MUNICIPALITIES:         # "Илинден", "Сопиште" as the city
        return places.SKOPJE, _SKOPJE_MUNICIPALITIES[n_city]
    if city != places.SKOPJE:
        return city, None
    for text in (region, street):
        n = norm(text).strip()
        if not n:
            continue
        if n in _CENTAR:
            return city, places.CENTAR
        if n in _SKOPJE_MUNICIPALITIES:
            return city, _SKOPJE_MUNICIPALITIES[n]
        d = places.infer_district(text, city)
        if d:
            return city, d
    return city, None


def _latlng(html: str) -> tuple[float | None, float | None]:
    # Next.js payload: "location":{…,"lat":41.99,"lng":21.43} (quotes may be backslash-escaped)
    m = re.search(r'\\?"location\\?":\{[^{}]*?\\?"lat\\?":(-?[\d.]+|null),\\?"lng\\?":(-?[\d.]+|null)', html)
    if not m or "null" in m.groups():
        return None, None
    lat, lng = float(m.group(1)), float(m.group(2))
    if not (40.8 <= lat <= 42.4 and 20.4 <= lng <= 23.1):     # outside North Macedonia
        return None, None
    return lat, lng


def _deal(ld: dict, props: dict, crumbs: list[str], url: str, price_text: str) -> str:
    tt = (props.get("transactionType") or "").lower()
    if not tt:
        joined = " ".join(crumbs).lower() + " " + url
        tt = "forrent" if re.search(r"издавање|под наем|/izdavanje", joined) else \
             "forsale" if re.search(r"продажба|/prodazba", joined) else ""
    if tt == "forrent":
        unit = ((ld.get("offers") or {}).get("eligibleDuration") or {}).get("unitCode")
        return "short_term" if unit == "DAY" or "дневно" in price_text else "rent"
    return "sale"


def parse_listing(html: str, url: str, mkd_per_eur: float = 61.5) -> dict:
    """One listing page → the card keys of reklama5.parse_list + the detail keys of parse_detail."""
    soup = BeautifulSoup(html, "html.parser")
    ld = _jsonld(soup)
    props = {p.get("name"): p.get("value") for p in ld.get("additionalProperty") or [] if isinstance(p, dict)}
    addr = ld.get("address") or {}
    offer = ld.get("offers") or {}

    nav = soup.find("nav", attrs={"aria-label": "Breadcrumb"})
    crumb_links = nav.find_all("a") if nav else []
    crumbs = [clean(a.get_text()) for a in crumb_links]
    slug = next((m.group(1) for a in crumb_links if (m := re.match(r"/oglasi/([\w-]+)$", a.get("href") or ""))),
                None) or (re.search(r"/oglas/([a-z]+(?:-prostor)?)-", url) or [None, None])[1]

    h1 = soup.find("h1")
    title = clean(ld.get("name")) or (clean(h1.get_text()) if h1 else "")

    # ---- price
    price_el = soup.find(attrs={"data-testid": "listing-detail-price"})
    # Per-m² ads show "2.250 €/м²" with the total underneath: "≈ 112.500 €".
    price_text = clean(price_el.parent.get_text(" ")) if price_el else ""
    price = None
    if offer.get("price") not in (None, ""):
        try:
            price = float(offer["price"])
            if str(offer.get("priceCurrency", "EUR")).upper() in ("MKD", "ДЕН"):
                price /= mkd_per_eur
        except (TypeError, ValueError):
            price = None
    if price is None:
        price = parse_price(price_text.rpartition("≈")[2], mkd_per_eur)
    if not price_text and price:
        price_text = f"{price:,.0f} €".replace(",", ".")

    # ---- kind / deal
    kind = KIND.get(props.get("propertyType") or "") or KIND_BY_SLUG.get(slug or "", "other")
    if kind == "house" and _WEEKEND.search(title):
        kind = "weekend_house"
    deal = _deal(ld, props, crumbs, url, price_text)

    # ---- fields (spec grid + features)
    fields: dict[str, str] = {}
    sec = _section(soup, "Површина")
    if sec:
        for dt in sec.find_all("dt"):
            dd = dt.find_next_sibling("dd")
            if dd:
                fields[clean(dt.get_text(" "))] = clean(dd.get_text(" "))
    sec = _section(soup, "Карактеристики")
    if sec:
        for span in sec.find_all("span"):
            k, sep, v = clean(span.get_text("")).partition(":")
            if sep and k and v.strip():
                fields[k.strip()] = v.strip()
        feats = [clean(li.get_text(" ")) for li in sec.find_all("li")]
        if feats:
            fields["Карактеристики"] = "; ".join(f for f in feats if f)
    if "Опременост" in fields:
        fields.setdefault("Опрема", fields["Опременост"])        # the key detail_update reads
    status = next((clean(s.get_text()) for s in soup.find_all("span")
                   if clean(s.get_text()) in ("Достапно", "Во постапка", "Продадено", "Издадено")), None)
    if status:
        fields["Статус"] = status

    # ---- description
    desc = ""
    sec = _section(soup, "Опис")
    rich = sec.select_one(".payload-richtext") if sec else None
    if rich:
        for br in rich.find_all("br"):
            br.replace_with("\n")
        blocks = rich.find_all(["p", "li", "h3", "h4"]) or [rich]
        desc = "\n".join(clean(b.get_text(" ")) for b in blocks if clean(b.get_text(" ")))
    desc = redact(desc or clean(ld.get("description")))

    # ---- area / rooms
    area = None
    fs = ld.get("floorSize") or {}
    try:
        area = float(fs.get("value")) if fs.get("value") not in (None, "") else None
    except (TypeError, ValueError):
        area = None
    if not area:
        area = parse_area(fields.get("Површина", "")) or parse_area(title) or parse_area(desc)
    rooms = None
    if fields.get("соби"):
        try:
            rooms = float(fields["соби"].replace(",", "."))
        except ValueError:
            rooms = None
    rooms = rooms or parse_rooms(title) or parse_rooms(desc)     # (JSON-LD numberOfRooms = bedrooms)

    # ---- place
    city = _city(addr.get("addressLocality"))
    street = clean(addr.get("streetAddress")) or None
    city, district = _district(city, clean(addr.get("addressRegion")) or None, street)
    loc_sec = _section(soup, "Локација")
    loc_span = loc_sec.find("span") if loc_sec else None
    address = street or (clean(loc_span.get_text()) if loc_span else None)
    lat, lng = _latlng(html)

    # ---- date / image / seller
    posted = None
    if ld.get("datePosted"):
        try:
            posted = datetime.fromisoformat(ld["datePosted"].replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            posted = None
    images = ld.get("image") or []
    image = images[0] if isinstance(images, list) and images else images if isinstance(images, str) else None
    if not image:
        og = soup.find("meta", attrs={"property": "og:image"})
        image = og.get("content") if og else None
    image = urljoin(BASE, image) if image else None

    contact = _section(soup, "Контактирај")
    role_el = contact.find("p") if contact else None
    role = clean(role_el.get_text()) if role_el else ""
    if role.startswith("Агенц"):
        agency = True
    elif role == "Корисник":
        agency = False
    else:
        agency = is_agency("", desc)

    return {
        # card keys (reklama5.parse_list)
        "source": SOURCE,
        "source_id": source_id(url) or url.rstrip("/").rsplit("/", 1)[-1],
        "url": url,
        "cat": None,
        "kind": kind,
        "deal": deal,
        "title": title,
        "price_eur": round(price, 2) if price else None,
        "price_text": price_text,
        "old_price_eur": None,
        "area_m2": area,
        "rooms": rooms,
        "city": city,
        "district": district,
        "posted": posted,
        "image": image,
        "promoted": False,
        # detail keys (reklama5.parse_detail)
        "description": desc,
        "fields": fields,
        "lat": lat,
        "lng": lng,
        "address": address,
        "seller": None,          # never stored: contact details stay on the site
        "agency": agency,
    }
