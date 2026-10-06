"""❓ Needs info: promising deals whose key facts aren't confirmed yet.

For each result we list the exact questions to ask the seller. A deal is
marked status="needs_info" when it scores well enough to be worth a call
but has open questions; once you answer in Telegram (✅ Fits, or a reply
such as "water yes, area 450") the facts are stored and the deal is
re-scored with them on every run.

Statuses: "ready" (nothing to ask), "needs_info", "pending" (the platform is
still reading the detail page), "confirmed" (you checked it).
"""
from __future__ import annotations

from .places import centar_unconfirmed

UTIL_Q = {
    "electricity": "Is there a working electricity connection on the property (not just nearby)?",
    "water": "Is there a water connection (mains or well) on the property?",
    "road": "Is there legal road access — asphalt or a registered road to the plot?",
}


def questions(item: dict, researcher: str, listing: dict | None = None) -> list[str]:
    """Only what the platform can't find out itself: asked once the detail page
    has been read (before that, the next run usually fills the gaps)."""
    listing = listing or {}
    if not listing.get("detail_at") and listing.get("source") != "kirsm":
        return []
    q = []
    util = item.get("utilities") or {}
    if item.get("kind") == "land":
        q += [UTIL_Q[k] for k in ("electricity", "water", "road") if util.get(k) == "unknown"]
        land_type = (item.get("metrics") or {}).get("Land type")
        if land_type == "Unknown":
            q.append("Is it building land (градежно) — can a permit for a house be issued?")
        elif land_type == "Agricultural":
            q.append("It's listed as agricultural: is re-zoning for a house realistic, and at what cost?")
    if not item.get("area"):
        q.append("What is the exact area (built area for houses, plot size for land)?")
    if researcher in ("flip", "airbnb") and centar_unconfirmed(listing) and not item.get("lat"):
        q.append("What is the exact address? (filed under Centar, but nothing in the ad confirms it)")
    return q


def annotate(results: dict, listings_by_id: dict, cfg: dict) -> dict:
    """Add item['questions'] and item['status'] to every result."""
    min_score = cfg["notify"].get("needs_info_min_score", 45)
    out = {}
    for researcher, items in results.items():
        rows = []
        for item in items:
            listing = listings_by_id.get(item["id"])
            qs = [] if (listing or {}).get("confirmed") else questions(item, researcher, listing)
            if (listing or {}).get("confirmed"):
                status = "confirmed"
            elif qs and item.get("score", 0) >= min_score:
                status = "needs_info"
            elif not item.get("qualified", True) and not qs:
                status = "pending"      # details still being collected — nothing to ask yet
            else:
                status = "ready"
            note = (listing or {}).get("owner_note")
            rows.append({**item, "questions": qs, "status": status, **({"owner_note": note} if note else {})})
        out[researcher] = rows
    return out


def public(results: dict) -> dict:
    """The public dashboard never shows that you contacted a seller."""
    return {r: [{k: v for k, v in {**x, "status": "ready" if x.get("status") == "confirmed" else x.get("status")}
                 .items() if k != "owner_note"} for x in items] for r, items in results.items()}
