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
    r"|(?<![\d\w])0[2-7]\d(?:[\s/.-]?\d){6}(?!\d)"                          # 078 33 56 23, 02 3 123 456
    r"|(?:\+|(?<![\d\w])00)[1-9]\d{0,2}[\s/.-]?\(?\d{1,4}\)?(?:[\s/.-]?\d){5,10}(?!\d)"   # +49 160 93986986
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
    # Not "dneven prestoj": in flat ads that is the living room ("дневен престој со кујна").
    "nokevanj", "nocevanj", "po nok", "na nok", "za nok", "na den", "kratok prestoj",
    "turisticki apartman", "turisticko smestuvanje", "rodenden", "proslav", "zabava za",
])


_BUYING = KeywordSet(["kupuvam", "kupuvame", "baram da kupam", "barame da kupime", "blej", "kerkoj"], whole_word=True)


def classify_deal(title: str, desc: str, price_eur: float | None, kind: str) -> str:
    """Return 'sale', 'rent', 'short_term' or 'wanted'."""
    t = norm(title)
    full = t + norm(desc)
    if _WANTED.any(t) or _BUYING.any(" ".join(norm(desc).split()[:4])):
        return "wanted"                 # "Kupuvam stan so pogled..." opening the description
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
    (re.compile(_NUM + r"\s*(?:декари|декар|дек|дулуми|дулум|dekari|dekar|dek|dulumi|dulum|dynym)(?![a-zа-ш])", re.I), 1000),
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


_THOUSAND = r"k\b|к\b|илј[а-ш]*|ilj[a-z]*|хиљ[а-ш]*|hilj[a-z]*"
_EUR = r"евра|евро|evra|evro|eur[a-z]*|eu\b|еу\b|€|e\b|е\b"
_MKD = r"мкд|mkd|ден[а-ш]*|den[a-z]*"
_TEXT_PRICE = re.compile(
    r"(?:cena|цена|price|prodavam za|продавам за|se prodava za|се продава за)[^\d€]{0,25}?"
    r"(\d{1,3}(?:[ .,]\d{3})+|\d{4,7}|\d{1,3}(?=\s*(?:" + _THOUSAND + r")))\s*(" + _THOUSAND + r")?\s*"
    r"(" + _EUR + "|" + _MKD + r")?", re.I)
# What follows a number when it is a rate, not a total: "2250 евра/м2", "1.900e,m2", "40 evra kvadrat".
_PER_M2_TAIL = re.compile(
    r"\s*(?:[/,]|\s|po\b|по\b|za\b|за\b|na\b|на\b|od\b|од\b|eden\b|еден\b)*\s*"
    r"(?:m2|м2|m²|м²|mk\b|мк\b|kv|кв|metar|метар|kvadrat|квадрат)", re.I)
_RATE = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:[.,]\d{3})+(?![.,]?\d)|\d{1,5}(?:[.,]\d{1,2})?)\s*(" + _EUR + "|" + _MKD + r")"
    r"(?=" + _PER_M2_TAIL.pattern + r")", re.I)


def _is_mkd(unit: str | None) -> bool:
    return bool(unit) and re.fullmatch(_MKD, unit, re.I) is not None


def price_in_text(text: str, mkd_per_eur: float = 61.5) -> float | None:
    """A total price the seller wrote in the description: 'Cena..20000evra',
    'цена 45.000 €', 'продавам за 20 000 евра', 'цена 45 илјади евра'.
    Rates ('цена 2250 евра/м2') are skipped: see price_per_m2_in_text.
    None when nothing clear."""
    if not text:
        return None
    t = html.unescape(text)
    for m in _TEXT_PRICE.finditer(t):
        digits = re.sub(r"[^\d]", "", m.group(1))
        if not digits:
            continue
        if _PER_M2_TAIL.match(t, m.end()):
            continue                                   # "цена: 2250 евра/м2" is a rate
        if _EXTRAS.search(m.group(0)):
            continue                                   # "цена на паркинг 15.000 евра" isn't the property
        v = float(digits) * (1000 if m.group(2) else 1)
        if _is_mkd(m.group(3)):
            v /= mkd_per_eur
        if 1000 <= v <= 5_000_000:
            return v
    return None


