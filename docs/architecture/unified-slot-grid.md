# Unified slot-grid — one calendar primitive for every industry

> **Status (2026-09-27, confirmed by the clean-up pass):** Phases 1, 2, 2b LIVE since 2026-06-30 (the office diary reads and places on the grid); multi-day stays and per-service durations are on the grid too. Phase 3 (FSM) and Phase 4 (TMS) NOT built; Phase 5 partly (offered holds live; the capacity function is still window-based).

**Status:** Phase 1 (pure engine) implemented; Phases 2–5 staged.
**Supersedes** the "modular per-module calendars" half of `modular-calendars-offer-engine.md`
(the offer-engine half still stands and sits on top of this).

## The problem we keep hitting

We have effectively grown **three calendars**:

| Module | Capacity unit | How a booking is stored today |
|---|---|---|
| Scheduling (office) | a *window/slot* (N parallel per window) | `window` = `'day' | 'am' | 'pm' | 'HH:MM'`, `capacity_per_window` |
| Field Service (FSM) | an *engineer's minutes/day* | `window='day'`, `engineer_id`, `duration_minutes`, `required_skill` |
| TMS (logistics) | a *vehicle's pallet-space/weight* | `window='day'`, `vehicle_id`, `spaces`, `weight_kg` |

They already share **one `bookings` table** — but the *time model* is bespoke per
module (`window` is a polymorphic text field), and each new industry tempts another
shape. Every time an industry wants a different booking granularity we reach for the
schema. That is the recurring pain.

## The idea (Kris): one flat 15-minute grid, the rest is config

- **The atomic unit is a 15-minute block.** It is the smallest slot any industry books.
- A day is a **flat grid of 96 blocks** (24h × 4). We never store 96 empty rows — a
  booking is just a `(start_block, length_blocks)` span on the grid, and "what's free"
  is *computed* from the gaps.
- **A booking of any length = N consecutive blocks.** 30 min = 2, 1 h = 4, 2 h = 8,
  8 h = 32, a full day = 96.
- **Slot size is config, not schema.** "We work in 2-hour slots" just means the grid is
  presented/booked in 8-block buckets. Change it any time; the table never changes.

The DB stops caring what industry a tenant is. Every industry difference becomes a
**setting on the same primitive.**

## The missing axis: *resource*

A calendar has **two** axes, and the grid above is only the first:

1. **Time** — the 15-min block grid (above).
2. **Resource** — *who/what* is booked. This is why the three calendars diverged:
   - Office → **1 resource** (the business; or one row per chair/room/staff if they want N).
   - FSM → **N resources** (engineers), each with its own grid **+ a skill tag**.
   - TMS → **N resources** (vehicles), each with its own grid **+ non-time capacity**
     (pallet space, weight).

So the unified model is: **a 15-min grid *per resource*, plus a small per-tenant config**
for (a) block size, (b) what a resource is and how many, (c) any non-time capacity.
Office, trades and logistics then stop being three calendars — they are **the same grid
with different config**.

### Honest caveat — TMS

TMS's *binding* constraint is pallet-space/weight, not time. The grid is still the
scheduling layer (which day / which vehicle), but the capacity check is "does this
vehicle have N spaces left that day", which rides as a **resource capacity attribute**
on top of the grid, not as block-occupancy. Office and FSM collapse together almost
perfectly; TMS reuses the grid for *placement* and keeps its space/weight check.

## Data model

### Config (per tenant, per *diary* — see "many diaries" below)

```
block_minutes   int   -- the tenant's slot size; MUST be a multiple of 15 (15..1440)
open_time       time  -- start of the bookable day (00:00 for a 24h grid)
close_time      time  -- end of the bookable day (24:00 for a 24h grid)
working_days    text  -- 'mon,tue,...'
min_notice_hours int
horizon_days    int
blackout_dates  date[]
```

