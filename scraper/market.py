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


# Bigger property asks less per m² — a 5,000 m² field vs a 300 m² building plot
# in the same town differ several-fold. Compare like with like.
SIZE_BANDS = {
    "apartment": [(0, 45, "under 45 m²"), (45, 75, "45–75 m²"), (75, 110, "75–110 m²"), (110, 10**9, "over 110 m²")],
    "house": [(0, 100, "under 100 m²"), (100, 180, "100–180 m²"), (180, 300, "180–300 m²"), (300, 10**9, "over 300 m²")],
    "weekend_house": [(0, 60, "under 60 m²"), (60, 120, "60–120 m²"), (120, 10**9, "over 120 m²")],
    "land": [(0, 400, "under 400 m²"), (400, 800, "400–800 m²"), (800, 1500, "800–1,500 m²"),
             (1500, 3000, "1,500–3,000 m²"), (3000, 10000, "3,000–10,000 m²"), (10000, 10**12, "over 10,000 m²")],
}
# For land and houses size matters more than the exact neighbourhood: same size
# anywhere in town beats all sizes next door. For flats, location comes first.
SIZE_FIRST = {"land", "house", "weekend_house"}


def size_band(listing: dict) -> str | None:
    a, bands = listing.get("area_m2"), SIZE_BANDS.get(listing.get("kind"))
    if not a or not bands:
        return None
    return next(label for lo, hi, label in bands if lo <= a < hi)


class Ref(tuple):
    """(median €/m², sample size, level) — unpacks like a 3-tuple; .band says
    which size band it was compared within (None = all sizes)."""
    band: str | None = None

    @staticmethod
    def make(median_ppm2, n, level, band=None) -> "Ref":
        r = Ref((median_ppm2, n, level))
        r.band = band
        return r

    def describe(self, kind: str) -> str:
        """'city median for 800–1,500 m² plots' / 'district median'."""
        noun = {"land": "plots", "house": "houses", "weekend_house": "weekend houses", "apartment": "flats"}.get(kind, "")
        return f"{self[2]} median" + (f" for {self.band} {noun}" if self.band else "")


def robust_median(values: list[float]) -> float:
    """Median of the middle 80% once there are enough values (outliers out)."""
    v = sorted(values)
    if len(v) >= 10:
        cut = len(v) // 10
        v = v[cut:len(v) - cut]
    return median(v)


class Market:
    def __init__(self, rows: list[dict], min_district=8, min_city=8, min_national=15):
        self.mins = (min_district, min_city, min_national)
        buckets: dict[tuple, list[float]] = defaultdict(list)
        for r in rows:
            v = ppm2(r)
            if v is None:
                continue
            k, d, band = r["kind"], r["deal"], size_band(r)
            # dict.fromkeys: a listing without a district must not count twice for its town
            for key in dict.fromkeys(((k, d, r.get("city"), r.get("district")), (k, d, r.get("city"), None),
                                      (k, d, None, None))):
                buckets[key].append(v)
                if band:
                    buckets[key + (band,)].append(v)
        self.buckets = buckets
        self.medians = {k: robust_median(v) for k, v in buckets.items()}

    def reference(self, kind: str, deal: str, city: str | None, district: str | None, area: float | None = None):
        """Return (median €/m², sample size, level) or (None, 0, None).

        Tries the same size band first (flats only), then all sizes, going
        district → city → national.
        """
        band = size_band({"kind": kind, "area_m2": area})
        levels = [((kind, deal, city, district), "district", self.mins[0]),
                  ((kind, deal, city, None), "city", self.mins[1]),
                  ((kind, deal, None, None), "national", self.mins[2])]
        levels = [x for x in levels if not ((x[1] == "district" and not district) or (x[1] == "city" and not city))]
        if band and kind in SIZE_FIRST:
            # same size band at every level first, then all sizes
            tries = [(key + (band,), level, need, band) for key, level, need in levels] + \
                    [(key, level, need, None) for key, level, need in levels]
        else:
            tries = [t for key, level, need in levels
                     for t in (((key + (band,), level, need, band),) if band else ()) + ((key, level, need, None),)]
        for k, level, need, b in tries:
            if len(self.buckets.get(k, ())) >= need:
                return Ref.make(self.medians[k], len(self.buckets[k]), level, b)
        return Ref.make(None, 0, None)

    def summary(self) -> list[dict]:
        """City-level medians for the dashboard's market tab."""
        out = []
        for key, vals in self.buckets.items():
            if len(key) != 4:
                continue                       # size-band buckets stay internal
            kind, deal, city, district = key
            if district is None and len(vals) >= 5:
                out.append({"kind": kind, "deal": deal, "city": city or "Цела Македонија",
                            "median_ppm2": round(robust_median(vals), 1), "samples": len(vals)})
        out.sort(key=lambda r: (r["kind"], r["deal"], -r["samples"]))
        return out
