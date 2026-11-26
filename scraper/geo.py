"""Altitude and distance to Skopje / Ohrid for land, houses and weekend houses.

Free services from Open-Meteo, no API key (fine for this personal, low-volume use):
- elevation API for exact GPS points (up to 100 per request);
- place search (geocoding) for ads without GPS: the village / locality named in
  the ad ("с. Велгошти", "викенд населба Голиево", "м.в. Чамлак"), in the title
  or the description. Its altitude is marked approximate ("~").
- driving distance and time to Skopje (Macedonia Square) and Ohrid (centre)
  from the public OpenStreetMap routing service (OSRM), many places per
  request, politely spaced.
Every answer is cached in deals.db, so each place is looked up only once.

Honesty rules (location level → what we claim):
- pin / ad text / named village  → altitude (village ones "~") and drive times;
- only the municipality / district (form field) → rough drive times, NO altitude
  (Sopište municipality runs from 300 m to 1,000 m+), stored as "area:<name>";
- only the town (form field, or a town named in the ad) → nothing: no altitude
  and no drive time, stored as "city:<name>". A plot "somewhere around Skopje" is
  never shown at Skopje's 247 m or "0 min from Skopje".
"""
from __future__ import annotations

import difflib
import json
import logging
import re
import time

import requests

from . import maps
from .text import KeywordSet, norm

log = logging.getLogger(__name__)
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
ROUTE_URL = "https://router.project-osrm.org/table/v1/driving/"
CITIES = {"skopje": (41.9961, 21.4317), "ohrid": (41.1172, 20.8016)}     # Macedonia Square, Ohrid centre
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
KINDS = ("land", "house", "weekend_house")
RESET_KEY = "meta:location-levels-v2"       # one-off re-lookup of old village / town fallbacks

# Latin → Cyrillic for place names typed in Latin ("vo Kuckovo" → "Кучково").
_LAT = [("dzh", "џ"), ("dz", "џ"), ("zh", "ж"), ("ch", "ч"), ("sh", "ш"), ("kj", "ќ"), ("gj", "ѓ"), ("lj", "љ"), ("nj", "њ"),
        ("a", "а"), ("b", "б"), ("c", "ц"), ("č", "ч"), ("ć", "ќ"), ("d", "д"), ("e", "е"), ("f", "ф"), ("g", "г"), ("h", "х"),
        ("i", "и"), ("j", "ј"), ("k", "к"), ("l", "л"), ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"), ("r", "р"), ("s", "с"),
        ("š", "ш"), ("t", "т"), ("u", "у"), ("v", "в"), ("z", "з"), ("ž", "ж")]

_L = "A-Za-zА-Ша-шЃЌЅЈЉЊЏѓќѕјљњџŠŽČĆšžčćËëÇç"
_WORD = rf"[{_L}][{_L}]{{2,}}"
# Two-word village names: "Долно Оризари", "Горно Соње", "Свети Николе", "Стар Дојран".
_PREFIXES = ("долно горно ново старо мало големо долни горни нова стара мали големи стар стари нов свети св "
             "dolno gorno novo staro malo golemo dolni gorni nova stara mali golemi star stari nov sveti sv").split()
_NAME = rf"((?:(?:{'|'.join(_PREFIXES)})\.?\s+)?{_WORD})"
_NOT_AFTER_LETTER = r"(?<![^\W\d_])"
_VILLAGE_WORD = r"(?:с\.|s\.|село|selo|населба|naselba|нас\.|nas\.)?\s*"
# Strong markers name a settlement wherever they appear (title or description).
_STRONG = re.compile(
    _NOT_AFTER_LETTER + r"(?:викенд\s+населба(?:та)?|vikend\s+naselba(?:ta)?|населено\s+место|naseleno\s+mesto|"
    r"атарот\s+на\s+(?:село|с\.)?|atarot\s+na\s+(?:selo|s\.)?|"
    r"селото|seloto|село|selo|с\.|s\.|населба(?:та)?|naselba(?:ta)?|нас\.|nas\.)\s*" + _NAME, re.I)
