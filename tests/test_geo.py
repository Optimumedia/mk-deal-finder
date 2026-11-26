"""Location, altitude and distance honesty (scraper/geo.py, scraper/maps.py). No network, temp DBs only."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scraper import geo, maps
from scraper.db import DB

CFG = {"geo": {"enabled": True, "max_lookups_per_run": 50, "place_vs_city_km": 40},
       "land": {"mountain_keywords": ["маврово", "mavrovo"]}, "scraper": {"stale_after_days": 10}}


def levels(listing):
    return geo.ranked_candidates({"extra": {}} | listing)


class CandidateTests(unittest.TestCase):
    def test_village_named_in_description(self):
        c = levels({"title": "Se prodavaat plac i Nivi", "city": "Велес",
                    "description": "SE PRODAVAAT NIVI I SUMI VO S. NEZILOVO (VELESKO)"})
        self.assertEqual(c[0], ("Незилово", "village"))
        self.assertEqual(c[-1], ("Велес", "city"))

    def test_two_word_villages(self):
        self.assertEqual(levels({"title": "Продавам две ниви во Долно Оризари - Битола", "city": "Битола"})[0],
                         ("Долно Оризари", "village"))
        self.assertEqual(levels({"title": "Dve parceli vo Nov Dojran", "city": "Гевгелија"})[0],
                         ("Нов Дојран", "village"))

    def test_weekend_settlement_and_all_title_markers(self):
        c = levels({"title": "Prodavam Plac Vikend Naselba Golievo Skopska Crna Gora", "city": "Скопје",
                    "district": "Чучер Сандево"})
        self.assertEqual(c[0], ("Голиево", "village"))
        # Not just the first "vo …": "vo okolina" is skipped, "na lisolaj" is found.
        self.assertEqual(levels({"title": "Se prodava niva vo okolina na lisolaj", "city": "Битола"})[0][0], "Лисолај")

    def test_towns_districts_and_noise_are_not_villages(self):
        c = dict(levels({"title": "Атрактивна нива веднаш до автопат Велес-Скопје", "city": "Скопје",
                         "district": "Ѓорче Петров"}))
        self.assertEqual(c["Велес"], "town")
        self.assertEqual(c["Ѓорче Петров"], "area")
        c = dict(levels({"title": "Se prodava plac vo Gorce Petrov na glavna ulica", "city": "Скопје",
                         "district": "Ѓорче Петров"}))
        self.assertNotIn("Петров", c)                   # never "Petrovec"
        self.assertNotIn("Главна", c)
        self.assertEqual(levels({"title": "Plac vo Centar", "city": "Скопје", "district": "Скопје Центар"})[0],
                         ("Центар", "area"))
        c = dict(levels({"title": "SE PRODAVAAT DVA PLACEVI VO OPSTINA ILINDEN AJVATOVCI", "city": "Скопје"}))
        self.assertEqual((c["Илинден"], c["Ајватовци"]), ("area", "village"))

    def test_same_name(self):
        self.assertTrue(geo._same_name("Мрзенци", "Mrzenci"))
        self.assertTrue(geo._same_name("Мирковци", "Mirkovce"))
        self.assertTrue(geo._same_name("Волково", "Vuči Dol"))        # geocoder's name for it
        self.assertFalse(geo._same_name("Долно", "Долно Соње"))
        self.assertFalse(geo._same_name("Петров", "Петровец"))


class GeoDBTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "t.db")
        self.g = geo.Geo(self.db, delay=0)

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def add(self, i, title, city="Скопје", district=None, desc="", **kw):
        now = datetime.now(timezone.utc).isoformat()
        cols = {"id": f"t:{i}", "source": "t", "source_id": str(i), "url": "u", "kind": "land", "deal": "sale",
                "title": title, "description": desc, "city": city, "district": district, "abroad": 0,
                "first_seen": now, "last_seen": now} | kw
        self.db.conn.execute(f"INSERT INTO listings ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                             list(cols.values()))

    def row(self, i):
        return self.db.conn.execute("SELECT * FROM listings WHERE id=?", (f"t:{i}",)).fetchone()


class NearestNamesakeTests(GeoDBTest):
    def test_namesake_nearest_the_ads_town_wins(self):
        two = {"results": [{"name": "Волково", "feature_code": "PPL", "elevation": 776, "latitude": 41.30, "longitude": 21.57},
                           {"name": "Vuči Dol", "feature_code": "PPL", "elevation": 286, "latitude": 42.045,
                            "longitude": 21.346}]}
        self.g._get = lambda url, params: two
        self.assertEqual(self.g.place("Волково", near=(41.9961, 21.4317))[2], 286)
        self.assertEqual(self.g.place("Волково", near=(41.3464, 21.5542))[2], 776)     # Prilep's Volkovo
        self.assertIs(geo.Geo(self.db).place("Нешто", fetch=False), geo._MISSING)

    def test_far_old_answer_is_looked_up_again_once(self):
        self.db.conn.execute("INSERT INTO geo_cache VALUES ('place:рибарци', 41.02, 21.47, 577, 'Ribarci', 'x')")
        calls = []

        def answer(url, params):
            calls.append(params["name"])
            return {"results": [{"name": "Ribarci", "feature_code": "PPL", "elevation": 577, "latitude": 41.02, "longitude": 21.47},
                                {"name": "Ribarci", "feature_code": "PPL", "elevation": 146, "latitude": 41.509,
                                 "longitude": 21.962}]}
        self.g._get = answer
        kavadarci = (41.4331, 22.0119)
        self.assertEqual(self.g.place("Рибарци", near=kavadarci)[2], 146)
        self.assertEqual(self.g.place("Рибарци", near=kavadarci)[2], 146)
        self.assertEqual(len(calls), 1)


class EnrichHonestyTests(GeoDBTest):
    def run_enrich(self, answers):
        geo_get = lambda self_, url, params: answers.get(params.get("name"), {"results": []})
        orig_get, orig_routes = geo.Geo._get, geo.routes
        geo.Geo._get, geo.routes = geo_get, lambda db, cfg, g=None: 0
        try:
            geo.enrich(self.db, CFG)
        finally:
            geo.Geo._get, geo.routes = orig_get, orig_routes

    def test_city_fallback_claims_no_altitude_and_no_drive_time(self):
        self.add(1, "Prodavam Plac Vikend Naselba Golievo Skopska Crna Gora")      # Golievo isn't in the geocoder
        self.add(2, "Plac vo Patishka Reka", district="Сопиште")
        self.add(3, "Prodavam plac", district="Сопиште")
        sk = {"results": [{"name": "Скопје", "feature_code": "PPLC", "elevation": 247, "latitude": 41.996, "longitude": 21.431}]}
        pr = {"results": [{"name": "Патишка Река", "feature_code": "PPL", "elevation": 957, "latitude": 41.85, "longitude": 21.27}]}
        so = {"results": [{"name": "Сопиште", "feature_code": "PPLA", "elevation": 333, "latitude": 41.95, "longitude": 21.43}]}
        self.run_enrich({"Скопје": sk, "Патишка Река": pr, "Сопиште": so})
        r = self.row(1)
        self.assertEqual((r["elevation"], r["elevation_src"], r["approx_lat"]), (None, "city:Скопје", None))
        r = self.row(2)
        self.assertEqual((r["elevation"], r["elevation_src"]), (957, "place:Патишка Река"))
        r = self.row(3)                                  # municipality only: rough distance, no altitude
        self.assertEqual((r["elevation"], r["elevation_src"], r["approx_lat"]), (None, "area:Сопиште", 41.95))

    def test_lost_pin_altitude_is_forgotten(self):
        self.add(1, "Плац", city="Охрид", elevation=700, elevation_src="gps", skopje_min=170)
        self.add(2, "Плац", city="Охрид", lat=41.1, lng=20.8, elevation=720, elevation_src="gps")
        self.assertEqual(geo.forget_lost_pins(self.db), 1)
        self.assertEqual((self.row(1)["elevation"], self.row(1)["skopje_min"]), (None, None))
        self.assertEqual(self.row(2)["elevation"], 720)

    def test_old_fallbacks_are_redone_once(self):
        self.add(1, "Plac", elevation=247, elevation_src="place:Скопје", approx_lat=41.99, approx_lng=21.43, skopje_min=0)
        self.assertEqual(geo.reset_fallbacks_once(self.db), 1)
        self.assertIsNone(self.row(1)["skopje_min"])
        self.add(2, "Plac", elevation=247, elevation_src="place:Скопје")
        self.assertEqual(geo.reset_fallbacks_once(self.db), 0)


class MapsQueryTests(unittest.TestCase):
    def test_found_village_goes_into_the_map_search(self):
        self.assertEqual(maps.query({"city": "Охрид", "elevation_src": "place:Велгошти"}), "Велгошти, Охрид, North Macedonia")
        self.assertEqual(maps.query({"city": "Охрид", "elevation_src": "city:Охрид"}), "Охрид, North Macedonia")


if __name__ == "__main__":
    unittest.main()
