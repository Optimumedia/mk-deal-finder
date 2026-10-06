"""Researcher 1 — rental arbitrage in Skopje.

Find flats offered for long-term rent where the rent is low for the spot,
and estimate what the same flat would earn on Airbnb / Booking.
"""
from __future__ import annotations

import math

from ..market import Market, total_price
from ..text import KeywordSet, norm
from .common import base, clamp, haversine_km, text_of, utilities_ok, utilities_status

NAME = "airbnb"
TITLE = "Airbnb / Booking arbitrage — Skopje"


def _location_factor(listing: dict, c: dict, hotspots: KeywordSet) -> tuple[float, float | None, list[str]]:
    reasons = []
    dist = None
    if listing.get("lat") and listing.get("lng"):
        dist = haversine_km(listing["lat"], listing["lng"], c["center_lat"], c["center_lng"])
        factor = max(c["decay_floor"], c["decay_per_km"] ** dist)
        reasons.append(f"{dist:.1f} km from Macedonia Square")
    else:
        factor = c["district_score"].get(listing.get("district") or "", 0.4)
        reasons.append(f"district {listing.get('district') or '?'} (no GPS)")
    spots = hotspots.find(norm(text_of(listing)))
    if spots and (dist is None or dist <= 3):   # a "square" 8 km out is a village square
        factor = min(1.05, factor + 0.08)
        reasons.append("near " + ", ".join(spots[:3]))
    return factor, dist, reasons


def run(listings: list[dict], market: Market, cfg: dict) -> list[dict]:
    c = cfg["airbnb"]
    hotspots = KeywordSet(c["hotspot_keywords"])
    out = []
    for l in listings:
        if l["kind"] not in ("apartment", "house") or l["deal"] != "rent" or l.get("city") != c["city"]:
            continue
        rent = total_price(l)
        if not rent or not (c["min_rent"] <= rent <= c["max_rent"]):
            continue
        util = utilities_status(l, urban_assumed=True)
        if not utilities_ok(util, strict=False):
            continue

        rooms = l.get("rooms")
        if not rooms and l.get("area_m2"):
            a = l["area_m2"]
            rooms = 1 if a < 42 else 2 if a < 65 else 3 if a < 90 else 4
        rooms_key = str(int(clamp(math.floor((rooms or 2) + 0.5), 1, 4)))

        factor, dist, reasons = _location_factor(l, c, hotspots)
        adr = c["adr_by_rooms"][rooms_key] * (0.75 + 0.25 * factor)   # price holds up better than demand
        occupancy = c["occupancy_center"] * factor
        nights = 30 * occupancy
        gross = adr * nights
        stays = nights / c["avg_stay_nights"]
        furnished = l.get("furnished")
        furnishing = 0 if furnished else (c["furnishing_cost"] if furnished is False else c["furnishing_cost"] / 2)
        costs = (gross * c["platform_fee"] + c["utilities_monthly"] + c["cleaning_per_stay"] * stays
                 + c["supplies_monthly"] + furnishing / c["furnishing_months"])
        profit = gross - costs - rent
        if profit < c["min_monthly_profit"]:
            continue
        margin = profit / gross if gross else 0
        upfront = rent * 2 + furnishing          # deposit + first month + furniture
        payback = upfront / profit if profit > 0 else None

        # How cheap is this rent for its spot?
        ref, n, level = market.reference(l["kind"], "rent", l.get("city"), l.get("district"))
        rent_discount = None
        if ref and l.get("area_m2"):
            rent_discount = 1 - (rent / l["area_m2"]) / ref
            if rent_discount > 0.1:
                reasons.append(f"rent {rent_discount:.0%} below {level} median")

        if furnished:
            reasons.append("furnished — ready to list")
        elif furnished is False:
            reasons.append("unfurnished — budget for furniture")

        score = 100 * clamp(0.55 * clamp(profit / 800) + 0.25 * clamp(factor) + 0.2 * clamp(margin / 0.5))
        r = base(l)
        r.update({
            "score": round(score),
            "utilities": util,
            "qualified": True,
            "reasons": reasons,
            "metrics": {
                "Rent / month": round(rent),
                "Est. nightly rate": round(adr),
                "Est. occupancy": f"{occupancy:.0%}",
                "Est. revenue / month": round(gross),
                "Est. costs / month": round(costs),
                "Est. profit / month": round(profit),
                "Margin": f"{margin:.0%}",
                "Upfront cash": round(upfront),
                "Payback (months)": round(payback, 1) if payback else None,
                "Distance to center (km)": round(dist, 1) if dist is not None else None,
                "Rent vs market": f"{-rent_discount:+.0%}" if rent_discount is not None else None,
            },
            "sort_value": profit,
        })
        out.append(r)
    out.sort(key=lambda r: (-r["score"], -r["sort_value"]))
    return out
