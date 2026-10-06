"""Business Idea Finder: taste model, ranking, filters, Telegram handlers, the Claude calls (faked).

Run:  python -m unittest discover tests
"""
import json
import random
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from ideas import learn, prompts, run, store, telegram

CFG = run.load_config()


def idea(title="Idea", **kw):
    base = {"title": title, "one_liner": "x", "category": "Micro-SaaS", "business_model": "Subscription",
            "channel": "SEO and content", "customer_type": "Small businesses", "geography": "EU",
            "lens": "New AI capabilities", "startup_cost_eur": 800, "automation_pct": 75, "weeks_to_revenue": 4,
            "profit_low_eur": 20000, "profit_high_eur": 200000, "self_score": 7, "tags": ["seo"]}
    return {**base, **kw}


class TempDB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ideas.db"
        self.patch = mock.patch.object(store, "DB_PATH", self.path)
        self.patch.start()
        self.conn = store.connect()

    def tearDown(self):
        self.conn.close()
        self.patch.stop()
        self.tmp.cleanup()


class ModelTests(unittest.TestCase):
    def test_no_ratings_is_neutral_and_trusts_claude(self):
        m = learn.Model([])
        self.assertEqual(m.predict(idea()), learn.NEUTRAL)
        self.assertGreater(m.score(idea(self_score=9), 0), m.score(idea(self_score=4), 0))

    def test_learns_category_preference(self):
        rated = ([idea(f"s{i}", category="Micro-SaaS", rating=9) for i in range(6)]
                 + [idea(f"c{i}", category="Content and media", rating=2) for i in range(6)])
        m = learn.Model(rated)
        saas, media = idea(category="Micro-SaaS"), idea(category="Content and media")
        self.assertGreater(m.predict(saas), 7)
        self.assertLess(m.predict(media), 4)
        self.assertIn("Category: Micro-SaaS", m.why(saas))
        self.assertTrue(1 <= m.predict(media) <= 10)

    def test_one_rating_is_a_nudge(self):
        m = learn.Model([idea(rating=10, category="Arbitrage")])
        self.assertLess(m.predict(idea(category="Arbitrage")) - m.mean, 1.0)

    def test_separates_correlated_traits(self):
        # Paid ads is always disliked; category varies. The blame should land on the channel.
        rated = []
        for i, cat in enumerate(["Micro-SaaS", "E-commerce", "Arbitrage", "Local services"] * 3):
            rated.append(idea(f"a{i}", category=cat, channel="Paid ads", rating=2))
            rated.append(idea(f"b{i}", category=cat, channel="SEO and content", rating=8))
        m = learn.Model(rated)
        self.assertLess(m.weights["channel=Paid ads"], -1.5)
        self.assertLess(abs(m.weights["category=E-commerce"]), 0.5)

    def test_solver(self):
        self.assertEqual([round(v, 6) for v in learn._solve([[4.0, 1.0], [1.0, 3.0]], [1.0, 2.0])],
                         [round(1 / 11, 6), round(7 / 11, 6)])

    def test_trust_grows_with_ratings(self):
        rated = [idea(f"x{i}", category="Micro-SaaS", rating=3) for i in range(30)]
        m = learn.Model(rated)
        # Claude says 9, you consistently say 3: with 30 ratings your history wins.
        self.assertLess(m.score(idea(category="Micro-SaaS", self_score=9), 0), 5)


