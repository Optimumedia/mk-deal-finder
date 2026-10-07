from __future__ import annotations

import math
from datetime import date

from ..db import days_ago

from .. import maps
from ..market import ppm2, total_price
from ..text import detect_utilities


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def haversine_km(lat1, lng1, lat2, lng2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def text_of(listing: dict) -> str:
    parts = [listing.get("title"), listing.get("description"), listing.get("address"), listing.get("district")]
    return "\n".join(p for p in parts if p)


def utilities_status(listing: dict, urban_assumed: bool) -> dict:
    """Electricity / water / road status for a listing.

    Each value: "yes" (mentioned in the ad), "no" (ad says it's missing),
    "assumed" (built property in a town — connected by default) or "unknown".
    """
    found = listing.get("utilities") or detect_utilities(text_of(listing))
    out = {}
    for k in ("electricity", "water", "road"):
        v = found.get(k)
        out[k] = "yes" if v is True else "no" if v is False else ("assumed" if urban_assumed else "unknown")
    return out


def utilities_ok(status: dict, strict: bool) -> bool:
    if any(v == "no" for v in status.values()):
        return False
    if strict:
        return all(v == "yes" for v in status.values())
    return True


def days_listed(listing: dict) -> int:
    start = listing.get("posted") or listing.get("first_seen", "")[:10]
    try:
        return (date.today() - date.fromisoformat(start[:10])).days
    except ValueError:
        return 0


def base(listing: dict) -> dict:
    """Fields every researcher result shares (what the dashboard shows)."""
    return {
        "id": listing["id"],
        "url": listing["url"],
        "title": listing.get("title"),
        "image": listing.get("image"),
        "kind": listing.get("kind"),
        "city": listing.get("city"),
        "district": listing.get("district"),
        "map_query": maps.query(listing),   # Google Maps search when there's no GPS
        # Altitude (m). Approximate when taken from the village / area, not the exact pin.
        "elevation": round(listing["elevation"]) if listing.get("elevation") is not None else None,
        "elevation_approx": (listing.get("elevation_src") or "").startswith("place:"),
        "elevation_place": (listing.get("elevation_src") or "")[6:] or None
                           if (listing.get("elevation_src") or "").startswith("place:") else None,
        "lat": listing.get("lat"),
        "lng": listing.get("lng"),
        "price": round(total_price(listing)) if total_price(listing) else None,
        "area": round(listing["area_m2"], 1) if listing.get("area_m2") else None,
        "ppm2": round(ppm2(listing), 1) if ppm2(listing) else None,
        "rooms": listing.get("rooms"),
        "agency": bool(listing.get("agency")),
        "posted": listing.get("posted"),
        "first_seen": (listing.get("first_seen") or "")[:10],
        "days_listed": days_listed(listing),
        "has_details": listing.get("detail_at") is not None,
        "is_new": (listing.get("first_seen") or "") >= days_ago(1),
    }
