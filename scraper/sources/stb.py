"""Stopanska banka — property the bank took over from bad loans and now sells.

Page: https://www.stb.com.mk/ostanati-sodrzini/prodazba-na-imot/
Every bank must publish an "Образец ИП" report each month (a National Bank
rule): the land, houses, flats and business premises it owns and wants to sell,
with the property-sheet number, area, whether the bank holds possession,
encumbrances, owner and an asking price in euros. Stopanska publishes it as a
small XLSX file linked from the page (17 properties on 31.10.2026).

Access (checked 2026-11-26): robots.txt allows everything except /umbraco/;
no crawl-delay. The site's terms allow copying for non-commercial use and using
parts of the information when the source is cited — every listing links back
to the bank's page. Two requests per run (page + file).

The "Лице за контакт" column (bank staff names and phones) is never read.
The bank's other list on that page ("имот од хипотека", /api/property-sales)
is bailiff sales without real prices — KIRSM already covers those, so skip it.
"""
from __future__ import annotations

import io
import re
import zipfile
from datetime import date, datetime
from urllib.parse import quote, urljoin, urlparse
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from ..places import SKOPJE, infer_district
from ..text import clean, norm, redact

SOURCE = "stb"
PAGE_URL = "https://www.stb.com.mk/ostanati-sodrzini/prodazba-na-imot/"
BANK = "Стопанска банка АД Скопје"
# A generic client name; never a personal e-mail.
USER_AGENT = "Mozilla/5.0 (compatible; MKDealFinder/1.0; personal non-commercial use)"

# Column letters of the IP form (header row "Реден број | Опис | Адреса | ...").
COLS = {"A": "no", "B": "type", "C": "address", "D": "sheet", "E": "area", "F": "possession",
        "G": "encumbrance", "H": "owner", "I": "price"}          # J = contact person: ignored on purpose

KIND_WORDS = [  # first match wins, checked against the normalised type text
    ("spr", "apartment"), ("kat od kuk", "apartment"),
    ("vikend", "weekend_house"), ("kuk", "house"), ("vila", "house"),   # before "stan": "станбена куќа"
    ("stan", "apartment"),
    # before land: "индустриски објект со земјиште" is a factory, not a plot
    ("dukan", "commercial"), ("delov", "commercial"), ("lokal", "commercial"), ("magacin", "commercial"),
    ("industr", "commercial"), ("hotel", "commercial"), ("fabrik", "commercial"), ("kancelar", "commercial"),
    ("garaz", "garage"),
    ("zemjist", "land"), ("plac", "land"), ("niva", "land"), ("parcel", "land"),
]
# Seats of municipalities; the address names one of them or only a cadastral municipality (КО).
TOWNS = ["Скопје", "Битола", "Куманово", "Прилеп", "Тетово", "Велес", "Штип", "Охрид", "Гостивар", "Струмица",
         "Кавадарци", "Кочани", "Кичево", "Струга", "Радовиш", "Гевгелија", "Дебар", "Крива Паланка",
         "Свети Николе", "Неготино", "Делчево", "Виница", "Ресен", "Пробиштип", "Берово", "Кратово", "Крушево",
         "Македонски Брод", "Валандово", "Демир Хисар", "Богданци", "Пехчево", "Демир Капија",
         "Македонска Каменица", "Дојран", "Вевчани", "Маврово"]
SKOPJE_PARTS = ["Бутел", "Шуто Оризари", "Карпош", "Аеродром", "Кисела Вода", "Гази Баба", "Чаир", "Ѓорче Петров",
                "Сарај", "Центар", "Илинден", "Петровец", "Арачиново", "Студеничани", "Сопиште", "Зелениково"]
_TOWN_NORM = sorted(((norm(t).strip(), t) for t in TOWNS), key=lambda x: -len(x[0]))
_KO = re.compile(r"\b[КK][ОO]\.?\s+([^,;]+)")


# ----------------------------------------------------------------- page
def xlsx_url(html: str, base: str = PAGE_URL) -> str | None:
    """Link to the latest 'Образец ИП' spreadsheet on the bank's sale page."""
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.select("a[href]"):
        href = a["href"]
        label = clean(a.get_text(" "))
        if href.lower().split("?")[0].endswith(".xlsx") and ("ИП" in label or "ип" in href.lower()):
            url = urljoin(base, href)
            p = urlparse(url)
            return p._replace(path=quote(p.path, safe="/%")).geturl()   # Cyrillic file name
    return None


