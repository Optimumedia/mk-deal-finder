"""Telegram bot listener: answers 👍 / 👎 taps instantly and a few commands.

    python -m scraper.bot          (setup_schedule.ps1 starts it at logon)

Commands (only from your own chat; everyone else is ignored):
    /top     best 3 current deals per researcher
    /status  when the last run happened and how it went
    /help    this list
Log: data/bot.log
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sqlite3
import sys
import time
from pathlib import Path

from . import feedback, notify, rating

ROOT = Path(__file__).resolve().parent.parent
DATA_JSON = ROOT / "docs" / "data.json"
DEALS_DB = ROOT / "data" / "deals.db"
log = logging.getLogger("bot")

HELP = ("MK Deal Finder bot\n\n"
        "Tap 👍 / 👎 under a deal: 👎 hides it for good, and your votes tune future scores "
        "for similar deals (same district, city, property type).\n\n"
        "/top – best current deals\n/status – last run\n/help – this message")


def listing_info(listing_id: str) -> dict | None:
    try:
        conn = sqlite3.connect(DEALS_DB.as_uri() + "?mode=ro", uri=True, timeout=30)
        row = conn.execute("SELECT kind, city, district, title FROM listings WHERE id=?", (listing_id,)).fetchone()
        conn.close()
    except sqlite3.Error as e:
        log.warning("deals.db lookup failed: %s", e)
        return None
    return dict(zip(("kind", "city", "district", "title"), row)) if row else None


def load_dashboard() -> dict | None:
    try:
        return json.loads(DATA_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def on_vote(cb: dict, conn) -> None:
    try:
        _, vote, researcher, listing_id = cb["data"].split("|", 3)
    except ValueError:
        return
    up = vote == "up"
    feedback.record(conn, listing_id, researcher, 1 if up else -1, listing_info(listing_id))
    log.info("vote %s %s %s", vote, researcher, listing_id)
    notify.api("answerCallbackQuery", callback_query_id=cb["id"],
               text="Saved 👍 — similar deals will score higher" if up else "Hidden 👎 — you won't see this one again")
    msg = cb.get("message") or {}
    if msg:
        notify.api("editMessageReplyMarkup", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
                   reply_markup=notify.vote_buttons(researcher, listing_id, vote))


def cmd_top(chat: str, conn) -> None:
    data = load_dashboard()
    if not data:
        notify.api("sendMessage", chat_id=chat, text="No results yet — the first run hasn't finished.")
        return
    results = {r["key"]: r["items"] for r in data["researchers"]}
    results = rating.rate_all(feedback.apply(results, feedback.load_votes()))
    for r in data["researchers"]:
        best = [x for x in results.get(r["key"], []) if x.get("qualified")][:3]
        for x in best:
            notify.api("sendMessage", chat_id=chat, parse_mode="HTML", disable_web_page_preview="true",
                       text=notify.format_deal(x, r["title"].split(" — ")[0], r["key"])[:4000],
                       reply_markup=notify.vote_buttons(r["key"], x["id"]))
            time.sleep(0.4)


def cmd_status(chat: str) -> None:
    data = load_dashboard()
    if not data:
        notify.api("sendMessage", chat_id=chat, text="No runs have finished yet.")
        return
    run = (data.get("runs") or [{}])[0]
    lines = [f"Last update: {data['generated_at'].replace('T', ' ')[:16]} UTC",
             f"Last run: {run.get('status', '?')} — {run.get('pages', 0)} pages, {run.get('details', 0)} details, "
             f"{run.get('new_listings', 0)} new listings",
             f"Listings tracked: {data.get('listings_tracked', 0):,}", ""]
    lines += [f"{r['title'].split(' — ')[0]}: {r['qualified']} deals" for r in data["researchers"]]
    if run.get("message"):
        lines.append(f"\n⚠️ {run['message']}")
    notify.api("sendMessage", chat_id=chat, text="\n".join(lines))


def main() -> int:
    (ROOT / "data").mkdir(exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(ROOT / "data" / "bot.log", maxBytes=1_000_000, backupCount=2,
                                                   encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", handlers=[handler])
    notify.load_settings()
    if not notify.configured():
        log.error("Telegram is not configured — run setup_telegram.ps1 first")
        return 1
    my_chat = str(os.environ["TELEGRAM_CHAT_ID"])
    conn = feedback.connect()
    offset = int(feedback.get_meta(conn, "telegram_offset", 0))
    log.info("bot started")
    while True:
        updates = notify.api("getUpdates", _timeout=70, offset=offset, timeout=50,
                             allowed_updates=["callback_query", "message"])
        if updates is None:
            time.sleep(15)          # offline, asleep, or another instance is polling
            continue
        for u in updates:
            offset = u["update_id"] + 1
            feedback.set_meta(conn, "telegram_offset", offset)
            try:
                if "callback_query" in u:
                    cb = u["callback_query"]
                    if str(cb.get("message", {}).get("chat", {}).get("id")) == my_chat:
                        on_vote(cb, conn)
                elif "message" in u:
                    msg = u["message"]
                    if str(msg["chat"]["id"]) != my_chat:
                        continue                      # not you — ignore
                    text = (msg.get("text") or "").strip().lower()
                    if text.startswith("/top"):
                        cmd_top(my_chat, conn)
                    elif text.startswith("/status"):
                        cmd_status(my_chat)
                    else:
                        notify.api("sendMessage", chat_id=my_chat, text=HELP)
            except Exception:
                log.exception("failed to handle update %s", u.get("update_id"))


if __name__ == "__main__":
    sys.exit(main())