# Locality names ("месност Чамлак", "м.в. Брдуница") — small, often not geocodable, tried after villages.
_LOCALITY = re.compile(_NOT_AFTER_LETTER + r"(?:месност(?:а)?|mesnost(?:a)?|м\.\s?в\.|m\.\s?v\.|место\s+викано|"
                       r"mesto\s+vikano|локалитет|lokalitet)\s*" + _NAME, re.I)
# Cadastral municipality in free text: "КО Стајковци", "K.O. Cresevo" (upper case only — "ko" is a common word).
_KO = re.compile(r"(?<![\w.])(?:КО|KO|К\.О\.|K\.O\.)\s+" + _NAME)
_MUNI = re.compile(_NOT_AFTER_LETTER + r"(?:општина|opstina|opština|opshtina|komuna|komuna e|комуна)\s+" + _NAME, re.I)
# Weak markers ("плац во Кучково", "plac na Ponikva", "kaj Luk Oil") — title only.
_WEAK = re.compile(_NOT_AFTER_LETTER + r"(?:во|vo|на|na|кај|kaj|до|do|под|pod|над|nad|близу|blizu|код|kod|"
                   r"покрај|pokraj|në|ne)\s+" + _VILLAGE_WORD + _NAME, re.I)
_CAPITALISED = re.compile(rf"(?<![{_L}])([A-ZА-ШЃЌЅЈЉЊЏŠŽČĆ][{_L}]{{3,}})")
_STOP = {norm(w).strip() for w in """
    центар centar центарот centarot град grad градот gradot близина blizina околина okolina околината okolinata
    скопје skopje плац plac плацот placot плацеви placevi одлична odlicna населба naselba викенд vikend куќа kuka kukja
    стан stan продажба prodazba викендица vikendica вила vila имот imot имотот imotot земјиште zemjiste zemjishte
    парцела parcela парцели parceli нива niva ниви nivi лозје lozje лозја lozja продавам prodavam продава prodava
    издавам izdavam итно itno хитно hitno ekskluzivno ексклузивно nova нова nov нов село selo селото seloto пат pat
    патот patot автопат avtopat главен glaven главниот glavniot реон reon градежен gradezen градежно gradezno улица ulica
    локација lokacija место mesto агенција agencija супер super атрактивна atraktivna убава ubava мирна mirna прва prva
    линија linija поглед pogled шума suma шуми sumi двор dvor плацови куќи kuki продаваат prodavaat povolno поволно
    region регион општина opstina komuna atar атар атарот atarot samiot самиот самото samoto blizu близу
    париз pariz
    земјоделски zemjodelski земјоделско zemjodelsko влез vlez рати rati под pod над nad до do кај kaj во vo на na
    од od со so за za покрај pokraj нас nas поглед само samo поле pole горна gorna долна dolna стара stara ново novo
    мала mala мал mal голем golem голема golema бели beli бела bela црн crn црна crna последна posledna вкупна vkupna
    луксузна luksuzna луксузен luksuzen модерен moderen модерна moderna добра dobra градски gradski индустриска
    industriska индустриска зона zona река reka вонград vongrad каменот забави ул ul мвр гази gazi бит bit нова
    стефан алек царев демир делцев дел del јени ката kata автобус avtobus автобуска avtobuska маало maalo маал maal
    индустријски industrijski индустриска industriska желез zelez железара zelezara желе zele обиколница obikolnica главна glavna главната glavnata
    sale bailiff house land plot for the agencija""".split()}
# Skopje's own municipalities: a district, never a village (and "Centar"/"Karpoš" were geocoded as villages).
_SKOPJE_URBAN = {norm(w).strip() for w in ["Центар", "Карпош", "Аеродром", "Кисела Вода", "Гази Баба", "Чаир", "Бутел",
                                            "Ѓорче Петров", "Сарај", "Шуто Оризари"]}
_URBAN_WORDS = {w for n in _SKOPJE_URBAN if " " in n for w in n.split()}
# Towns small and high enough that "in town" is a fair approximate altitude.
_HIGH_TOWNS = {norm(w).strip() for w in ["Крушево", "Берово", "Пехчево"]}


