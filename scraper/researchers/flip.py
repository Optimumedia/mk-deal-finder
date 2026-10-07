"""Researcher 3 — fix & flip anywhere in North Macedonia.

Flats and houses priced well under the local median €/m², with an estimate
of renovation, transaction costs and the resulting profit / ROI.
"""
from __future__ import annotations

import re
from datetime import date

from ..market import Market, ppm2, total_price
from ..places import centar_unconfirmed, other_town
from .common import base, clamp, text_of, utilities_ok, utilities_status

# Developer price lists ("станови од 45 до 90 m²", "во градба") are not one flat at one price.
_OFF_PLAN = re.compile(r"станови|stanovi|во градба|vo gradba|vo izgradba|изградба"
                       r"|\bod \d+.{0,20}\bdo \d+|од \d+.{0,20}до \d+", re.I)

_UNDER_CONSTRUCTION = re.compile(r"во градба|vo gradba|vo izgradba|во изградба|недовршен|nedovrsen", re.I)
_BASEMENT = re.compile(r"suteren|сутерен|polupodrum|полуподрум|подрумски стан|podrumski stan", re.I)

NAME = "flip"
TITLE = "Fix & flip — under market value"


def _year_built(listing: dict) -> int | None:
    v = (listing.get("fields") or {}).get("Година на градба", "")
    m = re.search(r"(1[89]\d\d|20\d\d)", v)
    return int(m.group(1)) if m else None


def _plot(listing: dict) -> int | None:
    """Plot size for houses, shown next to the built area so the two can't be confused."""
    f = listing.get("fields") or {}
    for k, v in f.items():
        if "парцела" in k.lower() or "плац" in k.lower():
            m = re.search(r"\d[\d.,]*", v)
            if m:
                try:
                    return int(float(m.group(0).replace(".", "").replace(",", ".")))
                except ValueError:
                    return None
    return None


def run(listings: list[dict], market: Market, cfg: dict) -> list[dict]:
    c = cfg["flip"]
    out = []
    for l in listings:
        if l["kind"] not in ("apartment", "house", "weekend_house") or l["deal"] != "sale" or l.get("abroad"):
            continue
        price, area, unit = total_price(l), l.get("area_m2"), ppm2(l)
        if not price or not unit or area < c["min_area_m2"] or price > c["max_price_eur"]:
            continue
        if other_town(l):
            continue                    # filed under Skopje but it's in Mavrovo, Struga…
        state = (l.get("fields") or {}).get("Состојба", "")
        year = _year_built(l)
        if ("градба" in state.lower() or (year and year > date.today().year)
                or _UNDER_CONSTRUCTION.search(text_of(l))
                or (l.get("price_note") == "per_m2" and _OFF_PLAN.search(text_of(l)))):
            continue                    # off-plan / still being built / developer price list
        # "Скопје Центар" with nothing central in the ad is usually the form default.
        district = None if centar_unconfirmed(l) else l.get("district")
        rf = market.reference(l["kind"], "sale", l.get("city"), district, area)
        ref, samples, level = rf
        # A national median is dominated by Skopje — every small-town flat would
        # look "cheap". Only trust district or city comparables.
        if not ref or level == "national":
            continue
        discount = 1 - unit / ref
        if discount < c["min_discount"] or discount > c["suspicious_discount"]:
            continue
        # Built homes are connected unless the ad says otherwise ("assumed").
        util = utilities_status(l, urban_assumed=True)
        if not utilities_ok(util, strict=False):
            continue

        heavy = bool(l.get("renovation")) or (year is not None and year < 1980)
        reno = area * (c["renovation_heavy_per_m2"] if heavy else c["renovation_light_per_m2"])
        market_value = ref * area
        resale = market_value * (1 - c["resale_discount"] * (1.5 if l["kind"] != "apartment" else 1))
        buy_costs = price * c["buy_costs"]
        holding = c["holding_months"] * c["holding_cost_per_month"]
        sell_costs = resale * c["sell_costs"]
        gain = resale - price - buy_costs - reno - holding - sell_costs
        tax = max(0.0, gain) * c["capital_gains_tax"]
        profit = gain - tax
        total_cash = price + buy_costs + reno + holding
        roi = profit / total_cash
        if total_cash > cfg.get("budget", {}).get("business", c.get("max_total_investment", 10**9)):
            continue                    # over the business budget once renovation and fees are added
        if roi < c["min_roi"]:
            continue

        reasons = [f"{discount:.0%} below the {rf.describe(l['kind'])} ({ref:.0f} €/m², {samples} listings)"]
        if heavy:
            reasons.append("needs renovation" + (f" (built {year})" if year else ""))
        if _BASEMENT.search(text_of(l)):
            reasons.append("⚠ basement / semi-basement — sells at a big discount, resale is harder")
        if l.get("site_old_price") and l["site_old_price"] > price:
            reasons.append(f"price already cut from {l['site_old_price']:,.0f} €")

        rent_ref, _, _ = market.reference("apartment", "rent", l.get("city"), l.get("district"), area)
        yield_gross = (rent_ref * area * 12 / price) if (rent_ref and l["kind"] == "apartment") else None
        if yield_gross and yield_gross > 0.07:
            reasons.append(f"or hold & rent: ~{yield_gross:.1%} gross yield")

        confidence = {"district": 1.0, "city": 0.75}[level] * clamp(samples / 30, 0.4, 1)
        score = 100 * clamp(0.5 * clamp(roi / 0.6) + 0.3 * clamp(discount / 0.5) + 0.2 * confidence)
        r = base(l)
        r.update({
            "score": round(score),
            "utilities": util,
            # Only trusted once the detail page confirmed size, condition and place.
            "qualified": bool(l.get("detail_at")),
            "comps": samples,
            "reasons": reasons,
            "metrics": {
                "Price": round(price),
                "€/m²": round(unit),
                "Local median €/m²": round(ref),
                "Below market": f"{discount:.0%}",
                "Est. market value": round(market_value),
                "Renovation est.": round(reno),
                "Buying costs est.": round(buy_costs),
                "Selling costs + tax est.": round(sell_costs + tax),
                "Est. profit": round(profit),
                "Total cash needed": round(total_cash),
                "ROI": f"{roi:.0%}",
                "Gross rental yield": f"{yield_gross:.1%}" if yield_gross else None,
                "Year built": year,
                "Plot (m²)": _plot(l),
            },
            "sort_value": profit,
        })
        out.append(r)
    out.sort(key=lambda r: -r["score"])
    return out
