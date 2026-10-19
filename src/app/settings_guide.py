"""Per-business Diary-settings recipes for the in-app assistant (app/concierge help mode).

When a logged-in customer asks "help me set up my diary", the assistant already knows who
they are — their industry (a known id, or their own words when they picked "Something else")
and their business brief. This module turns that into TAILORED guidance: which of the diary
settings that business should use, and how to fill them, in that trade's language — plus a
read of what they've already configured so the assistant can say "you've done X, now do Y".

Pure + data-only (config-not-code, same spirit as src/app/intake.py / src/app/diary_config.py): the
assistant's behaviour rules live in src/app/concierge.py; the RECIPES are injected as data. Every
recipe references only behaviour that is actually wired (app/scheduling + app/diary_config), so
the assistant never tells a customer to set something that wouldn't take effect.

Where settings live (the assistant links to these):
  * Diary settings  (/app/diary/settings): opening hours, working days, slot length, min
    notice, horizon, Bookings-per-slot, Services & durations, Resource types, Stays pricing
    & rules (rate/deposit/min-nights/check-in-out), Opening hours by day, Book by party size,
    Recurring bookings, Delivery & collection, Gap between bookings.
  * Booking requirements (/app/diary/intake): the details every booking must capture, and the
    Overnight-stays toggle (which turns bookings into multi-day stays).
"""
from __future__ import annotations

import re
from functools import lru_cache

# Always-true foundation, prepended to every recipe.
BASE_GUIDE = (
    "Foundation (every business, on the Diary settings page): set your Opening hours, "
    "Working days, Minimum notice and Booking horizon first. Then layer on the parts below "
    "that fit. The required details a booking must capture (and the Overnight-stays toggle) "
    "are on the separate Booking-requirements page. Each row there can be REMEMBERED on the "
    "customer's record — once (asked on the first booking, never again), confirmed each "
    "visit (read back and checked in one line), or until a date lapses (a licence, a "
    "certificate) — so a repeat customer is asked only what changes; a row about this "
    "visit alone is asked every time.")

