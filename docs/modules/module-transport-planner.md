# Transport planner agent + the office/transport diary split (BUILT)

**Status:** BUILT 2026-06-11, same day as the spec — all six build-order steps
shipped; 286 unit tests green; live E2E passed on the pilot's seeded week
(honest no-gain verdict on the seeded Monday, cancel→replan→the box-van
consolidation proposed by the persona, atomic approve, auto-apply, and the
dual office+transport same-day booking — demo state restored byte-identical).
Builds on docs/modules/module-route-planning.md
(LOCKED — its D5/Q3 and the planner_auto_apply amendment are inherited here)
and SUPERSEDES one line of docs/modules/module-fleet-capacity.md D3: "a tenant runs
either windows-mode scheduling or fleet, never both at once" no longer holds —
see D1. Pilot context: the demo week is seeded (BK-1002..BK-1013; Mon 15 = 18t
9.25/10 + box 3/6 — the consolidation demo; Tue 16 = 18t 10/10 full).

**Build addendum (one decision sharpened during live E2E):** the two capacity
models share the bookings table, so they need an explicit discriminator —
`spaces is null` = a PEOPLE row (office), `spaces` set = a GOODS row
(transport). Each diary counts ONLY its own rows: a pallet booking (whose
window is 'day') must never consume office slots, and office bookings never
consume vehicle space. Enforced in scheduling._used + office_rows (display),
in the hold_booking_if_capacity RPC, and the board hides spaces-null rows for
dual tenants (they have their own surface). Windows-only and fleet-only
tenants have no foreign rows, so the filters are no-ops there — the freeze
holds. Also confirmed in E2E: with >1 branch, a dual transport call still
resolves branch per fleet D4 (the agent asks when the email doesn't say).

## Goal
Two things one real business needs at once:

1. **The diary split.** An inbound "can I get half an hour with someone
   Thursday?" books a PERSON into the **office diary** (the existing
   windows/slots scheduling — opening hours, slot minutes, phone-answer days).
   An inbound "4 pallets to Leeds Friday" books GOODS into the **transport
   diary** (the fleet board — vehicles, pallet spaces, payload). Both live on
   the same tenant simultaneously; the agent routes each request to the right
   capacity model.
2. **The planner agent.** The 4pm transport-planner brain, automated: read a
   day's board, propose load consolidations (fewest vans, sensible miles),
   apply approved plans atomically, and — once trusted via settings — apply
   the safe class of moves itself.

## Decisions
### D1 — Two diaries, one tenant (supersedes fleet D3's either/or)
When BOTH scheduling and fleet rows are active, the tenant has two parallel
diaries: **office** (windows/slots model — day, am/pm, or timed slots;
`slot_minutes` already configurable, 15/30-minute chunks are a settings tweak,
not new code) and **transport** (the fleet vehicle pool). Today fleet
supersedes windows; after this build the pipeline injects BOTH capacity
models. Single-module tenants are byte-identical to today — frozen by tests,
as always.

### D2 — Same two tool names, a `diary` argument when both exist
The channel-agnostic contract stands: `check_availability` and `hold_booking`
remain the ONLY two booking tools (voice will call them unchanged). When both
diaries are active, both tools gain a REQUIRED `diary` enum arg
(`"office" | "transport"`) and the executor dispatches to the windows or
fleet path accordingly; the prompt explains the split ("office = timed
appointments for people; transport = pallet capacity for goods — route
invoices/orders/delivery queries to transport, advice calls and meetings to
office"). Tenants with one diary keep today's schemas EXACTLY (the arg is
injected only when both rows are active). Rejected: separate tool-name pairs
(breaks the voice-ready two-name contract) and pipeline-level intent
classification before tool injection (an email can contain both intents; the
model is the router, the tools stay honest).

### D3 — Surfaces: the diary splits into two pages
`/app/diary` = the office diary (the existing windows/slots calendar).
`/app/board` = the transport board (the fleet view that currently squats on
/app/diary in fleet mode). Burger shows both ("Office diary" / "Transport
board") when both modules are on; one module = one entry, today's behaviour.
"Page per lorry" is already real: each vehicle-day's manifest/route pages are
the per-lorry view — the board links to them.

### D4 — Postcode distances without Google
New `postcodes` lookup table (postcode → lat/lng) filled lazily from the free
postcodes.io bulk API (no key, no billing) and cached forever. Plan scoring
uses haversine — straight-line is plenty to rank merges and reject
geographic nonsense (a Hull drop on a Manchester run). Google stays where it
is: map display and (next slice) drop sequencing. If postcodes.io is down,
scoring degrades to fewest-vans only — never blocks a plan.

### D5 — The planner agent: tools, heuristic, plans
A second persona (`src/app/planner.py`), tools over fleet state, never an inbox:
- `get_day_state(date)` → per-vehicle: capacity, payload, branch + depot
  postcode, and current bookings (booking_ref, spaces, weight_kg,
  collection/delivery postcodes, status, `movable` — derived from the
  guardrails: same-day vehicle moves movable, date changes NOT movable by
  the planner v1).
- `propose_plan(date, moves[], summary)` → writes a `plans` row (table
  exists) status=proposed; moves are `{booking_id, to_vehicle_id}` v1
  (same-day only; cross-day moves and split orders stay out until the
  approval flow for date changes exists).
- Heuristic v1 (pure, unit-first in scheduling.py or planner.py):
  first-fit-decreasing bin-pack of the day's bookings onto the fewest
  vehicles that satisfy spaces+payload+branch; tie-break = total haversine
  miles from depot through drops; emit a plan ONLY if it frees ≥1 vehicle or
  cuts estimated miles materially — never churn for churn's sake.
- LLM's role v1 is narration + judgment (write the one-line rationale, veto
  nonsense), not arithmetic: the heuristic computes, the agent explains.
  Metered like drafting (usage op `plan`).

