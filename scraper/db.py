"""SQLite storage: listings, their price history, runs and sent alerts."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id              TEXT PRIMARY KEY,           -- "<source>:<source_id>"
    source          TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    url             TEXT NOT NULL,
    cat             INTEGER,
    kind            TEXT,                       -- apartment | house | land | weekend_house
    deal            TEXT,                       -- sale | rent | short_term | wanted
    title           TEXT,
    description     TEXT,
    price_eur       REAL,                       -- total price (or monthly rent)
    price_note      TEXT,                       -- e.g. "per_m2_inferred", "placeholder"
    site_old_price  REAL,                       -- strike-through price shown by the site
    first_price     REAL,
    area_m2         REAL,
    rooms           REAL,
    city            TEXT,
    district        TEXT,
    address         TEXT,
    lat             REAL,
    lng             REAL,
    land_type       TEXT,
    utilities       TEXT,                       -- JSON {"electricity":..,"water":..,"road":..}
    furnished       INTEGER,
    renovation      INTEGER,
    agency          INTEGER,
    abroad          INTEGER DEFAULT 0,
    fields          TEXT,                       -- JSON of the site's structured fields
    image           TEXT,
    promoted        INTEGER DEFAULT 0,
    posted          TEXT,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    detail_at       TEXT,
    auction_date    TEXT,                       -- bailiff sales: date of the sale
    auction_round   INTEGER,                    -- 1st / 2nd / 3rd sale (later = lower start)
    extra           TEXT                        -- JSON: source-specific details
);
CREATE INDEX IF NOT EXISTS ix_listings_kind ON listings(kind, deal, city);
CREATE INDEX IF NOT EXISTS ix_listings_seen ON listings(last_seen);

CREATE TABLE IF NOT EXISTS price_history (
    listing_id  TEXT NOT NULL,
    seen_at     TEXT NOT NULL,
    price_eur   REAL,
    PRIMARY KEY (listing_id, seen_at)
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT,
    finished_at TEXT,
    status      TEXT,
    pages       INTEGER,
    details     INTEGER,
    new_listings INTEGER,
    price_changes INTEGER,
    message     TEXT
);

CREATE TABLE IF NOT EXISTS notified (
    listing_id  TEXT NOT NULL,
    researcher  TEXT NOT NULL,
    notified_at TEXT NOT NULL,
    PRIMARY KEY (listing_id, researcher)
);
"""

JSON_COLS = ("utilities", "fields", "extra")
# Columns added after the first release — created on older databases at startup.
MIGRATIONS = {"auction_date": "TEXT", "auction_round": "INTEGER", "extra": "TEXT"}


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def days_ago(n: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).replace(microsecond=0).isoformat()


