"""Ideas, your 1-10 ratings and run history in data/ideas.db.

Local only (git-ignored), like data/feedback.db: the repo is public and which
business ideas you like is nobody else's business. Back the file up if you care
about the learning history.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "ideas.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ideas (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at       TEXT NOT NULL,
    run_date         TEXT NOT NULL,          -- local date of the run that wrote it
    lens             TEXT,
    title            TEXT NOT NULL,
    one_liner        TEXT,
    category         TEXT,
    business_model   TEXT,
    channel          TEXT,
    customer_type    TEXT,
    geography        TEXT,
    startup_cost_eur INTEGER,
    automation_pct   INTEGER,
    weeks_to_revenue INTEGER,
    profit_low_eur   INTEGER,
    profit_high_eur  INTEGER,
    self_score       INTEGER,                -- Claude's own 1-10
    data             TEXT NOT NULL,          -- the full idea as JSON
    status           TEXT NOT NULL DEFAULT 'held',   -- held | sent
    predicted        REAL,                   -- what the model expected you to rate it, when sent
    explore          INTEGER NOT NULL DEFAULT 0,     -- 1 = sent as the wildcard
    sent_at          TEXT,
    rating           INTEGER,                -- yours, 1-10
    rated_at         TEXT,
    note             TEXT                    -- your reply: why
);
CREATE INDEX IF NOT EXISTS ideas_status ON ideas(status, run_date);
CREATE TABLE IF NOT EXISTS messages (message_id INTEGER PRIMARY KEY, idea_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date    TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,               -- running | ok | error
    lenses      TEXT,
    candidates  INTEGER DEFAULT 0,
    kept        INTEGER DEFAULT 0,
    sent        INTEGER DEFAULT 0,
    cost_usd    REAL DEFAULT 0,
    sources     TEXT,                        -- JSON list of research URLs
    message     TEXT
);
"""

COLUMNS = ("lens", "title", "one_liner", "category", "business_model", "channel", "customer_type", "geography",
           "startup_cost_eur", "automation_pct", "weeks_to_revenue", "profit_low_eur", "profit_high_eur",
           "self_score")


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def row_to_idea(r: sqlite3.Row) -> dict:
    d = json.loads(r["data"])
    d.update({k: r[k] for k in r.keys() if k != "data"})
    return d


def add_idea(conn, idea: dict, run_date: str, lens: str | None) -> int:
    values = {**{k: idea.get(k) for k in COLUMNS}, "lens": lens}
    cur = conn.execute(
        f"INSERT INTO ideas (created_at, run_date, data, {', '.join(COLUMNS)}) "
        f"VALUES (?, ?, ?, {', '.join('?' for _ in COLUMNS)})",
        (now(), run_date, json.dumps(idea, ensure_ascii=False), *(values[k] for k in COLUMNS)))
    conn.commit()
    return cur.lastrowid


def get(conn, idea_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM ideas WHERE id=?", (idea_id,)).fetchone()
    return row_to_idea(r) if r else None


def rated(conn) -> list[dict]:
    return [row_to_idea(r) for r in conn.execute("SELECT * FROM ideas WHERE rating IS NOT NULL ORDER BY rated_at")]


def all_titles(conn, limit: int = 300) -> list[str]:
    return [r[0] for r in conn.execute("SELECT title FROM ideas ORDER BY id DESC LIMIT ?", (limit,))]


def all_ideas(conn) -> list[dict]:
    return [row_to_idea(r) for r in conn.execute("SELECT * FROM ideas ORDER BY id")]


def held_since(conn, run_date_from: str) -> list[dict]:
    """Unsent candidates from recent runs: they compete again with today's."""
    return [row_to_idea(r) for r in conn.execute(
        "SELECT * FROM ideas WHERE status='held' AND run_date >= ? ORDER BY id", (run_date_from,))]


def mark_sent(conn, idea_id: int, predicted: float, explore: bool) -> None:
    conn.execute("UPDATE ideas SET status='sent', sent_at=?, predicted=?, explore=? WHERE id=?",
                 (now(), round(predicted, 2), int(explore), idea_id))
    conn.commit()


def rate(conn, idea_id: int, rating: int) -> bool:
    cur = conn.execute("UPDATE ideas SET rating=?, rated_at=? WHERE id=?", (rating, now(), idea_id))
    conn.commit()
    return cur.rowcount > 0


def add_note(conn, idea_id: int, note: str) -> None:
    conn.execute("UPDATE ideas SET note = CASE WHEN note IS NULL OR note = '' THEN ? ELSE note || ' / ' || ? END "
                 "WHERE id=?", (note, note, idea_id))
    conn.commit()


def remember_message(conn, message_id: int, idea_id: int) -> None:
    conn.execute("INSERT OR REPLACE INTO messages VALUES (?,?)", (message_id, idea_id))
    conn.commit()


def message_idea(conn, message_id: int) -> int | None:
    r = conn.execute("SELECT idea_id FROM messages WHERE message_id=?", (message_id,)).fetchone()
    return r[0] if r else None


def unrated_sent(conn, limit: int = 5) -> list[dict]:
    return [row_to_idea(r) for r in conn.execute(
        "SELECT * FROM ideas WHERE status='sent' AND rating IS NULL ORDER BY sent_at DESC, id DESC LIMIT ?",
        (limit,))]


def start_run(conn, run_date: str) -> int:
    cur = conn.execute("INSERT INTO runs (run_date, started_at, status) VALUES (?,?,'running')", (run_date, now()))
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id: int, status: str, **fields) -> None:
    fields = {k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in fields.items()}
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE runs SET status=?, finished_at=?{', ' + sets if sets else ''} WHERE id=?",
                 (status, now(), *fields.values(), run_id))
    conn.commit()


def ran_today(conn, run_date: str) -> bool:
    return conn.execute("SELECT 1 FROM runs WHERE run_date=? AND status='ok' AND sent > 0",
                        (run_date,)).fetchone() is not None


def last_runs(conn, limit: int = 5) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]
