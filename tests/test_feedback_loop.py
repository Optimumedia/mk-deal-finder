"""The owner feedback loop: replies with seller facts, rejection reasons, learning.

Run:  python -m unittest discover tests
"""
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from scraper import feedback, learn, needs_info, reasons
from scraper.bot import parse_answer

CFG = tomllib.loads((Path(__file__).parent.parent / "config.toml").read_text(encoding="utf-8"))


class ParseAnswerTests(unittest.TestCase):
    def test_english(self):
        self.assertEqual(parse_answer("water yes, power yes, road no, area 450, price 32000, building yes"),
                         {"water": "yes", "electricity": "yes", "road": "no", "area_m2": 450.0,
                          "price_eur": 32000.0, "land_type": "building"})

    def test_macedonian(self):
        f = parse_answer("вода има, струја нема, 1.200 m2, цена 45 илј")
        self.assertEqual((f["water"], f["electricity"], f["area_m2"], f["price_eur"]), ("yes", "no", 1200.0, 45000.0))

    def test_negative_first(self):
        self.assertEqual(parse_answer("Нема пат до плацот"), {"road": "no"})

    def test_plain_note_has_no_facts(self):
        self.assertEqual(parse_answer("seller is abroad until May"), {})


class ReasonTests(unittest.TestCase):
    def test_keyboard_fits_telegram(self):
        for r in reasons.ALL:
            kb = reasons.keyboard(r, "reklama5:58101319")
            for row in kb["inline_keyboard"]:
                for b in row:
                    self.assertLessEqual(len(b["callback_data"].encode()), 64)
        land = [b["text"] for row in reasons.keyboard("land", "x")["inline_keyboard"] for b in row]
        self.assertIn("No water", land)
        self.assertNotIn("Rent too high for Airbnb", land)

    def rj(self, reason, **kw):
        base = {"listing_id": "old", "researcher": "flip", "reason": reason, "kind": "apartment",
                "city": "Скопје", "district": "Бутел", "area": 30.0}
        return {**base, **kw}

    def test_basement_rejections_penalise_basements(self):
        item = {"id": "new", "area": 60, "district": "Карпош", "city": "Скопје", "kind": "apartment"}
        basement = {"title": "Stan vo suteren 60m2", "description": ""}
        normal = {"title": "Stan na 3 kat 60m2", "description": ""}
        rjs = [self.rj("bsm", listing_id=f"x{i}") for i in range(3)]
        pen, why = reasons.penalties(item, basement, rjs, "flip")
        self.assertLess(pen, -5)
        self.assertIn("basement", why[0])
        self.assertEqual(reasons.penalties(item, normal, rjs, "flip"), (0.0, []))

    def test_too_small_learns_a_size(self):
        rjs = [self.rj("sml", listing_id=f"x{i}", area=a) for i, a in enumerate((28, 32, 35))]
        small = {"id": "n", "area": 30, "kind": "apartment", "city": "Скопје", "district": "Карпош"}
        big = dict(small, area=70)
        self.assertLess(reasons.penalties(small, {}, rjs, "flip")[0], 0)
        self.assertEqual(reasons.penalties(big, {}, rjs, "flip")[0], 0)

    def test_other_researchers_unaffected(self):
        rjs = [self.rj("bsm")]
        self.assertEqual(reasons.penalties({"id": "n"}, {"title": "suteren"}, rjs, "airbnb"), (0.0, []))


class StoreAndLearnTests(unittest.TestCase):
    def test_round_trip_and_report(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "feedback.db"
            conn = feedback.connect(path)
            listing = {"kind": "apartment", "city": "Скопје", "district": "Бутел", "area_m2": 28, "price_eur": 40000}
            for i, area in enumerate((25, 28, 33)):
                feedback.reject(conn, f"reklama5:{i}", "flip", "sml", dict(listing, area_m2=area))
            feedback.set_override(conn, "reklama5:9", "water", "yes")
            feedback.confirm(conn, "reklama5:9")
            conn.close()
            with mock.patch.object(feedback, "DB_PATH", path):
                text = learn.report()
            self.assertIn("3× Too small", text)
            self.assertIn("min_area_m2 = 35", text)        # median 28 → rounded up + 5
            facts, confirmed = feedback.load_overrides(path)
            self.assertEqual(facts["reklama5:9"]["water"], "yes")
            listings = [{"id": "reklama5:9", "utilities": {"water": None}}]
            feedback.apply_overrides(listings, facts, confirmed)
            self.assertTrue(listings[0]["utilities"]["water"])
            self.assertTrue(listings[0]["confirmed"])


class StatusTests(unittest.TestCase):
    def test_statuses(self):
        def item(**kw):
            return {"id": "a", "score": 70, "qualified": False, "kind": "land", "area": 600,
                    "utilities": {"electricity": "yes", "water": "unknown", "road": "yes"},
                    "metrics": {"Land type": "Building"}, **kw}
        out = needs_info.annotate({"land": [item()]}, {"a": {"detail_at": "2026-10-06"}}, CFG)["land"][0]
        self.assertEqual(out["status"], "needs_info")
        self.assertEqual(len(out["questions"]), 1)
        self.assertIn("water", out["questions"][0])
        pending = needs_info.annotate({"land": [item()]}, {"a": {}}, CFG)["land"][0]
        self.assertEqual(pending["status"], "pending")       # details not read yet: nothing to ask
        conf = needs_info.annotate({"land": [item()]}, {"a": {"confirmed": True}}, CFG)["land"][0]
        self.assertEqual(conf["status"], "confirmed")
        self.assertEqual(needs_info.public({"land": [conf]})["land"][0]["status"], "ready")


if __name__ == "__main__":
    unittest.main()
