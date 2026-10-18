# Scheduling module — bookings the agent can actually make (SCOPE — LOCKED)

> **Status (2026-09-27, confirmed by the clean-up pass):** BUILT (src/app/scheduling.py, the hold RPC, the diary), superseded in parts (slots, stays, offers). Proven live many times — see verified-behaviours, Booking and the diary.

**Status:** LOCKED 2026-06-10 — all four open questions answered with the recommended
options (day+AM/PM capacity windows; approve-&-send confirms the linked booking; tools
on the draft call; manual "Add booking" included in v1). Ready to build. Origin: module
roadmap (landing page "Next up", 2026-06-10), after the email module was live-verified
E2E on both providers.

## Goal
Give the email agent a **real diary to consult and book against**, so "can you collect
3 pallets Friday?" stops being a fact the agent must defer ("a colleague will confirm")
and becomes a booking: availability checked, a provisional hold placed, the reply
truthfully saying so, and a human confirming — or full-auto confirming — through the
same trust ladder the email module already established.

Scheduling is BLAM8's **second module**, so this spec also locks the platform pattern
every later module (Voice, Inventory…) will follow:
- A module = its own **tables + dashboard surface + tools for the shared agent**.
- Gated **per tenant by data** (`subscriptions` row), never by code paths.
- Works standalone (Diary + manual bookings, no email module required); interconnects
  when both are on (email ⇄ booking links, one approval drives both).

