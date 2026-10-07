"""Text normalisation and extraction for Macedonian listings.

Ads are written in Cyrillic, "shliokavica" Latin (struja, stan, se prodava)
and Albanian. Everything is folded into one plain-Latin form so a single
keyword list matches all of them.
"""
from __future__ import annotations

import html
import re
import unicodedata

_CYR = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ѓ": "g", "е": "e",
    "ж": "z", "з": "z", "ѕ": "dz", "и": "i", "ј": "j", "к": "k", "л": "l",
    "љ": "lj", "м": "m", "н": "n", "њ": "nj", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "ќ": "k", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "c", "џ": "dz", "ш": "s", "ё": "e", "й": "j", "ы": "i", "э": "e",
    "ю": "ju", "я": "ja", "ь": "", "ъ": "",
}
# Latin digraphs people type instead of č/š/ž/ќ/ѓ.
_DIGRAPHS = (("dzh", "dz"), ("ch", "c"), ("sh", "s"), ("zh", "z"), ("kj", "k"), ("gj", "g"))


def norm(text: str | None) -> str:
    """Lowercase, transliterate to plain Latin, collapse punctuation to spaces."""
    if not text:
        return ""
    t = html.unescape(text).lower()
    t = "".join(_CYR.get(ch, ch) for ch in t)
    t = unicodedata.normalize("NFKD", t)
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    for a, b in _DIGRAPHS:
        t = t.replace(a, b)
    # Keep clause boundaries as " . " so "nema voda, pristap ima" doesn't chain.
    t = re.sub(r"[.,;:!?\n\r()/\-–—•]+", " . ", t)
    t = re.sub(r"[^a-z0-9.]+", " ", t)
    t = re.sub(r"(?<![a-z0-9])\.(?![a-z0-9])", " . ", t)
    t = re.sub(r"(?:\s*\.\s*)+", " . ", t)
    return f" {' '.join(t.split())} "


