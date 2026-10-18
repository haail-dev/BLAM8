# Fleet / pallet capacity — vehicles as the unit of capacity (SCOPE — LOCKED)

**Status:** BUILT 2026-06-10 (steps 1–3: schema live on prod + RPC smoke-tested
in a rolled-back transaction; engine + tools; all surfaces; 188 tests green).
Remaining: step 4 — live E2E on the pilot tenant with Julius (configure 2
branches / 3 vans, email a booking through full-auto), then the demo story.
Locked 2026-06-10 — all four open questions answered with Julius
(one vehicle per order in v1, v2 is planning + split orders; spaces + type
factors + legal payload limit; own sellable module REPLACING the Inventory
hub card, sharing the scheduling engine; branches + per-mailbox defaults).
Origin: module-roadmap conversation after enterprise multi-mailbox shipped —
the competitive research named agent-placed booking with real capacity
enforcement as BLAM8's highest-value differentiator and operational depth as
the gap the logistics vertical expects closed.

## Goal
A haulier models its **branches** and **vehicles** (pallet spaces + legal
payload); inbound orders — email today, voice later — are checked against real
vehicle-day capacity and **booked onto a specific van** end to end: with
full-auto on, "6 pallets to Leeds Friday?" comes back booked with no human in
the loop. The tools are channel-agnostic on purpose: the voice module will
call exactly the same capacity brain.

## The architectural move
Scheduling v1 counts bookings against a per-window number. Fleet grows the
capacity model a dimension: **demand has a size (pallet spaces, optionally
weight) and supply is a pool of vehicles, each with its own size.** Everything
around the capacity decision — atomic holds, proposed→confirmed lifecycle,
approve-confirms / skip-releases (D4), the Diary, calendar push, the tool
loop — is the existing scheduling machinery, reused. Fourth outing of the
platform pattern: a module = tables + tools + a surface, gated by a
`subscriptions` row; the engine stays shared.

## Decisions
### D1 — One vehicle per order (v1)
An order must fit on ONE vehicle-day. Vehicle selection is **best-fit**: the
active vehicle with the smallest remaining space that still fits (ties →
smallest capacity, then id — deterministic), so big vans stay free for big
orders. A vehicle-day holds many orders up to its capacity — multi-drop
reality without routing. Orders too big for any single van: the agent offers
alternative dates or defers to a colleague — it never promises a split.
**v2 (named, deliberately out):** load planning + split orders across
vehicles/days.

### D2 — Demand = pallet spaces via type factors; weight = the legal limit
`pallet_types` seeded per tenant: full 1.0 · Euro 1.0 · half 0.5 ·
quarter 0.25 (editable later; unknown type in an email is treated as full —
conservative, and the draft says what it assumed). Demand = Σ qty × factor.
`vehicles.max_payload_kg` is the **legal second constraint**: enforced
whenever the order's weight is known (agent extracts it when stated); orders
with no stated weight pass the spaces check only — the spec is honest that
weight enforcement is only as good as what customers declare. Both
constraints must pass when known.

### D3 — Own module, shared engine; Fleet replaces Inventory on the hub
`subscriptions` row `module='fleet'` is the whole rollout: the hub card
lights (the roadmap **Inventory card is replaced by Fleet**), the Diary
becomes the fleet board, the draft call gains fleet-aware tools, Settings
gains the Fleet page. Implementation lives inside the scheduling engine
(scheduling.py / the bookings table / the Diary), NOT a parallel stack.
Co-existence rule: **fleet supersedes window capacity** — when the fleet row
is active the windows/capacity_per_window config is ignored and the vehicle
pool is the capacity source; a tenant runs either windows-mode scheduling or
fleet, never both at once. The pipeline's module gate widens to
`scheduling OR fleet`. Rejected: a fourth `windows` mode inside the
scheduling subscription (not separately sellable) and a fully separate module
(duplicates trusted holds machinery).

### D4 — Branches now, per-mailbox defaults (composes with multi-mailbox)
`branches` (name per tenant); `vehicles.branch_id`. A single-branch tenant
sees "Main" and never thinks about it. Branch resolution for a booking:
explicit ask in the email → the receiving mailbox's `default_branch_id`
(enterprise: orders@leeds books Leeds vans) → the tenant's only branch →
otherwise the agent asks the customer. Rejected for v1: branch-level
opening-hours/config inheritance (departments-as-config all over again —
later, with that refactor).

### D5 — Tools stay channel-agnostic and never leak the fleet
`check_availability` and `hold_booking` gain optional
`{pallets: [{type, qty}], weight_kg, branch}`; the executor computes spaces
from factors and the RPC assigns the vehicle. Replies talk capacity, never
vans ("we can take 6 pallets Friday" — vehicle assignment is an internal
detail the Diary shows the tenant). One-hold-per-email stands. Windows-mode
tenants see byte-identical tools (fleet arguments only injected when the
fleet row is active).