# Each profile: a stable id, a friendly label, the onboarding industry ids it's the default
# for, keywords matched against the customer's industry text (with the brief as a fallback —
# see match_profiles), and the recipe (the concrete steps for THAT business). Keywords match
# at a word start; ones whose last word is short (< 5 chars) must be whole words, longer ones
# are prefix stems ("plumb" → plumbing/plumber). Order matters only for display.
PROFILES = [
    {
        "id": "salon",
        "label": "Salon / barber / beauty",
        "industries": (),
        "keywords": ("salon", "barber", "hair", "hairdress", "beauty", "nails", "nail",
                     "spa", "waxing", "lashes", "brows", "aesthetic"),
        "recipe": (
            "Salon / barber / beauty:\n"
            "- Booking unit: 'Booked times', with Slot length set to your shortest service.\n"
            "- Services & durations: add every service (e.g. Cut 30 min, Cut & blow 45, Colour "
            "120) with its price — the agent then books the right length and quotes the price.\n"
            "- Resource types: add one per stylist/chair (or per room) with a count, so two "
            "people working in parallel = two bookable slots; one type can be full while another "
            "is free. If you don't need per-person pools, just set Bookings-per-slot to how many "
            "of you work at once.\n"
            "- Tie a service to a resource (the Resource box on the service row) when only one "
            "station can do it, e.g. Colour → the colour station.\n"
            "- Gap between bookings: set your clean-down/turnaround minutes.\n"
            "- Booking requirements: customer name, contact number, and which service."),
    },
    {
        "id": "clinic",
        "label": "Clinic (dentist / physio / vet / practitioner)",
        "industries": (),
        "keywords": ("dentist", "dental", "physio", "chiro", "osteo", "doctor", "gp", "clinic",
                     "vet", "veterinary", "therapy", "therapist", "podiatr", "optician", "medical"),
        "recipe": (
            "Clinic / practitioner:\n"
            "- Services & durations: each appointment type (e.g. Check-up 15, New patient 60) "
            "with price.\n"
            "- Resource types: one per practitioner or treatment room (count each), so capacity "
            "is per clinician.\n"
            "- Recurring bookings: turn on if you run courses of treatment (e.g. weekly physio).\n"
            "- Gap between bookings: any room turnaround.\n"
            "- Booking requirements: patient name, contact, and new-or-existing."),
    },
    {
        "id": "kennels",
        "label": "Kennels / cattery / pet boarding",
        "industries": (),
        "keywords": ("kennel", "cattery", "boarding", "boarder", "pet hotel", "doggy",
                     "dog daycare", "animal boarding"),
        "recipe": (
            "Kennels / cattery / boarding:\n"
            "- Diary settings: choose arrival-to-departure booking mode — a booking then runs from a "
            "drop-off to a collection and can span days.\n"
            "- Resource types: one per kennel/pen size (e.g. Small kennel, Large kennel) with a "
            "count each, so a dog can only take a dog space and large dogs need a large pen.\n"
            "- Stays pricing & rules: rate per night, a deposit to hold, minimum nights, and "
            "your check-in-from / collection-by times.\n"
            "- Gap between bookings: cleaning/turnaround between occupants.\n"
            "- Quantity: a customer boarding two pets books quantity 2 (two spaces).\n"
            "- Booking requirements: pet name, breed, vaccination/notes."),
    },
    {
        "id": "hire",
        "label": "Equipment / plant / tool hire",
        "industries": ("hire",),
        "keywords": ("hire", "rental", "plant", "equipment", "tool", "scaffold", "generator",
                     "excavator", "digger", "skip"),
        "recipe": (
            "Equipment / plant hire:\n"
            "- Resource types: one per item you hire (e.g. Excavator, Dumper, Generator), count "
            "= how many you own, rate per day or week — capacity is tracked per item.\n"
            "- Diary settings: choose arrival-to-departure booking mode so an item goes out from one date "
            "to another.\n"
            "- Quantity: hiring several of the same item = quantity N.\n"
            "- Delivery & collection: turn on if you deliver, set the fee/radius — the agent "
            "captures the delivery address.\n"
            "- Stays pricing & rules: a deposit (damage) and a minimum hire.\n"
            "- Gap between bookings: inspection/clean time between hires."),
    },
    {
        "id": "accommodation",
        "label": "Hotel / B&B / holiday let",
        "industries": (),
        "keywords": ("hotel", "b&b", "bed and breakfast", "guest house", "guesthouse",
                     "holiday let", "airbnb", "rooms", "lodge", "campsite", "glamping"),
        "recipe": (
            "Hotel / B&B / holiday let:\n"
            "- Diary settings: choose arrival-to-departure booking mode.\n"
            "- Resource types: one per room type (e.g. Single, Double, Suite) with a count and a "
            "per-night rate — each type is its own availability.\n"
            "- Stays pricing & rules: deposit, minimum nights, and check-in-after / "
            "check-out-before times.\n"
            "- Book by party size: optionally capture guests per room (set the max).\n"
            "- Gap between bookings: housekeeping turnaround.\n"
            "- Booking requirements: lead guest name, contact, number of guests."),
    },
    {
        "id": "vehicle_hire",
        "label": "Car / van hire",
        "industries": (),
        "keywords": ("car hire", "van hire", "vehicle hire", "car rental", "minibus"),
        "recipe": (
            "Car / van hire:\n"
            "- Resource types: one per vehicle class (e.g. Economy, SUV, Van) with a count and a "
            "per-day rate.\n"
            "- Diary settings: choose arrival-to-departure booking mode (vehicle out date→date).\n"
            "- Stays pricing & rules: deposit and minimum hire.\n"
            "- Delivery & collection: turn on if you deliver/collect or have depot pickup.\n"
            "- Booking requirements: driver name, licence, contact."),
    },
    {
        "id": "storage",
        "label": "Self-storage",
        "industries": (),
        "keywords": ("storage", "self storage", "self-storage", "lock up", "lock-up", "container store"),
        "recipe": (
            "Self-storage:\n"
            "- Resource types: one per unit size (e.g. 25 sq ft, 50 sq ft, 100 sq ft) with a "
            "count and a weekly rate.\n"
            "- Diary settings: choose arrival-to-departure booking mode for the occupied period.\n"
            "- Stays pricing & rules: deposit and a minimum term.\n"
            "- Note: open-ended 'until cancelled' isn't supported yet — book a long end date and "
            "extend when needed."),
    },
    {
        "id": "venue",
        "label": "Venue / function room hire",
        "industries": ("events",),
        "keywords": ("venue", "function", "hall", "room hire", "conference", "wedding",
                     "event space", "studio hire", "marquee"),
        "recipe": (
            "Venue / function-room hire:\n"
            "- Resource types: one per space/room you let, with a count and an hourly or daily "
            "rate.\n"
            "- Booking unit: 'Booked times' for hourly/half-day; choose arrival-to-departure mode in Diary settings for "
            "multi-day events.\n"
            "- Book by party size: capture guest numbers and set the max per space.\n"
            "- Stays pricing & rules: deposit; Gap between bookings for set-up/tear-down.\n"
            "- Booking requirements: event type, contact, headcount."),
    },
    {
        "id": "restaurant",
        "label": "Restaurant / cafe (table bookings)",
        "industries": (),
        "keywords": ("restaurant", "cafe", "café", "bistro", "diner", "covers", "table booking",
                     "dining", "eatery", "brasserie"),
        "recipe": (
            "Restaurant / cafe (covers):\n"
            "- Services & durations: add a 'Table' service whose length is a sitting (e.g. 120 "
            "min).\n"
            "- Book by party size: turn ON and set the max — and set Bookings-per-slot to your "
            "total covers; a booking then counts its party size against your covers, so a big "
            "party won't fit when only a few seats are left.\n"
            "- Gap between bookings: table turnaround.\n"
            "- Booking requirements: name, contact, party size."),
    },
    {
        "id": "lessons",
        "label": "Lessons / tutoring / instructor",
        "industries": (),
        "keywords": ("tutor", "tuition", "lesson", "music", "piano", "guitar", "driving",
                     "instructor", "coach", "class", "swimming", "language"),
        "recipe": (
            "Lessons / tutoring / instructor:\n"
            "- Services & durations: lesson lengths (e.g. 30 / 60 min) with price.\n"
            "- Recurring bookings: turn ON — most pupils want the SAME weekly slot; the agent "
            "books the series and capacity-checks each date.\n"
            "- Resource types: one per tutor/instructor if you have several.\n"
            "- Gap between bookings: travel time if you go to the pupil (mobile instructors).\n"
            "- Booking requirements: name, contact, level/subject."),
    },
    {
        "id": "garage",
        "label": "Garage / MOT / vehicle service",
        "industries": (),
        "keywords": ("garage", "mot", "tyre", "tyres", "mechanic", "car service", "bodyshop",
                     "valeting", "auto", "automotive"),
        "recipe": (
            "Garage / MOT / service:\n"
            "- Services & durations: MOT 45, Service 120, Tyre 30, etc. with price.\n"
            "- Resource types: one per ramp/bay with a count, so jobs are spread across bays.\n"
            "- Gap between bookings: any handover/clean time.\n"
            "- Booking requirements: registration, make/model, the service wanted."),
    },
    {
        "id": "studio",
        "label": "Tattoo / photography / studio",
        "industries": (),
        "keywords": ("tattoo", "piercing", "photograph", "photo", "studio", "barbering chair"),
        "recipe": (
            "Tattoo / photography / studio:\n"
            "- Services & durations: each session length (e.g. small piece 60, half-day 240) "
            "with price.\n"
            "- Resource types: one per artist/booth/studio space.\n"
            "- Gap between bookings: set-up / clean-down between sessions.\n"
            "- Booking requirements: name, contact, what they want (and consent/age where "
            "relevant).\n"
            "- Note: automatic deposits apply to overnight-stay bookings only for now — for "
            "session deposits, mention it in your reply wording / knowledge base."),
    },
    {
        # Run #9 (2026-09-05): "domestic cleaning" matched the callout recipe
        # below and the diary was sized like a plumber's. Cleaners, gardeners
        # and window cleaners run STANDING ROUNDS — the same houses on the same
        # weekday, ongoing — not call-outs.
        "id": "domestic_rounds",
        "label": "Regular domestic rounds (cleaners, gardeners, window cleaners)",
        "industries": (),
        "keywords": ("cleaning", "cleaner", "housekeep", "domestic", "gardener", "gardening",
                     "lawn", "window clean", "ironing", "mobile groom", "dog walk"),
        "recipe": (
            "Regular domestic rounds (you visit the same homes on a cycle):\n"
            "- Services & durations: one row per visit type with its length and price, "
            "timing 'half' — the customer is told morning or afternoon and you manage the "
            "hour (e.g. Regular clean 120 min half, Deep clean 240 half, Oven clean 90 half). "
            "Every priced service in the knowledge notes is a row.\n"
            "- Bookings-per-slot = how many of your people are out at once (three cleaners = 3). "
            "Add a Resource type per named person only if customers must keep the SAME person "
            "and you want the diary to enforce it.\n"
            "- Recurring bookings: ON with weekly and fortnightly — the standing slot IS the "
            "business; set the maximum to the ceiling so a regular runs as long as possible.\n"
            "- Gap between bookings: travel between houses (20-30 min).\n"
            "- Notice: the cancellation window (48 h is typical) — the same number is the late "
            "cancellation rule.\n"
            "- Delivery & collection: off (nothing is delivered). Party size: off. Overnight: off.\n"
            "- Booking requirements: property address (required); how we get in, pets, parking "
            "and 'anything to focus on this visit' OPTIONAL — settled at the look-round. Never a "
            "row for an alarm code."),
    },
    {
        "id": "field_service",
        "label": "Trades / field service (you travel to the job)",
        "industries": ("trades", "construction"),
        "keywords": ("plumb", "electric", "electrician", "heating", "hvac", "gas", "boiler",
                     "repair", "locksmith", "pest", "landscap", "roofing",
                     "builder", "joiner", "handyman", "appliance"),
        "recipe": (
            "Trades / field service (you travel to the customer):\n"
            "- This runs on Field OS · Trades: set up your Engineers (with skills and a "
            "daily-minutes capacity) and Job types (each with a duration, required skill and "
            "price) — the agent books a skill-matched engineer for the right length.\n"
            "- On the Diary settings: set Gap between bookings to your travel time so jobs aren't "
            "booked back-to-back across the patch.\n"
            "- Recurring bookings: turn on for regular maintenance/contracts.\n"
            "- Booking requirements: job address, what needs doing, access notes."),
    },
    {
        "id": "logistics",
        "label": "Logistics / haulage / courier",
        "industries": ("logistics", "wholesale"),
        "keywords": ("haulage", "logistics", "courier", "pallet", "freight", "transport",
                     "delivery firm", "distribution", "removals"),
        "recipe": (
            "Logistics / haulage / transport:\n"
            "- This runs on Field OS · Transport: set up your Vehicles (pallet spaces + weight) and "
            "branches — the agent sizes each load and books it onto the right van.\n"
            "- The office Diary settings (opening hours, notice, horizon) still shape when the "
            "transport diary is open.\n"
            "- Booking requirements: collection + delivery address, goods/pallet count."),
    },
    {
        "id": "appointments",
        "label": "General appointments / something else",
        "industries": ("recruitment", "lettings"),
        "keywords": ("appointment", "consult", "viewing", "meeting", "interview", "valuation",
                     "assessment", "fitting"),
        "recipe": (
            "General appointments:\n"
            "- Booking unit: 'Booked times' with a Slot length, or add Services & durations if "
            "different appointments take different lengths (with prices).\n"
            "- Bookings-per-slot: how many you can run at once; or add Resource types for "
            "per-person/per-room pools.\n"
            "- Recurring bookings: turn on if customers book regular repeat slots.\n"
            "- Booking requirements: name, contact, and what the appointment is for."),
    },
]

