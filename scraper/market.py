"""Market reference prices built from everything we've scraped.

"Market value" = median €/m² of comparable listings (same type, same deal,
same district → city → country), using only the recent window.

Which asking prices count as evidence (checked on the real data, 2026-11):
- only ads seen in the last `window_days`;
- an ad seen for under `min_days_listed` and then never again doesn't count:
  most are leftovers of the one-off first crawl of 6 Oct (never re-read;
  flats 7% and houses 31% under the city median), typos and re-posted ads;
- newer sightings weigh more: an ask last seen `half_life_days` ago counts half.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
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
    """Median of the middle 80% once there are enough values. (Trimming the same
    share off both ends leaves the median where it was: the median itself is
    what shrugs off outliers.)"""
    v = sorted(values)
    if len(v) >= 10:
        cut = len(v) // 10
        v = v[cut:len(v) - cut]
    return median(v)


def median_range(values: list[float]) -> tuple[float, float]:
    """Rough 95% range for the true median (order statistics, n/2 ± 1.96·√n/2).
    18 plots asking 22–260 €/m² give 'somewhere between 50 and 150': too wide
    to call anything X% below it."""
    v = sorted(values)
    n = len(v)
    half = 0.98 * n ** 0.5
    lo, hi = max(0, int(n / 2 - half)), min(n - 1, int(n / 2 + half + 0.999))
    return v[lo], v[hi]


def is_stable(values: list[float], med: float, max_width: float | None) -> bool:
    """True when the median is pinned down to within ±max_width/2 of itself."""
    if not max_width or not med:
        return True
    lo, hi = median_range(values)
    return (hi - lo) / med <= max_width


def weighted_median(pairs: list[tuple[float, float]]) -> float:
    """Median of (value, weight) pairs: the value with half the weight on each side.
    Equal weights give the ordinary median."""
    pairs = sorted(p for p in pairs if p[1] > 0)
    if not pairs:
        raise ValueError("no values")
    if all(w == pairs[0][1] for _, w in pairs):
        return median(v for v, _ in pairs)
    half, run = sum(w for _, w in pairs) / 2, 0.0
    for i, (v, w) in enumerate(pairs):
        run += w
        if abs(run - half) <= 1e-9 * half and i + 1 < len(pairs):
            return (v + pairs[i + 1][0]) / 2         # exactly half: between the two
        if run > half:
            return v
    return pairs[-1][0]


def _when(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def evidence_weight(row: dict, now: datetime, window_days: float | None = None,
                    half_life_days: float | None = None, min_days_listed: float | None = None) -> float:
    """How much one asking price counts towards the median (0 = not at all).
    Rows without last_seen (tests, the quick-check tool) count fully."""
    last, first = _when(row.get("last_seen")), _when(row.get("first_seen"))
    if last is None:
        return 1.0
    age = max(0.0, (now - last).total_seconds() / 86400)
    if window_days is not None and age > window_days:
        return 0.0
    # Seen for under a day or two, then gone for 3+ days: a one-off sighting we
    # could never re-check (first-crawl leftover, typo fixed by re-posting, duplicate).
    if min_days_listed and first is not None and age >= 3 \
            and (last - first).total_seconds() / 86400 < min_days_listed:
        return 0.0
    return 0.5 ** (age / half_life_days) if half_life_days else 1.0


class Market:
    def __init__(self, rows: list[dict], min_district=8, min_city=8, min_national=15, *,
                 window_days: float | None = None, half_life_days: float | None = None,
                 min_days_listed: float | None = None, max_ci_width: float | None = None,
                 now: datetime | None = None):
        self.mins = (min_district, min_city, min_national)
        self.max_ci_width = max_ci_width
        now = now or datetime.now(timezone.utc)
        weighted: dict[tuple, list[tuple[float, float]]] = defaultdict(list)
        self.excluded = 0                       # asks left out by the evidence rules
        for r in rows:
            v = ppm2(r)
            if v is None:
                continue
            w = evidence_weight(r, now, window_days, half_life_days, min_days_listed)
            if w <= 0:
                self.excluded += 1
                continue
            k, d, band = r["kind"], r["deal"], size_band(r)
            # dict.fromkeys: a listing without a district must not count twice for its town
            for key in dict.fromkeys(((k, d, r.get("city"), r.get("district")), (k, d, r.get("city"), None),
                                      (k, d, None, None))):
                weighted[key].append((v, w))
                if band:
                    weighted[key + (band,)].append((v, w))
        # Plain €/m² per bucket: len() = how many ads a median rests on.
        self.buckets = {k: [v for v, _ in vals] for k, vals in weighted.items()}
        self.medians = {k: weighted_median(vals) for k, vals in weighted.items()}
        self._stable: dict[tuple, bool] = {}

    @classmethod
    def from_config(cls, rows: list[dict], m: dict, now: datetime | None = None) -> "Market":
        """Market built with the [market] settings of config.toml."""
        return cls(rows, m["min_samples_district"], m["min_samples_city"], m["min_samples_national"],
                   window_days=m.get("window_days"), half_life_days=m.get("half_life_days"),
                   min_days_listed=m.get("min_days_listed"), max_ci_width=m.get("max_ci_width"), now=now)

    def reference(self, kind: str, deal: str, city: str | None, district: str | None, area: float | None = None):
        """Return (median €/m², sample size, level) or (None, 0, None).

        Tries the same size band first (flats only), then all sizes, going
        district → city → national. A level needs enough ads AND a median that
        is pinned down (see is_stable); otherwise the next, wider level is used.
        """
        band = size_band({"kind": kind, "area_m2": area})
        levels = [((kind, deal, city, district), "district", self.mins[0]),
                  ((kind, deal, city, None), "city", self.mins[1]),
                  ((kind, deal, None, None), "national", self.mins[2])]
        levels = [x for x in levels if not ((x[1] == "district" and not district) or (x[1] == "city" and not city))]
        if band and kind in SIZE_FIRST:
            # same size band in the district / town first, then all sizes there;
            # the whole country (same band first) only as a last resort
            local = [x for x in levels if x[1] != "national"]
            national = [x for x in levels if x[1] == "national"]
            tries = [(key + (band,), level, need, band) for key, level, need in local] + \
                    [(key, level, need, None) for key, level, need in local] + \
                    [(key + (band,), level, need, band) for key, level, need in national] + \
                    [(key, level, need, None) for key, level, need in national]
        else:
            tries = [t for key, level, need in levels
                     for t in (((key + (band,), level, need, band),) if band else ()) + ((key, level, need, None),)]
        for k, level, need, b in tries:
            if len(self.buckets.get(k, ())) >= need and self.stable(k):
                return Ref.make(self.medians[k], len(self.buckets[k]), level, b)
        return Ref.make(None, 0, None)

    def stable(self, key: tuple) -> bool:
        if key not in self._stable:
            self._stable[key] = is_stable(self.buckets[key], self.medians[key], self.max_ci_width)
        return self._stable[key]

    def summary(self) -> list[dict]:
        """City-level medians for the dashboard's market tab."""
        out = []
        for key, vals in self.buckets.items():
            if len(key) != 4:
                continue                       # size-band buckets stay internal
            kind, deal, city, district = key
            if district is None and len(vals) >= 5:
                out.append({"kind": kind, "deal": deal, "city": city or "Цела Македонија",
                            "median_ppm2": round(self.medians[key], 1), "samples": len(vals)})
        out.sort(key=lambda r: (r["kind"], r["deal"], -r["samples"]))
        return out
