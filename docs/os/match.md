# Match OS — Design & Build Spec

**Status:** 🌱 Not built. Vocabulary + CRM field seeds for **Recruitment** already exist (`src/app/industry_lingo.py`, hidden from the onboarding picker). This spec is the build-ready plan — **as amended by the v1 scope constraint below (2026-07-10)**.
**Author:** Julius Deane, w/ Claude synthesis
**Last updated:** 2026-07-10 (EU AI Act descope decision)
**Reads first:** `docs/BLAM8_operating_systems.md` §4.7 (the OS entry), `docs/architecture/CRM_design.md` (the platform this reuses — especially §3 six base entities, §10 LLM extraction, §26 enrichment, promise tracking in §5/§18), `docs/security/eu-ai-act-compliance.md` (§2 the Annex III landmine this spec must not step on).

> **One-liner.** Match OS is a **dual-sided self-filling CRM**. It reuses the whole CRM spine — self-filling contacts/companies, LLM extraction, promise tracking, sequences, the drafter in the tenant's voice, the in-process scheduler, RLS — and adds exactly one novel primitive: the **`match`** (candidate ↔ role, paired **by the recruiter**; AI fit-scoring is v2-gated — see the scope constraint), plus a dual-sided pipeline that briefs and chases *both* sides. Everything else is already shipped in Core.

---

## ⚖️ v1 SCOPE CONSTRAINT — EU AI Act (DECIDED, Julius, 2026-07-10)

**Match OS v1 ships NO candidate evaluation. None.** Specifically, v1 must not implement: `fit_score` / `fit_band` computation, LLM (or any algorithmic) scoring of candidates, ranking or ordering candidates by any AI-derived signal, shortlisting, auto-proposed matches, or filtering/hiding any candidate from the recruiter. **The recruiter decides who fits; BLAM8 kills the typing and the chasing.**

