"""Researcher 4 — Motivated Seller Radar.

Sellers who have cut their price, use "urgent" language, have been on the
market for months or keep re-posting the same property. These are the ads
where an offer well under asking has a real chance — across every property type.

What counts as a price cut (checked on the price history, Oct–Nov 2026):
- a price that held for under `min_hold_days` and then changed again is a
  blip, not a price (a typo fixed the same day, a card vs detail page flip);
- jumps over 2× either way are a switch between total and €/m², not a cut;
- the new, lower price must have been seen on a later run too, so a typo
  that's fixed by the next run never shows up as "cut 40%".
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date, datetime, timezone

from ..market import Market, ppm2, total_price
from ..text import KeywordSet, norm
from .common import base, clamp, days_listed, text_of, utilities_ok, utilities_status

NAME = "motivated"
TITLE = "Motivated sellers — negotiation targets"

USUAL_GAP = 0.10      # asking → agreed price, same as [flip].resale_discount


# ------------------------------------------------------------------ history
def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        try:
            d = datetime.fromisoformat(s[:10])
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _days(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 86400


def price_story(l: dict, min_hold_days: float = 1, confirm_days: float = 0.4) -> dict:
    """Price cuts in the listing's own unit (total or €/m²).

    {"peak": highest believable earlier price, "drop": 1 - now/peak,
     "cuts": downward steps, "since": when the first cut happened (date),
     "pending": a cut seen once only — wait for the next run,
     "blips": short-lived prices ignored, "site": the strike-through price counted}
    """
    raw = l.get("price_eur")
    out = {"peak": raw, "drop": 0.0, "cuts": 0, "since": None, "pending": False, "blips": 0, "site": False}
    if not raw:
        return out
    hist = l.get("price_history")
    if hist is None:                     # no history loaded: first price we saw is all we know
        hist = [(l.get("first_seen"), l["first_price"])] if l.get("first_price") else []
    pts = [(_ts(t), p) for t, p in hist if p and _ts(t)]
    pts = [(t, p) for t, p in pts if 0.5 <= p / raw <= 2.0]        # beyond 2×: unit switch or typo
    last_seen = _ts(l.get("last_seen")) or (pts[-1][0] if pts else None)
    # spells: (start, end, price); a spell that didn't last is a blip
    spells = [(t, pts[i + 1][0] if i + 1 < len(pts) else last_seen, p) for i, (t, p) in enumerate(pts)]
    held = []
    for i, (a, b, p) in enumerate(spells):
        current = i == len(spells) - 1
        if not current and _days(a, b) < min_hold_days:
            out["blips"] += 1
            continue
        if held and abs(held[-1][2] - p) <= 0.005 * p:
            held[-1] = (held[-1][0], b, p)                       # same price again: one spell
        else:
            held.append((a, b, p))
    earlier = held[:-1] if held and abs(held[-1][2] - raw) <= 0.005 * raw else held
    cur_start = held[-1][0] if held and earlier is not held else None
    peak = max((p for _, _, p in earlier), default=raw)
    cuts = [held[i][0] for i in range(1, len(held)) if held[i][2] < held[i - 1][2] * 0.99]
    # The site's strike-through price, now or when we first saw the ad (first_price
    # above the first recorded price came from that strike-through).
    first = l.get("first_price")
    site = max(l.get("site_old_price") or 0, first if first and pts and first > pts[0][1] else 0)
    if site and raw < site <= raw * 2 and site > peak:
        peak, out["site"] = site, True
    if peak > raw:
        out.update(peak=peak, drop=1 - raw / peak, cuts=max(len(cuts), 1),
                   since=(cuts[0] if cuts else None))
        if cur_start and last_seen and _days(cur_start, last_seen) < confirm_days and not out["site"]:
            out["pending"] = True                               # seen once at this price so far
    return out


def _words(title: str | None) -> set[str]:
    return set(re.findall(r"\w{3,}", (title or "").lower()))


def _photo(l: dict) -> str | None:
    img = l.get("image") or ""
    return None if not img or "noimage" in img.lower() else img


def attach_history(listings: list[dict], histories: dict[str, list], past: list[dict]) -> int:
    """Give each sale listing its price history and the earlier ads for the same
    property that ended before this one started (sellers delete and re-post to
    look new): same site, type, town, rooms, size ±3%, asking 0–25% more, and
    the same photo or the same distinctive title words. Agencies use template
    titles ("Se prodava stan vo Blok 70 vo Tetovo"), so words found in 1%+ of
    titles ("prodava", "stan", "tetovo") don't count as distinctive.
    Returns how many listings had earlier ads."""
    by_place = defaultdict(list)
    df: Counter = Counter()
    for p in past:
        if p.get("deal") == "sale" and p.get("price_note") != "per_m2" and p.get("area_m2"):
            by_place[(p.get("source"), p.get("kind"), p.get("city"))].append(p)
            df.update(_words(p.get("title")))
    common = {w for w, n in df.items() if n >= max(5, 0.01 * len(past))}
    distinct = lambda t: _words(t) - common
    found = 0
    for l in listings:
        if l.get("deal") != "sale":
            continue
        l["price_history"] = histories.get(l["id"], [])
        start, area, price = _ts(l.get("first_seen")), l.get("area_m2"), total_price(l)
        if not (start and area and price) or l.get("price_note") == "per_m2":
            continue                     # developer price lists repeat the same flat
        words = distinct(l.get("title"))
        earlier = []
        for p in by_place.get((l.get("source"), l.get("kind"), l.get("city")), ()):
            end = _ts(p.get("last_seen"))
            if p["id"] == l["id"] or not end or _days(end, start) < -1:
                continue                 # same ad, or still listed alongside (a duplicate, not a re-post)
            if abs(p["area_m2"] - area) > 0.03 * area:
                continue
            if l.get("district") and p.get("district") and l["district"] != p["district"]:
                continue
            if l.get("rooms") and p.get("rooms") and l["rooms"] != p["rooms"]:
                continue
            pp = total_price(p)
            if not pp or not 0.99 <= pp / price <= 1.25:
                continue                 # cheaper before, or a different flat in the same building
            pw = distinct(p.get("title"))
            same_photo = _photo(l) is not None and _photo(l) == _photo(p)
            if not same_photo and not (len(words & pw) >= 2 and len(words & pw) / len(words | pw) >= 0.5):
                continue
            earlier.append({"id": p["id"], "first_seen": (p.get("first_seen") or "")[:10],
                            "last_seen": (p.get("last_seen") or "")[:10], "price": round(pp)})
        if earlier:
            earlier.sort(key=lambda e: e["first_seen"])
            l["earlier_ads"] = earlier
            found += 1
    return found


def on_market_since(l: dict, today: date | None = None) -> tuple[int, str]:
    """(days on the market, how we know). Uses the oldest of: the ad's original
    posting date, our first sighting, the ad number's age (Reklama5 numbers grow
    ~975 a day, so a renewed ad still betrays its age) and earlier ads for the
    same property."""
    today = today or date.today()
    cands = []
    for key, why in (("posted", "posted"), ("first_seen", "first seen")):
        try:
            cands.append(((today - date.fromisoformat((l.get(key) or "")[:10])).days, why))
        except ValueError:
            pass
    if l.get("id_age_days") is not None:
        cands.append((l["id_age_days"], "ad number"))
    for e in l.get("earlier_ads") or ():
        try:
            cands.append(((today - date.fromisoformat(e["first_seen"])).days, "earlier ad"))
        except ValueError:
            pass
    return max(cands, default=(days_listed(l), "renewed"))


# ------------------------------------------------------------ opening offer
def _opening_offer(price, drop, ref, area, discount, *, months=0.0, cuts=0, relisted=0, urgent=False,
                   explain=False):
    """Where to start talking, from the usual ~10% asking-to-sale gap:
    wider (up to 20%) for months on the market, repeated cuts or re-posting,
    urgent wording or a price still above the local median; narrower (never
    under 8%) when the seller already cut 15%+ or is already below the median.
    Never below 90% of the typical local value (an insulting lowball gets no
    counter-offer) and always at least 8% under asking."""
    gap, why = USUAL_GAP, ["10% = usual gap between asking and sale price"]
    if months >= 6:
        gap += 0.03
        why.append(f"+3% on the market ~{months:.0f} months")
    if cuts >= 2 or relisted:
        gap += 0.02
        why.append("+2% cut more than once" if cuts >= 2 else "+2% re-posted ad")
    if urgent:
        gap += 0.02
        why.append("+2% urgent wording")
    if discount is not None and discount < -0.10:
        gap += 0.03
        why.append("+3% still above the local median")
    if drop >= 0.15:
        gap -= 0.04
        why.append("−4% already cut 15%+")
    if discount is not None and discount > 0.10:
        gap -= 0.03
        why.append("−3% already under the local median")
    if gap < 0.08:
        why.append("but at least 8% under asking")
    elif gap > 0.20:
        why.append("but no more than 20% under")
    gap = clamp(gap, 0.08, 0.20)
    offer = price * (1 - gap)
    floor = ref * area * 0.9 if ref and area and discount is not None else 0
    if offer < floor < price * 0.92:
        why.append("raised to 90% of the typical local value (a lowball gets no counter-offer)")
    offer = int(min(max(offer, floor), price * 0.92) // 100 * 100)      # round down: never above the cap
    if not explain:
        return offer
    return offer, f"{1 - offer / price:.0%} under asking: " + ", ".join(why)


# -------------------------------------------------------------------- run
def run(listings: list[dict], market: Market, cfg: dict) -> list[dict]:
    c = cfg["motivated"]
    urgent = KeywordSet(c["urgent_keywords"])
    hold, confirm = c.get("min_hold_days", 1), c.get("confirm_days", 0.4)
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

        # Cuts: our own history plus the site's strike-through price, in the
        # listing's own unit (total or per m²), converted to totals for display.
        raw = l["price_eur"]
        story = price_story(l, hold, confirm)
        drop, start = story["drop"], story["peak"] * (price / raw)
        old = l.get("site_old_price")
        if l.get("price_note") == "per_m2" and old and old > 6000:
            # Now "2,600 €/m²", struck-through "152,000 €" total: compare totals.
            start, drop, story["site"], story["cuts"] = old, 1 - price / old, True, 1
        if drop > c["max_believable_drop"] or drop < 0:   # 140,000 → 1,400 is a unit change, not a cut
            drop, start = 0, price
        cuts = story["cuts"] if drop > 0 else 0
        # An earlier ad for the same property that asked more is a cut too.
        earlier = l.get("earlier_ads") or []
        top_earlier = max(earlier, key=lambda e: e["price"]) if earlier else None
        relist_drop = 1 - price / top_earlier["price"] if top_earlier else 0
        if not 0 < relist_drop <= c["max_believable_drop"]:
            relist_drop = 0
        words = urgent.find(norm(text_of(l)))
        age = days_listed(l)
        real_age, age_src = on_market_since(l)

        signals = []
        if drop >= c["min_price_drop"] and story["pending"]:
            signals.append(f"new price cut {drop:.0%} ({start:,.0f} → {price:,.0f} €), "
                           "seen once — waiting for the next run to confirm")
            drop, cuts = drop / 2, 0          # half credit until it holds
        elif drop >= c["min_price_drop"]:
            how = f"cut {cuts}× " if cuts >= 2 else "price cut "
            src = " (the site shows the old price)" if story["site"] and cuts <= 1 else ""
            when = f" since {story['since']:%d %b}" if story.get("since") else ""
            signals.append(f"{how}{drop:.0%} ({start:,.0f} → {price:,.0f} €){when}{src}")
        if relist_drop >= c["min_price_drop"]:
            signals.append(f"re-posted: an earlier ad for the same property asked {top_earlier['price']:,.0f} € "
                           f"({top_earlier['first_seen']}), now {relist_drop:.0%} less")
        elif len(earlier) >= c.get("min_reposts", 2):
            signals.append(f"re-posted {len(earlier)}× since {earlier[0]['first_seen']} — still unsold")
        if words:
            signals.append("urgent wording: " + ", ".join(words[:3]))
        if not signals:
            continue
        if real_age >= c["stale_listing_days"]:
            months = real_age / 30.4
            signals.append(f"on the market ~{months:.0f} months" if months >= 2 else f"on the market {real_age} days")
        # (Renewing alone isn't a signal: most Reklama5 sellers renew every few
        # weeks. Renewals only feed the time on market, through the ad number.)

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

        best_drop = max(drop, relist_drop)
        repeated = max(0, cuts - 1) + min(len(earlier), 2) / 2      # cut again, re-posted
        score = 100 * clamp(
            0.35 * clamp(best_drop / 0.2)
            + 0.10 * clamp(repeated / 1.5)
            + 0.15 * clamp(len(words) / 2)
            + 0.15 * clamp(real_age / 365)
            + 0.25 * clamp(((discount or 0) + 0.05) / 0.35)
        )
        offer, why = _opening_offer(price, best_drop, ref, l.get("area_m2"), discount, months=real_age / 30.4,
                                    cuts=cuts, relisted=len(earlier), urgent=bool(words), explain=True)
        r = base(l)
        r.update({
            "score": round(score),
            "utilities": util,
            "qualified": qualified,
            "reasons": signals,
            "metrics": {
                "Price now": round(price),
                "Highest price seen": round(max(start, top_earlier["price"] if relist_drop else 0)),
                "Price cut": f"{best_drop:.0%}" if best_drop > 0 else "—",
                "Price cuts seen": cuts or None,
                "Earlier ads": len(earlier) or None,
                "Days since renewed": age,
                "On the market (est.)": f"~{real_age} days ({age_src})" if real_age > age else None,
                "vs typical price nearby": f"{-discount:+.0%}" if discount is not None else None,
                "Opening offer idea": offer,
                "Why this offer": why,
            },
            "sort_value": best_drop,
        })
        out.append(r)
    out.sort(key=lambda r: -r["score"])
    return out
