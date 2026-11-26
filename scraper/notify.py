"""Telegram alerts (free, optional).

Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the environment (run_local.ps1
loads them from .telegram). Without them every function here does nothing.

Telegram is a short nudge; you rate deals on the private dashboard. After each run:
- send():        only genuinely new, top-rated deals you haven't reviewed — one
                 message per property (never again for a re-post, a copy on
                 another site or a small price change; again only for a real
                 price cut), at most a few per run, nothing when nothing is great.
                 It also sends the weekly summary and warns about a broken source.
- send_due():    one reminder a day for pipeline next steps that are due
- send_status(): a warning when a run is blocked or fails
The pure parts (select(), format_deal(), weekly_text(), source_problems()) are
used by tests and to simulate what a run would send.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

from . import maps

log = logging.getLogger(__name__)

ICON = {"airbnb": "🛏", "land": "🌲", "weekend": "🏔", "flip": "🔨", "motivated": "📉", "auctions": "⚖️"}
SHORT = {"airbnb": "Airbnb", "land": "Land for a house", "weekend": "Weekend plot 700 m+", "flip": "Fix & flip",
         "motivated": "Motivated seller", "auctions": "Bailiff auction"}
# Besides price and area: the one or two numbers that make the case, per researcher.
KEY_METRICS = {
    "airbnb": ["Est. profit / month", "Payback (months)"],
    "land": ["Below market", "Land type"],
    "weekend": ["Below market", "Land type"],
    "flip": ["Below market", "Est. profit", "ROI"],
    "motivated": ["Price cut", "vs typical price nearby"],
    "auctions": ["Below market", "Sale date"],
}
EURO_METRICS = {"Est. profit / month", "Est. profit", "Rent / month"}
TELEGRAM_LIMIT = 4096
SETTINGS_FILE = Path(__file__).resolve().parent.parent / ".telegram"
PRIVATE_DASHBOARD = "http://localhost:8800"
PUBLIC_DASHBOARD = "https://optimumedia.github.io/mk-deal-finder/"
REPLY_HINT = "✍️ Record the answers on your dashboard (✅ Fits / ❌ Not a fit)."
SYSTEM = "system"          # notified.researcher for once-a-day / once-a-week messages


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


# ------------------------------------------------------------------ formatting
def esc(s) -> str:
    """Safe for Telegram HTML text and attribute values."""
    return html.escape(str(s), quote=True)


def eur(v) -> str:
    return f"€{v:,.0f}" if isinstance(v, (int, float)) else str(v)


def dashboard_url(cfg: dict | None = None) -> str:
    return (os.environ.get("DASHBOARD_URL") or ((cfg or {}).get("notify") or {}).get("dashboard_url")
            or PUBLIC_DASHBOARD).rstrip("/") + "/"


def deal_link(base: str, item: dict, researcher: str = "") -> str:
    h = f"#deal={quote(item['id'], safe='')}" + (f"&in={quote(researcher, safe='')}" if researcher else "")
    return base.rstrip("/") + "/" + h


def key_reason(item: dict) -> str | None:
    """The first reason that explains the deal (not a vote adjustment like '-4 from your votes')."""
    for r in item.get("reasons") or []:
        if not re.match(r"\s*[-−+]\s*\d", r):
            return r
    return None


def _numbers(item: dict, researcher: str) -> str:
    m = {k: v for k, v in (item.get("metrics") or {}).items() if v not in (None, "", "—", "-", "–")}
    parts = []
    if item.get("price") is not None:
        label = "start " if researcher == "auctions" else "rent " if researcher == "airbnb" else ""
        parts.append(f"<b>{label}{esc(eur(item['price']))}</b>" + ("/month" if researcher == "airbnb" else ""))
    if item.get("area"):
        parts.append(f"{item['area']:,.0f} m²")
    if item.get("ppm2") and researcher != "airbnb":
        parts.append(f"{item['ppm2']:,.0f} €/m²")
    for k in KEY_METRICS.get(researcher, []):
        if k in m:
            v = m[k]
            if k == "Below market":
                parts.append(f"{esc(v)} below market")
            elif k == "Price cut":
                parts.append(f"cut {esc(v)}")
            elif k == "Land type":
                parts.append(esc(str(v).lower()) + " land")
            elif k in EURO_METRICS:
                parts.append(f"{esc(k.lower())} {esc(eur(v))}")
            else:
                parts.append(f"{esc(k.lower())} {esc(v)}")
    return " · ".join(parts)


def _place(item: dict) -> str:
    where = " / ".join(esc(x) for x in dict.fromkeys(x for x in (item.get("city"), item.get("district")) if x))
    bits = [where] if where else []
    if item.get("elevation") is not None:
        bits.append(f"⛰ {'~' if item.get('elevation_approx') else ''}{item['elevation']:,.0f} m")
    if item.get("kind") in ("land", "house", "weekend_house"):
        drives = []
        for name, k in (("Skopje", "to_skopje"), ("Ohrid", "to_ohrid")):
            d = item.get(k) or {}
            m = d.get("min")
            if m is not None and (k == "to_skopje" or m <= 90):     # Ohrid only when it's close
                drives.append(f"{name} {m} min" if m < 60 else f"{name} {m // 60} h {m % 60:02d}")
            elif m is None and d.get("air_km") is not None and k == "to_skopje":
                drives.append(f"{name} ~{d['air_km']} km")
        if drives:
            bits.append("🚗 " + ", ".join(drives) + (" (approx.)" if item.get("distance_approx") else ""))
    return "📍 " + " · ".join(bits) if bits else ""


def _utilities(item: dict) -> str:
    u = item.get("utilities") or {}
    if item.get("kind") != "land" or not u:
        return ""
    label = {"electricity": "power", "water": "water", "road": "road"}
    yes = [label[k] for k in label if u.get(k) == "yes"]
    unknown = [label[k] for k in label if u.get(k) not in ("yes", "no")]
    no = [label[k] for k in label if u.get(k) == "no"]
    out = []
    if yes:
        out.append("✅ " + ", ".join(yes))
    if unknown:
        out.append("❓ " + ", ".join(unknown) + " not confirmed")
    if no:
        out.append("✗ no " + ", ".join(no))
    return " · ".join(out)


def format_deal(item: dict, researcher_title: str = "", researcher: str = "", *, cut: dict | None = None,
                also: list[str] | None = None, footer: str = "", base_url: str | None = None) -> str:
    """One-glance Telegram HTML: rating, price, key reason, altitude/drive for land, links.

    Everything from an ad is escaped, and the message always fits Telegram's limit
    (optional lines are dropped instead of cutting a tag in half)."""
    r = item.get("rating")
    head = (f"{r['emoji']} <b>{esc(r['label'])} · {item['score']}</b>" if r else f"<b>Score {item['score']}</b>")
    if item.get("status") == "needs_info":
        head = "❓ <b>Needs info</b> · " + head
    elif item.get("status") == "confirmed":
        head = "✅ <b>Confirmed by you</b> · " + head
    who = SHORT.get(researcher) or (researcher_title.split(" — ")[0] if researcher_title else "")
    if who:
        head += f"  {ICON.get(researcher, '🏠')} {esc(who)}"
    lines = [head]
    if cut:
        lines.append(f"📉 <b>Price cut {cut['pct']:.0f}%</b>: {esc(eur(cut['before']))} → {esc(eur(cut['after']))}"
                     " since the last alert")
    title = (item.get("title") or "listing")[:90]
    lines.append(f"<a href=\"{esc(item['url'])}\">{esc(title)}</a>")
    nums = _numbers(item, researcher)
    if nums:
        lines.append(nums)
    for line in (_place(item), _utilities(item)):
        if line:
            lines.append(line)
    reason = key_reason(item)
    optional = []
    if reason:
        optional.append("↳ " + esc(reason))
    if also:
        optional.append("Also fits: " + esc(", ".join(also)))
    if item.get("duplicates"):
        optional.append(f"Advertised {item['duplicates'] + 1}× (agencies / re-posts)")
    if item.get("status") == "needs_info" and item.get("questions"):
        ask = "Check the official notice or ask the bailiff:" if researcher == "auctions" else "Ask the seller:"
        optional.append(f"<b>{ask}</b>\n" + "\n".join(f"• {esc(q)}" for q in item["questions"][:3]))
        optional.append(REPLY_HINT)
    if item.get("owner_note"):
        optional.append(f"📝 Your note: {esc(item['owner_note'][:300])}")
    base = base_url or dashboard_url()
    links = [f"<a href=\"{esc(item['url'])}\">Ad</a>",
             f"<a href=\"{esc(deal_link(base, item, researcher))}\">Dashboard</a>"]
    gmaps = maps.url(item.get("lat"), item.get("lng"), item.get("map_query"))
    if gmaps:
        links.append(f"<a href=\"{esc(gmaps)}\">Map</a>" + ("" if item.get("lat") and item.get("lng") else " (area)"))
    tail = ["🔗 " + " · ".join(links),
            f"🖥 Rate it on your PC: {esc(PRIVATE_DASHBOARD)}/#deal={esc(quote(item['id'], safe=''))}"]
    if footer:
        tail.append(footer)
    while True:
        text = "\n".join(lines + optional + tail)
        if len(text) <= TELEGRAM_LIMIT or not optional:
            break
        optional.pop()
    if len(text) > TELEGRAM_LIMIT:          # only a pathological title could get here
        text = "\n".join([head, f"<a href=\"{esc(item['url'])}\">listing</a>"] + tail[:1])
    return text


# ------------------------------------------------------------------ alert memory
ALERT_SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_log (
    listing_id TEXT NOT NULL,
    researcher TEXT NOT NULL,
    kind       TEXT,
    city       TEXT,
    district   TEXT,
    area       REAL,
    price      REAL,          -- total price when the alert went out
    title      TEXT,
    image      TEXT,
    case_no    TEXT,          -- bailiff sales: court case number (identifies the property)
    score      INTEGER,
    why        TEXT,          -- new | cut | ask
    sent_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_alert_log_id ON alert_log(listing_id);
"""


