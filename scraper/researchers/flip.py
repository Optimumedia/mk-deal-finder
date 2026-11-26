"""Researcher 3 — fix & flip anywhere in North Macedonia.

Flats and houses priced well under comparable listings (same district or
same single-town market, same size band), with renovation, transaction costs,
capital-gains tax and the resulting profit / ROI.

Calibration from our own data (Nov 2026, ~1,500 sale flats with €/m²):
renovated second-hand flats ask ~0.93× the district median (the median also
holds new builds), "old condition" ones ~0.84×, basement flats ~0.73× and attic
flats ~0.92×. Village houses ask ~0.47× their town's house median — a village
is a different market, not a discount.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import date

from ..market import Market, ppm2, total_price
from ..places import CITY_CENTRES, SKOPJE, centar_unconfirmed, other_town
from ..text import KeywordSet, norm
from .common import base, clamp, haversine_km, text_of, utilities_ok, utilities_status

# Developer price lists ("станови од 45 до 90 m²", "во градба") are not one flat at one price.
_OFF_PLAN = re.compile(r"станови|stanovi|во градба|vo gradba|vo izgradba|изградба"
                       r"|\bod \d+.{0,20}\bdo \d+|од \d+.{0,20}до \d+", re.I)

_UNDER_CONSTRUCTION = re.compile(r"во градба|vo gradba|vo izgradba|во изградба|недовршен|nedovrsen"
                                 r"|груба градба|groba gradba|grub[ao] gradba|карабина|karabina", re.I)
_BASEMENT = re.compile(r"suteren|сутерен|polupodrum|полуподрум|подрумски стан|podrumski stan", re.I)
# Small "duplex" flats in old blocks are almost always built into the roof space.
_ATTIC = re.compile(r"поткров|potkrov|мансард|mansard|масандр|дуплекс|dupleks|duplex", re.I)
# Not a cash sale of the whole home at that price.
_SWAP = KeywordSet(["menuvam", "menuvame", "zamena", "razmena", "kompenzacija"])
# Part of the area isn't registered — it can't be sold at full value.
_UNREGISTERED = KeywordSet(["ne e upisan", "ne e upisana", "ne se upisani", "ne e zapisan", "ne e zapisana",
                            "ne e vo imoten list", "nelegaliziran", "nelegalizirana", "bespraven", "bespravno"])
# A share of a property ("1/2 од куќа", "идеален дел од стан") can't be renovated and resold on its own.
_SHARE = KeywordSet(["idealen del od stan", "idealen del od kuka", "idealen del od kukja", "idealen del od objekt",
                     "sosopstvenost", "suvlasnistvo", "polovina od kuka", "polovina od stan"])
_FRACTION = re.compile(r"(?<![\d/.])1\s*/\s*[2-4](?![\d/])\s*(?:од|od|del|дел|ideal|идеал)", re.I)
# Village house: "куќа во с. Кучково", "село Раштани", "vo s.Proevce".
_VILLAGE = re.compile(r"(?:^|[\s,(])(?:с\.|s\.|село\b|selo\b|во с\b|vo s\b)", re.I)
# …or in the text: "се наоѓа во с.Велгошти", "vo selo Zabeni".
_IN_VILLAGE = re.compile(r"\b(?:во|vo|na|на)\s+(?:с\.|s\.|село\b|selo\b|селото\b|seloto\b)", re.I)
_WEEKEND = KeywordSet(["vikendic", "vikend kuk"])
_HOLIDAY = KeywordSet(["vikendic", "vikend kuk", "za odmor", "odmor i prestoj", "prestoj vo priroda"])

log = logging.getLogger("scraper.flip")

NAME = "flip"
TITLE = "Fix & flip — under market value"

# Stages of the funnel, in order (logged each run so lost deals can be traced).
STAGES = ("homes for sale", "price & area known", "within budget", "right place", "built & whole",
          "local comparables", "below market", "utilities", "profitable")


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


def condition(listing: dict) -> str:
    """How much work the home needs before it can be resold:

    'renovated' — the site's own field says Реновиран / Нов (it wins over ad text
                  like "стара градба", which describes the building, not the flat);
    'heavy'     — "за реновирање" in the ad or field, or old condition in a pre-1980 building;
    'medium'    — "Стар" (second-hand, dated: bathroom, floors, paint, some electrics);
    'light'     — condition not stated.
    """
    f = listing.get("fields") or {}
    state = f.get("Состојба", "").lower()
    year = _year_built(listing)
    year = year if year and 1800 < year <= date.today().year else None
    m = re.search(r"(19\d\d|20\d\d)", f.get("Година на реновирање", ""))
    renovated_year = int(m.group(1)) if m else None
    if "за реновир" in state:
        return "heavy"
    if state.startswith(("реновир", "нов")) or (renovated_year and renovated_year >= date.today().year - 10):
        return "renovated"
    if listing.get("renovation"):
        return "heavy"
    if state.startswith("стар"):
        # An old house means roof, facade and installations too; an old flat is often just dated.
        return "heavy" if listing.get("kind") != "apartment" or (year and year < 1980) else "medium"
    if year and year >= date.today().year - 10:
        return "renovated"
    if year and year < 1980:
        return "heavy"
    return "light"


# Villages inside Skopje's municipalities (the district median there is the city's).
_SKOPJE_VILLAGES = KeywordSet(["волково", "volkovo", "кучково", "kuckovo", "никиштане", "nikistane", "стајковци",
                               "stajkovci", "идризово", "idrizovo", "јурумлери", "jurumleri", "трубарево",
                               "trubarevo", "бардовци", "bardovci", "морани", "morani", "драчево", "dracevo",
                               "сопиште", "sopiste", "бразда", "brazda", "мирковци", "mirkovci"], whole_word=True)
# Per-m² price written in the ad ("цена 1400 евра", "1.400 €/m2").
_STATED_PPM2 = re.compile(r"\bcena(?: e| od| na| po)? (\d(?: \. )?\d{3}|\d{3}) ?(?:e|eur|evr|evra|euro)\b")


def outside_town(listing: dict) -> bool:
    """A village or outlying settlement: a different (much cheaper, slower) market than the town."""
    if _VILLAGE.search(listing.get("title") or "") or _IN_VILLAGE.search(listing.get("description") or ""):
        return True
    city = listing.get("city")
    if city == SKOPJE:
        return _SKOPJE_VILLAGES.any(norm(" ".join(filter(None, [listing.get("title"), listing.get("address")]))))
    centre = CITY_CENTRES.get(city)
    if not centre:
        return False
    src = listing.get("elevation_src") or ""
    if listing.get("lat") and listing.get("lng"):
        return haversine_km(listing["lat"], listing["lng"], *centre) > 4
    if src.startswith("place:") and listing.get("approx_lat") and listing.get("approx_lng"):
        return haversine_km(listing["approx_lat"], listing["approx_lng"], *centre) > 3
    return False


def price_contradicted(listing: dict, unit: float) -> bool:
    """The ad text names a per-m² price that doesn't match price ÷ area (a deposit, an old price…)."""
    m = _STATED_PPM2.search(norm(listing.get("description")))
    if not m or listing["kind"] != "apartment":
        return False
    stated = float(m.group(1).replace(" . ", ""))
    return 400 <= stated <= 6000 and abs(unit / stated - 1) > 0.2


