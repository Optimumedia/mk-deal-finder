"""Entry point:  python -m scraper.run [--full] [--no-scrape] [--max-pages N] [--detail-budget N]

1. Read search pages (all pages on a full sweep, only new ads otherwise).
2. Read detail pages for the most promising new listings.
3. Rebuild market medians, run the four researchers.
4. Write docs/data.json for the dashboard and send Telegram alerts.
"""
from __future__ import annotations

import argparse
import re
import logging
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import clean, export, feedback, needs_info, notify, places, rating
from .db import DB, now
from .http import Blocked, PoliteSession
from .market import Market, ppm2
from .researchers.common import days_listed
from .researchers import airbnb, auctions, flip, land, motivated
from .sources import kirsm, nedviznosti, novel, reklama5

# Which parser reads a listing's detail page.
DETAIL_PARSERS = {reklama5.SOURCE: reklama5.parse_detail, novel.SOURCE: novel.parse_detail}
from .text import (KeywordSet, classify_deal, redact, detect_furnished, detect_utilities, is_abroad, land_type,
                   mentions_price_per_m2, needs_renovation, norm)

ROOT = Path(__file__).resolve().parent.parent
RESEARCHERS = [airbnb, land, flip, motivated, auctions]
log = logging.getLogger("scraper")


# ----------------------------------------------------------------- normalise
# 1111, 9999, 11111111, 1234, 123456 … — typed to get past the form, not prices.
_PLACEHOLDER = re.compile(r"^(\d)\1{3,}$|^9{3,}$|^1234(5(6(7(8)?)?)?)?$")


def price_and_deal(raw, kind: str, area, title: str, desc: str = "") -> tuple[float | None, str | None, str]:
    """Return (price, price_note, deal), sorting out how the price is meant.

    Sellers often type a price per m² ("1.400 €" for a flat) or a placeholder
    ("1 €"). Left alone, these become fake 99% bargains and fake rentals.
    """
    text = f"{title} {desc}"
    if raw is None:
        return None, None, classify_deal(title, desc, None, kind)
    if raw < 15 or _PLACEHOLDER.match(f"{raw:.0f}"):
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
    # "116 m² (35 m² inside + 81 m² terrace)": the interior is what's worth money.
    m = re.search(r"(?:vnatres[a-z]*|stanben[a-z]*|neto|korisn[a-z]*) (?:prostor|povrsin[a-z]*)[^0-9]{0,15}(\d+)",
                  norm(desc))
    if m and area and 0.35 * area <= float(m.group(1)) < 0.95 * area:
        area = float(m.group(1))
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


def scrape_kirsm(db: DB, http: PoliteSession, cfg: dict, stats: dict) -> None:
    """Bailiff sales: read pages until one holds only sales that are already over."""
    c = cfg.get("auctions", {})
    if not c.get("enabled", True):
        return
    mkd = cfg["market"]["mkd_per_eur"]
    seen = 0
    for page in range(1, c.get("max_pages", 30) + 1):
        try:
            html = http.get(kirsm.search_url(page))
        except requests.RequestException as e:
            log.warning("kirsm page %d failed: %s", page, e)
            break
        stats["pages"] += 1
        items = kirsm.parse_list(html, mkd)
        if not items:
            break
        ts = now()
        for it in items:
            is_new, changed = db.upsert_card(it, ts)
            stats["new"] += is_new
            stats["changed"] += changed
            db.apply_detail(f"{kirsm.SOURCE}:{it['source_id']}",
                            {k: it[k] for k in ("auction_date", "auction_round", "extra", "kind", "deal")}, ts)
            seen += 1
        db.conn.commit()
        if kirsm.all_past(items):
            break
    log.info("%-28s pages so far %4d  bailiff sales read %d", "auctions (KIRSM)", stats["pages"], seen)


def scrape_novel(db: DB, http: PoliteSession, cfg: dict, full: bool, stats: dict) -> None:
    """novelestate.com (big Skopje agency, mostly rentals). Pages aren't sorted
    by date, so the daily full sweep reads everything; other runs skim."""
    c = cfg.get("novel", {})
    if not c.get("enabled", True):
        return
    mkd = cfg["market"]["mkd_per_eur"]
    for deal, kind in (("rent", "apartment"), ("rent", "house"), ("sale", "apartment"), ("sale", "house"),
                       ("sale", "land")):
        seen: set[str] = set()
        for page in range(1, (c.get("full_max_pages", 110) if full else c.get("quick_pages", 3)) + 1):
            try:
                html = http.get(novel.search_url(deal, kind, page))
            except requests.RequestException as e:
                log.warning("novel %s/%s page %d failed: %s", deal, kind, page, e)
                break
            stats["pages"] += 1
            cards = novel.parse_list(html, mkd)
            unseen = {c["source_id"] for c in cards} - seen
            if not cards or not unseen:
                break
            seen |= unseen
            ts = now()
            for card in cards:
                card = normalise_card(card)
                card["deal"] = deal          # the list type is authoritative
                is_new, changed = db.upsert_card(card, ts)
                stats["new"] += is_new
                stats["changed"] += changed
            db.conn.commit()
    log.info("%-28s pages so far %4d  new %4d", "novelestate.com", stats["pages"], stats["new"])


