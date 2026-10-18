# TMS baseline — the logistics add-on, end to end (DEMO-GRADE)

> **Status (2026-09-27, confirmed by the clean-up pass):** BUILT (5d3d671, 8b0440a), demo-grade: live click-through on the demo tenant 2026-06-20. Tracking simulated; the map needs the Maps key.

**Status:** BUILDING 2026-06-20. Origin: `docs/archive/2026-07/tms-discovery-brief.md` found ~60–70%
of a baseline TMS already shipped (intake → capacity → book → manifest → planner)
and named the 7 missing pieces. Julius's call: build a *demo-grade* version of all
7 so he can record a video, take it to UK hauliers, and run discovery to tailor
the real thing. End goal: any logistics company switches on TMS and has a whole
operation, not a half.

## Goal
One paid **TMS add-on** that turns the existing transport board into a full
transport operation: price a job, dispatch it to a driver, prove delivery, let
the customer track it, see the fleet move, stay compliant, and invoice — every
screen live off the same `bookings` diary the email agent already writes into.
Demo-grade means: real data, real primary action per surface, honest stamps where
a real integration (GPS, card payments) would come later.

## The architectural move
Two moves.

1. **Packaging — Core vs vertical add-on.** Email + Scheduling become the
   **Core** every vertical buys. **Fleet/Capacity is not its own product** — it is
   the spine of **TMS**. So the separate `fleet` and `routing` subscription
   modules **collapse into one `tms` module** that bundles Fleet · Capacity ·
   Routing · Planner **+ the 7 new surfaces**. Flipping one `subscriptions` row
   (`module='tms'`) is the whole rollout, exactly as before. This matches the
   billing copy already in `config.py` (the £299 tier is *"BLAM8 Core + TMS"*).

2. **One event spine.** A new `shipment_events` table records a booking's
   lifecycle (`booked → loaded → out_for_delivery → delivered/failed`, plus
   signature/photo on the POD event). **The same rows power POD, the driver app,
   live tracking, and the customer portal** — four surfaces, one source of truth,
   all derived from the diary. No parallel stacks.

Everything else is the established platform pattern (fifth outing): a surface =
a pure view-model (`config + rows in → render-ready dict out`, unit-tested with no
DB, à la `src/app/tms.py` `overview()`) + a thin fetch wrapper + thin `db.py`
functions + a Jinja template, gated by the module row.

## Decisions

### D1 — Full merge, not a presentation alias
`fleet` + `routing` are replaced by `tms` everywhere the module is gated
(`dashboard.py`, `pipeline.py`, `admin.py`, `calendar_sync.py`). There is no
longer a "fleet without routing" tier — **TMS always includes routing + planner**
(the brief treats them as one product). A migration converts existing
subscription rows (`fleet`/`routing` → `tms`) so the live pilot keeps working.
The operator switchboard flips one `tms` toggle; activating it seeds the fleet
defaults (pallet types + Main branch + a default rate card).

### D2 — Pricing = a per-tenant rate card, miles from postcodes
`rate_cards` (one default per tenant): `base_charge + per_mile + per_pallet +
per_drop + fuel_pct`, with a `min_charge` floor. A job's quote is computed live
from its `spaces`/pallets and the miles between `collection_postcode` and
`delivery_postcode` (reusing `scheduling.haversine_miles()` + the `postcodes`
cache via `db.get_postcodes()`; no miles when a postcode is missing — the row
says so). "Quote" stores `bookings.quoted_amount` so invoicing can read it back.
Editable rate card in the UI; read-only fallback when unset.

### D3 — POD writes an event and completes the booking
Marking a drop **delivered** (typed signature + optional note/photo URL) or
**failed** inserts a `shipment_events` row and sets the booking `completed`
(delivered) — the status the diary already understands and excludes from active
capacity. This is the single write path shared by the office POD board
(`/app/tms/pod`) and the driver app (`/app/driver`). Photo is a URL field for the
demo (honest: "photo capture wires to the driver's camera in production").

