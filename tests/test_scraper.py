"""Run:  python -m unittest discover tests"""
import tempfile
import unittest
from unittest import mock
from datetime import date
from pathlib import Path

from scraper.market import Market, total_price
from scraper.run import price_and_deal
from scraper.sources import reklama5
from scraper.text import (classify_deal, detect_furnished, detect_utilities, is_abroad, land_type,
                          mentions_price_per_m2, norm, parse_area, parse_price, redact)

FIX = Path(__file__).parent / "fixtures"


def fixture(name):
    return (FIX / name).read_text(encoding="utf-8")


class TextTests(unittest.TestCase):
    def test_norm_folds_scripts(self):
        self.assertEqual(norm("Се продава ЌЕЛИЈА"), norm("se prodava kjelija"))
        self.assertEqual(norm("Струја"), norm("struja"))
        self.assertEqual(norm("Štip"), norm("Штип"))

    def test_prices(self):
        self.assertEqual(parse_price("155.000 €"), 155000)
        self.assertIsNone(parse_price("По Договор"))
        self.assertAlmostEqual(parse_price("6.150.000 МКД"), 100000)

    def test_areas(self):
        self.assertEqual(parse_area("Плац 825м²"), 825)
        self.assertEqual(parse_area("Површина:40 130м2"), 40130)
        self.assertEqual(parse_area("plac 3.581 M2"), 3581)
        self.assertEqual(parse_area("5 ари"), 500)
        self.assertEqual(parse_area("1,5 хектари"), 15000)

    def test_utilities(self):
        self.assertEqual(detect_utilities("Плац со струја, вода и асфалтен пат"),
                         {"electricity": True, "water": True, "road": True})
        self.assertEqual(detect_utilities("nema struja i voda, pristap so makadam"),
                         {"electricity": False, "water": False, "road": True})
        # District names and "travel" must not count as water / road.
        self.assertEqual(detect_utilities("Plac vo Kisela Voda, pogoden za patuvanje"),
                         {"electricity": None, "water": None, "road": None})
        self.assertEqual(set(detect_utilities("Комунално опремен плац").values()), {True})

    def test_deal_type(self):
        self.assertEqual(classify_deal("Се издава стан во Центар", "", 400, "apartment"), "rent")
        self.assertEqual(classify_deal("Se prodava stan", "", 90000, "apartment"), "sale")
        self.assertEqual(classify_deal("НОВ СТАН 70м²", "", 95000, "apartment"), "sale")
        self.assertEqual(classify_deal("НОВ СТАН 70м²", "", 450, "apartment"), "rent")
        self.assertEqual(classify_deal("Apartman za nokevanje", "", 30, "apartment"), "short_term")
        self.assertEqual(classify_deal("Kupuvam stan vo Karpos", "", None, "apartment"), "wanted")

    def test_misc(self):
        self.assertTrue(mentions_price_per_m2("цена 45 евра за м2"))
        self.assertFalse(mentions_price_per_m2("Plac 1200m2 cena 80000€"))
        self.assertTrue(is_abroad("Плац Сани, Халкидики"))
        self.assertFalse(is_abroad("Плац во Драчево"))
        self.assertEqual(land_type("Градежно", ""), "building")
        self.assertEqual(land_type(None, "se prodava niva"), "agricultural")
        self.assertTrue(detect_furnished("Комплетно наместен стан"))
        self.assertFalse(detect_furnished("Полу наместен"))

    def test_redact_contacts(self):
        self.assertEqual(redact("Сашо на 076 506 300 или 070/200-700"), "Сашо на [тел.] или [тел.]")
        self.assertEqual(redact("+389 2 3123 456, ana@mail.mk"), "[тел.], [e-mail]")
        self.assertEqual(redact("цена 105.000 €, 81 m², 2014"), "цена 105.000 €, 81 m², 2014")

    def test_price_resolution(self):
        self.assertEqual(price_and_deal(1, "land", 500, "Plac"), (None, "placeholder", "sale"))
        self.assertEqual(price_and_deal(80, "land", 500, "Plac"), (80, "per_m2", "sale"))
        self.assertEqual(price_and_deal(40000, "land", 500, "Plac"), (40000, None, "sale"))
        # Flats: per-m² prices must not become 99% bargains or fake rentals.
        self.assertEqual(price_and_deal(1400, "apartment", 100, "Се продава стан"), (1400, "per_m2", "sale"))
        self.assertEqual(price_and_deal(1200, "apartment", 60, "Стан 60м2 Центар"), (1200, "per_m2", "sale"))
        self.assertEqual(price_and_deal(400, "apartment", 60, "Стан 60м2 Центар"), (400, None, "rent"))
        self.assertEqual(price_and_deal(1200, "apartment", 60, "Се издава стан"), (1200, None, "rent"))
        self.assertEqual(price_and_deal(95000, "apartment", 60, "Стан 60м2"), (95000, None, "sale"))
        self.assertEqual(total_price({"price_eur": 80, "price_note": "per_m2", "area_m2": 500}), 40000)