class PickTests(unittest.TestCase):
    def test_variety_and_wildcard(self):
        rated = [idea(f"r{i}", category="Micro-SaaS", business_model="Subscription", channel="SEO and content",
                      rating=9) for i in range(10)]
        m = learn.Model(rated)
        pool = [idea(f"s{i}", category="Micro-SaaS", self_score=8) for i in range(5)]
        pool += [idea("Wild", category="Arbitrage", business_model="Licensing", channel="Paid ads", self_score=7)]
        pool += [idea("Low", category="E-commerce", self_score=3)]
        batch = learn.pick(pool, m, k=4, exploration=0.6, max_per_category=2)
        self.assertEqual(len(batch), 4)
        self.assertEqual([b[0]["title"] for b in batch if b[2]], ["Wild"])
        self.assertTrue(batch[-1][2])                                      # wildcard comes last
        self.assertEqual(len({id(b[0]) for b in batch}), 4)
        # A weak idea doesn't get in just for variety: a third strong Micro-SaaS fills the slot.
        self.assertNotIn("Low", [b[0]["title"] for b in batch])
        self.assertEqual(sum(b[0]["category"] == "Micro-SaaS" for b in batch), 3)
        # A decent one of another kind does take it.
        pool.append(idea("Decent", category="E-commerce", self_score=8))
        batch = learn.pick(pool, m, k=4, exploration=0.6, max_per_category=2)
        self.assertIn("Decent", [b[0]["title"] for b in batch])
        self.assertEqual(sum(b[0]["category"] == "Micro-SaaS" for b in batch), 2)

    def test_no_wildcard_before_enough_ratings(self):
        pool = [idea(f"s{i}", category=c, self_score=8) for i, c in enumerate(prompts.CATEGORIES[:5])]
        self.assertFalse(any(b[2] for b in learn.pick(pool, learn.Model([]), 5, 0.6, 2)))

    def test_fills_up_when_variety_rule_leaves_gaps(self):
        pool = [idea(f"s{i}", category="Micro-SaaS", self_score=5) for i in range(5)]
        self.assertEqual(len(learn.pick(pool, learn.Model([]), k=5, exploration=0.6, max_per_category=2)), 5)

    def test_small_pool(self):
        self.assertEqual(learn.pick([], learn.Model([]), 5, 0.6, 2), [])
        self.assertEqual(len(learn.pick([idea()], learn.Model([]), 5, 0.6, 2)), 1)


class LensTests(unittest.TestCase):
    def test_best_and_least_used(self):
        lenses = {"A": "", "B": "", "C": "", "D": ""}
        rated = [idea(lens="B", rating=9)] * 4 + [idea(lens="A", rating=3)] * 4
        used = rated + [idea(lens="C")] * 2
        self.assertEqual(learn.choose_lenses(lenses, rated, used, random.Random(1)), ["B", "D"])

    def test_config_lenses_are_valid(self):
        self.assertGreaterEqual(len(CFG["lenses"]), 3)
        self.assertEqual(len(learn.choose_lenses(CFG["lenses"], [], [], random.Random(0))), 2)


class CleanFilterTests(unittest.TestCase):
    def test_clean(self):
        c = run.clean({"title": " T ", "startup_cost_eur": "1200.4", "automation_pct": 140, "self_score": 0,
                       "lens": "nope", "tags": ["SEO ", ""]}, ["L1", "L2"])
        self.assertEqual((c["title"], c["startup_cost_eur"], c["automation_pct"], c["self_score"], c["lens"],
                          c["tags"]), ("T", 1200, 100, 1, "L1", ["seo"]))
        self.assertIsNone(run.clean({"title": ""}, ["L1"]))
        self.assertIsNone(run.clean("junk", ["L1"]))

    def test_filters(self):
        f = CFG["filters"]
        self.assertIsNone(run.violates(idea(), f))
        self.assertIn("startup cost", run.violates(idea(startup_cost_eur=f["max_startup_cost_eur"] + 1), f))
        self.assertIn("profit", run.violates(idea(profit_high_eur=f["min_annual_profit_eur"] - 1), f))
        self.assertIn("automation", run.violates(idea(automation_pct=f["min_automation_pct"] - 1), f))
        self.assertIsNotNone(run.violates(idea(startup_cost_eur=None), f))

    def test_repeats(self):
        earlier = ["Accessibility audits for Shopify stores"]
        self.assertTrue(run.is_repeat("Shopify store accessibility audits", earlier))
        self.assertTrue(run.is_repeat("AI accessibility audits for Shopify stores", earlier))
        self.assertFalse(run.is_repeat("Accessibility audits for WooCommerce stores", earlier))
        self.assertFalse(run.is_repeat("E-invoicing setup for Macedonian SMEs", earlier))
        self.assertFalse(run.is_repeat("Business process audits", ["Business news", "Process mining"]))


