"""Stopanska banka bank-owned property list — run:  python -m unittest discover tests"""
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from scraper.sources import reklama5, stb

FIX = Path(__file__).parent / "fixtures"
TODAY = date(2026, 11, 26)


class StbPageTests(unittest.TestCase):
    def test_xlsx_link_is_found_and_encoded(self):
        url = stb.xlsx_url((FIX / "stb_page.html").read_text(encoding="utf-8"))
        self.assertTrue(url.startswith("https://www.stb.com.mk/media/edcjfx2c/%D0%BE%D0%B1"))
        self.assertTrue(url.endswith("-31102026.xlsx"))
        self.assertNotIn("api/property-sales", url)

    def test_no_link(self):
        self.assertIsNone(stb.xlsx_url("<html><a href='/x.pdf'>Образец ИП</a></html>"))


class StbSheetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = (FIX / "stb_ip.xlsx").read_bytes()
        cls.items = stb.parse_xlsx(cls.data, today=TODAY)
        cls.by_sheet = {i["extra"]["sheet"]: i for i in cls.items}

    def test_count_and_keys(self):
        self.assertEqual(len(self.items), 17)
        r5 = reklama5.parse_list((FIX / "list_apartments.html").read_text(encoding="utf-8"), 159)
        card_keys = set(r5[0]) | {"deal"}
        self.assertTrue(card_keys <= set(self.items[0]), card_keys - set(self.items[0]))

    def test_first_row(self):
        i = self.items[0]
        self.assertEqual(i["source"], "stb")
        self.assertEqual(i["source_id"], "593-srbica-von-grad")
        self.assertEqual(i["kind"], "land")
        self.assertEqual(i["deal"], "sale")
        self.assertEqual(i["price_eur"], 2380)
        self.assertEqual(i["area_m2"], 1008)
        self.assertEqual(i["city"], "Србица")
        self.assertEqual(i["url"], stb.PAGE_URL)
        self.assertEqual(i["extra"]["report_date"], "2026-10-31")

    def test_kinds(self):
        kinds = {s: self.by_sheet[s]["kind"] for s in ("1608", "26631", "100057", "574, 1735", "1157")}
        self.assertEqual(kinds, {"1608": "apartment",      # спрат од куќа
                                 "26631": "commercial",    # дуќан
                                 "100057": "house",
                                 "574, 1735": "commercial",  # индустриски објект со земјиште
                                 "1157": "other"})         # фарма

    def test_prices_and_areas(self):
        big = self.by_sheet["192"]
        self.assertEqual(big["price_eur"], 1910000)
        self.assertEqual(big["area_m2"], 3136)
        self.assertEqual(big["fields"]["Земјиште"], "31905 м²")

    def test_places(self):
        butel = self.by_sheet["100057"]
        self.assertEqual((butel["city"], butel["district"]), ("Скопје", "Бутел"))
        self.assertEqual(self.by_sheet["12300"]["city"], "Битола")       # "KO Битола 3"
        self.assertEqual(self.by_sheet["26322"]["city"], "Битола")       # "КО 1-2 Битола"
        self.assertEqual(self.by_sheet["1157"]["city"], "Кочани")        # town named after the KO

    def test_flags(self):
        self.assertTrue(self.by_sheet["623"]["extra"]["co_owned"])
        self.assertIn("Сосопственост", self.by_sheet["623"]["description"])
        self.assertTrue(self.by_sheet["70161, 60433"]["extra"]["encumbered"])
        self.assertIn("товари", self.by_sheet["70161, 60433"]["description"])
        self.assertFalse(self.by_sheet["593"]["extra"]["encumbered"])

    def test_source_ids_unique(self):
        ids = [i["source_id"] for i in self.items]
        self.assertEqual(len(ids), len(set(ids)))

    def test_contact_column_never_read(self):
        rows = stb.read_rows(self.data)
        self.assertFalse(any("J" in r for r in rows))
        from scraper.text import redact
        for i in self.items:
            blob = repr(i)
            self.assertNotIn("контакт", blob.lower())
            self.assertEqual(redact(blob), blob)          # no phone numbers or e-mails anywhere
            self.assertNotIn("@", blob)

    def test_freshness(self):
        self.assertEqual(self.items[0]["renewed"], "2026-11-26")      # report is current
        old = stb.parse_xlsx(self.data, today=date(2027, 3, 1))
        self.assertEqual(old[0]["renewed"], "2026-10-31")            # stale report ages out

    def test_detail_fields(self):
        d = stb.detail_fields(self.by_sheet["593"])
        self.assertEqual(d["deal"], "sale")
        self.assertEqual(d["agency"], 0)
        self.assertEqual(set(d["utilities"]), {"electricity", "water", "road"})
        self.assertIn(d["land_type"], ("building", "agricultural", "unknown"))
        self.assertIsNone(stb.detail_fields(self.by_sheet["100057"])["land_type"])

    def test_helpers(self):
        self.assertEqual(stb.parse_price("1.910.000ЕУР"), 1910000)
        self.assertEqual(stb.parse_price("по договор"), None)
        self.assertEqual(stb.parse_areas("5.322м2+6.553м2"), [5322, 6553])
        self.assertEqual(stb.kind_of("станбена куќа"), "house")
        self.assertEqual(stb.kind_of("стан"), "apartment")


class StbStoreTests(unittest.TestCase):
    """The run step stores bank prices as given (a €1,000 shop is not €/m²)."""

    def test_scrape_stb_into_temp_db(self):
        from scraper import run
        from scraper.db import DB

        page = (FIX / "stb_page.html").read_text(encoding="utf-8")
        data = (FIX / "stb_ip.xlsx").read_bytes()
        http = mock.Mock()
        http.get.return_value = page
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(stb, "fetch_bytes", return_value=data) as fb:
            db = DB(str(Path(tmp) / "t.db"))
            stats = {"pages": 0, "new": 0, "changed": 0}
            run.scrape_stb(db, http, {"stb": {"enabled": True}}, stats)
            self.assertEqual(stats, {"pages": 2, "new": 17, "changed": 0})
            fb.assert_called_once()
            http.get.assert_called_once_with(stb.PAGE_URL)
            row = db.conn.execute("SELECT price_eur, price_note, deal, detail_at, extra FROM listings "
                                  "WHERE id='stb:26631-veles'").fetchone()
            self.assertEqual((row[0], row[1], row[2]), (1000, None, "sale"))
            self.assertIsNotNone(row[3])
            self.assertIn("report_date", row[4])
            run.scrape_stb(db, http, {"stb": {"enabled": True}}, stats)     # second run: nothing new
            self.assertEqual(stats["new"], 17)
            db.conn.close()

    def test_disabled(self):
        from scraper import run
        http = mock.Mock()
        run.scrape_stb(None, http, {"stb": {"enabled": False}}, {"pages": 0})
        http.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
