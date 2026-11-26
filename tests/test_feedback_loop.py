"""The owner feedback loop: replies with seller facts, rejection reasons, learning.

Run:  python -m unittest discover tests
"""
import json
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


class LearningFromExamplesTests(unittest.TestCase):
    """Small-data learning: re-posts, near-copies, likes, altitude, reason-specific questions."""

    def plot(self, i, **kw):
        base = {"id": i, "score": 70, "qualified": True, "kind": "land", "city": "Скопје", "district": "Зелениково",
                "area": 800.0, "price": 16000.0, "ppm2": 20.0, "elevation": 300.0, "title": f"Plac {i}",
                "utilities": {"electricity": "yes", "water": "yes", "road": "yes"}, "reasons": [], "questions": []}
        return {**base, **kw}

    def rj(self, i, reason="oth", note=None, **kw):
        return {"listing_id": i, "researcher": "land", "reason": reason, "note": note, "kind": "land",
                "city": "Скопје", "district": "Зелениково", "area": 800.0, "price": 16000.0, "title": f"Plac {i}", **kw}

    def test_effective_reason_reads_the_note(self):
        self.assertEqual(reasons.effective_reason({"reason": "oth", "note": "Looks far from the road"}), "nor")
        self.assertEqual(reasons.effective_reason({"reason": "oth", "note": "when I open the link is missing"}), "sold")
        self.assertEqual(reasons.effective_reason({"reason": "oth", "note": "dropped from the pipeline"}), "oth")
        self.assertEqual(reasons.effective_reason({"reason": "bsm", "note": "road"}), "bsm")

    def test_repost_of_a_rejected_ad_ranks_much_lower(self):
        text = "Se prodava plac za vikendica vo vikend naselba oresani asfaltiren pat do placot voda struja"
        ex = reasons.example("r:1", "land", -1, {"title": "Plac oresani", "description": text, "city": "Скопје",
                                                 "district": "Зелениково", "area_m2": 770, "price_eur": 15000},
                             reason="nor")
        item = self.plot("r:2", title="Plac vo Oresani", district="Сопиште", area=2000, price=60000)
        delta, why = reasons.learned(item, {"description": text}, [ex], "land")
        self.assertEqual(delta, -reasons.REPOST)
        self.assertIn("re-post", why[0])
        self.assertIn("no road access", why[0])
        self.assertEqual(reasons.learned(dict(item, id="r:1"), {"description": text}, [ex], "land"), (0.0, []))

    def test_near_copy_same_district_size_and_price(self):
        ex = reasons.example("r:1", "land", -1, self.rj("r:1"), reason="oth")
        near = self.plot("n", area=850, price=16500, ppm2=19.4)
        delta, why = reasons.learned(near, {}, [ex], "land")
        self.assertEqual(delta, -reasons.NEAR_DUP)
        self.assertIn("same area, size and price", why[0])
        other = self.plot("o", area=2000, price=60000, ppm2=30)
        self.assertEqual(reasons.learned(other, {}, [ex], "land")[0], 0)
        # Rejection rows from older versions saved plot prices per m² ("18"): still compared correctly.
        old = reasons.example("r:3", "land", -1, self.rj("r:3", price=20.0), reason="oth")
        self.assertEqual((old.price, old.ppm2), (16000.0, 20.0))

    def test_similar_to_a_liked_deal_ranks_higher_capped(self):
        liked = reasons.example("l:1", "land", 1, {"title": "Kozjak gradezen plac", "city": "Скопје",
                                                   "district": "Кисела Вода", "area": 470, "price": 8000,
                                                   "elevation": 280})
        item = self.plot("x", district="Кисела Вода", area=500, price=9000, elevation=320)
        delta, why = reasons.learned(item, {}, [liked], "land")
        self.assertEqual(delta, reasons.LIKE_MAX)
        self.assertIn("Kozjak", why[0])
        self.assertIn("similar size", why[0])
        weak = self.plot("y", district="Карпош", area=500, price=60000, elevation=1200)
        self.assertEqual(reasons.learned(weak, {}, [liked], "land")[0], 0)   # one trait in common is not enough
        # A vote on a Land plot counts for the Weekend tab too, not for flats.
        self.assertGreater(reasons.learned(item, {}, [liked], "weekend")[0], 0)
        self.assertEqual(reasons.learned(item, {}, [liked], "flip")[0], 0)

    def test_altitude_needs_examples_on_both_sides_and_only_brakes(self):
        mk = lambda i, v, e: reasons.example(i, "land", v, {"title": i, "city": "Охрид", "elevation": e})
        exs = [mk("u1", 1, 726), mk("u2", 1, 1034), mk("d1", -1, 230), mk("d2", -1, 290)]
        low = self.plot("low", city="Битола", district=None, elevation=240)
        high = self.plot("high", city="Битола", district=None, elevation=1500)
        d_low, why = reasons.learned(low, {}, exs, "land")
        self.assertLess(d_low, -2)
        self.assertIn("240 m", why[0])
        self.assertEqual(reasons.learned(high, {}, exs, "land"), (0.0, []))      # no lift for every high plot
        self.assertEqual(reasons.learned(low, {}, exs[:3], "land"), (0.0, []))   # one rejection: no pattern yet
        sold = exs[:2] + [mk("d1", -1, 230), reasons.example("d3", "land", -1, {"elevation": 250}, reason="sold")]
        self.assertEqual(reasons.learned(low, {}, sold, "land"), (0.0, []))      # a gone ad says nothing

    def test_total_learned_effect_is_capped(self):
        exs = [reasons.example("r:1", "land", -1, self.rj("r:1", title="Plac za vikendica Oresani"), reason="nor")]
        exs += [reasons.example(f"u{i}", "land", 1, {"city": "Охрид", "elevation": 1200}) for i in range(5)]
        exs += [reasons.example(f"d{i}", "land", -1, {"city": "Охрид", "elevation": 100}) for i in range(5)]
        delta, _ = reasons.learned(self.plot("n", elevation=100, title="Plac za vikendica Oresani"), {}, exs, "land")
        self.assertEqual(delta, reasons.LEARN_RANGE[0])

    def test_no_road_rejections_penalise_vague_ads_and_add_the_question(self):
        rjs = [self.rj("a", "nor", district="Ѓорче Петров"), self.rj("b", "nor")]
        vague = {"title": "Plac", "description": "pristap so zemjen pat, struja i voda vo blizina"}
        ask = []
        pen, why = reasons.penalties(self.plot("n", district="Карпош"), vague, rjs, "land", ask=ask)
        self.assertLess(pen, 0)
        self.assertIn("vague", why[0])
        self.assertTrue(ask and "road" in ask[0])
        ask = []
        pen, why = reasons.penalties(self.plot("m"), {"title": "Plac na asfalten pat"}, rjs, "land", ask=ask)
        self.assertEqual(pen, 0)                         # same area, clear ad: ask, don't punish
        self.assertEqual(len(ask), 1)
        self.assertIn("question added", why[0])
        self.assertEqual(reasons.penalties(self.plot("k", district="Карпош"), {"title": "Plac"}, rjs, "land"),
                         (0.0, []))

    def test_too_low_location_note_spares_mountain_plots(self):
        rjs = [self.rj("a", "loc", note="the location gets polluted in winter, too low", district="Карпош")]
        mountain = self.plot("m", district="Карпош", elevation=1600)
        valley = self.plot("v", district="Карпош", elevation=320)
        self.assertEqual(reasons.penalties(mountain, {}, rjs, "land")[0], 0)
        self.assertLess(reasons.penalties(valley, {}, rjs, "land")[0], 0)

    def test_apply_hides_rejected_plots_on_both_tabs_and_keeps_public_clean(self):
        results = {"land": [self.plot("a"), self.plot("b", area=820, price=16400)],
                   "weekend": [self.plot("a", elevation=900), self.plot("c", district="Сопиште", area=3000)]}
        votes = [{"listing_id": "a", "researcher": "land", "vote": -1, "kind": "land", "city": "Скопје",
                  "district": "Зелениково", "title": "Plac a"}]
        rjs = [self.rj("a", "nor")]
        out = feedback.apply(results, votes, rejections=rjs, listings={}, liked=[])
        self.assertEqual([x["id"] for x in out["weekend"]], ["c"])          # gone from Weekend too
        b = out["land"][0]
        self.assertLess(b["score"], 70)
        self.assertTrue(any("same area, size and price" in r for r in b["reasons"]))
        self.assertTrue(any("road" in q for q in b["questions"]))
        pub = feedback.apply(results, votes, personalize=False)
        self.assertEqual(pub["land"][0], results["land"][1])                # nothing personal leaks out

    def test_report_explains_what_was_learned(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "feedback.db"
            conn = feedback.connect(path)
            for i, (district, elev) in enumerate((("Ѓорче Петров", 250), ("Зелениково", 220))):
                feedback.reject(conn, f"r:{i}", "land", "nor",
                                {"kind": "land", "city": "Скопје", "district": district, "area_m2": 700})
            for i in range(2):
                feedback.reject(conn, f"p:{i}", "land", "oth", {"kind": "land", "city": "Скопје", "district": "Илинден"})
                feedback.add_rejection_note(conn, f"p:{i}", "dropped from the pipeline")
            for i, elev in enumerate((900, 1100)):
                feedback.pipeline_add(conn, f"l:{i}", "land")
                conn.execute("UPDATE pipeline SET snapshot=? WHERE listing_id=?", (json.dumps(
                    {"title": f"Mountain plot {i}", "city": "Охрид", "area": 1000, "price": 25000,
                     "elevation": elev, "utilities": {"electricity": "yes", "water": "yes", "road": "yes"},
                     "metrics": {"Land type": "Building"}}), f"l:{i}"))
            conn.commit()
            conn.close()
            stored = {"r:0": 250.0, "r:1": 220.0, "p:0": 300.0, "p:1": 280.0}
            with mock.patch.object(feedback, "DB_PATH", path), \
                    mock.patch.object(learn, "PRIVATE_JSON", Path(d) / "none.json"), \
                    mock.patch.object(learn, "_stored", lambda ids: {i: {"elevation": e} for i, e in stored.items()}):
                text = learn.report()
        self.assertIn("What you seem to like", text)
        self.assertIn("median 1,000 m", text)
        self.assertIn("2 with power, water and road confirmed", text)
        self.assertIn("question is added to plots in Ѓорче Петров, Зелениково", text)
        self.assertIn("ask whether a built road reaches the plot itself", text)
        self.assertIn("2 land deals were dropped without a reason", text)
        self.assertIn("Altitude: plots you liked sit at about 1,000 m", text)


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


class QuickCheckTests(unittest.TestCase):
    def test_extract_sale_rent_land(self):
        from scraper.quick_check import extract
        sale = extract("Се продава стан во Карпош, 65 м2, 3 соби, цена 89.000 €")
        self.assertEqual((sale["kind"], sale["deal"], sale["city"], sale["district"], sale["price_eur"], sale["area_m2"]),
                         ("apartment", "sale", "Скопје", "Карпош", 89000, 65))
        rent = extract("Izdavam stan vo Debar Maalo 55m2 namesten, 350 evra mesecno")
        self.assertEqual((rent["deal"], rent["price_note"], rent["district"]), ("rent", None, "Скопје Центар"))
        per_m2 = extract("Stan vo Aerodrom 70m2 1.500 €/m2")
        self.assertEqual((per_m2["price_eur"], per_m2["price_note"]), (1500, "per_m2"))
        land = extract("Prodavam plac vo Ohrid 600m2, struja i voda ima, 45.000€")
        self.assertEqual((land["kind"], land["city"], land["utilities"]["water"]), ("land", "Охрид", True))