def fetch_bytes(http, url: str) -> bytes | None:
    """GET through the shared polite session: robots.txt, the session's pace (or the
    site's Crawl-delay if longer), a generic User-Agent. None on 404."""
    import time

    from ..http import Blocked

    if not http._allowed(url):
        raise Blocked(f"robots.txt disallows {url}")
    rp = http._robots.get(urlparse(url).netloc)
    crawl_delay = (rp.crawl_delay(USER_AGENT) or rp.crawl_delay("*")) if rp else None
    if crawl_delay and float(crawl_delay) > http.delay:
        time.sleep(max(0.0, float(crawl_delay) - (time.monotonic() - http._last)))
    http._wait()
    http._last = time.monotonic()
    http.requests_made += 1
    r = http.s.get(url, timeout=http.timeout, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    if r.status_code == 404:
        return None
    if r.status_code == 403:
        raise Blocked(f"403 at {url}")
    r.raise_for_status()
    return r.content


# ----------------------------------------------------------------- spreadsheet
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def read_rows(data: bytes) -> list[dict[str, str]]:
    """First worksheet as [{column letter: text}], standard library only."""
    z = zipfile.ZipFile(io.BytesIO(data))
    names = z.namelist()
    shared = []
    if "xl/sharedStrings.xml" in names:
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{_NS}si"):
            shared.append("".join(t.text or "" for t in si.iter(f"{_NS}t")))
    sheet = "xl/worksheets/sheet1.xml" if "xl/worksheets/sheet1.xml" in names else \
        sorted(n for n in names if n.startswith("xl/worksheets/sheet"))[0]
    rows = []
    for row in ET.fromstring(z.read(sheet)).iter(f"{_NS}row"):
        cells = {}
        for c in row.iter(f"{_NS}c"):
            col = re.match(r"[A-Z]+", c.get("r", "")).group()
            if col not in COLS:
                continue                                  # contact column (J) is never read
            t, v = c.get("t"), c.find(f"{_NS}v")
            if t == "inlineStr":
                val = "".join(x.text or "" for x in c.iter(f"{_NS}t"))
            elif v is None or v.text is None:
                continue
            elif t == "s":
                val = shared[int(v.text)]
            else:
                val = v.text
            cells[col] = clean(val)
        if cells:
            rows.append(cells)
    return rows


def _number(s: str) -> float | None:
    """'1.008' → 1008, '2.380' → 2380, '12,5' → 12.5"""
    s = (s or "").replace(" ", "")
    if not s:
        return None
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?", s):
        s = s.replace(".", "")
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_price(text: str) -> float | None:
    m = re.search(r"(\d[\d.,\s]*)\s*(?:ЕУР|EUR|евра|евро|€)", text or "", re.I)
    return _number(m.group(1).strip()) if m else None


def parse_areas(text: str) -> list[float]:
    """'5.322м2+6.553м2' → [5322, 6553]"""
    return [a for a in (_number(x) for x in re.findall(r"(\d[\d.,]*)\s*(?:м2|м²|m2|m²)", text or "")) if a]


def kind_of(type_text: str) -> str:
    n = norm(type_text)
    for stem, kind in KIND_WORDS:
        if f" {stem}" in n:
            return kind
    return "other"


def place(address: str) -> tuple[str | None, str | None]:
    """Address → (city, Skopje municipality or None)."""
    n = norm(address)
    m = _KO.search(address or "")

    def skopje():   # the cadastral municipality decides when the street part names another
        return SKOPJE, (infer_district(m.group(1), SKOPJE) if m else None) or infer_district(address, SKOPJE)

    for t_norm, town in _TOWN_NORM:
        if f" {t_norm} " in n:
            return skopje() if town == SKOPJE else (town, None)
    if any(f" {norm(p).strip()} " in n for p in SKOPJE_PARTS):
        return skopje()
    if m:   # only a cadastral municipality: 'КО Србица Вон Град' → 'Србица'
        ko = re.sub(r"\s*\bвон\s+град\b.*$", "", m.group(1), flags=re.I)
        ko = clean(re.sub(r"^\d[\d\-\s]*|\s+\d+$", "", ko))
        return ko or None, None
    return None, None


def report_date(rows: list[dict]) -> str | None:
    """'со состојба на 31.10.2026 година' → '2026-10-31'"""
    for r in rows[:10]:
        m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", " ".join(r.values()))
        if m and "состојба" in " ".join(r.values()).lower():
            try:
                return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
            except ValueError:
                return None
    return None


def _yes(s: str | None) -> bool | None:
    s = norm(s).strip()
    return True if s.startswith("da") else False if s.startswith("ne") else None


def source_id(row: dict) -> str:
    """Stable across monthly reports (the row number is not): property sheet + cadastral municipality."""
    m = _KO.search(row.get("address", ""))
    ko = norm(m.group(1) if m else row.get("address", "")).replace(".", " ")
    sheet = re.sub(r"\D+", "-", row.get("sheet", "")).strip("-")
    return "-".join(filter(None, [sheet or None, "-".join(ko.split())[:40] or None]))


def detail_fields(item: dict) -> dict:
    """What run.py stores as the listing's detail (there is no detail page). Prices
    are taken as written: the bank's figures are totals, never €/m² or placeholders."""
    from ..text import detect_utilities, land_type, needs_renovation

    text = f"{item['title']}\n{item['description']}"
    return {
        "description": item["description"],
        "fields": item["fields"],
        "address": item["address"],
        "extra": item["extra"],
        "deal": "sale",
        "agency": 0,
        "abroad": 0,
        "utilities": detect_utilities(text),
        "renovation": int(needs_renovation(text)),
        "land_type": land_type(None, text) if item["kind"] == "land" else None,
    }


def parse_xlsx(data: bytes, page_url: str = PAGE_URL, today: date | None = None) -> list[dict]:
    """Rows of the IP report → listing dicts (same shape as the other sources)
    plus 'description', 'fields', 'extra' for the detail update."""
    rows = read_rows(data)
    as_of = report_date(rows)
    today = today or date.today()
    # The report is the bank's current offer: while it is recent, count the
    # listing as fresh (the 30-day age filter is meant for stale classifieds).
    fresh = as_of and (today - date.fromisoformat(as_of)).days <= 62
    renewed = today.isoformat() if fresh else as_of
    out = []
    for r in rows:
        if not re.fullmatch(r"\d+", r.get("A", "")) or not r.get("B") or not r.get("I") and not r.get("E"):
            continue
        if r.get("B") == "2" and r.get("C") == "3":
            continue                                   # the "1 | 2 | 3 …" column-number row
        row = {COLS[k]: v for k, v in r.items()}
        kind = kind_of(row["type"])
        areas = parse_areas(row.get("area", ""))
        price = parse_price(row.get("price", ""))
        city, district = place(row.get("address", ""))
        possession = _yes(row.get("possession"))
        encumbered = _yes(row.get("encumbrance"))
        owner = row.get("owner", "")
        co_owned = "сосопств" in owner.lower()
        type_mk = row["type"]
        title = redact(f"{type_mk[:1].upper()}{type_mk[1:]}" + (f" {int(areas[0])} м²" if areas else "")
                       + (f", {city}" if city else "") + " — продажба од банка")
        notes = []
        if possession is False:
            notes.append("Банката нема владение (имотот не е во нејзин посед).")
        if encumbered:
            notes.append("Има заложно право или други товари на имотот.")
        if co_owned:
            notes.append("Сосопственост — не е целиот имот во сопственост на банката.")
        desc = redact(
            f"Имот во сопственост на {BANK} наменет за продажба (Образец ИП"
            + (f", состојба на {datetime.fromisoformat(as_of).strftime('%d.%m.%Y')}" if as_of else "") + "). "
            f"{type_mk}, {row.get('address', '')}. Имотен лист: {row.get('sheet', '?')}. "
            f"Површина: {row.get('area', '?')}. Владение: {row.get('possession', '?')}. "
            f"Товари: {row.get('encumbrance', '?')}. Сопственост: {owner or '?'}. "
            f"Цена: {row.get('price', '?')}. " + " ".join(notes))
        fields = {"Тип": type_mk, "Адреса": row.get("address", ""), "Имотен лист": row.get("sheet", ""),
                  "Површина": row.get("area", ""), "Владение": row.get("possession", ""),
                  "Товари": row.get("encumbrance", ""), "Сопственост": owner, "Цена": row.get("price", "")}
        if len(areas) > 1:
            fields["Земјиште"] = f"{int(areas[1])} м²"
        out.append({
            "source": SOURCE,
            "source_id": source_id(row),
            "url": page_url,
            "cat": None,
            "kind": kind,
            "deal": "sale",
            "title": title,
            "price_eur": round(price) if price else None,
            "price_note": None,
            "price_text": row.get("price", ""),
            "old_price_eur": None,
            "area_m2": areas[0] if areas else None,
            "rooms": None,
            "city": city,
            "district": district,
            "posted": None,
            "renewed": renewed,
            "image": None,
            "promoted": False,
            "abroad": False,
            # detail-level facts (applied at once: there is no detail page to read)
            "description": desc,
            "fields": fields,
            "address": row.get("address") or None,
            "extra": {"bank": BANK, "report_date": as_of, "possession": possession, "encumbered": encumbered,
                      "co_owned": co_owned, "sheet": row.get("sheet"), "type_mk": type_mk},
        })
    return out
