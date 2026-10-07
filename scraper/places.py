"""Neighbourhood → municipality gazetteer for Skopje.

Sellers often pick the wrong district in the form (a flat in Ченто filed under
"Скопје Центар"), which wrecks "below market" comparisons. When the title
names a neighbourhood, that wins.
"""
from __future__ import annotations

from .text import KeywordSet, norm

SKOPJE = "Скопје"
CENTAR = "Скопје Центар"

# municipality (as Reklama5 names it) → neighbourhood words (Cyrillic + Latin, normalised at load)
NEIGHBOURHOODS = {
    # (not the bare word "центар": "10 min do centar" says nothing about the district)
    "Скопје Центар": ["дебар маало", "debar maalo", "буњаковец", "bunjakovec", "кеј", "kej", "капиштец", "kapistec",
                      "плоштад", "plostad", "стара железничка", "stara zeleznicka", "строг центар", "strog centar"],
    "Карпош": ["карпош", "karpos", "тафталиџе", "taftalidze", "влае", "vlae", "пржино", "przino", "злокуќани",
               "zlokukani", "козле", "kozle", "нерези", "nerezi", "буковиќ", "bukovik", "западен булевар"],
    "Аеродром": ["аеродром", "aerodrom", "мичурин", "micurin", "лисиче", "lisice", "ново лисиче", "jane sandanski",
                 "јане сандански"],
    "Кисела Вода": ["кисела вода", "kisela voda", "рампа", "rampa", "расадник", "rasadnik", "пинтија", "pintija",
                    "усје", "usje", "драчево", "dracevo", "припор", "pripor", "тефериче", "teferic"],
    "Гази Баба": ["гази баба", "gazi baba", "ченто", "cento", "автокоманда", "avtokomanda", "хиподром", "hipodrom",
                  "маџари", "madzari", "топанско поле", "topansko pole", "сингелиќ", "singelik", "железара", "zelezara",
                  "стајковци", "stajkovci", "идризово", "idrizovo", "јурумлери", "jurumleri", "трубарево", "trubarevo"],
    "Чаир": ["чаир", "cair", "топаана", "topaana", "бит пазар", "bit pazar"],
    "Бутел": ["бутел", "butel", "радишани", "radisani"],
    "Ѓорче Петров": ["ѓорче петров", "gorce petrov", "хром", "hrom", "кучково", "kuckovo", "волково", "volkovo",
                     "никиштане"],
    "Сарај": ["сарај", "saraj"],
    "Шуто Оризари": ["шуто оризари", "suto orizari", "шутка", "sutka"],
    # Outside the city proper — compare separately, never with Centar.
    "Илинден": ["илинден", "ilinden"],
    "Петровец": ["петровец", "petrovec"],
    "Сопиште": ["сопиште", "sopiste", "соње", "sonje"],
    "Студеничани": ["студеничани", "studenicani"],
    "Арачиново": ["арачиново", "aracinovo"],
    "Зелениково": ["зелениково", "zelenikovo"],
    "Чучер Сандево": ["чучер", "cucer", "бањане", "banjane"],
}
_SETS = {d: KeywordSet(words, whole_word=True) for d, words in NEIGHBOURHOODS.items()}
# Words that show an ad really is central (otherwise "Центар" is often the form default).
CENTRAL_HINTS = KeywordSet(NEIGHBOURHOODS[CENTAR] + ["во центар", "vo centar", "градски парк", "gradski park",
                                                      "универзална сала", "мала станица", "mala stanica"])
# Other towns / resorts: a "Skopje" ad naming one of these is filed in the wrong city.
_OTHER_PLACES = KeywordSet(["охрид", "ohrid", "струга", "struga", "маврово", "mavrovo", "битола", "bitola",
                            "куманово", "kumanovo", "тетово", "tetovo", "прилеп", "prilep", "велес", "veles",
                            "штип", "stip", "струмица", "strumica", "гевгелија", "gevgelija", "дојран", "dojran",
                            "крушево", "krusevo", "берово", "berovo", "попова шапка", "popova sapka", "кичево",
                            "kicevo", "гостивар", "gostivar", "кавадарци", "kavadarci", "кочани", "kocani"],
                           whole_word=True)


