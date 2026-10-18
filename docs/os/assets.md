# BLAM8 Assets OS — Design & Build Spec

> **Status (2026-09-27, confirmed by the clean-up pass):** BUILT, phases A–D (app/assets*.py; tests/test_assets.py), deployed. Live proof: none (runs since 2026-09-05 test the core only).

**Status:** Not built. Vocabulary + intake seeds exist for Lettings + Equipment/Plant Hire; the stay/deposit spine (multi-day capacity, per-night/week/day rate, hold deposit) is already SHIPPED as the scheduling "stays" model. This spec is the delta on top of that spine.
**Author:** Julius Deane, w/ Claude synthesis
**Last updated:** 2026-07-05
**Reads:** `docs/BLAM8_operating_systems.md` §2/§4.5/§5/§6, `docs/architecture/CRM_design.md` (format/depth template + platform patterns).

> **One-line read of this spec.** Assets OS is *not* a new capacity engine. BLAM8 already books multi-day, deposit-bearing, priced STAYS atomically (`src/app/scheduling.py` stay path + `db.insert_stay_booking` + `slotgrid` concurrent-occupancy). A holiday cottage, a digger, a marquee and a hire car are all the same primitive as a kennel stay: *one unit, occupied from arrival to return, with a deposit and a condition*. The genuinely new build is three things — a **`unit` registry** (the specific asset, its rate card, its next service date, its running condition), **deposit lifecycle** (take → hold → refund/deduct, on top of the existing Stripe-hosted-Checkout rail), and **condition photo-diffs** (before/after LLM comparison that produces damage-claim evidence). Everything else is reuse.

---

## §0 Positioning

**Shape.** Time-limited access to property or equipment. The customer takes possession of a physical thing for a window, then hands it back. Revenue is the hire fee; risk is damage, loss and double-booking; the recurring pain is deposits and disputes.

**Primitive.** `unit` — a *specific* hireable asset (Cottage "Rose", Digger JCB-3, Marquee 6×12, Reg AB12 CDE), carrying its availability window (a booking), its condition (0–5 + damage history) and its rate card. The demand side (who wants it, for when) is already the booking; the supply side (the specific thing, its state over time) is the new part.

**Daily-rhythm surface.** Three stacked queues on `/app/assets`:
1. **Today's handovers** — units going out (collection/check-in) and coming back (return/check-out) today, each with a one-tap "capture condition" action.
2. **Upcoming bookings** — the next 14 days of holds/confirmed hires, deposit status per row.
3. **Damage & maintenance queue** — open damage cases (photo-diff flagged), units below a condition floor, and units with `next_service_due` inside the lead window.

**Workflows.** enquiry → hold → confirm (deposit taken) → handover (out-condition captured) → in-use → return (in-condition captured) → condition check (photo-diff) → invoice reconciliation (deposit refund or deduction).

**Target industries (~150K UK SMBs).** Lettings (holiday + short-term), plant hire, equipment hire, car hire, boat hire, tool hire, party-equipment hire, camping hire. Rough UK counts: holiday-let owners/managers ~127K registered FHLs, plant hire ~2.5K firms (fragmented long tail), tool hire ~1.5K branches, self-drive car hire ~500 independents, the balance in party/marquee/camping micro-operators. The wedge below is one slice; the OS covers all of them by seed.

**Build state.** 🌱 Vocabulary seeds live (`src/app/industry_lingo.py` LINGO/CRM_SEEDS for `lettings` + `hire`), intake profiles live (`src/app/settings_guide.py` holiday-let/plant profiles with stay pricing + deposit), and the stay capacity spine is shipped. `operating_systems.py` already maps `lettings`/`hire` → `assets` and lists Assets OS as `live: False`. Module not built.

**Killer feature.** (1) Atomic capacity — *reused wholesale* from the scheduling promise (§12). (2) Condition photo-diffs — the novel LLM before/after comparison producing damage-claim evidence (§12).

**Wedge.** Holiday-cottage owners juggling Airbnb + direct bookings + deposits + damage claims + a caretaking/cleaning team. Currently a calendar, a spreadsheet, a Stripe dashboard and a folder of photos. BLAM8 makes it one flow: the enquiry email becomes a held unit, the deposit is a link in the confirmation draft, handover/return photos become a diff, and the deposit refund/deduction is one click with the evidence attached.

---

## §1 The primitive — `unit` vs the existing scheduling/booking

### What already exists (reuse, do not rebuild)
A `bookings` row IS the availability window. The scheduling engine already gives us, for free:

- **Multi-day occupancy.** `insert_stay_booking(... start_block, length_blocks ...)` places a hold spanning arrival→return on the unified 15-minute grid (`src/app/slotgrid.py`); `scheduling.diary_days`/`_booking_on_day` show a stay on *every* day it covers (`stay_span`).
- **Atomic capacity.** Concurrent-occupancy capacity (`slotgrid.max_concurrent`, `service_availability`) means N identical units = capacity N; the same promise the whole product rests on. Per-resource-type capacity already exists (`resource_type` column, `_pool_spans(..., resource=...)`).
- **Deposit + rate.** `diary_config.clean_stay_pricing` → `{rate, rate_unit(night|week|day), deposit, min_nights}`, `quote_stay(...)` computes the hire total, `deposit_amount` is already a column on `bookings` (written by `insert_stay_booking`).
- **Arrival/return semantics.** The `intake.STAY_FIELD` toggle turns a tenant's bookings into stays; `is_stay(cfg.intake_fields)` is the flag.