def floor_factor(listing: dict) -> tuple[float, str | None]:
    """Value of this flat vs a normal one in the same district (basements and attics sell for less)."""
    if listing["kind"] != "apartment":
        return 1.0, None
    floor = (listing.get("fields") or {}).get("Спрат", "")
    text = text_of(listing)
    if "сутерен" in floor.lower() or _BASEMENT.search(text):
        return 0.73, "basement / semi-basement"
    if "поткров" in floor.lower() or _ATTIC.search(text):
        return 0.92, "attic / duplex"
    return 1.0, None


def is_share(listing: dict) -> bool:
    t = text_of(listing)
    return _SHARE.any(norm(t)) or bool(_FRACTION.search(t))


def comparables(listing: dict, kind: str, market: Market, min_comps: int):
    """Like-for-like reference: same district (or a one-district town) and, where possible, same size band.

    Skopje is ten different markets (Centar asks ~3× Šuto Orizari), so a
    Skopje-wide median is never used. A district that is not its town's own
    name (Dojran filed under Gevgelija) never falls back to the town either.
    """
    city, district = listing.get("city"), listing.get("district")
    if centar_unconfirmed(listing):
        district = None                 # "Скопје Центар" with nothing central: the form default
    if not city:
        return None
    rf = market.reference(kind, "sale", city, district, listing.get("area_m2"))
    ref, samples, level = rf
    if not ref or level == "national" or samples < min_comps:
        return None
    if level == "city" and (city == SKOPJE or (district and district != city)):
        return None
    return rf