def price_per_m2_in_text(text: str, mkd_per_eur: float = 61.5, unique: bool = False) -> float | None:
    """A price per m² with its currency: '40 evra kvadrat', '45E/m2', '75 € / м²',
    'Cena 1.900e,m2', '4,5 евра/м2', '1500 ден/м2'. A bare '1199 kvadrati' is an area,
    not a rate. With unique, an ad quoting different small prices (several plots,
    "35 €/m² — the front plot 55 €") gives None: which rate goes with which plot is a guess."""
    if not text:
        return None
    t = html.unescape(text)
    rates = []
    for m in _RATE.finditer(t):
        v = _to_number(m.group(1))
        if v is None:
            continue
        if _is_mkd(m.group(2)):
            v /= mkd_per_eur
        if 0.5 <= v <= 10_000:
            rates.append(round(v, 2))
    if not rates:
        return None
    if unique:
        small = {_to_number(m.group(1)) for m in re.finditer(
            r"(?<![\d.,])(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:" + _EUR + r")(?![a-zа-ш])", t, re.I)}
        if len(set(rates) | {round(v, 2) for v in small if v}) > 1:
            return None
    return rates[0]


_EXTRAS = re.compile(r"parking|паркинг|garaz|гараж|podrum|подрум|ostava|остава|dvor|двор|teras|терас|"
                     r"depozit|депозит|provizij|провизиј|kaucij|кауциј|izdavanje|издавање|kirij|кириј", re.I)
_PO_DOGOVOR = KeywordSet(["po dogovor", "cena dogovor", "me marreveshje", "negotiable"])


def price_by_agreement(text: str) -> bool:
    """'Цена по договор' — the seller hasn't given a price."""
    return _PO_DOGOVOR.any(norm(text))


_TEXT_ALT = re.compile(
    r"(\d{1,2}[.,]\d{3}|\d{3,4})\s*(?:m|м|metri|метри|metra|метра)?\.?(?:\s+\w+){0,2}\s*"
    r"(?:nadmorska|надморска|(?<![a-zа-ш])(?:m\.?|м\.?)?(?:n\.?v\.?|н\.?в\.?)(?![a-zа-ш]))"
    # "visina / visocina / visočina / височина" and typos like "висона"
    r"|(?:nadmorska|надморска)\s+(?:vis|вис)\w*\D{0,12}(\d{1,2}[.,]\d{3}|\d{3,4})", re.I)


def altitude_in_text(text: str) -> float | None:
    """'1600m e nadmorska visochina', 'на 1.350 м надморска висина' → metres."""
    m = _TEXT_ALT.search(html.unescape(text or ""))
    if not m:
        return None
    raw = (m.group(1) or m.group(2)).replace(".", "").replace(",", "")
    v = float(raw)
    return v if 100 <= v <= 2800 else None


def parse_rooms(text: str) -> float | None:
    m = re.search(r"(\d+(?:[.,]5)?)\s*(?:соби|соба|sobi|soba)", html.unescape(text or ""), re.I)
    return float(m.group(1).replace(",", ".")) if m else None