_PHONE = re.compile(
    r"(?:\+|00)389[\s/.-]?\d(?:[\s/.-]?\d){6,9}(?!\d)"                     # +389 2 3123 456
    r"|(?<!\d)\(?0?[2-7]\d\)?[\s/.-]?\d{3}[\s/.-]?\d{3,4}(?!\d)"           # 076 506 300, 070/200-700
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def redact(text: str | None) -> str:
    """Remove phone numbers and e-mails — the repo is public, sellers' contacts stay on the site."""
    if not text:
        return text or ""
    text = _EMAIL.sub("[e-mail]", text)
    return _PHONE.sub(lambda m: "[тел.]" if len(re.sub(r"\D", "", m.group())) >= 8 else m.group(), text)


def clean(text: str | None) -> str:
    """Human-readable whitespace cleanup (keeps the original script)."""
    if not text:
        return ""
    return " ".join(html.unescape(text).split())


class KeywordSet:
    """Matches normalised keywords at word starts (so stems work: 'prodav' ⊂ 'prodava')."""

    def __init__(self, words, whole_word: bool = False):
        self.words = sorted({norm(w).strip() for w in words if norm(w).strip()})
        end = r"(?![a-z0-9])" if whole_word else ""
        self._re = (
            re.compile(r"(?<![a-z0-9])(" + "|".join(map(re.escape, self.words)) + ")" + end)
            if self.words else None
        )

    def find(self, normed: str) -> list[str]:
        return sorted(set(self._re.findall(normed))) if self._re else []

    def any(self, normed: str) -> bool:
        return bool(self._re and self._re.search(normed))


# ---------------------------------------------------------------- deal type
_RENT = KeywordSet([
    "se izdava", "izdava", "izdavam", "izdavanje", "iznajmuv", "iznajmuvam", "pod kirija",
    "kirija", "pod naem", "pod zakup", "zakup", "za izdavanje", "rent", "jepet me qira",
    "jep me qira", "me qira", "qira", "najam", "mesecno",
])
_SHORT_TERM = KeywordSet([
    "nokevanj", "nocevanj", "nokevan", "po nok", "na nok", "dnevno", "dnevni", "kratok prestoj",
    "kratkorocn", "apartman za odmor", "za odmor", "airbnb", "booking", "nocen", "per night",
])
_SALE = KeywordSet([
    "se prodava", "prodava", "prodavam", "prodazba", "na prodazba", "shitet",
    "for sale", "prodaja", "prodajem",
])
_WANTED = KeywordSet([
    "kupuvam", "kupuva", "baram", "se bara", "potrebno mi e", "potreben", "blej", "kerkoj",
    "barame", "kupuvame", "zamenuvam", "se zamenuva", "razmenuvam", "razmena", "menuvam za",
])


# Strong per-night / event wording that also counts when it's only in the
# description. ("pogoden za airbnb" alone means *suitable* for it — not counted.)
_SHORT_TERM_DESC = KeywordSet([
    "nokevanj", "nocevanj", "po nok", "na nok", "za nok", "dneven prestoj", "na den", "kratok prestoj",
    "turisticki apartman", "turisticko smestuvanje", "rodenden", "proslav", "zabava za",
])


def classify_deal(title: str, desc: str, price_eur: float | None, kind: str) -> str:
    """Return 'sale', 'rent', 'short_term' or 'wanted'."""
    t = norm(title)
    full = t + norm(desc)
    if _WANTED.any(t):
        return "wanted"
    if _SHORT_TERM.any(t):
        return "short_term"
    rent_t, sale_t = _RENT.any(t), _SALE.any(t)
    if rent_t and not sale_t:
        return "short_term" if _SHORT_TERM_DESC.any(norm(desc)) else "rent"
    if sale_t and not rent_t:
        return "sale"
    # Title ambiguous: fall back on the price — nobody rents a flat for €20k/month.
    if price_eur:
        rent_ceiling = 3000 if kind in ("apartment", "house", "weekend_house") else 800
        if price_eur <= rent_ceiling:
            return "short_term" if price_eur < 90 and _SHORT_TERM.any(full) else "rent"
        return "sale"
    if _RENT.any(full) and not _SALE.any(full):
        return "rent"
    return "sale"


# ---------------------------------------------------------------- numbers
_PRICE_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:[.,\s]\d{3})+|\d+)(?:,\d{1,2})?\s*(€|eur|евра|evra|мкд|mkd|ден|den)", re.I)


def parse_price(text: str, mkd_per_eur: float = 61.5) -> float | None:
    """'155.000 €' → 155000.0; 'По Договор' → None; MKD converted to EUR."""
    if not text:
        return None
    m = _PRICE_RE.search(html.unescape(text))
    if not m:
        return None
    digits = re.sub(r"[^\d]", "", m.group(1))
    if not digits:
        return None
    value = float(digits)
    if m.group(2).lower() in ("мкд", "mkd", "ден", "den"):
        value /= mkd_per_eur
    return value


_NUM = r"(\d{1,3}(?:[ .,]\d{3})+(?![\d])|\d+(?:[.,]\d+)?)"
_AREA_PATTERNS = [
    (re.compile(_NUM + r"\s*(?:m2|м2|m²|м²|m&#178;|мк|кв\.?\s?м|kv\.?\s?m|квадрат|kvadrat|metri kvadratni|metra kvadratni)", re.I), 1),
    (re.compile(_NUM + r"\s*(?:хектари|хектар|hektari|hektar|ha)(?![a-zа-ш])", re.I), 10000),
    (re.compile(_NUM + r"\s*(?:ари|ар|ari|ar)(?![a-zа-ш])", re.I), 100),
]


def _to_number(s: str) -> float | None:
    s = s.strip().rstrip(".,")
    if re.fullmatch(r"\d{1,3}([ .,]\d{3})+", s):    # 3.581 / 12,500 / 40 130 → thousands
        s = re.sub(r"[ .,]", "", s)
    else:
        s = s.replace(",", ".")
        if s.count(".") > 1:
            s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def parse_area(text: str) -> float | None:
    """Find the first plausible area in free text, in m²."""
    if not text:
        return None
    t = html.unescape(text)
    for rx, mult in _AREA_PATTERNS:
        for m in rx.finditer(t):
            n = _to_number(m.group(1))
            if n and 5 <= n * mult <= 5_000_000:
                return n * mult
    return None