def _ensure(conn) -> None:
    conn.executescript(ALERT_SCHEMA)


def load_history(db, keep_days: int = 365) -> list[dict]:
    """Every property we already alerted about: alert_log plus older alerts from the
    notified table (price then taken from the price history at the time of the alert)."""
    conn = db.conn
    _ensure(conn)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
    conn.execute("DELETE FROM alert_log WHERE sent_at < ?", (cutoff,))
    cols = ("listing_id", "researcher", "kind", "city", "district", "area", "price", "title", "image", "case_no",
            "sent_at")
    hist = [dict(zip(cols, r)) for r in conn.execute(f"SELECT {', '.join(cols)} FROM alert_log")]
    for h in hist:
        h["case"] = h.pop("case_no")
    legacy = conn.execute(
        "SELECT n.listing_id, n.researcher, l.kind, l.city, l.district, l.area_m2, l.price_eur, l.price_note,"
        " l.title, l.image, n.notified_at, l.extra FROM notified n LEFT JOIN listings l ON l.id = n.listing_id"
        " WHERE n.researcher NOT IN ('pipeline', ?) AND n.listing_id NOT IN (SELECT listing_id FROM alert_log)",
        (SYSTEM,)).fetchall()
    for lid, res, kind, city, district, area, price, note, title, image, at, extra in legacy:
        try:
            case = (json.loads(extra) or {}).get("case") if extra else None
        except (ValueError, AttributeError):
            case = None
        old = conn.execute("SELECT price_eur FROM price_history WHERE listing_id=? AND seen_at<=? "
                           "ORDER BY seen_at DESC LIMIT 1", (lid, at)).fetchone()
        p = old[0] if old and old[0] is not None else price
        if p is not None and note == "per_m2":
            p = p * area if area else None
        hist.append({"listing_id": lid, "researcher": res, "kind": kind, "city": city, "district": district,
                     "area": area,
                     "price": None if note == "placeholder" else p, "title": title, "image": image, "case": case,
                     "sent_at": at})
    return hist


