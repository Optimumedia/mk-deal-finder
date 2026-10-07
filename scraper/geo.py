"""Altitude and distance to Skopje / Ohrid for land, houses and weekend houses.

Free services from Open-Meteo, no API key (fine for this personal, low-volume use):
- elevation API for exact GPS points (up to 100 per request);
- place search (geocoding) for ads without GPS: the village / area named in
  the ad, then the neighbourhood, then the town; its altitude is marked
  approximate ("~").
- driving distance and time to Skopje (Macedonia Square) and Ohrid (centre)
  from the public OpenStreetMap routing service (OSRM), many places per
  request, politely spaced.
Every answer is cached in deals.db, so each place is looked up only once.
"""
from __future__ import annotations

import logging
import re
import time

import requests

from . import maps
from .text import norm

log = logging.getLogger(__name__)
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
ROUTE_URL = "https://router.project-osrm.org/table/v1/driving/"
CITIES = {"skopje": (41.9961, 21.4317), "ohrid": (41.1172, 20.8016)}     # Macedonia Square, Ohrid centre
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
KINDS = ("land", "house", "weekend_house")

# Latin → Cyrillic for place names typed in Latin ("vo Kuckovo" → "Кучково").
_LAT = [("dzh", "џ"), ("dz", "џ"), ("zh", "ж"), ("ch", "ч"), ("sh", "ш"), ("kj", "ќ"), ("gj", "ѓ"), ("lj", "љ"), ("nj", "њ"),
        ("a", "а"), ("b", "б"), ("c", "ц"), ("č", "ч"), ("ć", "ќ"), ("d", "д"), ("e", "е"), ("f", "ф"), ("g", "г"), ("h", "х"),
        ("i", "и"), ("j", "ј"), ("k", "к"), ("l", "л"), ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"), ("r", "р"), ("s", "с"),
        ("š", "ш"), ("t", "т"), ("u", "у"), ("v", "в"), ("z", "з"), ("ž", "ж")]
# "плац во Кучково", "plac vo s.Orman", "с. Велгошти"
_PLACE_IN_TITLE = re.compile(r"(?:\bво|\bvo|\bс\.|\bs\.|\bсело|\bselo|\bнас\.|\bnas\.|\bнаселба|\bnaselba)\s*"
                             r"(?:с\.|s\.|село|selo)?\s*([A-ZА-ШЃЌЅЈЉЊЏŠŽČĆa-zа-шѓќѕјљњџšžčć][\w\-ЃЌЅЈЉЊЏѓќѕјљњџšžčć]{2,})",
                             re.I)
_STOP = {"центар", "centar", "град", "grad", "близина", "blizina", "скопје", "skopje", "плац", "plac", "одлична",
         "odlicna", "населба", "naselba", "викенд", "vikend", "куќа", "kuka", "kukja", "стан", "stan", "продажба"}


def to_cyrillic(word: str) -> str:
    w = word.lower()
    if re.search(r"[а-шѓќѕјљњџ]", w):
        return word
    for a, b in _LAT:
        w = w.replace(a, b)
    return w[:1].upper() + w[1:]


def place_candidates(listing: dict) -> list[str]:
    """Names to try, most specific first."""
    out = []
    title = listing.get("title") or ""
    m = _PLACE_IN_TITLE.search(title)
    if m and norm(m.group(1)).strip() not in _STOP:
        out.append(to_cyrillic(m.group(1)))
    # Other capitalised words in the title ("…во близина на Охрид, Мешеишта").
    for w in re.findall(r"\b([A-ZА-ШЃЌЅЈЉЊЏ][a-zа-шѓќѕјљњџšžčć]{3,})\b", title)[:4]:
        if norm(w).strip() not in _STOP and w != listing.get("city"):
            out.append(to_cyrillic(w))
    ko = maps.cadastral_area((listing.get("extra") or {}).get("note"))
    if ko:
        out.append(ko)
    address = (listing.get("address") or "").strip()
    if address and len(address.split()) <= 3 and not re.search(r"\d", address):
        out.append(to_cyrillic(address))
    district = listing.get("district")
    if district:
        out.append(district.replace("Скопје ", ""))
    if listing.get("city"):
        out.append(listing["city"])
    return list(dict.fromkeys(x for x in out if x))


