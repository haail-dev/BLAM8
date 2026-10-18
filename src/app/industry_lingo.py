"""Per-industry vocabulary seeds for the drafting prompt (config, not custom code).

A tenant's `industry` (chosen at onboarding) seeds the system prompt with the
sector's everyday language so replies read native — "consignment / POD" for
logistics, "callout / day rate" for trades — with no per-tenant code. It's a
gentle default: the tenant's own voice config always takes precedence, and the
model is told to mirror the customer rather than force jargon. Unknown or
free-text industries (the "Something else" long tail) simply get no seed.

To add or tune a sector, edit the map below — that's the whole feature.
"""

LINGO = {
    "logistics": "Logistics & haulage: consignments, pallets/spaces, collection & "
                 "delivery, POD, lanes & postcodes, tail-lift, FTL/LTL.",
    "trades": "Trades & field service: jobs, callouts, site visits, quotes, parts & "
              "labour, day rate, certificates named in the business settings.",
    "construction": "Construction & contracting: tenders, RFQs, subcontractors, site "
                    "programme, variations, RAMS, retentions, snagging.",
    "recruitment": "Recruitment & staffing: candidates, clients, roles/vacancies, CVs, "
                   "shortlists, interviews, placements, perm/contract, day rate.",
    "wholesale": "Wholesale & distribution: orders, SKUs, stock & availability, lead "
                 "times, MOQ, trade pricing, pro forma, dispatch.",
    "lettings": "Lettings & property management: enquiries, viewings, tenancies (AST), "
                "referencing, deposits, inventories, EPC, maintenance & repairs.",
    "hire": "Equipment & plant hire: hire & off-hire, daily/weekly rates, availability, "
            "delivery & collection, damage waiver.",
    "events": "Events, catering & venues: enquiries, dates, covers/headcount, packages, "
              "deposits, dietary requirements, run sheets, staffing.",
    # Events OS (docs/os/events.md §9) — catering + planners share the venue vocabulary.
    "caterer": "Catering: covers/headcount, dietary requirements, menu & courses, final "
               "numbers, deposits, staffing, service times, allergens.",
    "wedding_planner": "Wedding & event planning: the couple/client, suppliers (caterer, "
                       "bar, florist, AV), run of show, timings, guest count, dietaries, staffing.",
    # Practice OS (docs/os/practice.md §9.3) — the appointment verticals.
    "dentist": "Dental practice: patients, check-ups & recalls, hygiene visits, treatment "
               "plans, NHS vs private, courses of treatment, UDAs, referrals, reminders, "
               "no-shows/FTAs.",
    "salon": "Salon, barber & beauty: clients, appointments, rebooking, stylists/therapists, "
             "colour & cut, treatments, patch tests, deposits, reminders, no-shows.",
    "physio": "Physio & clinic: patients, assessments, treatment blocks/courses, "
              "practitioners, referrals, insurers (Bupa, AXA, self-pay), recalls, reminders, "
              "DNAs/no-shows.",
    # Storefront OS (docs/os/storefront.md §9) — speak shop, not sales.
    "farm_shop": "Farm shop & grocer: stock & deliveries, orders & reservations, "
                 "collections, best-before/sell-by, crates & boxes, veg box, suppliers.",
    "deli": "Deli & butcher: counter orders, reservations & holds, cuts & joints, "
            "weights (£/kg), best-before, collections, specials to regulars.",
    "bakery": "Bakery & cafe: pre-orders, batches & loaves, collections, end-of-day "
              "clearance, allergens, standing orders, best-before.",
    "wine_merchant": "Wine & artisan retail: lines & vintages, cases & bottles, stock, "
                     "account/trade customers, tastings, allocations, personalised offers.",
    "nursery": "Nursery & garden centre: seasonal stock, plants & trays, reservations, "
               "collections, availability, suppliers, best-before/season.",
}


