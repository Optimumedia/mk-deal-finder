"""Your 👍 / 👎 votes from Telegram, and how they change scores.

Votes live in data/feedback.db — local only (git-ignored): the repo is public
and which deals you like is nobody else's business.

Effect on results:
- 👎 on a deal hides it everywhere (dashboard + alerts).
- Votes on similar deals for the same researcher nudge scores by up to ±20:
  same district ±10, same city ±5, same property type ±5, each scaled by
  (likes − dislikes) / (likes + dislikes + 3) so one vote is a nudge, not a verdict.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "feedback.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS votes (
    listing_id TEXT NOT NULL,
    researcher TEXT NOT NULL,
    vote       INTEGER NOT NULL,      -- +1 interested, -1 not for me
    kind       TEXT,
    city       TEXT,
    district   TEXT,
    title      TEXT,
    voted_at   TEXT NOT NULL,
    PRIMARY KEY (listing_id, researcher)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def record(conn, listing_id: str, researcher: str, vote: int, listing: dict | None):
    listing = listing or {}
    conn.execute(
        "INSERT OR REPLACE INTO votes VALUES (?,?,?,?,?,?,?,?)",
        (listing_id, researcher, vote, listing.get("kind"), listing.get("city"), listing.get("district"),
         listing.get("title"), datetime.now(timezone.utc).replace(microsecond=0).isoformat()),
    )
    conn.commit()


def get_meta(conn, key: str, default=None):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def set_meta(conn, key: str, value):
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))
    conn.commit()


def load_votes(path: Path = DB_PATH) -> list[dict]:
    if not path.exists():
        return []
    with connect(path) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM votes")]


def _lean(votes: list[dict]) -> tuple[float, int, int]:
    up = sum(1 for v in votes if v["vote"] > 0)
    dn = sum(1 for v in votes if v["vote"] < 0)
    return (up - dn) / (up + dn + 3), up, dn


def apply(results: dict, votes: list[dict], personalize: bool = True) -> dict:
    """Drop 👎 deals and (if personalize) adjust scores from your votes.

    The public dashboard uses personalize=False: a vanished listing reveals
    nothing, but "+8 from your votes in Карпош" would show your preferences.
    """
    if not votes:
        return results
    out = {}
    for researcher, items in results.items():
        mine = [v for v in votes if v["researcher"] == researcher]
        rejected = {v["listing_id"] for v in mine if v["vote"] < 0}
        kept = []
        for item in items:
            if item["id"] in rejected:
                continue
            if not personalize:
                kept.append(item)
                continue
            item = dict(item)
            others = [v for v in mine if v["listing_id"] != item["id"]]
            d_lean, d_up, d_dn = _lean([v for v in others if item.get("district") and v["district"] == item.get("district")
                                        and v["city"] == item.get("city")])
            c_lean, _, _ = _lean([v for v in others if v["city"] == item.get("city")])
            k_lean, _, _ = _lean([v for v in others if v["kind"] == item.get("kind")])
            delta = 10 * d_lean + 5 * c_lean + 5 * k_lean
            reasons = list(item.get("reasons", []))
            if abs(delta) >= 2:
                item["score"] = int(max(0, min(100, round(item["score"] + delta))))
                where = f" in {item['district']}" if (d_up or d_dn) else ""
                reasons.append(f"{delta:+.0f} from your votes on similar deals{where}")
            item["reasons"] = reasons
            kept.append(item)
        kept.sort(key=lambda x: (not x.get("qualified", True), -x["score"]))
        out[researcher] = kept
    return out
