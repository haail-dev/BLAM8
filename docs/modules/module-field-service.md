# Field Service (Trades) module — the 2nd vertical, end to end (DEMO-GRADE)

> **Status (2026-09-27, confirmed by the clean-up pass):** BUILT, Phases 1–4 (20d31eb…4e57fab; agents/field_service.py, /app/fsm, service reminders). Live e2e by script June 2026; not re-proven since.

**Status:** BUILDING 2026-06-30. Origin: Kris's call to make Trades & field service
the **second advertisable vertical** alongside Logistics/TMS, as a *full* industry
module (not just the agnostic core). The trades vertical already runs on the base
agent (email + scheduling + intake + the `trades` lingo seed in
`src/app/industry_lingo.py`); this module gives it the **depth TMS gives logistics** so
"Core + Field Service" becomes a real paid tier and a recordable demo.

## What software this replaces
A trades/field-service firm (plumbing, electrical, HVAC, locksmith, appliance repair)
runs on **FSM software** — Jobber, Tradify, Commusoft, Simpro, ServiceM8, Joblogic.
The slice BLAM8's email + scheduling agent owns: **size the job, triage emergencies,
match the right engineer, book the right-length slot, quote it, invoice it, and chase
the recurring service** — every screen live off the same `bookings` diary the email
agent already writes into. This is the trades analogue of TMS's transport board.

## The architectural move (mirrors TMS exactly)
1. **Packaging — Core vs vertical add-on.** Email + Scheduling stay the **Core**
   every vertical buys. **Engineer capacity is the spine of the Field Service
   module** (module key `fsm`), exactly as Fleet/Capacity is the spine of `tms`.
   Flipping one `subscriptions` row (`module='fsm'`) is the whole rollout. The
   "Core + FSM" tier reuses the existing `professional` price slot via
   `plans.INDUSTRY_MODULE_META` (industry=`trades` → module `fsm`).
2. **The hand-off pattern is reused verbatim** (`docs/modules/module-agent-handoff.md`). The
   base email agent stays the single voice; when `fsm` is active it gains ONE
   hand-off tool — `ask_field_service` — whose executor invokes a new specialist
   agent (`src/app/agents/field_service.py`). The specialist owns all trades reasoning
   (job sizing, skill match, emergency triage, quoting); the base agent never sees
   engineer internals and composes the reply. Structured result, not prose.
3. **Engineer-day capacity = the fleet model, re-skinned.** Engineers are to FSM what
   vehicles are to TMS; a job's **duration in minutes** is to an engineer-day what a
   load's **pallet spaces** are to a vehicle-day. A skill requirement gates which
   engineer can take a job, the way a branch/vehicle-type gates a load. The proven
   atomic best-fit hold RPC is duplicated and re-skinned, NOT re-invented.

Everything else is the established platform pattern: a surface = a pure view-model
(`config + rows in → render-ready dict out`, unit-tested with no DB) + thin fetch
wrapper + thin `db.py` functions + a Jinja template, gated by the module row.

## Decisions

### D1 — Capacity model: engineer-day working minutes + skill gating
New tables (tenant-isolated, RLS, like every table), mirroring the fleet section:
- `engineers` (← `vehicles`): `name`, `branch_id`, `skills text[]`
  (e.g. `{gas,plumbing,electrical}`), `daily_minutes int` (default 480 = 8h), `active`.
- `job_types` (← `pallet_types`): `name` (e.g. "Boiler service"), `default_minutes`,
  `required_skill text` (nullable), `default_price numeric` (nullable),
  `emergency bool default false` (a type that is always a priority callout).
- `bookings +=` `engineer_id`, `duration_minutes`, `job_type`, `required_skill`,
  `priority text default 'standard'` (`'standard'|'emergency'`). `quoted_amount` and
  the postcode columns already exist (the service address reuses
  `delivery_postcode` as the site postcode — no new column).