# Known onboarding industry id → the profile that's its safest default. Free-text industries
# (the 'other' bucket) fall through to keyword matching against the brief.
_INDUSTRY_DEFAULT = {}
for _p in PROFILES:
    for _ind in _p["industries"]:
        _INDUSTRY_DEFAULT.setdefault(_ind, _p["id"])


def _norm(s) -> str:
    return " ".join(str(s or "").strip().lower().split())


@lru_cache(maxsize=None)
def _kw_re(kw: str) -> re.Pattern:
    # A keyword matches at a word start. When its last word is short (< 5 chars) it must
    # also END at a word boundary — so 'auto' can't latch onto "automation", 'mot' onto
    # "remote" or 'spa' onto "space". Longer keywords stay prefix stems ('plumb' →
    # plumbing/plumber, 'kennel' → kennels).
    tail = r"" if len(kw.split()[-1]) >= 5 else r"\b"
    return re.compile(r"\b" + re.escape(kw) + tail)


def _kw_hits(text: str) -> list[str]:
    """Profile ids whose keywords appear in `text`, in PROFILES order."""
    return [p["id"] for p in PROFILES
            if any(_kw_re(k).search(text) for k in p["keywords"])]


# Industry values that say nothing about the sector — only these fall through to
# matching the BRIEF. A tenant who typed their own sector words has told us what they
# are; if that matches no profile, trades merely MENTIONED in the brief (customers,
# examples) must not hijack the match.
_VAGUE_INDUSTRIES = {"", "other", "something else"}