### D6 — The Diary becomes a fleet board in fleet mode
Month view: day badge = spaces used / fleet total (branch filter when >1).
Day panel: one row per vehicle with a load bar (5/8 spaces · kg when known)
and its booking chips; manual add takes pallets + type + branch; confirm /
decline / cancel and calendar push behave exactly as today (event summary
gains the load: "6 plt → LS9").

## Schema (one migration, via the POOLER)
```sql
create table if not exists branches (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete cascade,
  name text not null,
  created_at timestamptz not null default now()
);
create table if not exists vehicles (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete cascade,
  branch_id uuid references branches(id) on delete set null,
  name text not null,                    -- "18t curtainsider" / reg
  capacity_spaces numeric(6,2) not null, -- pallet spaces
  max_payload_kg numeric(8,1),           -- legal limit; null = not enforced
  active boolean not null default true,  -- inactive: kept for history, no new holds
  created_at timestamptz not null default now()
);
create table if not exists pallet_types (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete cascade,
  name text not null,                    -- full | euro | half | quarter (seeded)
  space_factor numeric(4,2) not null,
  unique (tenant_id, name)
);
alter table bookings add column if not exists vehicle_id uuid
  references vehicles(id) on delete set null;   -- null = windows-mode booking / unassigned
alter table bookings add column if not exists spaces    numeric(6,2);
alter table bookings add column if not exists weight_kg numeric(8,1);
alter table mailbox_config add column if not exists default_branch_id uuid
  references branches(id) on delete set null;
-- RLS: tenant-isolation policies on branches/vehicles/pallet_types (same shape).
-- hold_fleet_booking RPC (the ONE atomic capacity decision, like
-- hold_booking_if_capacity): advisory xact lock per (tenant, date);
-- best-fit across active vehicles in the branch where
--   remaining_spaces >= p_spaces AND (p_weight is null OR max_payload_kg is null
--   OR remaining_kg >= p_weight);
-- inserts the booking with vehicle_id; EMPTY result = no van fits.
```

## Wiring (smallest surface)
- **scheduling.py:** fleet availability calc + fleet executor branch +
  `diary_line` fleet flavour ("Fleet: 5 vans · 38 spaces · 2 branches");
  fleet-aware `calendar_weeks` payload.
- **db.py:** branches/vehicles/pallet_types CRUD, fleet hold RPC wrapper,
  vehicle-day load queries, mailbox default-branch get/set.
- **agent.py:** unchanged mechanics; the `<scheduling>` prompt section gains
  fleet wording (capacity facts ONLY from tools — the existing rule).
- **pipeline.py:** module gate becomes scheduling-or-fleet; executor built
  fleet-aware; everything else identical (D4 hold lifecycle reused).
- **dashboard.py + templates:** Fleet settings page (branches + vehicles
  CRUD, pallet-type factors read-only v1), Diary fleet board, hub MODULES
  swap (inventory → fleet), mailbox settings gains default branch (enterprise).
- **admin.py:** `TOGGLABLE_MODULES += 'fleet'`.

## Edge cases
- Vehicle deactivated/deleted with future bookings: bookings keep their date,
  `vehicle_id` nulls on delete → Diary shows them "unassigned" for a human to
  re-place; deactivation only blocks NEW holds.
- Zero vehicles configured + fleet active: tools report not-configured; the
  agent defers ("a colleague will confirm") rather than inventing capacity.
- Legacy windows bookings during a tenant's switch to fleet: render alongside
  the board (date-grouped), counted against nothing.
- Unknown pallet type → full (1.0) and the draft says so; no stated weight →
  spaces-only check.
- CC'd-to-two-mailboxes: two drafts may each place a hold (same as today) —
  capacity protects itself; approving one and skipping the other releases.
- Fractional spaces (half/quarter pallets) display as e.g. "6.5 / 8".

## Verification
- **Unit (first, like scheduling):** best-fit selection incl. ties; weight
  cap with/without stated weight; factor arithmetic; branch resolution chain
  (explicit > mailbox default > only-branch > ask); no-fit → alternatives;
  deactivated vehicles excluded; one-vehicle rule (no silent splits);
  windows-mode tenants byte-identical.
- **Live (pilot):** configure 2 branches / 3 vans → email "6 standard pallets
  Friday" → hold lands on the right van → approve → board shows the load →
  overflow order gets alternatives → full-auto end-to-end from an external
  sender.

## Out of scope (v1)
Load planning + split orders (the named v2), route/distance feasibility,
max-drops-per-vehicle knob (considered; deferred until a tenant asks),
TMS sync (this module is the surface a TMS integration later feeds),
price quoting from rate data, driver hours/working-time rules, per-branch
config inheritance, the voice channel itself (the tools are built ready).

## Build order
1. Schema migration + pallet-type seeding + `hold_fleet_booking` RPC —
   unit-tested against a rolled-back transaction like scheduling's.
2. Engine: fleet availability/executor in scheduling.py + tool surface +
   pipeline gate widening. Windows-mode tenants frozen by tests.
3. Surfaces: Fleet settings (branches/vehicles), Diary fleet board, hub card
   swap, admin toggle, mailbox default branch.
4. Live E2E on the pilot tenant, then the demo story (orders@ books a van
   autonomously).