class SchemaTests(unittest.TestCase):
    def test_schema_is_strict(self):
        s = prompts.idea_schema(["L1", "L2"])
        item = s["properties"]["ideas"]["items"]
        self.assertFalse(item["additionalProperties"])
        self.assertEqual(set(item["required"]), set(item["properties"]))
        ev = item["properties"]["evidence"]["items"]
        self.assertEqual(set(ev["required"]), set(ev["properties"]))
        json.dumps(s)

    def test_profile_skips_empty(self):
        t = prompts.profile_text({"about": "X", "skills": "", "hours_per_week": 10})
        self.assertEqual(t, "- About: X\n- Hours per week for a new idea: 10")

    def test_prompts_format(self):
        prompts.IDEATE_SYSTEM.format(max_cost=5000, min_profit=10000, min_auto=40)
        prompts.IDEATE_TASK.format(date="d", profile="p", taste="t", research="r", lens_names="a", avoid="-",
                                   n=10, max_per_category=2)
        prompts.RESEARCH_TASK.format(date="d", profile="p", lenses="l")


class StoreTelegramTests(TempDB):
    def test_keyboard_fits_telegram(self):
        kb = telegram.keyboard(10 ** 9, chosen=7)
        flat = [b for row in kb["inline_keyboard"] for b in row]
        self.assertEqual(len(flat), 10)
        self.assertTrue(all(len(b["callback_data"].encode()) <= 64 for b in flat))
        self.assertEqual([b["text"] for b in flat if b["text"].startswith("✓")], ["✓7"])

    def test_format_escapes_and_fits(self):
        i = idea(title="<b>Tags</b> & co", evidence=[{"signal": "s", "url": "javascript:alert(1)"},
                                                     {"signal": "ok", "url": "https://e.com/?a=1&b=2"}],
                 claude_does=["x" * 900] * 5, founder_does=["y" * 900] * 3)
        text = telegram.format_idea(i, "Category: Micro-SaaS +1.0", wildcard=True)
        self.assertLessEqual(len(text), 4000)
        self.assertIn("&lt;b&gt;Tags&lt;/b&gt; &amp; co", text)
        self.assertNotIn("javascript:", text)
        short = telegram.format_idea(idea(evidence=[{"signal": "ok", "url": "https://e.com/?a=1&b=2"}]))
        self.assertIn('href="https://e.com/?a=1&amp;b=2"', short)

    def test_rate_and_reply(self):
        iid = store.add_idea(self.conn, idea("Rated"), "2026-10-06", "New AI capabilities")
        store.remember_message(self.conn, 555, iid)
        cb = {"id": "q", "data": f"i|8|{iid}", "message": {"chat": {"id": 1}, "message_id": 555}}
        with mock.patch.object(telegram.notify, "api") as api:
            telegram.on_rate(cb)
            self.assertEqual(store.get(self.conn, iid)["rating"], 8)
            self.assertIn("8/10", api.call_args_list[0].kwargs["text"])
            telegram.on_rate({**cb, "data": f"i|11|{iid}"})            # out of range: ignored
            telegram.on_rate({**cb, "data": "i|x|y"})
            self.assertEqual(store.get(self.conn, iid)["rating"], 8)
            reply = {"chat": {"id": 1}, "message_id": 9, "reply_to_message": {"message_id": 555}}
            self.assertTrue(telegram.on_reply({**reply, "text": "too much cold email"}))
            self.assertTrue(telegram.on_reply({**reply, "text": "and needs a licence"}))
            self.assertTrue(telegram.on_reply({**reply, "text": " 3/10 "}))
            self.assertFalse(telegram.on_reply({**reply, "reply_to_message": {"message_id": 1}, "text": "x"}))
        got = store.get(self.conn, iid)
        self.assertEqual((got["rating"], got["note"]), (3, "too much cold email / and needs a licence"))

    def test_unrated_and_ran_today(self):
        iid = store.add_idea(self.conn, idea(), "2026-10-06", None)
        store.mark_sent(self.conn, iid, 6.2, True)
        self.assertEqual([i["id"] for i in store.unrated_sent(self.conn)], [iid])
        self.assertEqual(store.held_since(self.conn, "2026-01-01"), [])
        rid = store.start_run(self.conn, "2026-10-06")
        self.assertFalse(store.ran_today(self.conn, "2026-10-06"))
        store.finish_run(self.conn, rid, "ok", sent=1, lenses=["a"])
        self.assertTrue(store.ran_today(self.conn, "2026-10-06"))

    def test_report(self):
        self.assertIn("No ratings yet", learn.report(self.conn))
        for i in range(12):
            iid = store.add_idea(self.conn, idea(f"t{i}", category="Micro-SaaS" if i % 2 else "Arbitrage"),
                                 "2026-10-06", "New AI capabilities")
            store.mark_sent(self.conn, iid, 5.5, False)
            store.rate(self.conn, iid, 9 if i % 2 else 2)
        text = learn.report(self.conn)
        self.assertIn("12 rated", text)
        self.assertIn("Category: Micro-SaaS", text)
        self.assertIn("Prediction gap", text)
        brief = learn.prompt_brief(store.rated(self.conn), learn.Model(store.rated(self.conn)))
        self.assertIn("Rates higher", brief)
        self.assertIn("Highest rated", brief)


