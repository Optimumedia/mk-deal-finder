"""Market evidence rules and the Motivated-sellers price history.
Run:  python -m unittest discover -s tests"""
import tomllib
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from scraper.market import Market, evidence_weight, is_stable, median_range, weighted_median
from scraper.researchers import motivated
from scraper.researchers.motivated import _opening_offer, attach_history, on_market_since, price_story

NOW = datetime(2026, 11, 25, 12, tzinfo=timezone.utc)
CFG = tomllib.loads((Path(__file__).parent.parent / "config.toml").read_text(encoding="utf-8"))


def ago(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat()


def flat(ppm2, first, last, district="Карпош", area=60, **kw):
    return {"kind": "apartment", "deal": "sale", "city": "Скопје", "district": district,
            "price_eur": ppm2 * area, "area_m2": area, "first_seen": ago(first), "last_seen": ago(last), **kw}


class EvidenceTests(unittest.TestCase):
    def test_weights(self):
        self.assertEqual(evidence_weight({}, NOW, 60, 30, 2), 1.0)                     # no dates: counts fully
        self.assertEqual(evidence_weight(flat(1, 90, 70), NOW, 60, 30, 2), 0.0)         # outside the window
        self.assertEqual(evidence_weight(flat(1, 50, 50), NOW, 60, 30, 2), 0.0)         # seen once, then gone
        self.assertEqual(evidence_weight(flat(1, 1, 1), NOW, 60, 30, 2), 0.5 ** (1 / 30))  # new ad: kept
        self.assertAlmostEqual(evidence_weight(flat(1, 60, 30), NOW, 60, 30, 2), 0.5)

    def test_weighted_median(self):
        self.assertEqual(weighted_median([(1, 1), (2, 1), (3, 1), (4, 1)]), 2.5)
        self.assertEqual(weighted_median([(1, 0.1), (2, 0.1), (3, 1)]), 3)
        self.assertEqual(weighted_median([(1, 1), (5, 1), (9, 0)]), 3)                 # zero weight ignored

    def test_one_off_cheap_sightings_do_not_drag_the_median(self):
        rows = [flat(2400, 40, 0) for _ in range(20)] + [flat(1200, 50, 50) for _ in range(30)]
        old = Market(rows, 15, 8, 15)
        new = Market(rows, 15, 8, 15, window_days=60, half_life_days=30, min_days_listed=2, now=NOW)
        self.assertEqual(old.reference("apartment", "sale", "Скопје", "Карпош", 60)[0], 1200)
        ref = new.reference("apartment", "sale", "Скопје", "Карпош", 60)
        self.assertEqual((ref[0], ref[1]), (2400, 20))
        self.assertEqual(new.excluded, 30)

    def test_recent_asks_weigh_more(self):
        rows = [flat(2000, 50, 45) for _ in range(10)] + [flat(2600, 5, 0) for _ in range(9)]
        m = Market(rows, 15, 8, 15, window_days=60, half_life_days=30, now=NOW)
        self.assertEqual(m.reference("apartment", "sale", "Скопје", "Карпош", 60)[0], 2600)
        self.assertEqual(Market(rows, 15, 8, 15).reference("apartment", "sale", "Скопје", "Карпош", 60)[0], 2000)

    def test_from_config_reads_the_market_section(self):
        m = Market.from_config([flat(2000, 10, 0)] * 15, CFG["market"], now=NOW)
        self.assertEqual(m.mins, (CFG["market"]["min_samples_district"], CFG["market"]["min_samples_city"],
                                  CFG["market"]["min_samples_national"]))
        self.assertEqual(m.reference("apartment", "sale", "Скопје", "Карпош", 60)[2], "district")


class StabilityTests(unittest.TestCase):
    def plot(self, ppm2, district, area=1000):
        return {"kind": "land", "deal": "sale", "city": "Скопје", "district": district,
                "price_eur": ppm2 * area, "area_m2": area}

    def test_median_range(self):
        lo, hi = median_range(list(range(1, 101)))
        self.assertTrue(lo < 50 < hi and hi - lo < 25)
        self.assertTrue(is_stable([100] * 20, 100, 0.5))
        self.assertFalse(is_stable([22, 25, 45, 50, 57, 68, 90, 100, 105, 120, 120, 130, 150, 153, 170, 180, 241, 260],
                                   110, 0.8))

    def test_scattered_district_falls_back_to_town(self):
        scattered = [self.plot(v, "Карпош") for v in (20, 25, 30, 40, 60, 80, 100, 120, 150, 200, 250, 300, 400, 500, 600)]
        town = [self.plot(50 + i % 5, "Бутел") for i in range(40)]
        m = Market(scattered + town, 15, 8, 15, max_ci_width=0.8)
        self.assertEqual(m.reference("land", "sale", "Скопје", "Карпош", 1000)[2], "city")
        self.assertEqual(Market(scattered + town, 15, 8, 15).reference("land", "sale", "Скопје", "Карпош", 1000)[2],
                         "district")

    def test_town_all_sizes_beats_national_same_size(self):
        rows = [self.plot(50, "Карпош", 1000)] * 10 + [self.plot(50, "Карпош", 300)] * 10 + \
               [{**self.plot(5, None, 300), "city": "Штип"}] * 20
        r = Market(rows, 15, 8, 15).reference("land", "sale", "Скопје", None, 300)
        self.assertEqual((r[2], r.band), ("city", "under 400 m²"))
        r = Market(rows[:10] + rows[20:], 15, 8, 15).reference("land", "sale", "Скопје", None, 300)
        self.assertEqual((r[2], r.band, r[0]), ("city", None, 50))       # not the national 300 m² median of 5


class PriceStoryTests(unittest.TestCase):
    def story(self, hist, now_price, last=0, **kw):
        l = {"price_eur": now_price, "last_seen": ago(last), "first_seen": hist[0][0],
             "price_history": hist, **kw}
        return price_story(l)

    def test_typo_fixed_within_a_day_is_not_a_cut(self):
        s = self.story([(ago(30), 100000), (ago(10), 60000), (ago(9.8), 100000)], 100000)
        self.assertEqual((s["drop"], s["blips"]), (0.0, 1))

    def test_unit_switch_is_not_a_cut(self):
        s = self.story([(ago(30), 140000), (ago(10), 1400)], 1400)
        self.assertEqual(s["drop"], 0.0)

    def test_two_real_cuts(self):
        s = self.story([(ago(40), 100000), (ago(20), 95000), (ago(5), 85000)], 85000)
        self.assertAlmostEqual(s["drop"], 0.15)
        self.assertEqual(s["cuts"], 2)
        self.assertFalse(s["pending"])

    def test_new_cut_waits_for_the_next_run(self):
        s = self.story([(ago(40), 100000), (ago(0.1), 80000)], 80000, last=0.1)
        self.assertAlmostEqual(s["drop"], 0.2)
        self.assertTrue(s["pending"])

    def test_strike_through_price_counts(self):
        s = self.story([(ago(5), 90000)], 90000, site_old_price=100000)
        self.assertAlmostEqual(s["drop"], 0.1)
        self.assertTrue(s["site"])

    def test_without_history_uses_first_price(self):
        s = price_story({"price_eur": 90000, "first_price": 100000, "first_seen": ago(20), "last_seen": ago(0)})
        self.assertAlmostEqual(s["drop"], 0.1)


class RepostTests(unittest.TestCase):
    def ad(self, i, title, price, first, last, area=62, image=None, rooms=2.5):
        return {"id": f"reklama5:{i}", "source": "reklama5", "source_id": str(i), "kind": "apartment", "deal": "sale",
                "city": "Скопје", "district": "Центар", "title": title, "price_eur": price, "area_m2": area,
                "rooms": rooms, "image": image, "first_seen": ago(first), "last_seen": ago(last)}

    def filler(self):     # make the template words common, as in the real titles
        return [self.ad(1000 + i, f"Se prodava stan vo Centar {i}", 50000 + i, 60, 60, area=30 + i)
                for i in range(30)]

    def test_repost_with_a_lower_price(self):
        old = self.ad(1, "Se prodava stan Gradski zid kaj Soborna crkva", 95000, 50, 30)
        new = self.ad(2, "Stan Gradski zid kaj Soborna crkva 62m2", 85000, 20, 0)
        self.assertEqual(attach_history([new], {}, [old, new] + self.filler()), 1)
        self.assertEqual(new["earlier_ads"][0]["price"], 95000)

    def test_template_titles_for_different_flats_do_not_match(self):
        old = self.ad(1, "Se prodava stan vo Centar Blok", 95000, 50, 30)
        new = self.ad(2, "Se prodava stan vo Centar Blok", 85000, 20, 0)
        self.assertEqual(attach_history([new], {}, [old, new] + self.filler()), 0)

    def test_ad_listed_alongside_is_a_duplicate_not_a_repost(self):
        old = self.ad(1, "Stan Gradski zid kaj Soborna crkva", 95000, 50, 0)
        new = self.ad(2, "Stan Gradski zid kaj Soborna crkva", 85000, 20, 0)
        self.assertEqual(attach_history([new], {}, [old, new] + self.filler()), 0)

    def test_time_on_market_uses_the_oldest_evidence(self):
        today = date(2026, 11, 25)
        l = {"first_seen": "2026-11-20", "posted": "2026-11-20", "id_age_days": 400}
        self.assertEqual(on_market_since(l, today), (400, "ad number"))
        l = {"first_seen": "2026-11-20", "earlier_ads": [{"first_seen": "2026-10-06"}]}
        self.assertEqual(on_market_since(l, today), (50, "earlier ad"))


class OpeningOfferTests(unittest.TestCase):
    def test_usual_gap(self):
        self.assertEqual(_opening_offer(100000, 0, None, None, None), 90000)

    def test_wider_for_a_stuck_seller(self):
        offer, why = _opening_offer(100000, 0.08, None, None, -0.15, months=8, cuts=2, urgent=True, explain=True)
        self.assertEqual(offer, 80000)           # 10 + 3 + 2 + 2 + 3 = 20%
        self.assertIn("on the market ~8 months", why)

    def test_narrow_but_never_under_8_percent(self):
        offer, why = _opening_offer(100000, 0.2, 2000, 50, 0.2, explain=True)
        self.assertEqual(offer, 92000)
        self.assertIn("at least 8%", why)

    def test_not_below_90_percent_of_local_value(self):
        offer, why = _opening_offer(100000, 0, 2000, 50, 0.0, months=12, cuts=3, explain=True)
        self.assertEqual(offer, 90000)           # 85,000 would be under 90% of 100,000 market value
        self.assertIn("90% of the typical local value", why)


class MotivatedRunTests(unittest.TestCase):
    def listing(self, **kw):
        l = {"id": "reklama5:1", "url": "https://reklama5.mk/AdDetails?ad=1", "source": "reklama5", "source_id": "1",
             "kind": "apartment", "deal": "sale", "city": "Скопје", "district": "Карпош", "title": "Stan",
             "price_eur": 68000, "area_m2": 60, "first_seen": ago(40), "last_seen": ago(0),
             "price_history": [(ago(40), 80000), (ago(20), 76000), (ago(5), 68000)]}
        l.update(kw)
        return l

    def test_confirmed_cuts_are_reported(self):
        out = motivated.run([self.listing()], Market([]), CFG)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["reasons"][0].startswith("cut 2× 15%"))
        self.assertEqual(out[0]["metrics"]["Price cut"], "15%")
        self.assertLessEqual(out[0]["metrics"]["Opening offer idea"], 68000 * 0.92)
        self.assertIn("under asking", out[0]["metrics"]["Why this offer"])

    def test_typo_alone_is_not_a_motivated_seller(self):
        l = self.listing(price_eur=70000, price_history=[(ago(30), 70000), (ago(10), 40000), (ago(9.8), 70000)])
        self.assertEqual(motivated.run([l], Market([]), CFG), [])

    def test_unconfirmed_cut_gets_half_credit(self):
        l = self.listing(price_eur=64000, last_seen=ago(0.1), price_history=[(ago(40), 80000), (ago(0.1), 64000)])
        out = motivated.run([l], Market([]), CFG)
        self.assertIn("waiting for the next run", out[0]["reasons"][0])
        self.assertEqual(out[0]["metrics"]["Price cut"], "10%")


if __name__ == "__main__":
    unittest.main()