def match_profiles(industry: str | None, brief: str | None = None) -> list[dict]:
    """The recipe(s) most relevant to this customer. The industry is what they ARE, so it
    is matched first: keywords against the industry text, then a known industry id's default
    profile. Only when the industry says nothing (empty / 'other') does the brief get
    keyword-matched as a fallback (so 'other' + "boarding kennels for dogs" finds the
    kennels recipe) — a brief that merely mentions other trades (e.g. a SaaS business
    describing its garage/haulier customers) never overrides a stated industry.
    Deduped, best-effort, may be empty."""
    ind = _norm(industry)
    hits: list[dict] = []
    seen: set = set()

    def _add(pid):
        if pid and pid not in seen:
            p = next((x for x in PROFILES if x["id"] == pid), None)
            if p:
                seen.add(pid)
                hits.append(p)

    # Keyword matches on the industry text first (most specific — the customer's own
    # words), then the known industry id's default.
    for pid in _kw_hits(ind):
        _add(pid)
    if ind in _INDUSTRY_DEFAULT:
        _add(_INDUSTRY_DEFAULT[ind])
    # Brief fallback — only when the industry itself was vague AND matched nothing.
    if not hits and ind in _VAGUE_INDUSTRIES:
        for pid in _kw_hits(_norm(brief)):
            _add(pid)
    return hits


