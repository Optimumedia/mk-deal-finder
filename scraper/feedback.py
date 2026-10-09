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

import json
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
-- My pipeline: deals you marked 👍 / ✅, with the stage you're at and the next step.
CREATE TABLE IF NOT EXISTS pipeline (
    listing_id TEXT PRIMARY KEY,
    researcher TEXT NOT NULL,
    stage      TEXT NOT NULL,          -- see STAGES
    next_step  TEXT,
    due        TEXT,                   -- ISO date of the next step
    notes      TEXT,
    snapshot   TEXT,                   -- JSON copy of the deal (kept if the ad disappears)
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- Which Telegram message shows which deal, so replies can be matched.
CREATE TABLE IF NOT EXISTS messages (
    message_id INTEGER PRIMARY KEY,
    listing_id TEXT NOT NULL,
    researcher TEXT NOT NULL
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or DB_PATH          # looked up at call time (tests and tools can redirect it)
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
    """Forget your vote, rejection and confirmation for a deal."""
    conn.execute("DELETE FROM rejections WHERE listing_id=? AND researcher=?", (listing_id, researcher))
    conn.execute("DELETE FROM votes WHERE listing_id=? AND researcher=?", (listing_id, researcher))
    conn.execute("DELETE FROM confirmations WHERE listing_id=?", (listing_id,))
    conn.commit()


def annotate_mine(results: dict, path: Path | None = None) -> dict:
    """Add my_vote / my_reason / my_note / facts to each result (private dashboard only)."""
    path = path or DB_PATH
    if not path.exists():
        return results
    with closing(connect(path)) as conn:
        votes = {(r["listing_id"], r["researcher"]): r["vote"] for r in conn.execute("SELECT * FROM votes")}
        confirmed = {r[0] for r in conn.execute("SELECT listing_id FROM confirmations")}
        rejected = {(r["listing_id"], r["researcher"]): dict(r) for r in conn.execute("SELECT * FROM rejections")}
        facts: dict[str, dict] = {}
        for r in conn.execute("SELECT listing_id, field, value FROM overrides"):
            facts.setdefault(r[0], {})[r[1]] = r[2]
    out = {}
    for researcher, items in results.items():
        rows = []
        for x in items:
            v = votes.get((x["id"], researcher))
            mine = {"my_vote": "ok" if x["id"] in confirmed else "up" if v and v > 0 else "down" if v and v < 0 else None}
            rj = rejected.get((x["id"], researcher))
            if rj:
                mine["my_reason"], mine["my_note"] = rj["reason"], rj["note"]
            if x["id"] in facts:
                f = dict(facts[x["id"]])
                for k in ("area_m2", "price_eur"):
                    if k in f:
                        try:
                            f[k] = float(f[k])
                        except ValueError:
                            f.pop(k)
                mine["facts"] = f
            rows.append({**x, **mine})
        out[researcher] = rows
    return out


def rejected_list(path: Path | None = None) -> list[dict]:
    """Deals you rejected (they disappear from results), for the Undo list."""
    from .reasons import BY_CODE
    path = path or DB_PATH
    if not path.exists():
        return []
    with closing(connect(path)) as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM rejections ORDER BY rejected_at DESC")]
        # 👎 without a reason (older Telegram votes) count as rejections too
        bare = [dict(r) for r in conn.execute(
            "SELECT v.listing_id, v.researcher, v.title, v.voted_at AS rejected_at FROM votes v WHERE v.vote < 0 AND NOT "
            "EXISTS (SELECT 1 FROM rejections r WHERE r.listing_id = v.listing_id AND r.researcher = v.researcher)")]
    out = []
    for r in rows + bare:
        code = r.get("reason")
        reason = BY_CODE.get(code)
        learned = ("similar deals now rank lower" if reason and reason.learn == "learn"
                   else "smaller deals now rank lower" if reason and reason.learn == "size_min"
                   else "bigger deals now rank lower" if reason and reason.learn == "size_max"
                   else "reported as a data problem" if reason and reason.learn == "data"
                   else "this deal only" if reason else "no reason given — nothing learned")
        out.append({"id": r["listing_id"], "researcher": r["researcher"], "title": r.get("title"),
                    "reason": code, "reason_label": reason.label if reason else "no reason given",
                    "learned": learned, "note": r.get("note"), "rejected_at": r.get("rejected_at")})
    return out


STAGES = ["interested", "contacted", "viewing", "checks", "offer", "negotiating", "won", "dropped"]
NEXT_STEP = {
    "interested": "Call the seller / agency",
    "contacted": "Book a viewing",
    "viewing": "Visit: condition, documents, utilities on site",
    "checks": "Check the property sheet (имотен лист), debts and charges on it (товари), zoning / building permit",
    "offer": "Wait for the answer — follow up in 2 days",
    "negotiating": "Agree price, deposit and notary date",
    "won": "Done 🎉",
    "dropped": "—",
}
NEXT_STEP_AUCTION = {
    "interested": "Read the official notice",
    "contacted": "Ask the bailiff for a viewing",
    "checks": "Check debts and charges (товари) and whether it's occupied",
    "offer": "Pay the deposit before the sale",
    "negotiating": "Attend the sale",
}


def default_next_step(stage: str, researcher: str) -> str:
    if researcher == "auctions" and stage in NEXT_STEP_AUCTION:
        return NEXT_STEP_AUCTION[stage]
    return NEXT_STEP.get(stage, "")


def pipeline_add(conn, listing_id: str, researcher: str) -> None:
    """Put a deal into My pipeline (stage 'interested') unless it's already there."""
    ts = _now()
    conn.execute("INSERT OR IGNORE INTO pipeline (listing_id, researcher, stage, next_step, created_at, updated_at) "
                 "VALUES (?,?,?,?,?,?)",
                 (listing_id, researcher, "interested", default_next_step("interested", researcher), ts, ts))
    conn.commit()


def pipeline_update(conn, listing_id: str, **fields) -> bool:
    row = conn.execute("SELECT researcher, stage, next_step FROM pipeline WHERE listing_id=?", (listing_id,)).fetchone()
    if not row:
        return False
    sets = {}
    if "stage" in fields and fields["stage"] in STAGES and fields["stage"] != row["stage"]:
        sets["stage"] = fields["stage"]
        # Moving on: the next step becomes that stage's default unless you typed one.
        if "next_step" not in fields:
            sets["next_step"] = default_next_step(fields["stage"], row["researcher"])
    if "next_step" in fields:
        sets["next_step"] = (fields["next_step"] or "")[:300]
    if "due" in fields:
        sets["due"] = fields["due"] or None
    if "notes" in fields:
        sets["notes"] = (fields["notes"] or "")[:5000]
    if sets:
        sets["updated_at"] = _now()
        conn.execute(f"UPDATE pipeline SET {', '.join(k + '=?' for k in sets)} WHERE listing_id=?",
                     (*sets.values(), listing_id))
        conn.commit()
    return True


def pipeline_remove(conn, listing_id: str) -> None:
    conn.execute("DELETE FROM pipeline WHERE listing_id=?", (listing_id,))
    conn.commit()


def load_pipeline(path: Path | None = None) -> list[dict]:
    path = path or DB_PATH
    if not path.exists():
        return []
    with closing(connect(path)) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM pipeline ORDER BY updated_at DESC")]


def take_pipeline(results: dict, path: Path | None = None) -> tuple[dict, list[dict]]:
    """Move pipeline deals out of the result lists into their own list.

    Each entry carries the latest version of the deal if it's still found,
    otherwise the saved copy (flagged removed). Saved copies are refreshed."""
    entries = load_pipeline(path)
    if not entries:
        return results, []
    ids = {e["listing_id"] for e in entries}
    latest: dict[str, dict] = {}
    kept = {}
    for researcher, items in results.items():
        kept[researcher] = []
        for x in items:
            if x["id"] in ids:
                latest.setdefault(x["id"], x)
            else:
                kept[researcher].append(x)
    out = []
    with closing(connect(path or DB_PATH)) as conn:
        for e in entries:
            item = latest.get(e["listing_id"])
            if item is not None:
                conn.execute("UPDATE pipeline SET snapshot=? WHERE listing_id=?",
                             (json.dumps(item, ensure_ascii=False), e["listing_id"]))
            elif e.get("snapshot"):
                item = json.loads(e["snapshot"])
            out.append({"id": e["listing_id"], "researcher": e["researcher"], "stage": e["stage"],
                        "next_step": e["next_step"], "due": e["due"], "notes": e["notes"],
                        "created_at": e["created_at"], "updated_at": e["updated_at"],
                        # not in today's results: sold, removed, or now outside your filters
                        "removed": e["listing_id"] not in latest, "item": item or {"id": e["listing_id"]}})
        conn.commit()
    return kept, out


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
