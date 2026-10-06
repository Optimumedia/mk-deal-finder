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
