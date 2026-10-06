"""Entry point:  python -m scraper.run [--full] [--no-scrape] [--max-pages N] [--detail-budget N]

1. Read search pages (all pages on a full sweep, only new ads otherwise).
2. Read detail pages for the most promising new listings.
3. Rebuild market medians, run the four researchers.
4. Write docs/data.json for the dashboard and send Telegram alerts.
"""
from __future__ import annotations

import argparse
import logging
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import export, feedback, notify
from .db import DB, now
from .http import Blocked, PoliteSession
from .market import Market, ppm2
from .researchers.common import days_listed
from .researchers import airbnb, flip, land, motivated
from .sources import reklama5
from .text import (KeywordSet, classify_deal, redact, detect_furnished, detect_utilities, is_abroad, land_type,
                   mentions_price_per_m2, needs_renovation, norm)

ROOT = Path(__file__).resolve().parent.parent
RESEARCHERS = [airbnb, land, flip, motivated]
log = logging.getLogger("scraper")


# ----------------------------------------------------------------- normalise
def price_and_deal(raw, kind: str, area, title: str, desc: str = "") -> tuple[float | None, str | None, str]:
    """Return (price, price_note, deal), sorting out how the price is meant.

    Sellers often type a price per m² ("1.400 €" for a flat) or a placeholder
    ("1 €"). Left alone, these become fake 99% bargains and fake rentals.
    """
    text = f"{title} {desc}"
    if raw is None:
        return None, None, classify_deal(title, desc, None, kind)
    if raw < 15:
        return None, "placeholder", classify_deal(title, desc, None, kind)
    if kind == "land":
        if raw < 400 or (raw < 3000 and mentions_price_per_m2(text)):
            return raw, "per_m2", classify_deal(title, desc, None, kind)
        return raw, None, classify_deal(title, desc, raw, kind)

    deal = classify_deal(title, desc, raw, kind)
    said_rent = classify_deal(title, desc, None, kind) == "rent"
    if deal == "sale" and raw < 6000:
        # Nobody sells a flat for €1,400 total: it's €/m², or meaningless.
        return (raw, "per_m2", deal) if (area and raw >= 150) else (None, "placeholder", deal)
    if deal == "rent" and not said_rent and area and raw / area > 18:
        # €1,200 for a 60 m² flat is a sale price per m², not a monthly rent.
        return raw, "per_m2", "sale"
    return raw, None, deal


def normalise_card(card: dict) -> dict:
    card["title"] = redact(card["title"])
    card["price_eur"], card["price_note"], card["deal"] = price_and_deal(
        card["price_eur"], card["kind"], card["area_m2"], card["title"])
    card["abroad"] = is_abroad(card["title"])
    return card


def detail_update(listing: dict, d: dict) -> dict:
    title = redact(d.get("title") or listing["title"])
    desc = redact(d.get("description"))
    text = "\n".join(filter(None, [title, desc, d.get("address")]))
    fields = d.get("fields") or {}
    area = d.get("area_m2") or listing.get("area_m2")
    raw = d.get("price_eur") if d.get("price_eur") is not None else listing.get("price_eur")
    price, note, deal = price_and_deal(raw, listing["kind"], area, title, desc)
    upd = {
        "title": title,
        "description": desc,
        "fields": fields,
        "price_eur": price,
        "price_note": note,
        "area_m2": area,
        "rooms": d.get("rooms") or listing.get("rooms"),
        "lat": d.get("lat"),
        "lng": d.get("lng"),
        "address": redact(d.get("address")),
        "agency": int(bool(d.get("agency"))),
        "deal": deal,
        "utilities": detect_utilities(text),
        "furnished": _bool_int(detect_furnished(fields.get("Опрема", "") + " " + desc)),
        "renovation": int(needs_renovation(text) or "реновир" in fields.get("Состојба", "").lower()),
        "land_type": land_type(fields.get("Тип на земјиште"), text) if listing["kind"] == "land" else None,
        "abroad": int(is_abroad(text)),
    }
    if d.get("city"):
        upd["city"] = d["city"]
    if d.get("district"):
        upd["district"] = d["district"]
    if d.get("posted"):
        upd["posted"] = d["posted"]
    return upd


def _bool_int(v):
    return None if v is None else int(v)


# ----------------------------------------------------------------- scraping
def expand_queries(cfg: dict) -> list[tuple[str, int, int]]:
    out = []
    for q in cfg["scraper"]["queries"]:
        if q["city"] == "each":
            out += [(f"{q['name']}/{name}", q["cat"], cid) for cid, name in reklama5.CITIES.items()]
        else:
            out.append((q["name"], q["cat"], int(q["city"])))
    return out