class Reklama5Tests(unittest.TestCase):
    def test_location_with_macedonian_month(self):
        html = fixture("list_apartments.html").replace("23 сеп", "15 мај", 1)
        card = reklama5.parse_list(html, 159, today=date(2026, 10, 6))[0]
        self.assertEqual((card["district"], card["posted"]), ("Карпош", "2026-05-15"))

    def test_list_page(self):
        cards = reklama5.parse_list(fixture("list_apartments.html"), 159, today=date(2026, 10, 6))
        self.assertGreaterEqual(len(cards), 30)
        first = cards[0]
        self.assertEqual(first["source_id"], "5810131")
        self.assertEqual(first["price_eur"], 105000)
        self.assertEqual(first["area_m2"], 81)
        self.assertEqual(first["city"], "Скопје")
        self.assertEqual(first["district"], "Карпош")
        self.assertTrue(first["promoted"])

    def test_detail_page(self):
        d = reklama5.parse_detail(fixture("detail_apartment.html"))
        self.assertEqual(d["price_eur"], 105000)
        self.assertEqual(d["area_m2"], 81)
        self.assertEqual(d["rooms"], 4)
        # The seller left the form's default pin (east Skopje) for a flat in Тафталиџе (west).
        self.assertIsNone(d["lat"])
        self.assertIn("Тафталиџе", d["description"])
        self.assertEqual(d["fields"]["Година на градба"], "2014")

    def test_land_detail_uses_text_area(self):
        d = reklama5.parse_detail(fixture("detail_land.html"))
        self.assertEqual(d["area_m2"], 40130)   # form says "1 m²"; text says 40 130 m²


class MarketTests(unittest.TestCase):
    def test_fallback_levels(self):
        rows = [{"kind": "apartment", "deal": "sale", "city": "Скопје", "district": "Карпош",
                 "price_eur": 2000 * 60, "area_m2": 60}] * 10
        rows += [{"kind": "apartment", "deal": "sale", "city": "Скопје", "district": "Бутел",
                  "price_eur": 1000 * 60, "area_m2": 60}] * 3
        m = Market(rows, 8, 8, 15)
        self.assertEqual(m.reference("apartment", "sale", "Скопје", "Карпош")[2], "district")
        ref, n, level = m.reference("apartment", "sale", "Скопје", "Бутел")
        self.assertEqual((level, n), ("city", 13))
        self.assertEqual(m.reference("land", "sale", "Скопје", None), (None, 0, None))


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        from scraper import feedback
        self.fb = feedback
        mk = lambda i, district, score=60: {"id": i, "score": score, "qualified": True, "kind": "apartment",
                                            "city": "Скопје", "district": district, "reasons": []}
        self.results = {"flip": [mk("a", "Карпош"), mk("b", "Карпош"), mk("c", "Бутел"), mk("d", "Бутел")]}
        v = lambda i, district, vote: {"listing_id": i, "researcher": "flip", "vote": vote, "kind": "apartment",
                                       "city": "Скопје", "district": district}
        self.votes = [v("a", "Карпош", 1), v("c", "Бутел", -1)]

    def test_rejected_hidden_and_similar_nudged(self):
        out = {x["id"]: x for x in self.fb.apply(self.results, self.votes)["flip"]}
        self.assertNotIn("c", out)                       # 👎 hides the deal
        self.assertGreater(out["b"]["score"], 60)        # liked a deal in the same district
        self.assertLess(out["d"]["score"], 60)           # disliked a deal in the same district
        self.assertTrue(any("your votes" in r for r in out["b"]["reasons"]))

    def test_public_view_reveals_nothing(self):
        out = {x["id"]: x for x in self.fb.apply(self.results, self.votes, personalize=False)["flip"]}
        self.assertNotIn("c", out)
        self.assertEqual(out["b"]["score"], 60)
        self.assertEqual(out["b"]["reasons"], [])

    def test_button_payload_fits_telegram_limit(self):
        from scraper.notify import vote_buttons
        for b in vote_buttons("motivated", "reklama5:58101319")["inline_keyboard"][0]:
            self.assertLessEqual(len(b["callback_data"].encode()), 64)


