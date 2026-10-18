# Enterprise multi-mailbox — one tenant, many inboxes, many brains (SCOPE — LOCKED)

> **Status (2026-09-27, confirmed by the clean-up pass):** BUILT 2026-06-10 (ff0aa13, steps 1–3); live e2e on the logistics pilot 2026-06-10 mostly passed. Still open: auto-send to an external sender, a clean per-mailbox disconnect.

**Status:** LOCKED 2026-06-10 — all four open questions answered with Julius
(layered KB; departments as grouping labels; one Enterprise switch; build next
session). Origin: enterprise/MSA conversation after Scheduling + Calendar
Connect shipped — enterprise customers run multiple addresses per branch
(info@ vs orders@ vs accounts@), each needing its own knowledge and persona.

## Goal
Let an operator flip a tenant to **Enterprise** in the admin console; that
tenant can then connect **multiple mailboxes**, each with its own knowledge
base and its own agent persona, organised by **department** and **category** —
while a standard SaaS tenant remains byte-for-byte unchanged.

## The architectural move
BLAM8 was built on *one tenant = one mailbox = one brain*: the address and
provider live on `tenants`, connecting deletes the previous connection,
config + KB are tenant-scoped, the inbox has no mailbox dimension. The fix is
NOT an "extra emails" bolt-on. **The mailbox becomes a first-class entity, and
SaaS is the special case of one mailbox with no overrides.** One code path for
every tenant, gated by data (`tenants.plan`) — the platform pattern, third
outing (modules, window modes, now account tiers).

## Decisions
### D1 — `email_connections` is promoted to THE mailbox entity
It already holds the per-mailbox machinery (tokens, Graph subscription, Gmail
watermark). It gains `address`, `provider` (MOVES here from `tenants` — one
enterprise can mix Outlook depots with a Gmail branch), `label`, `department`,
`category`. `tenants.mailbox/provider` remain as a denormalised mirror of the
default (first) mailbox for legacy reads/back-compat, synced on connect.
Rejected: a separate `mailboxes` table 1:1 with connections (a join with no
information in it).

### D2 — One Enterprise switch in the operator console
Admin tenant page gains an **Account type** card: flipping to Enterprise sets
`tenants.plan='enterprise'` (PLANS gains the entry; allowance bespoke; revenue
already comes from `contract_monthly_gbp`) AND sets
`reporting_tier='enterprise'`. `db.is_enterprise(tenant)` = `plan=='enterprise'`
is the single gate for multi-mailbox behaviour. Rejected: separate toggles
(operator UX — one decision, one switch; reporting can still be flipped back
independently afterwards).

### D3 — Per-mailbox brain = tenant defaults ⊕ mailbox overrides
New `mailbox_config` (PK mailbox_id): same persona columns as `tenant_config`
(tone, agent_name, sign_off, do_list, never_do, escalation,
custom_instructions, examples), plus `purpose` (one line: "this mailbox
handles order intake" — injected as the mailbox's role framing) and
`auto_send boolean` (NULL = inherit). **All columns nullable; effective config
= `{**tenant_cfg, **{k:v for mailbox overrides if v is not None}}`** — merge
in code, in the pipeline, before `agent.draft_reply` (agent.py unchanged).
`business_brief` stays tenant-level (company identity is company-wide).
Escalation/never_do OVERRIDE when set (append semantics = later refinement).
orders@ can run full-auto while complaints@ stays draft-only.

### D4 — Layered knowledge: company-wide + per-mailbox
`knowledge_chunks.mailbox_id uuid NULL` — NULL = company-wide (everything
existing already is). Retrieval unions: `match_knowledge_chunks` gains
`p_mailbox_id` and matches `mailbox_id IS NULL OR mailbox_id = p_mailbox_id`.
Upload UI gains a target selector (Company-wide | a mailbox); the sources list
shows scope. Rejected: fully isolated per-mailbox KBs (company facts would be
pasted N times and drift).

### D5 — Pipeline becomes mailbox-aware end to end
- `email_log.mailbox_id uuid NULL` (NULL = legacy rows / the only mailbox).
- Graph webhook already resolves subscription → connection: pass its id as
  mailbox_id. Gmail poller iterates connections WHERE provider='google'
  (provider now per-connection — fixes the poll set for mixed tenants).
- Replies send from the mailbox that received: `send_reply(tenant_id,
  mailbox_id, …)` uses that connection.
- Self-mail guard widens to the SIBLING SET: inbound whose from_address is any
  of the tenant's mailbox addresses drafts but never auto-sends (orders@ and
  info@ cannot loop each other).
- Dedupe stays correct: provider message ids are mailbox-scoped, so one email
  CC'd to two connected mailboxes makes two drafts — one per mailbox — which
  is the desired behaviour (each persona answers its own inbox).

### D6 — Departments & categories are LABELS in v1
Free-text columns on the mailbox (UI suggests categories: info, orders,
support, invoicing…). They group the Mailboxes page, filter the Inbox, and
slice usage/reporting. Rejected for v1: departments as config entities with a
three-layer merge (tenant → department → mailbox) — clean later addition once
a customer asks for "everything in the Leeds depot behaves the same".

