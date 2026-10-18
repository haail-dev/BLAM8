# BLAM8 Practice OS — Design & Build Spec

> **Status (2026-09-27, confirmed by the clean-up pass):** BUILT, P0–P4 (app/practice_*.py, jobs/send_recall.py, jobs/send_appointment_reminders.py; tests/test_practice.py), deployed. Live proof: none.

**Status:** 🔨 Not built. Spec only. This is the third OS after Field (shipped, via TMS) and Storefront.
**Author:** Julius Deane, w/ Claude synthesis
**Last updated:** 2026-07-05
**Companion docs:** `docs/BLAM8_operating_systems.md` §4.3 (the OS entry), `docs/architecture/CRM_design.md` (the format/depth template + the CRM that Practice OS rides on), `docs/modules/module-scheduling.md` + `docs/architecture/unified-slot-grid.md` (the diary primitive this reuses), `docs/modules/module-field-service.md` D5 (the recurring-reminder pattern the recall engine is modelled on).

> **Read this first.** Practice OS is *not* a new booking engine. BLAM8 already has an appointment primitive: the scheduling diary (`src/app/scheduling.py`, `src/app/slots.py`, `src/app/slotgrid.py`), the `bookings` table with atomic per-window capacity (`db.hold_booking_if_capacity`), per-service durations (`src/app/services.py`, `service_availability`), the day-before reminder job (`src/app/jobs/send_reminders.py`), and a forward-dated recurring-service reminder (`src/app/jobs/send_service_reminders.py`) that is *already 90% of recall*. Practice OS is the recall cadence, the no-show scorer, and the patient-history field library layered on top of that spine, plus an industry seed set (dentists/salons/physios/…) and a daily-rhythm surface. Everything genuinely new is called out in the **§15 reuse map**. If a section doesn't say "NEW", assume it's reuse.

---

## §0 Positioning

**Shape.** "The customer visits by appointment for time or expertise." A practice sells slots of a practitioner's time. The relationship is *recurring by clinical/commercial cadence* — a dental check-up every 6 months, a hygienist every 3, a physio block of 6, a haircut every 5 weeks — and the single biggest revenue lever is bringing people back exactly when they're due. The second biggest is not losing the slot to a no-show.

**Wedge (dentists).** "Your recall goes out to every patient exactly when they're due, phrased in your voice, and the ones most likely to no-show get an extra reminder." Neither SOE/Software of Excellence nor Cliniko drafts recall *in the practitioner's own voice from the practice's own inbox*, and none of them score no-show risk from booking pattern + reply latency + prior history. This is the whole-inbox + UK wedge (per `MEMORY.md` pricing-and-competitive) applied to the biggest single-vertical market BLAM8 addresses.

**Target industries (~250K UK SMBs).** Dentists (~12K practices), GPs (private/mixed), physios (~15K), chiropractors, osteopaths, hairdressers/salons (~40K), barbers, beauticians, therapists/counsellors, tutors, coaches, personal trainers, veterinary practices, private clinics, aesthetic/podiatry/optometry. All share the `appointment` primitive; they differ only in vocabulary + recall cadence + which fields matter — i.e. the config layer.

**Build state.** Not built. The diary primitive, per-service durations, reminders, the self-filling CRM and the drafter are all shipped and reused wholesale. NEW code: recall scheduler, no-show scorer, the 48/24/2h reminder cycle, patient-history fields (incl. encrypted medical notes), the Practice daily surface, and the industry seeds.

**Pricing.** Core (£99) already includes the scheduling diary + CRM. Practice OS ships as a per-OS add-on module (`practice`) at **£149/mo**, consistent with the OS doc §9 recommendation (Core covers CRM+drafter+calendar; each OS's specific engine is a £150–300 add-on). It is *cheaper* than Field OS · Transport (£299) because it adds one engine (recall + no-show) rather than a whole fleet/routing stack. See §10.

---

## §1 The primitive — `appointment` vs the existing booking/diary

**The appointment IS the existing `bookings` row.** An appointment is a `bookings` row created through the exact same path a scheduling tenant already uses:

- **Time slot** — `bookings.booking_date` + `bookings.window` (a slot-mode `'HH:MM'`), dual-written onto the unified 15-min grid as `start_block`/`length_blocks` (`slotgrid`). Practice tenants run the diary in **`windows='slots'`** mode (`src/app/scheduling.py` `_slot_windows`) with `service_types` defined, so a 30-min check-up occupies 2 blocks and a 60-min new-patient exam occupies 4 — exactly the per-service-duration diary `src/app/services.py` already drives.
- **Service type** — `bookings.summary` + the resolved `services.find()` entry (name, minutes, price, optional `resource`). This is untouched: a dental practice defines `["Check-up" 20min, "Hygiene" 30min, "New patient" 45min, "Emergency" 15min]` in `scheduling_config.service_types` today.
- **Practitioner** — the `resource` dimension **already exists** on the per-service diary: `services` rows carry a `resource` (a named capacity pool / skill), and `service_availability(..., resource=...)` counts only that pool's bookings, with `capacity` overriding `capacity_per_window`. So "Dr Amara" / "Priya (hygienist)" / "Chair 2" map onto **resource types** with capacity 1 each. No new practitioner table for v1 — a practitioner is a resource string, seeded from a NEW `scheduling_config.practitioners` config list (names + optional per-day capacity), the twin of `service_types`. (Justification for deferring a table: the fleet module proved the "named pool as a config list, not a schema table" pattern works for vehicles-lite; practitioners v1 follows it. A real `practitioners` table with rota/holidays is a v2 item — §13.)
- **Client** — `bookings.contact` (email) + `bookings.details`, and the **CRM contact** keyed by that email. The client history summary, insurance, allergies etc. live as CRM custom fields on the contact/company (§4), self-filled by the existing extraction pipeline.

**What is genuinely NEW on top of the primitive (all additive):**

1. **Recall cadence** — a `bookings.recall_due_date` + `recall_interval_months` + `recall_reminder_queued_at`, the *direct analogue* of the FSM `service_due_date` / `service_reminder_queued_at` / `service_months` trio that already drives recurring-service reminders (`db.complete_job_and_schedule_service`, `db.list_jobs_due_service`, `db.mark_service_reminded`). On appointment completion we stamp `recall_due_date = completed_date + service.recall_months`; a forward-dated job chases it.
2. **No-show risk score** — a NEW `bookings.no_show_score` (0–100) + `no_show_band` + reason, computed by an LLM scorer (§12) from booking pattern, reply latency and prior no-show history. Drives the *extra* reminder in the cycle.
3. **Patient-history fields** — CRM custom fields (client history summary, insurance provider, treatment codes, standard recall cadence, allergies/medical notes, preferred contact method). Medical notes are **encrypted at rest** (§4) — the one place Practice OS departs from plain `custom_data`.
4. **The reminder *cycle*** — 48h/24h/2h before the appointment, vs the single day-before reminder that ships today. This is a small generalisation of `send_reminders.py` (multiple lead times + dedupe per-stage), not a rewrite (§6).

No new booking/capacity/availability code. A Practice tenant's appointment is a `bookings` row; the diary, the atomic hold, the grid math and the calendar all work unchanged.

---

## §2 Daily-rhythm surface — the Practice Today board

Route `/app/practice` (gated on the `practice` module, §7). Server-rendered Jinja2, same design system as `/app/diary` and the CRM. Three stacked cards, read entirely from existing + the new columns — no new aggregation store:

**1. Today's appointments.** The slot-mode day view for `today` (reuse `scheduling.diary_days` filtered to one date, or `service_availability`'s occupancy chart `_day_chart`). Each row: time · service · practitioner (resource) · client name (from CRM contact) · a **no-show chip** (green/amber/red from `no_show_band`) · a one-tap "Confirmed?" state (from the reminder cycle: has the client replied/confirmed). Clicking a row opens the appointment detail with the patient-history summary (§4) inline — so the practitioner sees allergies/notes/last-visit *before* the patient sits down. This is the "patient-history-aware" surface the killer feature promises, rendered for the human as well as injected into the drafter.

