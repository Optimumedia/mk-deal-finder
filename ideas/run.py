"""Daily business-idea run.

    python -m ideas.run               research, write ideas, send the best to Telegram
    python -m ideas.run --dry-run     same, but print the ideas instead of sending them
    python -m ideas.run --force       run even if today's ideas were already sent

1. fit the taste model on every rating so far        (ideas/learn.py)
2. pick two research lenses: best rated + least used
3. Claude Code researches today's signals on the web  (ideas/claude.py, your subscription)
4. Claude writes N ideas from them, told your taste
5. drop ideas outside the filters in ideas.toml, and repeats of earlier ideas
6. rank today's and recent unsent ideas by the rating you're expected to give
7. send the best few with 1-10 buttons (after 5 ratings, one is a wildcard)
Exit codes: 0 ok (or nothing to do), 2 failed (a Telegram warning is sent).
"""
from __future__ import annotations

import argparse
import html
import logging
import random
import re
import sys
import tomllib
from datetime import date, timedelta
from pathlib import Path

from scraper import notify

from . import learn, prompts, store

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "ideas.toml"
LOCAL_FILE = ROOT / ".ideas"           # this PC's Claude Code path (setup_ideas.ps1)
log = logging.getLogger("ideas")


def load_config(path: Path = CONFIG) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _int(v) -> int | None:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def clean(idea: dict, lens_names: list[str]) -> dict | None:
    """Normalise one idea from Claude; None if it's unusable."""
    if not isinstance(idea, dict) or not str(idea.get("title") or "").strip():
        return None
    out = dict(idea)
    out["title"] = str(idea["title"]).strip()[:120]
    for k in ("startup_cost_eur", "monthly_cost_eur", "weeks_to_revenue", "profit_low_eur", "profit_high_eur",
              "automation_pct", "self_score"):
        out[k] = _int(idea.get(k))
    if out["automation_pct"] is not None:
        out["automation_pct"] = max(0, min(100, out["automation_pct"]))
    if out["self_score"] is not None:
        out["self_score"] = max(1, min(10, out["self_score"]))
    if out.get("lens") not in lens_names:
        out["lens"] = lens_names[0] if lens_names else None
    out["tags"] = [str(t).strip().lower() for t in idea.get("tags") or [] if str(t).strip()][:5]
    return out


def violates(idea: dict, f: dict) -> str | None:
    """Why an idea breaks the filters in ideas.toml, or None."""
    if idea.get("startup_cost_eur") is None or idea["startup_cost_eur"] > f["max_startup_cost_eur"]:
        return f"startup cost {idea.get('startup_cost_eur')} > {f['max_startup_cost_eur']}"
    if idea.get("profit_high_eur") is None or idea["profit_high_eur"] < f["min_annual_profit_eur"]:
        return f"profit ceiling {idea.get('profit_high_eur')} < {f['min_annual_profit_eur']}"
    if idea.get("automation_pct") is None or idea["automation_pct"] < f["min_automation_pct"]:
        return f"automation {idea.get('automation_pct')}% < {f['min_automation_pct']}%"
    return None


_STOP = {"a", "an", "the", "for", "of", "to", "and", "with", "in", "on", "ai", "by", "your", "as", "service"}


def _words(text: str) -> set[str]:
    words = (w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP and len(w) > 1)
    return {w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in words}


