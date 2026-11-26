"""Why you rejected a deal — and how that teaches the researchers.

Every reason has a short code (fits Telegram's 64-byte button data), a label,
the researchers it applies to, and a *detector* that recognises similar
deals. Each time you reject something for a reason, deals that match the
same detector score lower (see penalties()); a few reasons are about data
quality and are reported for fixing instead.

    python -m scraper.learn      → summary + suggested config changes
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from .text import norm

ALL = ("airbnb", "land", "weekend", "flip", "motivated", "auctions")


def _words(*stems: str) -> Callable[[dict], bool]:
    rx = re.compile(r"(?<![a-z0-9])(?:" + "|".join(norm(s).strip() for s in stems) + ")")
    return lambda x: bool(rx.search(norm(" ".join(filter(None, [x.get("title"), x.get("description")])))))


@dataclass(frozen=True)
class Reason:
    code: str
    label: str
    researchers: tuple = ALL
    # Recognises deals like this one (None = the reason is about this listing only).
    similar: Callable[[dict], bool] | None = None
    # "learn" = lower scores of similar deals; "size_min"/"size_max" = learn a size limit;
    # "data" = a data/parsing problem for the developer; "self" = only this listing.
    learn: str = "learn"
    group: str = ""


REASONS: list[Reason] = [
    # Location
    Reason("loc", "Bad location / neighbourhood", group="Location"),
    Reason("far", "Too far from the centre", ("airbnb", "flip", "motivated"), group="Location"),
    # Price
    Reason("prc", "Not really under market / overpriced", group="Price"),
    Reason("pm2", "Price is per m² or misleading", learn="data", group="Price"),
    # Property
    Reason("sml", "Too small", ("airbnb", "flip", "motivated", "auctions", "land", "weekend"), learn="size_min", group="Property"),
    Reason("big", "Too big", ("airbnb", "flip", "motivated", "auctions", "land", "weekend"), learn="size_max", group="Property"),
    Reason("ren", "Needs too much renovation", ("flip", "motivated", "auctions", "airbnb"),
           _words("renovir", "adaptac", "stara gradba", "star stan", "za rusenje"), group="Property"),
    Reason("bsm", "Basement / ground floor / no light", ("airbnb", "flip", "motivated", "auctions"),
           _words("suteren", "сутерен", "podrum", "подрум", "prizem", "приземј", "visoko prizemje", "полуподрум"),
           group="Property"),
    Reason("old", "Old building / no lift", ("airbnb", "flip", "motivated"),
           _words("bez lift", "без лифт", "nema lift", "stara zgrada", "стара зграда"), group="Property"),
    Reason("nhm", "Not a home (office, shop, garage…)", ("airbnb", "flip", "motivated", "auctions"),
           _words("deloven", "деловен", "kancelar", "канцелар", "lokal", "локал", "garaz", "гаража", "magacin"),
           group="Property"),
    # Legal
    Reason("tit", "No clean title deed (имотен лист)", group="Legal",
           similar=_words("bez imoten", "без имотен", "vo postapka", "во постапка", "legaliz", "легализ")),
    Reason("shr", "Ownership share / co-owners", group="Legal",
           similar=_words("idealen del", "идеален дел", "sosopstven", "сосопствен")),
    Reason("ill", "Illegal / not legalised build", ("flip", "motivated", "auctions", "land", "weekend"),
           _words("legaliz", "легализ", "divo", "диво"), group="Legal"),
    # Land
    # Learned from: plots whose ad only says the utility is "nearby" / "can be connected",
    # and plots in the same area get the question added (see penalties()).
    Reason("noe", "No electricity", ("land", "weekend",), group="Land"),
    Reason("now", "No water", ("land", "weekend",), group="Land"),
    Reason("nor", "No road access", ("land", "weekend",), group="Land"),
    Reason("agr", "Agricultural — can't build", ("land", "weekend", "auctions", "motivated"),
           _words("zemjodel", "земјодел", "niva", "нива", "lozje", "лозје", "livada", "ливада"), group="Land"),
    Reason("ter", "Bad terrain (steep, flood, forest)", ("land", "weekend",),
           _words("strmn", "стрмн", "padina", "падина", "suma", "шума", "reka", "река"), group="Land"),
    # The ad itself
    Reason("dup", "Duplicate / already seen", learn="data", group="Ad"),
    Reason("sold", "Already sold / rented", learn="self", group="Ad"),
    Reason("fake", "Fake or bait ad", learn="data", group="Ad"),
    Reason("agc", "Agency won't share details", ("airbnb", "flip", "motivated", "land", "weekend"), learn="self", group="Ad"),
    # Strategy
    Reason("rnt", "Rent too high for Airbnb", ("airbnb",), learn="self", group="Strategy"),
    Reason("sub", "Landlord won't allow subletting", ("airbnb",), learn="self", group="Strategy"),
    Reason("str", "Not my strategy", group="Strategy"),
    Reason("oth", "Other — I'll type a note", learn="self", group="Other"),
]
BY_CODE = {r.code: r for r in REASONS}


def for_researcher(researcher: str) -> list[Reason]:
    return [r for r in REASONS if researcher in r.researchers]


def keyboard(researcher: str, listing_id: str) -> dict:
    """Reason buttons, two per row; callback 'r|<code>|<researcher>|<listing id>'."""
    btns = [{"text": r.label, "callback_data": f"r|{r.code}|{researcher}|{listing_id}"}
            for r in for_researcher(researcher)]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([{"text": "↩ Undo", "callback_data": f"r|undo|{researcher}|{listing_id}"}])
    return {"inline_keyboard": rows}


# Utility reasons → the wording that hid the problem in the ads you rejected:
# "во близина има струја и вода", "можност за приклучок", "проектирана улица", "пат со чакал".
_VAGUE = {
    "nor": _words("proektiran", "planiran pat", "pristapen pat do niv", "sljunak", "cakal", "zemjen pat", "makadam",
                  "poljski pat", "nema asfalt"),
    "noe": _words("moznost za prikluc", "moznost da se prikluc", "treba da se prikluc", "vo blizina ima struja",
                  "blizu struja", "struja vo blizina"),
    "now": _words("moznost za prikluc", "moznost da se prikluc", "treba da se prikluc", "vo blizina ima voda",
                  "vo blizina ima struja i voda", "voda vo blizina", "blizu voda"),
}
_ASK = {
    "nor": "You turned down plots near here for no road access: does a built road (asphalt or registered) "
           "reach the plot itself, not just the village?",
    "noe": "You turned down plots near here for no electricity: is power connected on the plot itself, "
           "and who pays the connection?",
    "now": "You turned down plots near here for no water: is water connected on the plot itself (mains or well)?",
}
# What a free-text note under "Other" really means.
_NOTE_HINTS = [
    ("nor", re.compile(r"\broad\b|\bpat\b|\bпат\b|access|пристап", re.I)),
    ("sold", re.compile(r"missing|removed|deleted|\bsold\b|продаден|не постои|\bgone\b", re.I)),
    ("loc", re.compile(r"pollut|smog|zagad|загад|too low|location|lokacij|локациј", re.I)),
]
_LOW = re.compile(r"pollut|smog|zagad|загад|too low|magla|магла|\bfog\b", re.I)


def effective_reason(rj: dict) -> str:
    """The reason to learn from: "Other" + a note that names the problem counts as that problem."""
    code = rj.get("reason") or ""
    if code == "oth" and rj.get("note"):
        for hint, rx in _NOTE_HINTS:
            if rx.search(rj["note"]):
                return hint
    return code


def penalties(item: dict, listing: dict, rejections: list[dict], researcher: str,
              ask: list[str] | None = None) -> tuple[float, list[str]]:
    """Score change (≤ 0) from your past rejections of similar deals, with explanations.

    Each matching reason costs up to 12 points, growing with how often you
    used it: 12 · n / (n + 2)  →  1 rejection −4, 3 → −7, 10 → −10.
    Questions worth asking the seller because of a past rejection are added to *ask*.
    """
    total, why = 0.0, []
    counts: dict[str, list[dict]] = {}
    for rj in rejections:
        if rj["researcher"] == researcher and rj["listing_id"] != item["id"]:
            counts.setdefault(effective_reason(rj), []).append(rj)
    elevation = item.get("elevation") if item.get("elevation") is not None else (listing or {}).get("elevation")
    for code, rjs in counts.items():
        reason = BY_CODE.get(code)
        if not reason or reason.learn in ("data", "self"):
            continue
        n = len(rjs)
        hit = False
        if reason.learn == "size_min":
            areas = sorted(r["area"] for r in rjs if r.get("area"))
            hit = bool(areas) and item.get("area") is not None and item["area"] <= areas[len(areas) // 2]
        elif reason.learn == "size_max":
            areas = sorted(r["area"] for r in rjs if r.get("area"))
            hit = bool(areas) and item.get("area") is not None and item["area"] >= areas[len(areas) // 2]
        elif code in _VAGUE:
            nearby = any(r.get("district") and r["district"] == item.get("district")
                         and r.get("city") == item.get("city") for r in rjs)
            hit = _VAGUE[code](listing or item)
            if (hit or nearby) and ask is not None and _ASK[code] not in ask:
                ask.append(_ASK[code])
            if nearby and not hit:
                why.append(f"ask first: you rejected a plot in {item['district']} for "
                           f"{reason.label.lower()} — question added")
        elif reason.similar is not None:
            hit = reason.similar(listing)
        elif code == "loc":
            def same_place(r):
                if not (r.get("district") and r["district"] == item.get("district")
                        and r.get("city") == item.get("city")):
                    return False
                # A municipality can run from the valley to the mountains: "too low / polluted"
                # in Karpoš says nothing about a plot at 1,600 m on Vodno.
                if elevation is not None and r.get("elevation") is not None and elevation >= r["elevation"] + 300:
                    return False
                return not (elevation is not None and elevation >= 700 and _LOW.search(r.get("note") or ""))
            hit = any(same_place(r) for r in rjs)
        elif code in ("prc", "str"):
            hit = any(r.get("kind") == item.get("kind") and r.get("city") == item.get("city") for r in rjs)
        elif code == "far":
            hit = any(r.get("city") == item.get("city") for r in rjs) and (item.get("district") or "") not in (
                "Скопје Центар", "Карпош")
        if hit:
            pts = 12 * n / (n + 2)
            total -= pts
            why.append(f"−{pts:.0f}: you rejected {n} deal{'s' if n > 1 else ''} as “{reason.label.lower()}”"
                       + (" — this ad is vague about it too" if code in _VAGUE else ""))
    return total, why


# ------------------------------------------------- learning from examples
# Your 👍 / ✅ deals and your ❌ deals, compared with each new deal. The data is
# small, so every effect is capped and spelled out in the deal's reasons.
PLOTS = {"land", "weekend"}       # both are plots to build on: a vote on one applies to the other
REPOST, NEAR_DUP = 15, 8          # points off for a re-post / a near-copy of a deal you rejected
LIKE_MAX = 6                      # points on for resembling a deal you're interested in
ALT_MAX = 8                       # altitude preference, before scaling by how many examples back it
ALT_BONUS_MAX = 0                 # only a brake on low plots: a lift would just add points to every high plot
LEARN_RANGE = (-20, 8)            # the whole learned adjustment stays inside this range
NOT_ABOUT_PLACE = ("sold", "dup", "fake", "pm2")   # rejections that say nothing about the place


def family(researcher: str) -> str:
    return "plots" if researcher in PLOTS else researcher


@dataclass
class Example:
    id: str
    researcher: str
    vote: int                      # +1 interested, -1 rejected
    title: str = ""
    words: frozenset = frozenset()
    city: str | None = None
    district: str | None = None
    area: float | None = None
    price: float | None = None     # total
    ppm2: float | None = None
    elevation: float | None = None
    reason: str | None = None      # effective reason code (rejections)
    note: str | None = None


def _num(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _wordset(*texts) -> frozenset:
    return frozenset(w for w in norm(" ".join(t for t in texts if t)).split() if len(w) > 2)


def example(listing_id: str, researcher: str, vote: int, *sources: dict | None, reason: str | None = None,
            note: str | None = None) -> Example:
    """Facts about a deal you voted on, each from the first source that has it:
    today's result, the stored listing, the saved snapshot, the rejection row."""
    sources = [s for s in sources if s]

    def first(*keys):
        for s in sources:
            for k in keys:
                if s.get(k) not in (None, ""):
                    return s[k]
        return None
    area, price, unit = _num(first("area", "area_m2")), _num(first("price")), _num(first("ppm2"))
    for s in sources:                       # a stored listing knows whether its price is per m²
        if "price_eur" in s:
            if s.get("price_note") == "per_m2" and s.get("price_eur") and s.get("area_m2"):
                unit, price = float(s["price_eur"]), float(s["price_eur"]) * float(s["area_m2"])
            elif price is None:
                price = _num(s.get("price_eur"))
            break
    if price and area and price < 300 and first("kind") == "land":
        unit, price = price, price * area   # older rejection rows saved plot prices like "18" (€/m²)
    if unit is None and price and area:
        unit = price / area
    described = next((s for s in sources if s.get("description")), {})
    return Example(listing_id, researcher, vote, title=first("title") or "",
                   words=_wordset(first("title"), described.get("description")),
                   city=first("city"), district=first("district"), area=area, price=price, ppm2=unit,
                   elevation=_num(first("elevation")), reason=reason, note=note)