def scrape_lists(db: DB, http: PoliteSession, cfg: dict, full: bool, max_pages_override: int | None,
                 stats: dict) -> None:
    """Read search pages; counts go into `stats` so a crash midway keeps them."""
    sc = cfg["scraper"]
    mkd = cfg["market"]["mkd_per_eur"]
    for name, cat, city in expand_queries(cfg):
        max_pages = max_pages_override or (sc["full_sweep_max_pages"] if full else sc["max_pages"])
        known_streak = 0
        seen_in_query: set[str] = set()
        for page in range(1, max_pages + 1):
            try:
                html = http.get(reklama5.search_url(cat, city, page))
            except requests.RequestException as e:
                log.warning("%s page %d failed, skipping the rest of this query: %s", name, page, e)
                break
            stats["pages"] += 1
            cards = reklama5.parse_list(html, cat, mkd)
            # Past the last page the site repeats earlier results — stop there.
            unseen = {c["source_id"] for c in cards if not c["promoted"]} - seen_in_query
            if not cards or not unseen:
                break
            seen_in_query |= unseen
            # Newest first: once a whole page is older than the limit, the rest is too.
            ages = [days_listed(c) for c in cards if not c["promoted"] and c.get("posted")]
            if ages and min(ages) > sc["max_listing_age_days"]:
                break
            cards = [c for c in cards if not c.get("posted") or days_listed(c) <= sc["max_listing_age_days"]]
            ts = now()
            fresh = 0
            for card in cards:
                is_new, changed = db.upsert_card(normalise_card(card), ts)
                stats["new"] += is_new
                stats["changed"] += changed
                fresh += is_new and not card["promoted"]
            db.conn.commit()
            known_streak = 0 if fresh else known_streak + 1
            if not full and known_streak >= sc["stop_after_known_pages"]:
                break
        log.info("%-28s pages so far %4d  new %4d  price changes %3d", name, stats["pages"], stats["new"], stats["changed"])


def detail_priority(l: dict, cfg: dict, market: Market, land_kw: KeywordSet, urgent_kw: KeywordSet) -> float:
    """How likely is this listing to matter? Only >0 gets its detail page read."""
    title = norm(l.get("title"))
    p = 0.0
    if l["kind"] in ("apartment", "house") and l["deal"] == "rent" and l.get("city") == cfg["airbnb"]["city"]:
        if l.get("price_eur") and cfg["airbnb"]["min_rent"] <= l["price_eur"] <= cfg["airbnb"]["max_rent"]:
            p = max(p, 3)
    if l["kind"] == "apartment" and l["deal"] == "short_term" and l.get("city") == cfg["airbnb"]["city"]:
        p = max(p, 1.5)   # real nightly prices calibrate the Airbnb researcher
    if l["kind"] == "land" and l["deal"] == "sale":
        p = max(p, 3 if (l.get("city") in cfg["land"]["cities"] or land_kw.any(title)) else 0.5)
    if l["kind"] in ("apartment", "house", "weekend_house") and l["deal"] == "sale":
        v = ppm2(l)
        ref, _, _ = market.reference(l["kind"], "sale", l.get("city"), l.get("district"))
        if v and ref and v < ref * (1 - cfg["flip"]["min_discount"] + 0.05):
            p = max(p, 2.5)
        elif l["kind"] == "weekend_house":
            p = max(p, 0.5)
    if l["deal"] == "sale" and (urgent_kw.any(title) or (l.get("site_old_price") or 0) > (l.get("price_eur") or 0)):
        p = max(p, 2)
    return p


def fetch_details(db: DB, http: PoliteSession, cfg: dict, budget: int) -> int:
    market = Market(db.market_rows(cfg["market"]["window_days"]))
    land_kw = KeywordSet(cfg["land"]["mountain_keywords"])
    urgent_kw = KeywordSet(cfg["motivated"]["urgent_keywords"])
    queue = [(detail_priority(l, cfg, market, land_kw, urgent_kw), l) for l in db.pending_details()]
    max_age = cfg["scraper"]["max_listing_age_days"]
    queue = [x for x in queue if x[0] > 0 and days_listed(x[1]) <= max_age]
    queue.sort(key=lambda x: x[1]["first_seen"], reverse=True)   # newest first...
    queue.sort(key=lambda x: -x[0])                               # ...within each priority
    log.info("detail queue: %d listings worth reading, budget %d", len(queue), budget)
    done = 0
    for _, l in queue[:budget]:
        try:
            html = http.get(l["url"])
        except requests.RequestException as e:
            log.warning("detail %s failed: %s", l["url"], e)
            continue
        if not html:          # ad was removed
            continue
        d = reklama5.parse_detail(html, cfg["market"]["mkd_per_eur"])
        db.apply_detail(l["id"], detail_update(l, d), now())
        done += 1
        if done % 25 == 0:
            db.conn.commit()
            log.info("  details %d/%d", done, min(budget, len(queue)))
    db.conn.commit()
    return done


