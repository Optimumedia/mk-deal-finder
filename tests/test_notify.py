"""Telegram alerts: selection, dedupe, formatting, weekly summary, health. Never calls Telegram."""
import os
import tempfile
import tomllib
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scraper import notify
from scraper.db import DB

CFG = tomllib.loads((Path(__file__).parent.parent / "config.toml").read_text(encoding="utf-8"))
NOW = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)
RESEARCHERS = [SimpleNamespace(NAME=n, TITLE=t) for n, t in
               (("land", "Land for a house / villa"), ("weekend", "Weekend plots 700 m+"),
                ("flip", "Fix & flip — under market value"), ("auctions", "Bailiff auctions — official sales"))]
RATING = {"once": ("💎", "Once-in-a-lifetime", 0), "exceptional": ("🔥", "Exceptional deal", 1),
          "great": ("⭐", "Great deal", 2), "good": ("👍", "Good deal", 3)}


def deal(id_, score=80, tier="exceptional", **kw):
    e, label, rank = RATING[tier]
    base = {"id": id_, "url": f"https://m.reklama5.mk/AdDetails?ad={id_.split(':')[-1]}", "title": f"Plac {id_}",
            "image": f"https://reklama5.mk/photos/big/{id_}.jpg", "kind": "land", "city": "Скопје",
            "district": "Карпош", "price": 30000, "area": 600.0, "ppm2": 50, "score": score, "qualified": True,
            "status": "ready", "first_seen": "2026-11-25", "my_vote": None, "questions": [],
            "rating": {"tier": tier, "emoji": e, "label": label, "rank": rank},
            "reasons": ["40% below the district median", "building land (градежно)"],
            "metrics": {"Below market": "40%", "Land type": "Building"},
            "utilities": {"electricity": "yes", "water": "yes", "road": "yes"},
            "elevation": 650, "to_skopje": {"km": 12, "min": 22, "air_km": 8}, "lat": None, "lng": None,
            "map_query": "Карпош, Скопје, North Macedonia"}
    base.update(kw)
    return base


def hist(item, days_ago=10, **kw):
    h = {**notify._sig(item), "researcher": "land", "sent_at": (NOW - timedelta(days=days_ago)).isoformat()}
    h.update(kw)
    return h


class FormatTests(unittest.TestCase):
    def test_one_glance_message(self):
        x = deal("reklama5:1", 92, "once", reasons=["-4 from your votes on similar deals", "50% below the city median"])
        t = notify.format_deal(x, "Land for a house / villa", "land", base_url="https://example.org/dash/")
        self.assertIn("💎 <b>Once-in-a-lifetime · 92</b>", t)
        self.assertIn("<b>€30,000</b> · 600 m² · 50 €/m² · 40% below market · building land", t)
        self.assertIn("⛰ 650 m", t)
        self.assertIn("🚗 Skopje 22 min", t)
        self.assertIn("✅ power, water, road", t)
        self.assertIn("↳ 50% below the city median", t)          # vote adjustments are not "the reason"
        self.assertIn('href="https://example.org/dash/#deal=reklama5%3A1&amp;in=land"', t)
        self.assertIn(">Ad</a>", t)

    def test_escapes_everything_from_the_ad(self):
        x = deal("reklama5:2", title='<b>Plac</b> & "pogled"', url='https://x.mk/a?b=1&c="2"',
                 reasons=["<script>"], owner_note="<i>")
        t = notify.format_deal(x, "", "land", base_url="https://e.org/")
        self.assertNotIn("<script>", t)
        self.assertIn("&lt;b&gt;Plac&lt;/b&gt; &amp; &quot;pogled&quot;", t)
        self.assertIn('href="https://x.mk/a?b=1&amp;c=&quot;2&quot;"', t)
        self.assertIn("📝 Your note: &lt;i&gt;", t)

    def test_never_exceeds_telegram_limit_and_keeps_tags_whole(self):
        x = deal("reklama5:3", status="needs_info", questions=["Q" * 1500] * 3, reasons=["R" * 3000])
        t = notify.format_deal(x, "", "land", base_url="https://e.org/")
        self.assertLessEqual(len(t), notify.TELEGRAM_LIMIT)
        self.assertEqual(t.count("<a "), t.count("</a>"))
        self.assertEqual(t.count("<b>"), t.count("</b>"))

    def test_needs_info_lists_unconfirmed_utilities_and_questions(self):
        x = deal("reklama5:4", status="needs_info", utilities={"electricity": "yes", "water": "unknown", "road": "unknown"},
                 questions=["Is there water?"])
        t = notify.format_deal(x, "", "land", base_url="https://e.org/")
        self.assertIn("❓ <b>Needs info</b>", t)
        self.assertIn("❓ water, road not confirmed", t)
        self.assertIn("• Is there water?", t)

    def test_price_cut_line(self):
        t = notify.format_deal(deal("reklama5:5"), "", "land", cut={"before": 40000, "after": 30000, "pct": 25},
                               base_url="https://e.org/")
        self.assertIn("📉 <b>Price cut 25%</b>: €40,000 → €30,000", t)


