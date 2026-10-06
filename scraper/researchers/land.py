"""Researcher 2 — land to build a house or villa.

Skopje and Ohrid plots, plus plots anywhere in mountain areas. Electricity,
water and road access must be confirmed in the ad.
"""
from __future__ import annotations

from ..market import Market, ppm2, total_price
from ..text import KeywordSet, land_type, norm
from .common import base, clamp, text_of, utilities_ok, utilities_status

NAME = "land"
TITLE = "Land for a house / villa"


def run(listings: list[dict], market: Market, cfg: dict) -> list[dict]:
    c = cfg["land"]
    mountains = KeywordSet(c["mountain_keywords"])
    out = []
    for l in listings:
        if l["kind"] != "land" or l["deal"] != "sale" or l.get("abroad"):
            continue
        price, area = total_price(l), l.get("area_m2")
        if not price or not area or not (c["min_area_m2"] <= area <= c["max_area_m2"]) or price > c["max_price_eur"]:
            continue
        text = text_of(l)
        n = norm(text)
        peaks = mountains.find(n)
        in_city = l.get("city") in c["cities"]
        if not (in_city or peaks):
            continue

        ltype = l.get("land_type_confirmed") or land_type((l.get("fields") or {}).get("Тип на земјиште"), text)
        if c["require_building_land"] and ltype != "building":
            continue
        util = utilities_status(l, urban_assumed=False)
        if any(v == "no" for v in util.values()):
            continue
        qualified = utilities_ok(util, strict=c["require_all_utilities"])

        reasons = []
        unit = ppm2(l)
        ref, samples, level = market.reference("land", "sale", l.get("city"), l.get("district"))
        discount = (1 - unit / ref) if (ref and unit) else None
        if discount is not None and discount > 0:
            reasons.append(f"{discount:.0%} below {level} median ({ref:.0f} €/m²)")
        if peaks:
            reasons.append("mountain area: " + ", ".join(peaks[:3]))
        if ltype == "building":
            reasons.append("building land (градежно)")
        elif ltype == "agricultural":
            reasons.append("⚠ agricultural — needs re-zoning to build")
        confirmed = [k for k, v in util.items() if v == "yes"]
        if confirmed:
            reasons.append("confirmed: " + ", ".join(confirmed))
        missing = [k for k, v in util.items() if v != "yes"]
        if missing:
            reasons.append("not mentioned: " + ", ".join(missing) + " — ask the seller")

        villa_size = 1.0 if 400 <= area <= 3000 else 0.6
        score = 100 * clamp(
            0.40 * clamp((discount or 0) / 0.5)
            + 0.25 * (len(confirmed) / 3)
            + (c["building_land_bonus"] / 100) * (ltype == "building")
            + 0.10 * villa_size
            + 0.10 * (1 if peaks else 0.6)
        )
        if ltype == "agricultural":
            score *= 0.7
        r = base(l)
        r.update({
            "score": round(score),
            "utilities": util,
            "qualified": qualified,
            "reasons": reasons,
            "metrics": {
                "Price": round(price),
                "Area (m²)": round(area),
                "€/m²": round(unit, 1) if unit else None,
                "Area median €/m²": round(ref, 1) if ref else None,
                "Below market": f"{discount:.0%}" if discount is not None else None,
                "Compared against": f"{samples} plots ({level})" if level else "not enough data yet",
                "Land type": {"building": "Building", "agricultural": "Agricultural"}.get(ltype, "Unknown"),
                "Region": "Mountain" if peaks else l.get("city"),
            },
            "sort_value": discount or 0,
        })
        out.append(r)
    out.sort(key=lambda r: (not r["qualified"], -r["score"]))
    return out
