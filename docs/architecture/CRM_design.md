# BLAM8 CRM — Design & Build Spec

**Status:** Phases 1–4 shipped (4a reporting + tasks, 4b follow-up sequences). Phase 5 (adjacencies) roadmap defined; Phase 5.1 on hold pending the Invoice add-on.
**Author:** Julius Deane, w/ Claude (Dispatch synthesis)
**Last updated:** 2026-07-05

## 1. Vision

**"The CRM that fills itself in."** BLAM8 already reads every inbound email, drafts every outbound reply, and knows the tenant's voice + business context. That means it's already touching 100% of the data any CRM would ask a user to type. The CRM module surfaces what BLAM8 has been collecting all along.

Every existing CRM (Salesforce, HubSpot, Pipedrive, Attio) has the same broken promise: "keep your data up to date and we'll help you." Every one of them is 60-80% empty within 90 days because nobody types after every call. BLAM8's version inverts the trade: "keep working the way you always have — using email. The CRM writes itself." No other product in market can honestly make that claim.

## 2. Positioning

Included in **Core (£99/mo)**. Not an upsell. Rationale: CRM is the sharpest single-line differentiator BLAM8 will ever have. Paywalling it into a higher tier hides the killer feature. Include it in Core, price vertical modules (TMS, Trades, Voice, etc.) higher. Core becomes 3x stickier without changing price.