def infer_district(title: str | None, city: str | None) -> str | None:
    """The Skopje municipality named in the title, if exactly one is named."""
    if city != SKOPJE or not title:
        return None
    t = norm(title)
    hits = {d for d, ks in _SETS.items() if ks.any(t)}
    return hits.pop() if len(hits) == 1 else None


def other_town(listing: dict) -> bool:
    """A Skopje ad whose title names another town (filed in the wrong city)."""
    return listing.get("city") == SKOPJE and _OTHER_PLACES.any(norm(listing.get("title")))


# Town centres (lat, lng) for the GPS sanity check.
CITY_CENTRES = {
    "Скопје": (41.9961, 21.4317), "Битола": (41.0297, 21.3292), "Куманово": (42.1322, 21.7144), "Прилеп": (41.3464, 21.5542),
    "Тетово": (42.0106, 20.9714), "Велес": (41.7156, 21.7756), "Штип": (41.7362, 22.1958), "Охрид": (41.1172, 20.8016),
    "Гостивар": (41.7964, 20.9083), "Струмица": (41.4375, 22.6431), "Кавадарци": (41.4331, 22.0119), "Кочани": (41.9164, 22.4125),
    "Кичево": (41.5128, 20.9631), "Струга": (41.1778, 20.6783), "Радовиш": (41.6383, 22.4647), "Гевгелија": (41.1392, 22.5025),
    "Дебар": (41.5250, 20.5272), "Крива Паланка": (42.2019, 22.3319), "Свети Николе": (41.8653, 21.9428), "Неготино": (41.4836, 22.0906),
    "Делчево": (41.9661, 22.7747), "Виница": (41.8828, 22.5092), "Ресен": (41.0894, 21.0122), "Пробиштип": (42.0031, 22.1783),
    "Берово": (41.7072, 22.8567), "Кратово": (42.0789, 22.1811), "Крушево": (41.3697, 21.2483), "Македонски Брод": (41.5136, 21.2153),
    "Валандово": (41.3172, 22.5614), "Демир Хисар": (41.2211, 21.2031),
}


def _km(a, b, c, d) -> float:
    import math
    p1, p2 = math.radians(a), math.radians(c)
    x = math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(math.radians(d - b))
    return 6371 * math.acos(max(-1.0, min(1.0, x)))


def refile_by_gps(listing: dict, max_km: float = 40) -> bool:
    """If the ad's pin is far from the filed town, re-file it under the nearest
    town (within 25 km) or mark the town unknown. Returns True when changed."""
    lat, lng, city = listing.get("lat"), listing.get("lng"), listing.get("city")
    if not (lat and lng) or city not in CITY_CENTRES:
        return False
    if _km(lat, lng, *CITY_CENTRES[city]) <= max_km:
        return False
    nearest = min(CITY_CENTRES, key=lambda c: _km(lat, lng, *CITY_CENTRES[c]))
    listing["city_form"] = city
    listing["city"] = nearest if _km(lat, lng, *CITY_CENTRES[nearest]) <= 25 else None
    listing["district"] = None
    return True


def fix_districts(listings: list[dict]) -> int:
    """Override seller-picked districts the title contradicts; returns how many changed."""
    changed = 0
    for l in listings:
        d = infer_district(l.get("title"), l.get("city"))
        if d and d != l.get("district"):
            l["district_form"] = l.get("district")
            l["district"] = d
            changed += 1
    return changed


def centar_unconfirmed(listing: dict) -> bool:
    """Filed under Centar but nothing in the ad says central — likely the form default."""
    if listing.get("district") != CENTAR:
        return False
    text = norm(" ".join(filter(None, [listing.get("title"), listing.get("description"), listing.get("address")])))
    return not CENTRAL_HINTS.any(text)
