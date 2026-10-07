"""Deal ratings: turn a 0–100 score into a tier a person can act on.

    💎 Once-in-a-lifetime   score ≥ 90   ┐ only with solid evidence —
    🔥 Exceptional deal     score ≥ 80   ┘ otherwise capped at "Great deal"
    ⭐ Great deal           score ≥ 70
    👍 Good deal            score ≥ 55
    👀 Worth a look         below 55

Solid evidence = detail page read, no open questions (❓ needs info), no ⚠ warnings, utilities confirmed where
the researcher requires it, and a local (district/city) market comparison
where the deal depends on one. A parsing glitch must never be called a
once-in-a-lifetime deal.
"""
from __future__ import annotations

TIERS = [
    (90, "once", "💎", "Once-in-a-lifetime"),
    (80, "exceptional", "🔥", "Exceptional deal"),
    (70, "great", "⭐", "Great deal"),
    (55, "good", "👍", "Good deal"),
    (0, "watch", "👀", "Worth a look"),
]
_ORDER = [t[1] for t in TIERS]
# Researchers whose verdict rests on a below-market comparison.
NEEDS_MARKET = {"land", "flip", "auctions"}


def _evidence_ok(item: dict, researcher: str) -> bool:
    if not item.get("qualified", True) or not item.get("has_details", True):
        return False
    if item.get("status") in ("needs_info", "pending"):
        return False            # open questions: can't be "once in a lifetime" yet
    if any("⚠" in r for r in item.get("reasons", [])):
        return False
    if researcher in NEEDS_MARKET and not (item.get("metrics") or {}).get("Below market"):
        return False
    return True


MIN_COMPS = {"once": 30, "exceptional": 15}     # comparable listings behind a top rating


def rate(item: dict, researcher: str) -> dict:
    score = item.get("score", 0)
    tier = next(t for t in TIERS if score >= t[0])
    if tier[1] in ("once", "exceptional") and not _evidence_ok(item, researcher):
        tier = TIERS[2]
    if researcher in NEEDS_MARKET:
        comps = item.get("comps") or 0
        if tier[1] == "once" and comps < MIN_COMPS["once"]:
            tier = TIERS[1]                 # 💎 only against a well-populated local market
        if tier[1] == "exceptional" and comps < MIN_COMPS["exceptional"]:
            tier = TIERS[2]
    if not item.get("qualified", True):
        tier = max(tier, TIERS[3], key=lambda t: _ORDER.index(t[1]))   # near-misses: "good" at best
    return {"tier": tier[1], "emoji": tier[2], "label": tier[3], "rank": _ORDER.index(tier[1])}


def rate_all(results: dict) -> dict:
    """Add item['rating'] everywhere and sort best tier first, then by score."""
    out = {}
    for researcher, items in results.items():
        rated = [{**x, "rating": rate(x, researcher)} for x in items]
        rated.sort(key=lambda x: (x["rating"]["rank"], -x.get("score", 0)))
        out[researcher] = rated
    return out


def label(item: dict) -> str:
    r = item.get("rating")
    return f"{r['emoji']} {r['label']}" if r else ""