_PER_M2 = re.compile(
    r"(?:€|eur|евра|evra|e)\s*(?:/|po|по|за|za|na|на)\s*(?:m2|м2|m²|м²|metar|метар|kvadrat|квадрат)"
    r"|(?:po|по|za|за)\s*(?:m2|м2|m²|м²|metar kvadraten|квадратен метар|kvadrat|квадрат)",
    re.I,
)


def mentions_price_per_m2(text: str) -> bool:
    """'80 €/m²', 'по 45 евра за м2', 'cena 30e po m2'."""
    return bool(_PER_M2.search(html.unescape(text or "")))


_ABROAD = KeywordSet([
    "grcija", "greece", "halkidik", "halikidik", "sitonija", "kasandra", "paralija", "bugarija", "bulgaria",
    "albanija", "albania", "durres", "saranda", "hrvatska", "croatia", "crna gora", "montenegro",
    "turcija", "turkey", "srbija", "serbia", "dubai", "spanija", "italija", "germanija", "avstrija",
    "kosovo", "pristina", "sunny beach", "budva", "tasos", "thassos", "nei pori", "leptokarija",
    "solun", "thessalon", "sitonij", "sithon", "nea epivates", "perea", "asprovalta", "kalitea", "hanioti",
])


def is_abroad(text: str) -> bool:
    return _ABROAD.any(norm(text))


def parse_rooms(text: str) -> float | None:
    m = re.search(r"(\d+(?:[.,]5)?)\s*(?:соби|соба|sobi|soba)", html.unescape(text or ""), re.I)
    return float(m.group(1).replace(",", ".")) if m else None


# ---------------------------------------------------------------- utilities
_UTILITY_STEMS = {
    "electricity": ["struj", "elektrik", "elektricn", "elektromre", "trafostanic", "rrym", "elektroenerget"],
    "water": ["voda", "vodovod", "bunar", "ujesjell", "uje", "water"],
    "road": ["asfalt", "pristap", "makadam", "magistral", "bulevar", "ulic", "rrug"],
}
# Short words that must match whole ("pat" would otherwise hit "patuvanje").
_UTILITY_WORDS = {
    "electricity": [],
    "water": [],
    "road": ["pat", "patot", "pateka", "pateki", "patista", "road", "drum"],
}
_ALL_INFRA = KeywordSet([
    "infrastruktur", "komunalno opremen", "komunalno ureden", "komunalii", "site prikluc", "urbaniziran",
])
# "can be connected", "planned", "nearby", "200 m away" — not the same as having it.
# Hedges: a utility mentioned this way is a hope or a neighbour's, not a connection.
_HEDGE = re.compile(
    r"moznost za|moze da se|se ocekuv[a-z]*|planiran[a-z]*|predviden[a-z]*|ke ima|ke se|ke bide|vo izgradba|uslovi za|"
    r"prikluc[a-z]*|vo neposredna blizina|vo blizina|nablizu|blizu|na \d+ ?m|do granica|postoeck[a-z]*|trafostanic[a-z]*|"
    r"dalekuvod|vodovodna mreza vo|na granica")
_HEDGE_WINDOW = 5      # words on either side of the utility word
# Place names that contain a utility word but say nothing about utilities.
_PLACE_NOISE = re.compile(r" (kisela voda|bela voda|studena voda|topla voda|crna voda|patiska|crven pat) ")
_NEGATORS = r"(?:nema|nemame|bez|pa|nedostasuva)"
# Words allowed between a negator and the utility it negates.
_LIST_GLUE = r"(?: (?:i|ili|ni|niti|nitu|dhe|e|struja|elektrika|voda|vodovod|pat|asfalt|kanalizacija|telefon|internet))*"


