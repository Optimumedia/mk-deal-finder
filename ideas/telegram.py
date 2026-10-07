"""Business ideas in Telegram: one message per idea with 1-10 rating buttons.

Uses the deal finder's bot (same .telegram settings). scraper/bot.py routes
'i|...' button taps, replies to idea messages and /ideas, /taste here.
Callback data: 'i|<rating>|<idea id>'.
"""
from __future__ import annotations

import html
import logging
import os
import re
import time

from scraper import notify

from . import learn, store

log = logging.getLogger("bot")


def _eur(v) -> str:
    if v is None:
        return "?"
    v = float(v)
    if v >= 1_000_000:
        return f"€{v / 1_000_000:.1f}M".replace(".0M", "M")
    if v >= 10_000:
        return f"€{v / 1000:.0f}k"
    return f"€{v:,.0f}"


def keyboard(idea_id: int, chosen: int | None = None) -> dict:
    def btn(n):
        return {"text": f"✓{n}" if n == chosen else str(n), "callback_data": f"i|{n}|{idea_id}"}
    return {"inline_keyboard": [[btn(n) for n in range(1, 6)], [btn(n) for n in range(6, 11)]]}


def _items(values, limit: int) -> list[str]:
    return [f"• {html.escape(str(v))}" for v in (values or [])[:limit]]


def format_idea(idea: dict, why: str = "", wildcard: bool = False) -> str:
    e = html.escape
    lines = []
    if wildcard:
        lines.append("🧪 <i>Wildcard: a kind of idea you haven't rated much yet</i>")
    lines += [f"💡 <b>{e(idea['title'])}</b>", f"<i>{e(idea.get('one_liner') or '')}</i>", "",
              f"💰 {_eur(idea.get('profit_low_eur'))}–{_eur(idea.get('profit_high_eur'))} profit/yr · "
              f"start {_eur(idea.get('startup_cost_eur'))} · 🤖 {idea.get('automation_pct', '?')}% automated · "
              f"first € in ~{idea.get('weeks_to_revenue', '?')} wks",
              f"<b>Customer:</b> {e(idea.get('customer') or '')}",
              f"<b>Offer:</b> {e(idea.get('offer') or '')}",
              f"<b>Math:</b> {e(idea.get('profit_math') or '')}"]
    if idea.get("claude_does"):
        lines += ["", "<b>🤖 Claude does</b>"] + _items(idea["claude_does"], 5)
    if idea.get("founder_does"):
        lines += ["<b>🙋 You do</b>"] + _items(idea["founder_does"], 3)
    if idea.get("validation_test"):
        lines += ["", f"<b>✅ Test it this week (&lt;€100):</b> {e(idea['validation_test'])}"]
    if idea.get("first_week"):
        lines += ["<b>🚀 First steps</b>"] + _items(idea["first_week"], 3)
    if idea.get("risks"):
        lines += ["<b>⚠️ Risks</b>"] + _items(idea["risks"], 3)
    ev = [x for x in idea.get("evidence") or [] if isinstance(x, dict)][:2]
    if ev:
        lines.append("<b>📰 Why now</b>")
        for x in ev:
            url = str(x.get("url") or "")
            link = f' (<a href="{e(url, quote=True)}">source</a>)' if re.match(r"https?://", url) else ""
            lines.append(f"• {e(str(x.get('signal') or ''))}{link}")
    meta = [f"{e(idea.get('category') or '')}", e(idea.get("lens") or "")]
    lines += ["", " · ".join(m for m in meta if m) + f" · Claude's score {idea.get('self_score', '?')}/10"]
    if why:
        lines.append(f"🎯 For you: {e(why)}")
    lines.append("<b>Rate it 1–10 👇</b> Reply to this message to say why.")
    text = "\n".join(lines)
    if len(text) > 4000:                    # Telegram's limit is 4096; drop detail, keep the buttons' context
        text = text[:3990].rsplit("\n", 1)[0] + "\n…"
    return text


def send_idea(conn, chat: str, idea: dict, why: str, wildcard: bool) -> bool:
    res = notify.api("sendMessage", chat_id=chat, parse_mode="HTML", disable_web_page_preview="true",
                     text=format_idea(idea, why, wildcard), reply_markup=keyboard(idea["id"], idea.get("rating")))
    if res:
        store.remember_message(conn, res["message_id"], idea["id"])
    time.sleep(0.4)
    return bool(res)


def on_rate(cb: dict) -> None:
    try:
        _, n, idea_id = cb["data"].split("|", 2)
        n, idea_id = int(n), int(idea_id)
    except ValueError:
        return
    if not 1 <= n <= 10:
        return
    conn = store.connect()
    try:
        ok = store.rate(conn, idea_id, n)
    finally:
        conn.close()
    if not ok:
        notify.api("answerCallbackQuery", callback_query_id=cb["id"], text="That idea isn't in the database any more.")
        return
    tip = "Reply with what you like about it." if n >= 7 else "Reply with what's wrong: that teaches me most."
    notify.api("answerCallbackQuery", callback_query_id=cb["id"], text=f"Saved {n}/10. {tip}")
    msg = cb.get("message") or {}
    if msg:
        notify.api("editMessageReplyMarkup", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
                   reply_markup=keyboard(idea_id, n))
    log.info("idea rated %s → %s", idea_id, n)


_JUST_A_NUMBER = re.compile(r"^\s*(10|[1-9])\s*(?:/\s*10)?\s*$")


def on_reply(msg: dict) -> bool:
    """A reply to an idea message: a note on why, or a rating typed as a number. False if not an idea."""
    conn = store.connect()
    try:
        idea_id = store.message_idea(conn, msg["reply_to_message"]["message_id"])
        if idea_id is None:
            return False
        text = (msg.get("text") or "").strip()
        m = _JUST_A_NUMBER.match(text)
        if m:
            store.rate(conn, idea_id, int(m.group(1)))
            reply = f"Saved {m.group(1)}/10."
        elif text:
            store.add_note(conn, idea_id, text[:1000])
            reply = "Noted. Tomorrow's ideas take it into account."
        else:
            return True
    finally:
        conn.close()
    notify.api("sendMessage", chat_id=msg["chat"]["id"], reply_to_message_id=msg["message_id"], text=reply)
    log.info("idea reply %s", idea_id)
    return True


def cmd_ideas(chat: str) -> None:
    """Resend the latest ideas you haven't rated yet."""
    conn = store.connect()
    try:
        todo = store.unrated_sent(conn, 5)
        if not todo:
            notify.api("sendMessage", chat_id=chat,
                       text="Nothing waiting for a rating. New ideas arrive every morning.")
            return
        model = learn.Model(store.rated(conn))
        for idea in todo:
            send_idea(conn, chat, idea, model.why(idea), bool(idea.get("explore")))
    finally:
        conn.close()


def cmd_taste(chat: str) -> None:
    notify.api("sendMessage", chat_id=chat, text=learn.report()[:4000])


def chat_id() -> str | None:
    return os.environ.get("TELEGRAM_CHAT_ID") if notify.configured() else None