# --- The Claude calls, against a fake client --------------------------------------------------------------

def msg(content, stop="end_turn", searches=0):
    usage = NS(input_tokens=1000, output_tokens=500, cache_creation_input_tokens=0, cache_read_input_tokens=0,
               server_tool_use=NS(web_search_requests=searches, web_fetch_requests=0))
    return NS(content=content, stop_reason=stop, usage=usage, stop_details=None)


class FakeStream:
    def __init__(self, m):
        self.m = m

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.m


class FakeClient:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.beta = NS(messages=NS(stream=self.stream))

    def stream(self, **kw):
        self.calls.append(kw)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return FakeStream(r)


def text(t):
    return NS(type="text", text=t)


def search_result(*urls):
    return NS(type="web_search_tool_result", content=[NS(type="web_search_result", url=u) for u in urls])


class ClaudeTests(unittest.TestCase):
    def claude(self, replies):
        from ideas.claude import Claude
        self.client = FakeClient(replies)
        return Claude(CFG["run"], client=self.client)

    def test_research_resumes_pause_and_collects_sources(self):
        c = self.claude([msg([search_result("https://a.eu"), text("part 1")], stop="pause_turn", searches=3),
                         msg([search_result("https://b.eu", "https://a.eu"), text("part 2")], searches=2)])
        brief, sources = c.research("2026-10-06", "- About: X", {"L": "desc"})
        self.assertEqual(brief, "part 1\npart 2")
        self.assertEqual(sources, ["https://a.eu", "https://b.eu"])
        self.assertEqual(c.usage.searches, 5)
        first, second = self.client.calls
        self.assertEqual(first["model"], CFG["run"]["model"])
        self.assertEqual(first["thinking"], {"type": "adaptive"})
        self.assertEqual(first["tools"][0]["type"], "web_search_20260209")
        self.assertEqual(first["fallbacks"], "default")
        self.assertEqual(second["messages"][-1]["role"], "assistant")      # resumed, no extra user turn
        self.assertGreater(c.usage.cost_usd(CFG["prices"]), 0)

    def test_search_error_object_is_ignored(self):
        err = NS(type="web_search_tool_result", content=NS(type="web_search_tool_result_error", error_code="x"))
        c = self.claude([msg([err, text("brief")])])
        self.assertEqual(c.research("d", "p", {"L": ""}), ("brief", []))

    def test_ideate_parses_structured_output(self):
        c = self.claude([msg([NS(type="thinking", thinking=""), text(json.dumps({"ideas": [idea("A")]}))])])
        out = c.ideate(date="d", profile="p", taste="t", research="r", lens_names=["L"], avoid=[], n=1,
                       filters=CFG["filters"], max_per_category=2)
        self.assertEqual(out[0]["title"], "A")
        call = self.client.calls[0]
        self.assertEqual(call["output_config"]["format"]["type"], "json_schema")
        self.assertNotIn("tools", call)

    def test_refusal_and_truncation_raise(self):
        from ideas.claude import ClaudeError
        with self.assertRaises(ClaudeError):
            self.claude([msg([], stop="refusal")]).research("d", "p", {"L": ""})
        with self.assertRaises(ClaudeError):
            self.claude([msg([text('{"ideas": [')], stop="max_tokens")]).ideate(
                date="d", profile="p", taste="t", research="r", lens_names=["L"], avoid=[], n=1,
                filters=CFG["filters"], max_per_category=2)

    def test_fallback_rejected_retries_without_it(self):
        import anthropic
        import httpx2
        req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        bad = anthropic.BadRequestError("fallbacks: unknown parameter",
                                        response=httpx2.Response(400, request=req), body=None)
        c = self.claude([bad, msg([text("ok")])])
        self.assertEqual(c.research("d", "p", {"L": ""})[0], "ok")
        self.assertNotIn("fallbacks", self.client.calls[1])
        self.assertFalse(c.fallback)


