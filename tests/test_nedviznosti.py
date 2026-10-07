"""Run:  python -m unittest discover tests"""
import re
import unittest
from pathlib import Path

from scraper.run import detail_update, normalise_card
from scraper.sources import nedviznosti, reklama5

FIX = Path(__file__).parent / "fixtures"
URL = "https://nedviznosti.com.mk/oglasi/oglas/stan-skopje-90m2-se-iznajmuva-4-soben-stan-2060"


def fixture(name):
    return (FIX / name).read_text(encoding="utf-8")


class SitemapTests(unittest.TestCase):
    def setUp(self):
        self.entries = nedviznosti.parse_sitemap(fixture("nedviznosti_sitemap.xml"))

    def test_only_listing_urls(self):
        self.assertEqual(len(self.entries), 27)       # 30 <url>s minus home, agency and category pages
        self.assertTrue(all("/oglasi/oglas/" in u for u, _ in self.entries))
        self.assertEqual(self.entries[0], ("https://nedviznosti.com.mk/oglasi/oglas/stan-skopje-35m2-stan-so-2-5-sobi-2064",
                                           "2026-10-06T20:48:59.838Z"))
        self.assertIn((URL, "2026-10-06T20:50:37.917Z"), self.entries)

    def test_changed_since(self):
        known = dict(self.entries)
        self.assertEqual(nedviznosti.changed_since(self.entries, known), [])
        known.pop(URL)
        first = self.entries[0][0]
        known[first] = "2026-10-01T00:00:00.000Z"
        self.assertEqual(nedviznosti.changed_since(self.entries, known), [first, URL])
        self.assertEqual(len(nedviznosti.changed_since(self.entries, {})), 27)

    def test_source_id(self):
        self.assertEqual(nedviznosti.source_id(URL), "2060")
        self.assertEqual(nedviznosti.source_id(
            "https://nedviznosti.com.mk/oglasi/oglas/kuka-skopje-250m2-kukja-bardovci-250m2-5-sobi-1779349284918"),
            "1779349284918")


class ListingTests(unittest.TestCase):
    def setUp(self):
        self.html = fixture("nedviznosti_detail.html")
        self.d = nedviznosti.parse_listing(self.html, URL)

    def test_has_every_reklama5_key(self):
        card_keys = set(reklama5.parse_list(fixture("list_apartments.html"), 159)[0])
        detail_keys = set(reklama5.parse_detail(fixture("detail_apartment.html")))
        self.assertLessEqual((card_keys | detail_keys) - {"renewed"}, set(self.d) | {"cat"})   # renewed: Reklama5 only
        self.assertIn("deal", self.d)

    def test_card_values(self):
        d = self.d
        self.assertEqual(d["source"], "nedviznosti")
        self.assertEqual(d["source_id"], "2060")
        self.assertEqual(d["url"], URL)
        self.assertIsNone(d["cat"])
        self.assertEqual(d["kind"], "apartment")
        self.assertEqual(d["deal"], "rent")
        self.assertEqual(d["title"], "Се изнајмува 4-собен стан со 3 спални во Капиштец")
        self.assertEqual(d["price_eur"], 600)
        self.assertEqual(d["price_text"], "600 €/месечно")
        self.assertIsNone(d["old_price_eur"])
        self.assertEqual(d["area_m2"], 90)
        self.assertEqual(d["rooms"], 4)           # "соби", not JSON-LD numberOfRooms (= 3 bedrooms)
        self.assertEqual(d["city"], "Скопје")
        self.assertEqual(d["district"], "Скопје Центар")   # site says "Центар"
        self.assertEqual(d["posted"], "2026-10-06")
        self.assertTrue(d["image"].startswith("https://cdn.rast.is/public/default/media/kompletno-ureden-stan"))
        self.assertFalse(d["promoted"])

    def test_detail_values(self):
        d = self.d
        self.assertTrue(d["description"].startswith("Се изнајмува комплетно уреден стан во Центар, Капиштец"))
        self.assertIn("Вселив веднаш.", d["description"])
        self.assertEqual(d["fields"]["Кат"], "4")
        self.assertEqual(d["fields"]["спални"], "3")
        self.assertEqual(d["fields"]["Греење"], "Централно")
        self.assertEqual(d["fields"]["Опрема"], "Целосно опремен")
        self.assertEqual(d["fields"]["Статус"], "Достапно")
        self.assertEqual(d["address"], "Капиштец")
        self.assertIsNone(d["lat"])               # this ad has no map pin
        self.assertIsNone(d["lng"])
        self.assertIsNone(d["seller"])
        self.assertIs(d["agency"], True)

    def test_no_contacts_leak(self):
        blob = repr(self.d)
        self.assertNotRegex(blob, r"@[\w-]+\.\w|\+389|\b07\d{7}\b")

    def test_html_fallback_without_jsonld(self):
        html = re.sub(r'<script type="application/ld\+json">.*?</script>', "", self.html, flags=re.S)
        d = nedviznosti.parse_listing(html, URL)
        self.assertEqual((d["kind"], d["deal"], d["price_eur"], d["area_m2"], d["rooms"]),
                         ("apartment", "rent", 600, 90, 4))
        self.assertEqual(d["title"], "Се изнајмува 4-собен стан со 3 спални во Капиштец")
        self.assertIsNone(d["posted"])            # date only lives in JSON-LD (+ "6 окт." text)

    def test_coordinates_from_payload(self):
        html = self.html.replace('\\"lat\\":null,\\"lng\\":null', '\\"lat\\":41.9961,\\"lng\\":21.4254')
        d = nedviznosti.parse_listing(html, URL)
        self.assertEqual((d["lat"], d["lng"]), (41.9961, 21.4254))
        html = self.html.replace('\\"lat\\":null,\\"lng\\":null', '\\"lat\\":48.85,\\"lng\\":2.35')
        self.assertIsNone(nedviznosti.parse_listing(html, URL)["lat"])     # outside North Macedonia

    def test_feeds_run_pipeline(self):
        card = normalise_card(dict(self.d))
        self.assertEqual((card["price_eur"], card["price_note"], card["deal"]), (600, None, "rent"))
        upd = detail_update(card, self.d)
        self.assertEqual(upd["deal"], "rent")
        self.assertEqual(upd["furnished"], 1)
        self.assertEqual(upd["agency"], 1)


class PlaceTests(unittest.TestCase):
    def test_districts(self):
        f = nedviznosti._district
        self.assertEqual(f("Скопје", "Центар", "Капиштец"), ("Скопје", "Скопје Центар"))
        self.assertEqual(f("Скопје", "Кисела Вода", "Мичурин"), ("Скопје", "Кисела Вода"))
        self.assertEqual(f("Скопје", "Козле", None), ("Скопје", "Карпош"))          # neighbourhood → municipality
        self.assertEqual(f("Скопје", "Ѓорче Петров", "Орман"), ("Скопје", "Ѓорче Петров"))
        self.assertEqual(f("Илинден", "", None), ("Скопје", "Илинден"))
        self.assertEqual(f("Охрид", "Центар", None), ("Охрид", None))

    def test_city_names(self):
        self.assertEqual(nedviznosti._city("Skopje"), "Скопје")
        self.assertEqual(nedviznosti._city("Охрид"), "Охрид")


if __name__ == "__main__":
    unittest.main()
