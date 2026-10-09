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
        # Every API test works on temporary files — never the owner's real feedback.db / private.json.
        self.patches = [mock.patch.object(feedback, "DB_PATH", self.path),
                        mock.patch.object(web, "PRIVATE_JSON", Path(self.tmp.name) / "private.json"),
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


class PipelineTests(ApiTests):
    def test_interested_moves_deal_to_pipeline_and_out_of_lists(self):
        web.vote({"id": "reklama5:10", "researcher": "land", "vote": "up"})
        results = {"land": [{"id": "reklama5:10", "title": "Plac A"}, {"id": "reklama5:11", "title": "Plac B"}]}
        lists, pipe = feedback.take_pipeline(results)
        self.assertEqual([x["id"] for x in lists["land"]], ["reklama5:11"])
        self.assertEqual((pipe[0]["stage"], pipe[0]["next_step"], pipe[0]["removed"]),
                         ("interested", "Call the seller / agency", False))
        # The ad disappears: the saved copy stays, flagged removed.
        lists, pipe = feedback.take_pipeline({"land": [{"id": "reklama5:11"}]})
        self.assertEqual((pipe[0]["item"]["title"], pipe[0]["removed"]), ("Plac A", True))

    def test_stage_moves_bring_their_next_step(self):
        web.vote({"id": "kirsm:5", "researcher": "auctions", "vote": "ok"})
        self.assertTrue(web.pipeline({"id": "kirsm:5", "stage": "checks", "due": "2026-10-20", "notes": "bailiff: Mon"})["ok"])
        e = feedback.load_pipeline()[0]
        self.assertEqual((e["stage"], e["due"], e["notes"]), ("checks", "2026-10-20", "bailiff: Mon"))
        self.assertIn("occupied", e["next_step"])                    # the auction-specific step
        self.assertFalse(web.pipeline({"id": "kirsm:5", "stage": "bought it"})["ok"])
        self.assertFalse(web.pipeline({"id": "kirsm:5", "due": "next week"})["ok"])

    def test_reject_or_undo_leaves_pipeline(self):
        web.vote({"id": "reklama5:12", "researcher": "flip", "vote": "up"})
        web.vote({"id": "reklama5:12", "researcher": "flip", "vote": "down", "reason": "prc"})
        self.assertEqual(feedback.load_pipeline(), [])
        web.vote({"id": "reklama5:13", "researcher": "flip", "vote": "up"})
        web.vote({"id": "reklama5:13", "researcher": "flip", "vote": "clear"})
        self.assertEqual(feedback.load_pipeline(), [])


class InstantReviewTests(ApiTests):
    """A vote must move the deal in private.json at once — not after the next re-score."""

    def setUp(self):
        super().setUp()
        self.pj = Path(self.tmp.name) / "private.json"
        card = lambda i, s: {"id": i, "score": s, "rating": {"rank": 3}, "qualified": True, "title": i}
        self.pj.write_text(json.dumps({"researchers": [{"key": "land", "items": [card("reklama5:1", 80), card("reklama5:2", 60)],
                                                        "count": 2, "qualified": 2}],
                                       "pipeline": [], "rejected": []}), encoding="utf-8")

    def read(self):
        return json.loads(self.pj.read_text(encoding="utf-8"))

    def test_interested_moves_to_pipeline_immediately(self):
        web.vote({"id": "reklama5:1", "researcher": "land", "vote": "up"})
        d = self.read()
        self.assertEqual([x["id"] for x in d["researchers"][0]["items"]], ["reklama5:2"])
        self.assertEqual((d["pipeline"][0]["id"], d["pipeline"][0]["stage"], d["pipeline"][0]["item"]["title"]),
                         ("reklama5:1", "interested", "reklama5:1"))

    def test_reject_archives_and_undo_restores(self):
        web.vote({"id": "reklama5:2", "researcher": "land", "vote": "down", "reason": "sml"})
        d = self.read()
        self.assertEqual([x["id"] for x in d["researchers"][0]["items"]], ["reklama5:1"])
        self.assertEqual((d["rejected"][0]["id"], d["rejected"][0]["reason_label"], d["rejected"][0]["learned"]),
                         ("reklama5:2", "Too small", "smaller deals now rank lower"))
        web.vote({"id": "reklama5:2", "researcher": "land", "vote": "clear"})
        d = self.read()
        self.assertEqual(sorted(x["id"] for x in d["researchers"][0]["items"]), ["reklama5:1", "reklama5:2"])
        self.assertEqual(d["rejected"], [])