def to_cyrillic(word: str) -> str:
    w = word.lower()
    if re.search(r"[а-шѓќѕјљњџ]", w):
        return word
    for a, b in _LAT:
        w = w.replace(a, b)
    return w[:1].upper() + w[1:]


def _clean(name: str) -> str:
    """'DOLNO ORIZARI' → 'Долно Оризари', 'с.Курија' stays 'Курија'."""
    words = [to_cyrillic(w) for w in name.replace(".", " ").split()]
    return " ".join(w[:1].upper() + w[1:].lower() for w in words if w)


# Named places that are often written in Latin or hidden in the text — with their proper spelling.
_MOUNTAIN_SPELLING = {
    "mavrovo": "Маврово", "popova sapka": "Попова Шапка", "krusevo": "Крушево", "pelister": "Пелистер",
    "brajcino": "Брајчино", "galicica": "Галичица", "vevcani": "Вевчани", "berovo": "Берово", "pehcevo": "Пехчево",
    "ponikva": "Пониква", "kratovo": "Кратово", "nikiste": "Никиштане", "lazaropole": "Лазарополе",
    "galicnik": "Галичник", "nizepole": "Нижеполе", "trnovo": "Трново", "magarevo": "Магарево",
    "ljubaniste": "Љубаништа", "velgosti": "Велгошти", "leunovo": "Леуново", "mavrovi anovi": "Маврови Анови",
    "dojran": "Дојран", "meseista": "Мешеишта", "otesevo": "Отешево", "pretor": "Претор", "elsani": "Елшани",
    "stenje": "Стење", "kuckovo": "Кучково", "patiska reka": "Патишка Река", "nezilovo": "Нежилово",
    "jablanica": "Јабланица", "kozjak": "Козјак", "vratnica": "Вратница", "rostuse": "Ростуше",
}
_MOUNTAIN_PLACES = KeywordSet(list(_MOUNTAIN_SPELLING), whole_word=True)


def _town_names() -> set[str]:
    from .places import CITY_CENTRES
    return {norm(t).strip() for t in CITY_CENTRES} | {"sveti nikole", "makedonski brod", "kriva palanka", "demir hisar"}


def ranked_candidates(listing: dict) -> list[tuple[str, str]]:
    """(name, level) to try, most specific first. level: 'village' (a settlement or
    locality named in the ad), 'town' (a town named in the ad), 'area' (the district /
    municipality from the form), 'city' (only the town the ad is filed under)."""
    title = listing.get("title") or ""
    desc = listing.get("description") or ""
    towns = _town_names()
    out: list[tuple[str, str]] = []
    # "општина Илинден, Ајватовци": the municipality is an area, the village is Ajvatovci.
    munis = {norm(_clean(m.group(1))).strip() for m in _MUNI.finditer(title + " \n " + desc)}

    def add(raw: str | None, level: str):
        if not raw:
            return
        name = _clean(raw)
        n = norm(name).strip()
        last = n.split(" ")[-1] if n else ""
        if not n:
            return
        if level == "area":                 # the form's district is a real name, never noise
            out.append((raw, level))
            return
        if n in _STOP or n in _PREFIXES or (last in _STOP and last not in ("selo", "reka", "pole")) or len(n) < 3:
            return          # "Ново Лагово" and "Ново Село" are villages; "plac vo Samo" is not
        if " " not in n and n in _URBAN_WORDS and listing.get("city") == "Скопје":
            return          # half of "Gorce Petrov" / "Kisela Voda" (Petrov ≠ Petrovec)
        if level == "village" and n in towns and n not in _HIGH_TOWNS:
            level = "town"
        elif n in _SKOPJE_URBAN or (n in munis and level == "village"):
            level = "area"
        out.append((name, level))

    for m in _STRONG.finditer(title):
        add(m.group(1), "village")
    for m in _WEAK.finditer(title):
        add(m.group(1), "village")
    for word in _MOUNTAIN_PLACES.find(norm(title)):
        add(_MOUNTAIN_SPELLING.get(word, word.title()), "village")
    for m in _STRONG.finditer(desc):
        add(m.group(1), "village")
    for m in _KO.finditer(title + " \n " + desc):
        add(m.group(1), "village")
    for word in _MOUNTAIN_PLACES.find(norm(desc)):
        add(_MOUNTAIN_SPELLING.get(word, word.title()), "village")
    for m in _LOCALITY.finditer(title + " \n " + desc):
        add(m.group(1), "village")
    # Other capitalised words in the title ("…во близина на Охрид, Мешеишта", "Se prodava plac - Pripor").
    words = _CAPITALISED.findall(title)[:6]
    i = 0
    while i < len(words):
        w = words[i] if words[i][1:].islower() else words[i].capitalize()      # "PONIKVA" → "Ponikva"
        if norm(w).strip() in _PREFIXES and i + 1 < len(words):              # "Sveti Nikole", "Gorno Sonje"
            w, i = f"{w} {words[i + 1].capitalize()}", i + 1
        if w != listing.get("city"):
            add(w, "village")
        i += 1
    ko = maps.cadastral_area((listing.get("extra") or {}).get("note"))
    if ko:
        add(ko, "village")
    address = (listing.get("address") or "").strip()
    if address and len(address.split()) <= 3 and not re.search(r"\d", address):
        add(address, "village")
    district = (listing.get("district") or "").replace("Скопје ", "")
    if district and district != listing.get("city"):
        add(district, "area")
    if listing.get("city"):
        out.append((listing["city"], "city"))
    # Villages first (in text order), then towns named in the ad, the form district, the form town.
    seen, uniq = set(), []
    for name, level in sorted(out, key=lambda x: ("village", "town", "area", "city").index(x[1])):
        if name not in seen:
            seen.add(name)
            uniq.append((name, level))
    return uniq


