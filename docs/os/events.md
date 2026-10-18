# BLAM8 Events OS — Design & Build Spec

> **Status (2026-09-27, confirmed by the clean-up pass):** BUILT, phases A–C (src/app/events_os.py, src/app/events_routes.py; tests/test_events_*.py), deployed and marked live. Live proof: none.

**Status:** 🌱 Vocabulary seed exists (`src/app/industry_lingo.py` LINGO["events"] + CRM_SEEDS["events"]); module not built. This spec is the build plan.
**Author:** Julius Deane, w/ Claude synthesis
**Last updated:** 2026-07-05
**Reads:** `docs/BLAM8_operating_systems.md` §2, §4.6, §5, §6 · `docs/architecture/CRM_design.md` (platform patterns, promises §5/§18, sequences §19.3)

---

## §0 Positioning

**Shape.** The customer books a *one-off, dated occasion* that the business staffs, caters and runs — a wedding, a corporate away-day, a conference, a private function. Unlike Practice OS (repeating appointments) or Assets OS (a unit hired and returned), an Events OS booking is a **long-lived, multi-party project**: one date, many stakeholders (the client, the caterer, the bar, the florist, the AV crew, the assigned staff, the guests-with-dietaries), and a fixed run-of-show that all of them must converge on by the day.

**The one-liner.** *"The events coordinator that chases everyone so you don't have to."* BLAM8 already reads the inbox and drafts in the venue's voice; Events OS turns that inbox into a coordination engine — every outstanding item across every supplier and staff member is tracked and chased automatically.

**Wedge.** The single-venue manager running **2–4 events a week** off a spreadsheet, a WhatsApp thread, a shared inbox and a paper checklist. They don't need Tripleseat or Perfect Venue's seating-chart bells; they need to stop being the human message-bus between the couple, the caterer and the bar. BLAM8 becomes one flow: enquiry lands → deal opens → deposit chased → every stakeholder's outstanding item tracked → run-of-show assembled → nobody dropped.

**Target industries (~40K UK SMBs).** Event venues (barns, halls, hotels-with-function-rooms), catering companies, wedding planners, corporate event organizers, small production companies, small conference organizers. All share the `event` primitive; industry seeds carry the vocabulary (weddings talk "carriages" and "top table"; corporate talks "breakout" and "AV rider").

**Build state.** Not built. Present today: the `events` industry id (hidden in `src/app/onboarding.py:52`), the `events` OS entry (`src/app/operating_systems.py:32`, `live: False`), the vocabulary seed and CRM field seeds (`src/app/industry_lingo.py`). This spec adds the primitive, the coordination engine, and the daily surface on top of the already-shipped CRM + drafter + scheduling platform.

---

## §1 The primitive — `event` as a multi-party project

An `event` is **a CRM deal with a date, a headcount, a stakeholder set, and a run-of-show**. It is not a new relationship-tracking substrate — it *is* a deal, enriched.

| CRM `deal` today | `event` needs it to also carry |
|---|---|
| title, company, primary contact, stage, value, status | ✅ reuse verbatim — an event's client relationship is exactly a deal |
| activity feed (emails in/out) | ✅ reuse — every enquiry/planning email already rolls up |
| promises (we-owe / they-owe) | ✅ **reuse and lean on hard** — the coordination engine (§12) is promise-tracking pointed at suppliers + staff, not just the client |
| custom_data (LLM-autofilled) | ✅ reuse — guest count, dietary notes, event date already seed via `CRM_SEEDS["events"]` |
| — | ➕ **event date + timings** (arrival/speeches/dinner/dance/carriages) |
| — | ➕ **guest list** (per-guest RSVP + dietary requirement) |
| — | ➕ **stakeholder set** (suppliers + assigned staff, each with an outstanding-item state) |
| — | ➕ **run-of-show** (the assembled minute-by-minute schedule + contingency notes) |

**Design decision: `crm_events` is a thin extension row, not a parallel entity.** Every event has exactly one backing `crm_deals` row (1:1). The deal carries the money, stage and client relationship; `crm_events` carries the date + event-shaped fields; the guest list, stakeholders and run-of-show hang off the event. This means **the entire CRM machine already works on events for free** — the drafter's `<customer_context>` block (`crm_intel.build_crm_context`), gone-quiet nudges, reporting, tasks and sequences all operate on the deal with zero new wiring. We add event-shaped surfaces, not a second CRM.

**Justifying each new table (§4 has the SQL):**

- `crm_events` — **new, required.** The deal has no date, no timings, no capacity. A one-off dated occasion needs them and the daily surface (§2) keys entirely off `event_date`. Cannot be `custom_data` because it's queried by range ("this week's events"), joined and sorted — JSONB is wrong for the spine field.
- `crm_event_guests` — **new, required.** A guest list is an unbounded set of people-with-attributes (RSVP, dietary, table). It is emphatically *not* CRM contacts (guests aren't the venue's customers; a wedding has 120 of them and the venue will never email most). A dedicated child table with a **dietary matrix rollup** is the point.
- `crm_event_stakeholders` — **new, required.** The killer feature. A supplier/staff member on an event, each with an `outstanding` state the chase engine drives. Could *almost* be modelled as promises alone, but a stakeholder is a first-class party (has contact details, a role, a confirmation status) that *owns* promises — so it's a light table that the promise engine references, not replaces (see §12).
- Run-of-show — **no new table.** It's the ordered timings on `crm_events` (`timings jsonb`) plus `contingency_notes text`. Assembled by the engine (§6), rendered as a document. A separate table buys nothing until we need per-line sign-off, which is a v2 concern.