class AirbnbCalibrationTests(unittest.TestCase):
    def test_blends_observed_nightly_prices_with_estimate(self):
        import tomllib
        from scraper.researchers.airbnb import calibrate_adr
        from scraper.text import KeywordSet
        c = tomllib.loads((Path(__file__).parent.parent / "config.toml").read_text(encoding="utf-8"))["airbnb"]
        ad = lambda p, title="Стан за ноќевање": {"kind": "apartment", "deal": "short_term", "city": "Скопје",
                                                   "district": "Скопје Центар", "price_eur": p, "rooms": 2.0,
                                                   "title": title, "lat": c["center_lat"], "lng": c["center_lng"]}
        prior, k = c["adr_by_rooms"]["2"], c["adr_prior_weight"]
        # Higher real prices raise the estimate (blended).
        adr, n = calibrate_adr([ad(90.0)] * 6, c, KeywordSet([]))["2"]
        self.assertEqual(n, 6)
        self.assertAlmostEqual(adr, (6 * 90 + k * prior) / (6 + k))
        # Budget ads never pull it below the estimate; hourly ads are ignored.
        self.assertEqual(calibrate_adr([ad(20.0)] * 6, c, KeywordSet([]))["2"][0], prior)
        self.assertEqual(calibrate_adr([ad(90.0, "Стан 3 часа")] * 6, c, KeywordSet([]))["2"], (prior, 0))
        # Too few ads → estimate unchanged.
        self.assertEqual(calibrate_adr([ad(90.0)] * 2, c, KeywordSet([]))["2"][0], prior)


class KirsmTests(unittest.TestCase):
    def test_parse_bailiff_sales(self):
        from scraper.sources import kirsm
        items = kirsm.parse_list(fixture("kirsm_list.html"))
        ids = [i["source_id"] for i in items]
        self.assertNotIn("14832", ids)                 # duplicate of 14833 removed
        flat = next(i for i in items if i["source_id"] == "14833")
        self.assertEqual((flat["kind"], flat["city"], flat["area_m2"], flat["auction_date"]),
                         ("apartment", "Скопје", 58.0, "2026-10-26"))
        self.assertAlmostEqual(flat["price_eur"], 6099000 / 61.5, delta=1)
        self.assertEqual(flat["deal"], "auction")

    def test_ownership_share(self):
        from scraper.sources.kirsm import ownership_share
        self.assertEqual(ownership_share("1/6 идеален дел -имотен лист"), "1/6")
        self.assertEqual(ownership_share("-1/2 (една половина) идеален дел"), "1/2")
        self.assertIsNone(ownership_share("ул. Мито Х. Василев бр.36-1/1 во Кавадарци"))