def seed_for(industry: str | None) -> str | None:
    """The vocabulary seed for an industry id, or None for unknown/free-text/empty."""
    if not industry:
        return None
    return LINGO.get(industry.strip().lower())


# ── CRM custom-field seeds (docs/architecture/CRM_design.md §9) ───────────────────────────
# Per-industry starting suggestions for the CRM fields wizard: 3-5 per entity
# that a firm in that sector actually tracks, each with the extraction_hint the
# LLM autofill matches against. The tenant accepts/edits/skips — these are
# suggestions, never silently applied. Unknown/free-text industries fall back
# to the generic set. Same rule as LINGO: editing this map IS the feature.

_GENERIC_CRM_SEEDS = {
    "company": (
        {"label": "What they do", "field_type": "text",
         "extraction_hint": "what the business does, their trade or line of work"},
        {"label": "Location", "field_type": "text",
         "extraction_hint": "town, city or area they operate from"},
    ),
    "contact": (
        {"label": "Role", "field_type": "text",
         "extraction_hint": "job title or role — owner, manager, admin"},
        {"label": "Phone", "field_type": "text",
         "extraction_hint": "phone or mobile number in signatures or the body"},
    ),
    "deal": (
        {"label": "Budget", "field_type": "number",
         "extraction_hint": "budget, target price or spend they mention"},
        {"label": "Timeline", "field_type": "text",
         "extraction_hint": "when they need it — dates, deadlines, 'by end of month'"},
    ),
}