def _words(t: str | None) -> set[str]:
    return set(re.findall(r"\w{3,}", (t or "").lower()))


_PLACEHOLDER = re.compile(r"no_?image|no_?photo|placeholder|/content/images/", re.I)   # site default pictures


def _kind(k: str | None) -> str | None:
    return "house" if k in ("house", "weekend_house") else k


def same_property(a: dict, b: dict) -> bool:
    """a / b: {listing_id, kind, city, area, price, title, image}. True for the same ad,
    a re-post, or a copy on another site (same type and town, same size ±3%, and the
    same photo, a similar title or a price within 15%)."""
    if a.get("listing_id") == b.get("listing_id"):
        return True
    ka, kb = str(a.get("listing_id")).startswith("kirsm:"), str(b.get("listing_id")).startswith("kirsm:")
    if ka or kb:
        # Bailiff sales have templated titles ("Стан, 61 m² — Скопје"): only the court case
        # number identifies the property (a new round of the same case is a price cut).
        # One case can sell several flats, so the size must match too.
        aa, ab = a.get("area"), b.get("area")
        return (ka and kb and bool(a.get("case")) and a.get("case") == b.get("case")
                and (not (aa and ab) or abs(aa - ab) <= 0.03 * max(aa, ab)))
    if _kind(a.get("kind")) != _kind(b.get("kind")) or not a.get("kind"):
        return False
    for k in ("city", "district"):
        if a.get(k) and b.get(k) and a[k] != b[k]:
            return False
    pa, pb = a.get("price"), b.get("price")
    same_photo = (bool(a.get("image")) and a.get("image") == b.get("image") and not _PLACEHOLDER.search(a["image"])
                  and (not (pa and pb) or abs(pa - pb) <= 0.4 * max(pa, pb)))
    wa, wb = _words(a.get("title")), _words(b.get("title"))
    similar_title = bool(wa and wb) and len(wa & wb) / len(wa | wb) >= 0.5
    aa, ab = a.get("area"), b.get("area")
    if aa and ab:
        if abs(aa - ab) > 0.03 * max(aa, ab):
            return False
        # Same size and (almost) the same price in one district is one property, even
        # with a new title; a cut re-post usually keeps its photo or wording.
        exact = (bool(a.get("district") and b.get("district")) and abs(aa - ab) <= 0.01 * max(aa, ab)
                 and bool(pa and pb) and abs(pa - pb) <= 0.05 * max(pa, pb))
        return same_photo or similar_title or exact
    return same_photo or (similar_title and bool(pa and pb) and abs(pa - pb) <= 0.02 * max(pa, pb))