**Update 2026-07-24 (Julius's tier decision, docs/crm-vs-os-architecture.md §6):**
"included in Core" now means exactly that — the CRM is the `crm` switchboard module,
bundled from Core up, and a real **unlock** relative to the £39 Email tier (which is
email-first: no CRM surfaces, no CRM LLM costs; only the zero-LLM ingest keeps
pre-filling so upgrade day lands on a full CRM). "Not an upsell" continues to mean
never OS-gated.

## 3. The six base entities

Skeleton stays fixed. Custom fields ride on top (phase 2).

- **Company** — atom of the CRM. Deliberately NOT Contact. Multiple people at one company should roll up. Inferred from email domain.
- **Contact** — a person, unique by email address, belongs to Company.
- **Deal** — an opportunity clustered around a subject/theme. Multiple threads can belong to one deal; one contact/company can have multiple deals.
- **Activity** — an email (from `email_log`), a calendar event (from BLAM8's scheduling), a voice call (future). Rolled up per contact / deal.
- **Stage** — per-tenant configurable pipeline stages. Defaults: Prospect → Qualifying → Negotiating → Won / Lost / Dormant.
- **Artifact** — attachments / referenced documents flowing through email, tagged to deals. Phase 1: recognize the link. Phase 3: process content.

## 4. Custom fields (phase 2)

Every business tracks slightly different things. The onboarding wizard (extending BLAM8's existing scheduling onboarding pattern) asks tenants what else they want to track — per Company, per Contact, per Deal. Vertical-seeded defaults suggested by industry (logistics: fleet_size, depot_count; trades: van_count, service_area; wholesale: credit_limit, payment_terms; etc.).

The killer part: **the LLM autofills custom field values from emails.** Tenant defines a field ("fleet_size", type=number, extraction_hint="mentions of fleet size, van count, vehicle numbers"). Every incoming email is checked against tenant's custom_field_definitions. When a prospect writes "we run 12 vans out of two depots," fleet_size=12, depot_count=2 land automatically. Confidence-gated: high confidence = autofill, medium = suggested value for one-click confirm, low = skip.

Nobody else can do this — every other CRM's custom fields still need manual data entry. BLAM8's don't.

## 5. Phase breakdown

**Phase 1 — Skeleton — ✅ SHIPPED** (merged `26b442d`, 2026-07-04 → 2026-07-05)

- Schema + migration for six entities
- Backfill from existing `email_log` (contacts, companies, activities)
- Minimal CRM router (list contacts, get contact detail, list companies, list deals, pipeline view)
- One killer view: contact detail page at `/app/crm/contacts/{id}`
- Nav entry in the app
- Multi-tenant isolation via existing RLS patterns
- Tests, full suite green
- `custom_data` JSONB column present but unused (leaves the seam for phase 2)

**Phase 2 — Custom fields + LLM extraction — ✅ SHIPPED** (merged `26b442d`, 2026-07-04 → 2026-07-05)

- `custom_field_definitions` table
- Onboarding wizard extension (per-entity field definition step, vertical-seeded)
- LLM extraction pipeline: on every processed email, check tenant's schema and fill values
- Confidence gate on autofill
- Live preview in wizard using tenant's last 5 emails
- UI for viewing / editing custom field values on entity cards

**Phase 3 — Intelligence — ✅ SHIPPED** (merged `26b442d`, follow-on fixes `bd6ba90` + `7871ef6`, 2026-07-04 → 2026-07-05)

- Deal auto-clustering (LLM tags emails/threads into deals)
- Promise tracking (extract commitments made in outgoing emails, track fulfillment)
- Cold-lead nudges ("Alison went quiet 12 days ago — want me to draft a check-in?")
- Drafter stage-awareness (every reply BLAM8 drafts knows the deal stage, promises, context)
- Pipeline kanban UI
- Artifact content processing (PDFs, contracts auto-tagged)
- Voice module integration (pre-call briefing: "You're calling Rob — here's the context")
- CRM Settings tab (stages editor, gone-quiet threshold, exports) added in follow-on `bd6ba90` / `7871ef6`

All three phases are merged to `main` and deployed to production. Branch tree is clean (12 feature branches deleted post-merge). End-to-end behaviour verified live on a real inbound — see § 17.

### § 5.5 Phase 3.5 — ✅ SHIPPED (post-launch fixes)

Surfaced during the live proof-of-work run (§ 17). Real known issues, ordered by demo impact. **All four shipped in commit `2045c2d` (merged to main, 2026-07-05).**

1. **Freemail-sender Company records.** ✅ **SHIPPED** (`2045c2d`, 2026-07-05). *(Biggest demo miss.)* When a sender uses a freemail address (gmail, outlook, etc.), no Company record is created because inference is domain-only. Consequence: company-scoped custom fields (Trade, van count) can't autofill even when extraction already has the values. Fix: create a Company record from the *stated* company name in the email body (LLM-extracted), not just the email domain. Domain-only inference stays for corporate senders; stated-name inference kicks in for freemail.
2. **Required-field probing (“High requirement” fields).** ✅ **SHIPPED** (`2045c2d`, 2026-07-05). The `required` checkbox exists in schema and UI but drives no behaviour. Add **High-requirement fields**: the agent probes conversationally in the next drafted reply until the value is collected. Concrete failure from the live test — the full name "David" was lost to the sign-off "Dave" because there was no probing logic to capture it.
3. **Promise semantic dedupe.** ✅ **SHIPPED** (`2045c2d`, 2026-07-05). Restated commitments stack as duplicate promises. Fix: pass open promises into the intel/extraction call the same way open deals are already passed, so the LLM knows what's already tracked.
4. **Auto-resolve promises when satisfied in-thread.** ✅ **SHIPPED** (`2045c2d`, 2026-07-05). Extension of #3 — when a customer follows through in a later email (attaches the quote, sends the NDA, etc.), the corresponding promise flips to resolved without user intervention.

### § 5.6 Phase 4 — Table-stakes gaps (✅ SHIPPED)

Absent from the original spec but non-negotiable for competitive parity (verified against Copper, Streak, Attio, folk, Nimble, HubSpot Breeze, Pipedrive, Capsule, Salesforce, Jobber). See § 16 for the positioning read.

- **Reporting dashboards.** ✅ SHIPPED (Phase 4a, `00e1bae`, 2026-07-05). Table stakes across every CRM.
- **Task management.** ✅ SHIPPED (Phase 4a, `00e1bae`, 2026-07-05). Table stakes across every CRM.
- **Multi-step automated follow-up sequences.** ✅ SHIPPED (Phase 4b, `40b9194`, 2026-07-05). Natural extension of BLAM8's gone-quiet nudges — conditions + branching + multi-touchpoint outreach on top of the existing nudge primitive. Puts BLAM8 ahead in a category incumbents charge extra for.
- **Native mobile app — LOW priority.** Forbes-cited buyer research weights mobile at ~1% of the CRM purchase decision. Deprioritize.
- **Scoping note (Trades vertical):** the Trades module needs a job-workflow (quoting / scheduling / invoicing, à la Jobber), NOT a sales pipeline. Relevant whenever the Trades module gets attention.

### § 5.7 Phase 5 — CRM adjacencies (deal-to-money + channel expansion)

Once Phase 4 (table-stakes gaps) ships, Phase 5 layers the differentiating adjacencies that turn BLAM8's CRM from best-in-class relationship intelligence into a business's whole revenue nervous system. Six modules, sequenced by leverage.

> **Numbering note.** These six modules were drafted as §§ 18–24, but §§ 18–21 were already taken by the Phase 3.5 / Phase 4 implementation specs (commit `c18e1ed`). The Phase 5 module specs are therefore appended at the tail of this doc as **§§ 22–28**; the build-order references below point at those renumbered sections.

Recommended build order:
1. **Proposals + invoicing loop-close (§ 22)** — highest impact; closes deal-to-money story. Do this first.
2. **WhatsApp / SMS channels (§ 23)** — unlocks Trades vertical.
3. **Voice call transcription (§ 24)** — requires voice module (separate roadmap item).
4. **Referral tracking (§ 25)** — small feature, big trust signal.
5. **Native enrichment (§ 26)** — small feature, competitive parity.
6. **Post-sale customer mode (§ 27)** — config-level, lifecycle completion.

Each module ships independently. § 22 alone justifies a full Phase 5.1 release.

## 6. Schema sketch (phase 1)

```sql
companies (
  id uuid primary key,
  tenant_id uuid not null references tenants(id),
  name text,
  domain text,
  first_seen_at timestamptz default now(),
  last_activity_at timestamptz,
  custom_data jsonb default '{}'::jsonb,  -- reserved for phase 2
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

contacts (
  id uuid primary key,
  tenant_id uuid not null references tenants(id),
  company_id uuid references companies(id),
  email text not null,
  name text,
  first_seen_at timestamptz default now(),
  last_activity_at timestamptz,
  custom_data jsonb default '{}'::jsonb,
  created_at timestamptz default now(),
  updated_at timestamptz default now(),
  unique(tenant_id, email)
);

deals (
  id uuid primary key,
  tenant_id uuid not null references tenants(id),
  company_id uuid references companies(id),
  primary_contact_id uuid references contacts(id),
  title text,
  stage_id uuid references stages(id),
  value_estimate numeric,
  currency text default 'GBP',
  opened_at timestamptz default now(),
  closed_at timestamptz,
  status text default 'open',  -- open | won | lost | dormant
  custom_data jsonb default '{}'::jsonb,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

stages (
  id uuid primary key,
  tenant_id uuid not null references tenants(id),
  name text not null,
  display_order integer default 0,
  stage_type text default 'active',  -- active | won | lost | dormant
  created_at timestamptz default now()
);

activities (
  id uuid primary key,
  tenant_id uuid not null references tenants(id),
  contact_id uuid references contacts(id),
  company_id uuid references companies(id),
  deal_id uuid references deals(id),
  activity_type text not null,  -- email | calendar_event | voice_call
  source_id text,  -- email_log id, calendar event id, etc.
  direction text,  -- inbound | outbound
  subject text,
  summary text,
  occurred_at timestamptz not null,
  created_at timestamptz default now()
);

artifacts (
  id uuid primary key,
  tenant_id uuid not null references tenants(id),
  deal_id uuid references deals(id),
  contact_id uuid references contacts(id),
  activity_id uuid references activities(id),
  artifact_type text,  -- attachment | referenced_doc
  filename text,
  content_type text,
  url_or_path text,
  created_at timestamptz default now()
);
```

Row-level security on every table, tenant_id predicate. Additive migration only.

## 7. API surface (phase 1)

```
GET  /api/crm/contacts                     — paginated list, filterable
GET  /api/crm/contacts/{id}                — detail with activity feed
GET  /api/crm/companies                    — paginated list
GET  /api/crm/companies/{id}               — detail with contacts + deals
GET  /api/crm/deals                        — paginated list
GET  /api/crm/deals/{id}                   — detail with activity feed
GET  /api/crm/pipeline                     — deals grouped by stage
GET  /api/crm/stages                       — tenant's stages
POST /api/crm/stages                       — add stage (phase 1 minimal, phase 2 in wizard)
```

Phase 2 adds: `/api/crm/custom-fields/*`, `/api/crm/nudges/*`
Phase 3 adds: `/api/crm/promises/*`, `/api/crm/deal-detection/*`

## 8. The contact detail page (phase 1 killer view)

Route: `/app/crm/contacts/{id}`

Layout (server-rendered Jinja2, matches BLAM8 design system):

```
[Contact name]
[Email] · [Company name]  · [Role — blank in phase 1, filled by LLM in phase 2]

Custom fields section  — empty placeholder in phase 1, populates in phase 2

Current deals
  [Deal title] · [Stage] · [Est. value] · [Days open]
  [Deal title] · [Stage] · [Est. value] · [Days open]

What you owe them / What they owe you
  [Phase 1: "Coming soon — populated as BLAM8 learns your workflow"]
  [Phase 3: derived from promise tracking]

Recent activity (last 20)
  [Date] · Inbound email · [Subject] · [Summary excerpt]
  [Date] · Outbound email · [Subject] · [Summary excerpt]

Last contact: [X] days ago  [visual indicator if >14 days]

[Draft check-in →]  button linking to existing draft flow
```

## 9. Onboarding wizard extension (phase 2)

Extends existing scheduling onboarding. Three sub-steps, one per entity (Company, Contact, Deal). Each shows 3-5 vertical-seeded suggestions the tenant can accept, edit, or skip.

Per custom field the tenant defines:

- `label` (display name)
- `field_type` (text | number | dropdown | date | bool)
- `options` (for dropdown)
- `required` (bool)
- `extraction_hint` (natural language — what to look for in emails)

At wizard end: live preview using tenant's last 5 inbound emails. "Based on emails from X, Y, Z, here's what BLAM8 would auto-fill." This preview is the sales moment.

Vertical seeds live in `src/app/industry_lingo.py` with a new `crm_seeds` block per industry.

## 10. LLM extraction (phase 2)

On every processed email:

1. Load tenant's `custom_field_definitions` for the relevant entity (or all three)
2. Compose prompt: "Extract values for these fields from this email. Fields: [definitions with hints]. Return JSON with field_key: {value, confidence}."
3. Anthropic Haiku 4.5 (same model already used for drafting — no new spend)
4. Post-process: high confidence → autofill entity's `custom_data`, medium → mark as suggested (surface in UI for one-click confirm), low → skip
5. Log to audit trail

Cost estimate: ~$0.0002 per email at Haiku pricing. Trial cap (100 emails) = ~$0.02 max per trial tenant. Negligible.

## 11. Guardrails (phase 2)

- **Field cap:** max 10 custom fields per entity per tenant in v1. Prevents onboarding-anxiety schema bloat.
- **Dead-field detection:** field with no autofilled or manually-set value in 90 days across all records → flag for removal.
- **Type migration:** if tenant realizes fleet_size should be dropdown not number, allow safe conversion (keep raw_value, add parsed_value).
- **Confidence threshold:** default 0.75 for autofill, 0.5-0.75 for suggested, <0.5 skip. Configurable per tenant.

## 12. Universe integration (long-term)

BLAM8's CRM becomes the relationship layer of the whole HAAIL universe:

- Sanctum queries it for context ("what did I promise Rob last month?")
- TravassAi queries it before booking client meetings (cuisine preferences from past dinners)
- OurTab can attribute client-project income against deals
- Voice module briefs pre-call context

The CRM is the shared identity of every person the user interacts with, across every agent. That's not a side effect. That's the deeper play.

## 13. Sequencing recommendation

Ship in three sessions:

- **Session 1 (Phase 1):** Skeleton + backfill + contact detail page. Julius has visible progress in ~a working day.
- **Session 2 (Phase 2):** Custom fields + LLM extraction + wizard. This is the "fills itself in" magic.
- **Session 3 (Phase 3):** Intelligence layer (deal clustering, promises, nudges, drafter integration). This is where BLAM8 becomes a business partner, not just a tool.

Each phase is shippable independently. Phase 1 alone provides value (BLAM8 has a working CRM view). Phases 2 and 3 layer magic on top.

## 14. Open questions

- **Deal cluster detection latency** — LLM cluster threads on write (cheap, delayed) or nightly re-cluster (accurate, more expensive). Start with on-write, add nightly if needed.
- **Company deduplication when the domain is shared** — Gmail addresses, generic domains. Phase 2 problem.
- **Multi-contact deals** — one deal at Acme, three people involved. Handle via primary_contact_id + a deal_participants join table if needed. Defer to phase 3.
- **Cold-lead nudge cadence** — 7 days? 14? Per-tenant configurable? Default 14, make configurable in phase 3.
- **Export path** — BLAM8's brand promise includes "yours to export." CRM must expose full export in whatever format (CSV, JSON). Phase 3 at latest.

## 15. Success metrics

Phase 1 delivered when:

- Tenant can log in and see auto-populated contacts, companies, activities from their historical email_log
- Contact detail page renders correctly for a real tenant with real data
- Full test suite green
- Migration ships cleanly on production

Phase 2 delivered when a new tenant can:

- Complete CRM onboarding in <90 seconds
- See a live preview of what BLAM8 would autofill from their real inbox
- Observe autofill happening in real-time as new emails arrive

Phase 3 delivered when the drafter's replies visibly reflect deal stage, promises, and relationship context — provable in a side-by-side test.

## 16. Competitive positioning

Findings from a competitive scan across Copper, Streak, Attio, folk, Nimble, HubSpot Breeze, Pipedrive, Capsule, Salesforce, Jobber, and others.

**The moat holds — and is stronger than initially framed.** No competitor does unattended, whole-inbox creation of contacts and deals. HubSpot and Pipedrive auto-logging requires 5+ prerequisites (the contact must already exist, email must be forwarded, etc.) before anything is captured. Copper / Streak / Attio auto-*enrich* records a human already created, and they enrich from firmographic and metadata sources — not from LLM extraction of deal content and promises out of message bodies. BLAM8's deal auto-clustering, two-way promise tracking, and gone-quiet nudges have no equivalent in the landscape.

**Lead with autofill, not the drafter.** AI drafting is now parity across the market (Capsule ships it free at $18/seat). The differentiator is the unattended relationship intelligence — records, deals, and promises filling themselves in — not reply generation. Messaging should reflect this. The "CRM that fills itself in" one-liner (Section 1) remains the right hook; keep it from being overshadowed by drafter talk.

**Automation-in-Core is a pricing dislocation.** Copper, Pipedrive, Jobber, and Capsule all gate follow-up sequences, auto-tasks, and workflow rules to mid-tier plans or above. BLAM8 bundling automation into Core (£99/mo) is a genuine value dislocation and worth making explicit in marketing copy.

## 17. Reference demo — Pearson Plumbing

A real inbound email, processed end-to-end and fully unattended, confirming the phase 1–3 pipeline in production.

Dave Pearson (Pearson Plumbing) emailed in. From that single message BLAM8:

- **Autofilled the Contact** — including role = owner, identified from the message.
- **Inferred the Company** (subject to the freemail limitation logged in § 5.5 #1 — Dave wrote from a gmail address).
- **Auto-created a £99 Prospect Deal** clustered around the enquiry.
- **Logged a promise** extracted from the email content.
- **Produced a stage-aware draft reply** reflecting the deal stage and context.

No manual data entry at any step. This is a repeatable outbound demo — a real inbound turning into a populated CRM record set in front of the prospect.

---

## 18. Phase 3.5 — Implementation spec

Four post-launch fixes surfaced during the Pearson Plumbing live run (§ 17). Each is scoped to a single session, independently shippable. Ordered by demo impact.

### 18.1 Freemail-sender Company inference

**Problem:** Domain-only inference fails for gmail/outlook/yahoo senders. No Company record → company-scoped custom fields (trade, van_count) have nowhere to land.

**Solution:** Dual-path company resolution.

```
on_email_processed(email):
  sender_domain = extract_domain(email.from_address)

  if sender_domain NOT in FREEMAIL_DOMAINS:
    # Path A — corporate sender (existing behaviour)
    company = get_or_create_company(domain=sender_domain, tenant_id)
  else:
    # Path B — freemail sender (NEW)
    stated_company = llm_extract_company_name(email.body, email.signature)
    if stated_company and confidence >= 0.7:
      company = fuzzy_match_or_create_company(
        name=stated_company, domain=None, tenant_id
      )
    else:
      company = None  # genuinely unknown — leave unlinked
```

**Schema change:** None. `companies.domain` is already nullable. Add index:

```sql
CREATE INDEX idx_companies_name_tenant
  ON companies (tenant_id, lower(name));
```

**Fuzzy matching:** Levenshtein distance ≤ 2 OR token-set overlap ≥ 0.85 between `stated_company` and existing `companies.name` for the same tenant. Prevents "Pearson Plumbing" / "Pearson Plumbing Ltd" / "Pearson plumbing" from creating three records.

**FREEMAIL_DOMAINS list:** Hardcoded set (~40 domains: gmail.com, googlemail.com, outlook.com, hotmail.com, yahoo.com, icloud.com, aol.com, protonmail.com, etc.). Store in `app/constants.py`. No API call needed.

**LLM prompt (company name extraction):**

```
Extract the company name from this email. Look in:
1. Email signature block
2. Self-introduction ("I run Pearson Plumbing")
3. Company references in body ("our company", "we at X")

Return JSON: {"company_name": "...", "confidence": 0.0-1.0}
If no company name is identifiable, return {"company_name": null, "confidence": 0.0}
```

Model: Haiku 4.5 (same as custom field extraction — no new spend).

**Files touched:**
- `src/app/crm.py` — add `resolve_company_for_freemail()`, update `process_email_for_crm()`
- `app/constants.py` — add `FREEMAIL_DOMAINS` set
- `migrations/` — add name index migration

**Tests:**
- Freemail sender with stated company → Company created, contact linked
- Freemail sender, company already exists (fuzzy match) → linked, no duplicate
- Freemail sender, no company identifiable → contact created, company_id=None
- Corporate sender → existing path unchanged

---

### 18.2 High-requirement field probing

**Problem:** `required=True` on a custom field definition drives no behaviour. Fields marked required stay empty if the first email doesn't contain the value. The agent never asks.

**Solution:** Introduce a probing layer in the draft pipeline. When BLAM8 drafts a reply and the contact/company has unfilled required fields, the drafter weaves a natural question into the reply.

**Concept: "High-requirement" fields**

Not every required field should trigger probing (some are internally required for reports but shouldn't be asked outright). New column:

```sql
ALTER TABLE custom_field_definitions
  ADD COLUMN probe_behaviour text DEFAULT 'passive';
  -- passive: extract only, never ask
  -- probe_once: ask once if missing after first exchange
  -- probe_persistent: ask in every draft until filled (max 3 attempts)
```

**Draft integration:**

```python
def enrich_draft_context(contact_id, tenant_id):
    """Called by the drafter before composing a reply."""
    missing = get_missing_probeable_fields(contact_id, tenant_id)
    if not missing:
        return {}

    return {
        "probe_instructions": build_probe_prompt(missing),
        "probe_fields": [f.label for f in missing],
        "attempt_count": get_probe_attempts(contact_id, missing)
    }
```

**Probe prompt injected into drafter system message:**

```
The following information is still missing for this contact. If natural
and conversational, weave ONE question into your reply to collect it.
Do NOT make the email feel like a form. Do NOT ask more than one thing.
Pick the highest-priority missing field.

Missing fields (priority order):
- {field.label}: {field.extraction_hint} (asked {n} times before)
```

**Attempt tracking:**

```sql
CREATE TABLE crm_probe_log (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  contact_id uuid NOT NULL REFERENCES contacts(id),
  field_definition_id uuid NOT NULL REFERENCES custom_field_definitions(id),
  attempt_number integer NOT NULL DEFAULT 1,
  draft_id uuid,  -- links to the draft that carried the probe
  resolved boolean DEFAULT false,
  created_at timestamptz DEFAULT now()
);
```

**Max attempts:** `probe_once` = 1 attempt. `probe_persistent` = 3 attempts max. After max attempts, field moves to `passive` behaviour for that contact (stops asking).

**Files touched:**
- `src/app/crm_fields.py` — add `get_missing_probeable_fields()`, `build_probe_prompt()`
- `src/app/crm_extract.py` — on successful extraction, mark probe as resolved
- `app/drafting.py` (or equivalent) — call `enrich_draft_context()` before composing
- `migrations/` — `probe_behaviour` column + `crm_probe_log` table
- Onboarding wizard — expose probe_behaviour toggle per field (default: `passive`)

**Tests:**
- Draft for contact with missing probe_once field → probe instruction present
- Draft for contact with all fields filled → no probe instruction
- Third attempt on probe_persistent field → probe stops, logged as exhausted
- Inbound reply fills the value → probe_log marked resolved, field populated

---

### 18.3 Promise semantic deduplication

**Problem:** When a commitment is restated across emails ("I'll send the quote Monday" → "Just confirming — quote coming Monday"), the system creates duplicate promise records. Clutter in the UI, inaccurate promise counts.

**Solution:** Pass existing open promises into the intel extraction call so the LLM can match against them.

**Current flow (broken):**

```
email → LLM extraction → new promise created (no awareness of existing)
```

**Fixed flow:**

```
email → load open promises for contact/deal → LLM extraction with context →
  if matches existing promise: reinforce (update last_seen_at, bump confidence)
  if new commitment: create new promise
```

**Prompt addition to `crm_intel.py` extraction call:**

```
## Existing open promises for this contact/deal:
{json list of open promises with id, description, created_at}

When extracting commitments from this email:
- If a statement restates or confirms an existing promise, return:
  {"action": "reinforce", "existing_promise_id": "...", "updated_description": "..."}
- If it's a genuinely new commitment, return:
  {"action": "create", "description": "...", "direction": "inbound|outbound"}
```

**Schema change:**

```sql
ALTER TABLE crm_promises
  ADD COLUMN last_seen_at timestamptz DEFAULT now(),
  ADD COLUMN mention_count integer DEFAULT 1;
```

On reinforce: `last_seen_at = now()`, `mention_count += 1`. Description updates only if the LLM returns a more specific version (longer or adds a date).

**Dedup confidence gate:** If the LLM says "reinforce" but returns low confidence (<0.6), create a new promise anyway and flag it for manual merge in the UI. Better to have a duplicate than silently swallow a distinct commitment.

**Files touched:**
- `src/app/crm_intel.py` — modify promise extraction to include existing promises in context
- `migrations/` — add `last_seen_at`, `mention_count` columns

**Tests:**
- Same promise restated → single record, mention_count=2
- Similar but distinct promise → two records created
- Low-confidence match → new record created + merge flag

---

### 18.4 Auto-resolve promises on fulfilment

**Problem:** When a contact fulfils a promise in a later email (sends the quote, attaches the NDA, provides the fleet size), the promise stays open. Manual resolution only.

**Solution:** Extend the promise extraction pass to check for fulfilment signals.

**Prompt addition (appended to the promise-aware extraction in 18.3):**

```
## Resolution detection
For each existing open promise, also check if this email FULFILS it:
- Outbound promise ("I'll send the quote") → resolved if this outbound email
  contains/attaches the promised item
- Inbound promise ("I'll get back to you by Friday") → resolved if this inbound
  email delivers what was promised

Return for each resolved promise:
{"action": "resolve", "existing_promise_id": "...", "resolution_evidence": "..."}
```

**Auto-resolve logic:**

```python
def process_promise_resolution(result, tenant_id):
    for item in result:
        if item["action"] == "resolve" and item.get("confidence", 0) >= 0.75:
            mark_promise_resolved(
                promise_id=item["existing_promise_id"],
                resolution_type="auto",
                evidence=item["resolution_evidence"],
                resolved_at=now()
            )
        elif item["action"] == "resolve" and item.get("confidence", 0) >= 0.5:
            mark_promise_suggested_resolved(
                promise_id=item["existing_promise_id"],
                evidence=item["resolution_evidence"]
            )
            # surfaces in UI: "BLAM8 thinks this was fulfilled — confirm?"
```

**Schema change:**

```sql
ALTER TABLE crm_promises
  ADD COLUMN resolution_type text,        -- manual | auto | suggested
  ADD COLUMN resolution_evidence text,
  ADD COLUMN resolved_at timestamptz;
```

**UI indication:** Resolved promises show a ✓ with the evidence snippet. Suggested resolutions show as a dismissable card: "Dave sent the quote you promised — mark as done?"

**Files touched:**
- `src/app/crm_intel.py` — extend promise processing with resolution detection
- `src/app/crm_routes.py` — add `POST /api/crm/promises/{id}/resolve` for manual + confirm
- `migrations/` — add resolution columns
- Frontend promise card component — resolved state + suggested-resolution card

**Tests:**
- Outbound promise "I'll send the quote" + later outbound with attachment → auto-resolved
- Inbound promise "I'll get back Friday" + Friday reply arrives → auto-resolved
- Low-confidence resolution → suggested, not auto-resolved
- Promise about X, email about Y → no resolution triggered

---

## 19. Phase 4 — Implementation spec

Three table-stakes features required for competitive parity (§ 5.6). Each is independently shippable. Mobile app deliberately excluded (1% weight in purchase decision per Forbes research).

**Status (2026-07-05):** All three shipped. 19.1 Reporting dashboards + 19.2 Task management as **Phase 4a** (`00e1bae`); 19.3 Multi-step follow-up sequences as **Phase 4b** (`40b9194`, schema live in prod). Phase 4 complete.

### 19.1 Reporting dashboards — ✅ SHIPPED (Phase 4a)

**What it is:** Visual pipeline and activity analytics. Every CRM competitor ships this. BLAM8's advantage: the data is *already complete* (auto-populated), so reports are accurate from day one — unlike competitors where reports reflect only what humans remembered to log.

**Views (v1 — four dashboards):**

1. **Pipeline summary** — total deal value by stage, deal count by stage, avg days in stage. Bar chart + summary cards.
2. **Activity heatmap** — emails sent/received per day over trailing 30/90 days. Shows engagement patterns.
3. **Won/Lost analysis** — deals closed in period, win rate, avg deal value, avg days to close. Filterable by date range.
4. **Leaderboard (multi-user tenants only)** — activity count and deal value per team member. Relevant when BLAM8 adds seat-based plans.

**Implementation approach:**

```
Route: /app/crm/reports
API:   GET /api/crm/reports/{report_type}?period=30d&stage_id=...
```

All reports are **computed server-side from existing tables** — no new data collection needed. Queries aggregate from `deals`, `activities`, `stages`.

**Key queries:**

```sql
-- Pipeline summary
SELECT s.name, s.display_order, COUNT(d.id) as deal_count,
       COALESCE(SUM(d.value_estimate), 0) as total_value,
       AVG(EXTRACT(EPOCH FROM (now() - d.opened_at)) / 86400) as avg_days
FROM stages s
LEFT JOIN deals d ON d.stage_id = s.id AND d.status = 'open'
WHERE s.tenant_id = :tenant_id
GROUP BY s.id ORDER BY s.display_order;

-- Activity heatmap
SELECT DATE(occurred_at) as day, direction, COUNT(*) as count
FROM activities
WHERE tenant_id = :tenant_id
  AND occurred_at >= now() - interval ':period days'
GROUP BY day, direction ORDER BY day;

-- Won/Lost
SELECT status, COUNT(*) as count,
       AVG(value_estimate) as avg_value,
       AVG(EXTRACT(EPOCH FROM (closed_at - opened_at)) / 86400) as avg_days_to_close
FROM deals
WHERE tenant_id = :tenant_id AND status IN ('won', 'lost')
  AND closed_at >= now() - interval ':period days'
GROUP BY status;
```

**Frontend:** Server-rendered Jinja2 (matches existing pattern). Charts via lightweight JS library (Chart.js — already in project deps, or a <10KB alternative like uPlot). No SPA framework needed.

**Files touched:**
- `src/app/crm_routes.py` — add `/reports` and `/api/crm/reports/` endpoints
- `templates/crm/reports.html` — dashboard page
- `static/js/crm_charts.js` — chart rendering (thin wrapper around Chart.js)

**No schema changes.** All data already exists.

---

### 19.2 Task management — ✅ SHIPPED (Phase 4a)

**What it is:** Simple to-do items linked to contacts, companies, or deals. Due dates, assignees (for multi-seat), completion tracking. Every CRM ships this; BLAM8's version auto-generates tasks from promises and deal stage transitions.

**Schema:**

```sql
CREATE TABLE crm_tasks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  title text NOT NULL,
  description text,
  due_date date,
  priority text DEFAULT 'normal',  -- low | normal | high | urgent
  status text DEFAULT 'open',       -- open | done | cancelled
  assignee_id uuid,                 -- future: references tenant_users
  -- Linkages (any combination, all optional)
  contact_id uuid REFERENCES contacts(id),
  company_id uuid REFERENCES companies(id),
  deal_id uuid REFERENCES deals(id),
  promise_id uuid REFERENCES crm_promises(id),
  -- Provenance
  source text DEFAULT 'manual',     -- manual | auto_promise | auto_stage | auto_nudge
  created_at timestamptz DEFAULT now(),
  completed_at timestamptz,
  updated_at timestamptz DEFAULT now()
);

CREATE INDEX idx_crm_tasks_tenant_status ON crm_tasks (tenant_id, status, due_date);
```

**Auto-generation rules:**

| Trigger | Task created | Due date |
|---|---|---|
| Promise created (outbound, has date) | "Follow through: {promise}" | Promised date |
| Promise created (outbound, no date) | "Follow through: {promise}" | +3 business days |
| Deal enters "Negotiating" stage | "Send proposal for {deal}" | +2 business days |
| Deal dormant >14 days | "Re-engage: {deal}" | Today |
| Gone-quiet nudge fires | "Check in with {contact}" | Today |

Auto-generated tasks link back to their source via `source` + `promise_id`/`deal_id`. When the source resolves (promise fulfilled, deal moves), the task auto-completes.

**API:**

```
GET    /api/crm/tasks                — list (filterable: status, due, contact, deal)
POST   /api/crm/tasks                — create
PATCH  /api/crm/tasks/{id}           — update (title, due, status, priority)
DELETE /api/crm/tasks/{id}           — soft-delete (status=cancelled)
GET    /api/crm/tasks/today          — due today + overdue (for the Today card)
```

**UI locations:**
- `/app/crm/tasks` — full task list with filters
- Contact detail page — tasks linked to that contact
- Deal detail page — tasks linked to that deal
- Today dashboard card — overdue + due-today count with link

**Files touched:**
- `src/app/crm_routes.py` — task CRUD endpoints
- `src/app/crm_intel.py` — auto-generation hooks (on promise create, stage change, nudge fire)
- `templates/crm/tasks.html` — task list page
- `templates/crm/contact_detail.html` — tasks section
- `migrations/` — `crm_tasks` table

---

### 19.3 Multi-step follow-up sequences — ✅ SHIPPED (Phase 4b)

**What it is:** Automated, behaviour-triggered email sequences. BLAM8 already has gone-quiet nudges (single touchpoint). This extends them into branching multi-step outreach. Nimble charges extra for this. BLAM8 includes it in Core — pricing dislocation (§ 16).

**Concept model:**

```
Sequence
  └── Steps (ordered)
        ├── Step 1: wait 3 days, send template A
        ├── Step 2: wait 5 days, if no reply → send template B
        ├── Step 3: wait 7 days, if no reply → send template C + mark deal dormant
        └── (exit conditions at any step: reply received, deal stage changes, manual stop)
```

**Schema:**

```sql
CREATE TABLE crm_sequences (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  name text NOT NULL,
  description text,
  trigger_type text NOT NULL,
  -- Triggers: manual | deal_stage_enter | gone_quiet | new_contact | tag_added
  trigger_config jsonb DEFAULT '{}',  -- e.g. {"stage_id": "...", "days_quiet": 14}
  status text DEFAULT 'active',       -- active | paused | archived
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);

CREATE TABLE crm_sequence_steps (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  sequence_id uuid NOT NULL REFERENCES crm_sequences(id) ON DELETE CASCADE,
  step_order integer NOT NULL,
  delay_days integer NOT NULL DEFAULT 3,
  action_type text NOT NULL DEFAULT 'send_email',
  -- send_email | update_stage | create_task | add_tag
  template_body text,           -- for send_email: the template (supports {{contact.name}}, etc.)
  template_subject text,
  exit_on_reply boolean DEFAULT true,
  side_effects jsonb DEFAULT '[]',  -- e.g. [{"action": "move_stage", "stage_id": "..."}]
  created_at timestamptz DEFAULT now()
);

CREATE TABLE crm_sequence_enrollments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  sequence_id uuid NOT NULL REFERENCES crm_sequences(id),
  contact_id uuid NOT NULL REFERENCES contacts(id),
  deal_id uuid REFERENCES deals(id),
  current_step integer DEFAULT 0,
  status text DEFAULT 'active',  -- active | completed | exited | paused
  next_action_at timestamptz,
  enrolled_at timestamptz DEFAULT now(),
  exited_at timestamptz,
  exit_reason text  -- replied | stage_changed | manual | completed | bounced
);

CREATE INDEX idx_enrollments_next_action
  ON crm_sequence_enrollments (tenant_id, status, next_action_at)
  WHERE status = 'active';
```

**Execution engine:**

Runs on the existing BLAM8 scheduler (same mechanism as gone-quiet nudge checks). Every 15 minutes:

```python
async def process_sequence_queue():
    """Find enrollments where next_action_at <= now() and status='active'."""
    due = await get_due_enrollments()
    for enrollment in due:
        step = get_step(enrollment.sequence_id, enrollment.current_step + 1)
        if not step:
            complete_enrollment(enrollment)
            continue

        # Check exit conditions
        if step.exit_on_reply and has_reply_since(enrollment):
            exit_enrollment(enrollment, reason="replied")
            continue

        # Execute step action
        if step.action_type == "send_email":
            draft = render_template(step, enrollment.contact, enrollment.deal)
            queue_send(draft, tenant_id=enrollment.tenant_id)
        elif step.action_type == "update_stage":
            move_deal_stage(enrollment.deal_id, step.side_effects)
        elif step.action_type == "create_task":
            create_task_from_step(step, enrollment)

        # Advance
        enrollment.current_step += 1
        enrollment.next_action_at = now() + timedelta(days=next_step.delay_days)
```

**Template variables available in step bodies:**

```
{{contact.name}}, {{contact.email}}, {{company.name}},
{{deal.title}}, {{deal.stage}}, {{deal.value}},
{{tenant.business_name}}, {{tenant.sender_name}},
{{days_since_last_contact}}, {{promise_summary}}
```

Templates are **passed through the existing BLAM8 drafter** so they inherit the tenant's voice/tone. The template is a *brief*, not a literal email — BLAM8 rewrites it in the tenant's style.

**Built-in sequence templates (seeded per vertical):**

| Vertical | Sequence | Steps |
|---|---|---|
| Generic | "Gone quiet — 3-touch re-engage" | Day 3: casual check-in → Day 8: value-add → Day 14: break-up email |
| Trades | "Quote follow-up" | Day 2: "Did you get a chance to review?" → Day 7: "Happy to adjust if needed" → Day 14: close or re-quote |
| Logistics | "RFQ response" | Day 1: confirm receipt → Day 5: "Any questions on pricing?" → Day 10: final availability window |

**API:**

```
GET    /api/crm/sequences               — list tenant's sequences
POST   /api/crm/sequences               — create
PATCH  /api/crm/sequences/{id}          — update
DELETE /api/crm/sequences/{id}          — archive

GET    /api/crm/sequences/{id}/steps    — list steps
POST   /api/crm/sequences/{id}/steps    — add step
PATCH  /api/crm/sequences/{id}/steps/{step_id} — edit step
DELETE /api/crm/sequences/{id}/steps/{step_id} — remove step

POST   /api/crm/sequences/{id}/enroll   — manually enroll a contact
GET    /api/crm/sequences/{id}/enrollments — view active enrollments
POST   /api/crm/enrollments/{id}/pause  — pause
POST   /api/crm/enrollments/{id}/exit   — force exit
```

**UI:**
- `/app/crm/sequences` — list + builder (step editor with drag-reorder)
- Contact detail page — "Active sequences" section showing enrollment status
- Deal detail page — sequence activity in timeline

**Guardrails:**
- Max 5 active sequences per tenant (Core tier). Prevents over-automation.
- Max 7 steps per sequence. Keeps outreach human-length.
- Global opt-out: if a contact replies "unsubscribe" or "stop emailing me", all enrollments for that contact exit immediately + contact flagged `do_not_sequence=true`.
- Rate limit: max 1 sequence email per contact per day (even if enrolled in multiple sequences). Queues collisions for next available slot.
- Bounce handling: hard bounce → exit enrollment + flag contact email invalid.

**Files touched:**
- `src/app/crm_sequences.py` — new module: sequence engine, enrollment processor, template renderer
- `src/app/crm_routes.py` — sequence CRUD + enrollment endpoints
- `src/app/scheduler.py` — add `process_sequence_queue` to the 15-min loop
- `templates/crm/sequences.html` — sequence list + builder UI
- `migrations/` — three new tables

---

## 20. Phase 3.5 + 4 success metrics

**Phase 3.5 delivered when:**

- Freemail sender with stated company name → Company record created and linked (demo: re-run Pearson Plumbing scenario, company populates)
- Required field with probe_behaviour='probe_once' → next draft contains a natural question for that field
- Restated promise → single record with mention_count > 1, not a duplicate
- Fulfilled promise → auto-resolved with evidence, visible in UI

**Phase 4 delivered when:**

- Tenant can view pipeline summary, activity heatmap, won/lost analysis from `/app/crm/reports`
- Tasks auto-generate from promises and stage transitions; complete when source resolves
- A 3-step follow-up sequence can be created, a contact enrolled, and all 3 emails fire on schedule with exit-on-reply working correctly
- All three features work with zero manual data entry (inherit the "fills itself in" principle)

## 21. Sequencing recommendation (Phases 3.5 + 4)

**Session 4 (Phase 3.5):** Four targeted fixes, all in existing files. Estimate: single focused session. Highest-impact fix (#1 freemail company) should land first as it's the most visible demo gap.

**Session 5 (Phase 4a — Reports + Tasks):** Natural pair — tasks feed into reports (task completion rates). No new architectural patterns needed; both build on existing schema.

**Session 6 (Phase 4b — Sequences):** Most complex new subsystem. Needs its own session. The sequence engine is a new execution loop but builds on existing scheduler + drafter infrastructure, so integration surface is well-defined.

Each phase remains independently shippable. Phase 3.5 fixes demo gaps. Phase 4a adds expected furniture. Phase 4b adds genuine competitive edge (automation in Core tier while competitors charge extra).

---

## 22. Phase 5.1 — Proposals + invoicing loop-close

*Why:* Every CRM stops at Deal→Won. BLAM8 continues to Deal→Won→Proposal drafted→Signed→Invoice sent→Payment received→Reconciled. Same LLM that drafts email replies now drafts proposals from deal context; same intelligence that catches promises catches payments. Nobody else in the CRM or accounting-tool space closes this loop from a single self-populating source. This is BLAM8's strongest single line: **"the only CRM that closes the deal AND collects the money."**

*Schema additions:*
```sql
proposals (
  id uuid primary key,
  tenant_id uuid not null references tenants(id),
  deal_id uuid not null references deals(id),
  contact_id uuid references contacts(id),
  version integer default 1,
  title text,
  body_html text,
  body_source jsonb,
  status text default 'draft',    -- draft | sent | viewed | signed | declined
  total_amount numeric,
  currency text default 'GBP',
  sent_at timestamptz,
  signed_at timestamptz,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

invoices (
  id uuid primary key,
  tenant_id uuid not null references tenants(id),
  deal_id uuid not null references deals(id),
  contact_id uuid references contacts(id),
  proposal_id uuid references proposals(id),
  invoice_number text,
  amount numeric not null,
  currency text default 'GBP',
  status text default 'draft',    -- draft | sent | paid | overdue | disputed
  due_date date,
  sent_at timestamptz,
  paid_at timestamptz,
  payment_method text,
  external_ref text,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

payments (
  id uuid primary key,
  tenant_id uuid not null references tenants(id),
  invoice_id uuid references invoices(id),
  deal_id uuid references deals(id),
  amount numeric not null,
  currency text default 'GBP',
  received_at timestamptz not null,
  source text,                    -- stripe | gocardless | bank | manual
  external_ref text,
  extracted_from text,            -- email_log id if detected from inbox
  created_at timestamptz default now()
);
```

*Flow:*
1. Deal moves to Won → BLAM8 offers "Generate proposal?" or auto-drafts based on tenant preference.
2. LLM composes proposal from: deal record + past emails + custom fields + tenant's proposal template + RAG (past proposals).
3. Owner reviews / edits / sends → status = `sent`.
4. Optional e-signature integration (DocuSign / Dropbox Sign) — status flips to `signed` on webhook.
5. Signed → auto-generate invoice from proposal (line items copied, due_date computed from tenant defaults).
6. Invoice sent via existing email drafter.
7. Payment detection: LLM scans inbox for payment notifications (Stripe receipts, GoCardless notifications, bank statement mentions, customer "just paid" emails) → matches to invoice → creates payment record → invoice status → paid → deal record annotated.
8. Overdue: invoice past due_date → BLAM8 drafts chaser email using deal context.

*API surface:*
- `POST /api/crm/deals/{id}/proposals`
- `GET /api/crm/proposals/{id}`
- `PATCH /api/crm/proposals/{id}`
- `POST /api/crm/proposals/{id}/send`
- `POST /api/crm/proposals/{id}/sign`
- `POST /api/crm/invoices`
- `POST /api/crm/invoices/{id}/send`
- `POST /api/crm/payments`
- `GET /api/crm/dashboard/receivables`

*Integrations:* Stripe / GoCardless webhooks (payment auto-detection), Companies House VAT lookup (invoice auto-fill), DocuSign / Dropbox Sign (e-signature, v2).

*Cost:* ~$0.001 per proposal (Haiku 4.5). Negligible.

*Dependencies:* Deals table (shipped). Email drafter (shipped). Payment source integrations (external, optional).

*Success criteria:* Tenant goes from "Deal Won" to "Payment recorded" without opening any other tool. Payment auto-detection accuracy ≥85% (paid deals with linked payment record ÷ paid deals total).

---

## 23. Phase 5.2 — WhatsApp + SMS channels

*Why:* BLAM8's drafter is transport-agnostic in architecture. Adding WhatsApp and SMS is symmetry, not rewrite. Trades and logistics run their businesses in WhatsApp — this unlocks massive latent adoption in verticals where email adoption is low.

*Schema additions:*
```sql
message_channels (
  id uuid primary key,
  tenant_id uuid not null references tenants(id),
  channel_type text not null,     -- email | whatsapp | sms
  channel_config jsonb,           -- credentials, phone_number, etc.
  is_active boolean default true,
  created_at timestamptz default now()
);
```

For messages themselves: keep `email_log` as-is (don't destabilise it), add parallel `whatsapp_messages` and `sms_messages` tables with the same shape. Activity feed unions across all three per contact/deal. Larger message-table refactor deferred to a later phase.

*Provider integrations:* WhatsApp Business API (via Twilio, Vonage, or Meta Cloud API direct); SMS via Twilio.

*Drafter changes:* minimal. Same tenant_config voice + RAG apply. Channel-specific formatting rules (WhatsApp shorter, no signatures) baked into `channel_style_hints` per channel.

*LLM extraction:* same pipeline as email. Contact / deal / promise / activity all populate the same way.

*Cost:* WhatsApp Business via Twilio ~$0.005 per outbound message, free inbound. UK SMS ~$0.02 outbound. Per-tenant variable — price into tier or pass through.

*Dependencies:* WhatsApp Business API approval (Meta review, 2–3 weeks). Twilio account setup (fast).

*Success criteria:* Trades tenant runs their whole customer conversation flow on WhatsApp with BLAM8 drafting every reply. Extraction accuracy matches email baseline.

---

## 24. Phase 5.3 — Voice call transcription → CRM activity

*Why:* BLAM8's CRM currently sees email + calendar. Voice calls are the biggest single blind spot — most customer relationships have significant call touchpoints that never enter the CRM. Once calls become activities, BLAM8's coverage of the relationship approaches 100%.

*Schema additions:*
```sql
voice_calls (
  id uuid primary key,
  tenant_id uuid not null references tenants(id),
  contact_id uuid references contacts(id),
  deal_id uuid references deals(id),
  direction text,                  -- inbound | outbound
  duration_seconds integer,
  audio_url text,
  transcript_text text,
  transcript_source text,          -- twilio | whisper | provider_x
  summary text,                    -- LLM-generated
  action_items jsonb,
  occurred_at timestamptz not null,
  created_at timestamptz default now()
);
```

*Provider options:* Twilio Voice + Media Streams (native); Aircall / Fireflies (existing phone-system integration); BLAM8 native voice module (roadmap); bring-your-own (user records call on phone, uploads audio, BLAM8 transcribes via Whisper).

*Flow:*
1. Call happens.
2. Audio transcribed via Whisper or provider transcription.
3. LLM summarizes + extracts action items + promises.
4. Attaches to contact / deal as activity.
5. Drafter now references call content in subsequent emails ("As we discussed on the phone yesterday, delivery date is 15 Aug").

*Cost:* Whisper ~$0.006/min, summarization ~$0.001/call. Cheap.

*Dependencies:* BLAM8 voice module OR external integration. Recording consent (UK regulatory — two-party consent required).

*Success criteria:* Call happens Monday → appears as CRM activity by Tuesday → drafter references call content correctly in Wednesday's reply.

---

## 25. Phase 5.4 — Referral tracking

*Why:* Every business tracks referrals badly. BLAM8's LLM extraction catches "Rob mentioned you" or "Sarah said you'd help" automatically. Attribution back to the referrer becomes queryable. No other CRM does this from email content alone.

*Schema additions:*
```sql
referrals (
  id uuid primary key,
  tenant_id uuid not null references tenants(id),
  referrer_contact_id uuid references contacts(id),
  referred_contact_id uuid references contacts(id) not null,
  deal_id uuid references deals(id),
  source_email_id text,            -- email_log id where referral was detected
  detected_at timestamptz not null,
  confidence numeric,
  status text default 'detected',  -- detected | confirmed | dismissed
  notes text,
  created_at timestamptz default now()
);
```

*Flow:*
1. Inbound email from new contact → LLM extraction runs.
2. LLM prompt includes: "was this person referred? If so, by whom (name mentioned)?"
3. Fuzzy-match name against existing contacts (disambiguate by domain if unclear).
4. Create referral record if confidence > threshold (default 0.7).
5. Referrer's contact page now shows "Referred X, resulted in Deal Y."
6. Dashboard: "Top referrers this quarter."

*Cost:* negligible — extra clause in existing LLM extraction prompt.

*Success criteria:* Tenant can answer "who's my best referrer" without having entered a referrer manually.

---

## 26. Phase 5.5 — Native enrichment (Companies House + LLM-derived)

*Why:* Every other CRM pays for third-party firmographic data (Clearbit, Apollo). BLAM8's LLM extracts more accurate + current data from conversation. Layering *free* external data (Companies House for UK Ltd companies) adds trust and completeness without new spend.

*Schema additions:* extend `companies.custom_data` with a `_enrichment` namespaced block:
- `registered_office`
- `incorporation_date`
- `company_number`
- `directors` (public info only)
- `filing_status`
- `source: "companies_house"`
- `fetched_at`

*Flow:*
1. New Company record created with UK domain.
2. Background job queries Companies House free API (no auth required).
3. Match company name + domain → pull registered info.
4. Store in `_enrichment` block.
5. Contact detail page shows subtle "registered as X Ltd, incorporated 2019" line.

*Cost:* free.

*Success criteria:* 60%+ of UK Ltd company matches enriched within 24 hours of Company creation.

---

## 27. Phase 5.6 — Post-sale customer mode

*Why:* BLAM8's drafter operates in one mode — sales / inbound-support. Once a Deal is Won, the same contact becomes a customer, but the drafter doesn't know. Adding a mode switch means: tone shifts from acquisition-friendly to relationship-maintenance; reply priorities shift (customer issues > new-sales-nudges); renewal awareness (drafter surfaces renewal conversations proactively).

*Schema additions:*
```sql
alter table contacts add column lifecycle_stage text default 'prospect';
-- prospect | qualified | customer | churned

alter table deals add column renewal_due_at timestamptz;
alter table deals add column is_recurring boolean default false;
```

`tenant_config` gains a `post_sale_voice_hints` field (structured similarly to existing voice hints).

*Flow:*
1. Deal moves to Won → primary contact `lifecycle_stage` → 'customer'.
2. Drafter checks stage on every reply — 'customer' triggers `post_sale_voice_hints`.
3. If `renewal_due_at` within 60 days → drafter proactively surfaces "renewal comes up in 8 weeks — draft a check-in?"
4. Churn detection: customer 90 days without contact + no open promise → `lifecycle_stage` → 'churned' + nudge to owner.

*Cost:* negligible — reuses existing infrastructure.

*Success criteria:* A customer's replies from BLAM8 sound noticeably different from a prospect's (measurable via tone A/B).

---

## 28. Aggregate cost + success metrics for Phase 5

Cost projections (per tenant per month, average):
- Proposals + invoicing: <$1
- WhatsApp / SMS: variable, ~$5–20 depending on volume
- Voice transcription: variable, ~$3–10 for call-heavy user
- Referral / enrichment / post-sale: <$1 combined

At £99 Core, Phase 5 doesn't compress margin as long as WhatsApp / voice volumes stay reasonable. Consider a modest usage cap or optional add-on pricing for high-volume users.

Aggregate success criteria: **A tenant runs their entire revenue lifecycle — first inbound touch through invoice paid — inside BLAM8, zero external tools required for the sales / delivery / payment loop.** Measure: % of deals with full lifecycle records (contact, activities across channels, promises tracked, proposal drafted, invoice sent, payment reconciled).