### D4 — Driver app is a mobile surface over the manifest
`/app/driver?vehicle=…` renders today's manifest (reusing
`scheduling.manifest_view()`) as a tap-through stop list with minimal chrome,
each stop opening the D3 POD action. Authenticated for the demo; a tokenised
per-driver link is the production follow-on.

### D5 — Invoicing groups delivered jobs by customer
`bookings.contact` is the customer identity (the schema's "cross-module identity
seed"). "Generate for period" groups `completed` bookings by contact over a date
range into an `invoices` + `invoice_lines` draft, each line = a booking at its
`quoted_amount` (recomputed from the rate card when unset), VAT at 20%, ref minted
like booking refs (`tenant_counters`). Status `draft → sent → paid` is a manual
flip for the demo (no Stripe — that's the follow-on).

### D6 — Customer portal is a public, tokenised timeline
`bookings.track_token` (random) backs a public `GET /track/{token}` page (no auth,
no app chrome) showing the shipment's `shipment_events` as a status timeline +
POD when delivered. The cockpit exposes a "share tracking link" per booking. This
is the same data the portal, tracking map, and POD board read.

### D7 — Live tracking is honest simulation
`/app/tms/tracking` plots each active vehicle's position for today **interpolated
along its stops by how many drops are marked delivered** — there is no GPS feed.
The page says so plainly ("simulated for the demo — integrates with your existing
tracker"). Reuses the Google Maps JS pattern from `fleet_route.html` +
`settings.google_places_api_key`. This is the one surface that is deliberately
not "real" — it shows the shape a tracker integration would fill.

### D8 — Compliance is dates vs today, RAG-flagged
`drivers` (licence/CPC expiry) + new `vehicles` columns (MOT/tax/service/
insurance) + a seeded `driver_hours` table. `/app/tms/compliance` shows two
tables with red/amber/green status computed from each date vs today and hours vs
a weekly limit. Pure date math; no DVLA/tacho integration (named follow-on).

### D9 — Feature-request form, in-app only
`feature_requests` (tenant + `submitted_by` + category + body + auto-tagged
`industry` from `tenants.industry`). Tenant form at `/app/requests` (knowledge-
page CRUD pattern: csrf + `?msg=` redirect) with the tenant's own submissions
listed; operator sees all at `/admin/requests` (service role, `require_admin`).
A public landing-page variant is a named follow-on, not built now.

## Data model (new)
- `drivers`, `rate_cards`, `shipment_events`, `invoices`, `invoice_lines`,
  `driver_hours`, `feature_requests` — all tenant-isolated (RLS) like every table.
- `vehicles` += `driver_id`, `mot_expiry`, `tax_expiry`, `service_due`,
  `insurance_expiry`.
- `bookings` += `quoted_amount`, `track_token`.
- Subscriptions: `fleet`/`routing` rows converted to `tms`.

## Surfaces & files
Pure modules: `src/app/pricing.py`, `src/app/pod.py`, `src/app/invoicing.py`,
`src/app/tracking.py`, `src/app/compliance.py`, `src/app/driver.py`. Templates under
`src/app/templates/app/` (`tms_*.html`, `driver.html`, `requests.html`,
`_tms_nav.html`) + public `src/app/templates/track.html` + `admin/requests.html`.
Routes in `dashboard.py` (public `/track` in `main.py`). A `_tms_nav.html` tab
strip (Cockpit · Board · Fleet · Pricing · POD · Invoices · Tracking ·
Compliance) ties the cockpit together.

## Out of scope (discovery follow-ons)
Real GPS/telematics; Stripe-paid invoices; the email agent auto-inserting
quotes/tracking links into replies; multi-driver-per-van scheduling; a public
landing-page request form. These are deliberately left for what hauliers tell us.

## Tests
Each pure view-model gets a no-DB unit test mirroring `tests/test_tms.py`
(dict factories in, assert on the returned dict). The merge updates the existing
gating tests to the `tms` vocabulary.