class Geo:
    def __init__(self, db, delay: float = 0.4):
        self.db, self.delay = db, delay
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "mk-deal-finder/1.0 (personal property research)"
        self.calls = 0
        db.conn.execute("""CREATE TABLE IF NOT EXISTS geo_cache (
            query TEXT PRIMARY KEY, lat REAL, lng REAL, elevation REAL, name TEXT, at TEXT)""")

    def _get(self, url: str, params: dict) -> dict | None:
        time.sleep(self.delay)
        self.calls += 1
        try:
            r = self.s.get(url, params=params, timeout=20)
            return r.json() if r.ok else None
        except (requests.RequestException, ValueError) as e:
            log.warning("geo request failed: %s", e)
            return None

    def place(self, name: str) -> tuple | None:
        """(lat, lng, elevation, found name) for a place in North Macedonia, cached."""
        key = "place:" + name.lower()
        row = self.db.conn.execute("SELECT lat, lng, elevation, name FROM geo_cache WHERE query=?", (key,)).fetchone()
        if row:
            return tuple(row) if row[0] is not None else None
        hit = None
        # Cyrillic first, then Latin ("Илинден" isn't indexed, "Ilinden" is).
        for q in dict.fromkeys([name, norm(name).strip().title()]):
            data = self._get(GEOCODE_URL, {"name": q, "count": 5, "language": "mk", "countryCode": "MK"}) or {}
            # Towns and villages only — never a peak or landmark of the same name
            # ("Ilinden" is also a 2,511 m summit).
            hit = next((x for x in data.get("results") or []
                        if str(x.get("feature_code", "")).startswith(("PPL", "ADM")) and x.get("elevation") is not None),
                       None)
            if hit:
                break
        from .db import now
        if hit:
            val = (hit["latitude"], hit["longitude"], hit["elevation"], hit.get("name"))
        else:
            val = (None, None, None, None)
        self.db.conn.execute("INSERT OR REPLACE INTO geo_cache VALUES (?,?,?,?,?,?)", (key, *val, now()))
        return val if val[0] is not None else None

    def points(self, coords: list[tuple[float, float]]) -> list[float | None]:
        """Elevation for exact GPS points (batches of 100)."""
        out: list[float | None] = []
        for i in range(0, len(coords), 100):
            chunk = coords[i:i + 100]
            data = self._get(ELEVATION_URL, {"latitude": ",".join(f"{a:.5f}" for a, _ in chunk),
                                             "longitude": ",".join(f"{b:.5f}" for _, b in chunk)}) or {}
            el = data.get("elevation") or [None] * len(chunk)
            out += [float(x) if x is not None else None for x in el]
        return out


def enrich(db, cfg: dict) -> int:
    """Fill elevation for active land / houses / weekend houses that don't have it yet."""
    c = cfg.get("geo", {})
    if not c.get("enabled", True):
        return 0
    from .db import days_ago
    rows = db.conn.execute(
        f"""SELECT id, title, address, district, city, lat, lng, extra FROM listings
            WHERE kind IN ({','.join('?' * len(KINDS))}) AND abroad = 0 AND last_seen >= ?
              AND ((elevation IS NULL AND COALESCE(elevation_src, '') != 'none')      -- never looked up
                   OR (lat IS NOT NULL AND COALESCE(elevation_src, '') != 'gps'))     -- GPS arrived: upgrade
            ORDER BY (kind = 'land') DESC, first_seen DESC""", (*KINDS, days_ago(cfg["scraper"]["stale_after_days"]))
    ).fetchall()
    geo = Geo(db)
    done = 0
    gps = [r for r in rows if r["lat"] and r["lng"]]
    for r, el in zip(gps, geo.points([(r["lat"], r["lng"]) for r in gps])):
        if el is not None:
            # exact pin: its altitude, and recompute routes from the pin instead of the village
            db.conn.execute("UPDATE listings SET elevation=?, elevation_src='gps', skopje_km=NULL, skopje_min=NULL, "
                            "ohrid_km=NULL, ohrid_min=NULL WHERE id=?", (el, r["id"]))
            done += 1
    import json
    for r in (r for r in rows if not (r["lat"] and r["lng"])):
        if geo.calls >= c.get("max_lookups_per_run", 400):
            break
        listing = dict(r)
        listing["extra"] = json.loads(r["extra"]) if r["extra"] else {}
        for name in place_candidates(listing):
            hit = geo.place(name)
            if hit:
                db.conn.execute("UPDATE listings SET elevation=?, elevation_src=?, approx_lat=?, approx_lng=? WHERE id=?",
                                (hit[2], "place:" + (hit[3] or name), hit[0], hit[1], r["id"]))
                done += 1
                if done % 25 == 0:
                    db.conn.commit()        # don't hold the database lock for the whole step
                break
        else:
            db.conn.execute("UPDATE listings SET elevation_src='none' WHERE id=?", (r["id"],))
    db.conn.commit()
    log.info("altitude: %d listings filled (%d lookups)", done, geo.calls)
    routes(db, cfg, geo)
    return done


