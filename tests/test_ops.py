"""Reliability: run lock, catch-up after a missed slot, per-run health and broken-source flags.
Every test works in a temporary folder; nothing touches data/."""
import json
import logging
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from scraper import ops
from scraper.db import DB

CET = timezone(timedelta(hours=1))


def utc(local: datetime) -> str:
    return local.astimezone(timezone.utc).isoformat()


class MissedSlotTests(unittest.TestCase):
    def test_on_time_run_means_nothing_to_catch_up(self):
        now = datetime(2026, 11, 25, 14, 0, tzinfo=CET)
        runs = [(utc(datetime(2026, 11, 25, 12, 15, 8, tzinfo=CET)), "ok")]
        self.assertIsNone(ops.missed_slot(now, runs))

    def test_pc_started_late_after_both_slots(self):
        # Real case, 21 Nov 2026: PC on at 21:01, both slots missed.
        now = datetime(2026, 11, 21, 21, 6, tzinfo=CET)
        runs = [(utc(datetime(2026, 11, 20, 16, 45, tzinfo=CET)), "ok")]
        self.assertEqual(ops.missed_slot(now, runs), datetime(2026, 11, 21, 16, 45, tzinfo=CET))

    def test_before_first_slot_of_the_day_waits_for_it(self):
        now = datetime(2026, 11, 26, 12, 5, tzinfo=CET)      # logon at 12:00, run due at 12:15
        runs = [(utc(datetime(2026, 11, 25, 16, 45, tzinfo=CET)), "ok")]
        self.assertIsNone(ops.missed_slot(now, runs))

    def test_interrupted_run_is_retried_once(self):
        now = datetime(2026, 11, 18, 13, 30, tzinfo=CET)
        died = [(utc(datetime(2026, 11, 18, 12, 15, tzinfo=CET)), "interrupted")]
        self.assertIsNotNone(ops.missed_slot(now, died))
        twice = died + [(utc(datetime(2026, 11, 18, 12, 30, tzinfo=CET)), "interrupted")]
        self.assertIsNone(ops.missed_slot(now, twice))      # two tries per slot is enough

    def test_blocked_or_failed_runs_count_as_done(self):
        now = datetime(2026, 11, 18, 13, 30, tzinfo=CET)
        for st in ("blocked", "error"):
            runs = [(utc(datetime(2026, 11, 18, 12, 16, tzinfo=CET)), st)]
            self.assertIsNone(ops.missed_slot(now, runs), st)

    def test_grace_period_after_slot(self):
        now = datetime(2026, 11, 26, 12, 25, tzinfo=CET)     # 10 min after 12:15: still give the task time
        runs = [(utc(datetime(2026, 11, 25, 16, 45, tzinfo=CET)), "ok")]
        self.assertIsNone(ops.missed_slot(now, runs))
        self.assertIsNotNone(ops.missed_slot(now + timedelta(minutes=15), runs))


class BrokenSourceTests(unittest.TestCase):
    history = [{"reklama5": {"seen": n, "pages": 300}, "novel": {"seen": 1580, "pages": 140},
                "nedviznosti": {"seen": 96, "pages": 95}} for n in (8273, 8198, 8211, 8240)]

    def test_normal_run_is_fine(self):
        cur = {"reklama5": {"seen": 8100, "pages": 320}, "novel": {"seen": 1500, "pages": 138},
               "nedviznosti": {"seen": 2, "pages": 3}}
        self.assertEqual(ops.broken_sources(cur, self.history), [])

    def test_source_returning_almost_nothing_is_flagged(self):
        cur = {"reklama5": {"seen": 8100, "pages": 320}, "novel": {"seen": 3, "pages": 5}}
        b = ops.broken_sources(cur, self.history)
        self.assertEqual([x["source"] for x in b], ["novel"])
        self.assertEqual(b[0]["usual"], 1580)
        self.assertIn("novelestate.com: 3 ads instead of the usual ~1,580", ops.plain_summary(b))

    def test_sitemap_source_only_flagged_when_nothing_loads(self):
        self.assertEqual(ops.broken_sources({"nedviznosti": {"seen": 0, "pages": 1}}, self.history), [])
        self.assertEqual(len(ops.broken_sources({"nedviznosti": {"seen": 0, "pages": 0}}, self.history)), 1)

    def test_needs_enough_history(self):
        self.assertEqual(ops.broken_sources({"novel": {"seen": 1, "pages": 2}}, self.history[:2]), [])

    def test_broken_runs_do_not_lower_the_average(self):
        hist = self.history[:3] + [{"novel": {"seen": 2, "pages": 2, "broken": True}}] * 5
        self.assertEqual(len(ops.broken_sources({"novel": {"seen": 2, "pages": 2}}, hist)), 1)

    def test_detail_pages_mostly_failing(self):
        self.assertTrue(ops.details_look_broken(3, 101, 900))
        self.assertFalse(ops.details_look_broken(100, 101, 900))
        self.assertFalse(ops.details_look_broken(0, 4, 900))        # too few to judge


