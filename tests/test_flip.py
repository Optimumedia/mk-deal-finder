"""Fix & flip researcher: like-for-like comparisons and honest economics (synthetic data only)."""
import tomllib
import unittest
from collections import Counter
from pathlib import Path

from scraper.market import Market
from scraper.researchers import flip

CFG = tomllib.loads((Path(__file__).parent.parent / "config.toml").read_text(encoding="utf-8"))


def flat(i, ppm, area=50, city="Скопје", district="Карпош", kind="apartment", title="Стан", desc="", **kw):
    return {"id": f"r:{i}", "url": "u", "source": "reklama5", "source_id": str(i), "kind": kind, "deal": "sale",
            "price_eur": ppm * area, "price_note": None, "area_m2": area, "city": city, "district": district,
            "title": title, "description": desc, "first_seen": "2026-11-20", "detail_at": "2026-11-20", **kw}


def market(ppms, **kw):
    """Comparable listings: one per €/m² value."""
    return Market([flat(1000 + n, p, **kw) for n, p in enumerate(ppms)], 15, 8, 15)


# 20 Karpoš flats 45–75 m² asking 2,000–2,950 €/m² (median ~2,475).
KARPOS = market([2000 + 50 * n for n in range(20)])


class FlipEconomicsTests(unittest.TestCase):
    def run_one(self, listing, mkt=KARPOS, cfg=CFG):
        f = Counter()
        out = flip.run([listing], mkt, cfg, funnel=f)
        return (out[0] if out else None), f

    def test_renovated_cheap_flat_is_a_deal_with_honest_numbers(self):
        r, _ = self.run_one(flat(1, 1300, area=50, fields={"Состојба": "Реновиран"}))
        self.assertIsNotNone(r)
        m = r["metrics"]
        c = CFG["flip"]
        self.assertEqual(m["Condition"], "renovated")
        price = 65000
        ref = m["Local median €/m²"]
        resale = ref * c["renovated_value_ratio"] * 50 * (1 - c["resale_discount"])
        buy, sell = price * c["buy_costs"], resale * c["sell_costs"]
        reno = 50 * c["renovation_refresh_per_m2"]
        hold = c["holding_months"] * c["holding_cost_per_month"]
        tax = (resale - sell - price - buy) * 0.10           # full gain: no 70% base, no exemption
        self.assertAlmostEqual(m["Est. profit"], round(resale - price - buy - reno - hold - sell - tax), delta=2)
        self.assertEqual(m["Months to sell est."], c["holding_months"])

    def test_condition_reading(self):
        self.assertEqual(flip.condition({"kind": "apartment", "renovation": 1, "fields": {"Состојба": "Реновиран"}}),
                         "renovated")       # the site's field beats "стара градба" in the text
        self.assertEqual(flip.condition({"kind": "apartment", "fields": {"Состојба": "Стар"}}), "medium")
        self.assertEqual(flip.condition({"kind": "apartment", "fields": {"Состојба": "Стар",
                                                                         "Година на градба": "1972"}}), "heavy")
        self.assertEqual(flip.condition({"kind": "house", "fields": {"Состојба": "Стара"}}), "heavy")
        self.assertEqual(flip.condition({"kind": "apartment", "fields": {}}), "light")
        self.assertEqual(flip.condition({"kind": "apartment", "fields": {"Состојба": "За реновирање"}}), "heavy")

    def test_basement_is_compared_with_basements(self):
        # 1,400 €/m² is 43% under a normal flat, but only ~22% under a basement one (0.73×).
        normal, _ = self.run_one(flat(2, 1400, fields={"Состојба": "Реновиран"}))
        base, _ = self.run_one(flat(3, 1400, title="Стан сутерен", fields={"Состојба": "Реновиран"}))
        self.assertGreater(int(normal["metrics"]["Below market"].rstrip("%")), 35)
        self.assertIsNone(base)             # too thin once the basement discount is priced in

    def test_village_share_swap_and_unregistered_are_skipped(self):
        for l in (flat(4, 1200, title="Куќа во с. Кучково", kind="house"),
                  flat(5, 1200, desc="Се продава идеален дел од стан"),
                  flat(6, 1200, title="Менувам стан за куќа"),
                  flat(7, 1200, desc="13 м2 не е упишана во имотен лист"),
                  flat(8, 1200, desc="Станот е во градба")):
            r, f = self.run_one(l)
            self.assertIsNone(r, l["title"] + l["description"])

    def test_no_skopje_wide_or_wrong_town_fallback(self):
        # No Butel comparables: a Skopje-wide median (Centar + Šuto Orizari) must not be used.
        skopje_wide = market([2000 + 50 * n for n in range(20)], district="Карпош")
        r, f = self.run_one(flat(9, 900, district="Бутел"), mkt=skopje_wide)
        self.assertIsNone(r)
        self.assertEqual(f["local comparables"], 0)
        # Dojran filed under Gevgelija is not compared with Gevgelija town.
        gev = market([900 + 10 * n for n in range(20)], city="Гевгелија", district="Гевгелија")
        r, f = self.run_one(flat(10, 400, city="Гевгелија", district="Дојран"), mkt=gev)
        self.assertEqual(f["local comparables"], 0)

    def test_text_price_contradiction(self):
        l = flat(11, 1000, desc="Нов стан, цена 1400 евра со ДДВ")
        self.assertTrue(flip.price_contradicted(l, 1000))
        self.assertFalse(flip.price_contradicted(flat(12, 1400, desc="цена 1400 евра"), 1400))

    def test_same_flat_from_two_agencies_shown_once(self):
        a = flat(13, 1300, fields={"Состојба": "Реновиран"})
        b = dict(flat(14, 1300, fields={"Состојба": "Реновиран"}), price_eur=1300 * 50 + 700)
        out = flip.run([a, b], KARPOS, CFG)
        self.assertEqual(len(out), 1)

    def test_small_town_is_slower_and_cheaper_to_sell(self):
        towns = market([800 + 10 * n for n in range(20)], city="Кичево", district="Кичево")
        r, _ = self.run_one(flat(15, 450, city="Кичево", district="Кичево", fields={"Состојба": "Реновиран"}),
                            mkt=towns)
        self.assertIsNotNone(r)
        self.assertEqual(r["metrics"]["Months to sell est."], CFG["flip"]["holding_months_small_town"])
        self.assertTrue(any("smaller market" in x for x in r["reasons"]))


if __name__ == "__main__":
    unittest.main()