# ---------------------------------------------------------------- utilities
_UTILITY_STEMS = {
    "electricity": ["struj", "elektrik", "elektricn", "elektromre", "trafostanic", "rrym", "elektroenerget"],
    "water": ["voda", "vodovod", "bunar", "ujesjell", "uje", "water"],
    "road": ["asfalt", "asvalt", "pristap", "prilaz", "magistral", "bulevar", "ulic", "rrug"],
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
    r"moznost za(?! gradb| izgradb| podelb| delb| dokup| kupuv)|moze da se|mozi da|se ocekuv[a-z]*|planiran[a-z]*|predvid[a-z]*|ke ima|ke se|ke bide|vo izgradba|"
    r"uslovi za|prikluc[a-z]*|vo neposredna blizina|vo blizina|nablizu|blizu|na \d+ ?m|do granica|postoeck[a-z]*|"
    r"trafostanic[a-z]*|dalekuvod|vodovodna mreza vo|na granica|pregovar[a-z]*|"
    # off the road, not on it: "одвоено од главен пат", "оддалечен 300 метри", "пристап со теренско возило"
    r"odvoen[a-z]*|oddalecen[a-z]*|odalecen[a-z]*|terensk[a-z]*|traktor[a-z]*|"
    # a dirt / gravel / field track is not the road the owner needs: "земјен пат", "макадам", "полски пат"
    r"zemjen[a-z]*|makadam[a-z]*|poljsk[a-z]*|"
    # on paper only: "проектирана улица", "според ДУП ќе има", "во процедура за струја"
    r"proektiran[a-z]*|spored dup|spored plan[a-z]*|procedur[a-z]*|vo postapka")
# Hedges that only qualify what comes right after them (checked in the 3 words before):
# "ima struja i priprema za voda" hedges the water, not the power.
_HEDGE_BEFORE = re.compile(
    r"priprema za|sprovedliv[a-z]*|trasiran[a-z]*|obezbeduvanj[a-z]*|spored dup|spored plan[a-z]*|proektiran[a-z]*|"
    # a distance from the road is not a road: "50m od glaven pat", "на 1.1 км од главен пат"
    r"(?:\d+ ?k?m|metri|metra|kilometr[a-z]*) od(?: (?:glav|regional|magistral|asfalt)[a-z]*)?$")
_HEDGE_WINDOW = 5      # words on either side of the utility word
# Place names that contain a utility word but say nothing about utilities.
_PLACE_NOISE = re.compile(
    r" (?:(?:kisela|bela|studena|topla|crna|gorna|dolna|sredna|smrdliva|zelena|sveta|mrtva|lesna) voda|patiska|crven pat)"
    r"|(?: vikano| lokalitet) [a-z]+ voda(?= )")       # "место викано Горна вода"
_NEGATORS = r"(?:nema|nemame|bez|pa|nedostasuva)"
# Words allowed between a negator and the utility it negates.
_LIST_GLUE = r"(?: (?:i|ili|ni|niti|nitu|dhe|e|struja|elektrika|voda|vodovod|pat|asfalt|kanalizacija|telefon|internet))*"


def _utility_regex(key: str) -> re.Pattern:
    stems = "|".join(map(re.escape, _UTILITY_STEMS[key]))
    words = "|".join(map(re.escape, _UTILITY_WORDS[key]))
    # "до неа водат два пата" (they lead) is not water
    parts = [f"(?:{stems})(?!t(?![a-z]))[a-z]*"] if stems else []
    if words:
        parts.append(f"(?:{words})(?![a-z0-9])")
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(parts) + ")")


_UTILITY_RE = {k: _utility_regex(k) for k in _UTILITY_STEMS}


