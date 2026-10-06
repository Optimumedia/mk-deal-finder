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

import re

from . import feedback, notify, rating, reasons
from .text import norm

ROOT = Path(__file__).resolve().parent.parent
DATA_JSON = ROOT / "docs" / "data.json"
DEALS_DB = ROOT / "data" / "deals.db"
log = logging.getLogger("bot")

HELP = ("MK Deal Finder bot\n\n"
        "Tap 👍 / 👎 under a deal: 👎 hides it for good, and your votes tune future scores "
        "for similar deals (same district, city, property type).\n\n"
        "Tap ❌ / 👎 and pick a reason: deals like it rank lower from then on.\n"
        "Reply to a deal with what the seller told you, e.g. 'water yes, area 450, price 32000'.\n"
        "Found something on Facebook / Viber? Forward or paste it here (or a Reklama5 link) for an instant check.\n\n"
        "/top – best current deals\n/status – last run\n/learn – what your rejections taught me\n"
        "/help – this message")


def listing_info(listing_id: str) -> dict | None:
    try:
        conn = sqlite3.connect(DEALS_DB.as_uri() + "?mode=ro", uri=True, timeout=30)
        row = conn.execute("SELECT kind, city, district, title, area_m2, price_eur FROM listings WHERE id=?",
                           (listing_id,)).fetchone()
        conn.close()
    except sqlite3.Error as e:
        log.warning("deals.db lookup failed: %s", e)
        return None
    return dict(zip(("kind", "city", "district", "title", "area_m2", "price_eur"), row)) if row else None