class FakeClaude:
    def __init__(self, run_cfg):
        self.usage = NS(searches=4, cost_usd=lambda prices: 0.42)

    def research(self, date, profile, lenses):
        self.lenses = list(lenses)
        return "signals", ["https://src.eu"]

    def ideate(self, **kw):
        lens = kw["lens_names"][0]
        names = ["Accessibility audits for Shopify stores", "E-invoicing setup for Macedonian firms",
                 "Tender alerts for construction companies", "Diaspora property management reports",
                 "Etsy listing translation service", "Grant application drafting for NGOs"]
        good = [idea(n, lens=lens, category=prompts.CATEGORIES[i]) for i, n in enumerate(names)]
        return good + [idea("Too expensive", startup_cost_eur=99999), idea("Shopify store accessibility audits"),
                       {"title": ""}]


class RunTests(TempDB):
    def test_end_to_end_sends_and_skips_second_run(self):
        sent = []

        def api(method, **kw):
            sent.append((method, kw))
            return {"message_id": len(sent)}
        with mock.patch("ideas.claude.Claude", FakeClaude), mock.patch.object(telegram, "chat_id", lambda: "1"), \
                mock.patch.object(telegram.notify, "api", api), mock.patch.object(telegram.time, "sleep"):
            self.assertEqual(run.main([]), 0)
            self.assertEqual(run.main([]), 0)                      # already ran today: no second batch
        ideas_sent = [kw for m, kw in sent if m == "sendMessage" and "reply_markup" in kw]
        self.assertEqual(len(ideas_sent), CFG["run"]["send_per_run"])
        self.assertIn("$0.42", sent[-1][1]["text"])
        r = store.last_runs(self.conn, 1)[0]
        self.assertEqual((r["status"], r["candidates"], r["kept"], r["sent"]), ("ok", 9, 6, 5))
        self.assertEqual(len(store.last_runs(self.conn, 10)), 1)
        held = store.held_since(self.conn, "2000-01-01")
        self.assertEqual(len(held), 1)                             # the 6th waits for another day

    def test_failure_is_reported(self):
        class Broken(FakeClaude):
            def research(self, *a):
                raise RuntimeError("boom")
        with mock.patch("ideas.claude.Claude", Broken), \
                mock.patch.object(run.notify, "send_status") as status:
            self.assertEqual(run.main([]), 2)
        self.assertIn("boom", status.call_args.args[0])
        self.assertEqual(store.last_runs(self.conn, 1)[0]["status"], "error")


if __name__ == "__main__":
    unittest.main()
