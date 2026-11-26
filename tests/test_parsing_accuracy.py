"""Parsing mistakes found by auditing the top-rated deals against their ad text
(Macedonian Cyrillic and Latin transliteration). No network, no real data."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scraper.db import DB
from scraper.run import detail_update, land_area, reconcile_text_price, renormalise
from scraper.text import (altitude_in_text, classify_deal, detect_utilities, land_type, parse_area,
                          price_by_agreement, price_in_text, price_per_m2_in_text, redact)


def U(e, w, r):
    return {"electricity": e, "water": w, "road": r}


class Utilities(unittest.TestCase):
    def test_hedged_mentions_are_not_connections(self):
        cases = [
            ("853 m2 ,ima struja I priprema za voda I pristapen pat", U(True, None, True)),
            ("Ima struja i priprema za voda", U(True, None, None)),
            ("na 1.1km od glaven pat, edinstven pristap so terensko vozilo ili traktor", U(None, None, None)),
            ("Placot e na dobra lokacija ,50m od glaven pat.", U(None, None, None)),
            ("Плацот е на 50 м од главен пат.", U(None, None, None)),
            ("плац во населено место одвоено од главен пат на 500тина метри", U(None, None, None)),
            ("na 30m od mestoto ima voda i struja i mozi da se prikluci", U(None, None, None)),
            ("Плацот има маркица за градба,спроведливост на вода,струја,трасиран пат.", U(None, None, None)),
            ("до плацот има изградени куќи; струја, вода, канализација во близина.", U(None, None, None)),
            ("важи до обезбедување на пристап/пат за кој во моментов преговараме", U(None, None, None)),
            ("на почетокот на нивата е проектирано улица. во близина има струја и вода", U(None, None, None)),
            ("Vo procedura e za struja i voda do samiot plac", U(None, None, None)),
            ("СПОРЕД ДУП, АСФАЛТИРАН ПАТ, ВОДА И КАНАЛИЗАЦИОНА МРЕЖА ЌЕ ИМА ВЕДНАШ ДО НИВАТА", U(None, None, None)),
            ("Има земјен пат до плацот", U(None, None, None)),
            ("pristap so makadamski pat", U(None, None, None)),
        ]
        for text, want in cases:
            with self.subTest(text=text):
                self.assertEqual(detect_utilities(text), want)

    def test_confirmed_mentions_still_count(self):
        cases = [
            ("Плац со струја, вода и асфалтен пат", U(True, True, True)),
            ("Има: струја, вода, асфалтен пат.", U(True, True, True)),
            ("So voda i struja, prilaz do placot.", U(True, True, True)),
            ("Na samo 10 km od Skopje so pristapen pat struja i voda.", U(True, True, True)),
            ("Se prodava plac pat, voda i struja veke obezbedeni.", U(True, True, True)),
            ("Od glavniot pat placot e oddalechen okolu trista metri.Do placot ima asvaltiran pat,voda,struja.",
             U(True, True, True)),
            ("-Има потребна инфраструктура\n-Дополнително", U(True, True, True)),
            ("струја има, вода во близина", U(True, None, None)),
            ("Има пристап асвалт, струја,вода со можност за градба.", U(True, True, True)),
            ("Заграден, чист имотен лист, струја, цевка со пумпа за вода, блиску има приклучок за вода и канализација, асфалтен пат, во градежна зона.",
             U(True, True, True)),
        ]
        for text, want in cases:
            with self.subTest(text=text):
                self.assertEqual(detect_utilities(text), want)

    def test_list_hedge_stops_at_the_sentence_end(self):
        self.assertEqual(detect_utilities("Обезбеден асвалтен пат,струја,вода.Во постапка е добивање на маркица"),
                         U(True, True, True))

    def test_infrastructure_heading_is_not_all_utilities(self):
        text = ("Инфраструктура: Локацијата е во градежна зона. Важечкиот урбанистички план "
                "предвидува станбена улица веднаш до самата парцела.")
        self.assertEqual(detect_utilities(text), U(None, None, None))

    def test_place_names_and_verbs_are_not_water(self):
        self.assertIsNone(detect_utilities("Prodavam niva vo Gorno Sonje vo mesto vikano Gorna voda")["water"])
        self.assertIsNone(detect_utilities("Се продава куќа на Смрдлива Вода, на два спрата")["water"])
        self.assertIsNone(detect_utilities("до неа водат два пата од различни страни")["water"])
        self.assertTrue(detect_utilities("ima voda i struja")["water"])


class Prices(unittest.TestCase):
    def test_rates_are_not_totals(self):
        self.assertEqual(price_in_text("цена: 2250 евра/м2 (во цената)\nВкупна цена: 245.250 евра"), 245250)
        self.assertIsNone(price_in_text("Cena 1.900e,m2"))
        self.assertIsNone(price_in_text("ЦЕНА НА ПОДЗЕМЕН ПАРКИНГ - 15.000 ЕВРА"))
        self.assertEqual(price_in_text("цена 45 илјади евра"), 45000)
        self.assertEqual(price_in_text("Cena 45k evra"), 45000)

    def test_price_per_m2(self):
        self.assertEqual(price_per_m2_in_text("Cena 1.900e,m2"), 1900)
        self.assertEqual(price_per_m2_in_text("cena 40 evra kvadrat gradezno zemjiste, 1199 kvadrati"), 40)
        self.assertEqual(price_per_m2_in_text("Цена: 46.950 € (75 € / м² - фиксна)"), 75)
        self.assertEqual(price_per_m2_in_text("*CENA 17 eur м2"), 17)
        self.assertEqual(price_per_m2_in_text("Цената е 4,5 евра/м2"), 4.5)
        self.assertEqual(price_per_m2_in_text("cena 200e/m2"), 200)
        self.assertAlmostEqual(price_per_m2_in_text("110 den po m2"), 110 / 61.5, places=2)
        self.assertIsNone(price_per_m2_in_text("1199 kvadrati"))
        # Two plots, two prices: which rate is which plot's is a guess.
        self.assertIsNone(price_per_m2_in_text("Преден плац 2483 м2 - цена 55 евра Заден плац 1194 м2 - 35 евра/м2",
                                               unique=True))

    def test_flat_rate_typed_as_price_in_text(self):
        # Field 74,000 € for 73 m², the ad says 1,400 €/m² → 102,200 €, not a 28% bargain.
        self.assertEqual(reconcile_text_price("apartment", 74000, None, 73, "STAN VO HIPODROM CENA 1400E SO DDV",
                                              "CENA 1400 EVRA SO VKLUCENO DDV."), (1400, "per_m2"))
        # A rate the old parser stored as a €1,900 total (and called it a rent).
        self.assertEqual(reconcile_text_price("apartment", 1900, "from_text", 58, "NOV DVOSOBEN STAN",
                                              "prodava stan. Cena 1.900e,m2"), (1900, "per_m2"))
        # Field total consistent with the stated rate: left alone.
        self.assertEqual(reconcile_text_price("apartment", 97650, None, 63, "Стан", "Цена: 1550 евра/м2"),
                         (97650, None))
        # "цена за паркинг 5000" is not the flat's rate.
        self.assertEqual(reconcile_text_price("apartment", 100000, None, 60, "Стан", "цена на паркинг 5000 евра"),
                         (100000, None))

    def test_rent_with_denars_in_text(self):
        self.assertEqual(reconcile_text_price("apartment", None, "placeholder", 56, "Стан центар",
                                              "Се изнајмува наместен стан. Цена 20000 плус режиски"),
                         (325, "mkd_converted"))

    def test_price_by_agreement_placeholder(self):
        desc = "Se prodava plac 2000 m2 so moznost za priklucok na voda i struja. Cena po dogovor."
        self.assertTrue(price_by_agreement(desc))
        self.assertEqual(reconcile_text_price("land", 5, "per_m2", 2000, "Se prodava niva 2000 m2", desc),
                         (None, "placeholder"))
        # A real figure with "по договор" stays.
        self.assertEqual(reconcile_text_price("land", 25000, None, 1906, "Плац", "💰 Цена по договор"),
                         (25000, None))

    def test_wanted_in_description(self):
        self.assertEqual(classify_deal("Stan so pogled na Vardar",
                                       "Kupuvam stan so pogled ba VARDAR. Cena do 900 evra za kvadrat", 900,
                                       "apartment"), "wanted")
        self.assertEqual(classify_deal("Стан", "Се продава стан, купувачот плаќа данок", 90000, "apartment"),
                         "sale")


class Areas(unittest.TestCase):
    def test_dekari(self):
        self.assertEqual(parse_area("plac 1,5 декари"), 1500)
        self.assertEqual(parse_area("3 dekari niva"), 3000)
        self.assertEqual(parse_area("5 ари"), 500)

    def test_placeholder_plot_size(self):
        self.assertEqual(land_area(999, "Se prodava plac 1.5 - 2 dekari - Leniste", ""), 2000)
        self.assertEqual(land_area(25, "NIVA", "niva zemjodelska vo Ilinden 3047 m2 25€ kvadrat"), 3047)
        self.assertIsNone(land_area(10, "Se prodava plac vo s. Rastak", "povekje info na tel."))
        self.assertEqual(land_area(1000, "Plac", "1000m2"), 1000)


class LandAndAltitude(unittest.TestCase):
    def test_zoning_words(self):
        self.assertEqual(land_type("Останато", "Се наоѓа во А-зона наменета за домување."), "building")
        self.assertEqual(land_type(None, "parcela e gradezno neizgradeno zemjiste, A 1 za domuvanje"), "building")
        self.assertEqual(land_type("Останато", "Продавам НИВА-ПЛАЦ, градежна парцела"), "agricultural")

    def test_altitude_spellings(self):
        self.assertEqual(altitude_in_text("на источна страна на надморска висона од 1.500 метри"), 1500)
        self.assertEqual(altitude_in_text("se naogja na nadmorska visocina od 500m"), 500)
        self.assertEqual(altitude_in_text("Вилата се наоѓа на 1300 мнв во чиста природа"), 1300)
        self.assertEqual(altitude_in_text("Pelister, 830 m n.v\n"), 830)
        self.assertEqual(altitude_in_text("1300 м.н.в."), 1300)
        self.assertIsNone(altitude_in_text("800 m2 investicija"))


class Privacy(unittest.TestCase):
    def test_foreign_phone_numbers(self):
        self.assertEqual(redact("Маријан – WhatsApp: +49 160 93986986"), "Маријан – WhatsApp: [тел.]")
        self.assertEqual(redact("0049 160 93986986"), "[тел.]")
        self.assertEqual(redact("k.p.162/13 1199 kvadrati, 20000 evra"), "k.p.162/13 1199 kvadrati, 20000 evra")


class StoredAdsAreReread(unittest.TestCase):
    """renormalise() brings old ads up to the current rules, and a card refresh
    doesn't undo a correction taken from the ad text."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def _card(self, sid, price, kind="apartment", area=73.0, title="STAN VO HIPODROM CENA 1400E SO DDV"):
        return {"source": "reklama5", "source_id": sid, "url": f"https://m.reklama5.mk/AdDetails?ad={sid}",
                "cat": 1, "kind": kind, "deal": "sale", "title": title, "price_eur": price, "price_note": None,
                "area_m2": area, "rooms": None, "city": "Скопје", "district": None, "image": None,
                "promoted": False, "posted": "2026-11-20"}

    def test_renormalise_and_card_refresh(self):
        db = self.db
        db.upsert_card(self._card("1", 74000.0), "2026-11-20T06:00:00")
        db.apply_detail("reklama5:1", {"description": "CENA 1400 EVRA SO VKLUCENO DDV.",
                                       "utilities": U(None, None, None), "fields": {}}, "2026-11-20T06:10:00")
        db.upsert_card(self._card("2", 5.0, kind="land", area=2000.0, title="Se prodava niva 2000 m2"),
                       "2026-11-20T06:00:00")
        db.conn.execute("UPDATE listings SET price_note='per_m2' WHERE id='reklama5:2'")
        db.apply_detail("reklama5:2", {"description": "Cena po dogovor. ima voda, struja i asfalten pat. "
                                                      "Inframstruktura: planirana.",
                                       "utilities": U(True, True, True), "land_type": "unknown",
                                       "fields": {"Тип на земјиште": "Останато"}}, "2026-11-20T06:10:00")
        db.conn.execute("UPDATE listings SET utilities=? WHERE id='reklama5:2'",
                        ('{"electricity": null, "water": null, "road": null}',))
        db.conn.commit()
        stats = renormalise(db, {"market": {"mkd_per_eur": 61.5}})
        self.assertEqual(stats["price"], 2)
        r1 = db.conn.execute("SELECT price_eur, price_note FROM listings WHERE id='reklama5:1'").fetchone()
        self.assertEqual(tuple(r1), (1400, "per_m2"))
        r2 = db.conn.execute("SELECT price_eur, price_note, utilities FROM listings WHERE id='reklama5:2'").fetchone()
        self.assertEqual((r2[0], r2[1]), (None, "placeholder"))
        self.assertIn('"water": true', r2[2])
        # Next run: the search card still shows the same raw figures — the corrections stay.
        _, changed = db.upsert_card(self._card("1", 74000.0), "2026-11-20T18:00:00")
        self.assertFalse(changed)
        db.upsert_card(self._card("2", 5.0, kind="land", area=2000.0, title="Se prodava niva 2000 m2"),
                       "2026-11-20T18:00:00")
        self.assertEqual(tuple(db.conn.execute(
            "SELECT price_eur, price_note FROM listings WHERE id='reklama5:1'").fetchone()), (1400, "per_m2"))
        self.assertEqual(tuple(db.conn.execute(
            "SELECT price_eur, price_note FROM listings WHERE id='reklama5:2'").fetchone()), (None, "placeholder"))
        # The seller really changes the price: the card wins, with the card's own note.
        _, changed = db.upsert_card(self._card("1", 80000.0), "2026-11-21T06:00:00")
        self.assertTrue(changed)
        self.assertEqual(tuple(db.conn.execute(
            "SELECT price_eur, price_note FROM listings WHERE id='reklama5:1'").fetchone()), (80000, None))

    def test_reread_without_pin_keeps_the_known_pin(self):
        db = self.db
        db.upsert_card(self._card("3", 50000.0, kind="land", area=800.0, title="Плац"), "2026-11-20T06:00:00")
        listing = {"kind": "land", "title": "Плац", "price_eur": 50000.0, "area_m2": 800.0}
        page = {"title": "Плац", "description": "плац 800 m2", "fields": {}, "area_m2": 800.0, "price_eur": 50000.0}
        db.apply_detail("reklama5:3", detail_update(listing, {**page, "lat": 41.95, "lng": 21.3}), "2026-11-20T06:10:00")
        db.conn.execute("UPDATE listings SET elevation=640, elevation_src='gps' WHERE id='reklama5:3'")
        # A week later the page is re-read and the map is gone.
        upd = detail_update(listing, {**page, "lat": None, "lng": None})
        self.assertNotIn("lat", upd)
        db.apply_detail("reklama5:3", upd, "2026-11-27T06:10:00")
        row = db.conn.execute("SELECT lat, lng, elevation_src FROM listings WHERE id='reklama5:3'").fetchone()
        self.assertEqual(tuple(row), (41.95, 21.3, "gps"))
        # A page with a (new) pin still replaces it.
        self.assertEqual(detail_update(listing, {**page, "lat": 41.9, "lng": 21.4})["lat"], 41.9)

    def test_detail_update_uses_text(self):
        listing = {"kind": "land", "title": "Se prodava plac 1.5 - 2 dekari - Leniste", "price_eur": 16.0,
                   "area_m2": 999.0}
        d = {"title": listing["title"], "description": "pat, voda i struja veke obezbedeni.",
             "fields": {"Квадратура": "999 m²", "Тип на земјиште": "Градежно"}, "area_m2": 999.0, "price_eur": 16.0}
        u = detail_update(listing, d)
        self.assertEqual(u["area_m2"], 2000)
        self.assertEqual((u["price_eur"], u["price_note"]), (16.0, "per_m2"))


if __name__ == "__main__":
    unittest.main()