# ----------------------------------------------------------------- analysis
def analyse(db: DB, cfg: dict) -> tuple[dict, Market]:
    m = cfg["market"]
    market = Market(db.market_rows(m["window_days"]), m["min_samples_district"], m["min_samples_city"],
                    m["min_samples_national"])
    max_age = cfg["scraper"]["max_listing_age_days"]
    listings = [l for l in db.active(cfg["scraper"]["stale_after_days"])
                if l["deal"] != "wanted" and days_listed(l) <= max_age]
    results = {}
    for r in RESEARCHERS:
        if cfg[r.NAME].get("enabled", True):
            results[r.NAME] = r.run(listings, market, cfg)
            log.info("researcher %-10s %4d deals (%d qualified)", r.NAME, len(results[r.NAME]),
                     sum(1 for x in results[r.NAME] if x["qualified"]))
    return results, market


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--full", action="store_true", help="read every page (default: automatic, once a day)")
    ap.add_argument("--quick", action="store_true", help="only new ads, even if a full sweep is due")
    ap.add_argument("--no-scrape", action="store_true", help="skip the network; re-run analysis + export only")
    ap.add_argument("--max-pages", type=int, help="cap pages per query (testing)")
    ap.add_argument("--detail-budget", type=int, help="override detail pages per run")
    ap.add_argument("--config", default=str(ROOT / "config.toml"))
    ap.add_argument("--db", default=str(ROOT / "data" / "deals.db"))
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S", stream=sys.stdout)
    cfg = tomllib.loads(Path(args.config).read_text(encoding="utf-8"))
    notify.load_settings()
    sc = cfg["scraper"]
    db = DB(args.db)
    first_run = not db.has_completed_run()     # backfill until one run has succeeded
    full = args.full or first_run or (
        not args.quick and datetime.now(timezone.utc).hour in sc.get("full_sweep_hours_utc", []))

    run_id = db.start_run()
    status, message, stats, details = "ok", "", {"pages": 0, "new": 0, "changed": 0}, 0
    if not args.no_scrape:
        http = PoliteSession(sc["delay_seconds"], sc["jitter_seconds"], sc["timeout_seconds"])
        log.info("run #%d — %s sweep", run_id, "full" if full else "quick")
        try:
            scrape_lists(db, http, cfg, full, args.max_pages, stats)
            budget = args.detail_budget or (sc["backfill_detail_budget"] if first_run else sc["detail_budget"])
            details = fetch_details(db, http, cfg, budget)
        except Blocked as e:
            status, message = "blocked", str(e)
            log.error("STOPPED — the site is refusing automated requests: %s", e)
        except Exception as e:     # still analyse + publish what we have
            status, message = "error", f"{type(e).__name__}: {e}"[:300]
            log.exception("run failed — publishing what was collected")
        db.prune(sc["prune_after_days"])

    results, market = analyse(db, cfg)
    db.finish_run(run_id, status=status, pages=stats["pages"], details=details, new_listings=stats["new"],
                  price_changes=stats["changed"], message=message)
    votes = feedback.load_votes()
    export.write(ROOT / "docs" / "data.json", feedback.apply(results, votes, personalize=False), market, db, cfg,
                 RESEARCHERS)
    notify.send(feedback.apply(results, votes), db, cfg, RESEARCHERS)
    if status != "ok":
        notify.send_status(
            f"⚠️ MK Deal Finder run #{run_id} {status.upper()} after {stats['pages']} pages.\n{message}\n\n"
            + ("The site is refusing automated requests; the scraper stopped instead of forcing its way in. "
               "It will try again at the next scheduled time." if status == "blocked"
               else "Data collected before the error was still published. Details: data\\local-run.log"))
    db.close()
    log.info("done: %s", status)
    return 0 if status == "ok" else 2


if __name__ == "__main__":
    sys.exit(main())
