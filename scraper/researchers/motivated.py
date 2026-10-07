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


def _opening_offer(price, drop, ref, area, discount) -> int:
    """−15% normally; −8% when the seller already cut 15%+; never below 90% of
    the typical price nearby (a lowball that insults gets no counter-offer)."""
    offer = price * (0.92 if drop >= 0.15 else 0.85)
    if ref and area and discount is not None:
        offer = max(offer, ref * area * 0.9)
    return int(round(min(offer, price * 0.92), -2))


def run(listings: list[dict], market: Market, cfg: dict) -> list[dict]:
    c = cfg["motivated"]
    urgent = KeywordSet(c["urgent_keywords"])
    out = []
    for l in listings:
        if l["deal"] != "sale" or l.get("abroad"):
            continue
        price = total_price(l)
        b = cfg.get("budget", {})
        limit = b.get("personal_home") if l["kind"] == "land" else b.get("business")
        if not price or price < c["min_price_eur"] or (limit and price > limit):
            continue
        util = utilities_status(l, urban_assumed=l["kind"] != "land")
        if not utilities_ok(util, strict=False):
            continue                    # the ad says a utility is missing
        # Land must have power, water and road confirmed (owner's rule): until then
        # it's a "❓ needs info" lead rather than a deal.
        qualified = l["kind"] != "land" or utilities_ok(util, strict=True)

        # Biggest observed cut: our own history, or the site's strike-through
        # price. Compared in the listing's own unit (total or per m²).
        raw = l["price_eur"]
        start_raw = max(x for x in (l.get("first_price"), l.get("site_old_price"), raw) if x)
        drop = 1 - raw / start_raw
        start = start_raw * (price / raw)
        old = l.get("site_old_price")
        if l.get("price_note") == "per_m2" and old and old > 6000:
            # Now "2,600 €/m²", struck-through "152,000 €" total: compare totals.
            start, drop = old, 1 - price / old
        if drop > c["max_believable_drop"] or drop < 0:   # 140,000 → 1,400 is a unit change, not a cut
            drop, start = 0, price
        words = urgent.find(norm(text_of(l)))
        age = days_listed(l)
        # Reklama5 ad numbers grow steadily, so a renewed ad still betrays its real age.
        real_age = l.get("id_age_days") or age

        signals = []
        if drop >= c["min_price_drop"]:
            signals.append(f"price cut {drop:.0%} ({start:,.0f} → {price:,.0f} €)")
        if words:
            signals.append("urgent wording: " + ", ".join(words[:3]))
        if not signals:
            continue
        if real_age >= c["stale_listing_days"]:
            months = real_age / 30.4
            signals.append(f"first advertised ~{months:.0f} months ago" if months >= 2
                           else f"on the market {real_age} days")

        unit = ppm2(l)
        ref, _, level = market.reference(l["kind"], "sale", l.get("city"), l.get("district"), l.get("area_m2"))
        # National medians mix Skopje with villages — only compare locally. Houses
        # without a detail page (or in villages) often carry the plot area: skip.
        comparable = level not in (None, "national") and not (
            l["kind"] == "house" and (not l.get("detail_at") or not l.get("district")))
        discount = (1 - unit / ref) if (unit and ref and comparable) else None
        if discount is not None and discount > 0.05:
            signals.append(f"already {discount:.0%} below {level} median")
        if discount is not None and discount < -0.25:
            continue   # still far above market even after the cut

        score = 100 * clamp(
            0.40 * clamp(drop / 0.2)
            + 0.20 * clamp(len(words) / 2)
            + 0.15 * clamp(real_age / 365)
            + 0.25 * clamp(((discount or 0) + 0.05) / 0.35)
        )
        r = base(l)
        r.update({
            "score": round(score),
            "utilities": util,
            "qualified": qualified,
            "reasons": signals,
            "metrics": {
                "Price now": round(price),
                "Highest price seen": round(start),
                "Price cut": f"{drop:.0%}" if drop > 0 else "—",
                "Days since renewed": age,
                "First advertised (est.)": f"~{real_age} days ago" if real_age > age else None,
                "vs typical price nearby": f"{-discount:+.0%}" if discount is not None else None,
                "Opening offer idea": _opening_offer(price, drop, ref, l.get("area_m2"), discount),
            },
            "sort_value": drop,
        })
        out.append(r)
    out.sort(key=lambda r: -r["score"])
    return out