def _sig(item: dict) -> dict:
    return {"listing_id": item["id"], "kind": item.get("kind"), "city": item.get("city"),
            "district": item.get("district"), "area": item.get("area"),
            "price": item.get("price"), "title": item.get("title"), "image": item.get("image"),
            "case": (item.get("metrics") or {}).get("Case no.")}


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ------------------------------------------------------------------ selection
def threshold(n: dict, researcher: str) -> int:
    return int((n.get("min_score_by_researcher") or {}).get(researcher, n.get("min_score", 70)))


def select(results: dict, history: list[dict], cfg: dict, now: datetime | None = None) -> tuple[list[dict], int]:
    """Pick the alerts for this run. Returns (picks, held_back) where each pick is
    {item, researcher, why: new|cut|ask, also: [other researchers], cut: {...}|None}.

    A deal goes out only if it's top-rated for its researcher, you haven't voted on
    it, it's new (first seen within new_within_days), and we never alerted about the
    same property — unless the price really dropped (≥ real_cut_pct and ≥ real_cut_eur)
    and the last alert about it is older than cooldown_days."""
    n = cfg["notify"]
    now = now or datetime.now(timezone.utc)
    tiers = {"once": 0, "exceptional": 1, "great": 2, "good": 3, "watch": 4}
    max_tier = tiers[n.get("min_rating", "great")]
    # An older ad that only now became top-rated (details read, a new researcher) still
    # gets one alert if it's this good — it was never alerted before.
    old_tier = tiers[n.get("old_ads_min_rating", "exceptional")]
    new_since = (now - timedelta(days=n.get("new_within_days", 7))).date().isoformat()
    cut_pct, cut_eur = n.get("real_cut_pct", 10) / 100, n.get("real_cut_eur", 1000)
    cooldown = timedelta(days=n.get("cooldown_days", 7))
    ask_min = n.get("alert_needs_info_min_score", 75)

    # The same listing can sit in several researchers: keep its strongest case.
    best: dict[str, dict] = {}
    for researcher, items in results.items():
        bar = threshold(n, researcher)
        for x in items:
            ask = x.get("status") == "needs_info"
            if not x.get("qualified") or x.get("my_vote") is not None or x.get("status") == "pending":
                continue
            if x.get("score", 0) < (max(bar, ask_min) if ask else bar):
                continue
            if (x.get("rating") or {}).get("rank", 0) > max_tier:
                continue
            cand = {"item": x, "researcher": researcher, "why": "ask" if ask else "new", "also": [],
                    "margin": x["score"] - bar}
            prev = best.get(x["id"])
            if prev is None:
                best[x["id"]] = cand
            elif cand["margin"] > prev["margin"]:
                cand["also"] = prev["also"] + [prev["researcher"]]
                best[x["id"]] = cand
            else:
                prev["also"].append(researcher)

    picks = []
    for c in best.values():
        x = c["item"]
        sig = _sig(x)
        matches = [h for h in history if same_property(sig, h)]
        if matches:
            last = max(matches, key=lambda h: h.get("sent_at") or "")
            before = last.get("price")
            after = x.get("price")
            when = _parse_ts(last.get("sent_at"))
            if (before and after and after <= before * (1 - cut_pct) and before - after >= cut_eur
                    and (when is None or now - when >= cooldown)):
                c["why"] = "cut"
                c["cut"] = {"before": before, "after": after, "pct": 100 * (before - after) / before}
                picks.append(c)
            continue
        if (x.get("first_seen") or "") < new_since and (x.get("rating") or {}).get("rank", 9) > old_tier:
            continue                    # not new: it's on the dashboard, no need to ping
        picks.append(c)

    # Two different ads of one property in the same run: keep the better one.
    picks.sort(key=lambda c: ((c["item"].get("rating") or {}).get("rank", 9), c["why"] == "ask", -c["item"]["score"]))
    unique = []
    for c in picks:
        if not any(same_property(_sig(c["item"]), _sig(u["item"])) for u in unique):
            unique.append(c)
    asks = 0
    out = []
    for c in unique:
        if c["why"] == "ask":
            if asks >= n.get("max_needs_info_per_run", 1):
                continue
            asks += 1
        out.append(c)
    limit = n.get("max_per_run", 3)
    return out[:limit], max(0, len(out) - limit)