def place_candidates(listing: dict) -> list[str]:
    """Names to try, most specific first."""
    return [n for n, _ in ranked_candidates(listing)]


# Villages the geocoder only knows under another name (it answers "Волково" with Vučji Dol, NW Skopje).
_ALIASES = {"volkovo": "vuci dol", "dojran": "star dojran"}


def _same_name(asked: str, found: str | None) -> bool:
    """The geocoder is fuzzy: 'Долно' must not become 'Долно Соње'."""
    a, b = norm(asked).strip(), norm(found).strip()
    if not a or not b:
        return False
    if _ALIASES.get(a) == b:
        return True
    if a == b or b.startswith(a + " ") and len(b) - len(a) <= 2:        # "Карпош" → "Карпош 2"
        return True
    # Spelling variants ("Мирковци" / Mirkovce, "Соње" / "Солње"), not another word ("Петров" ≠ "Петровец").
    return abs(len(a) - len(b)) <= 1 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.85


_MISSING = object()      # not cached and no lookups left this run


class Geo:
    def __init__(self, db, delay: float = 0.4, results: int = 10):
        self.db, self.delay, self.results = db, delay, results
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "mk-deal-finder/1.0 (personal property research)"
        self.calls = 0
        db.conn.execute("""CREATE TABLE IF NOT EXISTS geo_cache (
            query TEXT PRIMARY KEY, lat REAL, lng REAL, elevation REAL, name TEXT, at TEXT)""")
        # Every town / village result for a name, so the one nearest the ad's town can be picked.
        db.conn.execute("CREATE TABLE IF NOT EXISTS geo_results (query TEXT PRIMARY KEY, results TEXT, at TEXT)")

    def _get(self, url: str, params: dict) -> dict | None:
        time.sleep(self.delay)
        self.calls += 1
        try:
            r = self.s.get(url, params=params, timeout=20)
            return r.json() if r.ok else None
        except (requests.RequestException, ValueError) as e:
            log.warning("geo request failed: %s", e)
            return None

    def _fetch(self, name: str, near: tuple | None, max_km: float) -> list[tuple]:
        """Town / village results for a name (Cyrillic, then Latin when nothing near)."""
        found: list[tuple] = []
        for q in dict.fromkeys([name, norm(name).strip().title()]):
            data = self._get(GEOCODE_URL, {"name": q, "count": self.results, "language": "mk", "countryCode": "MK"}) or {}
            # Towns and villages only — never a peak or landmark of the same name
            # ("Ilinden" is also a 2,511 m summit).
            found += [(x["latitude"], x["longitude"], x["elevation"], x.get("name"))
                      for x in data.get("results") or []
                      if str(x.get("feature_code", "")).startswith(("PPL", "ADM")) and x.get("elevation") is not None]
            good = [h for h in found if _same_name(name, h[3]) or _same_name(q, h[3])]
            if good and (near is None or any(_km(h[0], h[1], *near) <= max_km for h in good)):
                break
        return list(dict.fromkeys(found))

    def place(self, name: str, near: tuple | None = None, max_km: float = 40, fetch: bool = True):
        """(lat, lng, elevation, found name) for a place in North Macedonia, cached.
        With `near`, the namesake closest to that point (there are several Volkovos).
        Returns _MISSING when the name isn't cached and `fetch` is False."""
        key = "place:" + name.lower()
        row = self.db.conn.execute("SELECT results FROM geo_results WHERE query=?", (key,)).fetchone()
        if row:
            hits = [tuple(h) for h in json.loads(row[0])]
        else:
            old = self.db.conn.execute("SELECT lat, lng, elevation, name FROM geo_cache WHERE query=?", (key,)).fetchone()
            if old and (old[0] is None or near is None or _km(old[0], old[1], *near) <= max_km):
                hits = [tuple(old)] if old[0] is not None else []      # old single answer is good enough
            elif not fetch:
                return _MISSING
            else:
                hits = self._fetch(name, near, max_km)
                from .db import now
                self.db.conn.execute("INSERT OR REPLACE INTO geo_results VALUES (?,?,?)", (key, json.dumps(hits), now()))
                best = hits[0] if hits else (None, None, None, None)
                self.db.conn.execute("INSERT OR REPLACE INTO geo_cache VALUES (?,?,?,?,?,?)", (key, *best, now()))
        hits = [h for h in hits if _same_name(name, h[3])]
        if not hits:
            return None
        if near:
            hits.sort(key=lambda h: _km(h[0], h[1], *near))
        return hits[0]

    def points(self, coords: list[tuple[float, float]]) -> list[float | None]:
        """Elevation for exact GPS points (batches of 100)."""
        out: list[float | None] = []
        for i in range(0, len(coords), 100):
            chunk = coords[i:i + 100]
            data = self._get(ELEVATION_URL, {"latitude": ",".join(f"{a:.5f}" for a, _ in chunk),
                                             "longitude": ",".join(f"{b:.5f}" for _, b in chunk)}) or {}
            el = data.get("elevation") or [None] * len(chunk)
            out += [float(x) if x is not None else None for x in el]
        return out


