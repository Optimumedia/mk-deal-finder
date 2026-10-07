"""Researcher 5 — Bailiff auctions (KIRSM register).

Flats, houses and land sold by bailiffs across North Macedonia, compared
with what similar property asks on the open market. Starting prices are
often well below market, and later sale rounds start lower still. Only sales
whose date is still ahead are shown.
"""
from __future__ import annotations

from datetime import date

from ..market import Market, ppm2, total_price
from .common import base, clamp

NAME = "auctions"
TITLE = "Bailiff auctions — official sales below market"
ROUND_TXT = {1: "1st sale", 2: "2nd sale (lower start)", 3: "3rd sale (lowest start)"}


def run(listings: list[dict], market: Market, cfg: dict) -> list[dict]:
    c = cfg["auctions"]
    today = date.today()
    out = []
    for l in listings:
        if l["deal"] != "auction" or l["kind"] not in c["kinds"] or not l.get("auction_date"):
            continue
        sale = date.fromisoformat(l["auction_date"])
        days_left = (sale - today).days
        if days_left < 0:
            continue
        price, area = total_price(l), l.get("area_m2")
        b = cfg.get("budget", {})
        # Land could be your own home; flats and houses at auction are business buys.
        limit = b.get("personal_home") if l["kind"] == "land" else b.get("business")
        if not price or price < c["min_price_eur"] or (limit and price > limit):
            continue
        unit = ppm2({**l, "deal": "sale"})          # sanity bounds of a normal sale
        rf = market.reference(l["kind"], "sale", l.get("city"), None, area)
        ref, samples, level = rf
        discount = (1 - unit / ref) if (unit and ref and level != "national") else None
        rnd = l.get("auction_round")
        extra = l.get("extra") or {}
        # Only a share of the property (e.g. 1/6 идеален дел): you'd co-own with strangers.
        if extra.get("share") and not c["include_partial_shares"]:
            continue

        reasons = []
        # Starting price: 1st sale = court appraisal; 2nd = appraisal − 1/3; 3rd lower still.
        appraisal = price * {2: 1.5, 3: 2.0}.get(rnd or 1, 1.0)
        app_gap = (1 - (appraisal / area) / ref) if (ref and area and level != "national") else None
        if app_gap is not None and app_gap >= c.get("suspicious_appraisal_gap", 0.35):
            reasons.append(f"⚠ court appraisal ~{appraisal:,.0f} € is {app_gap:.0%} under local asking — usually a "
                           f"problem asset (basement, shared land, encumbrance): read the notice")
        if discount is not None:
            if discount > 0:
                reasons.append(f"starts {discount:.0%} below the {l.get('city')} {rf.describe(l['kind'])} "
                               f"({ref:,.0f} €/m², {samples} listings)")
            else:
                reasons.append(f"starts {-discount:.0%} above {l.get('city')} asking median — only worth it after a cut")
        if rnd and rnd > 1:
            reasons.append(ROUND_TXT[rnd] + " — the earlier round found no buyer")
        reasons.append(f"sale on {sale.strftime('%d.%m.%Y')} ({'today' if days_left == 0 else f'in {days_left} days'})")
        if l["kind"] == "land":
            reasons.append("utilities unknown — check the notice and the cadastre")
        if extra.get("share"):
            reasons.append(f"⚠ only a {extra['share']} ownership share is sold — co-ownership")
        reasons.append("deposit 10% the day before; pay within 15 days or lose it; buyer pays transfer tax; "
                       "check товари, third-party land and occupancy in the notice")

        if discount is not None and discount < c["max_premium"] * -1:
            continue    # starts far above market — not a deal at this round
        urgency = 1 if days_left >= 3 else 0.6       # too close to the date to prepare
        score = 100 * clamp(0.55 * clamp(((discount or 0) + 0.05) / 0.45)
                            + 0.15 * ((rnd or 1) - 1) / 2
                            + 0.15 * (1 if discount is not None else 0.3)
                            + 0.15 * urgency)
        r = base(l)
        r.update({
            "score": round(score),
            "utilities": {"electricity": "unknown", "water": "unknown", "road": "unknown"} if l["kind"] == "land"
                         else {"electricity": "assumed", "water": "assumed", "road": "assumed"},
            "qualified": True,
            "comps": samples if discount is not None else 0,
            "reasons": reasons,
            "auction_date": l["auction_date"],
            "metrics": {
                "Starting price": round(price),
                "Area (m²)": round(area) if area else None,
                "€/m²": round(unit) if unit else None,
                "City asking median €/m²": round(ref) if (ref and level != "national") else None,
                "Below market": f"{discount:.0%}" if discount is not None and discount > 0 else None,
                "Sale date": sale.strftime("%d.%m.%Y"),
                "Days until sale": days_left,
                "Round": ROUND_TXT.get(rnd, "unknown"),
                "Court appraisal est.": round(appraisal),
                "Deposit (10%)": round(appraisal * 0.1),
                "Case no.": extra.get("case"),
                "Bailiff": extra.get("bailiff"),
            },
            "sort_value": discount or 0,
        })
        out.append(r)
    out.sort(key=lambda r: -r["score"])
    return out
