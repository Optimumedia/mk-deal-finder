"""Researcher 4 — Motivated Seller Radar.

Sellers who have cut their price, use "urgent" language, or have been
sitting on the market for weeks. These are the ads where an offer well
under asking has a real chance — across every property type.
"""
from __future__ import annotations

from ..market import Market, ppm2, total_price
from ..text import KeywordSet, norm
from .common import base, clamp, days_listed, text_of, utilities_ok, utilities_status

NAME = "motivated"
TITLE = "Motivated sellers — negotiation targets"


def run(listings: list[dict], market: Market, cfg: dict) -> list[dict]:
    c = cfg["motivated"]
    urgent = KeywordSet(c["urgent_keywords"])
    out = []
    for l in listings:
        if l["deal"] != "sale" or l.get("abroad"):
            continue
        price = total_price(l)
        if not price or price < c["min_price_eur"]:
            continue
        util = utilities_status(l, urban_assumed=l["kind"] != "land")
        if not utilities_ok(util, strict=False):
            continue

        # Biggest observed cut: our own history, or the site's strike-through
        # price. Compared in the listing's own unit (total or per m²).
        raw = l["price_eur"]
        start_raw = max(x for x in (l.get("first_price"), l.get("site_old_price"), raw) if x)
        drop = 1 - raw / start_raw
        if drop > c["max_believable_drop"]:     # 140,000 → 1,400 is a unit change, not a cut
            drop, start_raw = 0, raw
        start = start_raw * (price / raw)
        words = urgent.find(norm(text_of(l)))
        age = days_listed(l)

        signals = []
        if drop >= c["min_price_drop"]:
            signals.append(f"price cut {drop:.0%} ({start:,.0f} → {price:,.0f} €)")
        if words:
            signals.append("urgent wording: " + ", ".join(words[:3]))
        if not signals:
            continue
        if age >= c["stale_listing_days"]:
            signals.append(f"on the market {age} days")

        unit = ppm2(l)
        ref, _, level = market.reference(l["kind"], "sale", l.get("city"), l.get("district"))
        # National medians mix Skopje with villages — only compare locally.
        discount = (1 - unit / ref) if (unit and ref and level != "national") else None
        if discount is not None and discount > 0.05:
            signals.append(f"already {discount:.0%} below {level} median")
        if discount is not None and discount < -0.25:
            continue   # still far above market even after the cut

        score = 100 * clamp(
            0.40 * clamp(drop / 0.2)
            + 0.20 * clamp(len(words) / 2)
            + 0.15 * clamp(age / 90)
            + 0.25 * clamp(((discount or 0) + 0.05) / 0.35)
        )
        r = base(l)
        r.update({
            "score": round(score),
            "utilities": util,
            "qualified": True,
            "reasons": signals,
            "metrics": {
                "Price now": round(price),
                "Highest price seen": round(start),
                "Price cut": f"{drop:.0%}" if drop > 0 else "—",
                "Days listed": age,
                "vs market": f"{-discount:+.0%}" if discount is not None else None,
                "Opening offer idea (−15%)": round(price * 0.85, -2),
            },
            "sort_value": drop,
        })
        out.append(r)
    out.sort(key=lambda r: -r["score"])
    return out