**Why (this is a hard legal line, not a preference):** EU AI Act **Annex III 4(a)** classifies AI used "to analyse and filter job applications [and] evaluate candidates" as **HIGH-RISK** — obligations apply from **2 Aug 2026**: quality-management system, Annex IV technical documentation, conformity assessment + CE marking, public EU-database registration, bias/accuracy testing, post-market monitoring — and deployer duties land on our tenants. The extraterritorial hook (output *used* in the EU — one Dublin candidate in a UK agency's pipeline) means "UK-only" is not a shield, and the Art 6(3) "narrow procedural task" exemption cannot plausibly cover a score that drives who gets introduced. Full analysis: `docs/security/eu-ai-act-compliance.md` §2.

**What v1 IS (all limited-risk, Art 50 already handled platform-wide):** dual-sided self-filling records (candidate/role extraction is draft-first data-entry assistance — tenant confirms every field; no AI-derived ordering anywhere), **manual pairing** by the recruiter, two-sided briefing drafts, interview scheduling, the two-way promise ledger + chases, placement pipeline. The Bullhorn wedge survives: the pitch was never the score — it's "stops the typing", £99 vs £200.

**Marketing constraint:** never claim BLAM8 "ranks", "scores", "matches" or "evaluates" candidates. Say: *"you stay the judge of fit — BLAM8 does everything else."*

**v2 gate:** scoring may only be built as a deliberate, priced decision — requires (a) a design partner who values it enough to justify the high-risk compliance bill, (b) the Commission's final employment-AI guidance reviewed, (c) Julius's explicit sign-off on carrying the Annex III provider regime. The schema keeps the nullable `fit_*` columns so v2 needs no migration; the columns stay NULL in v1.

**Sequencing note:** Wholesale OS builds before Match (larger market ~80K vs ~30K, zero regulatory drag).

---

## §0 Positioning

**Shape.** "You pair two sides." A Match OS business does not sell a product or a service to one customer — it introduces **entity A to entity B** and gets paid when the pairing succeeds. Recruiters pair candidates with roles; tutoring services pair tutors with students; matchmaking pairs mentors with mentees. The unit of work is not a deal, it is a **match**, and the daily job is working a portfolio of live matches through introduction → interview → feedback → placement.

**Wedge.** Small recruitment agencies (1–5 people) currently paying **Bullhorn ~£200/mo** or living in **Excel + LinkedIn**. Bullhorn is an ATS/CRM that *still asks them to type everything*: every candidate, every role, every note after every call. BLAM8 does CRM + matching + placement pipeline for **£99** — and the relationship data **fills itself in** from the inbox they already work out of. The pitch is identical to the CRM's core promise (`docs/architecture/CRM_design.md §1`), just doubled: two entity types that both self-populate, and a fit score computed for free by the same LLM that already reads every email.

**Target industries (~30K UK SMBs).**

| Industry | Entity A ↔ Entity B | Notes |
|---|---|---|
| Recruitment agencies (perm/contract) | candidate ↔ role | The beachhead. Vocab seed already exists. |
| Agency / temp work | worker ↔ shift/assignment | Higher velocity, same shape. |
| Executive search | candidate ↔ mandate | Fewer, higher-value matches; verification matters most here. |
| Tutor–student matching | tutor ↔ student/subject | Consumer-side; "role" = a learning need. |
| Freelance marketplaces | freelancer ↔ brief | "role" = a gig/brief. |
| Sports coaching matches | coach ↔ athlete/club | |
| Mentor matching | mentor ↔ mentee | Often programme-run; light on money, heavy on scheduling. |

UK recruitment agencies alone number ~30K (the majority are micro-agencies of 1–5 people — precisely the segment Bullhorn over-serves and over-charges). This is the smallest of the seven OS markets by count but among the highest ACV-tolerant (recruiters live and die by their pipeline tooling).

**Build state.** 🌱 Vocabulary seed exists for Recruitment (`industry_lingo.LINGO["recruitment"]` + `industry_lingo.CRM_SEEDS["recruitment"]`), and the `recruitment` industry is registered in `src/app/onboarding.py:INDUSTRIES` (mapped to `os="match"`, currently `live: False, hidden: True`). `operating_systems.OPERATING_SYSTEMS` already lists Match OS (`live: False`). The module itself — the `match` primitive, fit scoring, dual-sided board — is not built. This spec builds it.

---

## §1 The primitive — the `match`

### The two sides vs the CRM's `contact` / `company` / `deal`

The CRM already has the right skeleton for **one** side of a match. A candidate is a person who writes email — that is a `crm_contact`. A client (the hiring company) is a `crm_company`, and the hiring manager is a `crm_contact` inside it. The novel entities are:

- **`role`** — a vacancy/brief/mandate. This is *shaped like a `crm_deal`* (an opportunity clustered around a subject, owned by a company, worth money on placement) but carries matching-specific structured fields (requirements, salary range, remote/hybrid/onsite, urgency) that a generic deal does not.
- **`candidate`** — the supply side. A candidate is *shaped like a `crm_contact`* (a person, unique by email, with an activity feed) but carries supply-specific structured fields (skills, experience, salary expectation, notice period, availability, location constraints) and, crucially, a **CV artifact** that gets LLM-parsed.
- **`match`** — the pairing itself: candidate A × role B, with an **LLM-computed fit score**, a **reasoning string shown to both sides**, and its own status lifecycle. This is genuinely new. Nothing in the CRM pairs two records and scores the fit.

### Reuse vs new — the decision, and its justification

| Concern | Decision | Why |
|---|---|---|
| The client company (hiring firm) | **REUSE `crm_companies`** | It is exactly a company. Companies House enrichment (`docs/architecture/CRM_design.md §26`) already targets this table — client verification comes for free. |
| The hiring-manager contact | **REUSE `crm_contacts`** | Exactly a contact. Self-fills from their emails. |
| The candidate | **NEW `match_candidates`** (referencing an underlying `crm_contacts` row) | A candidate *is* a person (so they get a contact row for the activity feed + self-fill), but candidate-specific fields (skills, notice period, CV parse, availability) are a distinct concern with distinct extraction and distinct GDPR handling (§14). Overloading `crm_contacts.custom_data` for a first-class, heavily-queried supply entity would be a category error — we query candidates by skill/availability constantly, and JSONB is the wrong home for that. |
| The role/vacancy | **NEW `match_roles`** (referencing an underlying `crm_deals` row) | A role *is* an opportunity worth money (so it gets a deal row — pipeline, value, stage, promise-clustering all reuse the deal machinery), but role-specific structured fields (requirements, salary_min/max, work_mode, urgency) and the fact that a role is matched against many candidates make it a first-class entity, not deal `custom_data`. |
| The pairing + fit score | **NEW `match_matches`** | No CRM equivalent exists. This is the OS primitive. |

**Why two tables that "wrap" CRM rows rather than pure `custom_data`:** the CRM's `custom_data` JSONB is the right tool for *tenant-defined, self-filling* fields (Sector, Headcount — see the recruitment seed). It is the wrong tool for the OS's *structural, queried, cross-joined* fields. We do both: `match_candidates`/`match_roles` hold the structural columns the OS queries and matches on; the underlying `crm_contacts`/`crm_deals` rows keep the activity feed, promises, custom fields, and drafter context. A candidate's `notice_period` is a structural column; their tenant-added "Right to work checked?" is `crm_contacts.custom_data`. This mirrors how the TMS/Field OS wraps bookings without forking the diary.

---

## §2 Daily-rhythm surface — the matching board

Per `docs/BLAM8_operating_systems.md §4.7`: *"today's matches to work on + pending replies from candidates or clients + interviews scheduled + placements to follow through."* This lands as a **dual-sided board** at `/app/match`, and as a **Today card** on the existing home dashboard (`src/app/dashboard.py:app_home` already renders a best-effort CRM card block — the Match card slots in beside it, same defensive try/except pattern).

The board has four columns/rails, each populated from an existing engine plus the new `match_matches` table:

1. **Today's matches to work on** — matches in status `proposed` or `introduced` whose `next_action_at <= today`. **v1 ordering: `next_action_at` asc, then recency — NEVER by any AI-derived signal (scope constraint).** "David (React, avail. 2wk) × Senior FE @ Acme — introduce?" *(v2-gated variant: ranked by `fit_score` desc with the score shown.)*
2. **Pending replies** — the *dual-sided* twist. Any thread where BLAM8 is waiting on a candidate OR a client. Reuses the gone-quiet recency logic already on contacts (`crm.with_recency(...)`, `crm.STALE_AFTER_DAYS`), computed for *both* sides of each live match. "Waiting on candidate David (3d) · Waiting on client Acme (interview feedback, 5d overdue)."
3. **Interviews scheduled** — matches in status `interview`, with the interview datetime (from Calendly webhook or a `crm_promises` due_hint). Drives the two-way promise chase (§12).
4. **Placements to follow through** — matches in status `placed`, showing the post-placement follow-up promises (start date confirmed by X, invoice due by Y, guarantee-period check-in by Z).

Each rail item deep-links to the **match detail page** (§8), which is the killer view: candidate side + role side + both-sides promise ledger + a one-click "Draft the intro to both sides". *(Fit score + reasoning on this page is v2-gated.)*

---

## §3 Workflow state machine

The `match` (and its two sides) move through the states below. **Every transition has a drafter touchpoint** — and because this is Match OS, most touchpoints brief *both sides* (the drafter composes to the candidate AND to the client, in the tenant's voice, in matching language). All drafting reuses `agent.compose_outbound(cfg, purpose, facts)` (proactive) or `agent.draft_reply(...)` (in-thread), persisted via `db.insert_email_log(...status="pending")` and auto-sent only when `cfg["auto_send"]` (the exact pattern in `src/app/crm_sequences.py:_do_send_email` and `src/app/jobs/send_service_reminders.py`).

```
 CANDIDATE INTAKE ──┐
                    ├──► MATCHING ──► INTRODUCTION ──► INTERVIEW ──► FEEDBACK ──► PLACEMENT ──► FOLLOW-UP
 ROLE INTAKE ───────┘   (recruiter    (brief both)     (schedule)   (chase both)  (offer)      (guarantee)
                          pairs; v1 has NO scoring — scope constraint)
```

`match_matches.status` enum: `proposed | introduced | interview | feedback | offer | placed | rejected | withdrawn`.

| Transition | What happens | Drafter touchpoint (BOTH sides unless noted) |
|---|---|---|
| **Candidate intake** | Inbound email / forwarded CV → `crm_contacts` upsert + `match_candidates` row; CV parsed by LLM (§11) → skills/experience/salary/notice/availability autofill. | *(candidate side)* Ack + probe for any high-requirement missing field (reuse `crm_fields.get_missing_probeable_fields` → probe instructions in the reply). |
| **Role intake** | Inbound from a client / forwarded JD → `crm_companies` + `crm_deals` + `match_roles`; requirements/salary/work_mode/urgency autofilled. Companies House enrichment fires on the new company (§11). | *(client side)* Ack + probe missing role essentials (salary band, must-haves). |
| **Matching** | **v1: the RECRUITER pairs** a candidate with a role from either detail page ("Match with…"); `match_matches` row written in `proposed` with `fit_score=NULL`. *(v2-gated: the §12 scoring engine auto-computes fit for candidate × open-role combinations.)* | none (background). Surfaces on the board rail 1. |
| **Introduction** | Tenant (or auto, per config) advances a proposed match. | **Brief both sides:** to the client — "Here's David, here's why he fits this role (fit 0.86: …)"; to the candidate — "Here's the Acme role, here's why it suits you." Same `reasoning`, phrased for each audience. Logs a two-way promise: *we owe the client a shortlist decision; candidate owes us availability.* |
| **Interview** | Interview booked (Calendly webhook, or extracted from a reply). Match → `interview`. | **Both sides:** confirm time/logistics to candidate + client. Promise: *client owes feedback by {date}; candidate owes attendance.* |
| **Feedback** | Interview happened. BLAM8 chases whoever owes feedback (§12 two-way chase). | **Client side** (chase feedback) + **candidate side** (chase their read on it). |
| **Placement / offer** | Offer extended → match → `offer`; accepted → `placed`; the underlying `crm_deal` moves to Won. | **Both:** offer-management drafts (to candidate: the offer; to client: confirmation). Placement fee promise recorded. |
| **Follow-up** | Post-placement. Guarantee-period check-ins, invoice chase, "how's David settling in?". | **Both:** candidate aftercare + client aftercare + (via CRM) invoice/receivables if Phase 5.1 proposals/invoicing is on. |

Rejected/withdrawn are terminal; a rejected candidate stays a live candidate for *other* roles (that's the point of a supply pool).

---

## §4 Schema — additive tables

Additive migration only, mirroring house conventions exactly: `id uuid PK`, `tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE`, RLS on every table with the standard `"tenant isolation - <table>"` policy, indexes on `(tenant_id, …)`. Wraps — never forks — `crm_contacts`/`crm_companies`/`crm_deals`.

```sql
-- ── Match OS — "pair two sides, score the fit" (docs/os/match.md) ──────────────

-- Supply side. Wraps a crm_contacts row (activity feed, custom fields, promises,
-- drafter context) and adds the structural, queried candidate columns.
create table if not exists match_candidates (
  id                uuid primary key default gen_random_uuid(),
  tenant_id         uuid not null references tenants(id) on delete cascade,
  contact_id        uuid not null references crm_contacts(id) on delete cascade,
  headline          text,                       -- "Senior React developer, 8y"
  skills            jsonb not null default '[]', -- ["React","TypeScript","Node"]
  experience_years  numeric,
  salary_expectation numeric,
  salary_currency   text default 'GBP',
  notice_period     text,                        -- "1 month", "immediate"
  availability      text,                        -- free text or ISO date
  available_from    date,
  work_mode_pref    text,                        -- remote | hybrid | onsite | any
  location          text,                        -- "Leeds / LS postcodes"
  location_radius_mi integer,
  cv_artifact_id    uuid references crm_artifacts(id) on delete set null,
  cv_parsed_at      timestamptz,
  status            text not null default 'active', -- active | placed | dormant | withdrawn
  custom_data       jsonb not null default '{}',  -- reserved; tenant fields live on the contact
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  unique (tenant_id, contact_id)
);
create index if not exists idx_match_candidates_tenant
  on match_candidates (tenant_id, status);
create index if not exists idx_match_candidates_available
  on match_candidates (tenant_id, available_from);

-- Demand side. Wraps a crm_deals row (pipeline, value, stage, promise-clustering)
-- and adds the structural role columns.
create table if not exists match_roles (
  id                uuid primary key default gen_random_uuid(),
  tenant_id         uuid not null references tenants(id) on delete cascade,
  deal_id           uuid not null references crm_deals(id) on delete cascade,
  company_id        uuid references crm_companies(id) on delete set null,
  title             text,                        -- "Senior Frontend Engineer"
  requirements      jsonb not null default '[]', -- ["React","5y+","UK right to work"]
  requirements_text text,                        -- the raw JD/brief for scoring context
  salary_min        numeric,
  salary_max        numeric,
  salary_currency   text default 'GBP',
  work_mode         text,                        -- remote | hybrid | onsite
  urgency           text default 'normal',       -- low | normal | high | urgent
  headcount         integer default 1,           -- vacancies on this role
  engagement        text,                        -- perm | contract | temp
  status            text not null default 'open',-- open | on_hold | filled | closed
  custom_data       jsonb not null default '{}',
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  unique (tenant_id, deal_id)
);
create index if not exists idx_match_roles_tenant
  on match_roles (tenant_id, status);

-- THE PRIMITIVE. Candidate × role, LLM-scored, dual-sided lifecycle.
create table if not exists match_matches (
  id                uuid primary key default gen_random_uuid(),
  tenant_id         uuid not null references tenants(id) on delete cascade,
  candidate_id      uuid not null references match_candidates(id) on delete cascade,
  role_id           uuid not null references match_roles(id) on delete cascade,
  fit_score         numeric,                     -- 0.00–1.00, LLM-computed
  fit_band          text,                        -- strong | possible | weak (derived from score)
  reasoning         text,                        -- shown to BOTH sides (the killer feature)
  reasoning_model   text,                        -- the model id that scored it (audit)
  scored_at         timestamptz,
  status            text not null default 'proposed',
  -- proposed | introduced | interview | feedback | offer | placed | rejected | withdrawn
  next_action_at    timestamptz,                 -- drives the board's "today" rail
  interview_at      timestamptz,                 -- populated from Calendly / extraction
  rejected_reason   text,
  placed_at         timestamptz,
  placement_fee     numeric,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  unique (tenant_id, candidate_id, role_id)      -- one live pairing per (cand, role)
);
create index if not exists idx_match_matches_board
  on match_matches (tenant_id, status, next_action_at);
create index if not exists idx_match_matches_role
  on match_matches (tenant_id, role_id, fit_score desc);
create index if not exists idx_match_matches_candidate
  on match_matches (tenant_id, candidate_id, fit_score desc);

-- RLS — identical shape to every CRM table (db/schema.sql).
alter table match_candidates enable row level security;
drop policy if exists "tenant isolation - match_candidates" on match_candidates;
create policy "tenant isolation - match_candidates" on match_candidates
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

alter table match_roles enable row level security;
drop policy if exists "tenant isolation - match_roles" on match_roles;
create policy "tenant isolation - match_roles" on match_roles
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

alter table match_matches enable row level security;
drop policy if exists "tenant isolation - match_matches" on match_matches;
create policy "tenant isolation - match_matches" on match_matches
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));
```

**Reused as-is (no new columns):** `crm_contacts`, `crm_companies`, `crm_deals`, `crm_activities`, `crm_stages`, `crm_artifacts` (the CV lands here), `crm_field_definitions` / `crm_field_suggestions` (tenant custom fields on candidate-as-contact and role-as-deal), `crm_promises` (two-way tracking — its `direction` is already `we_owe`/`they_owe`, exactly what dual-sided needs), `crm_sequences`/`crm_sequence_steps`/`crm_sequence_enrollments` (candidate/client nurture).

**Migration script:** `scripts_apply_match_migration.py`, mirroring `scripts/db/migrations/apply_crm_migration.py` — read `DB_PASSWORD` + project ref from `.env`, connect to `aws-1-eu-west-2.pooler.supabase.com:5432` as `postgres.{ref}`, `autocommit=True`, execute the block (idempotent: `create table if not exists` / `add column if not exists` / `drop policy if exists`), then verify each table via `to_regclass('public.match_matches')` and each policy via `pg_policies`. Per house convention the DDL lives in `db/schema.sql` under a marker (`-- Match OS — "pair two sides…"`) and the script slices that section by the marker string, same as the CRM migration.

---

## §5 DB layer — `db.py`-style signatures

New functions in `src/app/db.py`, prefixed `match_*`, in the same style as the `crm_*` family (Supabase client, tenant-scoped, optional `client: Client | None = None` for RLS-context reuse).

```python
# ── candidates ──────────────────────────────────────────────────────────────
def match_upsert_candidate(tenant_id: str, *, contact_id: str, fields: dict) -> dict:
    """Get-or-create + refresh the candidate row for a contact (race-safe
    select-then-insert, mirroring db.crm_get_or_create_contact — there is no
    generic crm_upsert_* in the codebase; this follows the same pattern)."""
def match_get_candidate(candidate_id: str, tenant_id: str, client=None) -> dict | None: ...
def match_candidate_by_contact(tenant_id: str, contact_id: str, client=None) -> dict | None: ...
def match_list_candidates(tenant_id: str, *, status: str = "active",
                          skill: str | None = None, available_by: str | None = None,
                          limit: int = 200, client=None) -> list[dict]: ...
def match_update_candidate(candidate_id: str, tenant_id: str, fields: dict, client=None) -> None: ...

# ── roles ───────────────────────────────────────────────────────────────────
def match_upsert_role(tenant_id: str, *, deal_id: str, company_id: str | None,
                      fields: dict) -> dict: ...
def match_get_role(role_id: str, tenant_id: str, client=None) -> dict | None: ...
def match_role_by_deal(tenant_id: str, deal_id: str, client=None) -> dict | None: ...
def match_list_roles(tenant_id: str, *, status: str = "open",
                     limit: int = 200, client=None) -> list[dict]: ...
def match_update_role(role_id: str, tenant_id: str, fields: dict, client=None) -> None: ...

# ── matches (the primitive) ─────────────────────────────────────────────────
def match_upsert_match(tenant_id: str, *, candidate_id: str, role_id: str,
                       fit_score: float | None, fit_band: str | None,
                       reasoning: str | None, reasoning_model: str | None) -> dict:
    """Insert or refresh a (candidate, role) pairing. Idempotent on
    match_matches unique(tenant_id, candidate_id, role_id) — re-scoring updates
    fit_score/reasoning/scored_at in place, never stacks (mirrors
    crm_insert_promise's source-uniq behaviour)."""
def match_get_match(match_id: str, tenant_id: str, client=None) -> dict | None: ...
def match_set_status(match_id: str, tenant_id: str, status: str, *,
                     next_action_at: str | None = None, client=None) -> None: ...
def match_matches_for_role(tenant_id: str, role_id: str, *, limit: int = 20,
                           client=None) -> list[dict]:
    """Top-fit candidates for a role — ordered by fit_score desc (shortlist)."""
def match_matches_for_candidate(tenant_id: str, candidate_id: str, *, limit: int = 20,
                                client=None) -> list[dict]: ...
def match_board(tenant_id: str, client=None) -> dict:
    """The daily-rhythm surface (§2): {today: [...], pending: [...],
    interviews: [...], placements: [...]} — one round-trip for the board page."""
def match_open_candidates_and_roles(tenant_id: str, client=None) -> tuple[list, list]:
    """Feed for the scoring sweep — active candidates × open roles."""
```

Reused verbatim (no new code): `db.crm_get_or_create_contact`, `db.crm_get_or_create_company`, `db.crm_create_company_by_name`, `db.crm_create_deal`, `db.crm_update_deal`, `db.crm_insert_activity`, `db.crm_insert_promise`, `db.crm_open_promises`, `db.crm_resolve_promise*`, `db.crm_merge_custom_data`, `db.crm_list_field_definitions`, `db.insert_email_log`, `db.mark_email_log_sent`, `db.log_agent_event`.

---

## §6 Engine / automation

New module `app/match_engine.py` (LLM fit scoring + intake wiring) — the Match analogue of `src/app/crm_intel.py`. It reuses the CRM's extraction, promise, and drafter engines rather than reinventing them.

### 6.1 Intake (self-filling, reused)

Match rides the CRM's existing per-email hook. When an email is processed for a Match-OS tenant:

1. **Contact/company self-fill** — unchanged CRM path (`src/app/crm.py`): sender → `crm_contacts` (+ freemail stated-company inference → `crm_companies`).
2. **Custom-field extraction** — unchanged (`src/app/crm_extract.py` + `app/crm_fields.apply_extraction`, `AUTOFILL_AT = 0.75` / `SUGGEST_AT = 0.50` gate). The recruitment `CRM_SEEDS` fields (Sector, Headcount, Role, Vacancy, Salary range, Perm/Contract) autofill here today.
3. **Match classification (NEW)** — `match_engine.classify_email(tenant_id, email_row)` runs one Haiku call (`settings.anthropic_model`, same model + spend profile as `crm_intel.analyze`) that decides: is this a **candidate** signal (skills/CV/availability) or a **role** signal (a vacancy/brief)? and extracts the structural fields for the relevant side.
   - Candidate → `match_upsert_candidate` (contact_id from step 1). If a CV is attached (a `crm_artifacts` row), enqueue CV parse (§11).
   - Role → `crm_create_deal` (if none clustered) + `match_upsert_role`.
4. **Promise extraction** — unchanged (`crm_intel.apply_promises`), already two-way via `direction ∈ {we_owe, they_owe}`. Dual-sided tracking is a property BLAM8 already has; Match OS just uses both directions heavily.

### 6.2 LLM fit scoring — ⚖️ V2-GATED: DO NOT BUILD IN v1 (scope constraint, 2026-07-10)

*(Everything in this subsection is candidate evaluation under EU AI Act Annex III 4(a) and is retained for a future, deliberately-priced v2 only. In v1, matches are created manually by the recruiter — `match_upsert_match` is called from the UI with `fit_score=NULL`.)*

`async match_engine.score_candidate_role(tenant_id, candidate: dict, role: dict) -> dict` — one structured Haiku call returning `{fit_score: 0-1, fit_band, reasoning}` (prompt in §12). Called by:

- `async match_engine.rescore_for_candidate(tenant_id, candidate_id)` — new/updated candidate × all open roles.
- `async match_engine.rescore_for_role(tenant_id, role_id)` — new/updated role × all active candidates.

Both `match_upsert_match(...)` the results (idempotent). To bound cost, a cheap pre-filter runs first: `match_engine.prefilter(candidate, role)` (Python, no LLM) drops obvious non-matches (work-mode conflict, salary gap > 40%, zero skill overlap) so the LLM only scores plausible pairs. At ~$0.0003/scored pair (Haiku), a tenant with 50 candidates × 20 open roles pre-filtered to ~150 plausible pairs is ≈ $0.05 — negligible.

### 6.3 The dual-sided chase (scheduler + promise/sequence engines)

Two background jobs, both wired into the **existing in-process scheduler** (`src/app/scheduler.py`, mirroring how `crm_sequences` is armed):

- **`match_engine.run_scoring_sweep()`** — `scheduler.run_daily(hour=6, minute=0, …)`. Re-scores candidates/roles touched in the last 24h (or on-write, if latency matters — start daily, add on-write later, same trade-off the CRM took for deal clustering). Refreshes the board.
- **The two-way chase** — **reuses the promise + sequence engines wholesale, no new loop needed.** Interview-scheduled, feedback-due, and offer-decision are all `crm_promises` with `due_hint` + `direction`. The existing gone-quiet nudge + `crm_sequences.process_sequence_queue()` (already on the 15-min `run_every` sweep via `src/app/jobs/process_sequences.py`) chase whichever side is overdue. Match seeds two default sequences (§9): a **candidate nurture** and a **client feedback chase**, so "client owes feedback, 5 days late" fires a drafted chaser automatically.

### 6.4 Drafter integration (brief both sides)

The introduction/interview/offer touchpoints call:

```python
result = await agent.compose_outbound(cfg, purpose=MATCH_INTRO_CLIENT, facts=<match facts>)
log_id, _ = db.insert_email_log(tenant_id, client_email, subject,
                                f"[match] intro", result.text, status="pending", kind="match")
usage.record_anthropic(tenant_id, result.model, result.usage, operation="match", email_log_id=log_id)
db.log_agent_event(tenant_id, "match", f"Intro drafted to {client_email}", ...)
if cfg.get("auto_send"):
    await mailbox.send_mail(tenant_id, client_email, subject, result.text)
    db.mark_email_log_sent(log_id, tenant_id, via="auto", edited=False)
```

…then the **same call again with `purpose=MATCH_INTRO_CANDIDATE`** to the candidate — that's the dual-sided part. `facts` carries the fit `reasoning` so both emails speak the *same* matching language, phrased for each audience. This is byte-for-byte the `crm_sequences._do_send_email` pattern; Match adds no new send path. In-thread replies (a candidate answering) go through `agent.draft_reply(cfg, email, …, crm_context=…)` where `crm_context` is enriched with the match state via `build_match_context()` (§8) — the Match analogue of `crm_intel.build_crm_context`.

---

## §7 API + routes

New router `app/match_routes.py` (JSON API, `require_onboarded`, RLS via `ctx.client`), mirroring `src/app/crm_routes.py`.

```
GET   /api/match/board                        — the four-rail daily surface
GET   /api/match/candidates                   — list (filter: status, skill, available_by)
POST  /api/match/candidates                   — manual add (also self-fills from email)
GET   /api/match/candidates/{id}              — detail + matches + activity + promises
PATCH /api/match/candidates/{id}
POST  /api/match/candidates/{id}/parse-cv     — (re)parse the attached CV

GET   /api/match/roles                         — list (filter: status, urgency)
POST  /api/match/roles
GET   /api/match/roles/{id}                    — detail + top-fit candidates (shortlist)
PATCH /api/match/roles/{id}

GET   /api/match/matches                       — list (filter: status, role_id, candidate_id)
POST  /api/match/matches/score                 — force a re-score for a candidate or role
GET   /api/match/matches/{id}                  — the match detail (fit + reasoning + both sides)
POST  /api/match/matches/{id}/advance          — state transition (introduce/interview/offer/place/reject)
POST  /api/match/matches/{id}/introduce        — draft-both-sides intro (returns two pending drafts)
```

`/introduce` and `/advance` compose via §6.4 and return the `email_log` draft ids so the UI can open them in the existing approval queue.

---

## §8 UI — the dual-sided matching board

Server-rendered Jinja2, matching the CRM/TMS design system. New pages under `templates/match/`:

- **`/app/match`** — the board (§2). Four rails: Today's matches · Pending replies (both sides) · Interviews · Placements. Nav entry added to `src/app/dashboard.py` and the Today card on `app/home.html` (beside the existing CRM card).
- **`/app/match/roles/{id}`** — role detail with the **shortlist**: top-fit candidates ranked by `fit_score`, each showing the fit band + a one-line reasoning excerpt, with "Introduce" buttons.
- **`/app/match/candidates/{id}`** — candidate detail: parsed CV summary, structural fields, custom fields (from the contact), and the roles they've been matched to.
- **`/app/match/matches/{id}` — the killer view.** Split screen: **candidate side** (skills, availability, salary expectation) | **role side** (requirements, salary band, work mode). Centre: the **fit score dial + the reasoning paragraph shown to both sides**. Below: the **two-way promise ledger** (we owe the client X by date; the client owes us feedback by date; the candidate owes us availability) rendered from `crm_open_promises`, and a **"Draft intro to both sides"** button.

`build_match_context(match, candidate, role, promises) -> str` produces the `<match_context>` block injected into `agent.draft_reply(crm_context=…)` so every in-thread reply already knows the match state — the Match analogue of `crm_intel.build_crm_context`.

---

## §9 Onboarding + industry seeds

Match OS is **almost entirely config** below the OS build (the point of the OS architecture — `docs/BLAM8_operating_systems.md §3`). The engineering above is the `match` primitive + scoring + board; recruitment (and the six other Match industries) then onboard as JSON seeds.

1. **`operating_systems.OPERATING_SYSTEMS`** — flip Match OS `live: True` (line ~35) once the module ships. `is_live("match")` then returns `True`, unlocking the OS-native experience.
2. **`onboarding.INDUSTRIES`** — unhide `recruitment` (remove `hidden: True`, set `live: True`; it already has `os: "match"`). Add the other Match industries as seeds: `tutoring`, `freelance`, `exec_search`, `sports_coaching`, `mentoring` (each `os: "match"`), starting `hidden` until their vocab lands.
3. **`industry_lingo.LINGO`** — recruitment seed already present (`"Recruitment & staffing: candidates, clients, roles/vacancies, CVs, shortlists, interviews, placements, perm/contract, day rate."`). Add sibling one-liners per Match industry.
4. **`industry_lingo.CRM_SEEDS["recruitment"]`** — already defines company (Sector, Headcount), contact (Role, Phone), deal (Vacancy, Salary range, Perm/Contract). These self-fill today. Add candidate-side seed fields (skills, notice period, availability) as `crm_field_definitions` on the candidate-as-contact.
5. **`onboarding.DATA_SOURCES`** — add a `recruitment` block: LinkedIn (candidate research), Calendly / your ATS, CV inbox/spreadsheet. Shapes the "live data" onboarding step.
6. **`INDUSTRY_TO_OS`** — `recruitment → match` already mapped (`operating_systems.INDUSTRY_TO_OS`). No change; keep the migration backfill CASE in step.

Onboarding otherwise reuses the shipped 2-step wizard (Shape → Industry → custom fields → voice/knowledge). A recruiter reaches "first drafted reply" on the same 5-minute path as every other tenant.

---

## §10 Module registration + plan wiring

1. **`src/app/dashboard.py:MODULES`** — add:
   ```python
   {"id": "match", "name": "Match OS · Recruitment", "group": "addon", "badge": "Recruitment",
    "href": "/app/match",
    "blurb": "The dual-sided CRM that fills itself in — candidates and clients, "
             "auto-populated from your inbox, with LLM-scored fit ratings and a "
             "placement pipeline that briefs and chases both sides."},
   ```
2. **`src/app/plans.py:INDUSTRY_MODULE_META`** — add:
   ```python
   "recruitment": {
       "key": "match", "label": "Match OS · Recruitment", "name": "Match OS · Recruitment",
       "features": ["Self-filling candidate & client CRM", "LLM-scored fit ratings",
                    "Dual-sided intro drafter", "Two-way promise tracking",
                    "Interview & placement pipeline", "CV parsing"],
   },
   ```
   `INDUSTRY_MODULE["recruitment"] = "match"` derives automatically; `MANAGED_MODULES` picks it up; `modules_for("professional", "recruitment")` then unlocks `match` on the +industry tiers.
3. **Pricing — undercut Bullhorn.** The dual-sided CRM + fit scoring rides in **Core (£99)**, exactly like the base CRM (`docs/architecture/CRM_design.md §2`) — that IS the wedge against Bullhorn's ~£200/mo. The Match OS *module* (board, pipeline, CV parsing at volume) sits on the **`professional` +industry tier (£299)** for agencies wanting the full placement operation, consistent with how Field OS · Transport prices. Net: a 1-person agency gets self-filling dual CRM + fit scoring for £99 (half of Bullhorn); a growing agency pays £299 for the full pipeline — still competitive, and with data that fills itself in.

---

## §11 OS-native integrations

| Integration | Purpose | v1 vs later | Reuse |
|---|---|---|---|
| **CV parsing** | Attached/forwarded CV (`crm_artifacts`) → skills, experience, salary, notice, availability → autofill `match_candidates`. | **v1.** Reuses the CRM's artifact-content-processing (`docs/architecture/CRM_design.md` Phase 3 artifacts) + the same Haiku extraction the custom fields use. No new vendor — PDF/docx → text → LLM. | `crm_artifacts`, `crm_extract`, `settings.anthropic_model`. |
| **Companies House** | Client (hiring company) verification + enrichment (company number, registered office, filing status). | **v1** — but it's *already on the CRM roadmap* (`docs/architecture/CRM_design.md §26`): the free Companies House API enriches new UK-Ltd companies. Match OS gets client verification for free the moment §26 ships; no Match-specific work. | `crm_companies.custom_data._enrichment` (§26). |
| **Calendly** | Interview scheduling. Webhook on booking → set `match_matches.status='interview'`, `interview_at`, and a `crm_promises` "client owes feedback by {date+2d}". | **v1** (webhook in; embed link out). Falls back to interview-time extraction from replies if a tenant doesn't use Calendly. | `crm_promises` for the feedback chase; scheduler already handles the due sweep. |
| **LinkedIn API** | Candidate research / enrichment (headline, skills confirmation). | **Later.** LinkedIn's API is heavily gated and its ToS restrict scraping — treat as a v2 enrichment source, not a v1 dependency. v1 self-fills from email + CV, which needs no LinkedIn access at all. | — |

The design principle: **v1 depends on nothing but the inbox + the CV.** Calendly and Companies House are additive and low-risk (Companies House is free + already roadmapped); LinkedIn is explicitly deferred because its API access is unreliable and its ToS are restrictive.

---

## §12 Killer feature deep-dive

### 12.1 LLM-scored fit ratings (with reasoning shown to both sides) — ⚖️ V2-GATED: DO NOT BUILD IN v1 (scope constraint, 2026-07-10)

**Features.**
- Every plausible candidate × role pair carries a `fit_score` (0.00–1.00), a `fit_band` (strong ≥ 0.75 · possible 0.50–0.74 · weak < 0.50), and a **`reasoning` string written to be shown to both sides** — the recruiter's differentiator ("here's *why* David fits") and the audit trail against bias (§14).
- Scores drive the board (rail 1 ranks by fit) and the shortlist (role detail ranks candidates by fit).
- Idempotent + cheap: pre-filter (Python) → Haiku only on plausible pairs → `match_upsert_match` upserts in place.

**The scoring prompt** (structured JSON out, `settings.anthropic_model`, `output_config` json_schema — same shape as `crm_intel.analyze` and `agent.compose_outbound`):

```
You are scoring how well a candidate fits a role for a UK recruiter.
Score the FIT, not the person's worth. Be specific and evidence-based.

## Role
Title: {role.title}
Requirements: {role.requirements}   Salary: {role.salary_min}–{role.salary_max} {ccy}
Work mode: {role.work_mode}   Engagement: {role.engagement}   Urgency: {role.urgency}
Brief: {role.requirements_text}

## Candidate
Headline: {candidate.headline}
Skills: {candidate.skills}   Experience: {candidate.experience_years}y
Salary expectation: {candidate.salary_expectation} {ccy}   Notice: {candidate.notice_period}
Availability: {candidate.availability}   Work-mode pref: {candidate.work_mode_pref}
Location: {candidate.location}   CV summary: {candidate.cv_summary}

## Instructions
- Weigh: skills/requirements overlap, seniority, salary alignment, work-mode/location
  fit, availability vs urgency.
- IGNORE and never mention: name, gender, age, ethnicity, nationality beyond stated
  right-to-work, or any protected characteristic. Score on capability + logistics only.
- The `reasoning` will be shown to BOTH the candidate and the client — write it so it
  is fair, professional, and useful to each. 2–3 sentences.

Return JSON: {"fit_score": 0.0-1.0, "fit_band": "strong|possible|weak",
              "reasoning": "..."}
```

**Thresholds.** `fit_band` derives from score at 0.75 / 0.50 — the *same* cut points as the CRM confidence gate (`crm_fields.AUTOFILL_AT`/`SUGGEST_AT`), for consistency. Only `strong`/`possible` matches surface on the board by default (weak stays queryable but out of the way). A `strong` match with an urgent role sets `next_action_at = today` (works its way to the top of rail 1).

**Reasoning shown to both sides — the mechanic.** The single `reasoning` string is stored once and injected into *both* intro drafts (§6.4) via the `facts` arg. The candidate sees "why this role suits you"; the client sees "why this candidate fits" — same underlying reasoning, re-voiced by the drafter for each audience in the tenant's tone. No other agency tool shows the *candidate* a fair, written rationale for why they were put forward.

### 12.2 Two-way promise tracking mechanics

Reuses `crm_promises` unchanged — its `direction` column is already `we_owe`/`they_owe`, which is *exactly* dual-sided. Match OS just populates both directions per match and adds due dates:

| Stage | Promise (direction) | Due | Chased by |
|---|---|---|---|
| Introduction | client owes a shortlist decision (`they_owe`) | +3 business days | gone-quiet nudge / client sequence |
| Interview scheduled | interview happens (both) · client owes feedback (`they_owe`) | interview_at / +2d | Calendly webhook + feedback sequence |
| Feedback | candidate owes their decision (`they_owe`) · we owe the client next steps (`we_owe`) | +2d | both sequences |
| Offer | client owes offer decision (`they_owe`) · we owe candidate the offer terms (`we_owe`) | +3d | chase both |
| Placement | we owe the client an invoice (`we_owe`, → CRM invoicing if §5.1 on) · we owe a guarantee-period check-in | fee terms / +30d | scheduler |

Each promise is created via `db.crm_insert_promise(direction=…, due_hint=…, deal_id=<role's deal>, contact_id=<the owing side>)`, auto-resolved when satisfied in-thread (`crm_intel.apply_promises` already does this, `RESOLVE_CONFIDENCE = 0.75`), and rendered in the match detail's two-way ledger. **The dual-sided chase is therefore free** — it's the promise engine BLAM8 already shipped, pointed at both parties.

---

## §13 Build order

Each phase independently shippable. Estimates assume single-dev + LLM assistance, on top of the shipped CRM.

- **Phase M0 — Config unlock (¼ day).** Flip `operating_systems` Match `live`, unhide `recruitment` in onboarding, add `DATA_SOURCES["recruitment"]`, add the `MODULES` + `plans.py` entries. Recruiters can already onboard onto the self-filling CRM with recruitment vocab — value before any Match code ships.
- **Phase M1 — The primitive + intake (1 week).** `db/schema.sql` block + `scripts_apply_match_migration.py`; `match_*` db functions; `match_engine.classify_email` intake wiring (candidate vs role, structural extraction); candidate/role detail pages. No scoring yet — just a dual-sided self-filling CRM.
- **Phase M2 — The board + manual pairing (½–1 week).** The four-rail board (v1 ordering: `next_action_at`, never AI-derived) + "Match with…" manual pairing from candidate/role pages + match detail view. **⚖️ The fit-scoring half of the original M2 (`score_candidate_role`, prefilter, `run_scoring_sweep`, shortlist) is V2-GATED — do not build (scope constraint).**
- **Phase M3 — Dual-sided drafter + two-way chase (½–1 week).** `/introduce` (draft both sides), the two default sequences (candidate nurture + client feedback chase), promise wiring per stage. Mostly reuse — the sequence/promise engines already exist.
- **Phase M4 — Integrations (1 week, parallelisable).** CV parsing (v1, reuses artifacts+extract), Calendly webhook, Companies House (free-riding on CRM §26). LinkedIn deferred.

**Total: ~3–4 weeks for v1** (the scoring week is gated out), consistent with `docs/BLAM8_operating_systems.md §8`'s "~4–6 weeks per OS". M0 ships day one; M1–M3 deliver the demonstrable product — "the recruitment CRM that fills itself in", not "the AI that ranks candidates".

---

## §14 Open questions + risks

- **Candidate data / GDPR (highest).** Candidates are individuals whose personal data (CV, salary, contact) BLAM8 stores and processes. Needs: a lawful basis (legitimate interest / consent), a retention policy (auto-dormant + purge unplaced candidates after N months — reuse the retention purge pattern already in `src/app/scheduler.py`), a right-to-erasure path (cascade delete `match_candidates` → `crm_contacts`), and candidate-facing transparency (the reasoning-shown-to-both-sides feature *helps* here). Do not ship M1 without at least the retention + erasure story. Recruiter's own DPA obligations apply — surface this in onboarding.
- **EU AI Act Annex III 4(a) (RESOLVED 2026-07-10 — the governing risk).** Candidate scoring/ranking/filtering = HIGH-RISK from 2 Aug 2026 (QMS, technical documentation, conformity assessment, CE marking, EU-database registration, post-market monitoring; deployer duties on tenants; extraterritorial hook defeats "UK-only"). **Decision: v1 ships no candidate evaluation whatsoever** — see the scope constraint at the top of this doc. Scoring is v2, gated on a design partner + final Commission guidance + Julius's explicit sign-off on the compliance bill.
- **Bias in fit scoring (v2-gated, high if ever built).** LLM scoring can encode bias. Mitigations already spec'd in §12.1: the prompt explicitly forbids scoring on protected characteristics; the `reasoning` is auditable and shown to the candidate (a natural check); scores advisory, never auto-reject, never *hide* candidates. If v2 happens, these become mandatory Annex III controls, not nice-to-haves — plus a periodic fairness audit and legal review before GA.
- **Score latency vs cost** — daily sweep (cheap, delayed) vs on-write (fresh, pricier). Start daily (M2), add on-write for urgent roles if recruiters ask. Same trade-off the CRM took for deal clustering (`docs/architecture/CRM_design.md §14`).
- **Candidate ↔ contact identity** — a candidate might also be a client's hiring manager elsewhere. Model: one `crm_contacts` row, which may have both a `match_candidates` row and be a company contact. The `unique(tenant_id, contact_id)` on candidates keeps it clean.
- **Consumer-side Match industries** (tutoring, matchmaking) pair two *individuals*, neither of whom is a "company." The `role` still works (a "learning need" is a deal), and both sides are contacts — no schema change, but the vocabulary/labels differ (industry seed job, not code).
- **LinkedIn reliability** — deferred precisely because it's a risk; v1 owes it nothing.

---

## §15 Reuse map

| Concern | REUSE (name it) | BUILD NEW |
|---|---|---|
| Client company | `crm_companies` + Companies House enrichment (`docs/architecture/CRM_design.md §26`) | — |
| Hiring-manager contact | `crm_contacts` | — |
| Self-filling records | `src/app/crm.py` (freemail/stated-company inference), `src/app/crm_extract.py`, `src/app/crm_fields.py` (gate 0.75/0.50) | `match_engine.classify_email` (candidate-vs-role decision) |
| Custom fields (candidate/role) | `crm_field_definitions` / `crm_field_suggestions`, recruitment `CRM_SEEDS` | candidate-side seed fields |
| Candidate (supply) | underlying `crm_contacts` row | `match_candidates` table + db fns |
| Role (demand) | underlying `crm_deals` row + `crm_stages` pipeline | `match_roles` table + db fns |
| **The match + fit score** | — | **`match_matches` table + `score_candidate_role` (the OS primitive)** |
| Two-way promises | `crm_promises` (`we_owe`/`they_owe`), `crm_intel.apply_promises`, auto-resolve | per-stage promise wiring |
| Chase / nurture | `crm_sequences` engine, `process_sequence_queue`, gone-quiet nudges | 2 default sequences (candidate + client) |
| Drafter (brief both sides) | `agent.compose_outbound` / `agent.draft_reply` (+ `crm_context`), tenant voice/config | `build_match_context`, MATCH_INTRO_* purposes |
| Outbound persist/send | `db.insert_email_log` → `usage.record_anthropic` → `mailbox.send_mail` if `auto_send` (the `crm_sequences._do_send_email` / `send_service_reminders` pattern) | — |
| Scheduler | `src/app/scheduler.py` `run_daily` / `run_every` | `run_scoring_sweep` job |
| CV parsing | `crm_artifacts` + `crm_extract` (Haiku) | CV-specific extraction prompt |
| Interview scheduling | `crm_promises` due-hint chase | Calendly webhook handler |
| Multi-tenant isolation | RLS + `auth_tenant_ids()` (identical policy shape) | policies on 3 new tables |
| Today surface | `src/app/dashboard.py:app_home` CRM-card pattern | Match board + card |
| Module registry / plans | `dashboard.MODULES`, `plans.INDUSTRY_MODULE_META`, `modules_for` | one entry each |
| OS layer / onboarding | `operating_systems`, `onboarding` 2-step wizard, `industry_lingo`, `INDUSTRY_TO_OS` | flip `live`, unhide recruitment, `DATA_SOURCES` block |
| Migration convention | `scripts/db/migrations/apply_crm_migration.py` (pooler + `.env` + `db/schema.sql` marker slice, idempotent) | `scripts_apply_match_migration.py` |

**The whole novel surface is three tables (`match_candidates`, `match_roles`, `match_matches`), one engine module (`match_engine.py` = classify + score + sweep), one router, and one board.** Everything else — the self-filling CRM, extraction, promises, sequences, drafter, voice, scheduler, RLS, plans, onboarding — is reused from Core.
