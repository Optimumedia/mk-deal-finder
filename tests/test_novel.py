"""Novel Real Estate parser — run:  python -m unittest discover tests"""
import unittest
from pathlib import Path

from scraper.sources import novel, reklama5

FIX = Path(__file__).parent / "fixtures"


def fixture(name):
    return (FIX / name).read_text(encoding="utf-8")


class NovelListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = fixture("novel_list.html")
        cls.cards = novel.parse_list(cls.html)

    def test_count_and_keys(self):
        self.assertEqual(len(self.cards), 12)
        r5 = reklama5.parse_list(fixture("list_apartments.html"), 159)
        self.assertEqual(set(self.cards[0]), (set(r5[0]) - {"renewed"}) | {"deal"})   # renewed: Reklama5 only

    def test_first_card(self):
        c = self.cards[0]
        self.assertEqual(c["source"], "novel")
        self.assertEqual(c["source_id"], "A15579")
        self.assertEqual(c["url"], "https://novelestate.com/estate_view.html?id=A15579")
        self.assertEqual(c["deal"], "rent")
        self.assertEqual(c["kind"], "apartment")
        self.assertEqual(c["price_eur"], 800)
        self.assertEqual(c["area_m2"], 65)
        self.assertIsNone(c["rooms"])             # cards show bedrooms only
        self.assertEqual(c["city"], "Скопје")
        self.assertEqual(c["district"], "Карпош")  # Тафталиџе 1
        self.assertEqual(c["title"], "Се издава Стан во Тафталиџе 1")
        self.assertEqual(c["image"], "https://novelestate.com/images/estate/28037/17.jpg")
        self.assertFalse(c["promoted"])

    def test_districts(self):
        by_id = {c["source_id"]: c for c in self.cards}
        self.assertEqual(by_id["A15169"]["district"], "Скопје Центар")   # Центар
        self.assertEqual(by_id["A16177"]["district"], "Скопје Центар")   # Дебар Маало
        self.assertEqual(by_id["A16182"]["district"], "Гази Баба")       # Автокоманда
        self.assertEqual(by_id["A16181"]["district"], "Аеродром")
        self.assertEqual(by_id["A14294"]["district"], "Кисела Вода")
        self.assertTrue(all(c["deal"] == "rent" for c in self.cards))

    def test_last_page(self):
        self.assertEqual(novel.last_page(self.html), 95)

    def test_search_url(self):
        self.assertEqual(novel.search_url("rent", "apartment", 2),
                         "https://novelestate.com/estate_list.html?serviceType=renting&estateType=apartment&page=2")
        self.assertIn("serviceType=selling&estateType=lot&page=1", novel.search_url("sale", "land", 1))

    def test_place(self):
        self.assertEqual(novel.place("Маврово"), ("Маврово", None))
        self.assertEqual(novel.place("Бардовци"), ("Скопје", "Карпош"))
        self.assertEqual(novel.place("Црниче"), ("Скопје", None))


class NovelDetailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = novel.parse_detail(fixture("novel_detail.html"))

    def test_keys_match_reklama5(self):
        r5 = reklama5.parse_detail(fixture("detail_apartment.html"))
        self.assertEqual(set(self.d), set(r5))

    def test_values(self):
        d = self.d
        self.assertEqual(d["title"], "Се издава Стан во Тафталиџе 1")
        self.assertEqual(d["price_eur"], 800)
        self.assertEqual(d["area_m2"], 65)
        self.assertEqual(d["rooms"], 3)
        self.assertEqual(d["district"], "Карпош")
        self.assertEqual(d["city"], "Скопје")
        self.assertTrue(d["agency"])
        self.assertIsNone(d["lat"])
        self.assertIn("наместен стан", d["description"])

    def test_fields(self):
        f = self.d["fields"]
        self.assertEqual(f["Ентериер"], "Наместен")
        self.assertEqual(f["Опрема"], "Наместен")   # alias read by run.detail_update
        self.assertEqual(f["Кат"], "2")
        self.assertEqual(f["Депозит"], "800 €")
        self.assertEqual(f["Број на спални"], "2")
        self.assertEqual(f["Клима"], "да")
        self.assertEqual(f["Последна промена"], "21/09/2026")

    def test_fixture_has_no_contacts(self):
        html = fixture("novel_detail.html") + fixture("novel_list.html")
        self.assertNotRegex(html, r"\+389|@novelestate")


if __name__ == "__main__":
    unittest.main()