# ------------------------------------------------------------------ sending
def _post(chat, text, item_id, researcher, fb_conn) -> bool:
    res = api("sendMessage", chat_id=chat, text=text, parse_mode="HTML", disable_web_page_preview="true")
    if res and fb_conn is not None:
        from . import feedback
        feedback.remember_message(fb_conn, res["message_id"], item_id, researcher)   # replies with seller facts
    time.sleep(0.4)        # stay well under Telegram's rate limit
    return bool(res)


def _log(db, c: dict) -> None:
    x = c["item"]
    db.conn.execute("INSERT INTO alert_log VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (x["id"], c["researcher"], x.get("kind"), x.get("city"), x.get("district"), x.get("area"),
                     x.get("price"),
                     x.get("title"), x.get("image"), _sig(x)["case"], x.get("score"), c["why"],
                     datetime.now(timezone.utc).replace(microsecond=0).isoformat()))
    db.mark_notified(x["id"], ("ask:" if c["why"] == "ask" else "") + c["researcher"])


def _titles(researchers) -> dict:
    return {r.NAME: r.TITLE.split(" — ")[0] for r in researchers}


def build_messages(results: dict, history: list[dict], cfg: dict, researchers, now=None) -> list[tuple[dict, str]]:
    """(pick, text) for every alert this run would send — used by send() and simulations."""
    picks, held = select(results, history, cfg, now)
    titles = _titles(researchers)
    base = dashboard_url(cfg)
    out = []
    for i, c in enumerate(picks):
        footer = ""
        if held and i == len(picks) - 1:
            footer = f"➕ {held} more top deal{'s' if held != 1 else ''} waiting on the dashboard"
        out.append((c, format_deal(c["item"], titles.get(c["researcher"], ""), c["researcher"], cut=c.get("cut"),
                                   also=[titles.get(a, a) for a in c["also"]], footer=footer, base_url=base)))
    return out