def scrape_nedviznosti(db: DB, http: PoliteSession, cfg: dict, stats: dict, first_run: bool) -> None:
    """nedviznosti.com.mk via its sitemap (robots.txt forbids paginated search).
    Only pages whose <lastmod> changed are fetched, capped per run. Their terms
    forbid redistributing content: descriptions stay in the private database
    and photos are not stored, so neither reaches the public dashboard."""
    c = cfg.get("nedviznosti", {})
    if not c.get("enabled", True):
        return
    try:
        entries = nedviznosti.parse_sitemap(http.get(nedviznosti.SITEMAP_URL))
    except requests.RequestException as e:
        log.warning("nedviznosti sitemap failed: %s", e)
        return
    stats["pages"] += 1
    lastmods = dict(entries)
    todo = nedviznosti.changed_since(entries, db.sitemap_known())
    todo.sort(key=lambda u: lastmods.get(u) or "", reverse=True)          # newest changes first
    cap = c.get("backfill_per_run", 250) if first_run else c.get("per_run", 60)
    mkd = cfg["market"]["mkd_per_eur"]
    done = 0
    for url in todo[:cap]:
        try:
            html = http.get(url)
        except requests.RequestException as e:
            log.warning("nedviznosti %s failed: %s", url, e)
            continue
        stats["pages"] += 1
        db.sitemap_seen(url, lastmods.get(url))
        if not html:
            continue
        d = nedviznosti.parse_listing(html, url, mkd)
        deal = d.get("deal")
        card = normalise_card({**d, "image": None})     # no photos: their terms forbid redistribution
        if deal in ("sale", "rent", "short_term"):
            card["deal"] = deal                          # the site's own category is authoritative
        ts = now()
        is_new, changed = db.upsert_card(card, ts)
        stats["new"] += is_new
        stats["changed"] += changed
        db.apply_detail(f"{nedviznosti.SOURCE}:{card['source_id']}", {**detail_update(card, d), "deal": card["deal"]}, ts)
        done += 1
        if done % 25 == 0:
            db.conn.commit()
    db.conn.commit()
    log.info("%-28s pages so far %4d  %d changed listings read (%d waiting)", "nedviznosti.com.mk",
             stats["pages"], done, max(0, len(todo) - cap))


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
        in_region = l.get("city") in cfg["land"]["cities"] or land_kw.any(title)
        # Land cards carry no area or utilities — the detail page is everything.
        p = max(p, (3.5 if l.get("price_eur") else 2.5) if in_region else 0.5)
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
    # Highest priority first; within a priority the most recently posted ads
    # (first_seen is misleading on a backfill: later pages = older ads).
    queue.sort(key=lambda x: (-x[0], days_listed(x[1])))
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
        parse = DETAIL_PARSERS.get(l["source"])
        if parse is None:
            continue
        d = parse(html, cfg["market"]["mkd_per_eur"])
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
    rows = db.market_rows(m["window_days"])
    places.fix_districts(rows)
    market = Market(clean.dedupe(rows), m["min_samples_district"], m["min_samples_city"], m["min_samples_national"])
    max_age = cfg["scraper"]["max_listing_age_days"]
    listings = [l for l in db.active(cfg["scraper"]["stale_after_days"])
                if l["deal"] != "wanted" and (l["deal"] == "auction" or days_listed(l) <= max_age)]
    clean.add_real_age(listings, cfg["scraper"]["reklama5_ids_per_day"])
    clean.drop_shared_pins(listings)
    moved = places.fix_districts(listings)
    before = len(listings)
    listings = clean.dedupe(listings)
    facts, confirmed = feedback.load_overrides()
    n = feedback.apply_overrides(listings, facts, confirmed)
    if n or confirmed:
        log.info("seller info applied to %d listings (%d confirmed by you)", n, len(confirmed))
    log.info("clean-up: %d districts corrected from titles, %d duplicate ads merged", moved, before - len(listings))
    results = {}
    for r in RESEARCHERS:
        if cfg[r.NAME].get("enabled", True):
            results[r.NAME] = r.run(listings, market, cfg)
            log.info("researcher %-10s %4d deals (%d qualified)", r.NAME, len(results[r.NAME]),
                     sum(1 for x in results[r.NAME] if x["qualified"]))
    by_id = {l["id"]: l for l in listings}
    return needs_info.annotate(results, by_id, cfg), market, by_id


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
            scrape_kirsm(db, http, cfg, stats)
            scrape_novel(db, http, cfg, full, stats)
            scrape_nedviznosti(db, http, cfg, stats, first_run)
            budget = args.detail_budget or (sc["backfill_detail_budget"] if first_run else sc["detail_budget"])
            details = fetch_details(db, http, cfg, budget)
        except Blocked as e:
            status, message = "blocked", str(e)
            log.error("STOPPED — the site is refusing automated requests: %s", e)
        except Exception as e:     # still analyse + publish what we have
            status, message = "error", f"{type(e).__name__}: {e}"[:300]
            log.exception("run failed — publishing what was collected")
        db.prune(sc["prune_after_days"])

    results, market, by_id = analyse(db, cfg)
    db.finish_run(run_id, status=status, pages=stats["pages"], details=details, new_listings=stats["new"],
                  price_changes=stats["changed"], message=message)
    votes = feedback.load_votes()
    public = needs_info.public(rating.rate_all(feedback.apply(results, votes, personalize=False)))
    export.write(ROOT / "docs" / "data.json", public, market, db, cfg, RESEARCHERS)
    private = feedback.apply(results, votes, rejections=feedback.load_rejections(), listings=by_id)
    notify.send(rating.rate_all(private), db, cfg, RESEARCHERS)
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
