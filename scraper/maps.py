"""Google Maps links for a deal — free, no API key (Google's "Maps URLs").

Exact pin when the ad has GPS; otherwise a search for the address /
neighbourhood / town, which lands in the right area but not on the building.
"""
from __future__ import annotations

import re
from urllib.parse import quote_plus

COUNTRY = "North Macedonia"
_PREFIX = {"долно", "горно", "ново", "старо", "мало", "големо", "долни", "горни", "нова", "стара", "мали", "големи"}


def cadastral_area(note: str | None) -> str | None:
    """'ИЛ бр.416 КО ГРУПЧИН Продажбата…' → 'Групчин'; 'КО Долно Лисиче-вон град' → 'Долно Лисиче'."""
    m = re.search(r"\bКО\s+(.+)", note or "")
    if not m:
        return None
    words = [w for w in re.split(r"[\s,.;:()–-]+", m.group(1)) if w]
    if not words or not words[0][0].isalpha():
        return None
    name = [words[0]]
    if words[0].lower() in _PREFIX and len(words) > 1:
        name.append(words[1])
    return " ".join(w.capitalize() for w in name)


def query(listing: dict) -> str | None:
    """Best text location for a listing without GPS: address → area → neighbourhood → town."""
    city = listing.get("city")
    district = listing.get("district")
    parts: list[str] = []
    address = (listing.get("address") or "").strip()
    if address and not re.fullmatch(r"[\d\s.,/-]*", address):
        parts.append(address)
    # Bailiff notices name the cadastral municipality ("КО Групчин") — a village or area.
    ko = cadastral_area((listing.get("extra") or {}).get("note"))
    if ko and not parts and ko.lower() != (city or "").lower():
        parts.append(ko)
    if district and district.startswith("Скопје "):
        parts.append(district.replace("Скопје ", ""))       # "Скопје Центар" → "Центар"
    elif district and district != city:
        parts.append(district)
    if city:
        parts.append(city)
    if not parts:
        return None
    return ", ".join(dict.fromkeys(parts + [COUNTRY]))


def url(lat: float | None, lng: float | None, q: str | None) -> str | None:
    if lat and lng:
        return f"https://www.google.com/maps/search/?api=1&query={lat:.6f},{lng:.6f}"
    if q:
        return f"https://www.google.com/maps/search/?api=1&query={quote_plus(q)}"
    return None