def _confirmed(n: str, rx: re.Pattern, skip_labels: bool = False) -> bool:
    """True when at least one mention of `rx` has no hedge ("се очекува", "можност за",
    "во близина", "услови за") within a few words of it in the same clause.
    With skip_labels, a word standing alone at the start of its clause
    ("Инфраструктура: ...") is a heading, not a statement."""
    for m in rx.finditer(n):
        wb = n[:m.start()].split()[-_HEDGE_WINDOW:]
        wa = n[m.end():].split()[:_HEDGE_WINDOW]
        if "." in wb:                                     # don't look past a sentence / clause break
            wb = wb[len(wb) - wb[::-1].index("."):]
        if "." in wa:
            wa = wa[:wa.index(".")]
        near_before = wb[-3:]
        # A clause of nothing but utility words ("струја", "асфалтиран пат") is a list item.
        if all(w in _LIST_WORDS or w == _STOP or any(r.fullmatch(w) for r in _UTILITY_RE.values())
               for w in wb + wa):
            if skip_labels:
                continue
            # A bare item in a comma list ("струја, вода, канализација во близина",
            # "спроведливост на вода, струја, трасиран пат") shares its neighbours' hedge —
            # within the same sentence ("…пат, струја, вода. Во постапка е маркицата").
            wb = n[:m.start()].split()[-7:]
            wa = n[m.end():].split()[:7]
            if _STOP in wb:
                wb = wb[len(wb) - wb[::-1].index(_STOP):]
            if _STOP in wa:
                wa = wa[:wa.index(_STOP)]
            # Look ahead for a trailing hedge ("…, канализација во близина"), behind only for
            # a leading one ("спроведливост на вода, струја") — "блиску има приклучок за
            # вода, асфалтен пат" still has the road.
            if not (_HEDGE.search(" ".join(wa)) or _HEDGE_BEFORE.search(" ".join(wb))):
                return True
            continue
        if not (_HEDGE.search(" ".join(wb)) or _HEDGE.search(" ".join(wa))
                or _HEDGE_BEFORE.search(" ".join(near_before))):
            return True
    return False


_LIST_WORDS = {"i", "ili", "dhe", "e", "so", "kanalizacija", "kanalizaciona", "kanalizacioni", "odvod", "mreza",
               "gradska", "gradski", "gradsko", "telefon", "internet", "asfaltiran", "asfalten", "pristapen"}
_STOP = "zzstop"
_SENTENCE_END = re.compile(r"[.!?]+(?=\s|[A-ZА-ЯЃЌЅЈЉЊЏ])|\n")
_ALL_INFRA_RE = re.compile(r"(?<![a-z0-9])(?:" + "|".join(map(re.escape, _ALL_INFRA.words)) + ")[a-z]*")


def detect_utilities(text: str) -> dict:
    """Return {'electricity': True/False/None, 'water': ..., 'road': ...}.

    True = mentioned, False = explicitly absent ("нема струја и вода"),
    None = not mentioned at all, or only hedged ("вода во близина", "можност за
    приклучок", "на 50 m од главен пат", "трасиран пат").
    """
    # norm() turns commas and full stops alike into " . "; mark real sentence ends first.
    text = _SENTENCE_END.sub(f". {_STOP} ", text or "")
    n = _PLACE_NOISE.sub(" ", norm(text))
    # "Комунално опремен" / "има инфраструктура" covers all three — but not a bare
    # "Инфраструктура:" heading, nor "ќе биде комунално опремен".
    all_infra = _confirmed(n, _ALL_INFRA_RE, skip_labels=True)
    out = {}
    for key, rx in _UTILITY_RE.items():
        # A negator before a run of utility words: "nema struja i voda".
        neg = re.search(r"(?<![a-z0-9])" + _NEGATORS + _LIST_GLUE + " " + rx.pattern[len("(?<![a-z0-9])"):], n)
        if neg:
            out[key] = False
            continue
        # Judge every mention on its own. "струја има, вода во близина" → power yes, water unknown.
        if _confirmed(n, rx) or (all_infra and not rx.search(n)):
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
_BUILDING_LAND = KeywordSet(["gradezno", "gradezen", "gradezna dozvola", "gradezna markica", "gradezna parcela",
                             "gradezna zona", "urbanisticki plan", "vo dup", "vo plan", "truall", "toke ndertimi",
                             # zoning class A = housing: "А-зона наменета за домување", "A1 za domuvanje"
                             "a zona", "a-zona", "zona a", "a1 za domuv", "a 1 za domuv", "namena za domuv",
                             "nameneta za domuv", "namenet za domuv", "zona za domuv", "markica za kuk",
                             "so markica"])
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