CRM_SEEDS = {
    "logistics": {
        "company": (
            {"label": "Fleet size", "field_type": "number",
             "extraction_hint": "mentions of fleet size, van/truck count, vehicle numbers"},
            {"label": "Depot count", "field_type": "number",
             "extraction_hint": "depots, branches, sites or bases they run"},
            {"label": "Goods type", "field_type": "text",
             "extraction_hint": "what they ship — pallets, parcels, chilled, ADR/hazardous"},
        ),
        "contact": (
            {"label": "Role", "field_type": "text",
             "extraction_hint": "job title — transport manager, planner, owner-driver"},
            {"label": "Phone", "field_type": "text",
             "extraction_hint": "phone or mobile number in signatures or the body"},
        ),
        "deal": (
            {"label": "Lane", "field_type": "text",
             "extraction_hint": "the route or lane — collection and delivery areas/postcodes"},
            {"label": "Volume per week", "field_type": "text",
             "extraction_hint": "regular volume — pallets/loads per week or month"},
        ),
    },
    "trades": {
        "company": (
            {"label": "Van count", "field_type": "number",
             "extraction_hint": "vans, crews or engineers on the road"},
            {"label": "Service area", "field_type": "text",
             "extraction_hint": "towns, postcodes or radius they cover"},
            {"label": "Trade", "field_type": "text",
             "extraction_hint": "their trade — plumbing, electrical, heating, roofing"},
        ),
        "contact": (
            {"label": "Role", "field_type": "text",
             "extraction_hint": "job title — owner, office manager, site contact"},
            {"label": "Phone", "field_type": "text",
             "extraction_hint": "phone or mobile number in signatures or the body"},
        ),
        "deal": (
            {"label": "Job type", "field_type": "text",
             "extraction_hint": "the work wanted — boiler swap, rewire, emergency callout"},
            {"label": "Property", "field_type": "text",
             "extraction_hint": "property type and address area — flat, semi, commercial unit"},
        ),
    },
    "construction": {
        "company": (
            {"label": "Trade/discipline", "field_type": "text",
             "extraction_hint": "their discipline — groundworks, M&E, fit-out, main contractor"},
            {"label": "Typical project size", "field_type": "text",
             "extraction_hint": "project values or scale they mention"},
        ),
        "contact": (
            {"label": "Role", "field_type": "text",
             "extraction_hint": "job title — QS, site manager, buyer, estimator"},
            {"label": "Phone", "field_type": "text",
             "extraction_hint": "phone or mobile number in signatures or the body"},
        ),
        "deal": (
            {"label": "Site", "field_type": "text",
             "extraction_hint": "site name or location of the works"},
            {"label": "Start date", "field_type": "date",
             "extraction_hint": "programme or start-on-site dates"},
        ),
    },
    "recruitment": {
        "company": (
            {"label": "Sector", "field_type": "text",
             "extraction_hint": "the sector they hire in — tech, care, industrial, finance"},
            {"label": "Headcount", "field_type": "number",
             "extraction_hint": "company size, number of staff"},
        ),
        "contact": (
            {"label": "Role", "field_type": "text",
             "extraction_hint": "job title — hiring manager, HR, talent lead"},
            {"label": "Phone", "field_type": "text",
             "extraction_hint": "phone or mobile number in signatures or the body"},
        ),
        "deal": (
            {"label": "Vacancy", "field_type": "text",
             "extraction_hint": "the role being filled — title and seniority"},
            {"label": "Salary range", "field_type": "text",
             "extraction_hint": "salary, day rate or package mentioned"},
            {"label": "Perm or contract", "field_type": "dropdown",
             "options": ["Perm", "Contract", "Temp"],
             "extraction_hint": "whether the role is permanent, contract or temp"},
        ),
    },
    "wholesale": {
        "company": (
            {"label": "Credit limit", "field_type": "number",
             "extraction_hint": "credit limit or account terms discussed"},
            {"label": "Payment terms", "field_type": "text",
             "extraction_hint": "payment terms — 30 days, pro forma, COD"},
            {"label": "Delivery area", "field_type": "text",
             "extraction_hint": "where they take delivery — region, depot, store locations"},
            # Wholesale OS killer-feature inputs (docs/os/wholesale.md §9): both
            # self-fill from email like credit_limit does, and both feed the
            # fulfilment planner / order gate directly.
            {"label": "Allocation priority", "field_type": "number",
             "extraction_hint": "priority tier if we're short — 1 is highest"},
            {"label": "Minimum order value", "field_type": "number",
             "extraction_hint": "minimum order value or carriage-paid threshold"},
        ),
        "contact": (
            {"label": "Role", "field_type": "text",
             "extraction_hint": "job title — buyer, purchasing, stores manager"},
            {"label": "Phone", "field_type": "text",
             "extraction_hint": "phone or mobile number in signatures or the body"},
        ),
        "deal": (
            {"label": "Products", "field_type": "text",
             "extraction_hint": "SKUs, lines or product ranges they're ordering"},
            {"label": "Order frequency", "field_type": "text",
             "extraction_hint": "how often they order — weekly, monthly, ad hoc"},
        ),
    },
    "lettings": {
        "company": (
            {"label": "Portfolio size", "field_type": "number",
             "extraction_hint": "number of properties or units they own/manage"},
            {"label": "Landlord or agent", "field_type": "dropdown",
             "options": ["Landlord", "Agent", "Developer"],
             "extraction_hint": "whether they're a landlord, letting agent or developer"},
            {"label": "Insurer", "field_type": "text",
             "extraction_hint": "the property/contents/liability insurer or policy they "
                                "mention — the cover behind the unit and its deposit"},
        ),
        "contact": (
            {"label": "Role", "field_type": "text",
             "extraction_hint": "job title — landlord, property manager, tenant"},
            {"label": "Phone", "field_type": "text",
             "extraction_hint": "phone or mobile number in signatures or the body"},
        ),
        "deal": (
            {"label": "Property", "field_type": "text",
             "extraction_hint": "the property address or description"},
            {"label": "Rent", "field_type": "number",
             "extraction_hint": "monthly rent or asking price"},
        ),
    },
    "hire": {
        "company": (
            {"label": "Typical kit", "field_type": "text",
             "extraction_hint": "what they hire — diggers, scaffolding, access platforms"},
            {"label": "Account or cash", "field_type": "dropdown",
             "options": ["Account", "Cash"],
             "extraction_hint": "account customer or one-off cash hire"},
            {"label": "Insurer", "field_type": "text",
             "extraction_hint": "the plant/equipment or damage-waiver insurer or policy they "
                                "mention — the cover behind the hire and its deposit"},
        ),
        "contact": (
            {"label": "Role", "field_type": "text",
             "extraction_hint": "job title — site manager, buyer, owner"},
            {"label": "Phone", "field_type": "text",
             "extraction_hint": "phone or mobile number in signatures or the body"},
        ),
        "deal": (
            {"label": "Equipment", "field_type": "text",
             "extraction_hint": "the kit wanted — machine type, size, attachments"},
            {"label": "Hire period", "field_type": "text",
             "extraction_hint": "dates or duration — weekend, week, long-term"},
        ),
    },
    "events": {
        "company": (
            {"label": "Event types", "field_type": "text",
             "extraction_hint": "what they run — weddings, corporate, festivals"},
            {"label": "Typical headcount", "field_type": "number",
             "extraction_hint": "covers, guests or headcount they mention"},
        ),
        "contact": (
            {"label": "Role", "field_type": "text",
             "extraction_hint": "job title — event manager, coordinator, couple/organiser"},
            {"label": "Phone", "field_type": "text",
             "extraction_hint": "phone or mobile number in signatures or the body"},
        ),
        "deal": (
            {"label": "Event date", "field_type": "date",
             "extraction_hint": "the event date or window"},
            {"label": "Headcount", "field_type": "number",
             "extraction_hint": "covers, guests or attendees for this event"},
            # Special category (security review M39): food allergies are health
            # data, so this field is never filled from email and never sent to
            # the AI — the owner types it in by hand.
            {"label": "Dietary notes", "field_type": "text", "sensitive": True,
             "extraction_hint": "dietary requirements — vegan, halal, allergies "
                                "(the owner fills this in by hand; the agent never reads it)"},
        ),
    },
    # ── Practice OS field library (docs/os/practice.md §9.3) ──────────────────
    # The non-clinical patient fields (insurer, preferred contact, treatment)
    # self-fill from the inbox. The "Allergies / medical" field is marked
    # "sensitive" (special category, security review M39): crm_fields never
    # fills it from email, never suggests a value for it, never probes for it,
    # and crm_intel never puts it into a prompt. The owner fills it by hand.
    # Nothing routes it to the encrypted practice_medical_notes table.
    "dentist": {
        "company": (
            {"label": "Insurer", "field_type": "text",
             "extraction_hint": "dental plan or insurer — Denplan, Bupa, NHS exempt, self-pay"},
        ),
        "contact": (
            {"label": "Preferred contact", "field_type": "dropdown",
             "options": ["Email", "Phone", "SMS", "WhatsApp"],
             "extraction_hint": "how they ask to be contacted"},
            {"label": "Recall cadence", "field_type": "text",
             "extraction_hint": "how often they're recalled — 6 months, 12 months, 3-month hygiene"},
            {"label": "Usual practitioner", "field_type": "text",
             "extraction_hint": "the dentist or hygienist they normally see"},
            {"label": "Allergies / medical", "field_type": "text",
             "sensitive": True,
             "extraction_hint": "stated allergies or medical conditions — latex, penicillin, "
                                "on blood thinners (the owner fills this in by hand; "
                                "the agent never reads it)"},
        ),
        "deal": (
            {"label": "Treatment", "field_type": "text",
             "extraction_hint": "what they're booked/asking for — check-up, filling, whitening, extraction"},
            {"label": "Treatment code", "field_type": "text",
             "extraction_hint": "any treatment/course code mentioned"},
        ),
    },
    "salon": {
        "company": (
            {"label": "How they found us", "field_type": "text",
             "extraction_hint": "referral, walk-in, social, returning client"},
        ),
        "contact": (
            {"label": "Preferred contact", "field_type": "dropdown",
             "options": ["Email", "Phone", "SMS", "WhatsApp"],
             "extraction_hint": "how they ask to be contacted"},
            {"label": "Rebook cadence", "field_type": "text",
             "extraction_hint": "how often they come in — every 5 weeks, 6-weekly, monthly"},
            {"label": "Usual stylist", "field_type": "text",
             "extraction_hint": "the stylist, barber or therapist they normally see"},
            {"label": "Allergies / medical", "field_type": "text",
             "sensitive": True,
             "extraction_hint": "stated allergies or skin sensitivities — patch-test flags, "
                                "colour reactions (the owner fills this in by hand; "
                                "the agent never reads it)"},
        ),
        "deal": (
            {"label": "Service", "field_type": "text",
             "extraction_hint": "what they're booked/asking for — cut, colour, treatment, blow-dry"},
        ),
    },
    "physio": {
        "company": (
            {"label": "Insurer", "field_type": "text",
             "extraction_hint": "insurer or funder — Bupa, AXA, self-pay, referral scheme"},
        ),
        "contact": (
            {"label": "Preferred contact", "field_type": "dropdown",
             "options": ["Email", "Phone", "SMS", "WhatsApp"],
             "extraction_hint": "how they ask to be contacted"},
            {"label": "Recall cadence", "field_type": "text",
             "extraction_hint": "review/recall interval — 6-week review, end-of-block check-in"},
            {"label": "Usual practitioner", "field_type": "text",
             "extraction_hint": "the physiotherapist they normally see"},
            {"label": "Allergies / medical", "field_type": "text",
             "sensitive": True,
             "extraction_hint": "stated conditions or contraindications — injuries, medications "
                                "(the owner fills this in by hand; the agent never reads it)"},
        ),
        "deal": (
            {"label": "Treatment", "field_type": "text",
             "extraction_hint": "what they're booked/asking for — assessment, treatment block, sports massage"},
            {"label": "Referral", "field_type": "text",
             "extraction_hint": "any GP/consultant referral or scheme mentioned"},
        ),
    },
    # ── Storefront OS field library (docs/os/storefront.md §9) ────────────────
    # The customer is a CRM contact/company, so these seed contact/company custom
    # fields. Storefront tracks TRANSACTIONS, not deals — deal seeds stay empty.
    "farm_shop": {
        "company": (
            {"label": "Account type", "field_type": "dropdown",
             "options": ["Retail", "Trade/wholesale", "Restaurant"],
             "extraction_hint": "whether they buy retail or on a trade account"},
        ),
        "contact": (
            {"label": "Regular for", "field_type": "text",
             "extraction_hint": "products they buy repeatedly — bread, veg box, cheese"},
            {"label": "Collection day", "field_type": "text",
             "extraction_hint": "the day they usually collect"},
        ),
        "deal": (),
    },
    "deli": {
        "company": (
            {"label": "Account type", "field_type": "dropdown",
             "options": ["Retail", "Trade/wholesale", "Restaurant"],
             "extraction_hint": "whether they buy retail or on a trade account"},
        ),
        "contact": (
            {"label": "Regular for", "field_type": "text",
             "extraction_hint": "cuts or products they order repeatedly"},
            {"label": "Collection day", "field_type": "text",
             "extraction_hint": "the day they usually collect"},
        ),
        "deal": (),
    },
    "bakery": {
        "company": (
            {"label": "Account type", "field_type": "dropdown",
             "options": ["Retail", "Trade/wholesale", "Cafe/restaurant"],
             "extraction_hint": "whether they buy retail or on a standing/trade order"},
        ),
        "contact": (
            {"label": "Standing order", "field_type": "text",
             "extraction_hint": "any regular/standing order — loaves, pastries per day"},
            {"label": "Collection day", "field_type": "text",
             "extraction_hint": "the day they usually collect or want delivery"},
        ),
        "deal": (),
    },
    "wine_merchant": {
        "company": (
            {"label": "Account type", "field_type": "dropdown",
             "options": ["Retail", "Trade", "Restaurant"],
             "extraction_hint": "retail customer or a trade/restaurant account"},
        ),
        "contact": (
            {"label": "Prefers", "field_type": "text",
             "extraction_hint": "styles/regions/lines they buy — reds, natural, Burgundy"},
            {"label": "Collection day", "field_type": "text",
             "extraction_hint": "when they usually collect"},
        ),
        "deal": (),
    },
    "nursery": {
        "company": (
            {"label": "Account type", "field_type": "dropdown",
             "options": ["Retail", "Trade/landscaper", "Grower"],
             "extraction_hint": "retail gardener or a trade/landscaper account"},
        ),
        "contact": (
            {"label": "Regular for", "field_type": "text",
             "extraction_hint": "plants/lines they buy — bedding, shrubs, compost"},
            {"label": "Collection day", "field_type": "text",
             "extraction_hint": "the day they usually collect"},
        ),
        "deal": (),
    },
}

