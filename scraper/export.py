"""Write docs/data.json — everything the static dashboard needs."""
from __future__ import annotations

import json
from pathlib import Path

from .db import now


def write(path: Path, results: dict, market, db, cfg: dict, researchers) -> None:
    cap = cfg["export"]["max_items_per_researcher"]
    payload = {
        "generated_at": now(),
        "listings_tracked": db.count(),
        "runs": db.recent_runs(14),
        "researchers": [
            {
                "key": r.NAME,
                "title": r.TITLE,
                # Docstring: "Researcher N — title" / blank line / explanation.
                "description": " ".join(((r.__doc__ or "").strip().split("\n\n") + [""])[1].split()),
                "count": len(results.get(r.NAME, [])),
                "qualified": sum(1 for x in results.get(r.NAME, []) if x["qualified"]),
                "items": results.get(r.NAME, [])[:cap],
            }
            for r in researchers if r.NAME in results
        ],
        "market": market.summary(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)
