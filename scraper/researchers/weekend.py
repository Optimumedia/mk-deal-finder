"""🏔 Weekend plots: land at 700 m or higher for a weekend house, anywhere in MK.

Altitude comes from the ad text, the exact map pin, or the village named in the
ad (marked approximate). Prices are compared with other high-altitude plots of
a similar size — a mountain plot is a different market from a Skopje plot.
Electricity, water and road access must be confirmed in the ad.
"""
from __future__ import annotations

from collections import defaultdict

from ..market import SIZE_BANDS, Market, Ref, ppm2, robust_median, total_price
from ..text import land_type
from .common import _distance as r_dist
from .common import base, clamp, fmt_drive, text_of, utilities_ok, utilities_status

NAME = "weekend"
TITLE = "Weekend plots 700 m+"


def _band(area: float) -> str:
    return next(label for lo, hi, label in SIZE_BANDS["land"] if lo <= area < hi)


def _high(l: dict, min_el: float) -> bool:
    return (l["kind"] == "land" and l["deal"] == "sale" and not l.get("abroad")
            and l.get("elevation") is not None and l["elevation"] >= min_el)


def mountain_market(listings: list[dict], min_el: float) -> dict:
    """€/m² of every plot at this altitude, by size band and overall."""
    by_band, everything = defaultdict(list), []
    for l in listings:
        if _high(l, min_el) and l.get("area_m2") and (u := ppm2(l)):
            by_band[_band(l["area_m2"])].append(u)
            everything.append(u)
    return {"bands": by_band, "all": everything}


def reference(mm: dict, area: float, min_band: int, min_all: int) -> Ref | None:
    band = _band(area)
    vals = mm["bands"].get(band, [])
    if len(vals) >= min_band:
        return Ref.make(robust_median(vals), len(vals), "mountain", band)
    if len(mm["all"]) >= min_all:
        return Ref.make(robust_median(mm["all"]), len(mm["all"]), "mountain")
    return None


def run(listings: list[dict], market: Market, cfg: dict) -> list[dict]:
    c = cfg.get("weekend", {})
    min_el = c.get("min_elevation", 700)
    budget = c.get("max_price_eur") or cfg.get("budget", {}).get("personal_home", 170000)
    mm = mountain_market(listings, min_el)
    out = []
    for l in listings:
        if not _high(l, min_el):
            continue
        price, area = total_price(l), l.get("area_m2")
        if not price or not area or price > budget or not (c.get("min_area_m2", 200) <= area <= c.get("max_area_m2", 20000)):
            continue
        text = text_of(l)
        ltype = l.get("land_type_confirmed") or land_type((l.get("fields") or {}).get("Тип на земјиште"), text)
        util = utilities_status(l, urban_assumed=False)
        if any(v == "no" for v in util.values()):
            continue
        qualified = utilities_ok(util, strict=c.get("require_all_utilities", True))

        unit = ppm2(l)
        rf = reference(mm, area, c.get("min_samples_band", 8), c.get("min_samples_all", 15))
        discount = (1 - unit / rf[0]) if (rf and unit) else None
        el = round(l["elevation"])
        approx = (l.get("elevation_src") or "").startswith("place:")
        drive = r_dist(l, "skopje")
        minutes = (drive or {}).get("min")

        reasons = [f"altitude {'~' if approx else ''}{el} m" + (" (village altitude — check the exact plot)" if approx else "")]
        if discount is not None and discount > 0:
            reasons.append(f"{discount:.0%} below the {rf.describe('land').replace('mountain median', 'median of 700 m+ plots')} "
                           f"({rf[0]:.0f} €/m², {rf[1]} listings)")
        elif discount is not None:
            reasons.append(f"{-discount:.0%} above the median of 700 m+ plots ({rf[0]:.0f} €/m²)")
        if ltype == "building":
            reasons.append("building land (градежно)")
        elif ltype == "agricultural":
            reasons.append("⚠ agricultural — needs re-zoning to build a weekend house")
        confirmed = [k for k, v in util.items() if v == "yes"]
        if confirmed:
            reasons.append("confirmed: " + ", ".join(confirmed))
        missing = [k for k, v in util.items() if v != "yes"]
        if missing:
            reasons.append("not mentioned: " + ", ".join(missing) + " — ask the seller")
        if minutes is not None:
            reasons.append(f"{fmt_drive(drive)} from Skopje")

        size_fit = 1.0 if 300 <= area <= 2500 else 0.6
        reach = 0.5 if minutes is None else clamp((180 - minutes) / 120)   # ≤60 min = 1, 3 h = 0
        alt_fit = 1.0 if el <= 1500 else 0.7            # very high = snowed in for months
        score = 100 * clamp(
            0.30 * clamp((discount or 0) / 0.5)
            + 0.30 * (len(confirmed) / 3)
            + 0.10 * (ltype == "building")
            + 0.10 * size_fit
            + 0.10 * reach
            + 0.10 * alt_fit
        )
        if ltype == "agricultural":
            score *= 0.7
        r = base(l)
        r.update({
            "score": round(score),
            "utilities": util,
            "qualified": qualified,
            "comps": rf[1] if rf else 0,
            "reasons": reasons,
            "metrics": {
                "Price": round(price),
                "Area (m²)": round(area),
                "€/m²": round(unit, 1) if unit else None,
                "700 m+ median €/m²": round(rf[0], 1) if rf else None,
                "Below market": f"{discount:.0%}" if discount is not None else None,
                "Compared against": (f"{rf[1]} plots at 700 m+ {rf.band or 'of all sizes'}" if rf
                                     else "too few 700 m+ plots listed to judge the price"),
                "Altitude (m)": el,
                "Altitude source": "village (approx.)" if approx else
                                   {"text": "the ad text", "gps": "map pin"}.get(l.get("elevation_src"), "—"),
                "Land type": {"building": "Building", "agricultural": "Agricultural"}.get(ltype, "Unknown"),
                "To Skopje": fmt_drive(drive),
                "To Ohrid": fmt_drive(r_dist(l, "ohrid")),
            },
            "sort_value": discount or 0,
        })
        out.append(r)
    out.sort(key=lambda r: (not r["qualified"], -r["score"]))
    return out