class SamePropertyTests(unittest.TestCase):
    def test_repost_with_same_photo(self):
        a = notify._sig(deal("reklama5:10"))
        b = {**a, "listing_id": "reklama5:99", "title": "Something else entirely", "price": 26000}
        self.assertTrue(notify.same_property(a, b))

    def test_copy_on_other_site_same_size_and_price(self):
        a = notify._sig(deal("reklama5:10"))
        b = {**a, "listing_id": "novel:S1", "image": None, "title": "Се продава Земјиште", "price": 30500, "area": 602}
        self.assertTrue(notify.same_property(a, b))

    def test_different_plot_same_size_other_district(self):
        a = notify._sig(deal("reklama5:10"))
        b = {**a, "listing_id": "reklama5:11", "image": None, "district": "Аеродром"}
        self.assertFalse(notify.same_property(a, b))

    def test_placeholder_photo_is_not_evidence(self):
        a = {**notify._sig(deal("reklama5:10")), "image": "/Content/images/noImage2.jpg", "price": 30000}
        b = {**a, "listing_id": "reklama5:11", "title": "other words here", "price": 150000}
        self.assertFalse(notify.same_property(a, b))

    def test_bailiff_sales_match_on_case_and_size_only(self):
        a = {"listing_id": "kirsm:1", "kind": "apartment", "city": "Скопје", "area": 61, "price": 79857,
             "title": "Bailiff sale: Стан, 61 m² — Скопје", "case": "535/2018"}
        self.assertTrue(notify.same_property(a, {**a, "listing_id": "kirsm:2", "price": 63684}))     # next round
        self.assertFalse(notify.same_property(a, {**a, "listing_id": "kirsm:3", "case": "1/2026"}))  # other flat
        self.assertFalse(notify.same_property(a, {**a, "listing_id": "kirsm:4", "area": 98}))        # same case, other flat