def _km(a, b, c, d) -> float:
    import math
    p1, p2 = math.radians(a), math.radians(c)
    x = math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(math.radians(d - b))
    return 6371 * math.acos(max(-1.0, min(1.0, x)))


def drop_default_pins(db, max_share: int = 3) -> int:
    """A pin shared by many ads is the site's default for a town, not a location."""
    shared = [(a, b) for a, b, n in db.conn.execute(
        "SELECT ROUND(lat, 5), ROUND(lng, 5), COUNT(*) FROM listings WHERE lat IS NOT NULL "
        "GROUP BY 1, 2 HAVING COUNT(*) > ?", (max_share,))]
    n = 0
    for a, b in shared:
        cur = db.conn.execute(
            "UPDATE listings SET lat=NULL, lng=NULL, elevation=CASE WHEN elevation_src='gps' THEN NULL ELSE elevation END, "
            "elevation_src=CASE WHEN elevation_src='gps' THEN NULL ELSE elevation_src END, "
            "skopje_km=NULL, skopje_min=NULL, ohrid_km=NULL, ohrid_min=NULL "
            "WHERE ROUND(lat, 5)=? AND ROUND(lng, 5)=?", (a, b))
        n += cur.rowcount
    if n:
        db.conn.commit()
        log.info("dropped %d default map pins (%d locations shared by >%d ads)", n, len(shared), max_share)
    return n


