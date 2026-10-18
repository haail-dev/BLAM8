# Route planning & vehicle manifests — the fleet extension (SCOPE — LOCKED)

> **Status (2026-09-27, confirmed by the clean-up pass):** BUILT steps 1–4 (769a80f, e63c150, 94d1936); the planner proven live 2026-06-11. Split orders, attachment ingestion and event-driven proposals NOT built.

**Status:** LOCKED 2026-06-11 — Julius greenlit the build same day; all four
open questions resolved with the stated recommendations (Q1 view-then-optimise
slices; Q2 two postcodes on the booking, NO drops table; Q3 button + freed-
capacity banner, event-driven proposals deferred to v2; Q4 manifests out →
orders in → attachment ingestion). Build order = the staircase below.
Origin: 2026-06-11 conversation — fleet E2E passed, marketing shipped, and the
gap surfaced: bookings ride a `vehicle_id` but there is no registration, no
structured drop locations, no per-van manifest, no map, and no way to react
when a cancellation frees a van. This module is the deck's Tier 4 made real,
and the formally-named v2 of fleet D1 (load planning + split orders) unlocks
at its top step.

## Goal
A dispatcher clicks a depot, clicks a vehicle, and sees its day: the
**manifest** (every booking on that vehicle-day with refs, pallets, weights,
postcodes) and the **route on a map**. Spreadsheets flow both ways — manifests
out, order books in. A **planner agent** watches the fleet state and proposes
re-allocations — a cancellation frees the 18t, so it suggests consolidating
two part-loads onto one van — and nothing moves until a human approves.
Draft-first, extended from replies to plans.

## The architectural move
Fleet made capacity real (demand has a size, supply is a pool of vehicles).
Route planning makes **location** real: a booking gains structured collection
and delivery postcodes, a branch gains an address, a vehicle gains a
registration. Everything downstream — manifest, map, optimised stop order,
spreadsheet round-trip, the planner — is a view or an agent over that data.
Fifth outing of the platform pattern: tables + tools + a surface, gated by a
`subscriptions` row; the scheduling/fleet engine stays the single source of
capacity truth (every move the planner applies re-runs the atomic RPC — a
plan can never overload a van).

## The staircase (build order)
1. **Foundations** — schema (registration, depot address, drop postcodes,
   booking reference), agent extraction via tool args, manifest view.
2. **Map** — vehicle-day route on Google Maps (depot origin → drops);
   optimised stop order + drive times (Q1: slice 1 or slice 2).
3. **Spreadsheets** — manifests out (xlsx/csv), order books in (xlsx/csv
   upload → preview → atomic best-fit per row; CSV is also the no-OAuth
   bridge for Google Sheets users — File → Download → CSV), fabricated
   demo dataset.
4. **Planner agent** — propose→approve plans: consolidation on cancellation,
   re-allocation, and (here, formally) split orders.

## Decisions
### D1 — Drop model: two postcodes on the booking, no drops table (Q2)
One booking = one collection point + one delivery point
(`collection_postcode`, `delivery_postcode`, both optional — UK postcodes are
enough for planning-quality routing). Multi-drop reality is already modelled:
a vehicle-day holds MANY bookings, and the route is the set of their points.
A single order with several delivery sites is entered as several bookings.
**Q2 (open):** does any near-term tenant need true multi-drop per booking?
If yes, a `drops` table goes in NOW (schema churn later is worse); default
recommendation is NO for v1.

