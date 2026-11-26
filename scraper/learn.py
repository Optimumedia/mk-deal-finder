"""What your votes and rejections taught the researchers — and what to change.

    python -m scraper.learn        (also the /learn command in Telegram)

Reads data/feedback.db (your private votes, rejection reasons, notes and
pipeline) and data/private.json (today's personalised scores) and prints:
- what the deals you liked have in common (towns, size, price, altitude);
- how often each rejection reason was used, with your notes, and how the
  scoring now reacts to it;
- which of today's deals moved because of it;
- concrete config suggestions and questions once a pattern is clear;
- data-quality problems for the developer (misleading prices, duplicates, gone ads).
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tomllib
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path
from statistics import median

from . import feedback
from .reasons import BY_CODE, altitude_profile, effective_reason

ROOT = Path(__file__).resolve().parent.parent
PRIVATE_JSON = ROOT / "data" / "private.json"
DEALS_DB = ROOT / "data" / "deals.db"
MIN_AREA_KEY = {"flip": ("flip", "min_area_m2"), "land": ("land", "min_area_m2"), "weekend": ("weekend", "min_area_m2")}
EFFECT = {"learn": "similar deals now rank lower", "size_min": "smaller deals now rank lower",
          "size_max": "bigger deals now rank lower", "data": "flagged as a data problem to fix",
          "self": "only this deal hidden"}
UNINFORMATIVE_NOTES = ("dropped from the pipeline",)


def _eur(v) -> str:
    return f"€{v:,.0f}"


def _span(vals, unit="", money=False) -> str:
    # a plot "for €325" is a misread price, not a preference
    vals = sorted(v for v in vals if v and not (money and v < 1000))
    if not vals:
        return "?"
    f = _eur if money else (lambda v: f"{v:,.0f}{unit}")
    return f(vals[0]) if len(vals) == 1 else f"{f(vals[0])}–{f(vals[-1])}"


def _today() -> list[dict]:
    """Today's personalised deals (private dashboard), or [] before the first run."""
    try:
        data = json.loads(PRIVATE_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for r in data.get("researchers", []):
        out += [{**x, "researcher": r["key"]} for x in r.get("items", [])]
    return out


def _stored(ids) -> dict[str, dict]:
    """The listings you voted on, read-only from deals.db (altitude, description, per-m² prices)."""
    ids = list(ids)
    if not ids or not DEALS_DB.exists():
        return {}
    try:
        with closing(sqlite3.connect(DEALS_DB.as_uri() + "?mode=ro", uri=True, timeout=30)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(f"SELECT * FROM listings WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
    except sqlite3.Error:
        return {}
    return {r["id"]: dict(r) for r in rows}


def report() -> str:
    rejections = feedback.load_rejections()
    votes = feedback.load_votes()
    liked = feedback.liked_snapshots()
    if not rejections and not votes and not liked:
        return ("Nothing to learn from yet. Tap ❌ / 👎 on deals that don't fit and pick a reason — "
                "after a few, I'll show the patterns and suggest settings here.")
    cfg = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
    ids = {r["listing_id"] for r in rejections} | {v["listing_id"] for v in votes} | {s["id"] for s in liked}
    exs = feedback.examples({}, votes, rejections, _stored(ids), liked)
    n_up = sum(1 for e in exs if e.vote > 0)
    bare_down = sum(1 for e in exs if e.vote < 0) - len(rejections)
    lines = [f"What I learned from you — {len(rejections)} rejection(s)"
             + (f" + {bare_down} 👎 without a reason" if bare_down > 0 else "") + f", {n_up} 👍/✅", ""]
    suggestions, data_issues = [], []

    # What the deals you liked have in common.
    snaps = {s["id"]: s for s in liked}
    up = [e for e in exs if e.vote > 0]
    if up:
        lines.append("What you seem to like:")
        for researcher in sorted({e.researcher for e in up}):
            group = [e for e in up if e.researcher == researcher]
            towns = Counter(e.city for e in group if e.city)
            utils = [snaps.get(e.id, {}).get("utilities") or {} for e in group]
            all3 = sum(1 for u in utils if u and all(v == "yes" for v in u.values()))
            building = sum(1 for e in group if (snaps.get(e.id, {}).get("metrics") or {}).get("Land type") == "Building")
            bits = [f"{len(group)} deal(s)", "in " + ", ".join(f"{t} ({c})" for t, c in towns.most_common())]
            bits.append(f"{_span([e.area for e in group], ' m²')}, {_span([e.price for e in group], money=True)}")
            elevs = [e.elevation for e in group if e.elevation is not None]
            if elevs:
                bits.append(f"altitude {_span(elevs, ' m')} (median {median(elevs):,.0f} m)")
            if building:
                bits.append(f"{building} building land")
            if all3:
                bits.append(f"{all3} with power, water and road confirmed")
            lines.append(f"  ■ {researcher}: " + "; ".join(bits))
        lines.append("")

    # Rejections, per researcher.
    by_r: dict[str, list[dict]] = defaultdict(list)
    for rj in rejections:
        by_r[rj["researcher"]].append(rj)
    for researcher, rjs in sorted(by_r.items()):
        lines.append(f"■ {researcher} — turned down")
        for code, n in Counter(effective_reason(r) for r in rjs).most_common():
            reason = BY_CODE.get(code)
            label = reason.label if reason else code
            group = [r for r in rjs if effective_reason(r) == code]
            remapped = sum(1 for r in group if r["reason"] != code)
            effect = EFFECT.get(reason.learn if reason else "", "")
            if code in ("nor", "noe", "now"):
                where = sorted({r["district"] for r in group if r.get("district")})
                effect = ("ads that only say it's “nearby” / “can be connected” / a dirt road now rank lower; "
                          f"the question is added to plots in {', '.join(where)}" if where else
                          "ads that only say it's “nearby” / “can be connected” now rank lower")
            elif code == "oth":
                effect = "no reason given, so only re-posts and near-copies of these ads rank lower"
            lines.append(f"  {n}× {label}" + (f" (from your note{'s' if remapped > 1 else ''})" if remapped else "")
                         + f" — {effect}")
            notes = [r["note"] for r in group if r.get("note") and r["note"] not in UNINFORMATIVE_NOTES]
            for note in notes[:3]:
                lines.append(f"      “{note[:120]}”")
            areas = [r["area"] for r in group if r.get("area")]
            if code == "sml" and len(areas) >= 3 and researcher in MIN_AREA_KEY:
                sec, key = MIN_AREA_KEY[researcher]
                limit = round(median(areas) / 5) * 5 + 5
                if limit > cfg[sec][key]:
                    suggestions.append(f"[{sec}] {key} = {limit}   (now {cfg[sec][key]}; "
                                       f"{len(areas)} 'too small' rejections, median {median(areas):.0f} m²)")
            if code == "agr" and researcher == "land" and n >= 3 and not cfg["land"]["require_building_land"]:
                suggestions.append(f"[land] require_building_land = true   ({n}× 'agricultural — can't build')")
            if code == "rnt" and researcher == "airbnb" and len(group) >= 3:
                rents = [r["price"] for r in group if r.get("price")]
                if rents and median(rents) - 50 < cfg["airbnb"]["max_rent"]:
                    suggestions.append(f"[airbnb] max_rent = {int(median(rents) - 50)}   (now {cfg['airbnb']['max_rent']}; "
                                       f"{len(rents)}× 'rent too high', median {median(rents):.0f} €)")
            if code == "shr" and n >= 1 and cfg["auctions"]["include_partial_shares"]:
                suggestions.append("[auctions] include_partial_shares = false")
            if code in ("nor", "noe", "now") and n >= 2:
                what = {"nor": "a built road reaches", "noe": "power is connected on", "now": "water is connected on"}[code]
                suggestions.append(f"Before calling about a plot, ask whether {what} the plot itself — "
                                   f"{n} of your rejections were ads that said “nearby” or “possible”.")
            if code == "sold":
                data_issues += [f"Ad already gone when you opened it: {r['title'] or r['listing_id']}" for r in group[:3]]
            if reason and reason.learn == "data":
                data_issues += [f"{label}: {r['title'] or r['listing_id']}" for r in group[:3]]
        districts = Counter(r["district"] for r in rjs if effective_reason(r) == "loc" and r.get("district"))
        if districts:
            lines.append("  bad-location districts: " + ", ".join(f"{d} ({c})" for d, c in districts.most_common(5)))
        bare = sum(1 for r in rjs if r["reason"] == "oth" and (r.get("note") or "") in UNINFORMATIVE_NOTES + ("",))
        if bare >= 2:
            suggestions.append(f"{bare} {researcher} deals were dropped without a reason. Next time pick one "
                               "(no road, location, price…) — that's what the scoring learns from.")
        lines.append("")

    # Altitude: the clearest pattern so far for plots.
    plots = [e for e in exs if e.researcher in ("land", "weekend")]
    prof = altitude_profile(plots)
    if prof:
        lines += [f"Altitude: plots you liked sit at about {prof['liked']:,.0f} m ({prof['n_up']} plots), the ones you "
                  f"turned down at about {prof['rejected']:,.0f} m ({prof['n_down']}). Plots below "
                  f"~{prof['line']:,.0f} m now rank up to {8 * prof['weight']:.0f} points lower.", ""]
        if prof["sign"] > 0 and prof["liked"] >= cfg["weekend"]["min_elevation"]:
            low_liked = sum(1 for e in plots if e.vote > 0 and e.elevation is not None
                            and e.elevation < cfg["weekend"]["min_elevation"])
            suggestions.append(f"If you only want mountain plots, use the Weekend tab ({cfg['weekend']['min_elevation']} m+) "
                               f"— or set [land] cities = [\"Охрид\"] so Skopje plots need "
                               f"{cfg['land']['mountain_min_elevation']} m+ or a mountain name"
                               + (f" (that would have hidden {low_liked} of the plots you liked)" if low_liked else ""))

    # What moved on today's list.
    today = _today()
    if today:
        tags = {"ranked higher": 0, "ranked lower": 0}
        moved = []
        for x in today:
            why = [r for r in x.get("reasons", []) if "ranked higher" in r or "ranked lower" in r
                   or "you rejected" in r or "question added" in r]
            for t in tags:
                tags[t] += any(t in r for r in why)
            if why:
                moved.append((x, why))
        asked = sum(1 for x in today if any(q.startswith("You turned down") for q in x.get("questions") or []))
        if moved:
            lines.append(f"On today's list: {tags['ranked higher']} deal(s) ranked higher, {tags['ranked lower']} "
                         f"lower, {asked} got a question from your rejections. For example:")
            # One example of each kind of change first (re-post, near-copy, like, altitude, question…), best first.
            kinds = ("re-post", "same area, size and price", "you marked interested", " m — plots you liked",
                     "question added", "you rejected")
            picked, ids = [], set()
            for k in kinds + ("",):
                for x, why in sorted(moved, key=lambda t: -t[0].get("score", 0)):
                    line = next((r for r in why if k in r), None)
                    if line and x["id"] not in ids and len(picked) < 6:
                        picked.append((x, line))
                        ids.add(x["id"])
                        if k:
                            break
            for x, line in picked:
                lines.append(f"  • {x['title'][:60]} ({x['researcher']}, now {x.get('score')}): {line}")
            lines.append("")
    if suggestions:
        lines += ["Suggestions:"] + [f"  {s}" for s in suggestions] + [""]
    if data_issues:
        lines += ["Data problems for the developer to fix:"] + [f"  • {d}" for d in data_issues[:10]]
    return "\n".join(lines).strip()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(report())