def _close(a, b, tol: float) -> bool:
    return bool(a and b and a > 0 and b > 0 and max(a, b) / min(a, b) <= 1 + tol)


def lookalike(item: dict, listing: dict | None, ex: Example) -> str | None:
    """"repost" = the same ad text again (usually under a new ad number);
    "near" = same village/district, same size and price."""
    if ex.id == item["id"] or item.get("city") != ex.city:
        return None
    words = _wordset(item.get("title"), (listing or {}).get("description"))
    if len(words) >= 8 and len(ex.words) >= 8 and len(words & ex.words) / len(words | ex.words) >= 0.6:
        return "repost"
    title = norm(item.get("title")).strip()
    if len(title) >= 12 and title == norm(ex.title).strip():
        return "repost"
    if ex.district and item.get("district") == ex.district and _close(item.get("area"), ex.area, 0.15) and (
            _close(item.get("ppm2"), ex.ppm2, 0.25) or _close(item.get("price"), ex.price, 0.2)):
        return "near"
    return None


def resemblance(item: dict, ex: Example) -> list[str]:
    """What this deal has in common with one you liked."""
    traits = []
    if ex.district and item.get("district") == ex.district and item.get("city") == ex.city:
        traits.append(f"also in {ex.district}")
    e = _num(item.get("elevation"))
    if e is not None and ex.elevation is not None and abs(e - ex.elevation) <= 150:
        traits.append(f"similar altitude, {e:,.0f} m")
    if _close(item.get("area"), ex.area, 0.35):
        traits.append(f"similar size, {item['area']:,.0f} m²")
    if _close(item.get("price"), ex.price, 0.35):
        traits.append(f"similar price, €{item['price']:,.0f}")
    return traits