def send(results: dict, db, cfg: dict, researchers) -> int:
    """After a run: new top deals, the weekly summary (if due) and broken-source warnings."""
    if not configured():
        return 0
    from . import feedback
    chat = os.environ["TELEGRAM_CHAT_ID"]
    history = load_history(db, cfg["notify"].get("history_days", 365))
    fb = feedback.connect()
    sent = 0
    try:
        for c, text in build_messages(results, history, cfg, researchers):
            if _post(chat, text, c["item"]["id"], c["researcher"], fb):
                _log(db, c)
                sent += 1
        db.conn.commit()
    finally:
        fb.close()
    send_health(db, cfg)
    send_weekly(results, db, cfg, researchers)
    return sent


def _once(db, key: str, text: str) -> bool:
    """Send text once per key (e.g. one week, one day); remembered in the notified table."""
    if db.was_notified(key, SYSTEM):
        return False
    if api("sendMessage", chat_id=os.environ["TELEGRAM_CHAT_ID"], text=text[:TELEGRAM_LIMIT], parse_mode="HTML",
           disable_web_page_preview="true"):
        db.mark_notified(key, SYSTEM)
        db.conn.commit()
        return True
    return False


def send_due(pipeline: list[dict], db, today: str | None = None) -> int:
    """One Telegram reminder per day listing pipeline next steps that are due."""
    if not configured():
        return 0
    text, count = due_text(pipeline, today)
    if not text:
        return 0
    today = today or date.today().isoformat()
    if db.was_notified(f"due:{today}", "pipeline"):
        return 0
    if api("sendMessage", chat_id=os.environ["TELEGRAM_CHAT_ID"], text=text, parse_mode="HTML",
           disable_web_page_preview="true"):
        db.mark_notified(f"due:{today}", "pipeline")
        db.conn.commit()
    return count


def due_text(pipeline: list[dict], today: str | None = None) -> tuple[str | None, int]:
    today = today or date.today().isoformat()
    due = sorted((e for e in pipeline if e.get("due") and e["due"] <= today and e["stage"] not in ("won", "dropped")),
                 key=lambda e: e["due"])
    if not due:
        return None, 0
    lines = [f"📋 <b>Next steps due</b> ({len(due)})"]
    for e in due[:15]:
        it = e.get("item") or {}
        late = (date.fromisoformat(today) - date.fromisoformat(e["due"][:10])).days
        when = "today" if late <= 0 else f"{late} day{'s' if late != 1 else ''} overdue"
        price = f" · {eur(it['price'])}" if isinstance(it.get("price"), (int, float)) else ""
        gone = " · ad removed" if e.get("removed") else ""
        lines.append(f"• <b>{esc(e.get('next_step') or 'Next step')}</b> ({when}) — "
                     f"<a href=\"{esc(it.get('url') or PRIVATE_DASHBOARD)}\">{esc((it.get('title') or e['id'])[:60])}</a>"
                     f"{esc(price)}{gone}")
    if len(due) > 15:
        lines.append(f"…and {len(due) - 15} more")
    lines.append(f"\nUpdate them on your dashboard: {PRIVATE_DASHBOARD}/#tab=pipeline")
    return "\n".join(lines)[:TELEGRAM_LIMIT], len(due)


