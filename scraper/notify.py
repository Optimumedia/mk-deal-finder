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


def vote_buttons(researcher: str, listing_id: str, chosen: str | None = None) -> dict:
    """Inline keyboard; callback data is 'v|<up|dn>|<researcher>|<listing id>' (< 64 bytes)."""
    def btn(vote, label):
        mark = " ✓" if vote == chosen else ""
        return {"text": label + mark, "callback_data": f"v|{vote}|{researcher}|{listing_id}"}
    return {"inline_keyboard": [[btn("up", "👍 Interested"), btn("dn", "👎 Not for me")]]}


def format_deal(item: dict, researcher_title: str = "", researcher: str = "") -> str:
    m = {k: v for k, v in item["metrics"].items() if v is not None}
    top = [(k, m[k]) for k in HEADLINE if k in m][:5] or list(m.items())[:4]
    where = " / ".join(x for x in (item.get("city"), item.get("district")) if x)
    lines = []
    if researcher_title:
        lines.append(f"{ICON.get(researcher, '🏠')} <i>{html.escape(researcher_title)}</i>")
    r = item.get("rating")
    lines += [
        (f"<b>{r['emoji']} {html.escape(r['label'])}</b> · score {item['score']}" if r else f"<b>{item['score']}</b>"),
        f"<a href=\"{item['url']}\">{html.escape(item['title'] or 'listing')}</a>",
        html.escape(where),
        " · ".join(f"{html.escape(k)}: {html.escape(str(v))}" for k, v in top),
    ]
    if item.get("reasons"):
        lines.append("↳ " + html.escape("; ".join(item["reasons"][:3])))
    return "\n".join(lines)


def send(results: dict, db, cfg: dict, researchers) -> int:
    """Send new qualified deals above the score threshold; each deal only once."""
    if not configured():
        return 0
    chat = os.environ["TELEGRAM_CHAT_ID"]
    n_max, min_score = cfg["notify"]["max_items_per_researcher"], cfg["notify"]["min_score"]
    sent = 0
    for r in researchers:
        fresh = [x for x in results.get(r.NAME, [])
                 if x["qualified"] and x["score"] >= min_score and not db.was_notified(x["id"], r.NAME)][:n_max]
        for x in fresh:
            ok = api("sendMessage", chat_id=chat, text=format_deal(x, r.TITLE.split(" — ")[0], r.NAME)[:4000],
                     parse_mode="HTML", disable_web_page_preview="true", reply_markup=vote_buttons(r.NAME, x["id"]))
            if ok:
                db.mark_notified(x["id"], r.NAME)
                sent += 1
            time.sleep(0.4)        # stay well under Telegram's rate limit
    db.conn.commit()
    if sent and os.environ.get("DASHBOARD_URL"):
        api("sendMessage", chat_id=chat, parse_mode="HTML", disable_web_page_preview="true",
            text=f"{sent} new deal{'s' if sent != 1 else ''} · <a href=\"{os.environ['DASHBOARD_URL']}\">open dashboard</a>")
    return sent


def send_status(text: str) -> None:
    """Plain warning message (failed / blocked runs)."""
    if configured():
        api("sendMessage", chat_id=os.environ["TELEGRAM_CHAT_ID"], text=text[:4000])