### D6 — Propose → approve → apply (and the auto ladder)
Plans render on the transport board as a diff (before/after load bars per
vehicle, move list with refs). Approve applies every move in ONE transaction
through the atomic RPC (release + re-hold per move; any move that no longer
fits fails the WHOLE plan loudly — stale plans die, never half-apply).
Discard resolves the row. Trigger v1 = "Replan this day" button + the passive
freed-capacity banner (locked Q3). `tenant_config.planner_auto_apply`
(default false) mirrors auto_send: when on, plans containing ONLY the safe
class (same-day vehicle moves) apply themselves and notify; anything else
still waits. Date moves ALWAYS require a human (inherited amendment).

### D7 — Gating and pricing posture
The planner ships inside the `routing` module (requires fleet, as today) —
no new module id for v1. Julius wants per-agent pricing; if the planner
becomes its own SKU later, it gets its own subscriptions row (`planner`,
requiring routing) and the gate widens — one line, priced separately. Deck
Tier 4 already covers the story either way.

## Schema (one migration, via the POOLER)
```sql
create table if not exists postcodes (
  postcode   text primary key,          -- normalised, e.g. 'LS9 8AS'
  lat        double precision not null,
  lng        double precision not null,
  fetched_at timestamptz not null default now()
);
-- no RLS: public reference data, service-role writes only.
alter table tenant_config add column if not exists planner_auto_apply boolean not null default false;
-- plans table already exists (route-planning migration).
```

## Wiring (smallest surface)
- **scheduling.py:** executor gains the diary-arg dispatch (both-active
  tenants only); haversine + plan-scoring helpers (pure); movable derivation.
- **planner.py (new):** persona prompt, get_day_state/propose_plan executors,
  the consolidation heuristic, plan apply (transactional release+re-hold).
- **db.py:** postcodes get/put, plans CRUD, planner_auto_apply in config.
- **agent.py:** `<scheduling>` section gains the two-diaries wording when
  both active; everything else untouched.
- **dashboard.py + templates:** /app/board split-out, plan diff panel
  (approve/discard), "Replan this day" button, freed-capacity banner,
  Settings gains the planner block (auto-apply toggle + guardrail copy).
- **pipeline.py:** gate = both-rows → diary-arg toolset; single-row tenants
  byte-identical (frozen by tests).

## Edge cases
- Plan apply races a new booking: the RPC is the judge; the plan fails whole
  with the blocking ref named.
- A move's vehicle deactivated since proposal: same — fails loudly.
- Unlocated bookings (no delivery postcode): consolidate by size only; the
  plan rationale says "2 drops unlocated — miles estimate partial".
- postcodes.io miss (bad postcode): cache the miss, score without it.
- Office+transport both asked in ONE email: two tools calls, two diaries,
  one reply — the one-hold-per-email rule relaxes to one PER DIARY (test).
- planner_auto_apply on + a mixed plan (vehicle moves + anything else):
  the whole plan waits for a human; never partial-auto.

## Verification
- Unit-first: heuristic (frees-a-van cases, tie-breaks, never-churn,
  payload/branch respected), movable derivation, haversine, diary-arg
  dispatch (single-diary tenants byte-identical — the freeze), plan apply
  atomicity incl. mid-apply failure rollback, auto-apply classification.
- Live (pilot, seeded week): Mon 15 "Replan" → proposes box→18t
  consolidation (9.25+3 = 12.25 > 10, so EXPECT a partial/no-gain verdict —
  the honest case) vs. cancel one 18t booking → replan frees the box (the
  demo); approve → board reflects; auto_apply on → same scenario applies
  itself; office diary books a 2pm call the same day transport books pallets.

## Out of scope (v1)
Cross-day/date-move planning by the planner (approval flow first), split
orders (still the named fleet v2), delivery time windows on transport
bookings (the office diary covers timed PEOPLE; goods windows come when a
tenant demands them), vehicle feature tags (tail-lift etc.), £ cost rates
(lexicographic vans-then-miles until rates exist), route-stop sequencing
(Google optimizeWaypointOrder — own small slice once Maps billing is live),
multi-depot rebalancing, driver hours.

## Build order
1. Schema (postcodes + planner_auto_apply) + postcodes.io client + haversine
   (pure, unit-tested).
2. Diary split: executor diary-arg + pipeline gate + /app/board + burger;
   single-diary freeze tests.
3. Planner core: get_day_state, heuristic, propose_plan (unit-first).
4. Board UI: plan diff, approve/discard, replan button, banner; apply txn.
5. planner_auto_apply setting + classification + Settings UI.
6. Live E2E on the seeded week (the Monday consolidation demo), then the
   demo story.