def send_status(text: str) -> None:
    """Plain warning message (failed / blocked runs)."""
    if configured():
        api("sendMessage", chat_id=os.environ["TELEGRAM_CHAT_ID"], text=text[:TELEGRAM_LIMIT])


# ------------------------------------------------------------------ health
SOURCE_NAMES = {"reklama5": "Reklama5", "novel": "Novel Estate", "nedviznosti": "Nedviznosti.com.mk",
                "kirsm": "Bailiff register (KIRSM)"}


def source_problems(db, cfg: dict, now: datetime | None = None) -> list[str]:
    """Sources that stopped delivering (nothing seen for source_stale_hours), and runs
    that never finished (PC shut down / crashed mid-run) since the last good run."""
    now = now or datetime.now(timezone.utc)
    hours = cfg["notify"].get("source_stale_hours", 36)
    out = []
    enabled = {s for s in SOURCE_NAMES if (cfg.get(s) or {}).get("enabled", True)}
    for src, last in db.conn.execute("SELECT source, MAX(last_seen) FROM listings GROUP BY source"):
        t = _parse_ts(last)
        if src in enabled and t and now - t > timedelta(hours=hours):
            out.append(f"{SOURCE_NAMES.get(src, src)}: no listings read for {(now - t).total_seconds() / 3600:.0f} h "
                       f"(last {t:%d %b %H:%M} UTC) — the site may have changed or be blocking.")
    runs = db.recent_runs(6)
    for r in runs[1:]:                      # runs[0] is the current one
        if r.get("status") == "ok":
            break
        if r.get("status") == "interrupted":
            out.append(f"Run #{r['id']} (started {str(r.get('started_at'))[:16].replace('T', ' ')} UTC) never "
                       "finished — the PC was probably shut down or went to sleep.")
            break
    return out


def send_health(db, cfg: dict) -> bool:
    problems = source_problems(db, cfg)
    if not problems:
        return False
    text = "⚠️ <b>MK Deal Finder: something needs a look</b>\n" + "\n".join(f"• {esc(p)}" for p in problems)
    return _once(db, f"health:{date.today().isoformat()}", text)     # at most once a day


# ------------------------------------------------------------------ bot: /top
def top_text(results: dict, cfg: dict, titles: dict, per_researcher: int = 2, limit: int = 10) -> str:
    """/top in one message: the best deals you haven't voted on, strongest first,
    at most per_researcher from each list, one line each."""
    rows, seen = [], set()
    for r, items in results.items():
        k = 0
        for x in items:
            if k >= per_researcher:
                break
            if not x.get("qualified") or x.get("my_vote") is not None or x["id"] in seen:
                continue
            seen.add(x["id"])
            rows.append((x, r))
            k += 1
    rows.sort(key=lambda p: ((p[0].get("rating") or {}).get("rank", 9), -p[0].get("score", 0)))
    if not rows:
        return "Nothing waiting — every good deal has been rated. 👌"
    base = dashboard_url(cfg)
    lines = ["🏆 <b>Best deals you haven't rated</b>"]
    for x, r in rows[:limit]:
        rt = x.get("rating") or {}
        bits = [f"{rt.get('emoji', '')} <b>{x.get('score')}</b> {ICON.get(r, '🏠')} {esc(SHORT.get(r, titles.get(r, r)))}"]
        if x.get("price") is not None:
            bits.append(esc(eur(x["price"])) + ("/month" if r == "airbnb" else ""))
        if x.get("area"):
            bits.append(f"{x['area']:,.0f} m²")
        if x.get("city"):
            bits.append(esc(x["city"]))
        if x.get("kind") == "land" and x.get("elevation") is not None:
            bits.append(f"⛰ {x['elevation']:,.0f} m")
        if x.get("status") == "needs_info":
            bits.append("❓")
        lines.append(" · ".join(bits) + f"\n   <a href=\"{esc(x['url'])}\">Ad</a> · "
                     f"<a href=\"{esc(deal_link(base, x, r))}\">Dashboard</a>")
    lines.append(f"\nRate them on your PC: {PRIVATE_DASHBOARD}")
    return "\n".join(lines)[:TELEGRAM_LIMIT]