class RatingTests(unittest.TestCase):
    def item(self, score, **kw):
        base = {"score": score, "qualified": True, "has_details": True, "reasons": [],
                "metrics": {"Below market": "40%"}, "comps": 40}
        base.update(kw)
        return base

    def test_tiers(self):
        from scraper.rating import rate
        self.assertEqual(rate(self.item(93), "flip")["tier"], "once")
        self.assertEqual(rate(self.item(84), "flip")["tier"], "exceptional")
        self.assertEqual(rate(self.item(72), "flip")["tier"], "great")
        self.assertEqual(rate(self.item(60), "flip")["tier"], "good")
        self.assertEqual(rate(self.item(30), "flip")["tier"], "watch")

    def test_top_tiers_need_solid_evidence(self):
        from scraper.rating import rate
        self.assertEqual(rate(self.item(95, has_details=False), "flip")["tier"], "great")
        self.assertEqual(rate(self.item(95, reasons=["⚠ check"]), "flip")["tier"], "great")
        self.assertEqual(rate(self.item(95, metrics={}), "flip")["tier"], "great")
        self.assertEqual(rate(self.item(95, qualified=False), "land")["tier"], "good")
        self.assertEqual(rate(self.item(95, metrics={}), "airbnb")["tier"], "once")
        self.assertEqual(rate(self.item(95, status="needs_info"), "auctions")["tier"], "great")

    def test_top_tiers_need_enough_comparables(self):
        from scraper.rating import rate
        self.assertEqual(rate(self.item(95, comps=20), "land")["tier"], "exceptional")   # 💎 needs 30+
        self.assertEqual(rate(self.item(95, comps=10), "land")["tier"], "great")         # 🔥 needs 15+
        self.assertEqual(rate(self.item(95, comps=0), "airbnb")["tier"], "once")        # no market comparison involved

    def test_sorted_best_first(self):
        from scraper.rating import rate_all
        out = rate_all({"flip": [self.item(60, id="a"), self.item(95, id="b", has_details=False),
                                 self.item(91, id="c")]})["flip"]
        self.assertEqual([x["id"] for x in out], ["c", "b", "a"])


class HttpRetryTests(unittest.TestCase):
    def test_timeouts_are_not_a_block(self):
        import requests
        from scraper.http import Blocked, PoliteSession, Unavailable
        s = PoliteSession(delay=0, jitter=0)
        s._allowed = lambda url: True
        s.s.get = lambda url, timeout: (_ for _ in ()).throw(requests.ReadTimeout("slow"))
        with mock.patch("scraper.http.time.sleep"):
            with self.assertRaises(Unavailable):
                s.get("https://m.reklama5.mk/AdDetails?ad=1")
        self.assertTrue(issubclass(Unavailable, requests.RequestException))   # callers skip and continue

    def test_challenge_is_a_block(self):
        from scraper.http import Blocked, PoliteSession
        s = PoliteSession(delay=0, jitter=0)
        s._allowed = lambda url: True
        s.s.get = lambda url, timeout: mock.Mock(status_code=403, text="")
        with self.assertRaises(Blocked):
            s.get("https://m.reklama5.mk/Search?cat=159")


if __name__ == "__main__":
    unittest.main()


class HouseAreaRegressionTests(unittest.TestCase):
    """Owner report (reklama5 ad 5564779): a 168 m² house on a 600 m² plot was valued as 600 m²."""

    def test_card_uses_built_area_not_plot(self):
        from scraper.sources.reklama5 import _card_area
        self.assertEqual(_card_area("Парцела: 600 m² • Изградена: 168 m² • 4 соби"), 168)
        self.assertIsNone(_card_area("Парцела: 600 m²"))          # unknown beats wrong

    def test_description_beats_plot_sized_area(self):
        from scraper.run import detail_update
        listing = {"kind": "house", "title": "СЕ ПРОДАВА КУЌА ВО ВИЗБЕГОВО", "price_eur": 273000, "area_m2": 600}
        d = {"title": listing["title"], "description": "СЕ ПРОДАВА КУЌА ОД 168М2, ПРИЗЕМЈЕ ПЛУС КАТ",
             "fields": {"Површина на парцела (m²)": "600 m²"}, "area_m2": 600, "price_eur": 273000}
        self.assertEqual(detail_update(listing, d)["area_m2"], 168)

    def test_maps_query(self):
        from scraper import maps
        self.assertEqual(maps.query({"city": "Тетово", "extra": {"note": "ИЛ бр.416 КО ГРУПЧИН Продажбата"}}),
                         "Групчин, Тетово, North Macedonia")
        self.assertIn("41.990000,21.430000", maps.url(41.99, 21.43, None))