## Why this shape (and not a calendar engine)
A UK haulier/courier books **jobs against daily capacity** ("we run 4 jobs a day,
mornings or afternoons"), not 15-minute slots. A capacity diary is honest to that,
trivially configurable per tenant (config-not-code), and avoids building a slot/
timezone/recurrence engine nobody asked for. `tenant_data_sources` already captures
which calendar/TMS each tenant actually uses — that data decides what v1.1 syncs to.

## What the platform already gives us (confirmed in code)
- `subscriptions.module` (default `'email'`) — the module switchboard, dormant until now.
- `usage_events.module` — per-module metering has been wired since Phase 1.
- `tenant_config` + KB + escalation — channel/module-agnostic agent brain.
- `agent.draft_reply` already runs structured output (`output_config` json_schema) on
  Haiku 4.5; the API supports **tools in the same call** (verified via the claude-api
  reference: tools + `output_config.format` combine; `strict: true` validates tool
  params). The tool loop is the only new call shape.
- Approval ladder + honest-prompt switching (draft-only vs full-auto) from Phase 5b.

## Decisions
### D1 — Booking model: date + window against configured capacity
A booking occupies one **window** on one **date**. Windows are tenant-configured:
`'day'` (default, capacity N per day) or `'ampm'` (capacity N per half-day). Plus
working days, minimum notice (hours), booking horizon (days), blackout dates.
Rejected: hourly slots (false precision for the vertical; easy to add later as a third
window mode without schema change).

### D2 — Source of truth: our own `bookings` table
- **A (recommended): own table.** Full control for capacity logic, RLS, audit.
- B: two-way sync with Outlook/Google Calendar — needs new OAuth scopes
  (`Calendars.ReadWrite` / Google calendar scope) → re-consent for every tenant; defer.
  A one-way "push confirmed bookings to calendar" is the natural v1.1.
- C: TMS integration — the long-game moat; needs the connector roadmap, not v1.

### D3 — Agent integration: tools on the draft call (the platform pattern)
When (and only when) the tenant's scheduling module is active, `draft_reply` gains two
tools; the call becomes a short tool loop (cap ~5 iterations), final output still the
existing `{reply, deferred, missing}` schema:

- **`check_availability(date_from, date_to)`** → `[{date, window, remaining}]`,
  filtered by working days, notice, horizon, blackouts. Description is prescriptive:
  "call BEFORE offering or agreeing to any date."
- **`hold_booking(date, window, summary, details?)`** → creates a `bookings` row with
  `status='proposed'` and returns its id, or a clean `full`/`outside_horizon` error.
  "At most one hold per email, only after check_availability shows space."
  Since 2026-09-20 it also answers `already_booked` (with the standing reference) when
  the customer's live diary — a proposed or confirmed booking of theirs — already covers
  the requested span: a details-only follow-up on a thread with two confirmed stays had
  re-held both and re-quoted them (kennels rig). Offered rows never block (accepting one
  flips it), a hold naming a different dependant on the same dates is its own visit, and
  an appointment during a stay is not the stay. The drafter's reservation guard makes the
  matching exception: a reply that names only the LISTED live references is not sent back
  for a forced tool call (`agent._claim_is_backed`).

**Capacity is enforced in code, not in the model:** the hold goes through a DB function
(`hold_booking_if_capacity`, same RPC pattern as `match_knowledge_chunks`) that checks
count-vs-capacity and inserts atomically — two racing emails cannot overbook. The agent
can only be *too cautious*, never wrong.

Rejected: two-pass extract-then-draft (deterministic but kills the multi-turn
negotiation — "Friday's full, Monday AM?" — and doubles model cost on booking mail).
The tools are also exactly what the Voice module will call later, verbatim.

**Prompt layer:** a scheduling section is appended to the system prompt only when the
tools are present: diary semantics, "you may truthfully say a provisional hold is
placed — the hold tool really placed it", never promise dates outside tool results,
mode-aware wording (draft-only: "a human confirms the booking"; full-auto: "the booking
is confirmed when your reply sends"). Today's date is injected here (daily cache
invalidation — acceptable; never per-request timestamps).

### D4 — Trust ladder: proposed → confirmed, one approval drives both
- The agent only ever creates **`proposed`** holds. Holds count against capacity
  (otherwise racing emails double-book) and expire via the nightly purge job
  (proposed + older than 7 days → cancelled).
- **Draft-only:** "Approve & send" on the linked draft confirms the booking in the same
  action; "Skip" releases the hold. Re-draft cancels the old hold so the agent can
  place a fresh one. The Diary also has manual confirm/decline for direct control.
- **Full-auto:** a successful auto-send confirms the hold. All existing holds-backs
  stay: escalation keywords, self-mail guard, duplicate-delivery dedupe.

### D5 — Fields: generic core + `details jsonb`
No logistics-specific columns. `summary` (one line) + `details` (industry-shaped JSON:
postcodes, pallets, weight…) keeps the module vertical-neutral; `tenants.industry`
steers what the prompt asks the agent to capture into `details`. Config, not code.

## Schema (one migration, applied via psycopg2 — see memory `apply-supabase-migrations-directly`)
```sql
create table if not exists bookings (
  id           uuid primary key default gen_random_uuid(),
  tenant_id    uuid not null references tenants(id) on delete cascade,
  contact      text,                          -- customer email (cross-module identity seed)
  summary      text not null,                 -- "3 pallets, M17 -> LS9"
  details      jsonb,                         -- industry-shaped extras
  booking_date date not null,
  window       text not null default 'day',   -- 'day' | 'am' | 'pm'
  status       text not null default 'proposed', -- proposed|confirmed|declined|cancelled|completed
  source       text not null default 'email', -- 'email' | 'manual'   (voice later)
  email_log_id uuid,                          -- the inbound that created it (interconnect)
  thread_id    text,                          -- provider thread (deep-link to conversation)
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);
create index if not exists bookings_tenant_date_idx on bookings (tenant_id, booking_date);
-- RLS: tenant isolation policy identical to email_log.

create table if not exists scheduling_config (
  tenant_id           uuid primary key references tenants(id) on delete cascade,
  capacity_per_window int  not null default 4,
  windows             text not null default 'day',            -- 'day' | 'ampm'
  working_days        text not null default 'mon,tue,wed,thu,fri',
  min_notice_hours    int  not null default 24,
  horizon_days        int  not null default 28,
  blackout_dates      date[] not null default '{}',
  updated_at          timestamptz not null default now()
);
-- + hold_booking_if_capacity(p_tenant_id, p_date, p_window, ...) returns bookings
--   (atomic capacity check + insert; RPC like match_knowledge_chunks)
```

## Wiring (smallest surface)
- **`src/app/scheduling.py`** (new): availability math, tool definitions, tool executor,
  config CRUD. The only file that knows what a booking is.
- **`src/app/agent.py`:** `draft_reply(..., tools=None, tool_executor=None)` — manual tool
  loop when tools given; sums usage across loop calls into the one DraftResult.
  `build_system_prompt` gains the scheduling section when tools are active.
- **`src/app/pipeline.py`:** look up module activation (`db.module_active`), pass tools;
  after `insert_email_log`, stamp `bookings.email_log_id`; confirm/release hold on
  auto-send success/failure.
- **`src/app/dashboard.py` + templates:** Diary view at `/app/diary` (group by date,
  capacity header, confirm/decline, link to conversation, manual "Add booking" form),
  Settings → Scheduling card, burger item — all gated on the subscription row. The
  `/app` module hub (built 2026-06-10) already routes its Scheduling card to
  `/app/diary`: activating the subscriptions row lights the card with no hub changes.
- **`src/app/db.py`:** `module_active`, bookings CRUD, RPC call.
- **`src/app/jobs/purge_email_log.py`:** expire stale proposed holds (7 days).
- **Admin console:** operator toggles the `subscriptions` row per tenant.

## Edge cases
- Race for the last slot → atomic RPC; loser gets `full`, agent offers alternatives. ✓
- Customer asks to move/cancel by email → **v1 limitation:** agent acknowledges and
  defers to a human; only the Diary can modify existing bookings. (Keeps the tool
  surface minimal; `amend_booking` is a clean later addition.)
- Relative dates ("Friday") → resolved by the model against the injected current date.
- Skipped/never-reviewed drafts → hold released on skip; expiry sweep as backstop.
- Email module off, scheduling on → Diary/manual only; no tools injected anywhere.
- Scheduling off (default) → zero behavioural change for existing tenants.

## Verification
- **Unit:** availability math (working days, notice, horizon, blackouts, ampm vs day);
  hold-when-full returns error; tool loop terminates + usage summed; prompt section
  present only with tools; approve-confirms / skip-releases.
- **Live (pilot):** booking email → draft cites a real hold visible in the Diary →
  approve → confirmed; second email for the same window when full → agent offers the
  next free window; full-auto round confirms without a click.

## Out of scope (v1)
Calendar push (v1.1), public booking page, agent-driven amend/cancel, multi-resource
(vehicles/drivers), recurring bookings, route-aware capacity, SMS confirmations,
deposits/payments, cross-channel contact identity (Voice-era).

## Decisions confirmed (2026-06-10)
1. **D1 — day + AM/PM windows** with per-window capacity. No time slots in v1.
2. **D4 — one approval drives both:** "Approve & send" confirms the linked booking;
   "Skip" releases the hold. Diary keeps manual confirm/decline.
3. **D3 — tools on the draft call** (`check_availability` + `hold_booking` in a tool
   loop). This is the locked platform pattern for all future modules.
4. **Standalone v1 is real:** the Diary ships with a manual "Add booking" form.

## Build order
1. Schema migration (bookings, scheduling_config, `hold_booking_if_capacity` RPC,
   RLS) + `db.module_active` + the pilot tenant's `subscriptions` row.
2. `src/app/scheduling.py` — availability math + tool defs + executor (unit-test first).
3. `agent.draft_reply` tool loop + scheduling prompt section (mode-aware).
4. Pipeline wiring: tools in, booking↔email_log links, approve/skip/auto-send effects.
5. Dashboard: Diary view + manual add + Settings card + burger item (gated).
6. Hold-expiry sweep in the purge job; admin toggle.
7. Live E2E on the pilot: draft-cites-hold → approve-confirms → full-window negotiation.
