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

ALL = ("airbnb", "land", "flip", "motivated", "auctions")


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
    Reason("sml", "Too small", ("airbnb", "flip", "motivated", "auctions", "land"), learn="size_min", group="Property"),
    Reason("big", "Too big", ("airbnb", "flip", "motivated", "auctions", "land"), learn="size_max", group="Property"),
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
    Reason("ill", "Illegal / not legalised build", ("flip", "motivated", "auctions", "land"),
           _words("legaliz", "легализ", "divo", "диво"), group="Legal"),
    # Land
    Reason("noe", "No electricity", ("land",), learn="self", group="Land"),
    Reason("now", "No water", ("land",), learn="self", group="Land"),
    Reason("nor", "No road access", ("land",), learn="self", group="Land"),
    Reason("agr", "Agricultural — can't build", ("land", "auctions", "motivated"),
           _words("zemjodel", "земјодел", "niva", "нива", "lozje", "лозје", "livada", "ливада"), group="Land"),
    Reason("ter", "Bad terrain (steep, flood, forest)", ("land",),
           _words("strmn", "стрмн", "padina", "падина", "suma", "шума", "reka", "река"), group="Land"),
    # The ad itself
    Reason("dup", "Duplicate / already seen", learn="data", group="Ad"),
    Reason("sold", "Already sold / rented", learn="self", group="Ad"),
    Reason("fake", "Fake or bait ad", learn="data", group="Ad"),
    Reason("agc", "Agency won't share details", ("airbnb", "flip", "motivated", "land"), learn="self", group="Ad"),
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


def penalties(item: dict, listing: dict, rejections: list[dict], researcher: str) -> tuple[float, list[str]]:
    """Score change (≤ 0) from your past rejections of similar deals, with explanations.

    Each matching reason costs up to 12 points, growing with how often you
    used it: 12 · n / (n + 2)  →  1 rejection −4, 3 → −7, 10 → −10.
    """
    total, why = 0.0, []
    counts: dict[str, list[dict]] = {}
    for rj in rejections:
        if rj["researcher"] == researcher and rj["listing_id"] != item["id"]:
            counts.setdefault(rj["reason"], []).append(rj)
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
        elif reason.similar is not None:
            hit = reason.similar(listing)
        elif code == "loc":
            hit = any(r.get("district") and r["district"] == item.get("district") and r.get("city") == item.get("city")
                      for r in rjs)
        elif code in ("prc", "str"):
            hit = any(r.get("kind") == item.get("kind") and r.get("city") == item.get("city") for r in rjs)
        elif code == "far":
            hit = any(r.get("city") == item.get("city") for r in rjs) and (item.get("district") or "") not in (
                "Скопје Центар", "Карпош")
        if hit:
            pts = 12 * n / (n + 2)
            total -= pts
            why.append(f"−{pts:.0f}: you rejected {n} deal{'s' if n > 1 else ''} as “{reason.label.lower()}”")
    return total, why