Everything else — companies, contacts, activities, stages, promises, tasks, sequences, custom fields, suggestions — is **reused unchanged** (`crm_*` tables already shipped).

---

## §2 Daily-rhythm surface

**Route:** `/app/events` — the Events OS home, the venue manager's morning screen. Mirrors the Field OS dispatch board and the office Diary strip (`dashboard._today_strip`), but the unit is the event, not the slot.

```
THIS WEEK                                      [◀ prev]  Week of 7 Jul  [next ▶]

┌─ Sat 12 Jul · Harper–Singh Wedding ──────────────── 118 guests ─ £8,400 ┐
│  Timings   Arrival 13:00 · Speeches 15:30 · Dinner 16:00 · Carriages 23:30 │
│  Dietary   6 vegan · 3 GF · 2 nut allergy · 1 halal      [matrix ▸]        │
│  Suppliers ✅ Caterer confirmed  ⚠ Bar (licence?)  ⏳ Florist (no reply 4d) │
│  Staff     4 of 6 assigned — 2 waiting staff still to confirm             │
│  Chase     BLAM8 is chasing: Florist (2nd nudge), Bar licence (staff)     │
│  Run sheet ● Draft ready — 3 gaps flagged            [open run-of-show ▸]  │
└──────────────────────────────────────────────────────────────────────────┘

┌─ Wed 16 Jul · Meridian Ltd Away-Day ─────────────── 40 guests ─ £2,100 ─┐
│  ...                                                                       │
└──────────────────────────────────────────────────────────────────────────┘

NEXT 4 WEEKS   3 events · 2 deposits outstanding · 1 run-sheet not started
```

Each card is a live rollup, computed server-side from `crm_events` + children (no data entry). The three status lights per supplier/staff (`✅ confirmed / ⚠ needs-something / ⏳ chasing`) are the manager's whole morning in one glance. Every ⏳ is the coordination engine already at work — the manager reads, they don't chase.

This surface also lands as a **Today card** on `/app` (like the CRM card in `dashboard.app_home`): "2 events this week · Harper–Singh needs the final headcount · BLAM8 is chasing 3 suppliers." Best-effort, hides on read failure, same pattern as the existing CRM card.

---

## §3 Workflow state machine

The stage pipeline is the tenant's `crm_stages` (already per-tenant configurable). Events OS seeds an **event-shaped default pipeline** at first touch (like `crm.DEFAULT_STAGES` but event-flavoured), and each transition is a **drafter touchpoint** — the drafter is invoked at every arrow, in the tenant's voice.

```
Enquiry ──▶ Quoted ──▶ Deposit ──▶ Planning ──▶ Run-of-show ──▶ Event day ──▶ Post-event
   │           │           │           │             │              │             │
   │           │           │           │             │              │             └─ post_event follow-up:
   │           │           │           │             │              │                thank-you + feedback +
   │           │           │           │             │              │                (photos/review/rebook)
   │           │           │           │             │              └─ day-of: final run-sheet to all
   │           │           │           │             │                 stakeholders + staff call-times
   │           │           │           │             └─ run-of-show assembled from timings + supplier
   │           │           │           │                confirmations; gaps flagged; sign-off requested
   │           │           │           └─ multi-stakeholder chase runs continuously here (§12):
   │           │           │              guest count firming, dietaries collected, suppliers booked
   │           │           └─ deposit chaser drafted; on payment detection (§11) → advance
   │           └─ LLM composes the quote/proposal from event fields + tenant template + RAG
   └─ enquiry auto-creates the deal+event (CRM deal clustering already does this on the first inbound)
```

| Transition | Drafter touchpoint | Mechanism (all reused) |
|---|---|---|
| inbound enquiry → **Enquiry** | none (silent capture) | `crm.ingest_email_log_row` + `crm_intel.apply_deal` already create the deal; the event row is created alongside when the LLM tags it as an event enquiry (§6) |
| Enquiry → **Quoted** | draft the quote/availability reply | `agent.draft_reply` with `crm_context` (event date, headcount, dietaries already in the block) |
| Quoted → **Deposit** | deposit request + chaser | `agent.compose_outbound` → `insert_email_log` (kind=`event_deposit`), auto-send if `cfg.auto_send`; overdue chaser via daily job |
| Deposit → **Planning** | confirmation + "here's what I need from you" | `compose_outbound`; opens the coordination promises for suppliers/staff |
| Planning → **Run-of-show** | request final numbers + assemble run sheet | engine assembles (§6); drafter requests the final headcount if still probeable |
| Run-of-show → **Event day** | final run-sheet blast to every stakeholder | `compose_outbound` per stakeholder (fan-out, §12) |
| Event day → **Post-event** | thank-you + feedback + rebook/referral | `compose_outbound`; optionally enroll in a post-event **sequence** (§6) |