def _percentile(market: Market, kind: str, listing: dict, rf, unit: float) -> float | None:
    """Share of the comparables asking less per m² than this one (0 = cheapest)."""
    key = (kind, "sale", listing.get("city"), listing.get("district") if rf[2] == "district" else None)
    if rf.band:
        key += (rf.band,)
    vals = market.buckets.get(key)
    if not vals:
        return None
    return sum(1 for v in vals if v < unit) / len(vals)


def run(listings: list[dict], market: Market, cfg: dict, funnel: Counter | None = None,
        debug: list | None = None) -> list[dict]:
    c = cfg["flip"]
    budget = cfg.get("budget", {}).get("business", c.get("max_total_investment", 10**9))
    liquid = set(c.get("liquid_cities", ()))
    funnel = funnel if funnel is not None else Counter()
    out = []
    for l in listings:
        if l["kind"] not in ("apartment", "house", "weekend_house") or l["deal"] != "sale" or l.get("abroad"):
            continue
        funnel["homes for sale"] += 1
        price, area, unit = total_price(l), l.get("area_m2"), ppm2(l)
        if not price or not unit or area < c["min_area_m2"]:
            continue
        funnel["price & area known"] += 1
        if price * (1 + c["buy_costs"]) > min(budget, c["max_price_eur"]):
            continue                    # can't even buy it within the business budget
        funnel["within budget"] += 1
        if other_town(l) or not l.get("city"):
            continue                    # filed under Skopje but it's in Mavrovo, Struga…
        kind = l["kind"]
        if kind == "house" and (_WEEKEND.any(norm(l.get("title"))) or _HOLIDAY.any(norm(l.get("description")))):
            kind = "weekend_house"      # "викендица" filed as a house: compare with weekend houses
        if outside_town(l):
            continue                    # village homes ask ~half the town median: another market
        funnel["right place"] += 1
        state = (l.get("fields") or {}).get("Состојба", "")
        year = _year_built(l)
        if ("градба" in state.lower() or (year and year > date.today().year)
                or _UNDER_CONSTRUCTION.search(text_of(l))
                or (l.get("price_note") == "per_m2" and _OFF_PLAN.search(text_of(l)))):
            continue                    # off-plan / still being built / developer price list
        if is_share(l):
            continue                    # only a share of the property is for sale
        if price_contradicted(l, unit):
            continue                    # "цена 1400 €/m²" in the text but price ÷ area says 1,014
        if kind != "apartment" and l.get("price_note") == "per_m2":
            continue                    # houses aren't sold per m² — the real price is unknown ("по договор")
        t = norm(text_of(l))
        if _SWAP.any(t) or _UNREGISTERED.any(t):
            continue                    # a swap offer, or m² that aren't in the title deed
        funnel["built & whole"] += 1
        rf = comparables(l, kind, market, c.get("min_comps", 10))
        if not rf:
            continue
        funnel["local comparables"] += 1
        ref, samples, level = rf
        factor, floor_note = floor_factor(l)
        local = ref * factor            # what a flat like this one asks here
        discount = 1 - unit / local
        if discount < c["min_discount"] or discount > c["suspicious_discount"]:
            continue
        pct = _percentile(market, kind, l, rf, unit)
        if pct is not None and pct > c.get("max_percentile", 0.25):
            continue                    # not among the cheapest comparables: the median is just skewed
        funnel["below market"] += 1
        # Built homes are connected unless the ad says otherwise ("assumed").
        util = utilities_status(l, urban_assumed=True)
        if not utilities_ok(util, strict=False):
            continue
        funnel["utilities"] += 1

        cond = condition(l)
        house = kind != "apartment"
        small_town = l.get("city") not in liquid
        per_m2 = {"heavy": c["renovation_heavy_per_m2"], "medium": c["renovation_medium_per_m2"],
                  "light": c["renovation_light_per_m2"], "renovated": c["renovation_refresh_per_m2"]}[cond]
        reno = (area * per_m2 * (c["renovation_house_factor"] if house and cond == "heavy" else 1)
                * (c["renovation_outside_skopje_factor"] if l.get("city") != SKOPJE else 1))
        # After the works it's a renovated second-hand home, which asks less than
        # the district median (that median also holds new builds).
        market_value = local * c["renovated_value_ratio"] * area
        haircut = c["resale_discount"] * (c["house_resale_factor"] if house else 1) \
            + (c["small_town_extra_discount"] if small_town else 0)
        resale = market_value * (1 - haircut)
        months = c["holding_months_house"] if house else (
            c["holding_months_small_town"] if small_town else c["holding_months"])
        buy_costs = price * c["buy_costs"]
        holding = months * c["holding_cost_per_month"]
        sell_costs = resale * c["sell_costs"]
        gain = resale - price - buy_costs - reno - holding - sell_costs
        # 10% PIT on the gain: a flip is neither lived in nor held 3–5 years, so no
        # exemption. Renovation only counts with invoices — assume it doesn't.
        tax = max(0.0, resale - sell_costs - price - buy_costs) * c["capital_gains_tax"]
        profit = gain - tax
        total_cash = price + buy_costs + reno + holding
        roi = profit / total_cash
        if debug is not None:
            debug.append((l, kind, cond, discount, price, reno, resale, profit, roi, total_cash))
        if total_cash > budget:
            continue                    # over the business budget once renovation and fees are added
        if roi < c["min_roi"] or profit < c.get("min_profit", 0):
            continue
        funnel["profitable"] += 1

        reasons = [f"{discount:.0%} below the {rf.describe(kind)} ({ref:.0f} €/m², {samples} listings)"]
        if floor_note:
            reasons[0] = (f"{discount:.0%} below what a {floor_note} flat asks here "
                          f"({local:.0f} €/m² = {factor:.0%} of the {rf.describe(kind)}, {samples} listings)")
            if factor < 0.8:
                reasons.append(f"⚠ {floor_note} — slow to resell, few buyers")
        if kind != l["kind"]:
            reasons.append("weekend house (викендица) — compared with weekend houses")
        reasons.append({"heavy": "needs full renovation" + (f" (built {year})" if year else ""),
                        "medium": "second-hand, dated — budgeted bathroom, floors, paint",
                        "light": "condition not stated — budgeted a light refresh",
                        "renovated": "already renovated / new — the profit is the price gap, not the works"}[cond])
        if small_town:
            reasons.append(f"smaller market — assumed a longer sale ({months} months) and a lower sale price")
        neg = c.get("negotiation_hint", 0.05)
        if neg:
            saved = price * neg * (1 + c["buy_costs"]) * (1 - c["capital_gains_tax"])
            reasons.append(f"every {neg:.0%} you negotiate off the asking price adds ~{saved:,.0f} € profit")
        if l.get("site_old_price") and l["site_old_price"] > price:
            reasons.append(f"price already cut from {l['site_old_price']:,.0f} €")

        rent_ref = None
        if l["kind"] == "apartment":
            rent_ref, _, rlevel = market.reference("apartment", "rent", l.get("city"), l.get("district"), area)
            rent_ref = rent_ref if rlevel != "national" else None
        yield_gross = (rent_ref * area * 12 / price) if rent_ref else None
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
                "Compared against": f"{level} · {rf.band or 'all sizes'} · {samples} listings",
                "Condition": cond,
                "Est. market value": round(market_value),
                "Expected sale price": round(resale),
                "Renovation est.": round(reno),
                "Buying costs est.": round(buy_costs),
                "Selling costs + tax est.": round(sell_costs + tax),
                "Taxes & fees est.": round(buy_costs + sell_costs + tax),
                "Months to sell est.": months,
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
    log.info("flip funnel: %s", " → ".join(f"{s} {funnel[s]}" for s in STAGES))
    return _dedupe(out)


def _dedupe(results: list[dict]) -> list[dict]:
    """Same home posted by owner and agency (same place, ±1 m², price within 3%): keep the best one."""
    kept: list[dict] = []
    for r in results:                   # best score first
        if any(k["kind"] == r["kind"] and k["city"] == r["city"] and k["district"] == r["district"]
               and abs((k["area"] or 0) - (r["area"] or 0)) <= 1
               and abs(k["price"] - r["price"]) <= 0.03 * max(k["price"], r["price"]) for k in kept):
            continue
        kept.append(r)
    return kept