class SizeBandTests(unittest.TestCase):
    """Owner request: compare with property of similar size (big plots are cheaper per m²)."""

    def rows(self, n, area, ppm2, district=None):
        return [{"kind": "land", "deal": "sale", "city": "Охрид", "district": district,
                 "price_eur": ppm2 * area, "area_m2": area}] * n

    def test_land_compared_within_its_size_band(self):
        m = Market(self.rows(10, 300, 100) + self.rows(10, 2000, 30), 8, 8, 15)
        big = m.reference("land", "sale", "Охрид", None, 2000)
        small = m.reference("land", "sale", "Охрид", None, 300)
        self.assertEqual((big[0], big.band), (30, "1,500–3,000 m²"))
        self.assertEqual((small[0], small.band), (100, "under 400 m²"))
        self.assertEqual(big.describe("land"), "city median for 1,500–3,000 m² plots")

    def test_same_size_in_town_beats_all_sizes_next_door(self):
        rows = self.rows(10, 2000, 30) + self.rows(15, 300, 100, district="Центар")
        m = Market(rows, 15, 8, 15)
        self.assertEqual(m.reference("land", "sale", "Охрид", "Центар", 2000).band, "1,500–3,000 m²")

    def test_too_few_in_band_falls_back_to_all_sizes(self):
        m = Market(self.rows(3, 2000, 30) + self.rows(10, 300, 100), 8, 8, 15)
        r = m.reference("land", "sale", "Охрид", None, 2000)
        self.assertIsNone(r.band)
        self.assertEqual(r[1], 13)


class AltitudeTests(unittest.TestCase):
    def test_place_candidates(self):
        from scraper.geo import place_candidates, to_cyrillic
        self.assertEqual(to_cyrillic("Kuckovo"), "Куцково")      # plain c → ц (č would be ч)
        c = place_candidates({"title": "Се продава плац во близина на Охрид, Мешеишта", "district": "Дебарца", "city": "Охрид"})
        self.assertEqual(c[0], "Мешеишта")
        self.assertIn("Групчин", place_candidates({"title": "Bailiff sale", "city": "Тетово",
                                                   "extra": {"note": "ИЛ бр.416 КО ГРУПЧИН Продажбата"}}))

    def test_prefers_villages_over_peaks(self):
        from scraper.db import DB
        from scraper.geo import Geo
        with tempfile.TemporaryDirectory() as d:
            db = DB(Path(d) / "t.db")
            g = Geo(db, delay=0)
            answers = {"Илинден": {"results": []},
                       "Ilinden": {"results": [{"name": "Ilinden", "feature_code": "PK", "elevation": 2511,
                                                "latitude": 41.0, "longitude": 21.0},
                                               {"name": "Ilinden", "feature_code": "PPLA", "elevation": 231,
                                                "latitude": 41.99, "longitude": 21.58}]}}
            g._get = lambda url, params: answers.get(params["name"], {})
            self.assertEqual(g.place("Илинден")[2], 231)
            g._get = lambda url, params: (_ for _ in ()).throw(AssertionError("should be cached"))
            self.assertEqual(g.place("Илинден")[2], 231)
            db.conn.close()


class DistanceTests(unittest.TestCase):
    def test_distance_formatting(self):
        from scraper.researchers.common import _distance, fmt_drive
        l = {"lat": 41.23814, "lng": 20.77414, "ohrid_km": 16.4, "ohrid_min": 15.2, "skopje_km": 158.1, "skopje_min": 169.4}
        self.assertEqual(fmt_drive(_distance(l, "ohrid")), "16 km · 15 min by car")
        self.assertEqual(fmt_drive(_distance(l, "skopje")), "158 km · 2 h 49 by car")
        village_only = {"approx_lat": 41.99297, "approx_lng": 21.58084}          # no route yet: straight line
        self.assertEqual(fmt_drive(_distance(village_only, "skopje")), "12 km (straight line)")
        self.assertIsNone(_distance({}, "skopje"))