The manager can override any stage by hand on the event card; the machine never forces a move (same contract as the CRM pipeline — nothing auto-advances a stage the tenant didn't consent to, except the two mechanical ones: deposit-paid detection and event-date-passed).

---

## §4 Schema — additive migration

House conventions: additive only, RLS on every table with the `auth_tenant_ids()` predicate, tenant_id on every row, indexed for the daily surface's range query, idempotent psycopg2 script (§13 / mirrors `scripts/db/migrations/apply_crm_phase4b_migration.py`). CRM tables are prefixed `crm_*`; these follow suit.

```sql
-- Events OS §4.6: the event primitive (1:1 with a crm_deals row)
CREATE TABLE IF NOT EXISTS crm_events (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  deal_id           uuid NOT NULL REFERENCES crm_deals(id) ON DELETE CASCADE,
  event_date        date,                       -- the spine field; NULL until pinned
  event_type        text,                       -- wedding | corporate | conference | private | other
  guest_count       integer,                    -- current best headcount (firmed over planning)
  guest_count_final boolean NOT NULL DEFAULT false,
  timings           jsonb NOT NULL DEFAULT '[]',-- [{"label":"Arrival","time":"13:00"}, ...] run-of-show spine
  drink_licence     text,                       -- 'held' | 'applied' | 'not_required' | NULL(unknown)
  contingency_notes text,                       -- wet-weather plan, backup supplier, etc.
  run_of_show       jsonb NOT NULL DEFAULT '{}',-- assembled sheet: {"lines":[...], "gaps":[...], "assembled_at":...}
  status            text NOT NULL DEFAULT 'planning', -- mirrors the deal; convenience for the board query
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (deal_id)                              -- one event per deal
);
CREATE INDEX IF NOT EXISTS idx_crm_events_tenant_date
  ON crm_events (tenant_id, event_date);        -- the "this week" range query

-- Guest list (NOT crm_contacts — guests aren't the venue's customers)
CREATE TABLE IF NOT EXISTS crm_event_guests (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  event_id      uuid NOT NULL REFERENCES crm_events(id) ON DELETE CASCADE,
  name          text,
  party_size    integer NOT NULL DEFAULT 1,     -- "+1", a family of 4, a table
  rsvp          text NOT NULL DEFAULT 'unknown',-- unknown | yes | no | maybe
  dietary       text,                           -- free text: 'vegan', 'nut allergy', 'GF'
  dietary_tags  jsonb NOT NULL DEFAULT '[]',    -- normalised: ["vegan"], ["gluten_free","nut_allergy"]
  table_name    text,                           -- optional seating hint (v1: freeform)
  source_email  text,                           -- email_log id the guest/dietary was extracted from
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_crm_event_guests_event
  ON crm_event_guests (tenant_id, event_id);

-- Stakeholders: the suppliers + assigned staff the chase engine drives (§12)
CREATE TABLE IF NOT EXISTS crm_event_stakeholders (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id      uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  event_id       uuid NOT NULL REFERENCES crm_events(id) ON DELETE CASCADE,
  kind           text NOT NULL,                 -- supplier | staff
  role           text NOT NULL,                 -- 'caterer' | 'bar' | 'florist' | 'AV' | 'waiting_staff' | ...
  name           text,
  email          text,                          -- who we chase (supplier contact or staff member)
  contact_id     uuid REFERENCES crm_contacts(id) ON DELETE SET NULL, -- if they're already a CRM contact
  confirmation   text NOT NULL DEFAULT 'pending',-- pending | confirmed | declined | needs_info
  outstanding    text,                          -- the item we're waiting on: 'final numbers', 'licence', 'call time'
  outstanding_promise_id uuid REFERENCES crm_promises(id) ON DELETE SET NULL, -- the tracked chase (§12)
  last_chased_at timestamptz,
  chase_count    integer NOT NULL DEFAULT 0,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_crm_event_stakeholders_event
  ON crm_event_stakeholders (tenant_id, event_id, confirmation);

-- RLS on all three (identical predicate to every crm_* table)
ALTER TABLE crm_events ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "tenant isolation - crm_events" ON crm_events;
CREATE POLICY "tenant isolation - crm_events" ON crm_events
  FOR ALL USING (tenant_id IN (SELECT auth_tenant_ids()))
  WITH CHECK (tenant_id IN (SELECT auth_tenant_ids()));

ALTER TABLE crm_event_guests ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "tenant isolation - crm_event_guests" ON crm_event_guests;
CREATE POLICY "tenant isolation - crm_event_guests" ON crm_event_guests
  FOR ALL USING (tenant_id IN (SELECT auth_tenant_ids()))
  WITH CHECK (tenant_id IN (SELECT auth_tenant_ids()));

ALTER TABLE crm_event_stakeholders ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "tenant isolation - crm_event_stakeholders" ON crm_event_stakeholders;
CREATE POLICY "tenant isolation - crm_event_stakeholders" ON crm_event_stakeholders
  FOR ALL USING (tenant_id IN (SELECT auth_tenant_ids()))
  WITH CHECK (tenant_id IN (SELECT auth_tenant_ids()));
```

**Capacity note (optional, v1.1).** Venue capacity ("the barn holds 150") is a `tenant_config` number, not a scheduling `bookings` row — an event isn't a diary slot, it's a whole-day exclusive occupancy. Double-booking a date is prevented cheaply by a uniqueness check on `crm_events (tenant_id, event_date)` at the *venue* level (configurable: some venues run two rooms). For multi-room venues we reuse `src/app/scheduling.py`'s atomic hold RPC keyed on a per-room resource; deferred to v1.1 (§11), most single-venue wedge tenants run one space.

---

## §5 DB layer — `src/app/db.py` function signatures

New `crm_event_*` helpers in `src/app/db.py`, RLS-scoped like every existing `crm_*` query (service-role writes for the engine, `client=`-scoped reads for routes).

```python
# ── Events ──────────────────────────────────────────────────────────────────
def crm_create_event(tenant_id, *, deal_id, event_date=None, event_type=None,
                     guest_count=None, client=None) -> dict: ...
def crm_get_event(event_id, tenant_id, *, client=None) -> dict | None: ...
def crm_get_event_by_deal(deal_id, tenant_id, *, client=None) -> dict | None: ...
def crm_update_event(event_id, tenant_id, updates: dict, *, client=None) -> dict: ...
def crm_events_between(tenant_id, start_date, end_date, *, client=None) -> list[dict]:
    """The daily surface's spine query — events with event_date in [start,end],
       ordered by date, each with joined deal (title/value/stage) + child rollups."""
def crm_list_events(tenant_id, *, status=None, limit=200, client=None) -> list[dict]: ...

# ── Guests + dietary rollup ──────────────────────────────────────────────────
def crm_upsert_guest(tenant_id, *, event_id, name=None, party_size=1, rsvp="unknown",
                     dietary=None, dietary_tags=None, source_email=None,
                     client=None) -> dict:
    """Idempotent on (event_id, lower(name)) so re-extraction from a later email
       reinforces one guest, not duplicates — same discipline as promise dedup."""
def crm_list_guests(tenant_id, event_id, *, client=None) -> list[dict]: ...
def crm_dietary_matrix(tenant_id, event_id, *, client=None) -> dict:
    """{"vegan": 6, "gluten_free": 3, "nut_allergy": 2, ...} aggregated from
       dietary_tags — the caterer order + the board card render from this."""
def crm_guest_count_rollup(tenant_id, event_id, *, client=None) -> dict:
    """{"confirmed": n, "declined": n, "unknown": n, "total_party": n} from rsvp."""

# ── Stakeholders (the chase substrate) ───────────────────────────────────────
def crm_add_stakeholder(tenant_id, *, event_id, kind, role, name=None, email=None,
                        contact_id=None, outstanding=None, client=None) -> dict: ...
def crm_list_stakeholders(tenant_id, event_id, *, client=None) -> list[dict]: ...
def crm_update_stakeholder(stakeholder_id, tenant_id, updates: dict, *,
                           client=None) -> dict:
    """confirmation / outstanding / last_chased_at / chase_count / promise link."""
def crm_stakeholders_to_chase(tenant_id, *, before_date=None) -> list[dict]:
    """Service-role sweep query: stakeholders whose confirmation='pending' or
       'needs_info' on events still in the future, with an open outstanding item —
       the coordination engine's work queue (§12)."""

# ── Run-of-show ──────────────────────────────────────────────────────────────
def crm_set_run_of_show(event_id, tenant_id, run_of_show: dict, *, client=None) -> dict: ...
```

`crm_create_event` is called from the intel hook (§6) when an enquiry is tagged as an event; the rest back the routes (§7) and the engine (§6). All follow the existing `crm_*` return-the-row / merge-updates convention seen in `crm.py`.

---

## §6 Engine / automation

Events OS adds **one new module** (`src/app/events_os.py`) and **two scheduled jobs**, all built on primitives already shipped. Nothing here is a new execution paradigm — it's the CRM intel/promise/sequence machine pointed at events.

### 6.1 Event capture (extends the CRM intel hook)

`crm_intel.process_email` already runs one Haiku call per email and creates/clusters deals. Events OS **extends that same call** (no second LLM round-trip) with an event branch: when the analysis tags an inbound as an event enquiry, `events_os.on_deal_is_event(deal_id, extracted)` creates the `crm_events` row and seeds `event_date`, `event_type`, `guest_count` from the extraction. The event-shaped custom fields (`Event date`, `Headcount`, `Dietary notes`) **already autofill** via the shipped `CRM_SEEDS["events"]` → `crm_fields.apply_extraction` path — Events OS just also mirrors the structured ones onto `crm_events` columns so the board query is cheap.

Guest + dietary extraction reuses the same confidence-gated pipeline: an inbound "we'll be 118, six vegans and a nut allergy on table 4" runs through the extraction, and `events_os.apply_guest_extraction` upserts guests + `dietary_tags` (dedup via `crm_upsert_guest`). High-confidence → applied; medium → a suggestion chip (`crm_insert_field_suggestion` pattern), exactly like custom fields.

### 6.2 The multi-stakeholder chase (built on promises + sequences)

This is the killer feature (§12). Mechanically:

- Each stakeholder's outstanding item is a **`crm_promises` row** (`direction='they_owe'`, `deal_id` = the event's deal), linked back via `crm_event_stakeholders.outstanding_promise_id`. The promise engine already extracts, dedups (§18.3) and auto-resolves (§18.4) these — so when the caterer replies "confirmed, we've got the vegan count," the promise auto-resolves and the stakeholder flips to `confirmed` with zero manual touch.
- Chasing is a **daily job**, `src/app/jobs/chase_stakeholders.py`, modelled exactly on `send_service_reminders.py`: for each Events OS tenant, `crm_stakeholders_to_chase()` returns pending stakeholders on upcoming events; for each, `agent.compose_outbound(cfg, CHASE_PURPOSE, facts)` drafts a nudge in the venue's voice → `db.insert_email_log(..., kind="event_chase")` → auto-send if `cfg.auto_send`, else a pending draft. `last_chased_at`/`chase_count` dedupe and back off (max 3 chases per item, then it surfaces to the manager as "needs a human" — same exhaustion contract as probe fields §18.2).
- The chase is **escalating and per-stakeholder**, which is what a coordinator does by hand: 4 days quiet → gentle nudge; 8 days → "we go to print on the run sheet Friday, can you confirm?"; still nothing → flag to manager. This is the gone-quiet-nudge cadence generalised to N parties per event.

