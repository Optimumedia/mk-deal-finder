"""Dashboard page (docs/index.html): structure the private server relies on, and honest labels.

Run:  python -m unittest discover tests
The behaviour tests run the page's own JavaScript in Node (skipped when Node isn't installed).
Only made-up items are used — never the real data files.
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "docs" / "index.html"
HARNESS = ROOT / "tests" / "fixtures" / "dashboard_harness.js"
HTML = PAGE.read_text(encoding="utf-8")


class StaticTests(unittest.TestCase):
    def test_ids_and_endpoints_private_mode_needs(self):
        for el_id in ("health", "tabs", "main", "drawer", "dBody", "dClose", "dShare", "scrim", "toasts",
                      "rescoreBtn", "privPill", "themeBtn", "rejPanel", "rejReason", "rejNote", "rejGo", "fSave",
                      "factsForm", "pStage", "pDue", "pNext", "pNotes", "pipeDrop", "pipeRemove", "archive"):
            self.assertRegex(HTML, rf'id="{el_id}"|id=\\?"{el_id}', el_id)
        for ep in ("/api/ping", "/api/vote", "/api/facts", "/api/pipeline", "/api/rescore", "/api/reasons", "/private.json"):
            self.assertIn(ep, HTML)

    def test_every_css_variable_is_defined(self):
        defined = set(re.findall(r"(--[\w-]+)\s*:", HTML))
        used = set(re.findall(r"var\((--[\w-]+)\)", HTML))      # var(--x, fallback) is allowed to be undefined
        self.assertEqual(used - defined, set())

    def test_muted_text_meets_wcag_aa_in_light_mode(self):
        root = re.search(r":root\{(.*?)\}", HTML, re.S).group(1)
        tok = dict(re.findall(r"(--[\w-]+):(#[0-9a-fA-F]{6})", root))

        def lum(h):
            c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            c = [x / 12.92 if x <= .03928 else ((x + .055) / 1.055) ** 2.4 for x in c]
            return .2126 * c[0] + .7152 * c[1] + .0722 * c[2]

        def ratio(a, b):
            hi, lo = sorted((lum(a), lum(b)), reverse=True)
            return (hi + .05) / (lo + .05)
        for bg in ("--bg", "--surface", "--surface-2"):
            self.assertGreaterEqual(ratio(tok["--ink-3"], tok[bg]), 4.5, bg)

    def test_no_blanket_local_claim(self):
        # The comparison may be district, city-wide or 700 m+ plots: never call it "local" across the board.
        self.assertFalse("under local asking" in HTML)


@unittest.skipUnless(shutil.which("node"), "Node.js not installed")
class BehaviourTests(unittest.TestCase):
    PLOT = {"id": "t:1", "_r": "land", "kind": "land", "city": "Скопје", "district": "Карпош", "price": 30000, "area": 1000,
            "ppm2": 30, "score": 86, "status": "needs_info", "lat": None, "lng": None, "elevation": 700,
            "elevation_approx": False, "distance_approx": True, "to_ohrid": {"km": 2, "min": 3},
            "utilities": {"electricity": "yes", "water": "unknown", "road": "yes"},
            "questions": ["Is it building land (градежно)?"], "is_new": True,
            "rating": {"tier": "good", "emoji": "👍", "label": "Good deal", "rank": 3},
            "reasons": ["50% below the city median for 800–1,500 m² plots (60 €/m², 166 listings)"],
            "metrics": {"€/m²": 30, "Area median €/m²": 60, "Below market": "50%",
                        "Compared against": "166 plots 800–1,500 m² (city)"}}

    @classmethod
    def run_js(cls, cases):
        p = subprocess.run(["node", str(HARNESS), str(PAGE)], input=json.dumps(cases), capture_output=True,
                           text=True, encoding="utf-8", timeout=60)
        if p.returncode:
            raise AssertionError(p.stderr)
        out = json.loads(p.stdout)
        for k, v in out.items():
            if isinstance(v, dict) and "error" in v:
                raise AssertionError(f"{k}: {v['error']}")
        return out

    def test_cards_say_what_is_approximate_and_unconfirmed(self):
        it = json.dumps(self.PLOT, ensure_ascii=False)
        above = json.dumps(dict(self.PLOT, metrics=dict(self.PLOT["metrics"], **{"Below market": "-20%"})), ensure_ascii=False)
        district = json.dumps(dict(self.PLOT, metrics=dict(self.PLOT["metrics"], **{"Compared against": "27 plots (district)"})),
                              ensure_ascii=False)
        r = self.run_js({
            "card": f"card({it})",
            "under": f"underTxt({it}, 50)",
            "under_district": f"underTxt({district}, 43)",
            "above": f"cfg('land').key({above}).line2",
            "reason": "reasonTxt('starts 50% below the Скопје city median for 45–75 m² flats')",
            "reason_district": "reasonTxt('43% below the district median for plots')",
            "alt": f"altChip({it})",
            "capped": f"isCapped({it})",
            "short_drive": "fmtMin(3) + '|' + fmtMin(119.6) + '|' + fmtMin(45)",
        })
        self.assertEqual(r["under"], "50% under city-wide asking (166 listings)")
        self.assertEqual(r["under_district"], "43% under nearby asking (27 listings)")
        self.assertEqual(r["above"], "20% above city-wide asking (166 listings)")
        self.assertEqual(r["reason"], "starts 50% under Скопје city-wide asking for 45–75 m² flats")
        self.assertEqual(r["reason_district"], "43% under nearby asking for plots")
        # No pin → the altitude is approximate even though it wasn't flagged as a village altitude.
        self.assertIn("~700 m approx.", r["alt"])
        self.assertIn("alt approx", r["alt"])
        self.assertTrue(r["capped"])                    # 86 would be 🔥, but it's held at 👍
        self.assertEqual(r["short_drive"], "under 5 min|2 h 00|45 min")
        card = r["card"]
        self.assertIn("Ask: Is it building land", card)  # what's unconfirmed is on the card itself
        self.assertIn("Power ✓ · Water ? · Road ✓", card)
        self.assertIn('class="pill new"', card)
        self.assertIn("rating held back", card)
        self.assertIn("under 5 min", card)
        self.assertNotIn("~under", card)


if __name__ == "__main__":
    unittest.main()
