"""Market reference prices built from everything we've scraped.

"Market value" = median €/m² of comparable listings (same type, same deal,
same district → city → country), using only the recent window.
"""
from __future__ import annotations

from collections import defaultdict
from statistics import median

# Plausible €/m² ranges; anything outside is a typo or wrong unit.
SANE_PPM2 = {
    ("apartment", "sale"): (250, 9000),
    ("house", "sale"): (60, 6000),
    ("weekend_house", "sale"): (30, 4000),
    ("land", "sale"): (0.5, 2500),
    ("apartment", "rent"): (1.5, 40),
    ("house", "rent"): (0.8, 30),
}


def total_price(listing: dict) -> float | None:
    """Listed price as a total, resolving 'per m²' prices."""
    p = listing.get("price_eur")
    if p is None or listing.get("price_note") == "placeholder":
        return None
    if listing.get("price_note") == "per_m2":
        a = listing.get("area_m2")
        return p * a if a else None
    return p


def ppm2(listing: dict) -> float | None:
    p, a = total_price(listing), listing.get("area_m2")
    if not p or not a:
        return None
    v = p / a
    lo, hi = SANE_PPM2.get((listing.get("kind"), listing.get("deal")), (0, float("inf")))
    return v if lo <= v <= hi else None


class Market:
    def __init__(self, rows: list[dict], min_district=8, min_city=8, min_national=15):
        self.mins = (min_district, min_city, min_national)
        buckets: dict[tuple, list[float]] = defaultdict(list)
        for r in rows:
            v = ppm2(r)
            if v is None:
                continue
            k, d = r["kind"], r["deal"]
            buckets[(k, d, r.get("city"), r.get("district"))].append(v)
            buckets[(k, d, r.get("city"), None)].append(v)
            buckets[(k, d, None, None)].append(v)
        self.buckets = buckets
        self.medians = {k: median(v) for k, v in buckets.items()}

    def reference(self, kind: str, deal: str, city: str | None, district: str | None):
        """Return (median €/m², sample size, level) or (None, 0, None)."""
        levels = (
            ((kind, deal, city, district), "district", self.mins[0]),
            ((kind, deal, city, None), "city", self.mins[1]),
            ((kind, deal, None, None), "national", self.mins[2]),
        )
        for key, level, need in levels:
            if level == "district" and not district:
                continue
            if level == "city" and not city:
                continue
            n = len(self.buckets.get(key, ()))
            if n >= need:
                return self.medians[key], n, level
        return None, 0, None

    def summary(self) -> list[dict]:
        """City-level medians for the dashboard's market tab."""
        out = []
        for (kind, deal, city, district), vals in self.buckets.items():
            if district is None and len(vals) >= 5:
                out.append({"kind": kind, "deal": deal, "city": city or "Цела Македонија",
                            "median_ppm2": round(median(vals), 1), "samples": len(vals)})
        out.sort(key=lambda r: (r["kind"], r["deal"], -r["samples"]))
        return out