# Events OS (docs/os/events.md §9) — catering + planners run the same event primitive,
# so they share the event venue's CRM field seeds (event date / headcount / dietaries).
for _ev in ("caterer", "wedding_planner"):
    CRM_SEEDS[_ev] = CRM_SEEDS["events"]

# Default starter catalogue per Storefront industry (docs/os/storefront.md §9,
# SHOP_SEEDS) — a minimal seed the OCR/manual stock-in then grows. Optional/nice
# -to-have in Phase 1: not auto-applied by onboarding yet (the confirm-catalogue
# sub-step is Phase 3), kept here so the wiring has a home. `lead` = the
# best-before lead default in days (a bakery clears fast, wine effectively never).
SHOP_SEEDS = {
    "farm_shop": {"lead": 2, "products": ["Free-range eggs", "Sourdough loaf",
                                          "Seasonal veg box", "Local cheddar", "Whole milk"]},
    "deli":      {"lead": 2, "products": ["Sliced ham", "Chorizo", "Olives",
                                          "Fresh pasta", "Parmesan"]},
    "bakery":    {"lead": 1, "products": ["Sourdough loaf", "Croissant",
                                          "Sausage roll", "Cinnamon bun", "White bloomer"]},
    "wine_merchant": {"lead": 0, "products": ["House red", "House white",
                                              "Champagne", "Craft lager", "Single malt"]},
    "nursery":   {"lead": 5, "products": ["Bedding plants tray", "Compost 50L",
                                          "Shrub — 3L pot", "Herb pot", "Seed potatoes"]},
}


def shop_seed_for(industry: str | None) -> dict | None:
    """The default starter catalogue for a Storefront industry, or None for others."""
    if not industry:
        return None
    return SHOP_SEEDS.get(industry.strip().lower())


def sensitive_seed_fields() -> frozenset:
    """(entity, label) of every seeded field a preset marks "sensitive" —
    special-category data (health, medical, allergy). crm_fields matches a
    tenant's stored definitions against this, because the flag itself is not
    a column on crm_field_definitions (security review M39)."""
    out = set()
    for seeds in (_GENERIC_CRM_SEEDS, *CRM_SEEDS.values()):
        for entity, fields in seeds.items():
            for f in fields:
                if f.get("sensitive"):
                    out.add((entity, f["label"]))
    return frozenset(out)


def crm_seeds_for(industry: str | None) -> dict:
    """The CRM field suggestions for an industry id — the generic set for
    unknown/free-text/empty (everyone gets a sensible starting point)."""
    if industry:
        seeds = CRM_SEEDS.get(industry.strip().lower())
        if seeds:
            return seeds
    return _GENERIC_CRM_SEEDS