class RunLockTests(unittest.TestCase):
    def test_second_run_is_refused_until_first_ends(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = ops.RunLock(Path(tmp) / "run.lock"), ops.RunLock(Path(tmp) / "run.lock")
            self.assertTrue(a.acquire())
            self.assertFalse(b.acquire())
            self.assertTrue(b.held_elsewhere())
            a.release()
            self.assertTrue(b.acquire())
            b.release()

    def test_main_exits_3_when_another_run_holds_the_lock(self):
        from scraper import run
        with tempfile.TemporaryDirectory() as tmp:
            dbp = Path(tmp) / "deals.db"
            with ops.RunLock(Path(tmp) / "run.lock") as held:
                held.acquire()
                with mock.patch.object(run, "DB") as db:
                    self.assertEqual(run.main(["--db", str(dbp)]), 3)
                    db.assert_not_called()                              # never touched the database

    def test_is_due_reads_the_database(self):
        from scraper import run
        with tempfile.TemporaryDirectory() as tmp:
            dbp = Path(tmp) / "deals.db"
            self.assertEqual(run.is_due(dbp, {})[0], True)              # no database yet
            db = DB(dbp)
            rid = db.start_run()
            db.finish_run(rid, status="ok", mode="full")
            db.close()
            with mock.patch("scraper.ops.missed_slot", return_value=None):
                self.assertFalse(run.is_due(dbp, {})[0])
            with ops.RunLock(Path(tmp) / "run.lock") as held:
                held.acquire()
                self.assertEqual(run.is_due(dbp, {}), (False, "a run is in progress"))


class RunHealthStorageTests(unittest.TestCase):
    def test_health_is_stored_and_history_is_per_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DB(Path(tmp) / "deals.db")
            for mode, seen in (("full", 8200), ("quick", 2600), ("full", 8100)):
                rid = db.start_run()
                db.finish_run(rid, status="ok", mode=mode, duration_s=1900, warnings=2, broken=None,
                              health={"sources": {"reklama5": {"seen": seen, "pages": 300}}})
            rid = db.start_run()
            db.finish_run(rid, status="blocked", mode="full", health={"sources": {"reklama5": {"seen": 5}}})
            self.assertEqual([h["reklama5"]["seen"] for h in db.source_history("full")], [8100, 8200])
            self.assertEqual([h["reklama5"]["seen"] for h in db.source_history("quick")], [2600])
            last = db.recent_runs(1)[0]
            self.assertEqual(last["health"]["sources"]["reklama5"]["seen"], 5)   # decoded for the dashboard
            db.close()

    def test_old_database_gets_new_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "deals.db"
            con = sqlite3.connect(p)
            con.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT, "
                        "status TEXT, pages INTEGER, details INTEGER, new_listings INTEGER, price_changes INTEGER, "
                        "message TEXT)")
            con.commit()
            con.close()
            db = DB(p)
            cols = {r[1] for r in db.conn.execute("PRAGMA table_info(runs)")}
            self.assertTrue({"mode", "duration_s", "warnings", "broken", "health"} <= cols)
            db.close()

    def test_seen_since_counts_per_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DB(Path(tmp) / "deals.db")
            card = dict(cat=1, kind="land", deal="sale", title="t", price_eur=1000.0, area_m2=500.0, rooms=None,
                        city="Скопје", district=None, image=None, promoted=False, posted=None, url="u")
            db.upsert_card({**card, "source": "reklama5", "source_id": "1"}, "2026-11-25T10:00:00+00:00")
            db.upsert_card({**card, "source": "reklama5", "source_id": "2"}, "2026-11-25T12:00:00+00:00")
            db.upsert_card({**card, "source": "novel", "source_id": "3"}, "2026-11-25T12:00:00+00:00")
            self.assertEqual(db.seen_since("2026-11-25T11:00:00+00:00"), {"reklama5": 1, "novel": 1})
            db.close()


