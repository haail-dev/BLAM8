"""Operating Systems — the seven business "shapes" (config, not custom code).

A tenant's `industry` decides its OS: the fundamental workflow pattern a business
runs on (docs/BLAM8_operating_systems.md). Once an OS is built it covers every
industry of that shape via a config seed, not new code — so OS is the layer we
build to and the layer we sell. Derivation is one-way and lossy on purpose: a
sector maps to at most one OS, and anything off the map (incl. "other" and the
free-text long tail) simply gets no OS until we grow the mapping.

Only Field OS ships today (the TMS spine, generalised); the rest are named here
so onboarding can show the whole menu with the not-yet-built ones marked. To
add a shape, edit the list below; to route a new industry, edit the map.
"""

# Ordered — this is the on-screen menu order for the Shape selector.
OPERATING_SYSTEMS = [
    {"id": "field", "label": "Field OS",
     "blurb": "You provide services at a customer location.",
     "live": True},
    {"id": "storefront", "label": "Storefront OS",
     "blurb": "Customers visit your shop to buy goods.",
     "live": True},
    {"id": "practice", "label": "Practice OS",
     "blurb": "Customers book time with you.",
     "live": True},
    {"id": "wholesale", "label": "Wholesale OS",
     "blurb": "You supply goods to other businesses.",
     "live": True},
    {"id": "assets", "label": "Assets OS",
     "blurb": "Customers reserve the use of your resources for a period.",
     "live": True},
    {"id": "events", "label": "Events OS",
     "blurb": "You organise events and the people involved.",
     "live": True},
    {"id": "match", "label": "Match OS",
     "blurb": "You match people or organisations to one another.",
     "live": False},
]

# Industry → OS (doc §6). Anything absent — incl. "other" and free-text sectors —
# derives no OS. Keep this in step with the migration backfill CASE.
INDUSTRY_TO_OS = {
    "logistics": "field",
    "trades": "field",
    "construction": "field",
    "recruitment": "match",
    "wholesale": "wholesale",
    # Assets OS (docs/os/assets.md §9) — the hire-shape verticals. lettings + hire
    # are the live wedge; the hire sub-industries are valid ids (stored tenants /
    # lingo / seeds key off them) but present-but-hidden until featured.
    "lettings": "assets",
    "hire": "assets",
    "car_hire": "assets",
    "boat_hire": "assets",
    "tool_hire": "assets",
    "party_hire": "assets",
    "camping_hire": "assets",
    # Events OS (docs/os/events.md §9) — the one-off-dated-occasion verticals.
    "events": "events",
    "caterer": "events",
    "wedding_planner": "events",
    # Practice OS (docs/os/practice.md §9.1) — the appointment-shape verticals.
    "dentist": "practice",
    "physio": "practice",
    "salon": "practice",
    "barber": "practice",
    "therapist": "practice",
    "chiropractor": "practice",
    "vet": "practice",
    "tutor": "practice",
    "coach": "practice",
    "personal_trainer": "practice",
    "clinic": "practice",
    # Storefront OS (docs/os/storefront.md §9) — the shop-shape verticals.
    "farm_shop": "storefront",
    "deli": "storefront",
    "bakery": "storefront",
    "wine_merchant": "storefront",
    "nursery": "storefront",
}

_BY_ID = {os["id"]: os for os in OPERATING_SYSTEMS}


def os_for_industry(industry: str | None) -> str | None:
    """The OS id a stored industry derives to, or None when it's off the map."""
    return INDUSTRY_TO_OS.get(industry) if industry else None


def get_os(os_id: str | None) -> dict | None:
    """The OS entry (label/blurb/live), or None for an unknown id."""
    return _BY_ID.get(os_id) if os_id else None


def is_live(os_id: str | None) -> bool:
    """True only for OSes whose module actually ships today (currently Field)."""
    os = get_os(os_id)
    return bool(os and os["live"])