class DB:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(listings)")}
        for col, typ in MIGRATIONS.items():
            if col not in have:
                self.conn.execute(f"ALTER TABLE listings ADD COLUMN {col} {typ}")

    def close(self):
        self.conn.commit()
        self.conn.execute("VACUUM")
        self.conn.close()

    # ------------------------------------------------------------ listings
    def known_ids(self, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        q = f"SELECT id FROM listings WHERE id IN ({','.join('?' * len(ids))})"
        return {r[0] for r in self.conn.execute(q, ids)}

    def get(self, listing_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
        return _row(r) if r else None

    def upsert_card(self, card: dict, ts: str) -> tuple[bool, bool]:
        """Insert or refresh a listing from a search-result card.

        Returns (is_new, price_changed).
        """
        lid = f"{card['source']}:{card['source_id']}"
        old = self.conn.execute("SELECT price_eur, area_m2, detail_at FROM listings WHERE id=?", (lid,)).fetchone()
        if old is None:
            self.conn.execute(
                """INSERT INTO listings (id, source, source_id, url, cat, kind, deal, title, price_eur, price_note,
                       site_old_price, first_price, area_m2, rooms, city, district, image, promoted, posted,
                       abroad, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (lid, card["source"], card["source_id"], card["url"], card["cat"], card["kind"], card["deal"],
                 card["title"], card["price_eur"], card.get("price_note"), card.get("old_price_eur"),
                 card.get("old_price_eur") or card["price_eur"], card["area_m2"], card["rooms"], card["city"],
                 card["district"], card["image"], int(card["promoted"]), card["posted"], int(card.get("abroad", 0)),
                 ts, ts),
            )
            self._history(lid, ts, card["price_eur"])
            return True, False

        changed = card["price_eur"] is not None and old["price_eur"] is not None and \
            abs(card["price_eur"] - old["price_eur"]) > 0.5
        # Once the detail page has been read, its area/price beat the card's.
        detailed = old["detail_at"] is not None
        self.conn.execute(
            """UPDATE listings SET last_seen=?, title=COALESCE(?, title), image=COALESCE(?, image),
                   promoted=?, site_old_price=COALESCE(?, site_old_price),
                   price_eur=CASE WHEN ? THEN ? ELSE price_eur END,
                   area_m2=CASE WHEN ? THEN area_m2 ELSE ? END,
                   price_note=CASE WHEN ? THEN price_note ELSE ? END,
                   deal=CASE WHEN ? THEN deal ELSE ? END,
                   posted=COALESCE(?, posted)
               WHERE id=?""",
            # Until a detail page is read, re-apply today's card parsing so parser
            # fixes reach listings stored earlier (plot vs house size, placeholders).
            (ts, card["title"] if not detailed else None, card["image"], int(card["promoted"]),
             card.get("old_price_eur"), changed or old["price_eur"] is None, card["price_eur"],
             detailed, card["area_m2"], detailed, card.get("price_note"), detailed, card["deal"],
             card.get("posted"), lid),
        )
        if changed:
            self._history(lid, ts, card["price_eur"])
        return False, changed

    def apply_detail(self, lid: str, data: dict, ts: str):
        cols = {k: (json.dumps(v, ensure_ascii=False) if k in JSON_COLS else v) for k, v in data.items()}
        cols["detail_at"] = ts
        sets = ", ".join(f"{k}=?" for k in cols)
        self.conn.execute(f"UPDATE listings SET {sets} WHERE id=?", (*cols.values(), lid))

    def _history(self, lid: str, ts: str, price: float | None):
        self.conn.execute("INSERT OR REPLACE INTO price_history VALUES (?,?,?)", (lid, ts, price))

    def pending_details(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM listings WHERE detail_at IS NULL AND last_seen >= ? AND deal != 'wanted' AND abroad = 0",
            (days_ago(3),),
        )
        return [_row(r) for r in rows]

    def active(self, stale_days: float) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM listings WHERE last_seen >= ?", (days_ago(stale_days),))
        return [_row(r) for r in rows]

    def market_rows(self, window_days: float) -> list[dict]:
        rows = self.conn.execute(
            """SELECT id, source, source_id, kind, deal, city, district, price_eur, price_note, area_m2, title,
                      image, detail_at FROM listings
               WHERE last_seen >= ? AND price_eur IS NOT NULL AND area_m2 IS NOT NULL AND abroad = 0
                 AND deal != 'auction'
                 AND COALESCE(price_note, '') != 'placeholder'""",
            (days_ago(window_days),),
        )
        return [dict(r) for r in rows]

    def price_history(self, lid: str) -> list[tuple[str, float]]:
        return [tuple(r) for r in self.conn.execute(
            "SELECT seen_at, price_eur FROM price_history WHERE listing_id=? ORDER BY seen_at", (lid,))]

    def prune(self, days: float):
        cutoff = days_ago(days)
        self.conn.execute("DELETE FROM price_history WHERE listing_id IN (SELECT id FROM listings WHERE last_seen < ?)", (cutoff,))
        self.conn.execute("DELETE FROM notified WHERE listing_id IN (SELECT id FROM listings WHERE last_seen < ?)", (cutoff,))
        self.conn.execute("DELETE FROM listings WHERE last_seen < ?", (cutoff,))

    # ------------------------------------------------------------ runs / alerts
    def start_run(self) -> int:
        self.conn.execute("UPDATE runs SET status = 'interrupted' WHERE status = 'running'")
        cur = self.conn.execute("INSERT INTO runs (started_at, status) VALUES (?, 'running')", (now(),))
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, **kw):
        kw["finished_at"] = now()
        sets = ", ".join(f"{k}=?" for k in kw)
        self.conn.execute(f"UPDATE runs SET {sets} WHERE id=?", (*kw.values(), run_id))
        self.conn.commit()

    def recent_runs(self, n: int = 10) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (n,))]

    def was_notified(self, lid: str, researcher: str) -> bool:
        return self.conn.execute("SELECT 1 FROM notified WHERE listing_id=? AND researcher=?",
                                 (lid, researcher)).fetchone() is not None

    def mark_notified(self, lid: str, researcher: str):
        self.conn.execute("INSERT OR IGNORE INTO notified VALUES (?,?,?)", (lid, researcher, now()))

    def has_completed_run(self) -> bool:
        return self.conn.execute("SELECT 1 FROM runs WHERE status = 'ok' AND pages > 0").fetchone() is not None

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    for k in JSON_COLS:
        if d.get(k):
            d[k] = json.loads(d[k])
    return d