def altitude_profile(exs: list[Example]) -> dict | None:
    """Do the plots you liked sit clearly higher (or lower) than the ones you rejected?"""
    from statistics import median
    up = [e.elevation for e in exs if e.vote > 0 and e.elevation is not None]
    down = [e.elevation for e in exs if e.vote < 0 and e.elevation is not None and e.reason not in NOT_ABOUT_PLACE]
    if len(up) < 2 or len(down) < 2:
        return None
    hi, lo = median(up), median(down)
    if abs(hi - lo) < 200:
        return None
    k = min(len(up), len(down))
    return {"liked": hi, "rejected": lo, "line": (hi + lo) / 2, "half_gap": abs(hi - lo) / 2,
            "sign": 1 if hi > lo else -1, "weight": k / (k + 2), "n_up": len(up), "n_down": len(down)}


def learned(item: dict, listing: dict | None, exs: list[Example], researcher: str) -> tuple[float, list[str]]:
    """Score change from the deals you voted on that look like this one, with the why.

    − re-post of a deal you rejected: −15; same district, size and price: −8
    + shares 3–4 traits (district, altitude, size, price) with one you liked: +4 / +6
    ± altitude, once your 👍 and ❌ plots clearly differ: up to −8 · k/(k+2) for plots below the midpoint
    The total stays within −20 … +8."""
    mine = [e for e in exs if family(e.researcher) == family(researcher) and e.id != item["id"]]
    delta, why = 0.0, []
    worst = None
    for e in mine:
        if e.vote >= 0 or e.reason == "sold":
            continue
        kind = lookalike(item, listing, e)
        if kind == "repost":
            worst = (kind, e)
            break
        if kind == "near" and worst is None:
            worst = (kind, e)
    if worst:
        kind, e = worst
        pts = REPOST if kind == "repost" else NEAR_DUP
        label = BY_CODE[e.reason].label.lower() if e.reason in BY_CODE and e.reason != "oth" else ""
        what = "looks like a re-post of" if kind == "repost" else "same area, size and price as"
        delta -= pts
        why.append(f"−{pts} ranked lower: {what} “{e.title[:60]}” you rejected" + (f" for {label}" if label else ""))
    else:
        scored = [(resemblance(item, e), e) for e in mine if e.vote > 0]
        best = max(scored, key=lambda t: len(t[0]), default=None)
        if best and len(best[0]) >= 3:
            pts = LIKE_MAX if len(best[0]) >= 4 else LIKE_MAX * 2 // 3
            delta += pts
            why.append(f"+{pts} ranked higher: like “{best[1].title[:60]}” you marked interested "
                       f"({', '.join(best[0])})")
    elev = _num(item.get("elevation"))
    prof = altitude_profile(mine) if family(researcher) == "plots" else None
    if prof and elev is not None:
        pts = ALT_MAX * prof["weight"] * max(-1.0, min(1.0, prof["sign"] * (elev - prof["line"]) / prof["half_gap"]))
        pts = min(pts, ALT_BONUS_MAX)
        if abs(pts) >= 1:
            delta += pts
            sign = "+" if pts > 0 else "−"
            why.append(f"{sign}{abs(pts):.0f} ranked {'higher' if pts > 0 else 'lower'}: at {elev:,.0f} m — plots you "
                       f"liked sit at about {prof['liked']:,.0f} m, ones you turned down at about "
                       f"{prof['rejected']:,.0f} m")
    lo, hi = LEARN_RANGE
    return max(lo, min(hi, delta)), why