**2. No-show risk panel.** Today's + tomorrow's appointments sorted by `no_show_score` desc, red band first. Each shows the score, the top reason ("2 prior no-shows, booked 6 weeks out, hasn't opened last 2 reminders"), and an **[Extra reminder →]** action that queues an additional drafted nudge (§12). This panel is where the wedge lives visibly.

**3. Recall queue.** Clients whose `recall_due_date` falls in a window (default: due within the next 14 days, plus overdue), grouped by service ("Check-ups due", "Hygiene due"). Each: client · service · due date · last seen · a **[Draft recall →]** action (or auto-drafted already, pending in the approval queue). Count badge on the nav. This is the recall engine's human face; the engine itself (§6) drafts proactively, this is the review/trigger surface.

Everything on this board is derived, not stored: no-show scores are computed on a schedule and cached on the booking row; the recall queue is a `bookings` query on `recall_due_date`; "confirmed?" reads the reminder cycle state. The board adds no tables.

---

## §3 Workflow state machine

The lifecycle, and the drafter/composer touchpoint at each transition. Reused pieces named in **bold**; new logic marked NEW.

```
                        ┌─────────────────────────────────────────────┐
   booking request ────▶│ (inbound email → agent.draft_reply)         │
   (email/whatsapp)     │  drafter checks the diary, offers slots,      │
                        │  places a hold  → status: proposed            │  REUSE: draft_reply + SCHEDULING_TOOLS
                        └───────────────────┬─────────────────────────┘
                                            ▼
        human approves ────────────▶ status: confirmed                    REUSE: dashboard confirm action
                                            │  (calendar event pushed)     REUSE: calendar_sync
                                            ▼
              ┌───────────── reminder cycle (NEW: 48h / 24h / 2h) ──────────────┐
              │  each lead time: compose_outbound in tenant voice, pending draft │  REUSE: compose_outbound + insert_email_log
              │  or auto-sent (auto_send). The 24h + 2h touch also asks the      │  NEW: 3 lead times + per-stage dedupe
              │  client to confirm; a reply flips "confirmed?" true.             │
              │  HIGH no_show_band → an EXTRA reminder is injected (§12).        │  NEW: no-show-gated extra touch
              └───────────────────────────────┬─────────────────────────────────┘
                                               ▼
                              appointment happens (in person)
                                               │
                    human marks outcome on the Today board:
                        ┌──────────────┬───────────────┐
                        ▼              ▼               ▼
                    completed      no_show         cancelled            NEW: outcome buttons → set_booking_status + record
                        │              │
                        │              └─▶ record no-show on the CRM contact       NEW: feeds the no-show scorer's history
                        ▼                  (crm custom field no_show_count++)
        completion → follow-up drafter (NEW touchpoint):                REUSE: compose_outbound
          "great to see you, here's aftercare / next steps"
                        │
                        ▼
        recall stamp (NEW): recall_due_date = completed + service.recall_months   ANALOGUE of complete_job_and_schedule_service
                        │
                        ▼   … 3 / 6 / 12 months later …
        recall drafter (NEW): compose_outbound "time for your check-up"           REUSE: compose_outbound; NEW: recall job
          in the practitioner's voice → pending draft or auto-sent
                        │
                        ▼
                back to "booking request" (the loop closes)
```

**Touchpoint summary (every arrow that produces a message reuses the existing composer):**

