"""Optional Telegram alerts for new top deals (free).

Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (environment / GitHub secrets).
Without them this module does nothing.
"""
from __future__ import annotations

import html
import logging
import os

import requests

log = logging.getLogger(__name__)


def _fmt(item: dict) -> str:
    m = {k: v for k, v in item["metrics"].items() if v is not None}
    top = list(m.items())[:4]
    where = " / ".join(x for x in (item.get("city"), item.get("district")) if x)
    lines = [
        f"<b>{item['score']}</b> · <a href=\"{item['url']}\">{html.escape(item['title'] or 'listing')}</a>",
        html.escape(where),
        " · ".join(f"{html.escape(k)}: {html.escape(str(v))}" for k, v in top),
    ]
    if item.get("reasons"):
        lines.append("↳ " + html.escape("; ".join(item["reasons"][:2])))
    return "\n".join(lines)


def send(results: dict, db, cfg: dict, researchers) -> None:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    n_max, min_score = cfg["notify"]["max_items_per_researcher"], cfg["notify"]["min_score"]
    blocks = []
    for r in researchers:
        fresh = [x for x in results.get(r.NAME, [])
                 if x["qualified"] and x["score"] >= min_score and not db.was_notified(x["id"], r.NAME)][:n_max]
        if not fresh:
            continue
        blocks.append(f"🏠 <b>{html.escape(r.TITLE)}</b>\n\n" + "\n\n".join(_fmt(x) for x in fresh))
        for x in fresh:
            db.mark_notified(x["id"], r.NAME)
    site = os.environ.get("DASHBOARD_URL")
    for text in blocks:
        if site:
            text += f"\n\n<a href=\"{site}\">Open dashboard</a>"
        try:
            resp = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=20, data={
                "chat_id": chat, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": "true"})
            if not resp.ok:
                log.warning("telegram: %s %s", resp.status_code, resp.text[:200])
        except requests.RequestException as e:
            log.warning("telegram failed: %s", e)
    db.conn.commit()