`block_minutes` replaces the role of today's `slot_minutes`, now constrained to 15-min
multiples. `windows='day'|'ampm'|'slots'` becomes a *presentation* of the grid:
`day` = one block of `close-open`; `ampm` = two; `slots` = `block_minutes` buckets.

### Resource (per tenant)

```
resources(id, tenant_id, kind, name, skills text[], capacity_spaces numeric, max_payload_kg numeric, active)
```

- `kind` ∈ `office | engineer | vehicle`. `office` tenants get one implicit resource
  (no rows needed) or N named ones.
- `skills` used by FSM; `capacity_spaces`/`max_payload_kg` used by TMS; null elsewhere.
- This generalises today's `engineers` and `vehicles` tables (they become `resources`
  rows, or `resources` is a view over them during migration — see Migration).

### Booking (the existing `bookings` table, two columns added)

```
booking_date  date
start_block   int   -- 0..95 (15-min index from midnight); NULL for legacy rows
length_blocks int   -- 1..96;  NULL for legacy rows
resource_id   uuid  -- engineer_id/vehicle_id unify into this; NULL = office/unassigned
```

`window`, `duration_minutes`, `spaces`, `weight_kg`, `engineer_id`, `vehicle_id` are
**kept** (backward-compat + TMS capacity). `start_block`/`length_blocks` are derived for
new rows and back-filled for old ones from `window`+`slot_minutes`/`duration_minutes`.

## How each module maps onto the grid

| | resource(s) | block size | a booking is | capacity check |
|---|---|---|---|---|
| Office | the business (1) or N rooms/staff | `block_minutes` (e.g. 60) | `start_block + N` | block free on ≥1 resource |
| FSM | engineers | by job-type duration | `start_block` (or whole-day) `+ job blocks` | a skill-matched engineer has the blocks free |
| TMS | vehicles | whole-day | whole-day span | vehicle has spaces/weight left that day |

## Migration (additive, backward-compatible)

1. Add `resources` table; add `start_block`, `length_blocks`, `resource_id` to `bookings`;
   add `block_minutes` to config (default 15; existing `slot_minutes` copied, rounded to
   the nearest 15).
2. Back-fill `start_block`/`length_blocks` from `window`+duration via
   `slotgrid.window_to_blocks` (Phase 1 ships this helper). Legacy rows that can't be
   resolved keep NULL and render exactly as today.
3. New writes set both the legacy columns **and** the new ones (dual-write) until every
   read path is migrated; then the legacy `window` text becomes display-only.
4. `engineers`/`vehicles` stay as the source of truth for skills/space; `resources` is a
   read view over them initially, a table later.

No existing booking is rewritten destructively; nothing is dropped until every read path
uses the grid.

## Many diaries per tenant (ties in the modular-calendars work)

Each *module* a tenant runs is one diary (its own resources + block config): office
diary, engineer diary, vehicle diary. The grid + resource model is the shared primitive
under all of them; "onboard the calendar **layout**, not just the requirements" (from
`modular-calendars-offer-engine.md`) becomes: pick block size, define resources, set
hours — all config.

## Phasing

- **Phase 1 — pure engine (this change).** `src/app/slotgrid.py`: block math, config→slots,
  per-resource availability, overlap, legacy `window`→blocks migration helper. Fully
  unit-tested, **wired into nothing** — zero risk to live booking.
- **Phase 2 — office onto the grid.** Reimplement office `availability()`/`place_hold`
  on `slotgrid` behind the existing tools; dual-write `start_block`/`length_blocks`;
  config UI gains a 15-min-multiple block-size picker. Prove on an office-only tenant.
- **Phase 3 — FSM onto the grid.** Engineers become resources; `book_engineer` places on
  the engineer's block grid (timed jobs, not just whole-day).
- **Phase 4 — TMS onto the grid.** Vehicles become resources; keep space/weight capacity,
  use the grid for day placement.
- **Phase 5 — onboarding the layout + offer engine.** The `modular-calendars-offer-engine`
  work sits on top: offer-3 holds, TTLs, layout onboarding.

Each phase is independently shippable and reviewed before the next.
