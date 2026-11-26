"""Researcher 1 — rental arbitrage in Skopje.

Find flats offered for long-term rent where the rent is low for the spot,
and estimate what the same flat would earn on Airbnb / Booking.
"""
from __future__ import annotations

import math
import re
from statistics import median

from ..market import Market, total_price
from ..text import KeywordSet, classify_deal, norm
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


def _rooms_key(listing: dict) -> str:
    rooms = listing.get("rooms")
    if not rooms and listing.get("area_m2"):
        a = listing["area_m2"]
        rooms = 1 if a < 42 else 2 if a < 65 else 3 if a < 90 else 4
    # Floor, not round: a "3,5 соби" flat earns like a 3-room one.
    return str(int(clamp(math.floor(rooms or 2), 1, 4)))


def calibrate_adr(listings: list[dict], c: dict, hotspots: KeywordSet) -> dict[str, tuple[float, int]]:
    """Nightly rate per room count, learned from Skopje flats advertised per night.

    Each observed price is converted to its city-centre equivalent (undoing
    the location factor), then blended with the config estimate:
        adr = (n · observed median + k · config) / (n + k)
    so a handful of odd ads can't swing it, and real data wins as it grows.
    """
    lo, hi = c["observed_adr_range"]
    seen: dict[str, list[float]] = {}
    for l in listings:
        if l["kind"] != "apartment" or effective_deal(l, c) != "short_term" or l.get("city") != c["city"] or l.get("abroad"):
            continue
        if _HOURLY.search(" ".join(filter(None, [l.get("title"), l.get("description")]))):
            continue    # "3 часа", "дневен престој" — priced per few hours, not per night
        p = l.get("price_eur")
        if not p or not (lo <= p <= hi) or l.get("price_note"):
            continue
        factor, _, _ = _location_factor(l, c, hotspots)
        seen.setdefault(_rooms_key(l), []).append(p / (0.75 + 0.25 * factor))
    k, need = c["adr_prior_weight"], c["adr_min_samples"]
    out = {}
    for key, prior in c["adr_by_rooms"].items():
        obs = seen.get(key, [])
        if len(obs) < need:
            out[key] = (prior, len(obs))
            continue
        blended = (len(obs) * median(obs) + k * prior) / (len(obs) + k)
        # Classified per-night ads are the budget end of the market: they may
        # raise the estimate, never pull it below what a decent listing earns.
        out[key] = (max(prior, blended), len(obs))
    return out


_HOURLY = re.compile(r"\d\s*(?:h|ч|часа|часови|sati|casa|caсa)\b|na cas\b|на час\b", re.I)
# "Дневен престој" is the living room in Macedonian flat ads ("дневен престој
# со кујна"), not a day-use stay, yet it got ~40 monthly Skopje rentals filed
# as short-term. Day-use ads also say "до 3 часа", which _HOURLY catches.
_LIVING_ROOM = re.compile(r"dneven\s+prestoj")

# Not a flat to live in: offices, shops, party venues. Many ordinary flats add
# "погоден и за канцеларија" (also suits an office) and agency footers say
# "локал 2", so business words only count in the title (unless the title also
# offers it for living), or in the description when it says the place is
# offered *as* business space or *only* for business. Matched on norm() text.
_BIZ = r"(?:deloven|delovn|kancelar|ordinac|lokal(?:i|ot)?\b|magacin)"
_NOT_A_HOME_TITLE = re.compile(r"(?<![a-z])" + _BIZ)
_LIVING = re.compile(r"(?<![a-z])(?:ziveenje|domuvanje|stanben)")
_NOT_A_HOME_DESC = re.compile(
    r"(?<![a-z])(?:prvenstveno|isklucivo|iskluchivo|samo|edinstveno)\s+(?:za\s+)?(?:firm|" + _BIZ + r")"
    r"|(?<![a-z])(?:izdava|izdavam|izdavame|iznajmuva|iznajmuvam)\W+" + _BIZ +
    r"|(?<![a-z])(?:rodenden|proslav|zabav|sala za)")


def not_a_home(listing: dict) -> bool:
    """True when the ad is for an office / shop / party venue rather than a flat."""
    title, desc = norm(listing.get("title")), norm(listing.get("description"))
    if _NOT_A_HOME_TITLE.search(title) and not _LIVING.search(title):
        return True
    return bool(_NOT_A_HOME_DESC.search(desc) or _NOT_A_HOME_DESC.search(title))