### D2 — Tools stay channel-agnostic; extraction is the contract
`hold_booking` gains optional `{collection_postcode, delivery_postcode,
reference}` (reference = the customer's order/invoice ref — "tag the delivery
invoice to the vehicle"). The executor stores them on the booking; replies
never mention routing or vans (fleet D5 stands). Windows-mode and
fleet-without-routing tenants see byte-identical tools — the routing variant
is injected only when the routing row is active. Voice will call the same
tools unchanged.

### D3 — Maps: Google, display-first, per vehicle-day
Google Maps JS embed + Routes/Directions API. v1 scope: one vehicle-day at a
time — depot (branch address) as origin, drops as waypoints, polyline on the
map, drive-time totals. `optimizeWaypointOrder` gives single-van stop-order
optimisation almost free (Q1 decides which slice). Geocode failures pin the
drop as "unlocated" and the route renders without it — never blocks the
manifest. NOT in scope: live tracking/telematics, traffic re-planning,
driver app. Ops item: billing-enabled Google Maps API key (pennies at pilot
volume).

### D4 — Spreadsheets: files first, Sheets sync explicitly rejected (Q4)
Order of delivery (Q4 to confirm): **manifests out** (download a vehicle-day
or whole-day board as xlsx/csv) → **orders in** (upload an order book; every
row preview-validated, then booked through `hold_fleet_booking` — a bulk
import cannot overload a van by construction; row statuses: booked / no-fit
(alternatives listed) / bad-row, never silent skips) → **email-attachment
ingestion** (the pipeline already sees attachments; "forward us your order
book and it's booked" is the wedge demo).
**Live Google Sheets sync REJECTED for v1**: a sensitive OAuth scope while
the Google app is still in verification would cost more than it returns;
files get 90% of the value.
**Demo dataset:** fabricate a realistic haulier order book (fictional
customers, real-shaped UK lanes/refs/pallet mixes — Wilmots-shaped, never
Wilmots data) on a dedicated DEMO tenant, kept off the HaailLTD pilot. The
same files are the importer's test fixtures.

### D5 — Planner agent: plans are drafts (Q3 for triggers)
A second agent persona whose tools read/write fleet state, never an inbox:
`get_day_state` (per-van loads + manifests), `propose_plan` (a batch of
moves: move booking → van/date, merge part-loads, split an order), and an
apply step. A plan is a **proposed** row a human reviews on the board
(diff view: before/after load bars); **approve applies every move through
the atomic capacity RPC in one transaction** — any move that no longer fits
fails the whole plan with a reason (stale plans die loudly, not quietly).
Split orders (fleet D1's named v2) are legal ONLY inside an approved plan.
Optimisation v1 is heuristic (re-run best-fit, consolidation rules: fewest
vans, then fewest total miles via Routes API estimates) — OR-Tools/VRP is the
named escalation when a tenant's scale demands it, not before.
**Q3 (open):** triggers — button-only ("Replan this day") in v1, or also an
event nudge when a cancellation frees capacity? Recommendation: button in
v1 PLUS a passive banner ("capacity freed on Fri — review?"), full
event-driven proposals in v2.
**Amendment (Julius, 2026-06-11, post-lock):** the planner's first job is
**pallet optimisation** (consolidate part-loads onto fewer vans), and a
tenant setting `planner_auto_apply` (default OFF) mirrors `auto_send`:
propose→approve stays the default posture; switching it on lets accepted
plan shapes apply themselves — the same earn-trust-then-automate ladder as
drafting. Settings define the guardrails (e.g. never auto-move a booking to
a different DATE without approval; vehicle moves within the same day are
the auto-appliable class).

### D6 — Own module row, requires fleet
`subscriptions` row `module='routing'`, valid only with fleet active (the
admin toggle enforces it). Separately sellable (the deck's Tier 4), zero new
code paths for fleet-only tenants. Hub: the Fleet card gains a "Routes"
affordance when routing is on; no new hub card in v1.

## Schema (one migration, via the POOLER)
```sql
alter table vehicles  add column if not exists registration text;          -- number plate
alter table branches  add column if not exists address  text;
alter table branches  add column if not exists postcode text;              -- route origin
alter table bookings  add column if not exists collection_postcode text;
alter table bookings  add column if not exists delivery_postcode  text;
alter table bookings  add column if not exists reference          text;    -- customer order/invoice ref
create table if not exists plans (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references tenants(id) on delete cascade,
  plan_date   date not null,
  status      text not null default 'proposed',  -- proposed | applied | discarded
  summary     text,                              -- agent's one-line rationale
  moves       jsonb not null,                    -- [{booking_id, to_vehicle_id, to_date, split:{...}}]
  created_at  timestamptz not null default now(),
  resolved_at timestamptz
);
-- RLS: tenant-isolation policy on plans (same shape as bookings).
-- Apply = transaction: per move, release old hold + hold_fleet_booking on the
-- target; ANY failure rolls back the whole plan. The RPC stays the only door.
```

## Wiring (smallest surface)
- **scheduling.py:** manifest builder (pure: vehicle-day → ordered drop list),
  plan validation/apply helpers, tool-schema routing variant.
- **db.py:** postcode/reference persistence, plans CRUD, manifest queries.
- **agent.py:** unchanged mechanics; `<scheduling>` prompt gains one line
  (capture postcodes/reference when stated — facts from the email only).
- **planner.py (new):** the planner persona — prompt, tools, propose loop.
  Metered like drafting (`usage_events` op `plan`).
- **dashboard.py + templates:** vehicle-day manifest page + map embed
  (Maps JS, server passes waypoints), xlsx export endpoint, xlsx import
  (upload → preview table → commit), plan review panel on the fleet board
  (before/after bars, approve/discard).
- **admin.py:** `TOGGLABLE_MODULES += 'routing'` (guard: fleet required).
- **Fabrication script:** generates the demo order book xlsx (also a fixture).

## Edge cases
- Booking without postcodes (legacy/unstated): on the manifest, off the map,
  counted in a "n unlocated" chip — the draft already says what it assumed.
- Geocode failure: drop pinned unlocated; route renders without it.
- Plan staleness: board changed since proposal → apply re-validates every
  move; one failure = whole plan rejected with the failing booking named.
- Optimised stop order is advisory — the manifest prints both entry order
  and suggested order; the driver's reality wins.
- Import: duplicate references flagged in preview (warn, not block);
  rows too big for any van listed with alternative dates (never silently
  dropped); unknown pallet types → full, per the existing rule.
- Cancelled booking inside a proposed plan → plan dies loudly on apply.

## Verification
- **Unit (first, as always):** manifest ordering, xlsx round-trip
  (export → import → identical bookings), import preview statuses, plan
  apply atomicity (one bad move rolls back all), postcode/reference
  extraction in the executor, routing-off tenants byte-identical.
- **Live (demo tenant):** seed fabricated order book → board fills →
  depot → van → map shows the route → manifest xlsx downloads → cancel one
  booking → planner proposes consolidation → approve → board + map reflect
  it → an email order with postcodes lands on the map end-to-end.

## Out of scope (v1)
Live Google Sheets sync, telematics/GPS/live tracking, driver-facing views,
traffic-aware or multi-day re-planning, OR-Tools/VRP solving (named
escalation), customer-facing ETAs, multi-drop single bookings (unless Q2
flips), price-per-mile quoting.

## Open questions — RESOLVED at lock (2026-06-11)
- **Q1** — Stage-one slice = foundations + manifest + plain map; optimised
  stop order ships in slice two. ✔ locked
- **Q2** — Two postcodes on the booking, NO drops table; a single order with
  several delivery sites is entered as several bookings. Revisit only when a
  real tenant needs true multi-drop per order. ✔ locked
- **Q3** — Planner triggers v1 = "Replan this day" button + a passive
  freed-capacity banner; full event-driven proposals are v2. ✔ locked
- **Q4** — Spreadsheets land manifests-out → orders-in → email-attachment
  ingestion, in that order. ✔ locked