def _anywhere(name: str, known: KeywordSet | None = None) -> bool:
    """Named resorts, mountain areas and other towns: valid wherever the ad is filed."""
    from .places import _OTHER_PLACES
    return _OTHER_PLACES.any(norm(name)) or bool(known and known.any(norm(name)))


def near_city(hit, city: str | None, max_km: float, name: str = "", known: KeywordSet | None = None) -> bool:
    """Is a place hit plausibly in the town the ad is filed under? Named resorts and
    mountain areas (Mavrovo, Ponikva…) are often filed under Skopje: those always count."""
    from .places import CITY_CENTRES
    if city not in CITY_CENTRES:
        return True
    if _anywhere(name, known):
        return True
    return _km(hit[0], hit[1], *CITY_CENTRES[city]) <= max_km


def forget_far_places(db, max_km: float, known: KeywordSet | None = None) -> int:
    """Village altitudes taken from a namesake far from the ad's town: look them up again."""
    n = 0
    for r in db.conn.execute("SELECT id, city, approx_lat, approx_lng, elevation_src FROM listings "
                             "WHERE (elevation_src LIKE 'place:%' OR elevation_src LIKE 'area:%') "
                             "AND approx_lat IS NOT NULL").fetchall():
        if not near_city((r["approx_lat"], r["approx_lng"]), r["city"], max_km, r["elevation_src"].split(":", 1)[1], known):
            db.conn.execute("UPDATE listings SET elevation=NULL, elevation_src=NULL, approx_lat=NULL, approx_lng=NULL, "
                            "skopje_km=NULL, skopje_min=NULL, ohrid_km=NULL, ohrid_min=NULL WHERE id=?", (r["id"],))
            n += 1
    return n


def forget_lost_pins(db) -> int:
    """Pin altitude ('gps') whose pin is gone (a detail re-read without a map, a dropped
    default pin): the altitude and drive times belonged to that pin — locate it again."""
    cur = db.conn.execute("UPDATE listings SET elevation=NULL, elevation_src=NULL, approx_lat=NULL, approx_lng=NULL, "
                          "skopje_km=NULL, skopje_min=NULL, ohrid_km=NULL, ohrid_min=NULL "
                          "WHERE elevation_src='gps' AND (lat IS NULL OR lng IS NULL)")
    if cur.rowcount:
        db.conn.commit()
        log.info("geo: %d pin altitudes had lost their pin — locating them again", cur.rowcount)
    return cur.rowcount


def reset_fallbacks_once(db) -> int:
    """One-off: re-do every village / town lookup with the better place-name reading
    (description villages, two-word names, nearest namesake, no city altitude)."""
    if db.conn.execute("SELECT 1 FROM geo_cache WHERE query=?", (RESET_KEY,)).fetchone():
        return 0
    cur = db.conn.execute("UPDATE listings SET elevation=NULL, elevation_src=NULL, approx_lat=NULL, approx_lng=NULL, "
                          "skopje_km=NULL, skopje_min=NULL, ohrid_km=NULL, ohrid_min=NULL "
                          "WHERE lat IS NULL AND (elevation_src LIKE 'place:%' OR elevation_src = 'none')")
    from .db import now
    db.conn.execute("INSERT OR REPLACE INTO geo_cache (query, at) VALUES (?, ?)", (RESET_KEY, now()))
    db.conn.commit()
    if cur.rowcount:
        log.info("geo: re-reading the place names of %d listings", cur.rowcount)
    return cur.rowcount


def locate(geo: Geo, listing: dict, max_km: float, known: KeywordSet | None, fetch: bool = True):
    """Best location for an ad without a usable pin → (level, name, hit) or _MISSING.
    level: 'village' (altitude + routes), 'area' (routes only), 'city' (nothing), 'none'."""
    from .places import CITY_CENTRES
    city = listing.get("city")
    near = CITY_CENTRES.get(city)
    town = None
    for name, level in ranked_candidates(listing):
        if level in ("town", "city"):
            # A town is too big to claim an altitude or a drive time for the plot:
            # keep looking for a village, else report the town only.
            town = town or name
            continue
        if level == "area" and town:
            continue        # the ad names another town: the form's district is not where it is
        hit = geo.place(name, near=None if _anywhere(name, known) else near, max_km=max_km, fetch=fetch)
        if hit is _MISSING:
            return _MISSING
        if hit and not near_city(hit, city, max_km, hit[3] or name, known):
            continue        # a namesake village elsewhere
        if hit:
            if level == "village" and near and _km(hit[0], hit[1], *near) < 1.5 and norm(hit[3]).strip() in _town_names():
                town = town or hit[3] or name                # the "village" is the town itself
                continue
            return level, (name if norm(name).strip() in _ALIASES else hit[3] or name), hit
    return ("city", town, None) if town else ("none", None, None)