def routes(db, cfg: dict, geo: "Geo | None" = None) -> int:
    """Driving km / minutes to Skopje and Ohrid, for listings with a location
    (exact pin or village). Places are rounded to ~100 m and cached."""
    c = cfg.get("geo", {})
    geo = geo or Geo(db)
    db.conn.execute("""CREATE TABLE IF NOT EXISTS route_cache (
        key TEXT PRIMARY KEY, skopje_km REAL, skopje_min REAL, ohrid_km REAL, ohrid_min REAL)""")
    rows = db.conn.execute(
        f"""SELECT id, COALESCE(lat, approx_lat) AS la, COALESCE(lng, approx_lng) AS lo FROM listings
            WHERE kind IN ({','.join('?' * len(KINDS))}) AND skopje_km IS NULL AND abroad = 0
              AND COALESCE(lat, approx_lat) IS NOT NULL""", KINDS).fetchall()
    by_key: dict[str, list] = {}
    for r in rows:
        by_key.setdefault(f"{r['la']:.3f},{r['lo']:.3f}", []).append(r["id"])
    cached = {k: tuple(v) for k, *v in db.conn.execute("SELECT * FROM route_cache")}
    todo = [k for k in by_key if k not in cached]
    batch, requests_made = c.get("routes_per_request", 90), 0
    for i in range(0, len(todo), batch):
        if requests_made >= c.get("max_route_requests_per_run", 10):
            break
        keys = todo[i:i + batch]
        pts = [CITIES["skopje"], CITIES["ohrid"]] + [tuple(map(float, k.split(","))) for k in keys]
        url = ROUTE_URL + ";".join(f"{lo:.5f},{la:.5f}" for la, lo in pts)
        time.sleep(1.5)                                     # public demo server: be gentle
        requests_made += 1
        try:
            data = geo.s.get(url, params={"sources": ";".join(str(j) for j in range(2, len(pts))),
                                          "destinations": "0;1", "annotations": "duration,distance"}, timeout=40).json()
        except (requests.RequestException, ValueError) as e:
            log.warning("routing failed: %s", e)
            break
        if data.get("code") != "Ok":
            log.warning("routing: %s", data.get("message") or data.get("code"))
            break
        for k, dur, dist in zip(keys, data["durations"], data["distances"]):
            val = tuple(round(x / div, 1) if x is not None else None
                        for x, div in ((dist[0], 1000), (dur[0], 60), (dist[1], 1000), (dur[1], 60)))
            db.conn.execute("INSERT OR REPLACE INTO route_cache VALUES (?,?,?,?,?)", (k, *val))
            cached[k] = val
    n = 0
    for k, ids in by_key.items():
        if k in cached:
            db.conn.executemany("UPDATE listings SET skopje_km=?, skopje_min=?, ohrid_km=?, ohrid_min=? WHERE id=?",
                                [(*cached[k], lid) for lid in ids])
            n += len(ids)
    db.conn.commit()
    log.info("distances: %d listings filled (%d routing requests)", n, requests_made)
    return n
