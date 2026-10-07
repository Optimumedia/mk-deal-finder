"""Clean-up applied to listings before the researchers see them.

1. Real age: Reklama5 ad numbers grow steadily (~975 a day), so an ad that
   was "renewed" yesterday but carries an old number is really old.
2. Shared pins: a coordinate used by many ads is a map default, not a house.
3. Duplicates: the same property advertised by several agencies or reposted
   becomes one listing; the copies are counted ("also advertised 3×").
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from .market import total_price


def add_real_age(listings: list[dict], ids_per_day: float) -> None:
    ids = [int(l["source_id"]) for l in listings if l.get("source") == "reklama5" and l["source_id"].isdigit()]
    if not ids:
        return
    newest = max(ids)
    for l in listings:
        if l.get("source") == "reklama5" and l["source_id"].isdigit():
            l["id_age_days"] = max(0, int((newest - int(l["source_id"])) / ids_per_day))


def drop_shared_pins(listings: list[dict], max_share: int = 5) -> None:
    pins = Counter((round(l["lat"], 4), round(l["lng"], 4)) for l in listings if l.get("lat") and l.get("lng"))
    for l in listings:
        if l.get("lat") and l.get("lng") and pins[(round(l["lat"], 4), round(l["lng"], 4))] > max_share:
            l["lat"] = l["lng"] = None


def _words(title: str | None) -> set[str]:
    return {w for w in re.findall(r"\w{3,}", (title or "").lower())}


def _same(a: dict, b: dict) -> bool:
    if a.get("district") and b.get("district") and a["district"] != b["district"]:
        return False
    if a.get("source") != b.get("source"):
        # Same type, city, price (±1%) and size (±3%) on two sites is one property —
        # except rents, which cluster on round numbers: those need the same district too.
        return a["deal"] == "sale" or bool(a.get("district") and a.get("district") == b.get("district"))
    if a.get("image") and a.get("image") == b.get("image"):
        return True
    # Big sale prices rarely collide by chance; rents and round numbers do,
    # so those also need similar titles.
    if a["deal"] == "sale" and (total_price(a) or 0) >= 30000 and a.get("district") and b.get("district"):
        return True                     # same district, same price and size: one property
    wa, wb = _words(a.get("title")), _words(b.get("title"))
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= 0.5


def dedupe(listings: list[dict]) -> list[dict]:
    """Merge listings with the same type, deal and city, price within 1% and
    area within 3% (plus the checks in _same). Keeps the copy that has a
    detail page, else the oldest ad."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    passthrough = []
    for l in listings:
        p, a = total_price(l), l.get("area_m2")
        if l.get("source") == "kirsm" or not p or not a:      # auctions are never duplicates of ads
            passthrough.append(l)
        else:
            groups[(l["kind"], l["deal"], l.get("city"))].append(l)

    out = passthrough
    for items in groups.values():
        items.sort(key=total_price)
        parent = list(range(len(items)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i, a in enumerate(items):
            pa = total_price(a)
            for j in range(i + 1, len(items)):
                b = items[j]
                if total_price(b) > pa * 1.01:
                    break
                if abs(a["area_m2"] - b["area_m2"]) <= 0.03 * a["area_m2"] and _same(a, b):
                    parent[find(j)] = find(i)
        clusters: dict[int, list[dict]] = defaultdict(list)
        for i, l in enumerate(items):
            clusters[find(i)].append(l)
        for members in clusters.values():
            # Prefer the copy whose details were read, then Reklama5 (more fields), then the oldest ad.
            keep = min(members, key=lambda l: (l.get("detail_at") is None, l.get("source") != "reklama5",
                                               int(l["source_id"]) if l["source_id"].isdigit() else 0))
            if len(members) > 1:
                keep = dict(keep)
                keep["duplicates"] = len(members) - 1
                ages = [m.get("id_age_days") for m in members if m.get("id_age_days") is not None]
                if ages:
                    keep["id_age_days"] = max(ages)      # the oldest copy proves how long it's been for sale
            out.append(keep)
    return out