# ------------------------------------------------------------------ weekly summary
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def weekly_text(results: dict, db, cfg: dict, researchers, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    n = cfg["notify"]
    since = now - timedelta(days=7)
    titles = _titles(researchers)
    lines = [f"🗓 <b>Your week</b> ({since:%d %b} – {now:%d %b})"]
    runs = [r for r in db.recent_runs(40) if (_parse_ts(r.get("started_at")) or now) >= since]
    bad = [r for r in runs if r.get("status") not in ("ok", "running")]
    new_ads = sum(r.get("new_listings") or 0 for r in runs)
    lines.append(f"Runs: {len(runs) - len(bad)} ok" + (f", {len(bad)} failed/interrupted" if bad else "")
                 + f" · {new_ads:,} new ads read")
    new_top = []
    for researcher, items in results.items():
        bar = threshold(n, researcher)
        k = [x for x in items if x.get("qualified") and x.get("score", 0) >= bar
             and (x.get("first_seen") or "") >= since.date().isoformat()]
        if k:
            new_top.append(f"{ICON.get(researcher, '🏠')} {esc(titles.get(researcher, researcher))}: {len(k)}")
    lines.append("New top deals: " + (" · ".join(new_top) if new_top else "none this week"))
    _ensure(db.conn)
    sent = db.conn.execute("SELECT why, COUNT(*) FROM alert_log WHERE sent_at >= ? GROUP BY why",
                           (since.isoformat(),)).fetchall()
    total = sum(c for _, c in sent)
    cuts = dict(sent).get("cut", 0)
    lines.append(f"Alerts sent: {total}" + (f" ({cuts} price cut{'s' if cuts != 1 else ''})" if cuts else ""))
    waiting = sorted(((x, r) for r, items in results.items() for x in items
                      if x.get("qualified") and x.get("my_vote") is None and x.get("status") != "pending"
                      and x.get("score", 0) >= threshold(n, r)),
                     key=lambda p: ((p[0].get("rating") or {}).get("rank", 9), -p[0]["score"]))
    seen, best = set(), []
    for x, r in waiting:
        if x["id"] not in seen:
            seen.add(x["id"])
            best.append((x, r))
    if best:
        base = dashboard_url(cfg)
        lines.append(f"\n<b>Waiting for your rating</b> ({len(best)}):")
        for x, r in best[:5]:
            rt = x.get("rating") or {}
            lines.append(f"{rt.get('emoji', '')} {x['score']} {ICON.get(r, '')} "
                         f"<a href=\"{esc(deal_link(base, x, r))}\">{esc((x.get('title') or 'listing')[:45])}</a>"
                         + (f" · {esc(eur(x['price']))}" if x.get("price") is not None else "")
                         + (f" · {esc(x['city'])}" if x.get("city") else ""))
    problems = source_problems(db, cfg, now)
    if problems:
        lines.append("\n⚠️ " + "\n⚠️ ".join(esc(p) for p in problems))
    lines.append(f"\nRate them on your PC: {PRIVATE_DASHBOARD}")
    return "\n".join(lines)[:TELEGRAM_LIMIT]


def send_weekly(results: dict, db, cfg: dict, researchers, now: datetime | None = None) -> bool:
    n = cfg["notify"]
    if not n.get("weekly_summary", True):
        return False
    now = now or datetime.now(timezone.utc)
    day = WEEKDAYS.index(str(n.get("weekly_day", "sun"))[:3].lower())
    if now.weekday() < day:                 # first run on or after that day, once per ISO week
        return False
    y, w, _ = now.isocalendar()
    return _once(db, f"week:{y}-W{w:02d}", weekly_text(results, db, cfg, researchers, now))