So the *booking* half of a unit is done. What Assets OS adds is the **supply-side identity and state** the scheduling engine has no concept of: *which specific asset*, *its condition over time*, *its rate card independent of the diary*, *its service schedule*.

### The `unit` — new, and justified
A booking is a demand event; it is created and destroyed per hire. A unit is a durable supply object that outlives every booking against it — it accrues damage history, needs servicing, has a per-unit rate and a per-unit condition trajectory. You cannot hang that on `bookings` (wrong lifetime) or on `custom_data` (needs querying, indexing, its own child rows). This is the fleet module's own `vehicles` table pattern exactly: vehicles are the durable supply pool; `bookings` are demand placed on them via `vehicle_id`. **`asset_units` is the `vehicles` of Assets OS**, and `bookings.unit_id` is the `vehicles`/`bookings.vehicle_id` link, reused verbatim.

| Concept | Where it lives | Reuse or new |
|---|---|---|
| "Someone wants Rose Cottage 5–12 Aug" | `bookings` row (stay span) | **REUSE** — `insert_stay_booking` |
| "Rose Cottage is a specific 2-bed unit, £120/night, £200 deposit, condition 5" | `asset_units` row | **NEW** (mirrors `vehicles`) |
| "This booking is against Rose Cottage" | `bookings.unit_id` FK | **NEW column** (mirrors `bookings.vehicle_id`) |
| "3 identical marquees, don't overbook" | concurrent-occupancy capacity, `resource_type` per pool | **REUSE** — `service_availability`/`max_concurrent` |
| "Rose was handed over at condition 5, returned at 3, here's the diff" | `condition_reports` + `damage_cases` | **NEW** |
| "Deposit of £200 taken, £50 deducted for the burn mark" | `unit_deposits` (over Stripe rail) | **NEW** wrapper over reused Stripe |

**Capacity model choice (per unit vs per pool).** Two tenant shapes, one engine:
- *Named-unit tenants* (holiday lets, car hire, boats): each unit is unique. A booking pins `unit_id`; availability of unit U = "no active booking overlaps this span on U". This is `service_availability` with `capacity=1` scoped to the unit's own bookings (filter by `unit_id`), or equivalently `resource_type = unit_id`.
- *Fungible-pool tenants* (tool hire: 12 identical drills): the pool is the capacity. A booking consumes 1 of N; `unit_id` is assigned best-fit at handover, exactly like `best_fit`/`hold_fleet_booking` picks a vehicle. Availability = concurrent occupancy < N.

Both fall out of the existing math by choosing the scope. No new capacity code — only a new *scoping key* (`unit_id` or a unit `resource_type`).

---

## §2 Daily-rhythm surface — `/app/assets`

A single view-model `assets.board(tenant_id, now)` (pure, à la `pod.board` / `scheduling.diary`), fetching bookings + units + open damage cases + condition reports, returning three lanes. Rendered server-side Jinja2, house design system.

```
Today · Tuesday 08 July
──────────────────────────────────────────────
HANDOVERS TODAY (4)
  OUT  · Rose Cottage        · Mr Adekunle · 08 Jul → 12 Jul · deposit £200 ✓ held
         [Capture handover condition →]
  OUT  · Digger JCB-3        · BuildCo     · 08 Jul → 15 Jul · deposit £250 ⏳ unpaid
         [Send deposit link →] [Capture condition →]
  BACK · Marquee 6×12        · Village Hall· returned today   · [Capture return condition →]
  BACK · Reg AB12 CDE (car)  · S. Okoro    · returned today   · out:5 → [check on return]

UPCOMING (next 14 days · 9 bookings)
  10 Jul  Kayak x2 (pool)    · hold        · deposit pending
  11 Jul  Rose Cottage       · confirmed   · deposit ✓
  ...

DAMAGE & MAINTENANCE (3)
  ⚠ Digger JCB-3   · condition 5→2 · hydraulic leak (photo-diff, 0.82) · deposit hold £250
  ⚠ Marquee 6×12   · condition 4   · below floor (3.5) → service?
  🔧 Reg AB12 CDE   · next service due 14 Jul (MOT) · in 6 days
```

Reuses the "Today's diary strip" idea (`dashboard._today_strip`) but keyed on handover/return transitions rather than office windows. The maintenance lane reuses the service-reminder cadence logic (`REMINDER_LEAD_DAYS`).

---

## §3 Workflow state machine