def is_repeat(title: str, earlier: list[str], threshold: float = 0.65) -> bool:
    """Same idea under a slightly different name: most title words shared ("store" = "stores")."""
    a = _words(title)
    if not a:
        return False
    for t in earlier:
        b = _words(t)
        if b and len(a & b) / len(a | b) >= threshold:
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print the ideas, don't send them")
    ap.add_argument("--force", action="store_true", help="run even if today's ideas were already sent")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.stdout.reconfigure(encoding="utf-8")
    notify.load_settings()
    notify.load_settings(LOCAL_FILE)
    cfg = load_config()
    run_cfg, filters = cfg["run"], cfg["filters"]
    today = date.today().isoformat()
    conn = store.connect()
    if store.ran_today(conn, today) and not args.force and not args.dry_run:
        log.info("today's ideas were already sent; use --force to run again")
        return 0
    run_id = store.start_run(conn, today)
    claude = None
    try:
        from .claude import Claude                 # finds Claude Code; the bot doesn't need it
        claude = Claude(run_cfg)
        rated = store.rated(conn)
        model = learn.Model(rated)
        everything = store.all_ideas(conn)
        lens_names = learn.choose_lenses(cfg["lenses"], rated, everything, random.Random(today))
        profile = prompts.profile_text(cfg["profile"])
        log.info("lenses: %s · %d ratings so far", ", ".join(lens_names), len(rated))

        research, sources = claude.research(today, profile, {n: cfg["lenses"][n] for n in lens_names})
        log.info("research: %d chars, %d sources", len(research), len(sources))

        earlier = store.all_titles(conn)
        raw = claude.ideate(date=today, profile=profile, taste=learn.prompt_brief(rated, model), research=research,
                            lens_names=lens_names, avoid=earlier, n=int(run_cfg["candidates_per_run"]),
                            filters=filters, max_per_category=int(run_cfg["max_per_category"]))
        kept = 0
        for idea in raw:
            idea = clean(idea, lens_names)
            if idea is None:
                continue
            why_not = violates(idea, filters)
            if why_not:
                log.info("dropped '%s': %s", idea["title"], why_not)
                continue
            if is_repeat(idea["title"], earlier):
                log.info("dropped '%s': repeats an earlier idea", idea["title"])
                continue
            store.add_idea(conn, idea, today, idea.get("lens"))
            earlier.append(idea["title"])
            kept += 1
        log.info("%d ideas written, %d kept", len(raw), kept)

        since = (date.today() - timedelta(days=int(run_cfg["keep_candidates_days"]))).isoformat()
        pool = store.held_since(conn, since)
        batch = learn.pick(pool, model, int(run_cfg["send_per_run"]), float(run_cfg["exploration"]),
                           int(run_cfg["max_per_category"]))
        sent = deliver(conn, batch, model, args.dry_run)
        store.finish_run(conn, run_id, "ok", lenses=lens_names, candidates=len(raw), kept=kept,
                         sent=sent, sources=sources)
        log.info("sent %d ideas · $0 spent (subscription; same tokens on the API would be $%.2f)",
                 sent, claude.usage.list_value_usd)
        return 0
    except Exception as e:                          # report every failure, never die silently
        log.exception("idea run failed")
        store.finish_run(conn, run_id, "error", message=str(e)[:500])
        if not args.dry_run:
            notify.send_status(f"⚠️ Idea Finder: today's run failed: {str(e)[:300]}")
        return 2
    finally:
        conn.close()


def deliver(conn, batch: list[tuple[dict, float, bool]], model: learn.Model, dry_run: bool) -> int:
    from . import telegram
    chat = None if dry_run else telegram.chat_id()
    if chat is None:
        for idea, predicted, wildcard in batch:
            print("\n" + "=" * 70)
            print(html.unescape(re.sub(r"<[^>]+>", "", telegram.format_idea(idea, model.why(idea), wildcard))))
            print(f"(expected rating {predicted:.1f})")
        if not dry_run:
            log.warning("Telegram isn't configured, so the ideas were only printed (and stay unsent)")
        return 0
    n = 0
    for idea, predicted, wildcard in batch:
        if telegram.send_idea(conn, chat, idea, model.why(idea), wildcard):
            store.mark_sent(conn, idea["id"], predicted, wildcard)
            n += 1
    if n:
        notify.api("sendMessage", chat_id=chat,
                   text=f"☀️ {n} new business idea{'s' if n != 1 else ''} · $0 spent (your Claude plan)\n"
                        "Rate each 1–10. Reply to an idea to say why. /taste shows what I've learned.")
    return n


if __name__ == "__main__":
    sys.exit(main())