def enrich(db, cfg: dict) -> int:
    """Fill elevation for active land / houses / weekend houses that don't have it yet."""
    c = cfg.get("geo", {})
    if not c.get("enabled", True):
        return 0
    from .db import days_ago
    max_km, budget = c.get("place_vs_city_km", 40), c.get("max_lookups_per_run", 400)
    geo = Geo(db, results=c.get("geocode_results", 10))
    done = 0
    drop_default_pins(db)
    forget_lost_pins(db)
    reset_fallbacks_once(db)
    known = KeywordSet(cfg["land"]["mountain_keywords"])
    if (far := forget_far_places(db, max_km, known)):
        log.info("geo: %d village altitudes were a namesake far from the ad's town — looking them up again", far)
    rows = db.conn.execute(
        f"""SELECT id, title, description, address, district, city, lat, lng, extra, elevation_src FROM listings
            WHERE kind IN ({','.join('?' * len(KINDS))}) AND abroad = 0 AND last_seen >= ?
              AND (elevation_src IS NULL                                             -- never looked up
                   OR (lat IS NOT NULL AND elevation_src NOT IN ('gps', 'text')))    -- GPS arrived: upgrade
            ORDER BY (kind = 'land') DESC, first_seen DESC""", (*KINDS, days_ago(cfg["scraper"]["stale_after_days"]))
    ).fetchall()
    gps = [r for r in rows if r["lat"] and r["lng"]]
    # A pin that sits far from a village named in the ad is the seller's leftover: use the village.
    from .places import CITY_CENTRES
    kept = []
    for r in gps:
        names = [n for n, level in ranked_candidates(dict(r) | {"extra": {}}) if level == "village"]
        near = None if names and _anywhere(names[0], known) else CITY_CENTRES.get(r["city"])
        hit = geo.place(names[0], near=near, max_km=max_km, fetch=geo.calls < budget) if names else None
        if hit and hit is not _MISSING and near_city(hit, r["city"], max_km, hit[3] or names[0], known) \
                and _km(r["lat"], r["lng"], hit[0], hit[1]) > c.get("pin_vs_place_km", 15):
            db.conn.execute("UPDATE listings SET lat=NULL, lng=NULL, elevation=?, elevation_src=?, approx_lat=?, approx_lng=?, "
                            "skopje_km=NULL, skopje_min=NULL, ohrid_km=NULL, ohrid_min=NULL WHERE id=?",
                            (hit[2], "place:" + (hit[3] or names[0]), hit[0], hit[1], r["id"]))
            done += 1
        else:
            kept.append(r)
    for r, el in zip(kept, geo.points([(r["lat"], r["lng"]) for r in kept])):
        if el is not None:
            # exact pin: its altitude, and recompute routes from the pin instead of the village
            db.conn.execute("UPDATE listings SET elevation=?, elevation_src='gps', approx_lat=NULL, approx_lng=NULL, "
                            "skopje_km=NULL, skopje_min=NULL, ohrid_km=NULL, ohrid_min=NULL WHERE id=?", (el, r["id"]))
            done += 1
    pending = 0
    for r in (r for r in rows if not (r["lat"] and r["lng"])):
        listing = dict(r)
        listing["extra"] = json.loads(r["extra"]) if r["extra"] else {}
        found = locate(geo, listing, max_km, known, fetch=geo.calls < budget)
        if found is _MISSING:
            pending += 1            # out of lookups this run: try again next run
            continue
        level, name, hit = found
        if level == "village":
            db.conn.execute("UPDATE listings SET elevation=?, elevation_src=?, approx_lat=?, approx_lng=? WHERE id=?",
                            (hit[2], "place:" + name, hit[0], hit[1], r["id"]))
        elif level == "area":
            db.conn.execute("UPDATE listings SET elevation=NULL, elevation_src=?, approx_lat=?, approx_lng=? WHERE id=?",
                            ("area:" + name, hit[0], hit[1], r["id"]))
        elif level == "city":
            db.conn.execute("UPDATE listings SET elevation=NULL, elevation_src=?, approx_lat=NULL, approx_lng=NULL, "
                            "skopje_km=NULL, skopje_min=NULL, ohrid_km=NULL, ohrid_min=NULL WHERE id=?",
                            ("city:" + name, r["id"]))
        else:
            db.conn.execute("UPDATE listings SET elevation_src='none' WHERE id=?", (r["id"],))
        done += 1
        if done % 25 == 0:
            db.conn.commit()        # don't hold the database lock for the whole step
    db.conn.commit()
    log.info("altitude: %d listings located (%d lookups, %d waiting for the next run)", done, geo.calls, pending)
    routes(db, cfg, geo)
    return done