For structured, template-driven outreach (e.g. a standard "final numbers request" a caterer gets for every event), the tenant can wire a **`crm_sequences` sequence** triggered on stage-enter-Planning — the shipped sequence engine (`crm_sequences.py`) already does branching, exit-on-reply, rate-limiting and voice-rewriting. Events OS seeds event-shaped default sequences (§9).

### 6.3 Run-of-show assembly (`run_daily`)

A daily job (`src/app/jobs/assemble_run_sheets.py`, scheduled via `scheduler.run_daily`) walks events entering the run-of-show window (e.g. 14 days out). For each: `events_os.assemble_run_of_show(event)` merges `timings` + supplier confirmations + staff call-times + dietary matrix into `run_of_show` JSON, flags **gaps** (a timing with no responsible stakeholder, an unconfirmed supplier, a missing final headcount), and drafts the sign-off request to the client via `compose_outbound`. The board card's "3 gaps flagged" reads straight off this.

### 6.4 Drafter integration (the through-line)

Every outbound above — quote, deposit chaser, stakeholder chase, run-sheet blast, post-event thank-you — follows the **one shipped outbound pattern**:

```
compose_outbound(cfg, PURPOSE, facts)  →  insert_email_log(kind=...)  →
  log_agent_event(...)  →  (cfg.auto_send ?  mailbox.send_mail + mark_email_log_sent : leave pending)
```

