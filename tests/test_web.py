"""Private dashboard API (rating on the platform).

Run:  python -m unittest discover tests
"""
import json
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from scraper import feedback, web


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "feedback.db"
        self.patches = [mock.patch.object(feedback, "DB_PATH", self.path),
                        mock.patch.object(web, "_listing", lambda lid: {"kind": "land", "city": "Охрид",
                                                                         "district": None, "title": "Plac", "area_m2": 600,
                                                                         "price_eur": 30000})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_reject_with_reason_then_undo(self):
        self.assertTrue(web.vote({"id": "reklama5:1", "researcher": "land", "vote": "down", "reason": "agr",
                                  "note": "seller said it's a field"})["ok"])
        rj = feedback.load_rejections()
        self.assertEqual((rj[0]["reason"], rj[0]["note"]), ("agr", "seller said it's a field"))
        self.assertEqual(feedback.rejected_list()[0]["reason_label"], "Agricultural — can't build")
        web.vote({"id": "reklama5:1", "researcher": "land", "vote": "clear"})
        self.assertEqual(feedback.load_rejections(), [])
        self.assertEqual(feedback.load_votes(), [])

    def test_confirm_replaces_earlier_rejection(self):
        web.vote({"id": "reklama5:2", "researcher": "land", "vote": "down", "reason": "loc"})
        web.vote({"id": "reklama5:2", "researcher": "land", "vote": "ok"})
        facts, confirmed = feedback.load_overrides()
        self.assertIn("reklama5:2", confirmed)
        self.assertEqual(feedback.load_rejections(), [])
        self.assertEqual([v["vote"] for v in feedback.load_votes()], [1])

    def test_facts_validation_and_storage(self):
        self.assertFalse(web.facts({"id": "reklama5:3", "facts": {"area_m2": "abc"}})["ok"])
        self.assertTrue(web.facts({"id": "reklama5:3", "facts": {"water": "yes", "road": "no", "area_m2": "450",
                                                                  "land_type": "building", "hack": "x"}})["ok"])
        facts, _ = feedback.load_overrides()
        self.assertEqual(facts["reklama5:3"], {"water": "yes", "road": "no", "area_m2": "450.0", "land_type": "building"})
        web.facts({"id": "reklama5:3", "facts": {"water": "unknown"}})
        self.assertNotIn("water", feedback.load_overrides()[0]["reklama5:3"])

    def test_bad_requests(self):
        self.assertFalse(web.vote({"id": "x", "researcher": "nope", "vote": "up"})["ok"])
        self.assertFalse(web.vote({"id": "x", "researcher": "land", "vote": "down", "reason": "zzz"})["ok"])

    def test_my_annotations(self):
        web.vote({"id": "reklama5:4", "researcher": "land", "vote": "up"})
        web.facts({"id": "reklama5:4", "facts": {"water": "yes", "price_eur": 25000}})
        out = feedback.annotate_mine({"land": [{"id": "reklama5:4"}, {"id": "reklama5:5"}]})["land"]
        self.assertEqual(out[0]["my_vote"], "up")
        self.assertEqual(out[0]["facts"], {"water": "yes", "price_eur": 25000.0})
        self.assertIsNone(out[1]["my_vote"])


class HttpTests(unittest.TestCase):
    """The real server: ping works; POSTs from other websites are refused."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = web.serve(8813)

    @classmethod
    def tearDownClass(cls):
        if cls.httpd:
            cls.httpd.shutdown()
            cls.httpd.server_close()
            for s in getattr(cls.httpd, "extra_servers", []):
                s.shutdown()
                s.server_close()

    def req(self, path, body=None, origin=None):
        r = urllib.request.Request(f"http://127.0.0.1:8813{path}", data=json.dumps(body).encode() if body else None,
                                   headers={"Content-Type": "application/json", **({"Origin": origin} if origin else {})})
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_ping_and_reasons(self):
        self.assertEqual(self.req("/api/ping"), (200, {"ok": True, "private": True}))
        code, data = self.req("/api/reasons")
        self.assertIn("land", data)
        self.assertTrue(any(r["code"] == "agr" for r in data["land"]))

    def test_cross_site_post_refused(self):
        code, data = self.req("/api/vote", {"id": "x", "researcher": "land", "vote": "up"}, origin="https://evil.example")
        self.assertEqual(code, 403)


if __name__ == "__main__":
    unittest.main()
