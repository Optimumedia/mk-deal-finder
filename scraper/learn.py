"""What your rejections taught the researchers — and what to change.

    python -m scraper.learn        (also the /learn command in Telegram)

Reads data/feedback.db (your private votes and rejection reasons) and prints:
- how often each reason was used, per researcher;
- how the scoring now reacts to it (similar deals lose points);
- concrete config suggestions once a pattern is clear (e.g. a minimum size);
- data-quality problems for the developer (misleading prices, duplicates, fakes).
"""
from __future__ import annotations

import sys
import tomllib
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median

from . import feedback
from .reasons import BY_CODE

ROOT = Path(__file__).resolve().parent.parent
MIN_AREA_KEY = {"flip": ("flip", "min_area_m2"), "land": ("land", "min_area_m2")}


def report() -> str:
    rejections = feedback.load_rejections()
    votes = feedback.load_votes()
    if not rejections and not votes:
        return ("Nothing to learn from yet. Tap ❌ / 👎 on deals that don't fit and pick a reason — "
                "after a few, I'll show the patterns and suggest settings here.")
    cfg = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
    lines = [f"What I learned from you — {len(rejections)} rejection(s), "
             f"{sum(1 for v in votes if v['vote'] > 0)} 👍/✅", ""]
    by_r: dict[str, list[dict]] = defaultdict(list)
    for rj in rejections:
        by_r[rj["researcher"]].append(rj)

    suggestions, data_issues = [], []
    for researcher, rjs in sorted(by_r.items()):
        lines.append(f"■ {researcher}")
        for code, n in Counter(r["reason"] for r in rjs).most_common():
            reason = BY_CODE.get(code)
            label = reason.label if reason else code
            effect = {"learn": "similar deals now rank lower",
                      "size_min": "smaller deals now rank lower", "size_max": "bigger deals now rank lower",
                      "data": "flagged as a data problem to fix", "self": "only this deal hidden"}.get(
                reason.learn if reason else "", "")
            lines.append(f"  {n}× {label} — {effect}")
            group = [r for r in rjs if r["reason"] == code]
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
            if reason and reason.learn == "data":
                data_issues += [f"{label}: {r['title'] or r['listing_id']}" for r in group[:3]]
        districts = Counter(r["district"] for r in rjs if r["reason"] == "loc" and r.get("district"))
        if districts:
            lines.append("  bad-location districts: " + ", ".join(f"{d} ({c})" for d, c in districts.most_common(5)))
        lines.append("")
    if suggestions:
        lines += ["Suggested settings (config.toml):"] + [f"  {s}" for s in suggestions] + [""]
    if data_issues:
        lines += ["Data problems for the developer to fix:"] + [f"  • {d}" for d in data_issues[:10]]
    return "\n".join(lines).strip()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print(report())