def _utility_regex(key: str) -> re.Pattern:
    stems = "|".join(map(re.escape, _UTILITY_STEMS[key]))
    words = "|".join(map(re.escape, _UTILITY_WORDS[key]))
    parts = [f"(?:{stems})[a-z]*"] if stems else []
    if words:
        parts.append(f"(?:{words})(?![a-z0-9])")
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(parts) + ")")


_UTILITY_RE = {k: _utility_regex(k) for k in _UTILITY_STEMS}


def detect_utilities(text: str) -> dict:
    """Return {'electricity': True/False/None, 'water': ..., 'road': ...}.

    True = mentioned, False = explicitly absent ("нема струја и вода"),
    None = not mentioned at all.
    """
    n = _PLACE_NOISE.sub(" ", norm(text))
    all_infra = _ALL_INFRA.any(n)
    out = {}
    for key, rx in _UTILITY_RE.items():
        # A negator before a run of utility words: "nema struja i voda".
        neg = re.search(r"(?<![a-z0-9])" + _NEGATORS + _LIST_GLUE + " " + rx.pattern[len("(?<![a-z0-9])"):], n)
        if neg:
            out[key] = False
            continue
        # Judge every mention on its own: confirmed only if at least one mention
        # has no hedge ("се очекува", "можност за", "во близина", "услови за") within
        # a few words of it. "струја има, вода во близина" → power yes, water unknown.
        confirmed = False
        for m in rx.finditer(n):
            wb = n[:m.start()].split()[-_HEDGE_WINDOW:]
            wa = n[m.end():].split()[:_HEDGE_WINDOW]
            if "." in wb:                                     # don't look past a sentence / clause break
                wb = wb[len(wb) - wb[::-1].index("."):]
            if "." in wa:
                wa = wa[:wa.index(".")]
            if not (_HEDGE.search(" ".join(wb)) or _HEDGE.search(" ".join(wa))):
                confirmed = True
                break
        if confirmed or (all_infra and not rx.search(n)):
            out[key] = True
        else:
            out[key] = None                                   # hedged or not mentioned — ask the seller
    return out


_FURNISHED = KeywordSet(["namesten", "opremen", "mobiliran", "furnished", "komplet opremen", "nov namestaj", "mobiluar"])
_UNFURNISHED = KeywordSet(["prazen", "nenamesten", "neopremen", "bez namestaj", "unfurnished", "polu namesten"])


def detect_furnished(text: str) -> bool | None:
    n = norm(text)
    if _UNFURNISHED.any(n):
        return False
    if _FURNISHED.any(n):
        return True
    return None


_RENOVATION = KeywordSet([
    "za renovir", "potrebno renovir", "potrebna adaptac", "za adaptac", "stara gradba", "star stan",
    "stara kuka", "za rusenje", "potrebno vlozuvanje", "bara vlozuvanje", "per renovim",
])


def needs_renovation(text: str) -> bool:
    return _RENOVATION.any(norm(text))


# Strong words only: "можност за градба" (could be built on) is a hope, not a zoning.
_BUILDING_LAND = KeywordSet(["gradezno", "gradezen", "gradezna dozvola", "gradezna markica", "urbanisticki plan",
                             "vo dup", "vo plan", "truall", "toke ndertimi"])
_AGRI_LAND = KeywordSet(["zemjodelsk", "niva", "nivi", "lozje", "ovostarnik", "livada", "suma", "pasiste", "bujq"])


def land_type(field_value: str | None, text: str) -> str:
    """'building', 'agricultural' or 'unknown'.

    The site's own field wins ("Градежно" / "Нива"); the ad text is consulted
    only when the field is empty or "Останато", and a text that mentions a
    field (нива/лозје) without an explicit zoning word is agricultural.
    """
    f = norm(field_value)
    if _BUILDING_LAND.any(f):
        return "building"
    if _AGRI_LAND.any(f):
        return "agricultural"
    t = norm(text)
    if _BUILDING_LAND.any(t) and not _AGRI_LAND.any(t):
        return "building"
    if _AGRI_LAND.any(t):
        return "agricultural"
    return "unknown"
