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
from contextlib import closing
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
-- Facts you learned from the seller ("water yes", "area 450"), applied on every run.
CREATE TABLE IF NOT EXISTS overrides (
    listing_id TEXT NOT NULL,
    field      TEXT NOT NULL,       -- price_eur | area_m2 | electricity | water | road | land_type | note
    value      TEXT,
    set_at     TEXT NOT NULL,
    PRIMARY KEY (listing_id, field)
);
-- "✅ Fits": you checked the open questions with the seller.
CREATE TABLE IF NOT EXISTS confirmations (listing_id TEXT PRIMARY KEY, confirmed_at TEXT NOT NULL);
-- Why you rejected a deal (reason code from scraper/reasons.py) + a snapshot of
-- the deal, so later deals can be compared with it.
CREATE TABLE IF NOT EXISTS rejections (
    listing_id TEXT NOT NULL,
    researcher TEXT NOT NULL,
    reason     TEXT NOT NULL,
    note       TEXT,
    kind       TEXT,
    city       TEXT,
    district   TEXT,
    area       REAL,
    price      REAL,
    title      TEXT,
    rejected_at TEXT NOT NULL,
    PRIMARY KEY (listing_id, researcher)
);
-- Which Telegram message shows which deal, so replies can be matched.
CREATE TABLE IF NOT EXISTS messages (
    message_id INTEGER PRIMARY KEY,
    listing_id TEXT NOT NULL,
    researcher TEXT NOT NULL
);
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


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def set_override(conn, listing_id: str, field: str, value) -> None:
    conn.execute("INSERT OR REPLACE INTO overrides VALUES (?,?,?,?)", (listing_id, field, str(value), _now()))
    conn.commit()


def confirm(conn, listing_id: str) -> None:
    conn.execute("INSERT OR REPLACE INTO confirmations VALUES (?,?)", (listing_id, _now()))
    conn.commit()


def remember_message(conn, message_id: int, listing_id: str, researcher: str) -> None:
    conn.execute("INSERT OR REPLACE INTO messages VALUES (?,?,?)", (message_id, listing_id, researcher))
    conn.commit()


def message_listing(conn, message_id: int) -> tuple[str, str] | None:
    r = conn.execute("SELECT listing_id, researcher FROM messages WHERE message_id=?", (message_id,)).fetchone()
    return (r[0], r[1]) if r else None


def reject(conn, listing_id: str, researcher: str, reason: str, listing: dict | None) -> None:
    l = listing or {}
    conn.execute("INSERT OR REPLACE INTO rejections VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (listing_id, researcher, reason, None, l.get("kind"), l.get("city"), l.get("district"),
                  l.get("area_m2"), l.get("price_eur"), l.get("title"), _now()))
    conn.commit()


def add_rejection_note(conn, listing_id: str, note: str) -> bool:
    cur = conn.execute("UPDATE rejections SET note = COALESCE(note || ' / ', '') || ? WHERE listing_id = ?",
                       (note, listing_id))
    conn.commit()
    return cur.rowcount > 0


def undo(conn, listing_id: str, researcher: str) -> None:
    conn.execute("DELETE FROM rejections WHERE listing_id=? AND researcher=?", (listing_id, researcher))
    conn.execute("DELETE FROM votes WHERE listing_id=? AND researcher=?", (listing_id, researcher))
    conn.commit()


def load_rejections(path: Path | None = None) -> list[dict]:
    path = path or DB_PATH
    if not path.exists():
        return []
    with closing(connect(path)) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM rejections")]


def load_overrides(path: Path | None = None) -> tuple[dict[str, dict], set[str]]:
    """({listing_id: {field: value}}, {confirmed listing ids})."""
    path = path or DB_PATH
    if not path.exists():
        return {}, set()
    with closing(connect(path)) as conn:
        facts: dict[str, dict] = {}
        for r in conn.execute("SELECT listing_id, field, value FROM overrides"):
            facts.setdefault(r[0], {})[r[1]] = r[2]
        confirmed = {r[0] for r in conn.execute("SELECT listing_id FROM confirmations")}
    return facts, confirmed


def apply_overrides(listings: list[dict], facts: dict[str, dict], confirmed: set[str]) -> int:
    """Put what you learned from sellers into the listings before scoring."""
    n = 0
    for l in listings:
        f = facts.get(l["id"])
        if f:
            n += 1
            if "price_eur" in f:
                l["price_eur"], l["price_note"] = float(f["price_eur"]), None
            if "area_m2" in f:
                l["area_m2"] = float(f["area_m2"])
            util = dict(l.get("utilities") or {})
            for k in ("electricity", "water", "road"):
                if k in f:
                    util[k] = f[k] == "yes"
            l["utilities"] = util
            if "land_type" in f:
                l["land_type"] = l["land_type_confirmed"] = f["land_type"]
            l["owner_note"] = f.get("note")
        if l["id"] in confirmed:
            l["confirmed"] = True
            l["detail_at"] = l.get("detail_at") or "confirmed"
    return n


def get_meta(conn, key: str, default=None):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def set_meta(conn, key: str, value):
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))
    conn.commit()


def load_votes(path: Path | None = None) -> list[dict]:
    path = path or DB_PATH
    if not path.exists():
        return []
    with closing(connect(path)) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM votes")]


def _lean(votes: list[dict]) -> tuple[float, int, int]:
    up = sum(1 for v in votes if v["vote"] > 0)
    dn = sum(1 for v in votes if v["vote"] < 0)
    return (up - dn) / (up + dn + 3), up, dn


def apply(results: dict, votes: list[dict], personalize: bool = True, rejections: list[dict] | None = None,
          listings: dict | None = None) -> dict:
    """Drop 👎 deals and (if personalize) adjust scores from your votes.

    The public dashboard uses personalize=False: a vanished listing reveals
    nothing, but "+8 from your votes in Карпош" would show your preferences.
    """
    if not votes and not rejections:
        return results
    from .reasons import penalties
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
                where = f" in {item['district']}" if (d_up or d_dn) else ""
                reasons.append(f"{delta:+.0f} from your votes on similar deals{where}")
            # What your rejection reasons say about deals like this one.
            pen, why = penalties(item, (listings or {}).get(item["id"], item), rejections or [], researcher)
            reasons += why
            delta += pen
            if abs(delta) >= 2:
                item["score"] = int(max(0, min(100, round(item["score"] + delta))))
            item["reasons"] = reasons
            kept.append(item)
        kept.sort(key=lambda x: (not x.get("qualified", True), -x["score"]))
        out[researcher] = kept
    return out