def effective_deal(listing: dict, c: dict) -> str:
    """The deal type, correcting monthly rentals mis-filed as short-term.

    A "short-term" ad priced like a monthly rent (≥ min_rent) is re-checked
    with "дневен престој" (living room) removed from the description; if
    nothing else says per-night, it is a long-term rental.
    """
    deal = listing.get("deal")
    p = listing.get("price_eur")
    if deal != "short_term" or listing.get("kind") not in ("apartment", "house") or not p or p < c["min_rent"]:
        return deal
    desc = _LIVING_ROOM.sub(" ", norm(listing.get("description")))
    return classify_deal(listing.get("title") or "", desc, p, listing["kind"])


def run(listings: list[dict], market: Market, cfg: dict) -> list[dict]:
    c = cfg["airbnb"]
    hotspots = KeywordSet(c["hotspot_keywords"])
    adr_table = calibrate_adr(listings, c, hotspots)
    out = []
    for l in listings:
        if l["kind"] not in ("apartment", "house") or effective_deal(l, c) != "rent" or l.get("city") != c["city"]:
            continue
        rent = total_price(l)
        if not rent or not (c["min_rent"] <= rent <= c["max_rent"]):
            continue
        if not_a_home(l):
            continue
        util = utilities_status(l, urban_assumed=True)
        if not utilities_ok(util, strict=False):
            continue

        rooms_key = _rooms_key(l)
        factor, dist, reasons = _location_factor(l, c, hotspots)
        base_adr, adr_samples = adr_table[rooms_key]
        adr = base_adr * (0.75 + 0.25 * factor)   # price holds up better than demand
        occupancy = c["occupancy_center"] * factor
        nights = 30 * occupancy
        gross = adr * nights
        stays = nights / c["avg_stay_nights"]
        furnished = l.get("furnished")
        # A furnished long-term let still needs guest kit (linen, towels, kitchen
        # basics, smart lock, photos) before it can be listed.
        setup = c.get("setup_cost_furnished", 0)
        furnishing = setup if furnished else (c["furnishing_cost"] if furnished is False
                                              else (c["furnishing_cost"] + setup) / 2)
        utilities = c["utilities_monthly"] + c["utilities_per_m2"] * (l.get("area_m2") or 55)
        cleaning = c["cleaning_per_stay"] * (1.5 if int(rooms_key) >= 3 else 1)
        costs = (gross * c["platform_fee"] + utilities + cleaning * stays
                 + c["supplies_monthly"] + furnishing / c["furnishing_months"])
        costs += c.get("tourist_tax_per_guest_night", 0) * c.get("guests_per_stay", 2) * nights
        costs += gross * c.get("rental_tax_of_gross", 0)
        profit = gross - costs - rent
        # Highest rent at which the flat still clears the profit target. Asking
        # rents are often negotiable by ~10%, so a flat that gets there with a
        # realistic offer is kept, with the offer to make (profit shown at asking).
        target = c["min_monthly_profit"]
        max_rent = rent + profit - target
        if max_rent < rent * (1 - c.get("negotiation_margin", 0)):
            continue
        if max_rent < rent:
            reasons.insert(0, f"reaches €{target:.0f}/month only at rent ≤ €{max_rent:.0f} "
                              f"({max_rent / rent - 1:+.0%}): offer that")
        margin = profit / gross if gross else 0
        upfront = rent * 2 + furnishing          # deposit + first month + furniture
        if upfront > cfg.get("budget", {}).get("business", 10**9):
            continue
        payback = upfront / profit if profit > 0 else None

        # How cheap is this rent for its spot?
        ref, n, level = market.reference(l["kind"], "rent", l.get("city"), l.get("district"), l.get("area_m2"))
        rent_discount = None
        if ref and l.get("area_m2"):
            rent_discount = 1 - (rent / l["area_m2"]) / ref
            if rent_discount > 0.1:
                reasons.append(f"rent {rent_discount:.0%} below {level} median")

        if furnished:
            reasons.append(f"furnished — needs only guest kit (~€{setup:,.0f})" if setup else "furnished — ready to list")
        elif furnished is False:
            reasons.append("unfurnished — budget for furniture")

        # Scaled to realistic Skopje numbers (2026 market data): ~€300/month after
        # tax is excellent; fast payback on the set-up cash matters as much.
        # Payback on 2× rent says little; rank on profit, location and margin.
        score = 100 * clamp(0.55 * clamp(profit / 300) + 0.25 * clamp(factor) + 0.20 * clamp(margin / 0.35))
        r = base(l)
        r.update({
            "score": round(score),
            "utilities": util,
            "qualified": True,
            "comps": adr_samples,            # real per-night ads behind the nightly rate
            "reasons": reasons,
            "metrics": {
                "Rent / month": round(rent),
                f"Max rent for €{target:.0f} profit": round(max_rent),
                "Est. nightly rate": round(adr),
                "Nightly rate based on": (f"{adr_samples} Skopje per-night ads + estimate" if adr_samples
                                          else "config estimate only"),
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