verbatim from `send_service_reminders._remind_one` and `crm_sequences._do_send_email`. Reply drafting (when a supplier/client emails *in*) is `agent.draft_reply` with `crm_context=crm_intel.build_crm_context(...)` — the event's date, headcount, dietaries, open promises and open stakeholder items all ride in the existing `<customer_context>` block. **No new drafting code**; Events OS just supplies richer facts to the same two functions.

Scheduler wiring in `src/app/main.py`: `run_daily` for the two daily jobs (mirroring the reminders job) and the existing 15-min `run_every` sequence sweep already covers event sequences.

---

## §7 API + routes

New router `src/app/events_routes.py`, prefix `/api/events`, `require_tenant` + `require_csrf` on writes, mirroring `crm_routes.py` conventions (RLS via `ctx.client`).

```
GET    /api/events                         — list (filter: status, from, to)
GET    /api/events/week?start=YYYY-MM-DD    — the daily-surface rollup (board)
GET    /api/events/{id}                     — event detail: deal + guests + stakeholders + run-of-show
PATCH  /api/events/{id}                     — edit date/type/timings/licence/contingency
POST   /api/events                          — manual create (usually auto-created from an enquiry)

GET    /api/events/{id}/guests              — guest list
POST   /api/events/{id}/guests              — add / bulk-import guests (CSV paste, v1)
PATCH  /api/events/{id}/guests/{guest_id}   — edit rsvp / dietary / party_size
GET    /api/events/{id}/dietary-matrix      — aggregated counts (drives caterer order)

GET    /api/events/{id}/stakeholders        — suppliers + staff with confirmation state
POST   /api/events/{id}/stakeholders        — add a supplier/staff member (opens the chase)
PATCH  /api/events/{id}/stakeholders/{sid}  — confirm / set outstanding / mark needs-info
POST   /api/events/{id}/stakeholders/{sid}/chase  — chase now (manual trigger of the auto-chase)

POST   /api/events/{id}/run-of-show/assemble — (re)assemble the run sheet on demand
GET    /api/events/{id}/run-of-show          — the assembled sheet + flagged gaps
POST   /api/events/{id}/run-of-show/send     — blast the final run sheet to all stakeholders
```