def load_dashboard() -> dict | None:
    try:
        return json.loads(DATA_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _edit_buttons(cb: dict, markup: dict) -> None:
    msg = cb.get("message") or {}
    if msg:
        notify.api("editMessageReplyMarkup", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
                   reply_markup=markup)


def on_vote(cb: dict, conn) -> None:
    """👍 / 👎 / ✅ Fits / ❌ Not a fit — 👎 and ❌ open the reason picker."""
    try:
        _, vote, researcher, listing_id = cb["data"].split("|", 3)
    except ValueError:
        return
    info = listing_info(listing_id)
    if vote == "dn":
        feedback.record(conn, listing_id, researcher, -1, info)
        notify.api("answerCallbackQuery", callback_query_id=cb["id"], text="Why isn't it a fit? Pick a reason 👇")
        _edit_buttons(cb, reasons.keyboard(researcher, listing_id))
    elif vote == "ok":
        feedback.record(conn, listing_id, researcher, 1, info)
        feedback.confirm(conn, listing_id)
        notify.api("answerCallbackQuery", callback_query_id=cb["id"],
                   text="✅ Confirmed — re-scored on the next run. Reply with any details you learned.")
        _edit_buttons(cb, {"inline_keyboard": [[{"text": "✅ Confirmed by you", "callback_data": "x"},
                                                 {"text": "❌ Not a fit", "callback_data": f"v|dn|{researcher}|{listing_id}"}]]})
    else:
        feedback.record(conn, listing_id, researcher, 1, info)
        notify.api("answerCallbackQuery", callback_query_id=cb["id"], text="Saved 👍 — similar deals will score higher")
        _edit_buttons(cb, notify.vote_buttons(researcher, listing_id, "up"))
    log.info("vote %s %s %s", vote, researcher, listing_id)


def on_reason(cb: dict, conn) -> None:
    try:
        _, code, researcher, listing_id = cb["data"].split("|", 3)
    except ValueError:
        return
    if code == "undo":
        feedback.undo(conn, listing_id, researcher)
        notify.api("answerCallbackQuery", callback_query_id=cb["id"], text="Undone — the deal is back")
        _edit_buttons(cb, notify.vote_buttons(researcher, listing_id))
        return
    reason = reasons.BY_CODE.get(code)
    if not reason:
        return
    feedback.reject(conn, listing_id, researcher, code, listing_info(listing_id))
    log.info("reject %s %s %s", code, researcher, listing_id)
    tip = ("Reply to this message with your reason." if code == "oth"
           else "Got it — similar deals will rank lower." if reason.learn in ("learn", "size_min", "size_max")
           else "Noted.")
    notify.api("answerCallbackQuery", callback_query_id=cb["id"], text=f"❌ {reason.label}. {tip}")
    _edit_buttons(cb, {"inline_keyboard": [[{"text": f"❌ {reason.label}", "callback_data": "x"},
                                             {"text": "↩ Undo", "callback_data": f"r|undo|{researcher}|{listing_id}"}]]})


_YES = r"(?:yes|y|da|ima|ok|true|potvrdeno)"
_NO = r"(?:no|n|ne|nema|false)"
_UTIL_WORDS = {"electricity": r"(?:struja|elektrika|elektricna|power|electricity|current)",
               "water": r"(?:voda|vodovod|water)",
               "road": r"(?:pat|patot|asfalt|road|pristap|access)"}


def parse_answer(text: str) -> dict:
    """'water yes, power no, area 450, price 32.000, building yes' (MK / Latin / EN) → facts."""
    n = norm(text)
    raw = text.lower()
    facts = {}
    for key, w in _UTIL_WORDS.items():
        if re.search(rf"(?<![a-z]){w} {_NO}(?![a-z])|(?<![a-z]){_NO} {w}(?![a-z])", n):
            facts[key] = "no"
        elif re.search(rf"(?<![a-z]){w} {_YES}(?![a-z])|(?<![a-z])(?:ima|has) {w}(?![a-z])", n):
            facts[key] = "yes"
    m = re.search(r"(?:price|cena|цена|€|eur)\s*[:=]?\s*(\d[\d.,]*)\s*(k\b|илј|ilj)?", raw)
    if m:
        v = float(re.sub(r"[^\d]", "", m.group(1)) or 0) * (1000 if m.group(2) else 1)
        if v >= 100:
            facts["price_eur"] = v
    m = (re.search(r"(?:area|povrsina|површина|kvadratura|квадратура|size)\s*[:=]?\s*(\d[\d.,]*)", raw)
         or re.search(r"(\d[\d.,]*)\s*(?:m2|м2|m²|м²|kv|кв)", raw))
    if m:
        num = m.group(1).rstrip(".,")
        num = num.replace(".", "").replace(",", "") if re.fullmatch(r"\d{1,3}([.,]\d{3})+", num) else num.replace(",", ".")
        try:
            v = float(num)
        except ValueError:
            v = 0
        if v >= 10:
            facts["area_m2"] = v
    if re.search(rf"(?:building|gradez[a-z]*) {_NO}(?![a-z])|zemjodel|agricultur", n):
        facts["land_type"] = "agricultural"
    elif re.search(rf"(?:building|gradez[a-z]*) {_YES}(?![a-z])|(?<![a-z])gradezn[a-z]*", n):
        facts["land_type"] = "building"
    return facts


FACT_LABEL = {"electricity": "⚡ power", "water": "💧 water", "road": "🛣 road", "price_eur": "price",
              "area_m2": "area", "land_type": "land type"}


def on_reply(msg: dict, conn) -> None:
    """A reply to a deal message: facts from the seller, or a note on a rejection."""
    ref = feedback.message_listing(conn, msg["reply_to_message"]["message_id"])
    chat = msg["chat"]["id"]
    if not ref:
        notify.api("sendMessage", chat_id=chat,
                   text="I can't match that message to a deal — reply to one of the deal alerts.")
        return
    listing_id, researcher = ref
    text = (msg.get("text") or "").strip()
    facts = parse_answer(text)
    for k, v in facts.items():
        feedback.set_override(conn, listing_id, k, v)
    noted_rejection = feedback.add_rejection_note(conn, listing_id, text)
    if not facts and not noted_rejection:
        feedback.set_override(conn, listing_id, "note", text[:500])
    parts = [f"{FACT_LABEL[k]}: {'✓' if v == 'yes' else '✗' if v == 'no' else v}" for k, v in facts.items()]
    reply = ("Saved " + ", ".join(parts) + "." if parts else
             "Saved as the reason for rejecting it." if noted_rejection else "Saved as a note on this deal.")
    notify.api("sendMessage", chat_id=chat, reply_to_message_id=msg["message_id"],
               text=reply + " It's re-scored with this on the next run (06:15 / 18:15).")
    log.info("reply %s %s facts=%s", researcher, listing_id, facts)


_R5_LINK = re.compile(r"https?://(?:m\.|www\.)?reklama5\.mk/AdDetails\?ad=(\d+)", re.I)


def cmd_quick_check(chat: str, text: str, reply_to: int) -> None:
    """A forwarded post or a Reklama5 link → instant verdict against the market data."""
    import tomllib
    from . import quick_check
    from .http import PoliteSession
    from .sources import reklama5
    cfg = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
    m = _R5_LINK.search(text)
    if m:
        try:
            d = reklama5.parse_detail(PoliteSession(0.5, 0.5).get(reklama5.detail_url(m.group(1))))
            text = "\n".join(filter(None, [
                d.get("title"), d.get("description"), d.get("address"),
                " ".join(f"{k}: {v}" for k, v in (d.get("fields") or {}).items()),
                f"{d['price_eur']:.0f} €" if d.get("price_eur") else "",
                f"{d['area_m2']:.0f} m2" if d.get("area_m2") else "",
                d.get("city"), d.get("district")]))
        except Exception as e:      # a bad link must never stop the bot
            log.warning("quick check fetch failed: %s", e)
    lead = quick_check.extract(text)
    notify.api("sendMessage", chat_id=chat, reply_to_message_id=reply_to,
               text=quick_check.verdict(lead, quick_check.market(cfg))
               + "\n\n(Private: forwarded leads are never published.)")


def cmd_learn(chat: str) -> None:
    from .learn import report
    notify.api("sendMessage", chat_id=chat, text=report()[:4000])


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
            res = notify.api("sendMessage", chat_id=chat, parse_mode="HTML", disable_web_page_preview="true",
                             text=notify.format_deal(x, r["title"].split(" — ")[0], r["key"])[:4000],
                             reply_markup=notify.vote_buttons(r["key"], x["id"],
                                                              needs_info=x.get("status") == "needs_info"))
            if res:
                feedback.remember_message(conn, res["message_id"], x["id"], r["key"])
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
                        data = cb.get("data", "")
                        if data.startswith("r|"):
                            on_reason(cb, conn)
                        elif data.startswith("v|"):
                            on_vote(cb, conn)
                        else:
                            notify.api("answerCallbackQuery", callback_query_id=cb["id"])
                elif "message" in u:
                    msg = u["message"]
                    if str(msg["chat"]["id"]) != my_chat:
                        continue                      # not you — ignore
                    # Forwarded photo posts carry their text as a caption.
                    original = (msg.get("text") or msg.get("caption") or "").strip()
                    text = original.lower()
                    if msg.get("reply_to_message") and not text.startswith("/"):
                        on_reply(msg, conn)
                    elif text.startswith("/learn"):
                        cmd_learn(my_chat)
                    elif text.startswith("/top"):
                        cmd_top(my_chat, conn)
                    elif text.startswith("/status"):
                        cmd_status(my_chat)
                    elif text.startswith(("/help", "/start")) or not re.search(r"\d|http", text):
                        notify.api("sendMessage", chat_id=my_chat, text=HELP)
                    else:
                        cmd_quick_check(my_chat, original, msg["message_id"])
            except Exception:
                log.exception("failed to handle update %s", u.get("update_id"))


if __name__ == "__main__":
    sys.exit(main())