- `hold_engineer_booking` RPC (← `hold_fleet_booking`): given `(date, duration,
  required_skill, preferred_engineer?, branch?)` best-fit an **active** engineer who
  **has the required skill** and has **enough free minutes that day** (sum of held
  jobs' `duration_minutes` + this one ≤ `daily_minutes`); place the hold atomically.
  Best-fit = the engineer with the least spare capacity that still fits (tightest
  pack), same philosophy as the van best-fit. Returns `(engineer_id, free_minutes)`.

### D2 — The FSM specialist agent (`src/app/agents/field_service.py`)
Same shape as `src/app/agents/tms.py`:
- `ASK_FIELD_SERVICE_TOOL` (`ask_field_service`) — the ONE hand-off tool on the base
  agent, injected only when `fsm` is active. "Ask the field-service desk about a
  job/callout/site visit…". Pass the customer's words.
- Own system prompt — the trades tool-discipline rules:
  - Size the job from `job_types` (a "boiler service" is ~60 min); if the request
    doesn't name a recognisable job, return `needs_info` with the one question.
  - **Emergency triage**: no heating / no hot water / leak / gas smell / no power /
    lockout → `priority='emergency'`, offer the earliest capacity; a `job_types` row
    flagged `emergency` is always priority. Honest: never promise "today" unless the
    tool shows capacity today.
  - **Skill match**: a job's `required_skill` must be matched by an engineer who has
    it — pass it to the tools; never book a skill no active engineer holds → `no_fit`.
  - Always `check_engineer_availability` before promising a date; never invent one.
    When the asked date doesn't fit, put the nearest fitting dates in `alternatives`.
  - Place ONE hold via `hold_job` only when the customer asks to book and it fits; a
    held result is REAL. Carry the `booking_ref` back.
  - **Quote** from the rate card when the customer asks the cost: callout + labour
    (duration × hourly) + the job-type price; state it as an estimate, list
    assumptions. Never name the individual engineer to the customer (internal, like
    vehicles in TMS).
- Structured result `FSM_SCHEMA`: `{status (held|offered|no_fit|needs_info|error),
  date, job_type, duration_minutes, required_skill, priority, booking_ref,
  quote {callout, labour, parts, total}, assumed[], alternatives[], ask, message}`.
- `make_handoff(tenant_id, contact, thread_id, mailbox_id)` — binds identity
  out-of-band, one hand-off per email, holds alias into `state['bookings']`, tokens
  into `state['usage']`. Identical contract to TMS (D5/D6/D7 of the hand-off spec).

### D3 — Quotes/pricing = a per-tenant trades rate card
`fsm_rate_cards` (one default per tenant): `callout_charge`, `hourly_rate`,
`min_charge`, `vat_pct` (+ per-`job_type` price overrides on the `job_types` row).
A quote = `callout + ceil(duration/60)*hourly + job_type.default_price`, floored at
`min_charge`. Editable in the UI; read-only fallback when unset. Reuses the
`src/app/pricing.py` rate-card pattern. The quote stores onto `bookings.quoted_amount`
so invoicing reads it back (same cross-module seam as TMS).

### D4 — Invoicing generalises to trades jobs
`src/app/invoicing.py` currently assumes transport (`spaces`/miles). Generalise the line
amount: a completed booking with `duration_minutes`/`job_type` invoices from its
stored `quoted_amount`, else recomputes from the trades rate card (labour from
duration + job-type price). Grouping by `bookings.contact` is unchanged. Same
`invoices`/`invoice_lines` tables, VAT, refs.

### D5 — Recurring service reminders (compliance) = reverse hand-off
Trades' analogue of TMS day-before reminders, but **forward-dated recurring**: a
`service_due_date` on a completed job (e.g. annual boiler service / Gas Safe /
landlord cert) drives a proactive "time to rebook your service" email. Reuses the
in-process daily job (`src/app/scheduler.py:run_daily`) + the reverse hand-off
(`field_service.outbound_reminder` frames the facts → base composes in the tenant
voice → queued as `kind='service_reminder'` pending draft for approval, or sent for
full-auto). Same `email_log.kind` routing + dedupe as the day-before reminders.

### D6 — FSM cockpit board
`/app/fsm` (← the fleet board): today/this-week engineer board — each engineer's
booked minutes vs `daily_minutes` as a load bar, jobs as blocks, emergencies
flagged. Reuses `fleet_board(dual=…)` capacity-isolation logic so a dual tenant's
office rows never appear on the engineer board. Nav tab strip `_fsm_nav.html`
(Cockpit · Board · Engineers · Job types · Pricing · Invoices).

### D7 — Plans / onboarding / landing flip (makes it sellable + demoable)
- `plans.INDUSTRY_MODULE_META["trades"] = {key:"fsm", label:"FSM",
  name:"Field Service", features:[…]}` → "Core + FSM" becomes a buyable
  `professional` tier for trades tenants (the in-development placeholder goes away).
- `onboarding.INDUSTRIES`: trades `live: True`; `DATA_SOURCES["trades"]` =
  FSM vendors (Jobber, Tradify, Commusoft, Simpro, ServiceM8).
- Landing/positioning: the trades vertical gets its own proof line.
- A seeded **Thornbeck Plumbing & Heating** demo tenant (the persona from
  yesterday's recording, `docs`/memory `project_blam8_demo_recording`) on the `fsm`
  module: a couple of gas-safe + general engineers, job types (service/repair/
  install/emergency callout), a rate card — so the demo books a real job.

## Data model (new)
- `engineers`, `job_types`, `fsm_rate_cards` — tenant-isolated (RLS).
- `bookings +=` `engineer_id`, `duration_minutes`, `job_type`, `required_skill`,
  `priority`, `service_due_date`.
- Subscriptions: new `module='fsm'` row activates the vertical; activating it seeds
  trades defaults (a Main branch reuse + starter job types + a default rate card).
- `invoices`/`invoice_lines` reused unchanged.

## Phased rollout (build order; the AD needs Phase 1 + the quote slice of Phase 2)
1. **Phase 1 — capacity + hand-off (the spine).** `engineers`/`job_types` tables +
   bookings cols + `hold_engineer_booking` RPC + `src/app/agents/field_service.py` +
   `ask_field_service` gated on `fsm` + base composes. **Acceptance: a trades
   tenant's inbound job email → the agent sizes it, triages emergency, skill-matches,
   checks engineer capacity, holds, and the base composes the reply** — behaviourally
   the trades twin of the TMS hold. Verified by `tests/test_fsm_agent.py` + a live
   E2E (`scripts_live_e2e_fsm_handoff.py`).
2. **Phase 2 — quotes + cockpit + invoicing.** Trades rate card + quote in the reply,
   `/app/fsm` engineer board, invoicing generalised to trades jobs.
3. **Phase 3 — recurring service reminders** (reverse hand-off, D5).
4. **Phase 4 — sellable + demoable**: plans/onboarding/landing flip (D7) + the
   Thornbeck demo tenant seed. Then record the trades video alongside the logistics one.

## Out of scope (discovery follow-ons)
Per-engineer mobile app / tokenised job links; live engineer GPS; Stripe-paid
invoices; parts/stock inventory; multi-engineer jobs; customer self-service booking
portal. Left for what trades firms tell us in discovery — same posture as TMS.

## Tests
Each pure piece gets a no-DB unit test (`tests/test_fsm_agent.py`,
`tests/test_fsm_pricing.py`, extend `tests/test_invoicing.py`). The RPC gets a
rolled-back smoke test in its migration script (à la `scripts_apply_fleet_migration`).
Behaviour parity with the established hold lifecycle is the bar, as it was for TMS.