Stage moves, promises, tasks and sequences on an event go through the **existing** `/api/crm/*` endpoints (it's a deal) — Events OS deliberately does not re-implement them.

---

## §8 UI — pages + nav

Server-rendered Jinja2, matching the CRM/dashboard house style (`templates/crm/*`, `templates/app/*`).

- **`/app/events`** — the daily surface (§2): this-week board of event cards + next-4-weeks strip. The Events OS home.
- **`/app/events/{id}`** — event detail. Header = deal facts (client, value, stage) reusing the deal card partial. Tabs/sections: **Timings & run-of-show** (editable, with gap flags) · **Guests & dietary** (list + matrix, CSV paste import) · **Suppliers & staff** (stakeholder cards with the ✅/⚠/⏳ lights + "BLAM8 last chased 2d ago" + chase-now) · **Activity** (the deal's email feed, reused) · **Promises** (open items, reused CRM component).
- **Today card** on `/app` — added to `dashboard.app_home` alongside the CRM card: this week's events + what's being chased.
- **Nav.** Events OS gets its own left-nav entry (like TMS/FSM), lit only when the tenant's OS is `events` and the module is active. The `MODULES` registry entry (§10) renders its card on `/app/modules`.

Run-of-show export to PDF reuses the invoicing PDF path (`/app/invoices` already renders PDFs) — a run sheet is a document, same renderer.

---

## §9 Onboarding + industry seeds

Everything below the Config-vs-Code line (§3 of the OS doc) — no new engineering, just seeds and flags flipped.

1. **`operating_systems.py`** — flip `{"id": "events", ..., "live": True}` (currently `False`, line 32). This is the switch that makes Events OS selectable as a Shape.
2. **`onboarding.py` INDUSTRIES** — the `events` entry (line 52) already exists but is `hidden: True`. Remove `hidden` and set `live: True` so "Events, catering & venues" appears in the picker under the Events shape. The id stays `events` (stored tenants + lingo + intake all key off it).
3. **`industry_lingo.py`** — `LINGO["events"]` and `CRM_SEEDS["events"]` **already exist** (covers/headcount, dietary requirements, run sheets, staffing; event date + headcount + dietary notes as CRM fields). Extend `CRM_SEEDS["events"]` with the run-of-show-shaped hints if wanted (arrival/carriages timings, drink licence) — pure config edit.
4. **`onboarding.py` DATA_SOURCES** — add an `"events"` block: caterer POS (vendors: "Nutritics", "Caterease", "Other"), Stripe (deposits), booking/enquiry system (vendors: "Tripleseat", "Perfect Venue", "Other"), spreadsheet, calendar. Drives the "connect your systems" onboarding step + the roadmap signal.
5. **New tenants** onboard Shape → Industry → the existing custom-fields wizard (events seeds pre-loaded) → voice/knowledge. No new wizard step needed; the event-shaped fields ride the existing custom-fields flow.
6. **Default event pipeline** — `events_os.ensure_event_stages(tenant_id)` (analogue of `crm.ensure_default_stages`) seeds Enquiry → Quoted → Deposit → Planning → Run-of-show → Event day → Post-event → (Cancelled) on first event touch, if the tenant hasn't customised their stages.

---

## §10 Module registration + plan wiring

1. **`dashboard.py` MODULES** — add:
   ```python
   {"id": "events", "name": "Events OS", "group": "addon", "badge": "Events",
    "href": "/app/events",
    "blurb": "Run every event as one flow: guests, dietaries, suppliers and staff — "
             "and BLAM8 chases every stakeholder to the run sheet so you never have to."},
   ```
2. **`plans.py` INDUSTRY_MODULE_META** — add the `events` entry (switchboard key `"events"`), so the `+industry` tier renders "Core + Events OS" with its feature list (multi-stakeholder chase, guest & dietary tracking, run-of-show, supplier coordination). `INDUSTRY_MODULE` derives from it automatically; `MANAGED_MODULES` picks it up.
3. **`plans.py` INDUSTRY_TO_OS** is already `{"events": "events"}` in `operating_systems.py` — nothing to change there.
4. **Pricing tier.** Per the OS doc §9 recommendation: **Core (£99) covers the CRM + drafter + calendar that Events OS's coordination is built on**; the Events OS *module* (the board, guest/dietary tracking, run-of-show, the stakeholder chase engine) is the `+industry` upsell — slot it at the `professional` tier price (£299) alongside the other verticals, or price to inbound. The chase engine is the value-justifier: it replaces a part-time coordinator.

---

## §11 OS-native integrations

| Integration | Why | v1 | Later |
|---|---|---|---|
| **Stripe (deposits)** | Deposit is a hard stage gate; payment detection auto-advances Deposit→Planning | ✅ v1 — reuse the Phase 5.1 payment-detection pattern (`docs/architecture/CRM_design.md §22`): LLM scans inbox for Stripe receipts → matches deposit → advances stage. Direct Stripe webhook is the clean version. | Stripe Checkout link generated + sent with the deposit request |
| **Caterer POS** (Nutritics, Caterease) | Push the dietary matrix + final numbers as the catering order | later (v1.1) — v1 sends the caterer the matrix by email (they're a stakeholder the chase already emails); a real POS push needs per-vendor API work | Nutritics/Caterease order push |
| **Venue capacity systems** | Large multi-room venues need atomic room-availability | later — single-venue wedge (the target) needs only the per-date uniqueness check (§4). Multi-room reuses `scheduling.py`'s atomic hold RPC | Tripleseat/Perfect Venue two-way sync |
| **Mailchimp (invitations)** | Large events blast invites + collect RSVPs | later — v1 handles RSVPs that arrive by email (extraction → guest upsert); Mailchimp is for >100-guest invite campaigns | Mailchimp audience sync from the guest list; RSVP webhook → guest upsert |

The pattern across all of these: **email is the v1 integration**. A supplier, a caterer, a guest are all just parties BLAM8 already emails and reads — the chase engine and extraction cover them today; the vendor APIs are optimisations for scale, not prerequisites. This is the whole-inbox wedge again.

---

## §12 Killer feature deep-dive — multi-touch coordination

**The claim:** BLAM8 chases every stakeholder on every event to the run sheet, so the venue manager stops being the human message-bus. Concretely, for the Harper–Singh wedding:

```
                        ┌─────────────────────────────────────────┐
                        │  EVENT: Harper–Singh · Sat 12 Jul · 118  │
                        └─────────────────────────────────────────┘
   they_owe promises (one per outstanding item), all on the event's deal:
   ┌───────────────┬────────────────┬───────────────┬────────────────┬──────────────┐
   │ Caterer       │ Bar            │ Florist       │ Waiting staff  │ Client       │
   │ final numbers │ drink licence  │ delivery time │ call-time       │ final head-  │
   │ ✅ resolved   │ ⚠ needs info   │ ⏳ chasing (2)│ ⏳ chasing (1)  │ count ⏳     │
   └───────────────┴────────────────┴───────────────┴────────────────┴──────────────┘
        each is a crm_promises row → each is auto-chased → each auto-resolves on reply
```

**LLM mechanics, built entirely on shipped primitives:**

1. **Detecting the outstanding item.** When a supplier is added (or first emails in), the intel pass (`crm_intel.analyze`) reads what *we* owe them and what *they* owe us — it already does this for every email in both directions. A stakeholder's `outstanding` is a `they_owe` promise; the same prompt that logs "they owe us the signed NDA" logs "the bar owes us the drink-licence confirmation."

2. **Tracking without duplication.** Promise dedup (§18.3) already passes open promises into the extraction call so restatements reinforce rather than stack. Across a 6-week planning conversation with the florist, "still need your delivery window" stays *one* tracked item with a `mention_count`, not twelve.

3. **Chasing each party.** The daily `chase_stakeholders` job (§6.2) walks `crm_stakeholders_to_chase()` and, per open item, drafts a voice-matched nudge via `compose_outbound` — escalating by `chase_count`, backing off after 3, then flagging to the manager. Because it's per-stakeholder, five parties on one event get five appropriately-different chases, none of which the manager wrote.

4. **Auto-resolving on follow-through.** Promise auto-resolution (§18.4) already flips a promise to `resolved` when a later email fulfils it. The caterer replying "we're set for 118 incl. the 6 vegan" auto-resolves the item, flips the stakeholder to `confirmed`, and clears the ⏳ on the board — no click. The manager sees the light turn green, not a to-do.

5. **Closing the gaps.** Run-of-show assembly (§6.3) reads which promises are still open and surfaces them as run-sheet gaps — so "unconfirmed: florist delivery time" appears on the sheet until the promise resolves.

The entire feature is **promise-tracking + the outbound-compose pattern + the gone-quiet cadence**, generalised from one counterparty (the CRM's customer) to N counterparties per event, with a thin `crm_event_stakeholders` table giving each party a first-class identity + confirmation state. That reuse is the reason this ships in weeks, not months — the hard parts (extraction, dedup, resolution, voice-matched outbound, scheduler) are already in production and proven on the Pearson Plumbing run (`docs/architecture/CRM_design.md §17`).

---

## §13 Build order

Each phase independently shippable; estimates assume single-dev pace with LLM assistance, on top of the shipped CRM platform.

- **Phase A — Primitive + surface (≈1 week).** Migration (`scripts/db/migrations/apply_events_migration.py`, idempotent, mirrors 4b) · `crm_event_*` db layer · `events_os.on_deal_is_event` capture hook (extends the intel call) · `/app/events` board + `/app/events/{id}` detail · `MODULES` + `plans.py` + onboarding flags flipped. **Ships a working, self-filling events board** — enquiries become events with dates/headcount/dietaries, zero data entry. This alone is demo-able.
- **Phase B — The chase engine (≈1–1.5 weeks).** `crm_event_stakeholders` wired to promises · `chase_stakeholders` daily job (clone of `send_service_reminders`) · escalation/backoff/exhaustion · stakeholder UI with the status lights + chase-now. **Ships the killer feature.**
- **Phase C — Run-of-show + post-event (≈1 week).** `assemble_run_sheets` daily job · run-of-show UI + gap flags + PDF export (reuse invoicing renderer) · run-sheet blast · post-event thank-you/feedback sequence seeds.
- **Phase D — Integrations (≈1 week each, demand-led).** Stripe deposit auto-advance (reuse Phase 5.1) · Mailchimp RSVP sync · caterer POS push · multi-room capacity (reuse scheduling RPC).

**Total to killer-feature demo (A + B): ~2.5 weeks.** Consistent with the OS doc's "~4–6 weeks per OS" once C + D land. The heavy reuse (§15) is what compresses it.

---

## §14 Open questions + risks

- **Guests are not contacts, but RSVPs arrive as emails.** When a guest emails their dietary in, we upsert a `crm_event_guests` row — but should that guest also become a `crm_contact`? Recommendation: **no** by default (guests aren't the venue's customers; 118 contacts per wedding pollutes the CRM). Extraction routes guest info to the guest table via the event context; only the *booker* is a contact. Revisit if venues want to remarket to guests.
- **Which event does an inbound belong to?** A wedding planner runs many concurrent events for different clients; the deal-clustering already scopes by contact, but a supplier (one caterer) works *across* events. Chase replies must resolve the right event's promise — key the promise by `deal_id` and match the supplier's reply to the event it references (subject/thread + LLM disambiguation). Risk: a supplier's terse "confirmed" with no event reference. Mitigation: chase emails carry a per-event reference the reply quotes (like `booking_ref`).
- **Chase tone + frequency.** Over-chasing a supplier the venue has a relationship with is worse than under-chasing. Backoff (max 3) + `auto_send` gating (draft-first unless full-auto) + a per-stakeholder "don't chase" flag mitigate. Default draft-first for the chase engine even for auto-send tenants, until trust is earned.
- **Capacity for multi-room venues.** Deferred (§4/§11); confirm the single-venue wedge doesn't hit it before v1.1.
- **Run-of-show is a document with real liability** (wrong carriage time = missed taxis). Assembly must flag gaps loudly and never fabricate a timing — the LLM assembles from stated facts only, gaps stay gaps. Same "do NOT invent dates" guardrail already in `compose_outbound` briefs.

---

## §15 Reuse map

| Part | REUSE (shipped) | BUILD NEW |
|---|---|---|
| Client relationship / deal | `crm_deals`, `crm_companies`, `crm_contacts`, `crm_activities`, stages, pipeline (`crm.py`, `crm_routes.py`) | — |
| Event as a project | the deal, extended | `crm_events` (1:1 with deal) |
| Self-filling fields (date/headcount/dietary) | `crm_fields.py` extraction + `CRM_SEEDS["events"]` (already seeded) | mirror structured fields onto `crm_events` columns |
| Guest list + dietary matrix | the extraction pipeline + confidence gate | `crm_event_guests` + `crm_dietary_matrix` rollup |
| Stakeholder chase — detection | `crm_intel.analyze` (they-owe promise extraction) | — |
| Stakeholder chase — dedup | promise semantic dedup (§18.3) | — |
| Stakeholder chase — resolution | promise auto-resolve (§18.4) | — |
| Stakeholder chase — outreach | `agent.compose_outbound` + `insert_email_log` + `log_agent_event` + auto-send (the `send_service_reminders`/`crm_sequences` pattern) | `src/app/jobs/chase_stakeholders.py` (a clone), `crm_event_stakeholders` table |
| Structured multi-touch (final-numbers, etc.) | `crm_sequences.py` engine (branching, exit-on-reply, rate-limit, voice-rewrite) | event-shaped seed sequences |
| Run-of-show assembly | `scheduler.run_daily` + `compose_outbound` | `src/app/jobs/assemble_run_sheets.py`, `events_os.assemble_run_of_show` |
| Drafter (replies + outbound) in tenant voice | `agent.draft_reply` (+`crm_context`), `agent.compose_outbound`, `crm_intel.build_crm_context` | richer facts only — no drafting code |
| Deposit / payment detection | Phase 5.1 payment-detection (`docs/architecture/CRM_design.md §22`) | Stripe wiring (v1) |
| Capacity (multi-room) | `scheduling.py` atomic hold RPC | per-room resource key (v1.1) |
| Scheduler | `run_daily` / `run_every` (`scheduler.py`) + `main.py` wiring | two `run_daily` registrations |
| RLS / multi-tenant | `auth_tenant_ids()` policy pattern | policies on 3 new tables |
| Migration | psycopg2 idempotent convention (`scripts/db/migrations/apply_crm_phase4b_migration.py`) | `scripts/db/migrations/apply_events_migration.py` |
| Onboarding / OS / plan wiring | `operating_systems.py`, `onboarding.py`, `industry_lingo.py`, `plans.py`, `dashboard.py` MODULES | flip flags + add registry/meta entries |
| Board UI + detail | CRM/dashboard Jinja2 house style, deal-card + promise partials, invoicing PDF renderer | `templates/events/*` (board, detail, run-of-show) |
| API | `crm_routes.py` route conventions (RLS, CSRF) | `src/app/events_routes.py` |

**Net new code:** one module (`src/app/events_os.py`), one router (`src/app/events_routes.py`), two jobs (clones), three tables, a handful of templates, and config-layer flags. Everything hard is reused.