The hire lifecycle. Each transition names its **drafter touchpoint** (the reply/outbound BLAM8 composes in the tenant's voice via `agent.draft_reply` inbound or `agent.compose_outbound` proactive). Booking `status` reuses the existing set (`proposed`/`confirmed`/`completed`/`cancelled`); the *hire-specific* substate rides on a new `bookings.hire_state` column so we never fork the capacity-consuming `ACTIVE_STATUSES` contract.

| # | State | Trigger | booking.status / hire_state | Drafter touchpoint |
|---|---|---|---|---|
| 1 | **enquiry** | inbound email asking to hire | none yet — CRM contact/deal autofills (reuse) | `draft_reply`: answer, quote from unit rate card, offer dates that `check_availability` returns |
| 2 | **hold** | customer picks dates | `proposed` / `held` — `place_hold`/`insert_stay_booking` scoped to the unit | `draft_reply`: "held Rose 5–12 Aug, £840 + £200 deposit to confirm" + **deposit link** |
| 3 | **confirm (deposit)** | deposit paid (Stripe webhook) OR human confirms | `confirmed` / `reserved`; `unit_deposits.status='held'` | `compose_outbound`: booking confirmation, handover instructions, arrival details |
| 4 | **handover** | unit goes out; out-condition captured | `confirmed` / `out` | `compose_outbound` (optional): "you're all set, here's the checkout condition on file" |
| 5 | **in-use** | during the hire window | `confirmed` / `out` | none (or a mid-hire check-in for long hires, reuse gone-quiet nudge) |
| 6 | **return** | unit comes back; in-condition captured | `confirmed` / `returned` | none — internal transition |
| 7 | **condition check** | photo-diff runs on return | `returned`; may open `damage_cases` | `compose_outbound`: clean return → "all good, refunding your deposit"; damage → "we found X, here's the photo evidence, proposing £Y deduction" |
| 8 | **invoice reconciliation** | deposit refunded/deducted; hire invoiced | `completed`; `unit_deposits.status='refunded'\|'deducted'`; invoice via shared engine | `compose_outbound`: final receipt + deposit outcome; overdue balance chaser reuses invoicing chasers |

Guard rails carried from scheduling: a hold that would overbook a unit is rejected atomically (`full`); a hire whose deposit is unpaid past a cutoff can auto-release the hold (reuse the reminder sweep to cancel stale unpaid holds).

---

## §4 Schema — additive tables

House conventions: `tenant_id uuid not null references tenants(id)`, RLS `tenant_id IN (SELECT auth_tenant_ids())` on every table, additive-only, idempotent psycopg2 migration (`scripts/db/migrations/apply_assets_migration.py`, pattern of `scripts/db/migrations/apply_crm_phase4b_migration.py`). All CRM/booking FKs point at the *actual* table names in prod (`crm_contacts`, `crm_deals`, `bookings`).

```sql
-- The durable supply object. The `vehicles` of Assets OS.
CREATE TABLE IF NOT EXISTS asset_units (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  asset_ref         text,                       -- tenant's own asset ID / reg / plate
  name              text NOT NULL,              -- "Rose Cottage", "Digger JCB-3"
  category          text,                       -- free text or seeded (cottage|digger|marquee|car|kayak|tool)
  resource_type     text,                       -- capacity pool key; NULL = its own named unit
  -- rate card (independent of the diary; a per-unit override of the tenant stay_pricing default)
  rate_daily        numeric,
  rate_weekly       numeric,
  rate_monthly      numeric,
  deposit_default   numeric,                    -- deposit taken to hold this unit
  -- condition + service
  condition_rating  numeric(2,1) DEFAULT 5.0,   -- 0.0–5.0, the CURRENT running condition
  condition_floor   numeric(2,1) DEFAULT 3.0,   -- below this -> maintenance queue
  next_service_due  date,
  service_interval_days integer,                -- for auto-advancing next_service_due
  active            boolean NOT NULL DEFAULT true,
  custom_data       jsonb NOT NULL DEFAULT '{}'::jsonb,  -- seam, like companies.custom_data
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_asset_units_tenant       ON asset_units (tenant_id, active);
CREATE INDEX IF NOT EXISTS idx_asset_units_tenant_type  ON asset_units (tenant_id, resource_type);
CREATE INDEX IF NOT EXISTS idx_asset_units_service_due  ON asset_units (tenant_id, next_service_due)
  WHERE next_service_due IS NOT NULL;

-- Link a booking to the specific unit it hires. Mirrors bookings.vehicle_id exactly.
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS unit_id    uuid REFERENCES asset_units(id) ON DELETE SET NULL;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS hire_state text;   -- held|reserved|out|returned (NULL = not a hire)
CREATE INDEX IF NOT EXISTS idx_bookings_unit ON bookings (tenant_id, unit_id) WHERE unit_id IS NOT NULL;

-- One condition snapshot: at handover (out) or return (in). Photos live in Supabase Storage;
-- we store their URLs (same as shipment_events.photo_url in the POD board).
CREATE TABLE IF NOT EXISTS condition_reports (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  unit_id       uuid NOT NULL REFERENCES asset_units(id) ON DELETE CASCADE,
  booking_id    uuid REFERENCES bookings(id) ON DELETE SET NULL,
  phase         text NOT NULL,                 -- 'out' (handover) | 'in' (return)
  rating        numeric(2,1),                  -- 0.0–5.0 assessed at this snapshot
  photo_urls    jsonb NOT NULL DEFAULT '[]'::jsonb,   -- ordered list of Storage URLs
  notes         text,
  captured_by   text,                          -- caretaker/staff name (free text, magic-link capture)
  captured_at   timestamptz NOT NULL DEFAULT now(),
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_condition_reports_unit    ON condition_reports (tenant_id, unit_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_condition_reports_booking ON condition_reports (tenant_id, booking_id);

-- A damage finding for one hire — the output of a photo-diff or a manual flag; the
-- evidence bundle a deposit deduction stands on.
CREATE TABLE IF NOT EXISTS damage_cases (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id          uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  unit_id            uuid NOT NULL REFERENCES asset_units(id) ON DELETE CASCADE,
  booking_id         uuid REFERENCES bookings(id) ON DELETE SET NULL,
  out_report_id      uuid REFERENCES condition_reports(id) ON DELETE SET NULL,
  in_report_id       uuid REFERENCES condition_reports(id) ON DELETE SET NULL,
  description        text,                      -- LLM or human summary of the damage
  diff_findings      jsonb NOT NULL DEFAULT '[]'::jsonb,  -- [{area, change, severity, confidence, out_url, in_url}]
  diff_confidence    numeric,                   -- overall model confidence in the comparison
  severity           text,                      -- none|minor|moderate|major
  proposed_deduction numeric,
  status             text NOT NULL DEFAULT 'open', -- open|disputed|resolved|dismissed
  resolution         text,                      -- deducted|refunded|waived|goodwill
  resolved_amount    numeric,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_damage_cases_open ON damage_cases (tenant_id, status) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_damage_cases_unit ON damage_cases (tenant_id, unit_id);

-- Deposit lifecycle over the existing Stripe rail. One row per hire's deposit.
CREATE TABLE IF NOT EXISTS unit_deposits (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  booking_id      uuid NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
  unit_id         uuid REFERENCES asset_units(id) ON DELETE SET NULL,
  amount          numeric NOT NULL,
  currency        text NOT NULL DEFAULT 'GBP',
  status          text NOT NULL DEFAULT 'pending', -- pending|held|deducted|refunded|failed|released
  method          text,                          -- stripe_hold (manual capture) | stripe_charge | manual
  external_ref    text,                          -- Stripe PaymentIntent / SetupIntent id
  deducted_amount numeric DEFAULT 0,
  damage_case_id  uuid REFERENCES damage_cases(id) ON DELETE SET NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_unit_deposits_booking ON unit_deposits (booking_id);
CREATE INDEX IF NOT EXISTS idx_unit_deposits_status ON unit_deposits (tenant_id, status);

-- RLS on every table (idempotent).
ALTER TABLE asset_units        ENABLE ROW LEVEL SECURITY;
ALTER TABLE condition_reports  ENABLE ROW LEVEL SECURITY;
ALTER TABLE damage_cases       ENABLE ROW LEVEL SECURITY;
ALTER TABLE unit_deposits      ENABLE ROW LEVEL SECURITY;
-- (one DROP POLICY IF EXISTS + CREATE POLICY "tenant isolation - <table>" per table,
--  FOR ALL USING (tenant_id IN (SELECT auth_tenant_ids())) WITH CHECK (...) — verbatim
--  from the Phase 4b migration.)
```

Reuse note: `crm_contacts`/`crm_deals` are NOT duplicated — the hirer is a CRM contact (autofilled), the hire is a CRM deal (auto-clustered). `bookings.thread_id`, `contact`, `details`, `deposit_amount`, `booking_ref` all carry over unchanged. Photos reuse the `photo_url` storage convention already in `shipment_events` (POD) — the Storage bucket + upload path is the one thing to confirm exists (POD uses it via the driver app; `pod.py` reads `photo_url`).

---

## §5 DB layer — `src/app/db.py`-style signatures

New functions in `db.py` (service-role or ctx-client, RLS-scoped, mirroring the fleet/vehicle CRUD block):

```python
# ── units (the vehicles-CRUD twin) ───────────────────────────────────────────
def list_units(tenant_id, *, active=None, client=None) -> list[dict]
def get_unit(unit_id, tenant_id, client=None) -> dict | None
def add_unit(tenant_id, unit: dict, client=None) -> dict
def update_unit(unit_id, tenant_id, fields: dict, client=None) -> dict
def set_unit_condition(unit_id, tenant_id, rating: float, client=None) -> None   # running condition
def units_due_service(tenant_id, before_iso: str, client=None) -> list[dict]     # maintenance lane
def seed_assets_defaults(tenant_id) -> None   # idempotent activation seed (like seed_fleet_defaults)

# ── availability + hold, scoped to a unit (thin wrappers over the reused engine) ─
def list_unit_bookings(tenant_id, unit_id, date_from_iso, date_to_iso, client=None) -> list[dict]
def hold_unit_booking(tenant_id, unit_id, start_block, length_blocks, summary,
                      *, deposit_amount=None, contact=None, details=None,
                      thread_id=None, source="email", status="proposed") -> dict | None
    # For a NAMED unit: pre-check no active booking overlaps the span on this unit
    #   (slotgrid.max_concurrent scoped to unit_id, capacity=1), then insert_stay_booking
    #   with unit_id + hire_state='held'. Atomic hardening = an assets sibling of the
    #   hold_stay_if_capacity RPC (see §12). For a POOL unit: no unit_id at hold; assigned
    #   best-fit at handover, exactly like hold_fleet_booking picks a vehicle.

# ── condition reports + photos ────────────────────────────────────────────────
def insert_condition_report(tenant_id, unit_id, booking_id, phase, *,
                            rating=None, photo_urls=None, notes=None, captured_by=None) -> dict
def list_condition_reports(tenant_id, *, unit_id=None, booking_id=None, client=None) -> list[dict]
def latest_out_report(tenant_id, booking_id, client=None) -> dict | None   # the 'out' baseline

# ── damage cases ──────────────────────────────────────────────────────────────
def insert_damage_case(tenant_id, unit_id, booking_id, *, out_report_id, in_report_id,
                       diff_findings, diff_confidence, severity, description,
                       proposed_deduction=None) -> dict
def list_damage_cases(tenant_id, *, status=None, client=None) -> list[dict]
def resolve_damage_case(case_id, tenant_id, *, resolution, resolved_amount, client=None) -> dict

# ── deposits (over the reused Stripe rail) ────────────────────────────────────
def upsert_deposit(tenant_id, booking_id, unit_id, amount, *, currency="GBP",
                   method="stripe_hold") -> dict
def set_deposit_status(deposit_id, tenant_id, status, *, external_ref=None,
                       deducted_amount=None, damage_case_id=None, client=None) -> dict
def get_deposit_for_booking(tenant_id, booking_id, client=None) -> dict | None

# ── tenants-with-module fan-out for the sweeps (reused pattern) ───────────────
# db.tenants_with_modules(("assets",)) already exists — used by the maintenance/handover job.
```

---

## §6 Engine / automation — `src/app/assets.py` + jobs

`src/app/assets.py` holds the pure view-models and the availability/quote wrappers (à la `pod.py`, `tms.py`, `scheduling.py`): `board(...)`, `unit_availability(...)`, `quote_unit(...)` (delegates to `diary_config.quote_stay` with the unit's rate override), `handovers_for(now)`, `maintenance_queue(now)`. No new capacity math — it composes the shipped functions.

Three sweeps registered on the **existing in-process scheduler** (`src/app/scheduler.py`), fanning out over `db.tenants_with_modules(("assets",))`, each idempotent (dedupe via a `*_queued_at`/state column exactly like `bookings.reminder_queued_at`):

1. **Handover reminders** — `run_daily(7, 30, ...)`. For units going out tomorrow with a paid deposit: `compose_outbound` a "you collect tomorrow" note → `insert_email_log(status='pending'|auto)` → optional `mailbox.send_mail` (the exact `send_service_reminders._remind_one` shape). For units going out with an *unpaid* deposit: chase the deposit link.

2. **Deposit lifecycle sweep** — `run_every(900, ...)` (15 min, alongside the CRM sequence sweep). Releases stale unpaid holds past a cutoff (cancel the booking, `hire_state=NULL`); flips `unit_deposits.status` from the Stripe webhook queue; drafts the refund/receipt outbound when a case resolves.

3. **Condition-diff pipeline** — triggered on `insert_condition_report(phase='in')` (not a timed sweep): fetch the matching `phase='out'` baseline, run the photo-diff LLM call (§12), write a `damage_case` when findings exceed the confidence/severity gate, and queue the appropriate `compose_outbound` (clean-return refund OR damage-with-evidence draft). Metered via `usage.record_anthropic(operation="condition_diff")`, same books as `crm_extract`/drafting.

Drafter integration is identical to the rest of the platform: facts assembled by the module → `compose_outbound(cfg, purpose, facts)` in the tenant voice → `insert_email_log` → auto-send iff `cfg.auto_send`, else pending for approval. Inbound hire enquiries flow through the normal `draft_reply` path with the unit rate card + availability injected into the scheduling prompt section (§3 state 1–2), reusing the existing tool-calling executor (`check_availability`/`hold_booking`).

---

## §7 API + routes — `src/app/assets_routes.py`

Server-rendered pages + a JSON surface, mirroring `crm_routes.py`/TMS route naming. All `Depends(require_onboarded)` + module gate (`assets` in `db.active_modules`), CSRF on writes (`require_csrf`).

```
# Surfaces
GET  /app/assets                      — the daily board (§2)
GET  /app/assets/units                — unit registry (list + add)
GET  /app/assets/units/{id}           — unit detail: rate card, condition trajectory, hire history, service
GET  /app/assets/bookings/{id}        — one hire: dates, deposit status, condition reports, damage case
GET  /app/assets/damage               — the damage-case queue
GET  /app/assets/damage/{id}          — one case: side-by-side photo-diff + evidence bundle

# Unit CRUD
POST   /app/assets/units              — add unit
PATCH  /api/assets/units/{id}         — edit rate/condition_floor/service
DELETE /api/assets/units/{id}         — soft delete (active=false)

# Condition capture (staff/caretaker — magic-link scoped, see §11)
GET  /capture/{token}                 — mobile condition-capture page (photo upload + rating)
POST /capture/{token}                 — persist condition_report (+ photos to Storage)

# Deposits
POST /api/assets/bookings/{id}/deposit/link     — mint Stripe deposit checkout/hold, return URL for the draft
POST /api/assets/bookings/{id}/deposit/refund   — refund/release
POST /api/assets/bookings/{id}/deposit/deduct   — capture N from the hold, link a damage_case

# Damage cases
POST /api/assets/damage/{id}/resolve  — deducted|refunded|waived + amount
POST /api/assets/damage/{id}/rerun    — re-run the diff (e.g. after adding photos)

# JSON for the board (dashboard cards)
GET  /api/assets/board                — today's handovers + upcoming + maintenance counts
GET  /api/assets/availability?unit_id=&from=&to=   — reused availability, scoped to a unit
```

Deposit-link + capture endpoints are the only genuinely new I/O; everything else is CRUD + the reused availability read.

---

## §8 UI — pages + nav

Server-rendered Jinja2, house design system (right-aligned icon pill settings entry, like Diary/CRM). Pages:

- **`/app/assets` (board)** — the three-lane daily surface (§2). Handover rows carry the one-tap capture CTA; upcoming rows show deposit chips; maintenance rows link to the unit.
- **`/app/assets/units` + `/units/{id}`** — the unit registry (the `vehicles` settings-page twin): asset ref, category, rate card (daily/weekly/monthly + deposit), condition rating with its trajectory sparkline, next-service field, hire history. Condition history reads from `condition_reports`.
- **`/app/assets/damage` + `/damage/{id}`** — the queue and the case page: **side-by-side out/in photos** with the diff findings annotated, the proposed deduction, and the deposit action buttons. This page IS the damage-claim evidence bundle (exportable, §12).
- **`/capture/{token}`** — a stripped mobile page for the caretaker/handover staff: pick the booking, snap/upload photos, set a 0–5 rating, note. No login (tokenised magic-link, §11), like `/track/{token}` and the driver app.

**Nav / module hub.** Add an `assets` card to `dashboard.MODULES` (group `addon`, badge "Lettings & hire", href `/app/assets`). It lights when a `subscriptions` row (`module='assets'`, `status='active'`) exists — the switchboard rollout, zero code path change (same as TMS/FSM).

**Today dashboard presence.** In `app_home`, when `assets` in `mods`, surface a routing card (today's handovers count + open damage cases + deposits pending), best-effort try/except like the CRM card.

---

## §9 Onboarding + industry seeds

The seeds already largely exist — Assets OS onboarding is mostly *unhiding* and *filling out*.

- **`src/app/operating_systems.py`** — flip Assets OS `live: True` in `OPERATING_SYSTEMS` and add any new industries to `INDUSTRY_TO_OS` (already: `lettings→assets`, `hire→assets`; add `car_hire`, `boat_hire`, `tool_hire`, `party_hire`, `camping_hire` when featured).
- **`src/app/onboarding.py` INDUSTRIES** — remove `"hidden": True` from `lettings` + `hire`, set `"live": True`; add the new hire sub-industries as entries (`os: "assets"`). Keep the free-text "Something else" catch-all.
- **`DATA_SOURCES`** — add an `assets`-appropriate block: `{kind: "channel_manager", vendors: ["Airbnb","Booking.com","Vrbo","Other"]}` for lettings; `{kind: "hire_software", vendors: ["inspHire","Point of Rental","Current RMS","Other"]}` for hire; plus calendar + spreadsheet. Feeds the roadmap-signal capture already in `data_post`.
- **`src/app/industry_lingo.py`** — LINGO + CRM_SEEDS for `lettings`/`hire` exist (portfolio size, landlord/agent, property, rent; typical kit, account/cash, equipment, hire period). Extend CRM_SEEDS with a `unit`-adjacent deal field where useful (e.g. lettings deal "Party size", hire deal "Off-hire date"). Add seeds for the new hire sub-industries by cloning `hire`.
- **Intake / stay setup (reuse).** Assets tenants turn on the `intake.STAY_FIELD` toggle (departure/collection) → their bookings become spans. `settings_guide.py` already ships holiday-let / plant-hire profiles with stay pricing + deposit + min-hire copy. The assets-activation seed (`seed_assets_defaults`) turns stays on and seeds a couple of example units so the diary works out of the box (mirrors `seed_fleet_defaults` seeding pallet types + a Main branch).

---

## §10 Module registration + plan wiring

- **`src/app/dashboard.py` MODULES** — new `assets` entry (§8).
- **`src/app/plans.py`**
  - `INDUSTRY_MODULE_META`: add
    ```python
    "lettings": {"key": "assets", "label": "Assets OS · Lettings", "name": "Assets OS · Lettings",
                 "features": ["Unit availability & atomic booking", "Deposits (take, hold, refund, deduct)",
                              "Condition photo-diffs & damage claims", "Handover & return board",
                              "Maintenance scheduling"]},
    "hire":     {"key": "assets", "label": "Assets OS · Hire", "name": "Assets OS · Hire",
                 "features": ["Fleet-of-units availability", "Deposits & damage claims",
                              "Condition photo-diffs", "Off-hire & return board", "Service scheduling"]},
    ```
    (`INDUSTRY_MODULE` derives `lettings→assets`, `hire→assets` automatically; `MANAGED_MODULES` picks `assets` up.)
  - The `professional` tier renders "Core + Assets OS · Lettings/Hire" per-industry via the existing `billing_plans(industry)` machinery — no new tier code, just the META entry.
- **`BASE_PLAN_MODULES`** — unchanged; `assets` is an industry module unlocked on the `+industry` tiers (`professional`/`scale`/`enterprise`), exactly like `tms`/`fsm`.
- **Pricing tier.** Assets OS is the tenant's industry module on the **£299 Core + Field/Assets** slot (the per-vertical add-on price). CRM + scheduling + drafter remain in Core £99 (the stay/deposit spine is already Core scheduling). Deposits and condition-diffs are the paid differentiator that justifies the +industry tier. Stripe price ids are env-driven; no catalog change beyond the per-industry label.

---

## §11 OS-native integrations

| Integration | v1 | Later |
|---|---|---|
| **Stripe deposits** | **Reuse the shipped Stripe rail** (`src/app/billing.py` — hosted Checkout, PCI-scope zero, server-side redirect, webhook sync). v1: a deposit is a **Stripe PaymentIntent with manual capture** (an authorised hold) OR a straight charge, minted per booking, link dropped into the confirmation draft; webhook flips `unit_deposits.status`. Refund = Stripe refund/void; deduct = capture N of the held amount. | GoCardless for account customers (net terms); saved-card (SetupIntent) for repeat hirers; multi-currency. |
| **Damage photo capture** | Tokenised **magic-link capture page** (`/capture/{token}`, lean like `/track/{token}` + the driver app), photos to Supabase Storage, URL stored in `condition_reports.photo_urls` (the `shipment_events.photo_url` convention). | Native driver-app-style installable capture; EXIF/timestamp verification; on-site offline queue. |
| **Insurance lookup** | Out of scope for v1 beyond a free-text `insurer` custom field on the unit/contact (LLM-autofilled from email, reusing CRM extraction). | Automated insurer/policy validation API for hire businesses; excess-aware deposit sizing. |
| **Maintenance scheduling** | `next_service_due` + `service_interval_days` on the unit; the maintenance lane + the handover sweep surface due services; a completed service advances `next_service_due` by the interval. Reuses the recurring-service cadence pattern from FSM (`REMINDER_LEAD_DAYS`, `service_months`). | Integration with third-party maintenance/MOT reminder systems; parts/cost logging. |

---

## §12 Killer feature deep-dive

### (a) Atomic capacity — REUSE, be explicit
The "can't double-book, ever" promise is **not rebuilt**. It IS the scheduling primitive:

- **Named unit** (cottage/car/boat): a hire is a stay span (`insert_stay_booking` with `unit_id`). Availability of unit U over a window = `slotgrid.max_concurrent` of *U's active bookings* against capacity 1 — i.e. `service_availability(...)`/the stay-offer path scoped by `unit_id`. Two racing enquiries for the same cottage-week cannot both hold: the atomic decision lands in the DB, exactly as `hold_booking_if_capacity`/`hold_fleet_booking` do today. The production hardening is an **`hold_unit_if_capacity` RPC** — the assets sibling of `hold_stay_if_capacity` named in `db.insert_stay_booking`'s docstring — doing the concurrent-occupancy check + insert in one transaction. Until that ships, the app-layer pre-check + unique-ish constraint is the same "too cautious, never wrong" posture the rest of the diary runs on.
- **Fungible pool** (12 drills, 4 kayaks): capacity N, best-fit unit assignment at handover — the `best_fit`/`hold_fleet_booking` pattern verbatim, just picking an `asset_unit` instead of a `vehicle`.

The value line is unchanged from the product's core promise, now applied to physical assets: *the diary the agent books into is the same diary the deposit and the handover run off*.

### (b) Condition photo-diffs — the novel part
The one thing no incumbent does: **before/after LLM photo comparison that produces damage-claim evidence**.

**Trigger.** On `insert_condition_report(phase='in')` at return, fetch the `phase='out'` baseline for the same `booking_id` (`db.latest_out_report`).

**Mechanics.**
1. Pair photos by view. v1: pair by **ordinal position** (out photo 1 ↔ in photo 1) — the capture UI prompts the same fixed shot list per unit category ("front", "interior", "engine/mechanism", "accessories"), so ordering is meaningful. `asset_units.custom_data._shot_list` (seeded per category) drives the prompts.
2. One multimodal Anthropic call per pair (the model already used for drafting/extraction — no new provider; reuse `agent.client()`). Structured JSON output (`output_config` json_schema, same shape as `crm_extract.EXTRACT_SCHEMA`):
   ```
   SYSTEM: "You compare a BEFORE and an AFTER photo of the same hired item from the
   same viewpoint. Report ONLY new damage or material change visible in AFTER that
   is NOT in BEFORE — scratches, dents, stains, breakage, missing parts, wear beyond
   fair use. Ignore lighting, angle and reflection differences. Never invent damage.
   For each finding return {area, change, severity(minor|moderate|major), confidence(0-1)}.
   If AFTER matches BEFORE, return an empty findings list with a clean-return note."
   USER: [before image] [after image] + unit category + fair-wear guidance.
   ```
3. Aggregate findings → `damage_cases.diff_findings` (each carries its out/in URL), an overall `diff_confidence`, a `severity` (max of findings), and — when the tenant sets per-category repair costs — a `proposed_deduction`.

**Confidence gate (mirrors the CRM autofill gate exactly — `crm_fields.gate`):**
- **≥ 0.80 and severity ≥ moderate** → open a `damage_case` `status='open'`, draft the "we found X, here's the evidence, proposing £Y deduction" outbound (owner approves before sending — deposits are disputes-prone, never auto-send a deduction claim).
- **0.55–0.80** → open the case but flag "review the photos" — surface on the damage queue, no draft yet.
- **< 0.55 or empty findings** → clean return; draft the deposit-refund note.

The deduction itself is **always human-approved** (§14 risk). BLAM8 produces the evidence and the draft; the owner clicks deduct/refund/waive.

**Evidence bundle.** The `/app/assets/damage/{id}` page is the claim: side-by-side out/in photos, per-finding annotations, timestamps, `captured_by`, the model's reasoning, and the deposit trail. Exportable (PDF/JSON) — "yours to export" brand promise. This is what a holiday-let owner sends Airbnb's resolution centre or shows a disputing hirer.

**Failure modes to state honestly:** angle/lighting false positives (mitigated by fixed shot list + the "ignore lighting" instruction + the confidence gate + human approval); pre-existing damage the out-photo missed (mitigated by requiring an out-report before a deduction — no baseline, no claim); photos not taken (the handover reminder chases capture; a hire with no out-report can't produce a diff and shows "no baseline captured"). The model is an *evidence assistant*, never the adjudicator.

---

## §13 Build order — independently-shippable phases

**Phase A — Units + reused booking (the spine visible).** `asset_units` table + `bookings.unit_id`/`hire_state`, the unit registry UI, `hold_unit_booking` scoping the *shipped* stay availability to a unit, the `/app/assets` board (handovers + upcoming lanes, no damage lane yet), module registration + plan META, onboarding unhide. A lettings tenant can register cottages and take atomic bookings against them. **~1 week.** (Mostly wiring; the capacity engine is reused.)

**Phase B — Deposits.** `unit_deposits` + the Stripe deposit-link/refund/deduct endpoints over the reused `billing.py` rail, deposit chips on the board, the deposit lifecycle sweep, the deposit-link touchpoint in the confirmation draft. Deposit taken → held → refunded end to end. **~1 week.**

**Phase C — Condition + photo-diffs (the killer feature).** `condition_reports` + `damage_cases`, the `/capture/{token}` magic-link page + Storage upload, the diff pipeline + gate, the damage queue + evidence-bundle page, the clean-return/damage drafter touchpoints, deduction wired to the deposit. **~1.5–2 weeks** (the multimodal diff + capture UX is the real new engineering).

**Phase D — Maintenance + polish.** `next_service_due`/interval, the maintenance lane + handover sweep, insurer field, export of the evidence bundle, per-category shot lists + repair-cost defaults. **~1 week.**

Total ~4.5–5 weeks single-dev with LLM assistance — consistent with the doc's "~4–6 weeks per OS" estimate. Phases A/B are shippable demos on their own; C is the differentiator.

---

## §14 Open questions + risks

- **Deposit hold duration.** Stripe PaymentIntent manual-capture authorisations expire (~7 days card-dependent). For long hires the authorised hold lapses before return. Decision: for hires longer than the hold window, take a **charge + refund** (money moves) rather than an authorisation, or re-authorise near return. Flag per-tenant; default to charge-and-refund for >5-day hires.
- **Damage disputes + liability.** BLAM8 must never be the adjudicator. Every deduction is human-approved; the model output is evidence + a *proposal*. Copy and ToS must make clear the tenant owns the deduction decision. Consider a required "dispute window" before capture.
- **Pre-existing damage / no baseline.** No `phase='out'` report → no deduction is permitted (UI hard-blocks the deduct button). The handover reminder must push out-capture hard.
- **Photo authenticity.** v1 trusts uploaded photos. EXIF/timestamp verification and same-device capture are later hardening; state the limitation to tenants.
- **Named-unit vs pool ambiguity.** A tenant with "3 cottages" that are genuinely different (named) vs "12 identical drills" (pool) must be classified at onboarding — it changes the capacity scoping. Ask explicitly ("are your units interchangeable?"); default lettings→named, tool/party hire→pool.
- **Channel-manager double-booking.** The atomic promise only covers bookings *through BLAM8*. An Airbnb booking made outside BLAM8 can still clash until a channel-manager sync exists (v2). v1 honesty: BLAM8 owns the direct-booking diary; import Airbnb/Booking.com via the calendar/data-source capture as read-only busy blocks (reuse the existing calendar-import-as-busy-block path).
- **Deposit vs hire-fee accounting.** The deposit is not revenue (it's a liability held); the hire fee is. Keep `unit_deposits` separate from the invoicing engine's revenue so reconciliation and VAT stay correct.

---

## §15 Reuse map

| Part | REUSE (name it) | BUILD NEW |
|---|---|---|
| Multi-day availability window | `bookings` + `insert_stay_booking` + `slotgrid` (spans, `max_concurrent`, `stay_span`) | `bookings.unit_id`/`hire_state` columns; unit-scoped wrapper `hold_unit_booking` |
| Atomic "can't double-book" | `hold_booking_if_capacity`/`hold_fleet_booking` RPC pattern; `service_availability` concurrent-occupancy | `hold_unit_if_capacity` RPC (assets sibling; hardening) |
| Fungible-pool best-fit assignment | `scheduling.best_fit` + `hold_fleet_booking` (vehicle pool) | pool scoping over `asset_units` instead of `vehicles` |
| Rate + deposit + min-hire | `diary_config.clean_stay_pricing`/`quote_stay`; `bookings.deposit_amount`; `settings_guide` stay profiles | per-unit rate override on `asset_units`; `unit_deposits` lifecycle |
| The durable supply object | `vehicles` table + fleet CRUD (`list_vehicles`, `seed_fleet_defaults`) as the exact pattern | `asset_units` table + `db.list_units`/`add_unit`/`seed_assets_defaults` |
| Deposit payment rail | `src/app/billing.py` Stripe hosted Checkout + webhook sync (PCI-zero) | deposit-specific PaymentIntent manual-capture wrappers + refund/deduct endpoints |
| Photo capture + storage | `shipment_events.photo_url` + Supabase Storage; `/track/{token}` + driver-app magic-link pattern | `/capture/{token}` condition page; `condition_reports.photo_urls` |
| Before/after damage comparison | `agent.client()` multimodal call; `crm_extract` json-schema output + `crm_fields.gate` confidence pattern | the photo-diff prompt, pairing, `damage_cases`, evidence-bundle page |
| Self-filling hirer/deal data | CRM autofill (`crm_extract`, `crm_fields`), deal auto-clustering, promises (`crm_intel`), gone-quiet nudges | `unit`-adjacent CRM seeds (already partly present) |
| Drafter in tenant voice | `agent.draft_reply` (inbound), `agent.compose_outbound` (proactive); `insert_email_log` → `mailbox.send_mail` (`send_service_reminders` shape) | per-state facts assembly (confirmation, deposit link, refund, damage-with-evidence) |
| Automation loops | `scheduler.run_daily`/`run_every`; `db.tenants_with_modules`; `*_queued_at` dedupe | 3 assets sweeps (handover, deposit lifecycle, diff pipeline) |
| Maintenance cadence | FSM recurring-service pattern (`REMINDER_LEAD_DAYS`, `service_months`, `send_service_reminders`) | `next_service_due`/`service_interval_days` + maintenance lane |
| Module registry + plan wiring | `dashboard.MODULES`, `subscriptions`-row switchboard, `plans.INDUSTRY_MODULE_META`/`billing_plans(industry)` | `assets` MODULES card + two META entries |
| OS + onboarding | `operating_systems.py` (`lettings`/`hire`→`assets` mapped), `onboarding.INDUSTRIES`/`DATA_SOURCES`, `industry_lingo` seeds, `intake.STAY_FIELD` toggle | flip `assets` live; unhide lettings/hire; assets `DATA_SOURCES` block; hire sub-industry seeds |
| Migration convention | `scripts/db/migrations/apply_crm_phase4b_migration.py` (idempotent psycopg2, RLS, verify) | `scripts/db/migrations/apply_assets_migration.py` |
| Daily surface view-model | `pod.board`/`tms`/`dashboard._today_strip` pure view-model pattern | `assets.board` three-lane handover/upcoming/damage |