class MainHealthTests(unittest.TestCase):
    """main() end to end with the network and publishing replaced by fakes."""

    def run_once(self, dbp, novel_ads):
        from scraper import run
        card = dict(cat=1, kind="land", deal="sale", title="t", price_eur=1000.0, area_m2=500.0, rooms=None,
                    city="Скопје", district=None, image=None, promoted=False, posted=None, url="u")

        def lists(db, http, cfg, full, mp, stats):
            for i in range(30):
                db.upsert_card({**card, "source": "reklama5", "source_id": str(i)}, run.now())
            stats["pages"] += 10

        def novel(db, http, cfg, full, stats):
            for i in range(novel_ads):
                db.upsert_card({**card, "source": "novel", "source_id": str(i)}, run.now())
            stats["pages"] += 5

        def pages(*a):
            a[-2 if isinstance(a[-1], bool) else -1]["pages"] += 1

        with mock.patch.object(run, "scrape_lists", lists), mock.patch.object(run, "scrape_novel", novel), \
                mock.patch.object(run, "scrape_kirsm", pages), mock.patch.object(run, "scrape_nedviznosti", pages), \
                mock.patch.object(run, "fetch_details", return_value=4), mock.patch.object(run.geo, "enrich"), \
                mock.patch.object(run, "analyse", return_value=({}, None, {})), \
                mock.patch.object(run, "write_outputs", return_value=([], [])), \
                mock.patch.object(run, "renormalise"), mock.patch.object(run, "notify") as nt, \
                mock.patch("time.sleep"):
            code = run.main(["--db", str(dbp), "--quick"])
        return code, nt

    def test_source_that_suddenly_returns_nothing_is_flagged_and_alerted(self):
        import time
        with tempfile.TemporaryDirectory() as tmp:
            dbp = Path(tmp) / "deals.db"
            for _ in range(4):                      # 1st run is a full backfill, then 3 quick ones = history
                code, nt = self.run_once(dbp, novel_ads=20)
                self.assertEqual(code, 0)
                nt.send_status.assert_not_called()
                time.sleep(1.1)                     # next run's cards get a later last_seen
            code, nt = self.run_once(dbp, novel_ads=0)
            self.assertEqual(code, 0)
            nt.send_status.assert_called_once()
            self.assertIn("novelestate.com", nt.send_status.call_args[0][0])
            db = DB(dbp)
            last = db.recent_runs(1)[0]
            db.close()
            self.assertEqual(last["status"], "ok")
            self.assertTrue(last["broken"].startswith("novelestate.com: 0 ads"))
            self.assertIn("Looks broken", last["message"])
            src = last["health"]["sources"]
            self.assertEqual(src["reklama5"]["seen"], 30)
            self.assertEqual(src["reklama5"]["pages"], 10)
            self.assertEqual(src["kirsm"]["pages"], 1)
            self.assertIsNotNone(last["duration_s"])
            self.assertEqual(last["health"]["details"]["read"], 4)


class CounterTests(unittest.TestCase):
    def test_counts_warnings_per_step_and_reads_detail_queue(self):
        c = ops.Counter()
        lg = logging.getLogger("scraper.test_ops")
        lg.addHandler(c)
        lg.propagate = False
        lg.setLevel(logging.INFO)                  # the run sets INFO via basicConfig
        try:
            c.phase = "novel"
            lg.warning("novel page failed")
            c.phase = "details"
            lg.info("detail queue: %d listings worth reading, budget %d", 101, 900)
            lg.error("boom")
            lg.warning("again")
        finally:
            lg.removeHandler(c)
        self.assertEqual(c.warnings, {"novel": 1, "details": 2})
        self.assertEqual(c.detail_queue, 101)
        self.assertEqual(c.total(), 3)


class SlowRequestTests(unittest.TestCase):
    def test_slow_and_failed_answers_are_counted(self):
        import requests
        from scraper.http import PoliteSession
        s = PoliteSession(delay=0, jitter=0)
        s._allowed = lambda url: True
        answers = iter([requests.ConnectionError("reset"), mock.Mock(status_code=200, text="ok", encoding="utf-8")])

        def get(url, timeout):
            a = next(answers)
            if isinstance(a, Exception):
                raise a
            return a
        s.s.get = get
        with mock.patch("scraper.http.time.sleep"), mock.patch("scraper.http.SLOW_SECONDS", -1):
            self.assertEqual(s.get("https://m.reklama5.mk/x"), "ok")
        st = s.stats()
        self.assertEqual((st["requests"], st["failed"], st["slow"]), (2, 1, 2))


if __name__ == "__main__":
    unittest.main()