class SelectTests(unittest.TestCase):
    def pick(self, results, history=(), **cfg):
        c = {"notify": {**CFG["notify"], **cfg}}
        return notify.select(results, list(history), c, NOW)

    def test_quiet_when_nothing_great(self):
        picks, held = self.pick({"land": [deal("reklama5:1", 65, "good")], "auctions": [deal("kirsm:1", 80, "exceptional")]})
        self.assertEqual((picks, held), ([], 0))          # auctions need 85, land needs 70

    def test_per_researcher_threshold(self):
        picks, _ = self.pick({"land": [deal("reklama5:1", 72, "great")], "auctions": [deal("kirsm:1", 86)]})
        self.assertEqual({p["item"]["id"] for p in picks}, {"reklama5:1", "kirsm:1"})

    def test_skips_reviewed_unqualified_and_old(self):
        picks, _ = self.pick({"land": [deal("reklama5:1", my_vote=1), deal("reklama5:2", qualified=False),
                                       deal("reklama5:3", 75, "great", first_seen="2026-10-01"),
                                       deal("reklama5:4", 85, "exceptional", first_seen="2026-10-01")]})
        self.assertEqual([p["item"]["id"] for p in picks], ["reklama5:4"])   # old but exceptional and never sent

    def test_one_message_per_listing_across_researchers(self):
        x = deal("reklama5:1", 80)
        picks, _ = self.pick({"land": [x], "weekend": [dict(x, score=90)]})
        self.assertEqual(len(picks), 1)
        self.assertEqual((picks[0]["researcher"], picks[0]["also"]), ("weekend", ["land"]))

    def test_never_repeats_a_property(self):
        x = deal("reklama5:1")
        repost = deal("reklama5:2", price=29500)                 # same photo, new ad id, -2%
        self.assertEqual(self.pick({"land": [x]}, [hist(x)])[0], [])
        self.assertEqual(self.pick({"land": [repost]}, [hist(x)])[0], [])

    def test_real_price_cut_is_alerted_again(self):
        x = deal("reklama5:1", price=30000)
        picks, _ = self.pick({"land": [x]}, [hist(x, price=36000)])
        self.assertEqual(picks[0]["why"], "cut")
        self.assertAlmostEqual(picks[0]["cut"]["pct"], 16.7, places=1)
        self.assertEqual(self.pick({"land": [x]}, [hist(x, price=32000)])[0], [])           # 6%: not a real cut
        self.assertEqual(self.pick({"land": [x]}, [hist(x, days_ago=2, price=36000)])[0], [])  # cool-down

    def test_same_property_twice_in_one_run(self):
        a, b = deal("reklama5:1", 90, "once"), deal("reklama5:2", 80)
        picks, _ = self.pick({"land": [a, b]})
        self.assertEqual([p["item"]["id"] for p in picks], ["reklama5:1"])

    def test_max_per_run_and_needs_info_cap(self):
        items = [deal(f"reklama5:{i}", 80 + i, image=None, title=f"t{i}", area=100.0 * i) for i in range(1, 6)]
        asks = [deal(f"reklama5:{i}", 80, status="needs_info", image=None, title=f"a{i}", area=50.0 * i + 7)
                for i in range(10, 13)]
        picks, held = self.pick({"land": items + asks})
        self.assertEqual(len(picks), 3)
        self.assertEqual(held, 3)                       # 5 deals + 1 allowed ask − 3 sent
        self.assertTrue(all(p["why"] == "new" for p in picks))
        picks, _ = self.pick({"land": asks})
        self.assertEqual(len(picks), 1)

    def test_weak_needs_info_lead_is_not_sent(self):
        self.assertEqual(self.pick({"land": [deal("reklama5:1", 72, "great", status="needs_info")]})[0], [])


class SendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "deals.db")
        self.env = mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1",
                                                "DASHBOARD_URL": "https://e.org/"})
        self.env.start()
        self.patches = [mock.patch("scraper.feedback.DB_PATH", Path(self.tmp.name) / "feedback.db"),
                        mock.patch("scraper.notify.time.sleep"),
                        mock.patch("scraper.notify.api", return_value={"message_id": 7})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.env.stop()
        self.db.conn.close()
        self.tmp.cleanup()

    def test_sends_once_then_stays_quiet(self):
        today = datetime.now(timezone.utc).date().isoformat()
        cfg = {"notify": {**CFG["notify"], "weekly_summary": False}}
        results = {"land": [deal("reklama5:1", first_seen=today)], "weekend": [deal("reklama5:1", 90, "once", first_seen=today)]}
        self.assertEqual(notify.send(results, self.db, cfg, RESEARCHERS), 1)
        self.assertEqual(notify.send(results, self.db, cfg, RESEARCHERS), 0)
        repost = {"land": [deal("reklama5:2", first_seen=today, price=29800)]}     # same photo, new id
        self.assertEqual(notify.send(repost, self.db, cfg, RESEARCHERS), 0)
        texts = [c.kwargs["text"] for c in notify.api.call_args_list if c.args[0] == "sendMessage"]
        self.assertEqual(len(texts), 1)
        self.assertIn("Also fits: Land for a house / villa", texts[0])
        self.assertTrue(self.db.was_notified("reklama5:1", "weekend"))
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM alert_log").fetchone()[0], 1)

    def test_not_configured_sends_nothing(self):
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": ""}):
            self.assertEqual(notify.send({"land": [deal("reklama5:1")]}, self.db, CFG, RESEARCHERS), 0)
        notify.api.assert_not_called()

    def test_weekly_summary_once_a_week_on_its_day(self):
        cfg = {"notify": {**CFG["notify"], "weekly_day": "sun"}}
        results = {"land": [deal("reklama5:1")]}
        saturday, sunday = datetime(2026, 11, 28, 9, tzinfo=timezone.utc), datetime(2026, 11, 29, 9, tzinfo=timezone.utc)
        self.assertFalse(notify.send_weekly(results, self.db, cfg, RESEARCHERS, saturday))
        self.assertTrue(notify.send_weekly(results, self.db, cfg, RESEARCHERS, sunday))
        self.assertFalse(notify.send_weekly(results, self.db, cfg, RESEARCHERS, sunday + timedelta(hours=6)))
        text = notify.api.call_args.kwargs["text"]
        self.assertIn("Your week", text)
        self.assertIn("Waiting for your rating", text)

    def test_source_and_interrupted_run_warnings(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=50)).isoformat()
        fresh = datetime.now(timezone.utc).isoformat()
        for i, (src, seen) in enumerate((("reklama5", fresh), ("novel", old))):
            self.db.conn.execute("INSERT INTO listings (id, source, source_id, url, first_seen, last_seen) "
                                 "VALUES (?,?,?,?,?,?)", (f"{src}:{i}", src, str(i), "u", seen, seen))
        self.db.conn.execute("INSERT INTO runs (started_at, status) VALUES (?, 'ok')", (old,))
        self.db.conn.execute("INSERT INTO runs (started_at, status) VALUES (?, 'interrupted')", (old,))
        self.db.conn.execute("INSERT INTO runs (started_at, status) VALUES (?, 'running')", (fresh,))
        problems = notify.source_problems(self.db, CFG)
        self.assertEqual(len(problems), 2)
        self.assertTrue(problems[0].startswith("Novel Estate: no listings read for 50 h"))
        self.assertIn("never finished", problems[1])
        self.assertTrue(notify.send_health(self.db, CFG))
        self.assertFalse(notify.send_health(self.db, CFG))          # once a day

    def test_due_reminder(self):
        pipe = [{"id": "reklama5:1", "stage": "interested", "next_step": "Call <seller>", "due": "2026-11-24",
                 "item": {"url": "https://x/1", "title": "Plac", "price": 30000}},
                {"id": "reklama5:2", "stage": "won", "due": "2026-11-20", "item": {}},
                {"id": "reklama5:3", "stage": "visit", "due": "2026-12-20", "item": {}}]
        text, n = notify.due_text(pipe, "2026-11-26")
        self.assertEqual(n, 1)
        self.assertIn("<b>Call &lt;seller&gt;</b> (2 days overdue)", text)
        self.assertEqual(notify.send_due(pipe, self.db, "2026-11-26"), 1)
        self.assertEqual(notify.send_due(pipe, self.db, "2026-11-26"), 0)     # once a day


class BotTests(unittest.TestCase):
    def test_top_is_one_message_of_unrated_deals(self):
        from scraper import bot  # noqa: F401  (import must not need Telegram or data files)
        results = {"land": [deal("reklama5:1", 92, "once"), deal("reklama5:2", my_vote=1), deal("reklama5:3", 70, "great")],
                   "auctions": [deal("kirsm:1", 85, kind="apartment")]}
        t = notify.top_text(results, CFG, {"land": "Land", "auctions": "Auctions"})
        self.assertEqual(t.count("Dashboard</a>"), 3)
        self.assertNotIn("reklama5%3A2", t)
        self.assertLess(t.index("reklama5%3A1"), t.index("kirsm%3A1"))
        self.assertIn("Nothing waiting", notify.top_text({"land": []}, CFG, {}))

    def test_status_text(self):
        from scraper import bot
        data = {"generated_at": "2026-11-25T15:02:32", "listings_tracked": 27000,
                "runs": [{"status": "ok", "pages": 206}, {"status": "interrupted"}],
                "researchers": [{"title": "Land for a house / villa", "qualified": 49}],
                "pipeline": [{"due": "2026-01-01", "stage": "interested"}]}
        t = bot.status_text(data)
        self.assertIn("Recent runs: 1 of 2 ok", t)
        self.assertIn("1 pipeline step due", t)


if __name__ == "__main__":
    unittest.main()