def routes(db, cfg: dict, geo: "Geo | None" = None) -> int:
    """Driving km / minutes to Skopje and Ohrid, for listings with a location
    (exact pin or village). Places are rounded to ~100 m and cached."""
    c = cfg.get("geo", {})
    geo = geo or Geo(db)
    db.conn.execute("""CREATE TABLE IF NOT EXISTS route_cache (
        key TEXT PRIMARY KEY, skopje_km REAL, skopje_min REAL, ohrid_km REAL, ohrid_min REAL)""")
    rows = db.conn.execute(
        f"""SELECT id, COALESCE(lat, approx_lat) AS la, COALESCE(lng, approx_lng) AS lo FROM listings
            WHERE kind IN ({','.join('?' * len(KINDS))}) AND skopje_km IS NULL AND abroad = 0
              AND COALESCE(lat, approx_lat) IS NOT NULL""", KINDS).fetchall()
    by_key: dict[str, list] = {}
    for r in rows:
        by_key.setdefault(f"{r['la']:.3f},{r['lo']:.3f}", []).append(r["id"])
    cached = {k: tuple(v) for k, *v in db.conn.execute("SELECT * FROM route_cache")}
    todo = [k for k in by_key if k not in cached]
    batch, requests_made = c.get("routes_per_request", 90), 0
    for i in range(0, len(todo), batch):
        if requests_made >= c.get("max_route_requests_per_run", 10):
            break
        keys = todo[i:i + batch]
        pts = [CITIES["skopje"], CITIES["ohrid"]] + [tuple(map(float, k.split(","))) for k in keys]
        url = ROUTE_URL + ";".join(f"{lo:.5f},{la:.5f}" for la, lo in pts)
        time.sleep(1.5)                                     # public demo server: be gentle
        requests_made += 1
        try:
            data = geo.s.get(url, params={"sources": ";".join(str(j) for j in range(2, len(pts))),
                                          "destinations": "0;1", "annotations": "duration,distance"}, timeout=40).json()
        except (requests.RequestException, ValueError) as e:
            log.warning("routing failed: %s", e)
            break
        if data.get("code") != "Ok":
            log.warning("routing: %s", data.get("message") or data.get("code"))
            break
        for k, dur, dist in zip(keys, data["durations"], data["distances"]):
            val = tuple(round(x / div, 1) if x is not None else None
                        for x, div in ((dist[0], 1000), (dur[0], 60), (dist[1], 1000), (dur[1], 60)))
            db.conn.execute("INSERT OR REPLACE INTO route_cache VALUES (?,?,?,?,?)", (k, *val))
            cached[k] = val
    n = 0
    for k, ids in by_key.items():
        if k in cached:
            db.conn.executemany("UPDATE listings SET skopje_km=?, skopje_min=?, ohrid_km=?, ohrid_min=? WHERE id=?",
                                [(*cached[k], lid) for lid in ids])
            n += len(ids)
    db.conn.commit()
    log.info("distances: %d listings filled (%d routing requests)", n, requests_made)
    return n