## Schema (one migration, via the POOLER — db.<ref> host is IPv6-only, see memory)
```sql
alter table email_connections add column if not exists address    text;
alter table email_connections add column if not exists provider   text;
alter table email_connections add column if not exists label      text;
alter table email_connections add column if not exists department text;
alter table email_connections add column if not exists category   text;
update email_connections ec set provider = t.provider, address = t.mailbox
from tenants t where ec.tenant_id = t.id and ec.provider is null;

create table if not exists mailbox_config (
  mailbox_id  uuid primary key references email_connections(id) on delete cascade,
  tenant_id   uuid not null references tenants(id) on delete cascade,  -- RLS wall
  purpose     text, tone text, agent_name text, sign_off text,
  do_list     text, never_do text, escalation text,
  custom_instructions text, examples text,
  auto_send   boolean,                       -- null = inherit tenant_config
  updated_at  timestamptz not null default now()
);
-- RLS: tenant isolation policy identical to tenant_config.

alter table knowledge_chunks add column if not exists mailbox_id uuid
  references email_connections(id) on delete cascade;   -- null = company-wide
alter table email_log add column if not exists mailbox_id uuid;
create index if not exists email_log_tenant_mailbox_idx
  on email_log (tenant_id, mailbox_id) where mailbox_id is not null;
-- match_knowledge_chunks(p_tenant_id, p_query_embedding, p_match_count,
--   p_mailbox_id uuid default null):
--   where kc.tenant_id = p_tenant_id
--     and (kc.mailbox_id is null or kc.mailbox_id = p_mailbox_id)
```
Connect flow: `oauth_states` gains `mode text default 'replace'` — the
enterprise "Add mailbox" link passes add-mode; callback then INSERTS (or
replaces the row matching the same address) instead of today's delete-all.
SaaS keeps replace-all. `upsert_email_connection` grows accordingly.

## Wiring (smallest surface)
- **`src/app/db.py`:** `is_enterprise`, mailbox CRUD (list/label/department),
  mailbox_config get/upsert, `effective_config(tenant_id, mailbox_id)` merge,
  KB calls take optional mailbox_id, email_log insert/list take mailbox_id,
  connection lookups by id (not just "the one").
- **`src/app/pipeline.py`:** `draft_for_email(..., mailbox_id=None)` — effective
  config + layered KB + sibling-set guard; stamps email_log.mailbox_id;
  passes mailbox_id through redraft.
- **`src/app/webhook.py` / `src/app/jobs/poll_gmail.py`:** hand the connection id into
  the pipeline (webhook already has it; poller iterates per-connection
  provider).
- **`src/app/mailbox.py`:** send/get against a specific connection.
- **`src/app/oauth.py`:** add-mode connect; sync the tenants mirror columns.
- **`src/app/admin.py` + admin/tenant.html:** Account type card (the flip).
- **`src/app/dashboard.py` + templates:** Mailboxes page (grouped by department,
  add/edit labels, per-mailbox settings + knowledge links), mailbox filter on
  Inbox/Conversations, knowledge upload target selector. All gated on
  `is_enterprise` — SaaS sees today's UI untouched.
- **Scheduling:** diary + module gate stay tenant-level in v1.

## Edge cases
- Disconnecting one mailbox must not nuke siblings (today's delete-by-tenant
  becomes delete-by-connection-id for enterprise).
- A mailbox with no mailbox_config row = pure tenant defaults (SaaS identity).
- Legacy email_log rows (mailbox_id null) render under the default mailbox.
- CC'd-to-two-mailboxes → two drafts, by design (mailbox-scoped message ids).
- Downgrade enterprise→SaaS with >1 mailbox connected: block in admin until
  extras are disconnected (never silently drop tokens).
- Usage: per-mailbox/department reporting derives via email_log join — no
  usage_events schema change in v1.

## Verification
- **Unit:** config merge (null-inherit, override, purpose line); KB union
  retrieval; connect append-vs-replace by mode + plan; sibling-set guard;
  legacy null-mailbox rendering; poller set by connection provider.
- **Live:** flip a test tenant to Enterprise → connect a second mailbox →
  distinct personas + KBs answer side by side; full-auto on one mailbox only;
  reply threads from the correct address; one mailbox disconnects cleanly.

## Out of scope (v1)
Department-level config inheritance, per-mailbox module gating, per-department
diaries, mailbox-level billing/allowances, shared-KB edit permissions,
cross-mailbox contact identity (Voice-era), Google app verification.

## Build order
1. Schema migration + admin Account type flip + connection promotion
   (address/provider/label backfill) + add-mode connect + mailbox-aware
   webhook/poller/send + email_log.mailbox_id. SaaS frozen by tests.
2. The brain: mailbox_config + effective_config merge + layered KB (RPC +
   upload target + retrieval) + per-mailbox auto_send/escalation + sibling
   guard.
3. Surfaces: Mailboxes page, inbox/conversations filter, per-mailbox settings
   + knowledge screens, usage by mailbox/department in enterprise reporting.
4. Live E2E (two mailboxes, two personas) on a test tenant, then the pilot
   demo story.