| Transition | Producer | Reuse / New |
|---|---|---|
| request → proposed | `agent.draft_reply` + slot tools | REUSE (`src/app/pipeline.py`) |
| proposed → confirmed | human confirm on diary | REUSE (`dashboard` confirm) |
| confirmed → reminder ×3 | `agent.compose_outbound` per lead time | NEW job, REUSE composer |
| HIGH no-show → extra reminder | `agent.compose_outbound` | NEW gate, REUSE composer |
| appointment → completed/no_show | human outcome button | NEW route, REUSE `set_booking_status` |
| completed → follow-up | `agent.compose_outbound` | NEW trigger, REUSE composer |
| completed → recall stamp | `db.complete_appointment_and_schedule_recall` | NEW fn (twin of FSM's) |
| recall due → recall draft | `agent.compose_outbound` | NEW job, REUSE composer |

The reminder cycle and recall could alternatively be expressed as **CRM sequences** (`src/app/crm_sequences.py`) — a per-contact enrollment with time-delayed `send_email` steps. That engine already does "wait N days → compose_outbound in voice → exit on reply → rate-limit 1/day". For v1 we build recall + reminders as dedicated forward-dated jobs (mirroring `send_service_reminders.py`) because they key off `bookings` dates, not enrollment dates, and need the no-show gate; the *sequences engine stays the mechanism for open-ended re-engagement* (a lapsed patient who never rebooked). See §6 for the split rationale.

---

## §4 Schema — additive tables/columns

All additive. RLS on every new table via the house policy (`tenant_id IN (SELECT auth_tenant_ids())`). Indexes mirror the FSM/CRM conventions. Reuses `bookings`, `crm_contacts`, `crm_companies`, `scheduling_config` where apt.

### 4.1 `bookings` — additive columns (the appointment gains recall + no-show)

```sql
-- Practice OS: recall cadence (the FSM service_due_date twin) + no-show risk.
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS recall_due_date          date;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS recall_interval_months   integer;   -- copied from the service at completion
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS recall_reminder_queued_at timestamptz;  -- dedupe, ≤1 recall per cycle
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS recall_of_booking_id     uuid REFERENCES bookings(id) ON DELETE SET NULL; -- the appt this recall chased
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS practitioner             text;      -- resource pool name (denormalised for the board)
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS no_show_score            integer;   -- 0..100, scorer output
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS no_show_band             text;      -- low | medium | high
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS no_show_reason           text;      -- one-line human explanation
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS no_show_scored_at        timestamptz;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS extra_reminder_queued_at timestamptz;  -- dedupe the high-risk extra touch
-- The 48/24/2h cycle needs a per-stage sent stamp. jsonb keeps it additive (no 3 columns):
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS reminder_stages_sent     jsonb NOT NULL DEFAULT '{}'::jsonb;
  -- e.g. {"48h":"2026-07-05T09:00Z","24h":"...","2h":"..."} — presence = that stage queued.

CREATE INDEX IF NOT EXISTS idx_bookings_recall_due
  ON bookings (tenant_id, recall_due_date)
  WHERE recall_due_date IS NOT NULL AND recall_reminder_queued_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_bookings_noshow_scan
  ON bookings (tenant_id, booking_date, status);
```

`bookings` already has RLS. These columns inherit it. The `recall_*` set is deliberately the *shape* of the shipped FSM `service_*` set so the two reminder jobs are siblings.

### 4.2 `scheduling_config` — additive config (practitioners + recall defaults)

`scheduling_config.service_types` (jsonb) already carries `[{name, minutes, price, resource, buffer}]`. Practice OS adds two optional keys per service and one config list — **no schema change**, just richer jsonb the tenant's settings form writes (like `service_types` today):

```
scheduling_config.service_types[i].recall_months : int | null   -- standard recall cadence for this service (6 for a check-up)
scheduling_config.practitioners : [ {name, resource, capacity_per_day} ]   -- the resource pools (NEW config list, twin of service_types)
scheduling_config.reminder_cycle : {leads_hours:[48,24,2], ask_confirm:true}  -- overridable; default the 3-touch cycle
```

`src/app/practice_config.py` cleans the stored list and supplies each service's recall cadence. The list is not injected into the diary prompt and does not establish named-person availability; individual working hours and leave are not modelled.

### 4.3 Patient-history fields — CRM custom fields, NOT a new table

The field library (client history summary, insurance provider, treatment codes, recall cadence, allergies, preferred contact method) is **CRM `custom_field_definitions` + `custom_data`**, self-filled by the shipped extraction pipeline (`src/app/crm_extract.py` + `src/app/crm_fields.py`). They are seeded per-industry from `industry_lingo.CRM_SEEDS` (§9). No new table — this is the whole point of the "CRM that fills itself in": a patient's insurer, stated allergies and "coming in for a filling" land from the email body automatically.

**Exception — encrypted medical notes.** Clinical/medical notes and allergies are special-category data (UK GDPR Art. 9). They must not sit in plaintext `custom_data`. NEW table, encrypted at rest:

```sql
CREATE TABLE IF NOT EXISTS practice_medical_notes (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  contact_id      uuid NOT NULL REFERENCES crm_contacts(id) ON DELETE CASCADE,
  note_ciphertext bytea NOT NULL,          -- encrypted with the tenant token-encryption key (app/crypto)
  note_kind       text NOT NULL DEFAULT 'note',   -- note | allergy | medication | condition
  source          text,                    -- manual | extracted
  source_email_id text,                    -- email_log id when auto-extracted
  created_by      text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_practice_med_notes_contact
  ON practice_medical_notes (tenant_id, contact_id);

ALTER TABLE practice_medical_notes ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "tenant isolation - practice_medical_notes" ON practice_medical_notes;
CREATE POLICY "tenant isolation - practice_medical_notes" ON practice_medical_notes
  FOR ALL USING (tenant_id IN (SELECT auth_tenant_ids()))
  WITH CHECK (tenant_id IN (SELECT auth_tenant_ids()));
```

Encryption reuses the existing symmetric envelope BLAM8 already uses for mailbox OAuth tokens (`src/app/crypto.py` / the `TOKEN_ENCRYPTION_KEY` pattern — see `MEMORY.md` local-token-encryption-key-mismatch). `encrypt()`/`decrypt()` wrap `note_ciphertext`. Notes are decrypted only inside `/app/practice` for a logged-in tenant user and inside the drafter's history-summary build — never returned raw over an API, never logged, never sent to the LLM extraction call in bulk (only the derived, non-clinical summary rides into the drafter — see §12 privacy note).

### 4.4 No new appointment/practitioner/recall tables

Deliberately: recall + no-show are *columns on `bookings`*, practitioners are a *config list*, patient history is *CRM fields*. The only new table is `practice_medical_notes`, justified solely by the encryption requirement. This keeps Practice OS additive and lets the shipped diary/CRM/calendar code operate on Practice tenants unchanged.

---

## §5 DB layer — `db.py`-style function signatures

New functions in `src/app/db.py` (Supabase client, `tenant_id` on every call, service-role variant for the sweeps). Named to sit beside the FSM twins they mirror.

```python
# ── recall (the FSM complete_job_and_schedule_service twin) ──────────────────
def complete_appointment_and_schedule_recall(
        booking_id: str, tenant_id: str, outcome: str = "completed",
        client: Client | None = None) -> str | None:
    """Set the appointment's status (completed | no_show), and — if its service has a
    recall_months cadence — stamp recall_due_date = booking_date + recall_months and copy
    recall_interval_months. Returns the recall due date (iso) or None. Twin of
    complete_job_and_schedule_service; reuses _add_months(). On outcome='no_show' also
    bumps the contact's no_show history (crm custom field / activity) for the scorer."""

def list_appointments_due_recall(tenant_id: str, cutoff_iso: str,
                                 client: Client | None = None) -> list[dict]:
    """Completed appointments whose recall_due_date <= cutoff and recall_reminder_queued_at
    is null, with a client email. The recall work set — sibling of list_jobs_due_service."""

def mark_recall_reminded(booking_id: str, tenant_id: str, client=None) -> None:
    """Stamp recall_reminder_queued_at (dedupe — ≤1 recall per cycle). Twin of
    mark_service_reminded."""

# ── the 48/24/2h reminder cycle ──────────────────────────────────────────────
def list_appointments_for_reminder_stage(tenant_id: str, stage: str,
        window_start_iso: str, window_end_iso: str, client=None) -> list[dict]:
    """CONFIRMED appointments whose start falls in [window_start,window_end] and whose
    reminder_stages_sent lacks `stage` (e.g. '24h'). The multi-stage generalisation of
    list_bookings_for_reminder (which is day-before only)."""

def mark_reminder_stage_sent(booking_id: str, stage: str, tenant_id: str, client=None) -> None:
    """Merge {stage: now()} into reminder_stages_sent (jsonb) — dedupe per stage."""

# ── no-show scoring ──────────────────────────────────────────────────────────
def list_appointments_to_score(tenant_id: str, horizon_days: int = 2, client=None) -> list[dict]:
    """CONFIRMED appointments starting within horizon_days, not scored since their last
    reminder/reply changed (no_show_scored_at stale). The scorer's work set."""

def save_no_show_score(booking_id: str, tenant_id: str, score: int, band: str,
                       reason: str, client=None) -> None:
    """Persist no_show_score/band/reason/no_show_scored_at on the booking."""

def mark_extra_reminder_queued(booking_id: str, tenant_id: str, client=None) -> None:
    """Dedupe the high-risk extra reminder (extra_reminder_queued_at)."""

def contact_no_show_history(tenant_id: str, email: str, client=None) -> dict:
    """Prior-behaviour signal for the scorer: {total_appts, no_shows, last_no_show_at,
    avg_reply_latency_hours, reminders_ignored}. Computed from bookings + email_log for
    this contact. Cheap aggregate, no new table."""

# ── practitioners (config-list resource pools, no table) ─────────────────────
def practice_board(tenant_id: str, client=None, now=None) -> dict:
    """Everything /app/practice renders: today's appointments (with no_show chips +
    patient summaries), the risk panel (today+tomorrow by score), and the recall queue.
    Composed from list_active_bookings + the new columns + crm contact lookups + the
    medical-notes summary. Pure-ish view-model, mirrors scheduling.diary()/fleet_board()."""

# ── encrypted medical notes ──────────────────────────────────────────────────
def list_medical_notes(tenant_id: str, contact_id: str, client=None) -> list[dict]:
    """Decrypt (app.crypto) and return this contact's notes, newest first. Tenant-user
    context only — never called from a public/portal route."""

def add_medical_note(tenant_id: str, contact_id: str, text: str, kind: str = "note",
                     source: str = "manual", source_email_id: str | None = None,
                     created_by: str | None = None, client=None) -> dict:
    """Encrypt `text` and insert. Returns the row WITHOUT ciphertext."""

def patient_history_summary(tenant_id: str, contact_id: str, client=None) -> str:
    """A short, drafter-safe summary of who this patient is: CRM fields (insurer, recall
    cadence, last visit) PLUS a redacted note line ('has recorded allergies — see notes')
    but NOT the raw clinical text. Feeds crm_intel.build_crm_context and the board."""
```

`_add_months` (already in `db.py:2146`) is reused verbatim by the recall stamp. `list_active_bookings`, `get_scheduling_config`, `bookings_for_contact`, `set_booking_status`, `hold_booking_if_capacity`, `crm_contact_by_email`, `crm_merge_custom_data`, `insert_email_log`, `mark_email_log_sent`, `log_agent_event` are all reused unchanged.

---

## §6 Engine / automation

Four automations, all on the **in-process scheduler** (`src/app/scheduler.py`) already used for reminders and the CRM sequence sweep — no new Render cron. Registered in `src/app/main.py` alongside the existing `run_daily`/`run_every` loops, gated so they only touch `practice`-module tenants (`db.tenants_with_modules(("practice",))`, the pattern `send_reminders.py` uses for `("scheduling","tms")`).

### 6.1 Recall scheduler — `src/app/jobs/send_recall.py` (NEW, twin of `send_service_reminders.py`)

Daily (`run_daily`, ~08:00 UTC). For each practice tenant: `list_appointments_due_recall(tenant_id, cutoff = today + RECALL_LEAD_DAYS)`; for each, build the recall facts (service, months since last visit, practitioner), `agent.compose_outbound(cfg, RECALL_PURPOSE, facts)` in the tenant's voice, `insert_email_log(status="pending", kind="recall")`, `mark_recall_reminded` (dedupe before send), `log_agent_event`, and auto-send if `cfg["auto_send"]`. Byte-for-byte the structure of `send_service_reminders._remind_one` — the only differences are the work-set query and the purpose string:

```python
RECALL_PURPOSE = ("A warm nudge that the patient is due their routine appointment again "
                  "(for example a 6-month dental check-up or a hygiene visit). Invite them "
                  "to rebook; do NOT promise or suggest a specific date or time — keep it "
                  "short, personal and in the practice's voice. If they have a usual "
                  "practitioner, mention them by name.")
```

`RECALL_LEAD_DAYS = 14` (chase two weeks before due, plus overdue). The composer already inherits tenant voice + config; recall goes out *in the practitioner's voice* for free.

### 6.2 Reminder cycle — `src/app/jobs/send_appointment_reminders.py` (NEW, generalises `send_reminders.py`)

Runs `run_every(30*60, …)` (every 30 min, so 2h reminders land on time) — the same interval mechanism the sequence sweep uses. For each stage in the tenant's `reminder_cycle.leads_hours` (default `[48,24,2]`):

- Compute the target window `[now+lead, now+lead+interval]` in `DIARY_TZ`.
- `list_appointments_for_reminder_stage(tenant_id, f"{lead}h", …)` → CONFIRMED appts starting in that window without this stage sent.
- `compose_outbound(cfg, REMINDER_PURPOSE[stage], facts)`; the 24h + 2h stages append "reply to confirm you'll be there" when `ask_confirm`. `insert_email_log(kind="reminder")`, `mark_reminder_stage_sent(booking_id, "24h")`, auto-send per `auto_send`.

This is a generalisation of the shipped day-before job (which is effectively the `24h` stage) — same compose/log/dedupe/auto-send skeleton, now parameterised by lead time with a per-stage jsonb dedupe instead of the single `reminder_queued_at` boolean. The office-vs-transport `_facts_for` split is dropped (Practice appointments are always office rows).

### 6.3 No-show scorer — `src/app/jobs/score_no_shows.py` (NEW)

Runs `run_every(60*60, …)` (hourly) and also on demand when a reminder is sent (reply latency changed). For each practice tenant: `list_appointments_to_score(tenant_id, horizon_days=2)`; for each, gather features via `contact_no_show_history` + the booking's own pattern, call the LLM scorer (§12), `save_no_show_score`. Then, if `band == "high"` and `extra_reminder_queued_at` is null and we're inside the extra-reminder window (e.g. 3–6h before), compose + queue the **extra reminder** (`compose_outbound`, `kind="reminder"`, `mark_extra_reminder_queued`). Isolated per booking in try/except so one failure never breaks the sweep (the house rule in every job).

### 6.4 Follow-up drafter — hooked on the outcome action (NEW trigger, no new loop)

When the human marks an appointment **completed** on the Today board (§7 `POST /app/practice/appointments/{id}/outcome`), fire a best-effort follow-up: `complete_appointment_and_schedule_recall` (stamps the recall) then `compose_outbound(cfg, FOLLOWUP_PURPOSE, facts)` → pending draft (or auto-send). No scheduled job needed — it's synchronous with the outcome click, like the CRM's on-write hooks.

**Why dedicated jobs, not the CRM sequences engine, for recall + reminders.** Recall and the 48/24/2h cycle key off **`bookings` dates** (appointment start, recall due date) and need the **no-show gate** — the sequence engine keys off **enrollment dates** and a fixed step ladder. Forcing them into sequences would mean minting/expiring an enrollment per appointment and re-deriving delays from booking dates on every sweep. The FSM precedent (a dedicated forward-dated `send_service_reminders.py`, *not* a sequence) is the right template and we follow it. The **sequences engine remains the mechanism for open-ended lapsed-patient re-engagement** (a patient who ignored recall for 6 months → enroll in a "we miss you" 3-touch sequence via `crm_sequences.crm_seed_default_sequences`), which is exactly its shape.

---

## §7 API + routes

New router `src/app/practice_routes.py`, prefix `/app/practice`, every route `Depends(require_tenant)` + a `_require_practice(ctx)` gate (the twin of `_require_tms`/`_require_fsm` in `dashboard.py` — `db.module_active(tenant_id, "practice")` → 404 if off). CSRF on every mutation (`require_csrf`), matching the house pattern.

```
GET   /app/practice                          — the Today board (§2): appts + risk panel + recall queue
GET   /app/practice/appointments/{id}        — appointment detail: service, practitioner, client,
                                                patient-history summary + medical notes (decrypted, tenant-user only)
POST  /app/practice/appointments/{id}/outcome    — {outcome: completed|no_show|cancelled}
                                                   → set_booking_status + recall stamp + follow-up draft (§6.4)
POST  /app/practice/appointments/{id}/remind     — queue an EXTRA reminder now (the risk-panel action)
GET   /app/practice/recall                   — full recall queue, filterable by service / overdue
POST  /app/practice/recall/{booking_id}/draft    — draft a recall now (the queue's [Draft recall →] button)

# patient history / medical notes (tenant-user context ONLY — never public/portal)
GET   /app/practice/patients/{contact_id}/notes  — list decrypted notes
POST  /app/practice/patients/{contact_id}/notes  — add an (encrypted) note
DELETE /app/practice/patients/{contact_id}/notes/{note_id}

# practitioners + recall cadence live in the existing diary settings form (§8), not a new API.
```

No public/portal routes carry medical data. The customer-facing booking/reminder surface stays the existing email/WhatsApp thread + (optionally, later) a `/track`-style tokenised confirm link (§11) that shows only date/time, never notes.

---

## §8 UI — pages + nav

- **`/app/practice`** — the Today board (§2). NEW template `templates/app/practice.html`, reusing the diary's day-view partial for card 1 and the CRM card styling for cards 2–3.
- **`/app/practice/appointments/{id}`** — appointment + patient panel. NEW `templates/app/practice_appointment.html`. The patient panel reuses the CRM contact-detail partial and adds a **Medical notes** section (decrypted, add/edit).
- **Recall queue** — a tab on the board and a full page `templates/app/practice_recall.html`.
- **Settings** — practitioners + per-service `recall_months` + the reminder-cycle leads are added to the **existing diary settings form** (`/app/settings` scheduling section, `dashboard.py` `save_scheduling` path) via `src/app/practice_config.py`, exactly as `service_types` is edited today. No separate settings page.
- **Nav.** Add a `MODULES` entry (§10) so the Practice board appears in the module rail with a recall-due count badge (like the draft count). The board is the Practice tenant's home surface, replacing the generic today-strip that `dashboard._today_strip` shows for plain scheduling tenants.

All server-rendered Jinja2, house design system, no SPA — consistent with CRM/diary/TMS.

---

## §9 Onboarding + industry seeds

Practice OS introduces the **first industries in the `practice` OS** — none of dentists/salons/physios/etc. are in the picker yet (`operating_systems.py` maps only field/match/wholesale/assets/events industries today; Practice has *no* industries mapped — the biggest coverage gap, per OS doc §6).

### 9.1 `operating_systems.py`

Flip Practice live and add the industry→OS mappings:

```python
# OPERATING_SYSTEMS: set practice "live": True  (was False)
INDUSTRY_TO_OS = {
    ... existing ...,
    "dentist": "practice", "physio": "practice", "salon": "practice",
    "barber": "practice", "therapist": "practice", "chiropractor": "practice",
    "vet": "practice", "tutor": "practice", "coach": "practice",
    "personal_trainer": "practice", "clinic": "practice",
}
```

`is_live("practice")` then returns True, so onboarding shows Practice as a buildable Shape.

### 9.2 `onboarding.py` INDUSTRIES — add the Practice industries (`os: "practice"`)

New entries (starting with the wedge + the two biggest adjacent markets un-hidden, the rest present-but-hidden behind "Something else" until their vocab is tuned, exactly how construction/lettings/etc. are handled today):

```python
{"id": "dentist", "label": "Dental practice", "os": "practice", "live": True,
 "blurb": "Recall, reminders, no-show prediction — the whole patient diary."},
{"id": "salon", "label": "Salon, barber & beauty", "os": "practice", "live": True,
 "blurb": "Rebooking nudges, reminders, no-show prediction, client history."},
{"id": "physio", "label": "Physio & clinic", "os": "practice", "live": True,
 "blurb": "Appointment reminders, recall, treatment-block follow-ups."},
# present-but-hidden (valid ids for stored tenants / lingo / seeds; onboard via "Something else"):
{"id": "chiropractor", ...,"live": False, "hidden": True},
{"id": "vet", ...}, {"id": "tutor", ...}, {"id": "coach", ...},
{"id": "personal_trainer", ...}, {"id": "therapist", ...},
```

### 9.3 `industry_lingo.py` — LINGO + CRM_SEEDS per Practice industry

Add a `LINGO` line per industry (native vocabulary for the drafter) and a `CRM_SEEDS` block that IS the Practice field library (§4.3), self-filled by extraction. Example (dentist):

```python
LINGO["dentist"] = ("Dental practice: patients, check-ups & recalls, hygiene visits, "
    "treatment plans, NHS vs private, courses of treatment, UDAs, referrals, "
    "reminders, no-shows/FTAs.")

CRM_SEEDS["dentist"] = {
  "company": (   # the referring practice / employer / insurer household, when relevant
    {"label": "Insurer", "field_type": "text",
     "extraction_hint": "dental plan or insurer — Denplan, Bupa, NHS exempt, self-pay"},
  ),
  "contact": (   # the patient
    {"label": "Preferred contact", "field_type": "dropdown",
     "options": ["Email", "Phone", "SMS", "WhatsApp"],
     "extraction_hint": "how they ask to be contacted"},
    {"label": "Recall cadence", "field_type": "text",
     "extraction_hint": "how often they're recalled — 6 months, 12 months, 3-month hygiene"},
    {"label": "Usual practitioner", "field_type": "text",
     "extraction_hint": "the dentist or hygienist they normally see"},
    {"label": "Allergies / medical", "field_type": "text",
     "extraction_hint": "stated allergies or medical conditions — latex, penicillin, on blood thinners"},
     # NOTE: extraction of clinical/allergy content routes to practice_medical_notes (encrypted), §4.3
  ),
  "deal": (      # the current course of treatment / enquiry
    {"label": "Treatment", "field_type": "text",
     "extraction_hint": "what they're booked/asking for — check-up, filling, whitening, extraction"},
    {"label": "Treatment code", "field_type": "text",
     "extraction_hint": "any treatment/course code mentioned"},
  ),
}
```

Salon, physio, vet, tutor etc. each get their own `LINGO` + `CRM_SEEDS` block (recall cadence, preferred contact, and a service/treatment field are the common shape; the vocabulary differs). Adding a Practice industry after launch is *config only* — a `LINGO` line + a `CRM_SEEDS` block + an `INDUSTRY_TO_OS` entry, no code.

### 9.4 `onboarding.py` DATA_SOURCES — practice systems

```python
DATA_SOURCES["dentist"] = [
  {"kind": "practice_system", "label": "Practice management system",
   "hint": "Where patients, appointments and recalls live.",
   "vendors": ["Software of Excellence (SOE)", "Dentally", "SOE Exact", "R4", "Other"]},
  {"kind": "calendar", "label": "Calendar", "hint": "Outlook or Google.", "vendors": []},
]
DATA_SOURCES["physio"] = [{"kind": "practice_system", "label": "Clinic software",
   "vendors": ["Cliniko", "Jane", "WriteUpp", "TM3", "Other"], "hint": "..."}, ...]
DATA_SOURCES["salon"]  = [{"kind": "booking_system", "label": "Booking system",
   "vendors": ["Fresha", "Treatwell", "Timely", "Other"]}, ...]
```

These are captured as structured config (the integration-roadmap signal), matching how logistics/trades sources are captured today. The `_default` set already covers calendar/booking/spreadsheet for the rest.

### 9.5 The onboarding CRM preview is the sales moment

At the fields step, the existing "live preview using the tenant's last 5 emails" (`crm_fields`, CRM_design §9) now previews *patient* autofill — "from this enquiry BLAM8 would fill: insurer=Denplan, treatment=check-up, preferred contact=WhatsApp". Same mechanism, Practice-flavoured seeds. No new code — it falls out of `crm_seeds_for(industry)`.

---

## §10 Module registration + plan wiring

### 10.1 `dashboard.py` MODULES — add the `practice` entry

```python
{"id": "practice", "name": "Practice OS", "group": "addon", "badge": "Practice",
 "href": "/app/practice",
 "blurb": "The whole patient diary: recall to every patient exactly when they're due in "
          "your voice, 48/24/2-hour reminders, no-show prediction with an extra nudge for "
          "the risky ones, and patient-history-aware replies — every screen live off the "
          "diary the agent books into."},
```

And in `dashboard.py` app-home flags: `"practice_on": "practice" in mods`. The Practice board replaces the generic today-strip for practice tenants (guard `_today_strip` behind `"practice" not in mods`).

### 10.2 `plans.py` — INDUSTRY_MODULE_META + entitlement

Practice is an OS module, but wired the same way TMS/FSM are — an industry's custom module, unlocked on the `professional`/+industry tier for Practice industries:

```python
INDUSTRY_MODULE_META["dentist"] = {
  "key": "practice", "label": "Practice OS", "name": "Practice OS",
  "features": ["Automatic recall in your voice", "48/24/2-hour reminders",
               "No-show prediction + extra reminders", "Patient history that fills itself in",
               "Recall cadence per service"]}
# same "key": "practice" meta for salon, physio, vet, tutor, coach, ... (all Practice industries)
```

`INDUSTRY_MODULE` derives `{"dentist": "practice", "salon": "practice", ...}` automatically. `MANAGED_MODULES` picks up `practice` via the existing `dict.fromkeys(INDUSTRY_MODULE.values())`. So a dental tenant on the `professional` tier (renamed "Core + Practice OS") gets `{scheduling, crm, practice}`; `modules_for` and `apply_plan_modules` need no change beyond the meta entries.

**Pricing tier.** Reuse the `professional` slot (£299 today) — but Practice's engine is lighter than Transport, so introduce a Practice-priced variant. Cleanest: keep `professional` as the "+industry" tier and set Practice's headline at **£149/mo** via a per-OS price point (the OS doc §9 range). Concretely, add a Stripe price id `stripe_practice_price_id` and let `billing_plans(industry)` show £149 for the +industry tier when the industry's module key is `practice` (a small conditional in `plans.billing_plans`, mirroring how it already rewrites the tier name/features per industry). Julius picks the final number; the plumbing is one price-id + one branch.

`tenants_with_modules(("practice",))` (the sweep gate) and `module_active(tenant_id, "practice")` (the route gate) work with no change — they're generic over the module key.

---

## §11 OS-native integrations — v1 vs later

| Integration | Role | v1? |
|---|---|---|
| **Google / Outlook Calendar** | Push confirmed appointments; two-way sync of external bookings | **v1 — REUSE** `src/app/calendar_sync.py` (already pushes confirmed bookings). Nothing new. |
| **Email (Gmail/Graph)** | The whole inbound/booking/reminder/recall channel | **v1 — REUSE** the mailbox spine. This is the wedge — the practice's own inbox runs the diary. |
| **WhatsApp for reminders** | Reminders + recall over WhatsApp (patients live there) | **Later.** BLAM8's drafter is transport-agnostic (CRM_design §23). Practice reminders/recall route through the same `compose_outbound`; when the WhatsApp channel ships (`whatsapp` module, already a "coming soon" MODULES stub), the reminder/recall jobs pick `channel` from the contact's `preferred_contact` field. Design the jobs channel-agnostic from day one (a `channel` arg on `_remind_one`), wire WhatsApp when the channel lands. |
| **SOE / Software of Excellence / Dentally** | Dental PMS — import patient list + recall dates, write back appointments | **Later (import-first).** v1 captures the vendor as a data source (§9.4) and works from the inbox. v2: a one-way *import* of the patient list + due recalls (CSV/API) so recall covers the whole book on day one, not just patients who've emailed. Write-back is v3. |
| **Cliniko / Jane** | Physio/allied-health PMS | **Later**, same import-first shape. Both have public APIs (Cliniko REST, Jane more limited) — a read sync of patients + appointments is the highest-value first slice. |
| **Insurance provider lookups** | Verify/complete insurer + plan (Denplan, Bupa, NHS exemption) | **Later / cautious.** No clean public UK API; largely manual. v1: capture insurer as a self-filled CRM field. Defer any automated eligibility lookup — high effort, unclear ROI, and it touches special-category data. |
| **Tokenised confirm link** | A `/track`-style link in reminders so patients confirm/cancel without a login | **v1.5.** Reuse the driver-app/track tokenised-link pattern (`MEMORY.md` driver-app-module-and-access). Shows date/time only — never notes. Flips "confirmed?" and feeds the no-show scorer (a confirm click is a strong not-a-no-show signal). |

**The honest v1 boundary:** email-native, calendar-synced, recall + reminders + no-show scoring + patient-history all running off the inbox and the diary. PMS import and WhatsApp are the two highest-value follow-ons; both are additive.

---

## §12 Killer feature deep-dive

### 12.1 No-show prediction

**Why it's the wedge.** A UK dental/physio/salon chair sitting empty is pure lost revenue; industry FTA ("failed to attend") rates run 10–20%. Predicting *which* appointments will no-show, and spending an extra reminder only on those, is the single biggest revenue lever a practice has — and nothing in SOE/Cliniko/Jane does it from behavioural signal.

**Features fed to the scorer** (assembled by `contact_no_show_history` + the booking row, cheap, no new table):

- **Prior no-show history** — `no_shows / total_appts` for this contact, `last_no_show_at`. (The strongest single predictor.)
- **Booking pattern** — how far in advance booked (lead time), day-of-week/time (early-morning + late-Friday skew higher), whether it's a first appointment, whether it was rebooked/rescheduled before.
- **Reply latency & engagement** — `avg_reply_latency_hours` and `reminders_ignored` from `email_log` (did they open/reply to prior reminders? A silent patient scores higher risk). A **confirm-link click** or a reply to the 24h reminder sharply *lowers* the score.
- **Service type** — free check-ups no-show more than paid treatments; the scorer sees the service name + whether a deposit/price attaches.

**Scoring approach (LLM, reusing the Haiku call BLAM8 already meters).** The scorer is a structured-output LLM call (same model + `usage.record_anthropic` metering as extraction/drafting — no new spend, ~$0.0002/appt). A deterministic heuristic prior (weighted sum of the features above) is computed first and *passed into the prompt*, so the LLM refines rather than hallucinates and the score is stable/explainable:

```
System: You score how likely a patient is to miss an appointment, 0–100, and give a
one-line reason a receptionist would understand. You are given behavioural features and
a heuristic prior. Adjust the prior using judgement about the pattern; do not invent
facts not in the features. Return {"score":int,"band":"low|medium|high","reason":str}.

Features:
- prior appointments: {total}, no-shows: {no_shows} (last {last_no_show_at})
- booked {lead_days} days ahead, {weekday} {time}, first_visit={bool}, rescheduled={n}
- avg reply latency: {hours}h, last {k} reminders ignored: {bool}, confirmed_link_click={bool}
- service: {service_name}, paid={bool}
Heuristic prior score: {prior}
```

**Thresholds (config-overridable per tenant, sensible defaults):** `low` < 40, `medium` 40–69, `high` ≥ 70. Only `high` triggers the extra reminder. The band drives the Today-board chip colour. Thresholds live in `practice_config` so a practice with a packed book can raise the bar.

**The extra-reminder action.** For a `high`-band appointment, `score_no_shows` (§6.3) queues one additional drafted reminder in the 3–6h-before window (`compose_outbound`, warmer + a confirm ask, `mark_extra_reminder_queued` for dedupe). On the risk panel the human can also fire it manually (`POST …/remind`). The reminder never says "we think you'll no-show" — the facts handed to the composer are neutral ("a friendly extra confirmation for an appointment in a few hours; ask them to reply to confirm or let us know if they need to move it").

**Privacy.** The scorer sees behavioural metadata only — never the clinical notes. `contact_no_show_history` returns counts/latencies, not note text.

### 12.2 The recall drafter

**Mechanics.** On appointment completion, `complete_appointment_and_schedule_recall` stamps `recall_due_date = completed_date + service.recall_months` (6 for a dental check-up, 3 for hygiene, per the service's `recall_months`; if the patient has a `Recall cadence` CRM field it overrides). The daily `send_recall` job (§6.1) finds due recalls and drafts "time for your check-up" via `compose_outbound` — **in the practitioner's voice**, because the composer already loads the tenant's voice/config. The facts handed in name the service, the months since last visit, and the usual practitioner ("You last saw Priya in January…").

**Why it beats SOE/Cliniko.** Their recall is a template SMS/letter merge. BLAM8's is a *drafted, personal* message in the practice's own voice, from the practice's own inbox, that the patient can simply *reply to* to rebook — and the reply lands back in the same drafter that checks the diary and offers slots. The loop closes without the patient leaving their inbox or the practice touching a separate system. Recall becomes conversational, not a broadcast.

**Cadence correctness — the wedge promise ("exactly when due").** Because the cadence is per-service and stamped at completion, every patient is chased on *their* clock, not a batch date. A patient seen on the 3rd is recalled 6 months later on the 3rd, not lumped into a monthly mail-merge. The recall queue (§2) shows the practice the whole forward book of who's due when.

---

## §13 Build order

Each phase independently shippable. Rough single-developer-with-LLM estimate, consistent with the OS doc's ~4–6 week Practice figure.

- **Phase P0 — Foundations + industry seeds (2–3 days).** `operating_systems.py` flip + `INDUSTRY_TO_OS`; `onboarding.py` INDUSTRIES + DATA_SOURCES; `industry_lingo.py` LINGO + CRM_SEEDS for dentist/salon/physio; `plans.py` INDUSTRY_MODULE_META + Practice price id; `dashboard.py` MODULES entry + flag. Migration script for the `bookings` additive columns + `practice_medical_notes` (idempotent psycopg2, `scripts/db/migrations/apply_practice_migration.py`). **Shippable value:** dental/salon/physio tenants can onboard into Practice OS and their CRM fills with patient fields; no engine yet, but the self-filling patient book is real.
- **Phase P1 — Recall engine (3–4 days).** `practice_config.py` (practitioners + per-service `recall_months`); `db.complete_appointment_and_schedule_recall` + `list_appointments_due_recall` + `mark_recall_reminded`; `src/app/jobs/send_recall.py`; the recall queue UI + `/app/practice/recall`. **Shippable value: the wedge.** Recall goes out to every patient exactly when due, in the practice's voice. Demo-ready on its own.
- **Phase P2 — Reminder cycle (2–3 days).** Generalise the day-before job into `send_appointment_reminders.py` (48/24/2h, `reminder_stages_sent` dedupe, confirm ask). **Shippable value:** the fuller reminder cadence patients expect.
- **Phase P3 — No-show prediction (3–4 days).** `contact_no_show_history`, the heuristic prior + LLM scorer, `score_no_shows.py`, the extra-reminder gate, the Today board's risk panel + chips, the outcome buttons (completed/no_show/cancelled) that feed history. **Shippable value:** the second half of the wedge — the risky ones get an extra nudge.
- **Phase P4 — The Today board + patient panel + encrypted notes (2–3 days).** `/app/practice`, the appointment detail with the patient-history summary, `practice_medical_notes` add/edit (encrypted), `patient_history_summary` wired into `crm_intel.build_crm_context` so replies are patient-history-aware. **Shippable value:** the daily surface + patient-aware drafting.
- **Phase P5+ — Integrations (ongoing).** PMS import (SOE/Cliniko/Jane), WhatsApp channel, tokenised confirm link. Each additive.

**Total core (P0–P4): ~3 weeks.** P0+P1 alone (~1 week) is a demoable product — the recall wedge on a self-filling patient book.

---

## §14 Open questions + risks

- **Medical-data / UK GDPR (highest).** Allergies, conditions and clinical notes are Art. 9 special-category data. Mitigations in this spec: encryption at rest (`practice_medical_notes`, §4.3), notes never leave the tenant-user context, never logged, never sent to the LLM in bulk (only a redacted summary rides into the drafter). Open: (a) a per-tenant DPA + lawful-basis capture at onboarding; (b) whether to let the *extraction* pipeline write clinical content at all, or require it be typed manually by the practice (safer default: extraction may flag "this email mentions medical info — add a note?" as a suggestion, not auto-write it). Recommendation: **suggestion, not autofill, for medical content**; autofill is fine for non-clinical fields (insurer, preferred contact, treatment name).
- **Encryption key management.** Reusing the token-encryption key means the same key encrypts mailbox tokens and medical notes. `MEMORY.md` records a local/prod key mismatch (local `.env` ≠ Render's, decrypt fails silently). For medical notes a silent decrypt failure is worse than for a token — surface it loudly in `/app/practice`, and consider a separate `MEDICAL_ENCRYPTION_KEY` so a key rotation on one doesn't blind the other.
- **Insurance lookups.** No clean UK API; likely stays manual/self-filled indefinitely. Don't over-invest.
- **No-show scoring cold start.** A new patient / new tenant has no history — the scorer leans on booking-pattern priors and should band conservatively (default `low`/`medium`, not `high`) until there's signal, to avoid crying wolf. Track precision once live.
- **Practitioner model.** v1 uses resource-string pools (no rota/holidays/individual calendars). A busy multi-chair practice will want a real `practitioners` table with per-practitioner working hours + leave. Deferred to v2 (§1) — validate demand first.
- **Recall vs sequences overlap.** Two mechanisms touch "bring them back" (dedicated recall job + the lapsed-patient sequence). Keep the boundary crisp: recall = *on cadence, once*; sequences = *lapsed, multi-touch*. Document it so they don't double-message (the sequence should exclude contacts with an open/recent recall).
- **Deposit/no-show fee handling.** Some practices charge for FTAs. Out of scope for v1; would ride on the Phase 5 proposals/invoicing loop (CRM_design §22) if built.
- **WhatsApp timing.** Salons especially expect WhatsApp reminders. If salon is a launch industry, WhatsApp may need to move earlier than "later" — gate the launch industry choice on channel readiness.

---

## §15 Reuse map

The explicit reuse/build ledger. **This is the point of the spec:** Practice OS is mostly reuse.

| Part | REUSE (existing capability) | BUILD NEW |
|---|---|---|
| Appointment primitive | `bookings` table, `src/app/scheduling.py`, `src/app/slotgrid.py`, atomic `db.hold_booking_if_capacity` | additive `bookings` columns (recall_*, no_show_*, reminder_stages_sent, practitioner) |
| Per-service durations | `src/app/services.py`, `service_availability`, slot-mode diary | per-service `recall_months` key (config, no schema) |
| Practitioner dimension | the `resource` pool on services + `service_availability(resource=…)` | `scheduling_config.practitioners` config list + `src/app/practice_config.py` |
| Client record + history | CRM (`crm_contacts`/`companies`, `custom_data`), extraction (`crm_extract`/`crm_fields`), seeds (`industry_lingo.CRM_SEEDS`) | Practice CRM seeds (patient fields) |
| Medical notes | `app/crypto` envelope (mailbox-token encryption) | `practice_medical_notes` table (only new table) + note CRUD |
| Booking → confirm | `agent.draft_reply` + `SCHEDULING_TOOLS`, diary confirm action, `calendar_sync` | nothing |
| Reminder cycle | `src/app/jobs/send_reminders.py` (day-before) skeleton, `compose_outbound`, `insert_email_log`, `auto_send` | `send_appointment_reminders.py` (48/24/2h, per-stage dedupe) |
| Recall | `src/app/jobs/send_service_reminders.py` + `complete_job_and_schedule_service` + `list_jobs_due_service` + `_add_months` (the exact FSM pattern) | `send_recall.py` + `complete_appointment_and_schedule_recall` + `list_appointments_due_recall` |
| Follow-up drafter | `agent.compose_outbound`, on-write hook pattern | outcome-action trigger |
| No-show prediction | Haiku call + `usage.record_anthropic` metering, `email_log` reply data | scorer (`score_no_shows.py`), features (`contact_no_show_history`), heuristic prior, thresholds, extra-reminder gate |
| Lapsed-patient re-engagement | `src/app/crm_sequences.py` (full engine) | a Practice default sequence seed |
| Daily surface | `scheduling.diary_days`/`_day_chart`, CRM card styling | `/app/practice` board (`practice_routes.py`, `practice_board`, templates) |
| Scheduler / automation | `src/app/scheduler.py` `run_daily`/`run_every`, `tenants_with_modules` gate | job registrations in `src/app/main.py` |
| Drafter voice/context | tenant voice/config, `crm_intel.build_crm_context` | `patient_history_summary` injected into the context |
| Onboarding | `onboarding.py` wizard, `operating_systems.py`, `industry_lingo`, DATA_SOURCES | Practice industries + seeds + data sources + `is_live` flip |
| Module registry / plans | `dashboard.py` MODULES, `plans.py` INDUSTRY_MODULE(_META)/`modules_for`/`apply_plan_modules`, `module_active` gate | `practice` MODULES + META entries + price id |
| RLS / migrations | `auth_tenant_ids()` policy, idempotent psycopg2 script convention (`scripts/db/migrations/apply_crm_phase4b_migration.py`) | `scripts/db/migrations/apply_practice_migration.py` |

**Net new files:** `src/app/practice_config.py`, `src/app/practice_routes.py`, `src/app/jobs/send_recall.py`, `src/app/jobs/send_appointment_reminders.py`, `src/app/jobs/score_no_shows.py`, `templates/app/practice*.html`, `scripts/db/migrations/apply_practice_migration.py`. **Net new tables:** one (`practice_medical_notes`). Everything else is additive columns, config, seeds, and job registrations on top of shipped infrastructure.