def config_status(cfg: dict | None) -> str:
    """A short read of what the tenant has already configured on the diary, so the assistant
    can pick up where they left off rather than starting from scratch."""
    from app import diary_config, intake, services
    cfg = cfg or {}
    set_, todo = [], []
    (set_ if services.configured(cfg.get("service_types")) else todo).append("Services & durations")
    (set_ if diary_config.resources_configured(cfg.get("resource_types")) else todo).append("Resource types")
    (set_ if intake.stay_mode(cfg) else todo).append("Booking mode: arrival to departure")
    (set_ if diary_config.stay_pricing_configured(cfg.get("stay_pricing")) else todo).append("Stay pricing")
    (set_ if diary_config.day_hours_configured(cfg.get("day_hours")) else todo).append("Per-day hours / check-in-out")
    (set_ if diary_config.party_size_enabled(cfg.get("party_size")) else todo).append("Party size")
    (set_ if diary_config.recurring_enabled(cfg.get("recurring")) else todo).append("Recurring bookings")
    (set_ if diary_config.delivery_offered(cfg.get("delivery")) else todo).append("Delivery & collection")
    if int((cfg or {}).get("buffer_minutes") or 0) > 0:
        set_.append("Gap between bookings")
    else:
        todo.append("Gap between bookings")
    parts = []
    parts.append("Already set up: " + (", ".join(set_) if set_ else "nothing yet"))
    parts.append("Not yet set: " + (", ".join(todo) if todo else "everything's configured"))
    return "\n".join(parts)


def guide_for(industry: str | None, brief: str | None = None, cfg: dict | None = None) -> str:
    """The full settings-guidance block injected into the help prompt: the foundation, the
    matched recipe(s) for this customer's business (or a compact catalogue to choose from when
    nothing matches confidently), and a read of what they've already configured."""
    sections = [BASE_GUIDE]
    matched = match_profiles(industry, brief)
    if matched:
        sections.append("\n\n".join(p["recipe"] for p in matched[:2]))
    else:
        catalogue = "; ".join(f"{p['label']}" for p in PROFILES)
        sections.append(
            "Their business type wasn't an exact match — ask what they do, then use the closest "
            "of these recipes: " + catalogue + ".")
    if cfg is not None:
        sections.append("Their current diary setup:\n" + config_status(cfg))
    return "\n\n".join(sections)
