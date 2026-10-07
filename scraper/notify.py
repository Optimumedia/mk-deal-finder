"""Telegram alerts (free, optional).

Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the environment (run_local.ps1
loads them from .telegram). Without them every function here does nothing.

- send():        one message per new top deal, with 👍 / 👎 buttons
- send_status(): a warning when a run is blocked or fails
The buttons are answered by scraper/bot.py, which records your votes.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import time
from pathlib import Path

import requests

from . import maps

log = logging.getLogger(__name__)

# The numbers worth seeing on a phone, per researcher (first ones present win).
HEADLINE = ["Est. profit / month", "Rent / month", "Payback (months)", "Starting price", "Sale date",
            "Price", "Price now", "Area (m²)",
            "€/m²", "Below market", "Est. profit", "ROI", "Price cut", "Land type"]
ICON = {"airbnb": "🛏", "land": "🌲", "flip": "🔨", "motivated": "📉", "auctions": "⚖️"}


SETTINGS_FILE = Path(__file__).resolve().parent.parent / ".telegram"


def load_settings(path: Path = SETTINGS_FILE) -> None:
    """Read .telegram (written by setup_telegram.ps1) into the environment."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        m = re.match(r"\s*([A-Z_]+)\s*=\s*(.+?)\s*$", line)
        if m:
            os.environ.setdefault(m.group(1), m.group(2))


def configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def api(method: str, _timeout: float = 40, **params) -> dict | list | None:
    """Call the Telegram Bot API; returns the result or None (never raises)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    data = {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in params.items()}
    try:
        resp = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=data, timeout=_timeout)
        body = resp.json()
    except (requests.RequestException, ValueError) as e:
        log.warning("telegram %s failed: %s", method, e)
        return None
    if not body.get("ok"):
        log.warning("telegram %s: %s", method, str(body)[:200])
        return None
    return body["result"]


def vote_buttons(researcher: str, listing_id: str, chosen: str | None = None, needs_info: bool = False) -> dict:
    """Inline keyboard; callback data is 'v|<up|dn|ok>|<researcher>|<listing id>' (< 64 bytes).

    Needs-info leads get ✅ Fits (you checked with the seller) / ❌ Not a fit.
    """
    def btn(vote, label):
        mark = " ✓" if vote == chosen else ""
        return {"text": label + mark, "callback_data": f"v|{vote}|{researcher}|{listing_id}"}
    if needs_info:
        return {"inline_keyboard": [[btn("ok", "✅ Fits — confirmed"), btn("dn", "❌ Not a fit")]]}
    return {"inline_keyboard": [[btn("up", "👍 Interested"), btn("dn", "👎 Not for me")]]}


PRIVATE_DASHBOARD = "http://localhost:8800"
REPLY_HINT = ("✍️ After the call, record the answers on your dashboard (✅ Fits / ❌ Not a fit / "
              "'What the seller told you').")


def format_deal(item: dict, researcher_title: str = "", researcher: str = "") -> str:
    m = {k: v for k, v in item["metrics"].items() if v is not None}
    top = [(k, m[k]) for k in HEADLINE if k in m][:5] or list(m.items())[:4]
    where = " / ".join(x for x in (item.get("city"), item.get("district")) if x)
    lines = []
    if researcher_title:
        lines.append(f"{ICON.get(researcher, '🏠')} <i>{html.escape(researcher_title)}</i>")
    r = item.get("rating")
    head = f"<b>{r['emoji']} {html.escape(r['label'])}</b> · score {item['score']}" if r else f"<b>{item['score']}</b>"
    if item.get("status") == "needs_info":
        head = "❓ <b>Needs info</b> · " + head
    elif item.get("status") == "confirmed":
        head = "✅ <b>Confirmed by you</b> · " + head
    lines += [
        head,
        f"<a href=\"{item['url']}\">{html.escape(item['title'] or 'listing')}</a>",
        html.escape(where),
        " · ".join(f"{html.escape(k)}: {html.escape(str(v))}" for k, v in top),
    ]
    if item.get("reasons"):
        lines.append("↳ " + html.escape("; ".join(item["reasons"][:3])))
    if item.get("status") == "needs_info" and item.get("questions"):
        who = "Check the official notice or ask the bailiff:" if researcher == "auctions" else "Ask the seller:"
        lines.append(f"\n<b>{who}</b>")
        lines += [f"• {html.escape(q)}" for q in item["questions"]]
        lines.append("\n" + REPLY_HINT)
    gmaps = maps.url(item.get("lat"), item.get("lng"), item.get("map_query"))
    if gmaps:
        exact = item.get("lat") and item.get("lng")
        lines.append(f"📍 <a href=\"{html.escape(gmaps)}\">Google Maps</a>" + ("" if exact else " (area — no exact pin in the ad)"))
    deal_link = f"{os.environ.get('DASHBOARD_URL', '').rstrip('/')}/#deal={item['id']}" if os.environ.get("DASHBOARD_URL") else None
    if deal_link:
        lines.append(f"⭐ <a href=\"{html.escape(deal_link)}\">Open on the dashboard</a> · rate it on your PC: "
                     f"{PRIVATE_DASHBOARD}/#deal={html.escape(item['id'])}")
    if item.get("owner_note"):
        lines.append(f"📝 Your note: {html.escape(item['owner_note'])}")
    return "\n".join(lines)


def _post(chat, item, researcher, title, fb_conn) -> bool:
    # Rating happens on the platform (owner's choice) — the message links to the deal there.
    res = api("sendMessage", chat_id=chat, text=format_deal(item, title, researcher)[:4000], parse_mode="HTML",
              disable_web_page_preview="true")
    if res and fb_conn is not None:
        from . import feedback
        feedback.remember_message(fb_conn, res["message_id"], item["id"], researcher)
    time.sleep(0.4)        # stay well under Telegram's rate limit
    return bool(res)


def send(results: dict, db, cfg: dict, researchers) -> int:
    """Send new deals above the score threshold, plus a few ❓ needs-info leads; each only once."""
    if not configured():
        return 0
    from . import feedback
    chat = os.environ["TELEGRAM_CHAT_ID"]
    n = cfg["notify"]
    fb = feedback.connect()
    sent = asked = 0
    for r in researchers:
        title = r.TITLE.split(" — ")[0]
        items = results.get(r.NAME, [])
        fresh = [x for x in items if x["qualified"] and x.get("status") != "needs_info"
                 and x["score"] >= n["min_score"] and not db.was_notified(x["id"], r.NAME)][:n["max_items_per_researcher"]]
        for x in fresh:
            if _post(chat, x, r.NAME, title, fb):
                db.mark_notified(x["id"], r.NAME)
                sent += 1
    # Needs-info leads: best first across researchers, a few per run.
    leads = sorted(((x, r) for r in researchers for x in results.get(r.NAME, [])
                    if x.get("status") == "needs_info" and x["score"] >= n["needs_info_min_score"]
                    and not db.was_notified(x["id"], "ask:" + r.NAME)),
                   key=lambda p: -p[0]["score"])
    seen = set()
    for x, r in leads:
        if asked >= n["max_needs_info_per_run"]:
            break
        if x["id"] in seen:
            continue
        seen.add(x["id"])
        if _post(chat, x, r.NAME, r.TITLE.split(" — ")[0], fb):
            db.mark_notified(x["id"], "ask:" + r.NAME)
            asked += 1
    db.conn.commit()
    fb.close()
    if (sent or asked) and os.environ.get("DASHBOARD_URL"):
        parts = [f"{sent} new deal{'s' if sent != 1 else ''}"] + ([f"{asked} to check with sellers ❓"] if asked else [])
        api("sendMessage", chat_id=chat, parse_mode="HTML", disable_web_page_preview="true",
            text=" · ".join(parts) + f" · <a href=\"{os.environ['DASHBOARD_URL']}\">open dashboard</a>")
    return sent + asked


def send_status(text: str) -> None:
    """Plain warning message (failed / blocked runs)."""
    if configured():
        api("sendMessage", chat_id=os.environ["TELEGRAM_CHAT_ID"], text=text[:4000])
