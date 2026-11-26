"""Airbnb / Booking arbitrage researcher: deal clean-up, not-a-home test, economics."""
import tomllib
import unittest
from pathlib import Path

from scraper.market import Market
from scraper.researchers import airbnb
from scraper.text import KeywordSet, classify_deal

CFG = tomllib.loads((Path(__file__).parent.parent / "config.toml").read_text(encoding="utf-8"))
C = CFG["airbnb"]


def flat(i="reklama5:1", rent=150.0, deal="rent", title="Се издава стан во Центар", desc="", **kw):
    return {"id": i, "url": f"https://example.test/{i}", "source": "reklama5", "kind": "apartment",
            "deal": deal, "city": "Скопје", "district": "Скопје Центар", "price_eur": rent,
            "area_m2": 50.0, "rooms": 2.0, "furnished": 1, "title": title, "description": desc,
            "lat": C["center_lat"], "lng": C["center_lng"], "detail_at": "2026-11-25T00:00:00",
            "first_seen": "2026-11-25T00:00:00", **kw}


class DealCleanupTests(unittest.TestCase):
    def test_living_room_is_not_a_short_stay(self):
        # "дневен престој" = living room; a €400 ad with it is a monthly rental.
        self.assertEqual(classify_deal("Се издава стан", "Дневен престој со кујна, две спални", 400, "apartment"),
                         "rent")
        stored = flat(rent=400.0, deal="short_term", desc="Дневен престој со кујна, две спални")
        self.assertEqual(airbnb.effective_deal(stored, C), "rent")

    def test_real_short_stays_stay_short_term(self):
        self.assertEqual(airbnb.effective_deal(flat(rent=30.0, deal="short_term"), C), "short_term")
        nightly = flat(rent=150.0, deal="short_term", desc="Стан за краток престој и ноќевање")
        self.assertEqual(airbnb.effective_deal(nightly, C), "short_term")
        self.assertEqual(airbnb.effective_deal(flat(deal="sale", rent=90000.0), C), "sale")


class NotAHomeTests(unittest.TestCase):
    def test_flats_that_also_suit_an_office_are_homes(self):
        for desc in ("Погоден за живеење и за деловна намена или канцеларија",
                     "Odgovara i za kancelarija, moze kompletno da se namesti",
                     "Агенција: ТЕТОВО Ул. ЈНА бр. 51, локал 2",
                     "Одлична локација, до градски парк"):
            self.assertFalse(airbnb.not_a_home(flat(desc=desc)), desc)
        self.assertFalse(airbnb.not_a_home(flat(title="SE IZDAVA ZA ZIVEENJE ILI ZA KANCELARIJA")))

    def test_offices_and_venues_are_not(self):
        self.assertTrue(airbnb.not_a_home(flat(title="Se izdava kancelariski prostor vo Karpos 3")))
        self.assertTrue(airbnb.not_a_home(flat(title="Stan / kancelarija kaj Soboren hram")))
        self.assertTrue(airbnb.not_a_home(flat(desc="stanot se izdava prvenstveno za deloven prostor")))
        self.assertTrue(airbnb.not_a_home(flat(desc="Дарма Дома издава 🏢 Деловен простор | Центар")))
        self.assertTrue(airbnb.not_a_home(flat(desc="Простор за роденденски прослави")))


class EconomicsTests(unittest.TestCase):
    def run_one(self, listing, **cfg_over):
        cfg = {**CFG, "airbnb": {**C, **cfg_over}}
        return airbnb.run([listing], Market([]), cfg)

    def test_cheap_central_flat_is_a_deal_with_setup_kit_costed(self):
        out = self.run_one(flat(rent=120.0))
        self.assertEqual(len(out), 1)
        m = out[0]["metrics"]
        with_kit = m["Est. costs / month"]
        no_kit = self.run_one(flat(rent=120.0), setup_cost_furnished=0)[0]["metrics"]["Est. costs / month"]
        self.assertAlmostEqual(with_kit - no_kit, C["setup_cost_furnished"] / C["furnishing_months"], delta=1)
        self.assertEqual(m["Upfront cash"], 2 * 120 + C["setup_cost_furnished"])
        # Revenue minus costs minus rent = profit; tax and platform fee are in the costs.
        self.assertAlmostEqual(m["Est. revenue / month"] - m["Est. costs / month"] - 120,
                               m["Est. profit / month"], delta=1.5)

    def test_near_miss_kept_only_within_negotiation_margin(self):
        base = self.run_one(flat(rent=150.0), negotiation_margin=10)[0]["metrics"]
        max_rent = base[f"Max rent for €{C['min_monthly_profit']:.0f} profit"]
        just_over = round(max_rent / (1 - C["negotiation_margin"])) - 2      # needs a <10% cut
        out = self.run_one(flat(rent=float(just_over)))
        self.assertEqual(len(out), 1)
        self.assertIn("offer that", out[0]["reasons"][0])
        self.assertEqual(self.run_one(flat(rent=float(round(max_rent * 1.25)))), [])

    def test_mis_filed_short_term_rental_is_evaluated(self):
        out = self.run_one(flat(rent=120.0, deal="short_term", desc="Дневен престој со кујна"))
        self.assertEqual(len(out), 1)

    def test_detail_priority_reads_misfiled_and_priceless_rentals(self):
        from scraper.run import detail_priority
        kw = KeywordSet([])
        self.assertEqual(detail_priority(flat(rent=350.0, deal="short_term"), CFG, Market([]), kw, kw), 3)
        self.assertEqual(detail_priority(flat(rent=None), CFG, Market([]), kw, kw), 1.0)
        self.assertEqual(detail_priority(flat(rent=30.0, deal="short_term"), CFG, Market([]), kw, kw), 1.5)


if __name__ == "__main__":
    unittest.main()
