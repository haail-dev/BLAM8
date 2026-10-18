-- BLAutoM8 — Email Agent MVP
-- Supabase schema. Run this in the Supabase SQL editor (one-shot).
-- Spec: email-agent-build-spec.md §5
--
-- Order matters: extensions → tables → RLS → policies → vector index.

-- ──────────────────────────────────────────────────────────────
-- Extensions
-- ──────────────────────────────────────────────────────────────
create extension if not exists vector;     -- pgvector, for knowledge retrieval
create extension if not exists pgcrypto;   -- gen_random_uuid()

-- ──────────────────────────────────────────────────────────────
-- Core tables
-- ──────────────────────────────────────────────────────────────

-- A business using the platform
create table if not exists tenants (
  id           uuid primary key default gen_random_uuid(),
  name         text not null,
  provider     text not null default 'microsoft',   -- email provider: 'microsoft' | 'google' (later)
  mailbox      text,                                 -- the address being monitored
  mode         text not null default 'draft',        -- 'draft' | 'auto' (later)
  industry     text,                                 -- onboarding: 'logistics' | 'services' | … (presets workflows)
  onboarded_at timestamptz,                          -- null until the onboarding wizard is finished
  created_at   timestamptz not null default now()
);

-- Migration for existing databases (onboarding fields). The backfill marks
-- already-active tenants (those with a connected mailbox) as onboarded, so live
-- users are never pushed back through the wizard; brand-new tenants stay null.
alter table tenants add column if not exists industry     text;
-- Business "shape" (Field/Storefront/Practice/Wholesale/Assets/Events/Match),
-- derived from industry at write time — the OS layer we build to and sell.
alter table tenants add column if not exists operating_system text;
alter table tenants add column if not exists onboarded_at timestamptz;
update tenants t set onboarded_at = coalesce(t.onboarded_at, t.created_at)
where t.onboarded_at is null
  and exists (select 1 from email_connections ec where ec.tenant_id = t.id);

-- Maps a logged-in Supabase user to the tenant(s) they belong to.
-- NOT in the original spec, but RLS "keyed to tenant" is impossible without it:
-- a policy has to know which tenant the current auth.uid() belongs to.
create table if not exists tenant_members (
  tenant_id   uuid not null references tenants(id) on delete cascade,
  user_id     uuid not null references auth.users(id) on delete cascade,
  role        text not null default 'member',       -- 'owner' | 'member'
  created_at  timestamptz not null default now(),
  primary key (tenant_id, user_id)
);

-- Which modules a tenant has (platform vision lives here, dormant for now)
create table if not exists subscriptions (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references tenants(id) on delete cascade,
  module      text not null default 'email',        -- 'email' | 'scheduling' (more later)
  status      text not null default 'active',
  created_at  timestamptz not null default now()
);

-- Mailbox OAuth tokens (one row per connected mailbox)
create table if not exists email_connections (
  id              uuid primary key default gen_random_uuid(),
  tenant_id       uuid not null references tenants(id) on delete cascade,
  access_token    text,
  refresh_token   text,
  token_expires   timestamptz,
  subscription_id text,                              -- Graph webhook subscription id
  sub_expires     timestamptz,                       -- so the cron job knows when to renew
  created_at      timestamptz not null default now()
);

-- The agent's personality + rules for a tenant (the assembled-instruction source).
-- This is the TENANT layer of the system prompt: app/agent.build_system_prompt
-- wraps these fields inside the fixed platform (CORE) instructions, which always
-- take precedence. Everything that makes the agent "theirs" is data here, not code.
create table if not exists tenant_config (
  tenant_id           uuid primary key references tenants(id) on delete cascade,
  business_brief      text,       -- what they do, in a sentence or two
  tone                text,       -- formal / warm / brisk
  agent_name          text,       -- what the assistant calls itself in replies
  sign_off            text,       -- how every reply signs off
  do_list             text,       -- things the agent should always do
  never_do            text,       -- things the agent must never promise or do
  escalation          text,       -- keywords/topics that must go to a human
  custom_instructions text,       -- freeform catch-all tenant instructions
  examples            text,       -- a few real past replies they were happy with
  updated_at          timestamptz not null default now()
);

-- Migration for existing databases: the four fields added with the tenant
-- instruction layer. Safe to re-run — each no-ops once its column exists.
alter table tenant_config add column if not exists agent_name          text;
alter table tenant_config add column if not exists sign_off            text;
alter table tenant_config add column if not exists do_list             text;
alter table tenant_config add column if not exists custom_instructions text;

-- Full-auto send. When a tenant flips this on, the automatic inbound path sends the
-- agent's reply immediately and marks it 'sent', instead of queuing a pending draft —
-- EXCEPT escalation-matched emails, which are always held for a human. Default false:
-- every tenant starts in the product's safe draft-only mode.
alter table tenant_config add column if not exists auto_send boolean not null default false;

-- Knowledge base chunks, for retrieval (pgvector)
create table if not exists knowledge_chunks (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references tenants(id) on delete cascade,
  content     text,
  embedding   vector(1024),                          -- Voyage 4-series, 1024-dim (see README + migration below)
  source      text,                                  -- e.g. 'price-list.pdf'
  created_at  timestamptz not null default now()
);

-- Every email the agent handled (audit trail + the approval queue)
create table if not exists email_log (
  id              uuid primary key default gen_random_uuid(),
  tenant_id       uuid not null references tenants(id) on delete cascade,
  from_address    text,
  subject         text,
  body_in         text,
  draft_reply     text,
  status          text not null default 'pending',   -- 'pending' | 'approved' | 'sent' | 'skipped'
  created_at      timestamptz not null default now()
);

-- ──────────────────────────────────────────────────────────────
-- Row Level Security  (spec §5: non-negotiable for B2B)
-- ──────────────────────────────────────────────────────────────
-- The background agent uses the Supabase SERVICE ROLE key, which bypasses RLS
-- entirely — so webhook/cron writes are unaffected. These policies protect the
-- DASHBOARD, where a user authenticates with their own JWT and must only ever
-- see their own tenant's rows.

alter table tenants            enable row level security;
alter table tenant_members     enable row level security;
alter table subscriptions      enable row level security;
alter table email_connections  enable row level security;
alter table tenant_config      enable row level security;
alter table knowledge_chunks   enable row level security;
alter table email_log          enable row level security;

-- The set of tenant ids the current user belongs to.
-- security definer so it bypasses RLS on tenant_members (no policy recursion).
create or replace function auth_tenant_ids()
returns setof uuid
language sql stable security definer set search_path = public
as $$
  select tenant_id from tenant_members where user_id = auth.uid() and active   -- active only (security review 2026-09, M1)
$$;

-- A user can see their own membership rows
create policy "members see own memberships" on tenant_members
  for select using (user_id = auth.uid());

-- Members can read/update their tenant
create policy "members read tenant" on tenants
  for select using (id in (select auth_tenant_ids()));
create policy "members update tenant" on tenants
  for update using (id in (select auth_tenant_ids()));

-- Per-tenant isolation on the child tables
create policy "tenant isolation - subscriptions" on subscriptions
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

-- NOTE: email_connections holds OAuth tokens. RLS keeps them tenant-scoped, but
-- the dashboard should never SELECT the token columns. Prefer a column-limited
-- view for any dashboard read. Locked behind RLS is acceptable for the pilot.
create policy "tenant isolation - email_connections" on email_connections
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

create policy "tenant isolation - tenant_config" on tenant_config
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

create policy "tenant isolation - knowledge_chunks" on knowledge_chunks
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

create policy "tenant isolation - email_log" on email_log
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

-- ── Per-tenant data sources (onboarding: system-of-record / connectors) ──────
-- kind = 'tms' | 'calendar' | 'spreadsheet' | 'booking_system'; status =
-- 'requested' | 'connected' | 'skipped'. Multiple per tenant (additive) — and
-- aggregating across tenants tells you which connector to build next.
create table if not exists tenant_data_sources (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references tenants(id) on delete cascade,
  kind        text not null,
  vendor      text,
  status      text not null default 'requested',
  created_at  timestamptz not null default now()
);
alter table tenant_data_sources enable row level security;
drop policy if exists "tenant isolation - tenant_data_sources" on tenant_data_sources;
create policy "tenant isolation - tenant_data_sources" on tenant_data_sources
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

-- ──────────────────────────────────────────────────────────────
-- Vector search
-- ──────────────────────────────────────────────────────────────
-- IVFFlat index for cosine similarity. It needs data to train, so build it
-- AFTER the first knowledge base is loaded (safe to run now; just less optimal
-- while empty).
create index if not exists knowledge_chunks_embedding_idx
  on knowledge_chunks using ivfflat (embedding vector_cosine_ops)
  with (lists = 100);

-- Enterprise multi-mailbox (D4): a chunk can be scoped to ONE mailbox;
-- null = company-wide (everything pre-enterprise already is). Declared here —
-- before match_knowledge_chunks — because the function body references it and
-- `language sql` bodies are validated at creation time. Disconnecting a
-- mailbox deletes its scoped knowledge (cascade); company-wide is untouched.
alter table knowledge_chunks add column if not exists mailbox_id uuid
  references email_connections(id) on delete cascade;

-- Retrieve the closest chunks for one tenant. Called by the agent with the
-- service role (sees all tenants) — the p_tenant_id filter does the scoping.
-- Enterprise multi-mailbox (D4 — layered knowledge): p_mailbox_id unions the
-- company-wide chunks (mailbox_id null) with that mailbox's own; null (the
-- SaaS / unscoped path) matches company-wide only, which is everything a
-- standard tenant has. The old 3-arg signature must be dropped first —
-- PostgREST refuses an RPC with two candidate overloads.
drop function if exists match_knowledge_chunks(uuid, vector, int);
create or replace function match_knowledge_chunks (
  p_tenant_id uuid,
  p_query_embedding vector(1024),
  p_match_count int default 6,
  p_mailbox_id uuid default null
)
returns table (id uuid, content text, source text, similarity float)
language sql stable
as $$
  select
    kc.id,
    kc.content,
    kc.source,
    1 - (kc.embedding <=> p_query_embedding) as similarity
  from knowledge_chunks kc
  where kc.tenant_id = p_tenant_id
    and (kc.mailbox_id is null or kc.mailbox_id = p_mailbox_id)
  order by kc.embedding <=> p_query_embedding
  limit p_match_count
$$;

-- ──────────────────────────────────────────────────────────────
-- Phase 2 migration — knowledge base on Voyage (4-series, 1024-dim)
-- ──────────────────────────────────────────────────────────────
-- The CREATE TABLE above is correct for a fresh install. For an EXISTING
-- database whose knowledge_chunks.embedding is still vector(1536), this resets
-- the dimension. Guarded so it only fires when the dimension is actually wrong —
-- safe to re-run, and it will NOT wipe embeddings once the column is 1024.
-- (pgvector stores the dimension directly in pg_attribute.atttypmod.)
do $$
begin
  if (
    select atttypmod from pg_attribute
    where attrelid = 'public.knowledge_chunks'::regclass and attname = 'embedding'
  ) is distinct from 1024 then
    drop index if exists knowledge_chunks_embedding_idx;
    alter table knowledge_chunks drop column if exists embedding;
    alter table knowledge_chunks add column embedding vector(1024);
    create index knowledge_chunks_embedding_idx
      on knowledge_chunks using ivfflat (embedding vector_cosine_ops) with (lists = 100);
  end if;
end $$;

-- ──────────────────────────────────────────────────────────────
-- OAuth state store (security hardening batch)
-- ──────────────────────────────────────────────────────────────
-- Replaces the in-memory _pending dict in src/app/oauth.py so the CSRF/state binding
-- survives restarts and >1 worker. Service-role only: RLS on, NO policy, so a
-- dashboard user's JWT can never read or write it.
create table if not exists oauth_states (
  state       text primary key,
  tenant_id   uuid not null references tenants(id) on delete cascade,
  created_at  timestamptz not null default now(),
  expires_at  timestamptz not null default (now() + interval '10 minutes')
);
alter table oauth_states enable row level security;

-- ──────────────────────────────────────────────────────────────
-- Usage & cost tracking (Phase 1)
-- ──────────────────────────────────────────────────────────────
-- One row per paid API call (Anthropic draft + Voyage embed), metered at the
-- API call (NOT at send) so usage counts even when a draft is skipped or never
-- sent. cost_usd is computed at write time from app/usage.PRICING; raw token
-- columns are kept so cost can be recomputed if pricing changes.
create table if not exists usage_events (
  id                 uuid primary key default gen_random_uuid(),
  tenant_id          uuid not null references tenants(id) on delete cascade,
  created_at         timestamptz not null default now(),
  provider           text not null,                  -- 'anthropic' | 'voyage'
  model              text not null,                  -- e.g. 'claude-haiku-4-5-20251001'
  operation          text not null,                  -- 'draft' | 'embed_query' | 'embed_ingest'
  module             text not null default 'email',  -- forward-compat: per-module billing
  input_tokens       int not null default 0,
  output_tokens      int not null default 0,
  cache_read_tokens  int not null default 0,
  cache_write_tokens int not null default 0,
  embed_tokens       int not null default 0,
  cost_usd           numeric(12,6) not null default 0,
  email_log_id       uuid                            -- nullable link to the drafted email
);
create index if not exists usage_events_tenant_created_idx
  on usage_events (tenant_id, created_at desc);

alter table usage_events enable row level security;
drop policy if exists "tenant isolation - usage_events" on usage_events;
create policy "tenant isolation - usage_events" on usage_events
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

-- Tenant plan + billing anchor. Billing windows run monthly from billing_started_at
-- (per-tenant, NOT the calendar month). Existing tenants are anchored to created_at.
alter table tenants add column if not exists plan text not null default 'pilot';
alter table tenants add column if not exists billing_started_at timestamptz;
update tenants set billing_started_at = created_at where billing_started_at is null;
alter table tenants alter column billing_started_at set default now();
alter table tenants alter column billing_started_at set not null;

-- Per-business Concierge settings (task timeouts); empty = server defaults.
alter table tenants add column if not exists concierge_settings jsonb not null default '{}'::jsonb;

-- ──────────────────────────────────────────────────────────────
-- Operator console (Phase 2) — platform admins + revenue for margin
-- ──────────────────────────────────────────────────────────────
-- Platform staff (the operator), SEPARATE from tenant users. Service-role only:
-- RLS on, NO policy, so no tenant JWT can ever read or join it. Admins authenticate
-- via Supabase Auth but are AUTHORISED by membership here (app/admin_auth.require_admin),
-- after which admin routes use the service role for cross-tenant reads.
create table if not exists platform_admins (
  user_id    uuid primary key references auth.users(id) on delete cascade,
  email      text,
  role       text not null default 'admin',   -- future RBAC: owner | admin | support | finance
  created_at timestamptz not null default now()
);
alter table platform_admins enable row level security;   -- no policy => service-role only

-- Revenue for the margin view. contract_monthly_gbp (a negotiated MSA value) overrides
-- the plan list price when set; null => fall back to the plan's list price.
alter table tenants add column if not exists contract_monthly_gbp numeric;
-- Customer-facing reporting depth: 'standard' (SMB meter) | 'enterprise' (breakdown + export).
alter table tenants add column if not exists reporting_tier text not null default 'standard';

-- ──────────────────────────────────────────────────────────────
-- Phase 3 — knowledge-base self-improvement loop
-- ──────────────────────────────────────────────────────────────
-- Feature 1 (knowledge gaps): the drafting call self-reports (via structured output)
-- when it deferred a specific fact to a colleague because it wasn't in the knowledge
-- base. We store that missing topic here so the tenant knows exactly what to add.
-- Null = no gap. Feature 2 (flag-incorrect → fix) reuses knowledge_chunks; no migration.
alter table email_log add column if not exists knowledge_gap text;
-- Upgrade signal: the customer asked to book but the tenant has no scheduling
-- module, so the agent deferred. Surfaced to the human as "add Scheduling".
alter table email_log add column if not exists scheduling_gap boolean not null default false;

-- Phase 3.1 — auto-draft idempotency + inbox gap surfacing.
-- source_message_id links a draft back to the inbound provider message it replies to.
-- Microsoft Graph delivers change notifications at-least-once (and retries on a slow
-- ack), so without this a re-delivered notification double-drafts. The partial unique
-- index enforces one draft per inbound message; existing rows keep null and are
-- unaffected (the index only covers non-null ids). It also lets the inbox flag which
-- incoming emails produced a knowledge gap.
alter table email_log add column if not exists source_message_id text;
create unique index if not exists email_log_tenant_source_msg_uniq
  on email_log (tenant_id, source_message_id) where source_message_id is not null;

-- Phase 5 — provider-native threading. thread_id is the email provider's own
-- conversation id (Microsoft Graph conversationId / Gmail threadId), captured at
-- ingest so a conversation = one provider thread, not "all mail from a sender". Used
-- to scope the drafting agent's conversation memory; nullable, with historical rows
-- and any uncaptured path falling back to from_address grouping. No backfill.
alter table email_log add column if not exists thread_id text;
create index if not exists email_log_tenant_thread_idx
  on email_log (tenant_id, thread_id) where thread_id is not null;

-- Gmail auto-draft polling (the Gmail counterpart of the Graph webhook columns above).
-- gmail_history_id is the users.history.list watermark: messages added to INBOX after
-- this point get auto-drafted by src/app/jobs/poll_gmail.py. Null = not yet baselined —
-- the poller (and the OAuth callback) baseline it to "now" first, so a newly-connected
-- mailbox's backlog is never drafted.
alter table email_connections add column if not exists gmail_history_id text;

-- Phase 3.4 — ignored contacts. A sender the tenant has chosen to ignore: the agent
-- never auto-drafts to them and their thread is hidden from the inbox. Manage/undo
-- via the Ignored view. unique(tenant_id,address) so re-ignoring just updates the reason.
create table if not exists ignored_contacts (
  id         uuid primary key default gen_random_uuid(),
  tenant_id  uuid not null references tenants(id) on delete cascade,
  address    text not null,
  reason     text,
  created_at timestamptz not null default now(),
  unique (tenant_id, address)
);
alter table ignored_contacts enable row level security;
drop policy if exists "tenant isolation - ignored_contacts" on ignored_contacts;
create policy "tenant isolation - ignored_contacts" on ignored_contacts
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

-- ──────────────────────────────────────────────────────────────
-- Scheduling module (module 2) — capacity diary
-- ──────────────────────────────────────────────────────────────
-- docs/modules/module-scheduling.md. A booking occupies one window ('day', or 'am'/'pm'
-- when the tenant runs half-days) on one date, against configured capacity. The
-- agent only ever creates 'proposed' holds, which count against capacity until
-- confirmed or released (D4). 'offered' (docs/modules/module-smart-booking-offers.md) is
-- the softer state in front of that: a slot the agent OFFERED in a reply and is
-- exclusively reserving until the customer picks or expires_at passes — it
-- counts against capacity too, so two enquiries are never offered the same
-- slot. "window" is quoted throughout — reserved word.

create table if not exists bookings (
  id           uuid primary key default gen_random_uuid(),
  tenant_id    uuid not null references tenants(id) on delete cascade,
  contact      text,                              -- customer email (cross-module identity seed)
  summary      text not null,                     -- "3 pallets, M17 -> LS9"
  details      jsonb,                             -- industry-shaped extras (tenants.industry steers capture)
  booking_date date not null,
  "window"     text not null default 'day',       -- 'day' | 'am' | 'pm'
  status       text not null default 'proposed',  -- offered | proposed | confirmed | declined | cancelled | completed
  source       text not null default 'email',     -- 'email' | 'manual'  (voice later)
  email_log_id uuid,                              -- the inbound that created it (interconnect)
  thread_id    text,                              -- provider thread (deep-link to the conversation)
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);
create index if not exists bookings_tenant_date_idx on bookings (tenant_id, booking_date);

alter table bookings enable row level security;
drop policy if exists "tenant isolation - bookings" on bookings;
create policy "tenant isolation - bookings" on bookings
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

-- Per-tenant diary shape. One row per tenant; no row = the same defaults,
-- mirrored in app/scheduling.DEFAULT_CONFIG (and in the RPC's capacity fallback).
create table if not exists scheduling_config (
  tenant_id           uuid primary key references tenants(id) on delete cascade,
  capacity_per_window int  not null default 4,
  windows             text not null default 'day',            -- 'day' | 'ampm' | 'slots'
  working_days        text not null default 'mon,tue,wed,thu,fri',
  min_notice_hours    int  not null default 24,
  horizon_days        int  not null default 28,
  blackout_dates      date[] not null default '{}',
  open_time           time not null default '09:00',          -- slots mode: opening hours
  close_time          time not null default '17:00',
  slot_minutes        int  not null default 60,               -- slots mode: appointment length
  updated_at          timestamptz not null default now()
);
-- Migration for existing databases — 'slots' windows mode (D1 unlocked 2026-06-10):
-- real start-time windows generated from opening hours. bookings.window simply
-- carries the start time ('14:00'), no bookings migration needed.
alter table scheduling_config add column if not exists open_time    time not null default '09:00';
alter table scheduling_config add column if not exists close_time   time not null default '17:00';
alter table scheduling_config add column if not exists slot_minutes int  not null default 60;
-- Free travel buffer (minutes) the agent leaves between bookings so it never books back-to-back
-- without travel time — the no-cost baseline of the (paid) travel assistant. 0 = off.
alter table scheduling_config add column if not exists buffer_minutes int not null default 0;
-- Travel assistant on/off — the paid add-on's Diary-settings toggle. Only ever written
-- for tenants with the travel module visible; false = inbound (customers come to you).
alter table scheduling_config add column if not exists travel_mode boolean not null default false;

-- Calendar Connect (v1.1, Outlook first). One column carries both directions:
-- on a PUSHED booking it is the event BLAM8 created in the tenant's calendar
-- (deleted when the booking is cancelled); on an IMPORTED busy-block
-- (source='calendar') it is the originating event's id. Cancelled imports stay
-- as tombstones so the busy-sync never resurrects a block the tenant removed.
alter table bookings add column if not exists calendar_event_id text;
create index if not exists bookings_tenant_calendar_event_idx
  on bookings (tenant_id, calendar_event_id) where calendar_event_id is not null;
-- When the mailbox token grant included calendar scope (set at OAuth callback);
-- null = calendar not connected, drives the Diary's connect banner + the poller set.
alter table email_connections add column if not exists calendar_connected_at timestamptz;
alter table scheduling_config enable row level security;
drop policy if exists "tenant isolation - scheduling_config" on scheduling_config;
create policy "tenant isolation - scheduling_config" on scheduling_config
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

-- Atomic capacity check + insert (service-role RPC, like match_knowledge_chunks).
-- The ONE place capacity is decided: an advisory xact lock per (tenant, date)
-- serialises racing holds, so two concurrent emails can never overbook a window.
-- Returns the inserted row; an EMPTY result means "full" (a clean signal — no
-- exception for PostgREST to wrap). Date validation (notice/horizon/working
-- days/blackouts) happens in src/app/scheduling.py BEFORE this call; only the
-- capacity decision must be atomic. Overlap rule matches app/scheduling._used:
-- a 'day' booking blocks both halves; holding 'day' counts every active booking
-- on the date, whatever its window.
create or replace function hold_booking_if_capacity (
  p_tenant_id    uuid,
  p_booking_date date,
  p_window       text,
  p_summary      text,
  p_details      jsonb default null,
  p_contact      text default null,
  p_source       text default 'email',
  p_thread_id    text default null,
  p_status       text default 'proposed',
  p_expires_at   timestamptz default null   -- offered-slot TTL; null for proposed/confirmed
)
returns setof bookings
language plpgsql volatile
set search_path = public
as $$
declare
  v_capacity int;
  v_used     int;
begin
  -- 'day' | 'am' | 'pm' | a zero-padded start time like '14:00' (slots mode)
  if p_window not in ('day', 'am', 'pm') and p_window !~ '^\d{2}:\d{2}$' then
    raise exception 'invalid window: %', p_window;
  end if;
  if p_status not in ('offered', 'proposed', 'confirmed') then
    raise exception 'invalid hold status: %', p_status;
  end if;

  -- One writer per (tenant, date); racing holds for the same day queue here.
  perform pg_advisory_xact_lock(
    hashtextextended(p_tenant_id::text || ':' || p_booking_date::text, 0)
  );

  select capacity_per_window into v_capacity
  from scheduling_config where tenant_id = p_tenant_id;
  v_capacity := coalesce(v_capacity, 4);   -- no config row yet → default capacity

  -- Cross-mode overlap (mirrors app/scheduling._overlaps): 'day' blocks everything;
  -- 'am'/'pm' block slot times in their half; a slot blocks only itself.
  -- PEOPLE rows only (b.spaces is null — mirrors app/scheduling._used): a
  -- sized row is a transport booking on the fleet's capacity model and must
  -- never consume an office window (transport-planner D1, two parallel
  -- diaries). Windows-only tenants have no sized rows — behaviour unchanged.
  -- Offered slots count too (exclusive offers — docs/modules/module-smart-booking-offers.md),
  -- but an EXPIRED offer stops blocking capacity the moment its deadline passes,
  -- even before the release sweep has cancelled the row.
  select count(*) into v_used
  from bookings b
  where b.tenant_id = p_tenant_id
    and b.booking_date = p_booking_date
    and (b.status in ('proposed', 'confirmed')
         or (b.status = 'offered' and (b.expires_at is null or b.expires_at > now())))
    and b.spaces is null
    and (
      p_window = 'day' or b."window" = 'day' or b."window" = p_window
      or (b."window" = 'am' and p_window ~ '^\d{2}:\d{2}$' and p_window <  '12:00')
      or (b."window" = 'pm' and p_window ~ '^\d{2}:\d{2}$' and p_window >= '12:00')
      or (p_window = 'am' and b."window" ~ '^\d{2}:\d{2}$' and b."window" <  '12:00')
      or (p_window = 'pm' and b."window" ~ '^\d{2}:\d{2}$' and b."window" >= '12:00')
    );

  if v_used >= v_capacity then
    return;   -- full — empty result set
  end if;

  return query
    insert into bookings (tenant_id, contact, summary, details, booking_date, "window", status, source, thread_id, booking_ref, expires_at)
    values (p_tenant_id, p_contact, p_summary, p_details, p_booking_date, p_window, p_status, p_source, p_thread_id, next_booking_ref(p_tenant_id), p_expires_at)
    returning *;
end $$;

-- ──────────────────────────────────────────────────────────────
-- Enterprise multi-mailbox (docs/architecture/enterprise-multi-mailbox.md)
-- ──────────────────────────────────────────────────────────────
-- D1: email_connections IS the mailbox entity. It gains the address and the
-- provider (moved off tenants — one enterprise can mix Outlook depots with a
-- Gmail branch) plus the grouping labels (D6: free text in v1).
-- tenants.mailbox/provider remain as a denormalised mirror of the DEFAULT
-- (oldest) mailbox for legacy reads, synced on connect/disconnect.
alter table email_connections add column if not exists address    text;
alter table email_connections add column if not exists provider   text;
alter table email_connections add column if not exists label      text;
alter table email_connections add column if not exists department text;
alter table email_connections add column if not exists category   text;
update email_connections ec set provider = t.provider, address = t.mailbox
from tenants t where ec.tenant_id = t.id and ec.provider is null;

-- D3: per-mailbox brain = tenant defaults ⊕ these overrides. ALL persona
-- columns nullable — null means "inherit tenant_config" (a mailbox with no row
-- at all is the pure SaaS identity). `purpose` is the one-line role framing
-- ("this mailbox handles order intake"); `auto_send` lets orders@ run
-- full-auto while complaints@ stays draft-only. business_brief deliberately
-- absent: company identity is company-wide.
create table if not exists mailbox_config (
  mailbox_id  uuid primary key references email_connections(id) on delete cascade,
  tenant_id   uuid not null references tenants(id) on delete cascade,  -- RLS wall
  purpose     text,
  tone        text,
  agent_name  text,
  sign_off    text,
  do_list     text,
  never_do    text,
  escalation  text,
  custom_instructions text,
  examples    text,
  auto_send   boolean,                       -- null = inherit tenant_config
  updated_at  timestamptz not null default now()
);
alter table mailbox_config enable row level security;
drop policy if exists "tenant isolation - mailbox_config" on mailbox_config;
create policy "tenant isolation - mailbox_config" on mailbox_config
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

-- D5: the pipeline stamps which mailbox an email arrived in. Nullable, no FK:
-- legacy rows (and SaaS rows) stay null and render under the default mailbox,
-- and disconnecting a mailbox must not erase its audit history.
alter table email_log add column if not exists mailbox_id uuid;
create index if not exists email_log_tenant_mailbox_idx
  on email_log (tenant_id, mailbox_id) where mailbox_id is not null;

-- Connect flow: the enterprise "Add mailbox" link passes add-mode through the
-- OAuth round-trip; the callback then inserts alongside the tenant's existing
-- connections (or refreshes the row with the same address) instead of the SaaS
-- replace-all. Bound server-side at /oauth/connect — never trusted from the
-- callback query string.
alter table oauth_states add column if not exists mode text not null default 'replace';

-- ──────────────────────────────────────────────────────────────
-- Draft-quality stamps (operator console: approval / edit rates)
-- ──────────────────────────────────────────────────────────────
-- Stamped when a row reaches 'sent'. sent_via: 'human' (approved in the
-- dashboard) | 'auto' (full-auto pipeline send). edited: did the human change
-- the agent's text before approving (line-ending-normalised compare)? Auto
-- sends are edited=false by definition. NULL on both = a legacy row sent
-- before these stamps existed (excluded from the rates, never guessed).
-- The agent's draft itself is overwritten by human edits (by design — the
-- edited text is what was sent and audited), so the boolean is captured AT
-- send time; rates therefore accrue from deploy onward only.
alter table email_log add column if not exists sent_via text;
alter table email_log add column if not exists edited   boolean;
-- (knowledge_chunks.mailbox_id + the match_knowledge_chunks union live in the
-- Vector search section above — the function body references the column.)

-- ──────────────────────────────────────────────────────────────
-- Fleet / pallet capacity module (docs/modules/module-fleet-capacity.md)
-- ──────────────────────────────────────────────────────────────
-- Vehicles become the unit of capacity: demand has a size (pallet spaces,
-- optionally weight) and supply is a pool of vehicles, each with its own size.
-- Everything else — atomic holds, proposed→confirmed lifecycle, the Diary,
-- calendar push — is the scheduling machinery reused. A subscriptions row
-- module='fleet' is the whole rollout; when active it SUPERSEDES windows-mode
-- capacity (the windows/capacity_per_window config is ignored).

create table if not exists branches (
  id         uuid primary key default gen_random_uuid(),
  tenant_id  uuid not null references tenants(id) on delete cascade,
  name       text not null,
  created_at timestamptz not null default now()
);

create table if not exists vehicles (
  id              uuid primary key default gen_random_uuid(),
  tenant_id       uuid not null references tenants(id) on delete cascade,
  branch_id       uuid references branches(id) on delete set null,
  name            text not null,                    -- "18t curtainsider" / reg
  capacity_spaces numeric(6,2) not null,            -- pallet spaces
  max_payload_kg  numeric(8,1),                     -- legal limit; null = not enforced
  active          boolean not null default true,    -- inactive: kept for history, no new holds
  created_at      timestamptz not null default now()
);
create index if not exists vehicles_tenant_idx on vehicles (tenant_id);

-- Seeded per tenant on fleet activation: full 1.0 · euro 1.0 · half 0.5 ·
-- quarter 0.25 (D2). Read-only in the v1 UI; an unknown type in an email is
-- treated as full (conservative) and the draft says what it assumed.
create table if not exists pallet_types (
  id           uuid primary key default gen_random_uuid(),
  tenant_id    uuid not null references tenants(id) on delete cascade,
  name         text not null,
  space_factor numeric(4,2) not null,
  unique (tenant_id, name)
);

-- A fleet booking is a normal bookings row that carries its size and the van
-- it was placed on. vehicle_id null = windows-mode booking, or a fleet booking
-- whose vehicle was deleted (the Diary shows it "unassigned" for a human to
-- re-place; the date is kept). spaces null = legacy windows-mode row.
alter table bookings add column if not exists vehicle_id uuid
  references vehicles(id) on delete set null;
alter table bookings add column if not exists spaces    numeric(6,2);
alter table bookings add column if not exists weight_kg numeric(8,1);

-- D4 — branch resolution leg 2: the receiving mailbox's default branch
-- (enterprise: orders@leeds books Leeds vans). Null = no default.
alter table mailbox_config add column if not exists default_branch_id uuid
  references branches(id) on delete set null;

alter table branches     enable row level security;
alter table vehicles     enable row level security;
alter table pallet_types enable row level security;
drop policy if exists "tenant isolation - branches" on branches;
create policy "tenant isolation - branches" on branches
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));
drop policy if exists "tenant isolation - vehicles" on vehicles;
create policy "tenant isolation - vehicles" on vehicles
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));
drop policy if exists "tenant isolation - pallet_types" on pallet_types;
create policy "tenant isolation - pallet_types" on pallet_types
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

-- The ONE atomic fleet capacity decision (the fleet sibling of
-- hold_booking_if_capacity): same advisory xact lock per (tenant, date), so
-- fleet holds, windows holds and manual adds for the same day all serialise.
-- Best-fit (D1): among ACTIVE vehicles in the branch (null = whole fleet),
-- pick the one with the SMALLEST remaining space that still fits — ties broken
-- by smallest capacity, then id, so selection is deterministic and big vans
-- stay free for big orders. Weight (D2) is the legal second constraint:
-- enforced only when BOTH the order states a weight and the vehicle has a
-- limit; bookings with no recorded weight count 0 kg (the spec is honest that
-- weight enforcement is only as good as what customers declare).
-- Returns the inserted row; an EMPTY result means no single van fits (one
-- vehicle per order — splitting is v2). Date validation (notice/horizon/
-- working days/blackouts) happens in src/app/scheduling.py BEFORE this call.
create or replace function hold_fleet_booking (
  p_tenant_id    uuid,
  p_booking_date date,
  p_summary      text,
  p_spaces       numeric,
  p_weight_kg    numeric default null,
  p_branch_id    uuid default null,
  p_details      jsonb default null,
  p_contact      text default null,
  p_source       text default 'email',
  p_thread_id    text default null,
  p_status       text default 'proposed'
)
returns setof bookings
language plpgsql volatile
set search_path = public
as $$
declare
  v_vehicle uuid;
begin
  if p_status not in ('proposed', 'confirmed') then
    raise exception 'invalid hold status: %', p_status;
  end if;
  if p_spaces is null or p_spaces <= 0 then
    raise exception 'invalid spaces: %', p_spaces;
  end if;

  -- One writer per (tenant, date) — same lock key as hold_booking_if_capacity.
  perform pg_advisory_xact_lock(
    hashtextextended(p_tenant_id::text || ':' || p_booking_date::text, 0)
  );

  select v.id into v_vehicle
  from vehicles v
  left join lateral (
    select coalesce(sum(b.spaces), 0)    as used_spaces,
           coalesce(sum(b.weight_kg), 0) as used_kg
    from bookings b
    where b.tenant_id = p_tenant_id
      and b.vehicle_id = v.id
      and b.booking_date = p_booking_date
      and b.status in ('proposed', 'confirmed')
  ) load on true
  where v.tenant_id = p_tenant_id
    and v.active
    and (p_branch_id is null or v.branch_id = p_branch_id)
    and v.capacity_spaces - load.used_spaces >= p_spaces
    and (p_weight_kg is null or v.max_payload_kg is null
         or v.max_payload_kg - load.used_kg >= p_weight_kg)
  order by v.capacity_spaces - load.used_spaces asc, v.capacity_spaces asc, v.id asc
  limit 1;

  if v_vehicle is null then
    return;   -- no single van fits — empty result set
  end if;

  return query
    insert into bookings (tenant_id, contact, summary, details, booking_date, "window",
                          status, source, thread_id, vehicle_id, spaces, weight_kg,
                          booking_ref)
    values (p_tenant_id, p_contact, p_summary, p_details, p_booking_date, 'day',
            p_status, p_source, p_thread_id, v_vehicle, p_spaces, p_weight_kg,
            next_booking_ref(p_tenant_id))
    returning *;
end $$;

-- ─────────────────────────────────────────────────────────────────────────────
-- Route planning module (docs/modules/module-route-planning.md — LOCKED 2026-06-11).
-- Location becomes data: vehicles gain a registration, branches an address
-- (the route origin), bookings the collection/delivery postcodes and the
-- customer's own reference — captured by the agent ONLY when the email states
-- them (D2; the routing tool variant adds the optional args). plans is the
-- planner agent's propose→approve unit (D5): applying a plan re-runs the
-- atomic capacity RPC per move in ONE transaction, so a plan can never
-- overload a van. A subscriptions row module='routing' is the rollout;
-- the admin toggle requires fleet to be active first.

alter table vehicles add column if not exists registration text;   -- number plate
alter table branches add column if not exists address  text;
alter table branches add column if not exists postcode text;       -- route origin

alter table bookings add column if not exists collection_postcode text;
alter table bookings add column if not exists delivery_postcode  text;
alter table bookings add column if not exists reference          text;  -- customer order/invoice ref

create table if not exists plans (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references tenants(id) on delete cascade,
  plan_date   date not null,
  status      text not null default 'proposed',  -- proposed | applied | discarded
  summary     text,                              -- the agent's one-line rationale
  moves       jsonb not null,                    -- [{booking_id, to_vehicle_id, to_date, split…}]
  created_at  timestamptz not null default now(),
  resolved_at timestamptz
);
create index if not exists plans_tenant_date_idx on plans (tenant_id, plan_date);

alter table plans enable row level security;
drop policy if exists "tenant isolation - plans" on plans;
create policy "tenant isolation - plans" on plans
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

-- ─────────────────────────────────────────────────────────────────────────────
-- Booking references (normal-use fix, 2026-06-11). Real bookings get a
-- HUMAN reference the business issues on confirmation ("BK-1042") — the thing
-- a customer quotes in every follow-up. Minted atomically by next_booking_ref
-- INSIDE both hold RPCs, so every real booking path (agent hold, manual add,
-- order-book import, windows or fleet) gets one and racing holds can't collide;
-- calendar busy-blocks bypass the RPCs and correctly get none. The counter is
-- per tenant (each business sees its own tidy sequence, starting BK-1001).

create table if not exists tenant_counters (
  tenant_id   uuid primary key references tenants(id) on delete cascade,
  booking_seq integer not null default 0
);
alter table tenant_counters enable row level security;
drop policy if exists "tenant isolation - tenant_counters" on tenant_counters;
create policy "tenant isolation - tenant_counters" on tenant_counters
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

alter table bookings add column if not exists booking_ref text;
create index if not exists bookings_tenant_booking_ref_idx
  on bookings (tenant_id, booking_ref);

create or replace function next_booking_ref (p_tenant_id uuid)
returns text
language plpgsql volatile
set search_path = public
as $$
declare
  v_n integer;
begin
  insert into tenant_counters (tenant_id, booking_seq) values (p_tenant_id, 1)
  on conflict (tenant_id) do update set booking_seq = tenant_counters.booking_seq + 1
  returning booking_seq into v_n;
  return 'BK-' || (1000 + v_n);
end $$;

-- ─────────────────────────────────────────────────────────────────────────────
-- Stripe billing (subscription lifecycle as the source of truth)
-- Hosted Checkout + Customer Portal: no card data ever touches our box.
-- subscription_status is SEPARATE from the legacy `plan` (which is the module
-- bundle for feature gating) — status drives access (require_subscription).
-- billing_provider distinguishes self-serve Stripe customers from enterprise
-- MSA invoice billing and from grandfathered tenants that pre-date this column.
alter table tenants add column if not exists stripe_customer_id      text;
alter table tenants add column if not exists stripe_subscription_id  text;
alter table tenants add column if not exists subscription_status     text not null default 'none';
alter table tenants add column if not exists subscription_price_id   text;
alter table tenants add column if not exists trial_ends_at           timestamptz;
alter table tenants add column if not exists current_period_end      timestamptz;
alter table tenants add column if not exists billing_provider        text not null default 'none';

create unique index if not exists tenants_stripe_customer_id_uniq
  on tenants (stripe_customer_id) where stripe_customer_id is not null;
create unique index if not exists tenants_stripe_subscription_id_uniq
  on tenants (stripe_subscription_id) where stripe_subscription_id is not null;

-- Backfill (Scout review): BLAM8 already has live tenants from launch on 2026-06-19.
-- Without this, the day the access gate flips on, every existing tenant gets
-- redirected to /billing/plans and locked out. Anyone who completed onboarding
-- BEFORE Stripe was added is grandfathered to 'active' so they keep working;
-- the admin can convert them to real Stripe customers later from the operator
-- console. Brand-new signups land on status='none' (unchanged default).
update tenants
   set subscription_status = 'active',
       billing_provider    = 'grandfathered'
 where onboarded_at is not null
   and subscription_status = 'none'
   and billing_provider    = 'none';

-- Webhook event de-dup. Stripe retries delivery (up to 3 days) and at-least-once
-- semantics mean the same event id can arrive twice. The unique primary key on
-- the Stripe event id makes idempotency a write-time SQL guarantee — the
-- webhook handler does an INSERT … ON CONFLICT DO NOTHING and bails if the
-- conflict fires, so notifications and audit-log rows are written exactly once.
-- Service role only (no RLS policy — tenant JWTs never need to read it).
create table if not exists stripe_events (
  id          text primary key,                    -- evt_... (Stripe event id)
  type        text not null,                       -- e.g. 'customer.subscription.updated'
  received_at timestamptz not null default now()
);
alter table stripe_events enable row level security;

-- ─────────────────────────────────────────────────────────────────────────────
-- Transport planner module (docs/modules/module-transport-planner.md — READY TO BUILD
-- 2026-06-11). Two pieces: postcode geometry for plan scoring, and the
-- propose→approve→apply machinery over the existing plans table.

-- Postcode → lat/lng, filled lazily from the free postcodes.io bulk API and
-- cached forever (D4). lat/lng are NULLABLE so a postcodes.io miss (a bad or
-- retired postcode) is cached too — the planner then scores that drop by size
-- only instead of re-querying a known dud on every run. Reference data shared
-- by every tenant, written only by the service role: RLS on with NO policy
-- (the oauth_states pattern) — tenants never read it directly, the planner
-- runs server-side.
create table if not exists postcodes (
  postcode   text primary key,          -- normalised, e.g. 'LS9 8AS'
  lat        double precision,
  lng        double precision,
  fetched_at timestamptz not null default now()
);
alter table postcodes enable row level security;

-- The auto ladder (D6): mirrors auto_send. When on, a plan containing ONLY the
-- safe class of moves (same-day vehicle moves) applies itself; anything else —
-- and any plan while this is off — waits for a human on the board.
alter table tenant_config add column if not exists planner_auto_apply boolean not null default false;

-- Apply one proposed plan ATOMICALLY (D6): every move in one transaction,
-- under the same advisory lock as the hold RPCs, so plan applies, agent holds
-- and manual adds for that (tenant, date) all serialise. Moves are UPDATEs —
-- the booking row keeps its identity (booking_ref, email_log_id link,
-- calendar_event_id), capacity semantics are release+re-hold. After every
-- move lands, EVERY vehicle on the date is re-validated against the same
-- fits-or-fails judgement as hold_fleet_booking; any violation raises (named
-- loudly) and rolls back the WHOLE plan — stale plans die, never half-apply.
-- v1 applies same-day vehicle moves only; a move carrying to_date or split
-- is rejected outright (date changes always need the human approval flow).
create or replace function apply_plan (
  p_plan_id   uuid,
  p_tenant_id uuid
)
returns setof plans
language plpgsql volatile
set search_path = public
as $$
declare
  v_plan    plans%rowtype;
  v_move    jsonb;
  v_booking bookings%rowtype;
  v_vehicle vehicles%rowtype;
  v_bad     record;
begin
  select * into v_plan from plans
  where id = p_plan_id and tenant_id = p_tenant_id
  for update;
  if not found then
    raise exception 'plan not found';
  end if;
  if v_plan.status <> 'proposed' then
    raise exception 'plan already %', v_plan.status;
  end if;

  perform pg_advisory_xact_lock(
    hashtextextended(p_tenant_id::text || ':' || v_plan.plan_date::text, 0)
  );

  for v_move in select * from jsonb_array_elements(v_plan.moves)
  loop
    if (v_move ? 'to_date') or (v_move ? 'split') then
      raise exception 'plan contains a move that cannot be auto-applied (date move or split) — it needs a person';
    end if;
    select * into v_booking from bookings
    where id = (v_move->>'booking_id')::uuid and tenant_id = p_tenant_id;
    if not found then
      raise exception 'a booking in this plan no longer exists — the board has changed, replan the day';
    end if;
    if v_booking.status not in ('proposed', 'confirmed') then
      raise exception 'booking % is no longer active (now %) — replan the day',
        coalesce(v_booking.booking_ref, left(v_booking.id::text, 8)), v_booking.status;
    end if;
    if v_booking.booking_date <> v_plan.plan_date then
      raise exception 'booking % has moved to another date since this plan was proposed — replan the day',
        coalesce(v_booking.booking_ref, left(v_booking.id::text, 8));
    end if;
    if v_booking.spaces is null then
      raise exception 'booking % has no pallet size — it cannot be planned',
        coalesce(v_booking.booking_ref, left(v_booking.id::text, 8));
    end if;
    select * into v_vehicle from vehicles
    where id = (v_move->>'to_vehicle_id')::uuid and tenant_id = p_tenant_id;
    if not found then
      raise exception 'a target vehicle in this plan no longer exists — replan the day';
    end if;
    if not v_vehicle.active then
      raise exception 'vehicle % was deactivated since this plan was proposed — replan the day', v_vehicle.name;
    end if;
    update bookings set vehicle_id = v_vehicle.id, updated_at = now()
    where id = v_booking.id;
  end loop;

  -- The whole day re-validated at once, so cumulative interactions between
  -- moves (A frees space B relies on) are judged on the final state.
  select v.name,
         coalesce(sum(b.spaces), 0)    as used_spaces,
         v.capacity_spaces             as cap
  into v_bad
  from vehicles v
  join bookings b
    on b.vehicle_id = v.id
   and b.tenant_id = p_tenant_id
   and b.booking_date = v_plan.plan_date
   and b.status in ('proposed', 'confirmed')
  where v.tenant_id = p_tenant_id
  group by v.id
  having coalesce(sum(b.spaces), 0) > v.capacity_spaces
      or (v.max_payload_kg is not null
          and coalesce(sum(b.weight_kg), 0) > v.max_payload_kg)
  limit 1;
  if found then
    raise exception 'applying this plan would overload % (% of % spaces) — replan the day',
      v_bad.name, v_bad.used_spaces, v_bad.cap;
  end if;

  return query
    update plans set status = 'applied', resolved_at = now()
    where id = p_plan_id
    returning *;
end $$;

-- ─────────────────────────────────────────────────────────────────────────────
-- TMS baseline — drivers, pricing, POD/tracking/portal, invoicing, compliance
-- (docs/modules/module-tms-baseline.md — BUILDING 2026-06-20). The logistics add-on's
-- post-dispatch lifecycle, demo-grade: every screen reads the SAME bookings
-- diary the agent writes into. ONE event spine (shipment_events) powers POD +
-- live tracking + the customer portal. fleet+routing collapse into one 'tms'
-- module (the merge — converted at the foot of this section). All tenant-isolated.

-- Drivers — who runs the vans. Driver-side compliance (licence + CPC) lives here;
-- the vehicle-side dates live on vehicles. A driver is a vehicle's usual driver
-- (vehicles.driver_id) so the driver app can show "your" van's manifest.
create table if not exists drivers (
  id              uuid primary key default gen_random_uuid(),
  tenant_id       uuid not null references tenants(id) on delete cascade,
  name            text not null,
  phone           text,
  licence_no      text,
  licence_expiry  date,
  cpc_expiry      date,                              -- Driver CPC card expiry
  active          boolean not null default true,
  created_at      timestamptz not null default now()
);
create index if not exists drivers_tenant_idx on drivers (tenant_id);

-- Vehicle compliance + its usual driver (the merge gave TMS the whole vehicle
-- record; these are the regulated dates a transport office chases).
alter table vehicles add column if not exists driver_id        uuid references drivers(id) on delete set null;
alter table vehicles add column if not exists mot_expiry       date;
alter table vehicles add column if not exists tax_expiry       date;
alter table vehicles add column if not exists service_due      date;
alter table vehicles add column if not exists insurance_expiry date;

-- Rate cards — how a job is priced. One default per tenant, seeded on TMS
-- activation; editable in the UI. quote = base + per_mile·miles + per_pallet·
-- spaces + per_drop, uplifted by fuel_pct, floored at min_charge. Miles are the
-- collection→delivery great-circle distance (haversine over the postcodes cache).
create table if not exists rate_cards (
  id           uuid primary key default gen_random_uuid(),
  tenant_id    uuid not null references tenants(id) on delete cascade,
  name         text not null default 'Standard',
  base_charge  numeric(10,2) not null default 0,
  per_mile     numeric(10,2) not null default 0,
  per_pallet   numeric(10,2) not null default 0,
  per_drop     numeric(10,2) not null default 0,
  min_charge   numeric(10,2) not null default 0,
  fuel_pct     numeric(5,2)  not null default 0,     -- fuel surcharge %
  currency     text not null default 'GBP',
  active       boolean not null default true,
  created_at   timestamptz not null default now()
);
create index if not exists rate_cards_tenant_idx on rate_cards (tenant_id);

-- The quote stamped on a booking (pricing → invoicing reads it back), and a
-- random public token backing the customer tracking portal (/track/{token}).
alter table bookings add column if not exists quoted_amount numeric(10,2);
alter table bookings add column if not exists track_token   text;
create unique index if not exists bookings_track_token_uniq
  on bookings (track_token) where track_token is not null;

-- The shipment event spine — POD, live tracking and the portal all read these.
-- kind ∈ booked|loaded|out_for_delivery|delivered|failed|note. The delivered/
-- failed event carries the POD (signed_by + photo_url + note). lat/lng are
-- reserved for a real tracker feed (the demo simulates positions along the route).
create table if not exists shipment_events (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references tenants(id) on delete cascade,
  booking_id  uuid not null references bookings(id) on delete cascade,
  kind        text not null,
  note        text,
  signed_by   text,
  photo_url   text,
  lat         double precision,
  lng         double precision,
  driver_id   uuid references drivers(id) on delete set null,
  created_at  timestamptz not null default now()
);
create index if not exists shipment_events_booking_idx on shipment_events (booking_id, created_at);
create index if not exists shipment_events_tenant_idx  on shipment_events (tenant_id, created_at);

-- Invoices — group delivered bookings by customer (bookings.contact) over a
-- period; one line per booking at its quoted_amount, VAT at 20%. Ref minted like
-- booking refs (next_invoice_ref). status draft|sent|paid (manual for the demo).
create table if not exists invoices (
  id             uuid primary key default gen_random_uuid(),
  tenant_id      uuid not null references tenants(id) on delete cascade,
  invoice_ref    text,
  customer_email text,
  customer_name  text,
  status         text not null default 'draft',
  period_from    date,
  period_to      date,
  subtotal       numeric(12,2) not null default 0,
  vat            numeric(12,2) not null default 0,
  total          numeric(12,2) not null default 0,
  created_at     timestamptz not null default now()
);
create index if not exists invoices_tenant_idx on invoices (tenant_id, created_at);

create table if not exists invoice_lines (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references tenants(id) on delete cascade,  -- denormalised for RLS
  invoice_id  uuid not null references invoices(id) on delete cascade,
  booking_id  uuid references bookings(id) on delete set null,
  description text not null,
  amount      numeric(12,2) not null default 0
);
create index if not exists invoice_lines_invoice_idx on invoice_lines (invoice_id);

-- Invoice ref counter (mirrors booking_seq → next_booking_ref). INV-1001…
alter table tenant_counters add column if not exists invoice_seq integer not null default 0;
create or replace function next_invoice_ref (p_tenant_id uuid)
returns text
language plpgsql volatile
set search_path = public
as $$
declare
  v_n integer;
begin
  insert into tenant_counters (tenant_id, invoice_seq) values (p_tenant_id, 1)
  on conflict (tenant_id) do update set invoice_seq = tenant_counters.invoice_seq + 1
  returning invoice_seq into v_n;
  return 'INV-' || (1000 + v_n);
end $$;

-- Driver hours (compliance — Working Time / tacho proxy). Seeded for the demo;
-- a real integration reads the tachograph. One row per driver per work day.
create table if not exists driver_hours (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references tenants(id) on delete cascade,
  driver_id   uuid not null references drivers(id) on delete cascade,
  work_date   date not null,
  hours       numeric(5,2) not null default 0,
  created_at  timestamptz not null default now()
);
create index if not exists driver_hours_tenant_idx on driver_hours (tenant_id, work_date);

-- Feature requests — tenants tell us what to build (in-app form). industry is
-- auto-tagged from tenants.industry at submit time; the operator reads them all
-- at /admin/requests.
create table if not exists feature_requests (
  id           uuid primary key default gen_random_uuid(),
  tenant_id    uuid not null references tenants(id) on delete cascade,
  submitted_by text,                                 -- the submitting user's email
  category     text,
  body         text not null,
  industry     text,
  status       text not null default 'new',          -- new|reviewing|planned|done
  created_at   timestamptz not null default now()
);
create index if not exists feature_requests_tenant_idx on feature_requests (tenant_id, created_at);

-- RLS — tenant isolation on every new table (the house pattern).
alter table drivers          enable row level security;
alter table rate_cards       enable row level security;
alter table shipment_events  enable row level security;
alter table invoices         enable row level security;
alter table invoice_lines    enable row level security;
alter table driver_hours     enable row level security;
alter table feature_requests enable row level security;

drop policy if exists "tenant isolation - drivers" on drivers;
create policy "tenant isolation - drivers" on drivers
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));
drop policy if exists "tenant isolation - rate_cards" on rate_cards;
create policy "tenant isolation - rate_cards" on rate_cards
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));
drop policy if exists "tenant isolation - shipment_events" on shipment_events;
create policy "tenant isolation - shipment_events" on shipment_events
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));
drop policy if exists "tenant isolation - invoices" on invoices;
create policy "tenant isolation - invoices" on invoices
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));
drop policy if exists "tenant isolation - invoice_lines" on invoice_lines;
create policy "tenant isolation - invoice_lines" on invoice_lines
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));
drop policy if exists "tenant isolation - driver_hours" on driver_hours;
create policy "tenant isolation - driver_hours" on driver_hours
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));
drop policy if exists "tenant isolation - feature_requests" on feature_requests;
create policy "tenant isolation - feature_requests" on feature_requests
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

-- The merge: fleet + routing → one 'tms' module. Convert existing rows so live
-- tenants keep every transport surface. Idempotent (no-op on a fresh install).
insert into subscriptions (tenant_id, module, status)
  select distinct s.tenant_id, 'tms', 'active'
    from subscriptions s
   where s.module = 'fleet' and s.status = 'active'
     and not exists (select 1 from subscriptions t
                      where t.tenant_id = s.tenant_id and t.module = 'tms');
delete from subscriptions where module in ('fleet', 'routing');

-- ─────────────────────────────────────────────────────────────────────────────
-- Trial lock + email cap (src/app/trial.py — REQUIRE_SUBSCRIPTION_GATE)
-- The free trial a new tenant lands on at sign-up: 14 days OR the trial email
-- pot (TRIAL_EMAIL_CAP env, default 100), whichever first. SEPARATE from subscription_status (the Stripe access gate) —
-- a tenant with an active/trialing subscription is "converted" and never
-- trial-limited. trial_status ∈ active|grace_active|converted|expired_time|
-- expired_cap; the live decision is computed in app/trial.evaluate(), this
-- column is the cached/displayed value. trial_start_at defaults to now() so a
-- brand-new tenant starts the clock at row creation; existing tenants are
-- anchored to created_at.
alter table tenants add column if not exists trial_start_at    timestamptz;
alter table tenants add column if not exists trial_emails_used integer not null default 0;
alter table tenants add column if not exists trial_status      text not null default 'active';
alter table tenants add column if not exists trial_grace_until timestamptz;

update tenants set trial_start_at = created_at where trial_start_at is null;
alter table tenants alter column trial_start_at set default now();

-- Don't be a jerk to early users: any tenant already PAST the 14-day notional
-- trial at this deploy AND not on an active/trialing subscription gets a 7-day
-- grace runway before they're treated as expired. (Launch tenants were
-- grandfathered to subscription_status='active', so this is a no-op for them.)
update tenants
   set trial_status = 'grace_active',
       trial_grace_until = now() + interval '7 days'
 where subscription_status not in ('active', 'trialing')
   and coalesce(trial_start_at, created_at) < now() - interval '14 days'
   and trial_status = 'active';

-- Atomic email-counter increment. A single UPDATE (col = col + 1) takes the row
-- lock, so concurrent webhook/poller deliveries serialise and never lose a
-- count. Flips trial_status to 'expired_cap' once the cap is reached; a
-- converted tenant's status is left alone. Service-role RPC (app/db.increment_trial_email).
create or replace function increment_trial_email (
  p_tenant_id uuid,
  p_cap       integer default 1000
)
returns integer
language sql volatile
as $$
  update tenants
     set trial_emails_used = coalesce(trial_emails_used, 0) + 1,
         trial_status = case
            when trial_status = 'converted' then 'converted'
            when coalesce(trial_emails_used, 0) + 1 >= p_cap then 'expired_cap'
            else trial_status
         end
   where id = p_tenant_id
  returning trial_emails_used;
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- Google sign-in slots — RETIRED 2026-08-02 (kept for prod compatibility).
-- The invite gate moved from SSO sign-in to the Gmail mailbox connect (the step
-- that actually burns Google's 100 unverified-app grants): src/app/slots.py now
-- counts email_connections rows with provider='google' via a plain table query,
-- so nothing calls this function any more. Left in place — dropping it is a
-- needless prod migration.
-- Original purpose: distinct Google sign-ups against the 100-cap. The sign-in
-- identity lives in auth.identities (provider = 'google'|'azure'), which
-- PostgREST can't expose directly — so a SECURITY DEFINER function in the
-- public schema reads it for the service role. Counts DISTINCT user_id so a user
-- with multiple identity rows is one signup.
create or replace function count_provider_signups (p_provider text)
returns integer
language sql stable security definer set search_path = auth, public
as $$
  select count(distinct user_id)::int from auth.identities where provider = p_provider;
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- Agent activity log (app/agent_log.py — the burger-menu transparency page)
-- A curated, human-readable narrative of what the agents DID for a customer email:
-- drafted a reply, asked the transport desk (the hand-off — docs/module-agent-
-- handoff.md), held a booking. NOT token metering (that's usage_events) and not the
-- raw approval queue (that's email_log) — this is the "why did THIS land in the
-- calendar" trace. One row per key action, written best-effort by the pipeline
-- (service role), so a failed insert never breaks drafting.
--   kind ∈ drafted | transport | office_booking | sent
--   direction ∈ inbound | outbound  — 'outbound' future-proofs the Phase 2B reverse
--     hand-off (a specialist-initiated message the base composes); v1 is all inbound.
--   event_date is the DIARY date the action concerns (a held booking's date), so the
--     page can answer "what put something on THIS calendar day"; null when the action
--     isn't about a specific date.
--   email_log_id → the conversation; booking_id / booking_ref → the calendar entry;
--     detail is a small jsonb bag (outcome, badges, offered dates, the asked question).
--   mailbox_id is a bare uuid (matches email_log) — the receiving inbox, enterprise.
create table if not exists agent_events (
  id           uuid primary key default gen_random_uuid(),
  tenant_id    uuid not null references tenants(id) on delete cascade,
  mailbox_id   uuid,
  email_log_id uuid references email_log(id) on delete set null,
  booking_id   uuid references bookings(id) on delete set null,
  booking_ref  text,
  kind         text not null,
  direction    text not null default 'inbound',
  contact      text,
  subject      text,
  summary      text not null,
  event_date   date,
  detail       jsonb not null default '{}'::jsonb,
  created_at   timestamptz not null default now()
);
create index if not exists agent_events_tenant_idx on agent_events (tenant_id, created_at desc);
create index if not exists agent_events_date_idx   on agent_events (tenant_id, event_date);

-- Who did it (People slice 4, src/app/actor.py). NULL = BLAM8 itself, which is the
-- honest majority of this feed: drafting, the nightly jobs, a webhook. Never
-- guessed, and never back-filled — nobody knows who did the rows written before
-- this existed. actor_name is denormalised AS IT WAS, like staff_notes, so the
-- record still reads after that person has left and their membership is gone.
alter table agent_events add column if not exists actor_user_id uuid;
alter table agent_events add column if not exists actor_name text;
create index if not exists agent_events_actor_idx
  on agent_events (tenant_id, actor_user_id, created_at desc);

alter table agent_events enable row level security;
drop policy if exists "tenant isolation - agent_events" on agent_events;
create policy "tenant isolation - agent_events" on agent_events
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

-- ─────────────────────────────────────────────────────────────────────────────
-- Scheduling intake (src/app/intake.py — the per-tenant "what a booking needs" schema)
-- Keeps email + scheduling AGNOSTIC: each business defines the fields a booking must
-- capture (name, address, pet, …) so the agent collects them and CANNOT place a hold
-- until every required field is present. intake_fields is an ordered list of
-- {label, required, hint}. intake_configured_at stamps when the business completed the
-- scheduling onboarding; until it is set, a freshly-unlocked scheduling tenant's office
-- diary is blocked (set up first). NULL on a brand-new unlock; backfilled for existing
-- live tenants below so their bookings are never interrupted.
alter table scheduling_config add column if not exists intake_fields jsonb not null default '[]'::jsonb;
alter table scheduling_config add column if not exists intake_configured_at timestamptz;

-- Service types (src/app/services.py): the per-tenant list of bookable services, each with a
-- duration so the agent books the right slot length per service (a cut, an MOT, a 90-min
-- massage) instead of one fixed slot_minutes. An ordered list of {name, minutes, price}
-- (price optional). EMPTY = the single fixed-slot diary, byte-identical to before. A service
-- of N minutes is ceil(N/15) blocks on the unified grid; capacity stays concurrent occupancy.
alter table scheduling_config add column if not exists service_types jsonb not null default '[]'::jsonb;

-- ── Inbound-business diary depth (docs/architecture/unified-slot-grid.md) ──────────────────
-- Six more capabilities, all config-not-code (jsonb on this row, no per-feature table). EVERY
-- one defaults EMPTY/off, so a tenant that configures none is byte-identical to today.
--   resource_types : typed capacity catalogue [{name,count,rate,rate_unit,skills}] — each a
--                    separate concurrency pool (kennel sizes, room types, hire stock, bays,
--                    stylists). EMPTY = one fungible pool of capacity_per_window (today).
--   stay_pricing   : {rate,rate_unit,deposit,min_nights} for stays (which had no pricing).
--   day_hours      : per-weekday {mon:{open,close},…} overrides of the single opening span;
--                    plus checkin_after / checkout_before time rules for stays.
--   party_size     : {enabled,max_people} — a booking is FOR N people (covers/guests), counted
--                    against a people capacity (weighted concurrency).
--   recurring      : {enabled,max_occurrences,frequencies[]} — standing weekly/term bookings.
--   delivery       : {offered,fee,radius_miles} — a stay/hire that is delivered+collected.
-- buffer_minutes (added earlier) is generalised to "gap between bookings" and now applies to
-- services + stays (turnaround/cleaning), not only the legacy windows path.
alter table scheduling_config add column if not exists resource_types jsonb not null default '[]'::jsonb;
alter table scheduling_config add column if not exists stay_pricing   jsonb not null default '{}'::jsonb;
alter table scheduling_config add column if not exists day_hours      jsonb not null default '{}'::jsonb;
alter table scheduling_config add column if not exists party_size     jsonb not null default '{}'::jsonb;
alter table scheduling_config add column if not exists recurring      jsonb not null default '{}'::jsonb;
alter table scheduling_config add column if not exists delivery       jsonb not null default '{}'::jsonb;

-- Grandfather every tenant whose scheduling module is ALREADY active: mark intake
-- configured (no extra required fields = today's behaviour) so this never blocks a live
-- tenant. A scheduling_config row is created if they were running on defaults. Only
-- future, fresh unlocks get the blocking onboarding.
insert into scheduling_config (tenant_id, intake_configured_at)
  select s.tenant_id, now()
    from subscriptions s
   where s.module = 'scheduling' and s.status = 'active'
  on conflict (tenant_id) do update
    set intake_configured_at = coalesce(scheduling_config.intake_configured_at, now());

-- ─────────────────────────────────────────────────────────────────────────────
-- Phase 2B — proactive outbound (day-before booking reminders; src/app/jobs/send_reminders.py)
-- The reverse hand-off (docs/modules/module-agent-handoff.md D10): the agent starts the
-- conversation. email_log.kind tags a row's direction so the approval send picks the
-- right transport — 'inbound' (default: a reply, threaded via the provider's /reply) vs
-- 'reminder' (a fresh outbound, sent with a clean subject via send_mail). Reminders
-- still ride the normal approval queue (status='pending') unless the tenant is full-auto.
-- bookings.reminder_queued_at dedupes — at most one reminder per booking.
alter table email_log add column if not exists kind text not null default 'inbound';
alter table bookings  add column if not exists reminder_queued_at timestamptz;

-- ──────────────────────────────────────────────────────────────
-- Field Service (Trades) module (docs/modules/module-field-service.md)
-- ──────────────────────────────────────────────────────────────
-- The 2nd vertical's industry module (key 'fsm') — the trades twin of the fleet/
-- TMS spine. Engineers are the unit of capacity the way vehicles are for logistics:
-- a job's DURATION IN MINUTES fills an engineer-day the way pallet SPACES fill a
-- vehicle-day, and a SKILL requirement gates who can take a job the way a branch
-- gates a load. Everything else — atomic holds, proposed→confirmed lifecycle, the
-- diary, booking refs — is the scheduling machinery reused. A subscriptions row
-- module='fsm' is the whole rollout; when active it SUPERSEDES the base office
-- diary (the base agent hands ALL booking to the field-service specialist).
-- All tenant-isolated (RLS), like every table.

-- Engineers — the people who do the jobs (the fleet's `vehicles` sibling). skills is
-- a free-text set ({gas,plumbing,electrical}); a job's required_skill must be one of
-- them. daily_minutes is the bookable working day (480 = 8h). Inactive: kept for
-- history, takes no new holds (mirrors vehicles.active).
create table if not exists engineers (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id) on delete cascade,
  branch_id     uuid references branches(id) on delete set null,
  name          text not null,
  skills        text[] not null default '{}',
  daily_minutes int  not null default 480,
  active        boolean not null default true,
  created_at    timestamptz not null default now()
);
create index if not exists engineers_tenant_idx on engineers (tenant_id);

-- Job types — the menu of work, each with a default duration + the skill it needs
-- (the fleet's `pallet_types` sibling: the executor sizes a job from its type the
-- way it sizes a load from pallet factors). emergency flags a type that is always a
-- priority callout. default_price (nullable) seeds Phase-2 quoting. An unknown job
-- type in an email makes the specialist ask which service (it never guesses a size).
create table if not exists job_types (
  id              uuid primary key default gen_random_uuid(),
  tenant_id       uuid not null references tenants(id) on delete cascade,
  name            text not null,
  default_minutes int  not null default 60,
  required_skill  text,
  default_price   numeric(10,2),
  emergency       boolean not null default false,
  created_at      timestamptz not null default now(),
  unique (tenant_id, name)
);

-- A field-service booking is a normal bookings row carrying its engineer, the job's
-- size (minutes), the job type, the skill it needed and its priority. engineer_id
-- null = a non-FSM row, or an FSM job whose engineer was deleted (kept for a human to
-- re-assign). duration_minutes null = a non-FSM row. service_due_date seeds Phase-3
-- recurring service reminders (annual Gas Safe / boiler service).
alter table bookings add column if not exists engineer_id uuid
  references engineers(id) on delete set null;
alter table bookings add column if not exists duration_minutes int;
alter table bookings add column if not exists job_type         text;
alter table bookings add column if not exists required_skill   text;
alter table bookings add column if not exists priority         text not null default 'standard';
alter table bookings add column if not exists service_due_date date;

alter table engineers enable row level security;
alter table job_types enable row level security;
drop policy if exists "tenant isolation - engineers" on engineers;
create policy "tenant isolation - engineers" on engineers
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));
drop policy if exists "tenant isolation - job_types" on job_types;
create policy "tenant isolation - job_types" on job_types
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

-- The ONE atomic field-service capacity decision (the engineer sibling of
-- hold_fleet_booking): same advisory xact lock per (tenant, date), so engineer
-- holds and any other holds for that day serialise. Best-fit (mirrors best_fit /
-- hold_fleet_booking): among ACTIVE engineers in the branch (null = whole team) who
-- HAVE the required skill (null skill = anyone) and have enough free minutes that
-- day, pick the one with the SMALLEST remaining minutes that still fits — ties by
-- smallest day, then id — so engineers with a big free gap stay open for big jobs.
-- Free minutes = daily_minutes − Σ(active jobs' duration on that date). Returns the
-- inserted row; an EMPTY result means no single engineer can fit the job that day
-- (one engineer per job — splitting is out of scope). Date validation (notice/
-- horizon/working days/blackouts) happens in src/app/scheduling.py BEFORE this call.
create or replace function hold_engineer_booking (
  p_tenant_id      uuid,
  p_booking_date   date,
  p_summary        text,
  p_duration_min   int,
  p_required_skill text default null,
  p_job_type       text default null,
  p_priority       text default 'standard',
  p_branch_id      uuid default null,
  p_details        jsonb default null,
  p_contact        text default null,
  p_source         text default 'email',
  p_thread_id      text default null,
  p_status         text default 'proposed'
)
returns setof bookings
language plpgsql volatile
set search_path = public
as $$
declare
  v_engineer uuid;
begin
  if p_status not in ('proposed', 'confirmed') then
    raise exception 'invalid hold status: %', p_status;
  end if;
  if p_duration_min is null or p_duration_min <= 0 then
    raise exception 'invalid duration: %', p_duration_min;
  end if;
  if p_priority not in ('standard', 'emergency') then
    raise exception 'invalid priority: %', p_priority;
  end if;

  -- One writer per (tenant, date) — same lock key as the other hold RPCs.
  perform pg_advisory_xact_lock(
    hashtextextended(p_tenant_id::text || ':' || p_booking_date::text, 0)
  );

  select e.id into v_engineer
  from engineers e
  left join lateral (
    select coalesce(sum(b.duration_minutes), 0) as used_min
    from bookings b
    where b.tenant_id = p_tenant_id
      and b.engineer_id = e.id
      and b.booking_date = p_booking_date
      and b.status in ('proposed', 'confirmed')
  ) load on true
  where e.tenant_id = p_tenant_id
    and e.active
    and (p_branch_id is null or e.branch_id = p_branch_id)
    -- skill match is case-insensitive, to agree with app/scheduling.engineer_has_skill
    -- (so check_engineer_availability and book_job never disagree on a fit)
    and (p_required_skill is null
         or lower(p_required_skill) = any (array(select lower(x) from unnest(e.skills) as x)))
    and e.daily_minutes - load.used_min >= p_duration_min
  order by e.daily_minutes - load.used_min asc, e.daily_minutes asc, e.id asc
  limit 1;

  if v_engineer is null then
    return;   -- no single engineer fits the job that day — empty result set
  end if;

  return query
    insert into bookings (tenant_id, contact, summary, details, booking_date, "window",
                          status, source, thread_id, engineer_id, duration_minutes,
                          job_type, required_skill, priority, booking_ref)
    values (p_tenant_id, p_contact, p_summary, p_details, p_booking_date, 'day',
            p_status, p_source, p_thread_id, v_engineer, p_duration_min,
            p_job_type, p_required_skill, p_priority, next_booking_ref(p_tenant_id))
    returning *;
end $$;

-- ──────────────────────────────────────────────────────────────
-- Field Service pricing (docs/modules/module-field-service.md D3 — Phase 2)
-- ──────────────────────────────────────────────────────────────
-- How a job is priced for trades — the engineer sibling of rate_cards. One default
-- per tenant, seeded on fsm activation, editable in the UI. A quote = callout +
-- labour (estimated hours × hourly_rate) OR a job type's fixed default_price
-- (job_types.default_price, added in Phase 1), floored at min_charge, + VAT. The
-- quote is stamped on bookings.quoted_amount (added by the TMS baseline) so the same
-- value the customer was told flows through to invoicing.
create table if not exists fsm_rate_cards (
  id             uuid primary key default gen_random_uuid(),
  tenant_id      uuid not null references tenants(id) on delete cascade,
  name           text not null default 'Standard',
  callout_charge numeric(10,2) not null default 0,
  hourly_rate    numeric(10,2) not null default 0,
  min_charge     numeric(10,2) not null default 0,
  vat_pct        numeric(5,2)  not null default 20,
  currency       text not null default 'GBP',
  active         boolean not null default true,
  created_at     timestamptz not null default now()
);
create index if not exists fsm_rate_cards_tenant_idx on fsm_rate_cards (tenant_id);

alter table fsm_rate_cards enable row level security;
drop policy if exists "tenant isolation - fsm_rate_cards" on fsm_rate_cards;
create policy "tenant isolation - fsm_rate_cards" on fsm_rate_cards
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

-- ──────────────────────────────────────────────────────────────
-- Field Service recurring service reminders (docs/modules/module-field-service.md D5 — Phase 3)
-- ──────────────────────────────────────────────────────────────
-- The trades analogue of the day-before reminder, but FORWARD-dated + recurring: a job
-- type can recur (job_types.service_months — annual boiler service / Gas Safe = 12), and
-- completing a job of that type stamps bookings.service_due_date = booking_date + that
-- interval. The daily reminder job (src/app/jobs/send_service_reminders.py) then finds jobs
-- whose service is due soon and not yet chased, the specialist frames the rebook facts,
-- the base composes in the tenant voice (the same reverse hand-off as TMS). dedupe via
-- bookings.service_reminder_queued_at (separate from the day-before reminder_queued_at).
alter table job_types add column if not exists service_months int;
alter table bookings  add column if not exists service_reminder_queued_at timestamptz;

-- ──────────────────────────────────────────────────────────────
-- Practice OS (docs/os/practice.md §4) — recall cadence + no-show risk + medical notes
-- ──────────────────────────────────────────────────────────────
-- The appointment IS the bookings row. Practice OS is additive: recall_* is the shape of
-- the FSM service_* set (completing an appointment stamps recall_due_date = booking_date +
-- service.recall_months, chased by src/app/jobs/send_recall.py), no_show_* caches the scorer's
-- output, reminder_stages_sent (jsonb) dedupes the 48/24/2h cycle per stage, and
-- practitioner denormalises the resource pool for the board. Per-service recall_months
-- rides inside the existing service_types jsonb; practitioners + reminder_cycle are
-- top-level scheduling_config keys and so DO need their own jsonb columns
-- (src/app/practice_config.py owns both shapes).
alter table scheduling_config add column if not exists practitioners  jsonb not null default '[]'::jsonb;
alter table scheduling_config add column if not exists reminder_cycle jsonb not null default '{}'::jsonb;
alter table bookings add column if not exists recall_due_date           date;
alter table bookings add column if not exists recall_interval_months    integer;
alter table bookings add column if not exists recall_reminder_queued_at timestamptz;
alter table bookings add column if not exists recall_of_booking_id      uuid references bookings(id) on delete set null;
alter table bookings add column if not exists practitioner              text;
alter table bookings add column if not exists no_show_score             integer;
alter table bookings add column if not exists no_show_band              text;
alter table bookings add column if not exists no_show_reason            text;
alter table bookings add column if not exists no_show_scored_at         timestamptz;
alter table bookings add column if not exists extra_reminder_queued_at  timestamptz;
alter table bookings add column if not exists reminder_stages_sent      jsonb not null default '{}'::jsonb;

create index if not exists idx_bookings_recall_due
  on bookings (tenant_id, recall_due_date)
  where recall_due_date is not null and recall_reminder_queued_at is null;
create index if not exists idx_bookings_noshow_scan
  on bookings (tenant_id, booking_date, status);

-- Encrypted medical notes (UK GDPR Art. 9 special-category data): the ONE new Practice
-- table, justified solely by the encryption requirement. Ciphertext via app/crypto (the
-- same envelope mailbox OAuth tokens use). Decrypted only in a tenant-user context.
create table if not exists practice_medical_notes (
  id              uuid primary key default gen_random_uuid(),
  tenant_id       uuid not null references tenants(id) on delete cascade,
  contact_id      uuid not null references crm_contacts(id) on delete cascade,
  note_ciphertext bytea not null,
  note_kind       text not null default 'note',   -- note | allergy | medication | condition
  source          text,                    -- manual | extracted
  source_email_id text,                    -- email_log id when auto-extracted
  created_by      text,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);
create index if not exists idx_practice_med_notes_contact
  on practice_medical_notes (tenant_id, contact_id);

alter table practice_medical_notes enable row level security;
drop policy if exists "tenant isolation - practice_medical_notes" on practice_medical_notes;
create policy "tenant isolation - practice_medical_notes" on practice_medical_notes
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

-- ──────────────────────────────────────────────────────────────
-- Invoicing module — modular invoice settings (docs/modules/module-invoicing.md)
-- ──────────────────────────────────────────────────────────────
-- Invoicing is one SHARED engine (invoices/invoice_lines above) across every vertical;
-- each vertical only contributes a line-builder (how a completed job is priced). This
-- per-tenant config makes the invoice itself MODULAR — the business toggles which
-- sections appear (VAT, a business header, payment terms, a footer) and sets the VAT
-- rate, so the same engine renders a tailored invoice. `extras` jsonb future-proofs
-- per-vertical toggleable sections. No row = the defaults in src/app/invoice_config.py
-- (VAT shown at 20% — today's behaviour, so live tenants are unchanged).
create table if not exists invoice_settings (
  tenant_id             uuid primary key references tenants(id) on delete cascade,
  show_vat              boolean not null default true,
  vat_rate              numeric(5,2) not null default 20,
  show_business_details boolean not null default true,
  business_name         text,
  business_address      text,
  business_vat_no       text,
  business_contact      text,
  show_payment_terms    boolean not null default true,
  payment_terms         text not null default 'Payment due within 30 days of the invoice date.',
  footer_note           text,
  extras                jsonb not null default '{}'::jsonb,
  updated_at            timestamptz not null default now()
);
alter table invoice_settings enable row level security;
drop policy if exists "tenant isolation - invoice_settings" on invoice_settings;
create policy "tenant isolation - invoice_settings" on invoice_settings
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

-- When an invoice was issued (emailed) / settled — the Move-2 send flow stamps these.
alter table invoices add column if not exists sent_at timestamptz;
alter table invoices add column if not exists paid_at timestamptz;

-- ──────────────────────────────────────────────────────────────
-- Thread state + outcome (inbox-at-a-glance)
-- ──────────────────────────────────────────────────────────────
-- Every time the agent processes an inbound it self-reports, alongside the draft,
-- WHERE the conversation stands and WHAT it's about — so the inbox can show the
-- situation (what the customer wants + what the agent did) instead of a raw email
-- header + snippet. Especially valuable for email-only tenants (no diary module),
-- where automation otherwise runs silently with no surface for the outcome.
--   thread_state    — coarse triage bucket (see src/app/agent.py THREAD_STATES):
--                     'needs_attention' | 'in_progress' | 'resolved' | 'booked'.
--   request_summary — one short line: what the customer wants / their situation.
--   action_summary  — one short line: what the agent did about it in this reply.
-- All nullable; legacy rows (drafted before this) carry NULL and fall back to the
-- old subject/preview rendering. The latest row per contact wins in the inbox,
-- exactly like subject/status do.
alter table email_log add column if not exists thread_state    text;
alter table email_log add column if not exists request_summary text;
alter table email_log add column if not exists action_summary  text;

-- ──────────────────────────────────────────────────────────────
-- Unified slot-grid (docs/architecture/unified-slot-grid.md) — Phase 2 columns
-- ──────────────────────────────────────────────────────────────
-- One calendar primitive for every industry: a diary is a flat grid of 15-minute
-- blocks (96 per day). A booking occupies `length_blocks` consecutive blocks from
-- `start_block`; the slot size a tenant books in is config (`block_minutes`, a 15-min
-- multiple), never schema. These columns are ADDITIVE and back-filled from the existing
-- `window`/`slot_minutes` via app/slotgrid.window_to_blocks (scripts_backfill_slot_blocks.py)
-- — no existing row is rewritten destructively. The live read path keeps using `window`
-- until Phase 2b swaps it onto slotgrid.office_availability (parity proven in
-- tests/test_slotgrid_parity.py). NULL grid columns render exactly as today.
alter table scheduling_config add column if not exists block_minutes int not null default 15;
alter table bookings add column if not exists start_block   int;   -- 0..95, NULL = legacy/unmapped
alter table bookings add column if not exists length_blocks int;   -- 1..96, NULL = legacy/unmapped

-- Inbound-business depth (docs/architecture/unified-slot-grid.md). All nullable/additive — a booking that
-- uses none of these reads exactly as before.
--   resource_type   : which scheduling_config.resource_types pool the booking sits in (its
--                     concurrency is counted within that type only). NULL = the implicit pool.
--   quantity        : units consumed / party size (hire 5, 2 pets, table of 6). NULL = 1.
--   deposit_amount  : deposit taken/quoted to hold (stays, tattoo, hire…).
--   recurrence_id   : groups the occurrences of a standing/recurring series (same uuid).
alter table bookings add column if not exists resource_type  text;
alter table bookings add column if not exists quantity       int;
alter table bookings add column if not exists deposit_amount numeric(10,2);
alter table bookings add column if not exists recurrence_id  uuid;


-- Mailbox add-on billing — grandfathered flag
-- Each connected mailbox beyond a plan's included allowance bills as the
-- £39/mo extra-mailbox add-on (app/billing.sync_mailbox_addon). Mailboxes that
-- existed when this shipped are flagged grandfathered=true by the one-shot
-- backfill (scripts/db/migrations/apply_mailbox_addon_migration.py) so switching add-on
-- billing on never retro-charges an existing customer; every NEW connection
-- defaults false and is billable once it passes the allowance.
alter table email_connections add column if not exists grandfathered boolean not null default false;


-- Admin comp access — free/comped tenants (operator console)
-- Grants app access independent of Stripe and the trial, honoured by
-- trial.evaluate(). comp_access: null (off) | 'indefinite' | 'until_date';
-- comp_until: expiry for 'until_date'. The Stripe webhook NEVER writes these, so
-- a subscription event can't revoke a comp. For free-for-feedback users etc.
alter table tenants add column if not exists comp_access text;
alter table tenants add column if not exists comp_until  timestamptz;


-- Booking-request details (email-only gather flow — the pinned booking card)
-- ──────────────────────────────────────────────────────────────
-- The agent self-reports, alongside each draft, the booking details it has gathered
-- SO FAR across the whole thread — a cumulative list of {label, value} keyed by the
-- tenant's booking-requirements labels. The conversation view shows these in a single
-- pinned booking card (not one card per message), which updates in place as the
-- exchange goes on, so the operator sees the whole request at a glance and can confirm
-- or change the ready draft. NULL / [] on legacy rows and every non-booking reply.
alter table email_log add column if not exists booking_details jsonb;


-- Feature-request kind (feature | bug) — chatbot reports land in the same pipeline
-- ──────────────────────────────────────────────────────────────
-- The concierge (help mode) can now draft and file BOTH roadmap ideas and issue
-- reports, and the /app/requests form offers the same choice. `kind` separates the
-- roadmap signal from the bug queue on the tenant page and /admin/requests without
-- a second table; every legacy row is a feature idea.
alter table feature_requests add column if not exists kind text not null default 'feature';


-- Feature-request seen stamp (operator notification badge)
-- ──────────────────────────────────────────────────────────────
-- The operator nav shows an unseen-count badge on Requests; opening the page
-- stamps everything seen. NULL = the operator hasn't viewed it yet. Backfilled
-- to now() for rows that predate the badge so it starts from a clean zero.
alter table feature_requests add column if not exists seen_at timestamptz;
update feature_requests set seen_at = now() where seen_at is null and created_at < '2026-07-02';


-- Contact identity (the inbox shows people, not addresses)
-- ──────────────────────────────────────────────────────────────
-- Three layers per email_log row: `from_name` is the From header's display name,
-- captured free at ingest; `contact_name` / `contact_company` are what the customer
-- has STATED in the thread, self-reported by the agent alongside its other
-- per-draft fields (thread_state etc.). List surfaces show the best available
-- (stated name beats header name beats bare address); the thread view keeps the
-- address. NULL on legacy rows — they simply keep showing the address.
alter table email_log add column if not exists from_name       text;
alter table email_log add column if not exists contact_name    text;
alter table email_log add column if not exists contact_company text;

-- Google sign-in invites (operator console — src/app/admin.py /admin/invites).
-- Single-use personal codes for the Google invite gate (src/app/slots.py): generated
-- by the operator, redeemed once at /auth/google-invite. Reusable "channel"
-- codes live in GOOGLE_INVITE_CODES env instead. Service role only (RLS on, no
-- policies — GoTrue-authed clients can never read codes).
create table if not exists google_invites (
  id          uuid primary key default gen_random_uuid(),
  code        text not null unique,               -- stored uppercase
  email       text,                               -- who it's for (operator's records)
  note        text,                               -- channel / context
  created_at  timestamptz not null default now(),
  used_at     timestamptz,                        -- stamped when redeemed at the gate
  revoked_at  timestamptz
);
alter table google_invites enable row level security;

-- Google access requests ("no invite code? request access" on the login page).
-- Vetting queue for the invite gate: the operator reviews at /admin/invites and
-- either mints an invite (invited_at) or dismisses. Service role only.
create table if not exists google_access_requests (
  id           uuid primary key default gen_random_uuid(),
  email        text not null,
  message      text,                 -- "tell us about your business" (optional)
  created_at   timestamptz not null default now(),
  invited_at   timestamptz,          -- operator minted an invite for them
  dismissed_at timestamptz
);
alter table google_access_requests enable row level security;

-- AI disclosure (EU AI Act Article 50 — docs/security/eu-ai-act-compliance.md)
-- Auto-sent replies disclose they are AI-written; ON by default. Draft-first
-- sends are human-approved communications and never carry the forced footer.
alter table tenant_config add column if not exists ai_disclosure boolean not null default true;


-- CRM module — "the CRM that fills itself in" (docs/architecture/CRM_design.md)
-- ──────────────────────────────────────────────────────────────
-- Six fixed base entities; custom fields ride on top later (custom_data JSONB is
-- the phase-2 seam, present but unused). Everything auto-populates from email_log
-- (src/app/crm.py ingest + backfill) — the tenant never types. Company is the atom,
-- deliberately NOT Contact: several people at one firm roll up by email domain
-- (freemail domains never become companies). Ships in Core: crm_on is
-- unconditional in the dashboard nav, no subscriptions row needed (like 'email').

create table if not exists crm_companies (
  id               uuid primary key default gen_random_uuid(),
  tenant_id        uuid not null references tenants(id) on delete cascade,
  name             text,
  domain           text,                              -- lowercased email domain the firm was inferred from
  first_seen_at    timestamptz not null default now(),
  last_activity_at timestamptz,
  custom_data      jsonb not null default '{}'::jsonb, -- reserved for phase 2
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);
create index if not exists crm_companies_tenant_idx on crm_companies (tenant_id);
create unique index if not exists crm_companies_tenant_domain_uniq
  on crm_companies (tenant_id, domain) where domain is not null;

create table if not exists crm_contacts (
  id               uuid primary key default gen_random_uuid(),
  tenant_id        uuid not null references tenants(id) on delete cascade,
  company_id       uuid references crm_companies(id) on delete set null,
  email            text not null,                     -- lowercased; the identity key
  name             text,
  first_seen_at    timestamptz not null default now(),
  last_activity_at timestamptz,
  custom_data      jsonb not null default '{}'::jsonb,
  -- Sentiment (self-filled from every inbound email — crm_intel.apply_sentiment):
  sentiment            text,        -- 'positive'|'neutral'|'concerned'|'frustrated'|'angry'
  sentiment_trend      text,        -- 'improving'|'steady'|'worsening' vs the previous read
  sentiment_reason     text,        -- short trigger phrase ("third chase about the invoice")
  sentiment_updated_at timestamptz,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  unique (tenant_id, email)
);
create index if not exists crm_contacts_tenant_idx on crm_contacts (tenant_id);
create index if not exists crm_contacts_company_idx on crm_contacts (company_id);

-- Stages before deals (deals.stage_id FK). Per-tenant, configurable; defaults
-- (Prospect → Qualifying → Negotiating → Won / Lost / Dormant) are seeded lazily
-- by src/app/crm.py ensure_default_stages the first time a tenant's CRM is touched.
create table if not exists crm_stages (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id) on delete cascade,
  name          text not null,
  display_order integer not null default 0,
  stage_type    text not null default 'active',       -- 'active' | 'won' | 'lost' | 'dormant'
  created_at    timestamptz not null default now()
);
create index if not exists crm_stages_tenant_idx on crm_stages (tenant_id);

create table if not exists crm_deals (
  id                 uuid primary key default gen_random_uuid(),
  tenant_id          uuid not null references tenants(id) on delete cascade,
  company_id         uuid references crm_companies(id) on delete set null,
  primary_contact_id uuid references crm_contacts(id) on delete set null,
  title              text,
  stage_id           uuid references crm_stages(id) on delete set null,
  value_estimate     numeric,
  currency           text not null default 'GBP',
  opened_at          timestamptz not null default now(),
  closed_at          timestamptz,
  status             text not null default 'open',    -- 'open' | 'won' | 'lost' | 'dormant'
  custom_data        jsonb not null default '{}'::jsonb,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);
create index if not exists crm_deals_tenant_idx on crm_deals (tenant_id);
create index if not exists crm_deals_stage_idx on crm_deals (stage_id);

create table if not exists crm_activities (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id) on delete cascade,
  contact_id    uuid references crm_contacts(id) on delete cascade,
  company_id    uuid references crm_companies(id) on delete set null,
  deal_id       uuid references crm_deals(id) on delete set null,
  activity_type text not null,                        -- 'email' | 'calendar_event' | 'voice_call'
  source_id     text,                                 -- email_log id, booking id, …
  direction     text,                                 -- 'inbound' | 'outbound' (email only)
  subject       text,
  summary       text,
  sentiment     text,                               -- inbound emails: the mood read (emotional arc)
  occurred_at   timestamptz not null,
  created_at    timestamptz not null default now()
);
create index if not exists crm_activities_contact_idx on crm_activities (tenant_id, contact_id, occurred_at desc);
create index if not exists crm_activities_deal_idx on crm_activities (tenant_id, deal_id);
-- Idempotency spine for ingest + backfill: one activity per (source row,
-- direction) — re-running the prod backfill or a duplicate webhook delivery can
-- never double-write the feed. coalesce() because direction is null for
-- non-email activity.
create unique index if not exists crm_activities_source_uniq
  on crm_activities (tenant_id, activity_type, source_id, coalesce(direction, ''))
  where source_id is not null;

-- Documents on the CRM record (Kris 2026-09-04, src/app/documents.py): one row per
-- file kept — an inbound email's attachment ('attachment') or an operator upload
-- ('upload') — on the customer's record (contact_email / contact_id), optionally
-- pinned to the thread row, a booking, a dependant, and labelled as the checklist
-- document it satisfies. The bytes live in the private `documents` Storage bucket;
-- url_or_path is the object key.
create table if not exists crm_artifacts (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id) on delete cascade,
  deal_id       uuid references crm_deals(id) on delete set null,
  contact_id    uuid references crm_contacts(id) on delete set null,
  activity_id   uuid references crm_activities(id) on delete set null,
  artifact_type text,                                 -- 'attachment' | 'upload' | 'referenced_doc'
  filename      text,
  content_type  text,
  url_or_path   text,
  contact_email text,
  email_log_id  uuid references email_log(id) on delete set null,
  booking_id    uuid references bookings(id) on delete set null,
  instance_id   uuid references requirement_instances(id) on delete set null,
  size_bytes    int not null default 0,
  label         text,
  created_at    timestamptz not null default now()
);
create index if not exists crm_artifacts_tenant_idx on crm_artifacts (tenant_id);
create index if not exists crm_artifacts_contact_email_idx on crm_artifacts (tenant_id, contact_email, created_at desc);
create index if not exists crm_artifacts_booking_idx on crm_artifacts (tenant_id, booking_id);

alter table crm_companies  enable row level security;
alter table crm_contacts   enable row level security;
alter table crm_stages     enable row level security;
alter table crm_deals      enable row level security;
alter table crm_activities enable row level security;
alter table crm_artifacts  enable row level security;

drop policy if exists "tenant isolation - crm_companies" on crm_companies;
create policy "tenant isolation - crm_companies" on crm_companies
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));
drop policy if exists "tenant isolation - crm_contacts" on crm_contacts;
create policy "tenant isolation - crm_contacts" on crm_contacts
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));
drop policy if exists "tenant isolation - crm_stages" on crm_stages;
create policy "tenant isolation - crm_stages" on crm_stages
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));
drop policy if exists "tenant isolation - crm_deals" on crm_deals;
create policy "tenant isolation - crm_deals" on crm_deals
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));
drop policy if exists "tenant isolation - crm_activities" on crm_activities;
create policy "tenant isolation - crm_activities" on crm_activities
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));
drop policy if exists "tenant isolation - crm_artifacts" on crm_artifacts;
create policy "tenant isolation - crm_artifacts" on crm_artifacts
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));


-- Interview-first onboarding: what the tenant wants the agent to take off
-- their plate, captured in their own words at signup (src/app/onboarding.py
-- interview step). The seed for the per-tenant setup plan (phase B).
alter table tenant_config add column if not exists goals text;


-- CRM custom fields (phase 2 — docs/architecture/CRM_design.md §4/§9/§10/§11)
-- ──────────────────────────────────────────────────────────────
-- The tenant's own schema, riding on the entities' custom_data JSONB: each
-- definition says what to track and — the differentiator — an extraction_hint
-- telling the LLM what to look for in every email. Values land in custom_data
-- when confidence is high; middling confidence queues a suggestion row for
-- one-click confirm; low is dropped. Max 10 fields per entity (app-enforced,
-- src/app/crm_fields.py) so onboarding anxiety can't bloat the schema.

create table if not exists crm_field_definitions (
  id              uuid primary key default gen_random_uuid(),
  tenant_id       uuid not null references tenants(id) on delete cascade,
  entity          text not null,                      -- 'company' | 'contact' | 'deal'
  key             text not null,                      -- slug of label; the custom_data key
  label           text not null,
  field_type      text not null default 'text',       -- 'text' | 'number' | 'dropdown' | 'date' | 'bool'
  options         jsonb not null default '[]'::jsonb, -- dropdown choices
  required        boolean not null default false,
  extraction_hint text,                               -- natural language: what to look for in emails
  probe_behaviour text not null default 'passive',    -- Phase 3.5: 'passive' | 'probe_once' | 'probe_persistent'
  display_order   integer not null default 0,
  created_at      timestamptz not null default now(),
  unique (tenant_id, entity, key)
);
create index if not exists crm_field_definitions_tenant_idx
  on crm_field_definitions (tenant_id, entity);
-- Progress ladders (src/app/ladders.py, 2026-09-05): a contact-level ladder is a
-- dropdown definition carrying {notify, gates}; null for every other field.
alter table crm_field_definitions add column if not exists ladder jsonb;
-- Certificates on contacts (slice 4, 2026-09-05): a date field that EXPIRES, recalled N days ahead.
alter table crm_field_definitions add column if not exists durability text not null default 'once';
alter table crm_field_definitions add column if not exists recall_days integer not null default 30;
-- Run #8 (2026-09-05): how long an expiring field runs from the date on record, in months (0 = the date IS the expiry).
alter table crm_field_definitions add column if not exists valid_months integer not null default 0;

-- "Staff can see" (People slice 3 — src/app/crm_visibility.py). Kris, when the
-- roles were settled: default off "for anything onboarding marked as access
-- information (codes, keys), on for name, phone, the dependants' names and
-- notes." A definition carries no such marker — the tenant types a label and a
-- hint in their own words — so the DEFAULT is read from those words, and
-- staff_can_see_set records that a human has decided, after which nothing
-- re-derives it. A staff member given "See private details" reads a shut field
-- anyway; owners and managers are never narrowed at all.
alter table crm_field_definitions add column if not exists staff_can_see boolean not null default true;
alter table crm_field_definitions add column if not exists staff_can_see_set boolean not null default false;

-- Packages and the wallet (src/app/wallet.py, 2026-09-05). A package sold ahead of
-- use mints a wallet_credits row when its invoice is issued; bookings spend it
-- (wallet_spend, one per booking); the register (booking_attendance) records
-- the occurrence's outcome and overrides the automatic spend.
create table if not exists wallet_credits (
  id             uuid primary key default gen_random_uuid(),
  tenant_id      uuid not null references tenants(id) on delete cascade,
  contact_id     uuid not null,
  contact_email  text,
  package_key    text,
  package_name   text,
  unit           text not null default 'session',      -- 'session' | 'hour'
  quantity       numeric(10,2) not null default 0,
  used           numeric(10,2) not null default 0,
  price          numeric(12,2),
  expires_at     date,
  paid           boolean not null default false,
  invoice_id     uuid,
  instance_id    uuid,                                 -- "for <participant>" (optional)
  note           text,
  low_sent_at    timestamptz,
  expiry_sent_at timestamptz,
  created_at     timestamptz not null default now()
);
create index if not exists wallet_credits_contact_idx on wallet_credits (tenant_id, contact_id);
create index if not exists wallet_credits_invoice_idx on wallet_credits (tenant_id, invoice_id);
alter table wallet_credits enable row level security;
drop policy if exists "tenant isolation - wallet_credits" on wallet_credits;
create policy "tenant isolation - wallet_credits" on wallet_credits
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

create table if not exists wallet_spend (
  id         uuid primary key default gen_random_uuid(),
  tenant_id  uuid not null references tenants(id) on delete cascade,
  credit_id  uuid not null references wallet_credits(id) on delete cascade,
  booking_id uuid not null,
  amount     numeric(10,2) not null default 1,
  reason     text not null default 'attended',        -- attended | no_show | late_cancel | auto
  at         timestamptz not null default now(),
  unique (credit_id, booking_id)
);
create index if not exists wallet_spend_booking_idx on wallet_spend (tenant_id, booking_id);
alter table wallet_spend enable row level security;
drop policy if exists "tenant isolation - wallet_spend" on wallet_spend;
create policy "tenant isolation - wallet_spend" on wallet_spend
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

create table if not exists booking_attendance (
  booking_id uuid primary key,
  tenant_id  uuid not null references tenants(id) on delete cascade,
  status     text not null,                            -- attended | no_show | late_cancel | excused
  note       text,
  hours      numeric(6,2),
  logged_by  text not null default 'business',
  at         timestamptz not null default now()
);
create index if not exists booking_attendance_tenant_idx on booking_attendance (tenant_id);
alter table booking_attendance enable row level security;
drop policy if exists "tenant isolation - booking_attendance" on booking_attendance;
create policy "tenant isolation - booking_attendance" on booking_attendance
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

alter table invoice_lines add column if not exists package_key text;
alter table invoice_lines add column if not exists instance_id uuid;
-- Diary settings → Cancellations & credits: {spend, late_hours, no_show_spends, excused_refunds, hours}
alter table scheduling_config add column if not exists credits jsonb not null default '{}'::jsonb;

-- Courses, the waiting list and re-enrolment (src/app/courses.py, 2026-09-05). A
-- waiter sits on a CLASS ("slot|<service>|<weekday>|<HH:MM>") or a one-off date;
-- a freed place is offered to the first waiting for `offer_hours`, then moves on.
create table if not exists waitlist (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id) on delete cascade,
  contact_id    uuid,
  contact_email text not null,
  instance_id   uuid,                                  -- the participant (optional)
  participant   text,
  series_key    text not null,
  label         text,
  status        text not null default 'waiting',       -- waiting | offered | booked | lapsed | left
  note          text,
  source        text not null default 'email',         -- email | portal | manual
  booking_id    uuid,
  offered_at    timestamptz,
  expires_at    timestamptz,
  closed_at     timestamptz,
  created_at    timestamptz not null default now()
);
create index if not exists waitlist_key_idx on waitlist (tenant_id, series_key, status);
create index if not exists waitlist_contact_idx on waitlist (tenant_id, contact_email);
alter table waitlist enable row level security;
drop policy if exists "tenant isolation - waitlist" on waitlist;
create policy "tenant isolation - waitlist" on waitlist
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

-- One re-enrolment note per (series, customer).
create table if not exists course_notices (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id) on delete cascade,
  recurrence_id text not null,
  contact_email text not null,
  kind          text not null default 'reenrol',
  email_log_id  uuid,
  at            timestamptz not null default now(),
  unique (tenant_id, recurrence_id, contact_email, kind)
);
alter table course_notices enable row level security;
drop policy if exists "tenant isolation - course_notices" on course_notices;
create policy "tenant isolation - course_notices" on course_notices
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

-- Diary settings → Recurring → Courses: {courses, waitlist, waitlist_notify, offer_hours, reenrol_days, reenrol_notify}
alter table scheduling_config add column if not exists courses jsonb not null default '{}'::jsonb;

-- The customer-writable log (src/app/customer_log.py, slice 5, 2026-09-05): a
-- customer who IS the participant adds notes to their own bookings from the
-- portal (never a dependant's); the business adds its own lines. Append-only.
create table if not exists booking_notes (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id) on delete cascade,
  booking_id    uuid not null,
  booking_ref   text,
  contact_email text,
  author        text not null default 'customer',      -- customer | business
  body          text not null,
  created_at    timestamptz not null default now()
);
create index if not exists booking_notes_booking_idx on booking_notes (tenant_id, booking_id);
create index if not exists booking_notes_contact_idx on booking_notes (tenant_id, contact_email);
alter table booking_notes enable row level security;
drop policy if exists "tenant isolation - booking_notes" on booking_notes;
create policy "tenant isolation - booking_notes" on booking_notes
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));
-- Settings → Portal: "Customers can add notes to their own record" (default ON).
alter table tenant_config add column if not exists portal_customer_notes boolean not null default true;

-- Make-ups (src/app/makeups.py, slice 6, 2026-09-05): a one-off booking bound to a
-- missed occurrence (the register's "excused"), once per block, within N days.
create table if not exists makeups (
  id                uuid primary key default gen_random_uuid(),
  tenant_id         uuid not null references tenants(id) on delete cascade,
  contact_email     text,
  instance_id       uuid,
  missed_booking_id uuid not null,
  missed_ref        text,
  makeup_booking_id uuid not null,
  makeup_ref        text,
  term_key          text,                              -- the missed series' recurrence_id, or YYYY-Qn
  status            text not null default 'booked',
  created_at        timestamptz not null default now(),
  unique (tenant_id, missed_booking_id)
);
create index if not exists makeups_contact_idx on makeups (tenant_id, contact_email);
alter table makeups enable row level security;
drop policy if exists "tenant isolation - makeups" on makeups;
create policy "tenant isolation - makeups" on makeups
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));
-- Diary settings → Cancellations & credits → Make-ups: {on, per_term, notice_hours, within_days, no_show_ok, free}
alter table scheduling_config add column if not exists makeups jsonb not null default '{}'::jsonb;
-- The business's own words: {"booking": "lesson", "participant": "pupil"} — read by every
-- customer-facing rule the class-and-lesson programme added (nothing says "class" to a driving school).
alter table scheduling_config add column if not exists nouns jsonb not null default '{}'::jsonb;
-- Run #8 (2026-09-05): the Dependants settings page was answered — a kind, a ladder, or "nothing of the sort".
alter table scheduling_config add column if not exists dependants_configured_at timestamptz;
-- 6b: when the customer told the business (the notice rule); 5b: a withdrawn note.
alter table booking_attendance add column if not exists notified_at timestamptz;
alter table booking_notes add column if not exists deleted_at timestamptz;

-- The one-click-confirm queue: a middling-confidence extraction waits here
-- instead of writing itself into custom_data. Unique per (record, field, email)
-- so a re-processed email can never stack duplicate suggestions.
create table if not exists crm_field_suggestions (
  id           uuid primary key default gen_random_uuid(),
  tenant_id    uuid not null references tenants(id) on delete cascade,
  entity       text not null,                         -- 'company' | 'contact' | 'deal'
  record_id    uuid not null,                         -- the crm_contacts/companies/deals row
  field_key    text not null,
  value        text,                                  -- raw extracted value; typed on accept
  confidence   numeric,
  email_log_id uuid,                                  -- the email it came from (audit)
  status       text not null default 'pending',       -- 'pending' | 'accepted' | 'dismissed'
  created_at   timestamptz not null default now(),
  resolved_at  timestamptz
);
create index if not exists crm_field_suggestions_record_idx
  on crm_field_suggestions (tenant_id, record_id, status);
create unique index if not exists crm_field_suggestions_source_uniq
  on crm_field_suggestions (tenant_id, entity, record_id, field_key, email_log_id)
  where email_log_id is not null;

alter table crm_field_definitions enable row level security;
alter table crm_field_suggestions enable row level security;

drop policy if exists "tenant isolation - crm_field_definitions" on crm_field_definitions;
create policy "tenant isolation - crm_field_definitions" on crm_field_definitions
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));
drop policy if exists "tenant isolation - crm_field_suggestions" on crm_field_suggestions;
create policy "tenant isolation - crm_field_suggestions" on crm_field_suggestions
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));


-- CRM intelligence (phase 3 — docs/architecture/CRM_design.md §5)
-- ──────────────────────────────────────────────────────────────
-- Promise tracking: commitments the LLM reads out of the mail flow — what WE
-- owe them (from outgoing replies) and what THEY owe us (from inbound). The
-- killer view's "what you owe them / what they owe you" renders open rows;
-- one click marks them done. Unique per (email, direction, description) so a
-- re-processed email never stacks duplicates. Deals themselves reuse the
-- phase-1 crm_deals table — clustering only INSERTS there.

create table if not exists crm_promises (
  id                  uuid primary key default gen_random_uuid(),
  tenant_id           uuid not null references tenants(id) on delete cascade,
  contact_id          uuid references crm_contacts(id) on delete cascade,
  company_id          uuid references crm_companies(id) on delete set null,
  deal_id             uuid references crm_deals(id) on delete set null,
  direction           text not null,                     -- 'we_owe' | 'they_owe'
  description         text not null,
  due_hint            text,                              -- "by Friday", "end of month" — verbatim
  status              text not null default 'open',      -- 'open' | 'done' | 'dropped' | 'resolved'
  email_log_id        uuid,
  created_at          timestamptz not null default now(),
  resolved_at         timestamptz,
  -- Phase 3.5 §18.3: reinforcement tracking
  last_seen_at        timestamptz default now(),
  mention_count       integer not null default 1,
  -- Phase 3.5 §18.4: resolution provenance
  resolution_type     text,                              -- 'manual' | 'auto' | 'suggested' | 'confirmed'
  resolution_evidence text,
  -- Cleared-by link (2026-09-20): WHAT cleared it, so the green tick opens the
  -- thing itself — the email that fulfilled it, the booking, the invoice, the
  -- quote — rather than a phrase about it. A 'suggested' row carries the same
  -- three so Confirm keeps the link.
  resolved_by_kind    text,                              -- 'email' | 'booking' | 'invoice' | 'quote' | 'manual'
  resolved_by_id      text,                              -- the row id of that thing
  resolved_by_label   text                               -- 'BK-1017' / 'INV-1003' / 'their email' / 'your reply'
);
alter table crm_promises add column if not exists resolved_by_kind  text;
alter table crm_promises add column if not exists resolved_by_id    text;
alter table crm_promises add column if not exists resolved_by_label text;
create index if not exists crm_promises_contact_idx on crm_promises (tenant_id, contact_id, status);
create unique index if not exists crm_promises_source_uniq
  on crm_promises (tenant_id, email_log_id, direction, description)
  where email_log_id is not null;

alter table crm_promises enable row level security;
drop policy if exists "tenant isolation - crm_promises" on crm_promises;
create policy "tenant isolation - crm_promises" on crm_promises
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

-- Phase 3.5 §18.2: probe attempt tracking (high-requirement field probing)
create table if not exists crm_probe_log (
  id                  uuid primary key default gen_random_uuid(),
  tenant_id           uuid not null references tenants(id) on delete cascade,
  contact_id          uuid not null references crm_contacts(id) on delete cascade,
  field_definition_id uuid not null references crm_field_definitions(id) on delete cascade,
  attempt_number      integer not null default 1,
  draft_id            uuid,
  resolved            boolean not null default false,
  created_at          timestamptz not null default now()
);
create index if not exists crm_probe_log_contact_idx
  on crm_probe_log (tenant_id, contact_id, field_definition_id);

alter table crm_probe_log enable row level security;
drop policy if exists "tenant isolation - crm_probe_log" on crm_probe_log;
create policy "tenant isolation - crm_probe_log" on crm_probe_log
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

-- Phase 3.5 §18.1: name-based company lookup for freemail senders
create index if not exists idx_crm_companies_name_tenant
  on crm_companies (tenant_id, lower(name));

-- Phase 4a §19.2: CRM task management
create table if not exists crm_tasks (
  id           uuid primary key default gen_random_uuid(),
  tenant_id    uuid not null references tenants(id) on delete cascade,
  title        text not null,
  description  text,
  due_date     date,
  priority     text not null default 'normal',   -- low | normal | high | urgent
  status       text not null default 'open',     -- open | done | cancelled
  assignee_id  uuid,                             -- future: references tenant_users
  contact_id   uuid references crm_contacts(id) on delete set null,
  company_id   uuid references crm_companies(id) on delete set null,
  deal_id      uuid references crm_deals(id) on delete set null,
  promise_id   uuid references crm_promises(id) on delete set null,
  source       text not null default 'manual',   -- manual | auto_promise | auto_stage | auto_nudge
  created_at   timestamptz not null default now(),
  completed_at timestamptz,
  updated_at   timestamptz not null default now()
);
create index if not exists idx_crm_tasks_tenant_status
  on crm_tasks (tenant_id, status, due_date);

alter table crm_tasks enable row level security;
drop policy if exists "tenant isolation - crm_tasks" on crm_tasks;
create policy "tenant isolation - crm_tasks" on crm_tasks
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

-- Cold-lead nudge threshold (design §14: default 14, per-tenant configurable).
alter table tenant_config add column if not exists crm_quiet_days integer not null default 14;

-- Phase 4b §19.3: multi-step follow-up sequences.
-- Sequence → ordered steps → per-contact enrollments processed on a 15-min timer.
-- Extends the single-touch gone-quiet nudge into multi-step outreach; Core-tier.
-- tenant_id sits on all three (incl. steps) for uniform direct RLS.
create table if not exists crm_sequences (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id) on delete cascade,
  name          text not null,
  description   text,
  trigger_type  text not null default 'manual',   -- manual | deal_stage_enter | gone_quiet | new_contact | tag_added
  trigger_config jsonb not null default '{}',      -- e.g. {"stage_id": "...", "days_quiet": 14}
  status        text not null default 'active',    -- active | paused | archived
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

create table if not exists crm_sequence_steps (
  id               uuid primary key default gen_random_uuid(),
  tenant_id        uuid not null references tenants(id) on delete cascade,
  sequence_id      uuid not null references crm_sequences(id) on delete cascade,
  step_order       integer not null,
  delay_days       integer not null default 3,
  action_type      text not null default 'send_email',  -- send_email | update_stage | create_task | add_tag
  template_subject text,
  template_body    text,                                -- a BRIEF, not a literal email — the drafter rewrites it
  exit_on_reply    boolean not null default true,
  side_effects     jsonb not null default '[]',         -- e.g. [{"action": "move_stage", "stage_id": "..."}]
  created_at       timestamptz not null default now()
);
create index if not exists idx_crm_seq_steps_seq
  on crm_sequence_steps (tenant_id, sequence_id, step_order);

create table if not exists crm_sequence_enrollments (
  id             uuid primary key default gen_random_uuid(),
  tenant_id      uuid not null references tenants(id) on delete cascade,
  sequence_id    uuid not null references crm_sequences(id) on delete cascade,
  contact_id     uuid not null references crm_contacts(id) on delete cascade,
  deal_id        uuid references crm_deals(id) on delete set null,
  current_step   integer not null default 0,
  status         text not null default 'active',   -- active | completed | exited | paused
  next_action_at timestamptz,
  enrolled_at    timestamptz not null default now(),
  exited_at      timestamptz,
  exit_reason    text  -- replied | stage_changed | manual | completed | bounced | module_off
);
create index if not exists idx_crm_seq_enroll_due
  on crm_sequence_enrollments (tenant_id, status, next_action_at)
  where status = 'active';

-- Global per-contact opt-out from all sequences (design §19.3 guardrails).
alter table crm_contacts add column if not exists do_not_sequence boolean not null default false;

alter table crm_sequences enable row level security;
drop policy if exists "tenant isolation - crm_sequences" on crm_sequences;
create policy "tenant isolation - crm_sequences" on crm_sequences
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

alter table crm_sequence_steps enable row level security;
drop policy if exists "tenant isolation - crm_sequence_steps" on crm_sequence_steps;
create policy "tenant isolation - crm_sequence_steps" on crm_sequence_steps
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

alter table crm_sequence_enrollments enable row level security;
drop policy if exists "tenant isolation - crm_sequence_enrollments" on crm_sequence_enrollments;
create policy "tenant isolation - crm_sequence_enrollments" on crm_sequence_enrollments
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

-- ──────────────────────────────────────────────────────────────
-- Gmail push watch (live inbox — users.watch → Pub/Sub → /webhook/gmail)
-- ──────────────────────────────────────────────────────────────
-- When this mailbox's Gmail push watch lapses (~7 days after users.watch).
-- The renewal sweep (src/app/jobs/renew_subscriptions.py) re-arms any watch inside
-- its 24h window — and arms rows still NULL (pre-push mailboxes / failed arms).
-- NULL while GMAIL_PUBSUB_TOPIC is unset: push off, the poller carries Gmail.
alter table email_connections add column if not exists gmail_watch_expiry timestamptz;

-- ──────────────────────────────────────────────────────────────
-- Storefront OS (docs/os/storefront.md §4 — "the shop off your inbox")
-- ──────────────────────────────────────────────────────────────
-- The catalogue + the transaction primitive (a basket of goods against
-- decrementing inventory — no analogue) + an append-only stock ledger that
-- makes OCR auditable + the OCR-jobs table (Phase 3/4 — shipped now so later
-- phases need no migration). The customer IS a crm_contacts row (§1), so no
-- second customer table. Availability is decided in the DB by the two atomic
-- RPCs below — the shop siblings of hold_booking_if_capacity: two racing
-- emails can never over-reserve.

-- The catalogue (the field library, §1).
create table if not exists shop_products (
  id                    uuid primary key default gen_random_uuid(),
  tenant_id             uuid not null references tenants(id) on delete cascade,
  sku                   text,                        -- tenant's own code; nullable (many shops have none)
  name                  text not null,
  unit                  text default 'each',         -- each | kg | crate | bottle | loaf …
  unit_price            numeric,                     -- sell price per unit, GBP
  qty_on_hand           numeric not null default 0,  -- current stock, decremented on reserve/sale
  qty_reserved          numeric not null default 0,  -- held against open reservations (available = on_hand - reserved)
  best_before_date      date,                        -- current batch's sell-by (nullable; non-perishables have none)
  supplier              text,
  sell_before_lead_days integer default 2,           -- days before best_before to start nudging
  reorder_threshold     numeric,                     -- amber flag on the daily surface when qty_on_hand <= this
  active                boolean not null default true,
  custom_data           jsonb not null default '{}'::jsonb,  -- tenant extras (vintage, cut…), LLM-filled
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now()
);
-- one row per named product per tenant (dedupes OCR) — functional key must be a unique INDEX
create unique index if not exists shop_products_name_uniq on shop_products (tenant_id, lower(name));
create index if not exists shop_products_tenant_idx     on shop_products (tenant_id);
create index if not exists shop_products_bestbefore_idx on shop_products (tenant_id, best_before_date)
  where best_before_date is not null and qty_on_hand > 0;
create index if not exists shop_products_reorder_idx    on shop_products (tenant_id, active);

-- The primitive: transactions + their line items.
create table if not exists shop_transactions (
  id             uuid primary key default gen_random_uuid(),
  tenant_id      uuid not null references tenants(id) on delete cascade,
  contact_id     uuid references crm_contacts(id) on delete set null,  -- the customer IS a CRM contact (§1)
  company_id     uuid references crm_companies(id) on delete set null, -- account customers (cafe/restaurant)
  status         text not null default 'enquiry',   -- enquiry | reserved | ready | completed | declined | cancelled
  channel        text default 'email',              -- email | whatsapp | walk_in | phone
  payment_method text,                              -- cash | card | account | unpaid
  total_amount   numeric,                           -- Σ line items; denormalised for the daily surface
  currency       text not null default 'GBP',
  ready_date     date,                              -- when a reservation is collectable
  thread_id      text,                              -- links back to the email_log thread (drafter context)
  notes          text,
  custom_data    jsonb not null default '{}'::jsonb,
  created_at     timestamptz not null default now(),
  completed_at   timestamptz,
  updated_at     timestamptz not null default now()
);
create index if not exists shop_tx_tenant_status_idx on shop_transactions (tenant_id, status, ready_date);
create index if not exists shop_tx_contact_idx       on shop_transactions (tenant_id, contact_id);

create table if not exists shop_transaction_items (
  id             uuid primary key default gen_random_uuid(),
  tenant_id      uuid not null references tenants(id) on delete cascade,
  transaction_id uuid not null references shop_transactions(id) on delete cascade,
  product_id     uuid references shop_products(id) on delete set null,  -- null = free-text line not yet matched
  product_name   text not null,           -- captured verbatim (survives a product delete / pre-match)
  qty            numeric not null default 1,
  unit_price     numeric,                 -- snapshot at reservation time (price may change later)
  line_total     numeric,
  created_at     timestamptz not null default now()
);
create index if not exists shop_tx_items_tx_idx      on shop_transaction_items (tenant_id, transaction_id);
create index if not exists shop_tx_items_product_idx on shop_transaction_items (tenant_id, product_id);

-- The stock ledger: every change to on-hand, append-only (audit + OCR trust).
create table if not exists shop_stock_movements (
  id               uuid primary key default gen_random_uuid(),
  tenant_id        uuid not null references tenants(id) on delete cascade,
  product_id       uuid not null references shop_products(id) on delete cascade,
  delta            numeric not null,        -- +12 (delivery), -2 (reservation), -1 (sale), +2 (cancel)
  reason           text not null,           -- stock_in | reserve | release | sale | adjust | wastage
  source           text,                    -- ocr | manual | agent | scheduler
  source_ref       text,                    -- email_log id, transaction id, or OCR job id
  best_before_date date,                     -- the batch's sell-by, when a delivery states one
  note             text,
  created_at       timestamptz not null default now()
);
create index if not exists shop_stock_mov_product_idx on shop_stock_movements (tenant_id, product_id, created_at desc);

-- OCR extraction jobs (the killer feature's audit + confidence gate, §12.1) —
-- table ships now, engine is Phase 3.
create table if not exists shop_ocr_jobs (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id) on delete cascade,
  kind          text not null default 'delivery_note', -- delivery_note | invoice | shelf | chalkboard
  image_path    text,                    -- storage ref for the uploaded photo
  status        text not null default 'pending',  -- pending | extracted | applied | rejected
  extracted     jsonb,                   -- [{name, qty, unit, best_before, unit_price, confidence}]
  applied_at    timestamptz,
  email_log_id  text,                    -- when the photo arrived as an email attachment
  created_at    timestamptz not null default now()
);
create index if not exists shop_ocr_jobs_tenant_idx on shop_ocr_jobs (tenant_id, status, created_at desc);

alter table shop_products           enable row level security;
alter table shop_transactions       enable row level security;
alter table shop_transaction_items  enable row level security;
alter table shop_stock_movements    enable row level security;
alter table shop_ocr_jobs           enable row level security;
drop policy if exists "tenant isolation - shop_products" on shop_products;
create policy "tenant isolation - shop_products" on shop_products
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));
drop policy if exists "tenant isolation - shop_transactions" on shop_transactions;
create policy "tenant isolation - shop_transactions" on shop_transactions
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));
drop policy if exists "tenant isolation - shop_transaction_items" on shop_transaction_items;
create policy "tenant isolation - shop_transaction_items" on shop_transaction_items
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));
drop policy if exists "tenant isolation - shop_stock_movements" on shop_stock_movements;
create policy "tenant isolation - shop_stock_movements" on shop_stock_movements
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));
drop policy if exists "tenant isolation - shop_ocr_jobs" on shop_ocr_jobs;
create policy "tenant isolation - shop_ocr_jobs" on shop_ocr_jobs
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

-- The ONE atomic reservation decision (the shop sibling of hold_booking_if_capacity
-- / hold_fleet_booking, §5): a product row-lock (FOR UPDATE) serialises racing
-- holds so two concurrent emails can never over-reserve. Reserves p_qty IFF
-- (qty_on_hand - qty_reserved) >= p_qty: bumps qty_reserved, writes a 'reserve'
-- stock_movement, creates-or-updates the transaction + its line item. Returns
-- the transaction row; an EMPTY result means out_of_stock (a clean signal — no
-- exception for PostgREST to wrap), exactly like hold_booking_if_capacity.
create or replace function shop_hold_stock_if_available (
  p_tenant_id      uuid,
  p_product_id     uuid,
  p_qty            numeric,
  p_transaction_id uuid    default null,
  p_contact_id     uuid    default null,
  p_company_id     uuid    default null,
  p_ready_date     date    default null,
  p_channel        text    default 'email',
  p_thread_id      text    default null,
  p_source         text    default 'agent'
)
returns setof shop_transactions
language plpgsql volatile
set search_path = public
as $$
declare
  v_prod  shop_products%rowtype;
  v_avail numeric;
  v_tx    shop_transactions%rowtype;
  v_line  numeric;
begin
  if p_qty is null or p_qty <= 0 then
    raise exception 'invalid qty: %', p_qty;
  end if;

  -- Row-lock the product so concurrent reservations serialise on THIS row (the
  -- shop's unit of capacity is a product's available quantity, not a date).
  select * into v_prod
  from shop_products
  where id = p_product_id and tenant_id = p_tenant_id
  for update;
  if not found then
    return;   -- unknown product → out_of_stock (empty result)
  end if;

  v_avail := coalesce(v_prod.qty_on_hand, 0) - coalesce(v_prod.qty_reserved, 0);
  if v_avail < p_qty then
    return;   -- not enough available — empty result = out_of_stock
  end if;

  update shop_products
    set qty_reserved = coalesce(qty_reserved, 0) + p_qty,
        updated_at   = now()
    where id = p_product_id and tenant_id = p_tenant_id;

  v_line := round(coalesce(v_prod.unit_price, 0) * p_qty, 2);

  if p_transaction_id is null then
    insert into shop_transactions
      (tenant_id, contact_id, company_id, status, channel, ready_date, thread_id, total_amount)
    values
      (p_tenant_id, p_contact_id, p_company_id, 'reserved', p_channel, p_ready_date, p_thread_id, v_line)
    returning * into v_tx;
  else
    select * into v_tx
    from shop_transactions
    where id = p_transaction_id and tenant_id = p_tenant_id
    for update;
    if not found then
      raise exception 'unknown transaction: %', p_transaction_id;
    end if;
    update shop_transactions
      set status       = 'reserved',
          ready_date   = coalesce(p_ready_date, ready_date),
          total_amount = coalesce(total_amount, 0) + v_line,
          updated_at   = now()
      where id = p_transaction_id and tenant_id = p_tenant_id
      returning * into v_tx;
  end if;

  insert into shop_transaction_items
    (tenant_id, transaction_id, product_id, product_name, qty, unit_price, line_total)
  values
    (p_tenant_id, v_tx.id, p_product_id, v_prod.name, p_qty, v_prod.unit_price, v_line);

  insert into shop_stock_movements
    (tenant_id, product_id, delta, reason, source, source_ref)
  values
    (p_tenant_id, p_product_id, -p_qty, 'reserve', p_source, v_tx.id::text);

  return next v_tx;
end $$;

-- Atomic stock-in increment (the OCR/manual delivery path, §5): adds p_delta to
-- qty_on_hand under a row-lock and appends a 'stock_in' movement, so a delivery
-- top-up is as atomic as a reservation. Called per product line by
-- db.shop_apply_stock_in.
create or replace function shop_apply_stock_in_one (
  p_tenant_id   uuid,
  p_product_id  uuid,
  p_delta       numeric,
  p_best_before date    default null,
  p_source      text    default 'manual',
  p_source_ref  text    default null,
  p_note        text    default null
)
returns setof shop_products
language plpgsql volatile
set search_path = public
as $$
declare
  v_prod shop_products%rowtype;
begin
  if p_delta is null or p_delta = 0 then
    raise exception 'invalid stock-in delta: %', p_delta;
  end if;

  select * into v_prod
  from shop_products
  where id = p_product_id and tenant_id = p_tenant_id
  for update;
  if not found then
    raise exception 'unknown product: %', p_product_id;
  end if;

  update shop_products
    set qty_on_hand      = coalesce(qty_on_hand, 0) + p_delta,
        best_before_date = coalesce(p_best_before, best_before_date),
        updated_at       = now()
    where id = p_product_id and tenant_id = p_tenant_id
    returning * into v_prod;

  insert into shop_stock_movements
    (tenant_id, product_id, delta, reason, source, source_ref, best_before_date, note)
  values
    (p_tenant_id, p_product_id, p_delta, 'stock_in', p_source, p_source_ref, p_best_before, p_note);

  return next v_prod;
end $$;

-- ──────────────────────────────────────────────────────────────
-- SMS channel + missed-call text-back (docs/modules/module-sms-channel.md)
-- ──────────────────────────────────────────────────────────────
-- The channel spine (CRM_design §23's message_channels, final name). One row per
-- tenant per channel; the webhook routes inbound by the provisioned number.
create table if not exists channel_connections (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id) on delete cascade,
  channel_type  text not null,                        -- 'sms' (later 'whatsapp')
  phone_number  text,                                 -- E.164 — the tenant's provisioned number
  provider      text not null default 'twilio',
  provider_sids jsonb not null default '{}'::jsonb,   -- e.g. {"number_sid": "PN..."}
  config        jsonb not null default '{}'::jsonb,   -- text_back_enabled, forwarded_from, ...
  is_active     boolean not null default true,
  created_at    timestamptz not null default now()
);
-- Webhook routing key: a number maps to exactly one connection, platform-wide.
create unique index if not exists channel_conn_number_uniq
  on channel_connections (phone_number) where phone_number is not null;
create index if not exists channel_conn_tenant_idx
  on channel_connections (tenant_id, channel_type);

alter table channel_connections enable row level security;
drop policy if exists "tenant isolation - channel_connections" on channel_connections;
create policy "tenant isolation - channel_connections" on channel_connections
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

-- Which channel a message travelled on. Default 'email' — every existing row and
-- every mailbox write is untouched (module-sms-channel.md D1: one log, not
-- parallel tables — the inbox, threading, extraction, metering and quality
-- stamps all key off email_log and must not fork).
alter table email_log add column if not exists channel text not null default 'email';

-- Per-channel style overrides (module-sms-channel.md D3): {"sms": {"sign_as": ...}}.
alter table tenant_config add column if not exists channel_config jsonb not null default '{}'::jsonb;

-- SMS-only customers have no email; the phone IS the identity (module-sms-channel.md §1).
alter table crm_contacts add column if not exists phone text;
create unique index if not exists crm_contacts_tenant_phone_uniq
  on crm_contacts (tenant_id, phone) where phone is not null;

-- PECR/opt-out: a STOP is honoured forever (until START), even pre-contact —
-- keyed by number, not contact. sms.send checks this before every send.
create table if not exists sms_opt_outs (
  tenant_id    uuid not null references tenants(id) on delete cascade,
  phone        text not null,                         -- E.164
  opted_out_at timestamptz not null default now(),
  primary key (tenant_id, phone)
);

alter table sms_opt_outs enable row level security;
drop policy if exists "tenant isolation - sms_opt_outs" on sms_opt_outs;
create policy "tenant isolation - sms_opt_outs" on sms_opt_outs
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

-- Confirm-by-reply (module-sms-channel.md §6): the client's own "I'm coming"
-- (reply C / a confirm click). Distinct from status='confirmed' (the tenant's
-- approval); feeds the no-show scorer's confirmed_click feature.
alter table bookings add column if not exists client_confirmed_at timestamptz;

-- ──────────────────────────────────────────────────────────────
-- Assets OS (docs/os/assets.md §4 — "time-limited hire of property/equipment")
-- ──────────────────────────────────────────────────────────────
-- asset_units is the `vehicles` of Assets OS: the durable supply object that
-- outlives every booking against it. bookings.unit_id mirrors bookings.vehicle_id.
-- The condition/damage/deposit tables are created now (additive) so Phases B/C
-- need no new migration; only Phase A (units + unit-scoped booking) has an engine
-- today. Kept in step with scripts/db/migrations/apply_assets_migration.py.
create table if not exists asset_units (
  id                uuid primary key default gen_random_uuid(),
  tenant_id         uuid not null references tenants(id) on delete cascade,
  asset_ref         text,
  name              text not null,
  category          text,
  resource_type     text,                        -- capacity pool key; NULL = its own named unit
  rate_daily        numeric,
  rate_weekly       numeric,
  rate_monthly      numeric,
  deposit_default   numeric,
  condition_rating  numeric(2,1) default 5.0,
  condition_floor   numeric(2,1) default 3.0,
  next_service_due  date,
  service_interval_days integer,
  active            boolean not null default true,
  custom_data       jsonb not null default '{}'::jsonb,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);
create index if not exists idx_asset_units_tenant       on asset_units (tenant_id, active);
create index if not exists idx_asset_units_tenant_type  on asset_units (tenant_id, resource_type);
create index if not exists idx_asset_units_service_due  on asset_units (tenant_id, next_service_due)
  where next_service_due is not null;

alter table bookings add column if not exists unit_id    uuid references asset_units(id) on delete set null;
alter table bookings add column if not exists hire_state text;   -- held|reserved|out|returned (NULL = not a hire)
create index if not exists idx_bookings_unit on bookings (tenant_id, unit_id) where unit_id is not null;

create table if not exists condition_reports (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id) on delete cascade,
  unit_id       uuid not null references asset_units(id) on delete cascade,
  booking_id    uuid references bookings(id) on delete set null,
  phase         text not null,                   -- 'out' (handover) | 'in' (return)
  rating        numeric(2,1),
  photo_urls    jsonb not null default '[]'::jsonb,
  notes         text,
  captured_by   text,
  captured_at   timestamptz not null default now(),
  created_at    timestamptz not null default now()
);
create index if not exists idx_condition_reports_unit    on condition_reports (tenant_id, unit_id, captured_at);
create index if not exists idx_condition_reports_booking on condition_reports (tenant_id, booking_id);

create table if not exists damage_cases (
  id                 uuid primary key default gen_random_uuid(),
  tenant_id          uuid not null references tenants(id) on delete cascade,
  unit_id            uuid not null references asset_units(id) on delete cascade,
  booking_id         uuid references bookings(id) on delete set null,
  out_report_id      uuid references condition_reports(id) on delete set null,
  in_report_id       uuid references condition_reports(id) on delete set null,
  description        text,
  diff_findings      jsonb not null default '[]'::jsonb,
  diff_confidence    numeric,
  severity           text,
  proposed_deduction numeric,
  status             text not null default 'open',
  resolution         text,
  resolved_amount    numeric,
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);
create index if not exists idx_damage_cases_open on damage_cases (tenant_id, status) where status = 'open';
create index if not exists idx_damage_cases_unit on damage_cases (tenant_id, unit_id);

create table if not exists unit_deposits (
  id              uuid primary key default gen_random_uuid(),
  tenant_id       uuid not null references tenants(id) on delete cascade,
  booking_id      uuid not null references bookings(id) on delete cascade,
  unit_id         uuid references asset_units(id) on delete set null,
  amount          numeric not null,
  currency        text not null default 'GBP',
  status          text not null default 'pending', -- pending|held|deducted|refunded|failed|released
  method          text,
  external_ref    text,
  deducted_amount numeric default 0,
  damage_case_id  uuid references damage_cases(id) on delete set null,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);
-- Plain unique key as an index (never a table-level unique() constraint).
create unique index if not exists uq_unit_deposits_booking on unit_deposits (booking_id);
create index if not exists idx_unit_deposits_status on unit_deposits (tenant_id, status);

alter table asset_units enable row level security;
drop policy if exists "tenant isolation - asset_units" on asset_units;
create policy "tenant isolation - asset_units" on asset_units
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

alter table condition_reports enable row level security;
drop policy if exists "tenant isolation - condition_reports" on condition_reports;
create policy "tenant isolation - condition_reports" on condition_reports
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

alter table damage_cases enable row level security;
drop policy if exists "tenant isolation - damage_cases" on damage_cases;
create policy "tenant isolation - damage_cases" on damage_cases
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

alter table unit_deposits enable row level security;
drop policy if exists "tenant isolation - unit_deposits" on unit_deposits;
create policy "tenant isolation - unit_deposits" on unit_deposits
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

-- ── Events OS (docs/os/events.md §4) — the FULL set; only crm_events + the
-- self-filling capture hook have an engine in Phase A. An `event` IS a crm_deals
-- row (1:1) enriched, so the whole CRM machine works on events for free; these
-- three tables add the date/timings, the guest list (NOT contacts — §14) and the
-- stakeholder set the chase engine (Phase B) will drive.
create table if not exists crm_events (
  id                uuid primary key default gen_random_uuid(),
  tenant_id         uuid not null references tenants(id) on delete cascade,
  deal_id           uuid not null references crm_deals(id) on delete cascade,
  event_date        date,
  event_type        text,
  guest_count       integer,
  guest_count_final boolean not null default false,
  timings           jsonb not null default '[]'::jsonb,
  drink_licence     text,
  contingency_notes text,
  run_of_show       jsonb not null default '{}'::jsonb,
  status            text not null default 'planning',
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);
-- 1:1 with the deal — a unique INDEX, never a table-level unique() constraint.
create unique index if not exists uq_crm_events_deal on crm_events (deal_id);
create index if not exists idx_crm_events_tenant_date on crm_events (tenant_id, event_date);

create table if not exists crm_event_guests (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id) on delete cascade,
  event_id      uuid not null references crm_events(id) on delete cascade,
  name          text,
  party_size    integer not null default 1,
  rsvp          text not null default 'unknown',
  dietary       text,
  dietary_tags  jsonb not null default '[]'::jsonb,
  table_name    text,
  source_email  text,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
create index if not exists idx_crm_event_guests_event on crm_event_guests (tenant_id, event_id);
-- Idempotent guest upsert key (event_id, lower(name)) as a unique INDEX.
create unique index if not exists uq_crm_event_guests_name
  on crm_event_guests (tenant_id, event_id, lower(name));

create table if not exists crm_event_stakeholders (
  id             uuid primary key default gen_random_uuid(),
  tenant_id      uuid not null references tenants(id) on delete cascade,
  event_id       uuid not null references crm_events(id) on delete cascade,
  kind           text not null,
  role           text not null,
  name           text,
  email          text,
  contact_id     uuid references crm_contacts(id) on delete set null,
  confirmation   text not null default 'pending',
  outstanding    text,
  outstanding_promise_id uuid references crm_promises(id) on delete set null,
  last_chased_at timestamptz,
  chase_count    integer not null default 0,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);
create index if not exists idx_crm_event_stakeholders_event
  on crm_event_stakeholders (tenant_id, event_id, confirmation);
-- Idempotent stakeholder upsert keys on (event_id, kind, lower(role)) so re-extraction
-- reinforces one row rather than duplicating — a functional unique key, hence a
-- create unique index (never a table-level unique(lower(...))), like the guest index.
create unique index if not exists uq_crm_event_stakeholders_role
  on crm_event_stakeholders (tenant_id, event_id, kind, lower(role));

alter table crm_events enable row level security;
drop policy if exists "tenant isolation - crm_events" on crm_events;
create policy "tenant isolation - crm_events" on crm_events
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

alter table crm_event_guests enable row level security;
drop policy if exists "tenant isolation - crm_event_guests" on crm_event_guests;
create policy "tenant isolation - crm_event_guests" on crm_event_guests
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

alter table crm_event_stakeholders enable row level security;
drop policy if exists "tenant isolation - crm_event_stakeholders" on crm_event_stakeholders;
create policy "tenant isolation - crm_event_stakeholders" on crm_event_stakeholders
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

-- ──────────────────────────────────────────────────────────────
-- Wholesale OS (docs/os/wholesale.md §4 — "B2B distribution on repeat")
-- ──────────────────────────────────────────────────────────────
-- An order IS a crm_deal (is_order=true) carrying line items + a fulfilment
-- status distinct from the relationship pipeline's stage_id. Additive only.

alter table crm_deals add column if not exists is_order boolean not null default false;
alter table crm_deals add column if not exists wholesale_status text;
  -- quote | order | allocation | fulfilment | invoice | payment | held |
  -- skipped | on_hold_short | complete   (NULL for non-order deals)
alter table crm_deals add column if not exists required_by date;          -- ship-by date
alter table crm_deals add column if not exists standing_order_id uuid;    -- provenance
alter table crm_deals add column if not exists allocation_priority integer; -- override buyer default
create index if not exists idx_crm_deals_wholesale
  on crm_deals (tenant_id, wholesale_status, required_by) where is_order;

-- Order line items (new — deals carry no line table; lines are load-bearing
-- for allocation, margin and invoicing).
create table if not exists wholesale_order_lines (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id) on delete cascade,
  deal_id       uuid not null references crm_deals(id) on delete cascade,
  sku           text,                    -- free-text ok; producers rarely have a SKU scheme
  description   text not null,
  quantity      numeric not null default 0,
  unit          text,                    -- ea | case | kg | dozen …
  unit_price    numeric,                 -- trade price charged
  unit_cost     numeric,                 -- for margin (optional)
  allocated_qty numeric,                 -- filled at allocation; NULL = not yet allocated
  line_order    integer not null default 0,
  created_at    timestamptz not null default now()
);
create index if not exists idx_wholesale_lines_deal
  on wholesale_order_lines (tenant_id, deal_id, line_order);

-- Standing orders (new — a recurrence rule + a line template that SPAWNS order
-- deals on the daily sweep; reuses the scheduler pattern, not the sequences schema).
create table if not exists wholesale_standing_orders (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id) on delete cascade,
  company_id    uuid not null references crm_companies(id) on delete cascade,
  contact_id    uuid references crm_contacts(id) on delete set null,
  name          text,                   -- "Cornerstone — Tue/Fri bread"
  cadence       text not null,          -- weekly | fortnightly | monthly
  days_of_week  integer[] default '{}', -- 0=Mon … 6=Sun for weekly/fortnightly
  day_of_month  integer,                -- for monthly
  lead_days     integer not null default 1,  -- generate the order this many days ahead
  line_template jsonb not null default '[]', -- [{description,sku,quantity,unit,unit_price}]
  auto_confirm  boolean not null default false, -- trusted account: skip the review queue
  status        text not null default 'active',  -- active | paused | ended
  next_run_on   date,                   -- the engine's cursor
  last_run_on   date,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
create index if not exists idx_standing_orders_due
  on wholesale_standing_orders (tenant_id, status, next_run_on) where status = 'active';

alter table wholesale_order_lines enable row level security;
drop policy if exists "tenant isolation - wholesale_order_lines" on wholesale_order_lines;
create policy "tenant isolation - wholesale_order_lines" on wholesale_order_lines
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

alter table wholesale_standing_orders enable row level security;
drop policy if exists "tenant isolation - wholesale_standing_orders" on wholesale_standing_orders;
create policy "tenant isolation - wholesale_standing_orders" on wholesale_standing_orders
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

-- Partner referral programme (partners + tenant attribution) START
-- The 20%-lifetime referral track (docs/archive/2026-07/TODO.md): a partner gets a code, a
-- business types it at signup (or arrives on a ?ref= link), and the tenant is
-- attributed to the partner FOREVER (first attribution wins — the columns are
-- only ever written while referred_by_partner_id is null). Payouts stay manual
-- for now; /partner shows the partner their referrals + an earnings estimate.
create table if not exists partners (
  id               uuid primary key default gen_random_uuid(),
  name             text not null,
  email            text not null unique,   -- lowercased; matched against the partner's SSO sign-in email
  ref_code         text not null unique,   -- stored UPPERCASE; typed at signup / carried by ?ref= links
  commission_rate  numeric not null default 0.20,
  status           text not null default 'active',   -- active | disabled
  notes            text,
  created_at       timestamptz not null default now()
);
alter table partners enable row level security;   -- no policy => service-role only

-- Self-serve enrolment (2026-07-11): anyone can join by accepting the
-- programme terms — the gate is on PAYOUTS (ledger from collected invoices,
-- self-referral blocked, disable switch), not on joining. Operator-created
-- rows have source='operator' and a paper agreement instead of agreed_at.
alter table partners add column if not exists source text not null default 'operator';  -- operator | self_serve
alter table partners add column if not exists agreed_at timestamptz;   -- electronic acceptance
alter table partners add column if not exists terms_version text;
alter table partners add column if not exists signed_name text;   -- typed full name = the e-signature

alter table tenants add column if not exists referred_by_partner_id uuid references partners(id) on delete set null;
alter table tenants add column if not exists referral_code_used text;
alter table tenants add column if not exists referred_at timestamptz;
create index if not exists idx_tenants_referred_by on tenants (referred_by_partner_id);

-- Manual introductions: a partner registers a lead BEFORE the business signs
-- up (company name + contact email), protecting their attribution when no
-- referral link is used. Matched by signup email at the SSO callback. A lead
-- that loses (business already exists / another partner won the attribution)
-- is MARKED, never silently deleted — the portal shows ineligible/disputed.
create table if not exists partner_leads (
  id             uuid primary key default gen_random_uuid(),
  partner_id     uuid not null references partners(id) on delete cascade,
  company_name   text not null,
  contact_email  text not null,           -- lowercased
  status         text not null default 'registered',  -- registered | converted | disputed | ineligible
  reason         text,                    -- disputed/ineligible: other_partner | already_customer
  tenant_id      uuid references tenants(id) on delete set null,
  created_at     timestamptz not null default now(),
  expires_at     timestamptz not null default (now() + interval '90 days'),  -- attribution window
  converted_at   timestamptz
);
create index if not exists idx_partner_leads_partner on partner_leads (partner_id, created_at);
create index if not exists idx_partner_leads_email on partner_leads (contact_email);
alter table partner_leads enable row level security;   -- no policy => service-role only

-- Monthly payout statements (manual runs from /admin/partners). Created first:
-- partner_commissions FKs onto it.
create table if not exists partner_payouts (
  id            uuid primary key default gen_random_uuid(),
  partner_id    uuid not null references partners(id) on delete cascade,
  period_start  date,
  period_end    date,
  total         numeric not null,
  reference     text,                    -- bank ref / note
  paid_at       timestamptz not null default now(),
  created_at    timestamptz not null default now()
);
alter table partner_payouts enable row level security;   -- no policy => service-role only

-- The commission ledger. A row is EARNED money: written only from Stripe
-- invoice.paid (collected payment — promos/add-ons are exact, £0 trial
-- invoices never earn), 'scheduled' until a payout run marks it 'paid'.
-- unique(invoice_id) makes webhook retries idempotent. The 90-day window is
-- enforced at write time (app/partners.record_commission_for_invoice): a
-- referral whose FIRST collected payment falls outside the window earns
-- nothing; once converted in-window, every later invoice earns for life.
create table if not exists partner_commissions (
  id             uuid primary key default gen_random_uuid(),
  partner_id     uuid not null references partners(id) on delete cascade,
  tenant_id      uuid references tenants(id) on delete set null,
  invoice_id     text not null unique,   -- Stripe invoice id
  invoice_total  numeric not null,       -- GBP collected
  amount         numeric not null,       -- GBP commission (rate at earn time)
  status         text not null default 'scheduled',   -- scheduled | paid | void
  payout_id      uuid references partner_payouts(id) on delete set null,
  earned_at      timestamptz not null default now(),
  created_at     timestamptz not null default now()
);
create index if not exists idx_partner_commissions_partner on partner_commissions (partner_id, earned_at);
create index if not exists idx_partner_commissions_tenant on partner_commissions (tenant_id);
alter table partner_commissions enable row level security;   -- no policy => service-role only

-- Settlement preference (2026-07-30): a partner who is ALSO a tenant can take their
-- commission off their own subscription instead of a bank transfer — "refer two, yours
-- is free". The tenant is resolved once at opt-in and stored, so the credit always
-- lands on the same account even if the partner later belongs to several. 'cash' is
-- the default because that is what every existing partner signed up to.
alter table partners add column if not exists settlement_mode text not null default 'cash';   -- cash | credit
alter table partners add column if not exists credit_tenant_id uuid references tenants(id) on delete set null;

-- How a statement was settled. A 'credit' payout reduced the partner's own next
-- subscription invoice; 'bank' moved money. Deliberately ONE ledger either way, so
-- scheduled -> paid still happens in exactly one place (db.mark_commission_ids_paid)
-- and /partner's statement list needs no second query.
alter table partner_payouts add column if not exists method text not null default 'bank';   -- bank | credit

-- Withdrawal requests. A Stripe credit can only reduce an invoice to £0, never past
-- it, so surplus commission does not get paid by itself — the partner has to ask.
-- The request stores only the AMOUNT: the settle set is recomputed at payment time, so
-- a clawback landing in between can never be paid out against a stale selection.
create table if not exists partner_withdrawals (
  id             uuid primary key default gen_random_uuid(),
  partner_id     uuid not null references partners(id) on delete cascade,
  requested_gbp  numeric not null,
  settled_gbp    numeric,                              -- what the payout actually came to
  status         text not null default 'requested',    -- requested | paid | cancelled
  payout_id      uuid references partner_payouts(id) on delete set null,
  note           text,
  requested_at   timestamptz not null default now(),
  paid_at        timestamptz
);
create index if not exists idx_partner_withdrawals_partner on partner_withdrawals (partner_id, requested_at desc);
-- One open request per partner, enforced by the DATABASE rather than a read-then-write
-- in the route: a double-submit would otherwise create two.
create unique index if not exists idx_partner_withdrawals_one_open
  on partner_withdrawals (partner_id) where status = 'requested';
alter table partner_withdrawals enable row level security;   -- no policy => service-role only

-- Lead eligibility at registration time: does a signup account already exist
-- for this email? auth.users isn't visible to PostgREST, so a SECURITY DEFINER
-- function reads it (same pattern as count_provider_signups). Unlike that
-- harmless counter, this is an email-existence ORACLE — service-role ONLY,
-- never callable with the anon/authenticated keys.
create or replace function auth_user_exists (p_email text)
returns boolean
language sql stable security definer set search_path = auth, public
as $$
  select exists(select 1 from auth.users where lower(email) = lower(p_email));
$$;
revoke execute on function auth_user_exists(text) from public, anon, authenticated;
grant execute on function auth_user_exists(text) to service_role;
-- Partner referral programme END


-- OS founding-partner access gate (2026-07-11)
-- "Core + your OS" is request-only while the founding cohort is hand-picked:
-- the price stays public, but the OS-tier checkout is locked until the operator
-- approves the tenant. Tenants request from /billing/plans ("Request founding
-- access"); the operator vets at /admin/os-access (or grants directly from the
-- tenant page — e.g. someone met in person who never filed a request).
-- Approval flips tenants.os_access_approved, which /billing/checkout enforces
-- SERVER-SIDE for the OS-tier price ids — hiding the button is not the lock.
create table if not exists os_access_requests (
  id           uuid primary key default gen_random_uuid(),
  tenant_id    uuid not null references tenants(id) on delete cascade,
  email        text,                 -- requester's sign-in email (for the reply)
  message      text,                 -- "tell us what you run" (optional)
  created_at   timestamptz not null default now(),
  approved_at  timestamptz,          -- operator approved -> tenant unlocked
  dismissed_at timestamptz
);
create index if not exists os_access_requests_tenant_idx on os_access_requests (tenant_id);
alter table os_access_requests enable row level security;
alter table tenants add column if not exists os_access_approved boolean not null default false;


-- LLM call log — full-transcript observability (2026-07-11)
-- One row per Anthropic messages.create call, written best-effort by
-- src/app/llm.py: the EXACT request payload (system prompt, messages including
-- every tool-loop round, tool definitions) and the raw response, so the
-- operator can read every word that crossed the API. Base64 image/document
-- data is redacted to a size stub at write time; request/response are null
-- when settings.llm_log_mode = 'meta'.
-- SERVICE-ROLE ONLY (RLS on, no tenant policy): transcripts carry platform
-- prompt internals and customer email bodies — operator data, never
-- tenant-visible. Purged after settings.llm_log_retention_days by the daily
-- sweep (src/app/main.py); tenant delete cascades.
create table if not exists llm_calls (
  id            uuid primary key default gen_random_uuid(),
  created_at    timestamptz not null default now(),
  tenant_id     uuid references tenants(id) on delete cascade,
  request_id    uuid,                            -- groups every call of ONE user-facing
                                                 -- exchange (tool-loop rounds, nested
                                                 -- specialist calls, voice-learn batches)
  operation     text not null default '',        -- 'draft' | 'concierge_help' | ...
  module        text not null default 'email',
  model         text not null,
  stop_reason   text,                            -- 'api_error' when the SDK raised
  input_tokens  int not null default 0,
  output_tokens int not null default 0,
  duration_ms   int not null default 0,
  request       jsonb,
  response      jsonb
);
create index if not exists llm_calls_created_idx on llm_calls (created_at desc);
create index if not exists llm_calls_tenant_created_idx on llm_calls (tenant_id, created_at desc);
alter table llm_calls enable row level security;

-- ── Outbound send-as identity (2026-07-15) ─────────────────────────────────
-- When set, outbound mail from this mailbox carries this From address.
-- Gmail: must be a VERIFIED send-as alias on the account — Gmail silently
-- falls back to the primary address otherwise (safe). Microsoft: requires
-- SendAs rights on the address, so leave null unless that is granted.
-- First use: hello@blam8.com on the BLAM8 tenant's admin.blam8 mailbox, so
-- the product's own replies match the outreach identity.
alter table email_connections add column if not exists send_as_address text;

-- Somebody is told when a mailbox stops (Kris 2026-09-04)
-- ──────────────────────────────────────────────────────────────
-- A dead OAuth grant used to be invisible. The Gmail poller and the calendar
-- sync backed off to six-hourly and wrote ONE log line; the Graph subscription
-- renewer swallowed its exceptions outright, so a Microsoft mailbox's webhook
-- simply expired and auto-draft stopped within three days. In every case the
-- product had stopped doing the one thing it is paid for and nobody was told.
--
-- The streak lives HERE rather than in src/app/sync_backoff.py's process memory,
-- for two reasons: a redeploy must not reset the clock (Render redeploys often
-- enough to hide an hour-long outage forever), and "we already told them" has
-- to survive a restart or every deploy re-buzzes the same dead mailbox.
--
--   sync_failing_since — when the CURRENT failure streak began; null = healthy.
--   sync_error         — the last failure, short, for the operator to read.
--   health_alerted_at  — when the owner was told. Null while failing means the
--                        streak has not outlasted the grace period yet.
alter table email_connections add column if not exists sync_failing_since timestamptz;
alter table email_connections add column if not exists sync_error         text;
alter table email_connections add column if not exists health_alerted_at  timestamptz;

-- ── LLM exchange correlation (2026-07-26) ──────────────────────────────────
-- One user-facing exchange (a draft, a concierge/portal turn, a voice-learn
-- run) spans many llm_calls rows; request_id groups them so per-exchange cost
-- and the "promised a lookup, called no tool" pattern are queryable.
alter table llm_calls add column if not exists request_id uuid;


-- Support tickets — the chatbot raises tracked tickets (2026-07-25)
-- ──────────────────────────────────────────────────────────────
-- The concierge (pre-auth ask mode) can now collect a support issue
-- conversationally and file it as a TICKET into the same feature_requests
-- pipeline the in-app form and help mode already use (extend, don't
-- duplicate). Three changes:
--   * tenant_id goes NULLABLE: a website visitor who cannot log in (the
--     most common support case — e.g. a broken sign-in) has no tenant. RLS
--     stays as-is: tenant-JWT clients can never see a NULL-tenant row
--     (NULL never matches auth_tenant_ids()), so anonymous tickets are
--     service-role only — created and looked up exclusively by the server.
--   * every NEW row gets a short human reference (BLAM8-1042) from a global
--     sequence, minted by next_ticket_reference() (security definer, so the
--     service role needs no direct sequence grant). Existing rows are
--     backfilled so the tenant page can show a reference on everything.
--   * ticket fields: who to reply to, what account type, the raw error, an
--     operator status note (shown back on status checks), triage context.
-- Status vocabulary: feature rows keep new|reviewing|planned|done; support
-- tickets use open|in_progress|resolved|closed (kept as text — the app maps
-- labels; no constraint so legacy values never break an insert).
alter table feature_requests alter column tenant_id drop not null;
alter table feature_requests add column if not exists reference     text unique;
alter table feature_requests add column if not exists user_email    text;
alter table feature_requests add column if not exists account_type  text;          -- microsoft|google|none|'' (unknown)
alter table feature_requests add column if not exists error_message text;
alter table feature_requests add column if not exists severity      text not null default 'normal';  -- normal|high
alter table feature_requests add column if not exists status_note   text;          -- last operator update, quoted on status checks
alter table feature_requests add column if not exists page_url      text;          -- auto-collected triage context
alter table feature_requests add column if not exists user_agent    text;
alter table feature_requests add column if not exists redacted_at   timestamptz;   -- GDPR retention: PII scrubbed stamp

create sequence if not exists support_ticket_ref_seq start with 1001;

create or replace function next_ticket_reference() returns text
language sql security definer as $$
  select 'BLAM8-' || nextval('support_ticket_ref_seq')::text;
$$;

-- Backfill: give pre-existing rows a reference too (idempotent — only NULLs).
update feature_requests set reference = next_ticket_reference() where reference is null;

create index if not exists feature_requests_reference_idx on feature_requests (reference);
create index if not exists feature_requests_email_created_idx on feature_requests (user_email, created_at);


-- Voice auto-learning from sent mail (2026-07-25)
-- ──────────────────────────────────────────────────────────────
-- On mailbox connect (mid-onboarding) BLAM8 reads the tenant's recent sent
-- emails, extracts a structured voice profile with the LLM, and parks it here
-- for the onboarding voice step / settings page to show as a review screen.
-- Raw sent emails are NEVER stored — the profile is the only artefact.
--   voice_profile          the merged structured profile (src/app/voice_learning.py)
--   voice_profile_source   auto_learned | hybrid (auto then hand-adjusted) | manual
--   voice_learning_status  running | ready | accepted | insufficient | failed
--   voice_sample_draft     the "here's how I'll sound" preview reply
--   voice_notes            learned style description the drafter consumes
--                          (its own column so applying a profile never
--                          clobbers hand-written custom_instructions)
alter table tenant_config add column if not exists voice_profile jsonb;
alter table tenant_config add column if not exists voice_profile_source text;
alter table tenant_config add column if not exists voice_learning_status text;
alter table tenant_config add column if not exists voice_sample_draft text;
alter table tenant_config add column if not exists voice_emails_analysed integer;
alter table tenant_config add column if not exists voice_learned_at timestamptz;
alter table tenant_config add column if not exists voice_notes text;


-- Concierge business Q&A — chat history (2026-07-25)
-- ──────────────────────────────────────────────────────────────
-- Phase 1 of the in-app business concierge (docs/modules/module-concierge-business.md):
-- a READ-ONLY Q&A chat over the tenant's own data (inbox, CRM, diary, KB).
-- This table stores only the transcript so the /app/concierge page reloads
-- with history — the answers themselves are always recomputed live. user_id
-- records who asked (nullable: single-operator tenants predate per-user
-- attribution). Retention: the app trims to the newest rows per tenant.
create table if not exists concierge_messages (
  id         uuid primary key default gen_random_uuid(),
  tenant_id  uuid not null references tenants(id) on delete cascade,
  user_id    uuid,
  role       text not null check (role in ('user','assistant')),
  content    text not null,
  links      jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now()
);
create index if not exists idx_concierge_messages_tenant
  on concierge_messages (tenant_id, created_at desc);

alter table concierge_messages enable row level security;
drop policy if exists "tenant isolation - concierge_messages" on concierge_messages;
create policy "tenant isolation - concierge_messages" on concierge_messages
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));


-- Concierge business actions — proposals + audit (Phase 2, 2026-07-26)
-- ──────────────────────────────────────────────────────────────
-- docs/modules/module-concierge-business.md: the concierge may PROPOSE an action
-- (draft a reply, move a booking, update a deal stage, create a follow-up or
-- contact, add a KB entry) — a card the operator must explicitly Confirm.
-- One row per proposal, and the row IS the audit log:
--   status: pending -> executed | cancelled | superseded | failed
--   payload: the validated parameters incl. old->new values (server-built,
--            never raw model output)
--   result:  what the executor actually did / the failure reason
-- Confirmations are single-use (atomic pending->executed claim) and expire
-- 5 minutes after creation (enforced at claim time, not by a sweeper).
create table if not exists concierge_proposals (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references tenants(id) on delete cascade,
  user_id     uuid,
  kind        text not null,
  summary     text not null,
  payload     jsonb not null default '{}'::jsonb,
  status      text not null default 'pending',
  result      jsonb,
  created_at  timestamptz not null default now(),
  executed_at timestamptz
);
create index if not exists idx_concierge_proposals_tenant
  on concierge_proposals (tenant_id, status, created_at desc);

alter table concierge_proposals enable row level security;
drop policy if exists "tenant isolation - concierge_proposals" on concierge_proposals;
create policy "tenant isolation - concierge_proposals" on concierge_proposals
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

-- The Concierge's task list (docs/plan-concierge-task-list.md, 2026-09-25):
-- one row per task she works in the background while the owner talks on.
-- status: queued | working | waiting (a card in the waiting list) | done |
-- needs_you (her one question) | failed. Applied by
-- scripts/db/migrations/apply_concierge_tasks_migration.py.
create table if not exists concierge_tasks (
  id           uuid primary key default gen_random_uuid(),
  tenant_id    uuid not null references tenants(id) on delete cascade,
  user_id      uuid,
  batch_id     uuid not null,
  position     int  not null default 0,
  title        text not null,
  instruction  text not null,
  status       text not null default 'queued',
  outcome      text,
  proposal_id  uuid,
  surface      text not null default 'typed',
  created_at   timestamptz not null default now(),
  started_at   timestamptz,
  finished_at  timestamptz
);
create index if not exists idx_concierge_tasks_batch on concierge_tasks (batch_id, position);
create index if not exists idx_concierge_tasks_open on concierge_tasks (status, created_at);
alter table concierge_tasks enable row level security;
drop policy if exists "tenant isolation - concierge_tasks" on concierge_tasks;
create policy "tenant isolation - concierge_tasks" on concierge_tasks
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

-- The concierge's "move a booking" needs what nothing else has needed yet: a
-- date/window change that KEEPS the row (booking_ref, details, deposits,
-- calendar linkage) while deciding capacity atomically. Sibling of
-- hold_booking_if_capacity: same advisory lock, same overlap rule, but the
-- moving row never blocks itself and the write is an UPDATE. Returns the
-- updated row, or nothing when the target is full / the row isn't movable.
-- Date validation (notice/horizon/working days/blackouts) happens in
-- src/app/scheduling.py BEFORE this call, exactly like holds.
create or replace function move_booking_if_capacity (
  p_tenant_id  uuid,
  p_booking_id uuid,
  p_new_date   date,
  p_new_window text
)
returns setof bookings
language plpgsql volatile
set search_path = public
as $$
declare
  v_capacity int;
  v_used     int;
begin
  if p_new_window not in ('day', 'am', 'pm') and p_new_window !~ '^\d{2}:\d{2}$' then
    raise exception 'invalid window: %', p_new_window;
  end if;

  -- Only a live booking the tenant owns can move.
  if not exists (
    select 1 from bookings
    where id = p_booking_id and tenant_id = p_tenant_id
      and status in ('proposed', 'confirmed')
  ) then
    return;
  end if;

  perform pg_advisory_xact_lock(
    hashtextextended(p_tenant_id::text || ':' || p_new_date::text, 0)
  );

  select capacity_per_window into v_capacity
  from scheduling_config where tenant_id = p_tenant_id;
  v_capacity := coalesce(v_capacity, 4);

  select count(*) into v_used
  from bookings b
  where b.tenant_id = p_tenant_id
    and b.booking_date = p_new_date
    and (b.status in ('proposed', 'confirmed')
         or (b.status = 'offered' and (b.expires_at is null or b.expires_at > now())))
    and b.spaces is null
    and b.id <> p_booking_id
    and (
      p_new_window = 'day' or b."window" = 'day' or b."window" = p_new_window
      or (b."window" = 'am' and p_new_window ~ '^\d{2}:\d{2}$' and p_new_window <  '12:00')
      or (b."window" = 'pm' and p_new_window ~ '^\d{2}:\d{2}$' and p_new_window >= '12:00')
      or (p_new_window = 'am' and b."window" ~ '^\d{2}:\d{2}$' and b."window" <  '12:00')
      or (p_new_window = 'pm' and b."window" ~ '^\d{2}:\d{2}$' and b."window" >= '12:00')
    );

  if v_used >= v_capacity then
    return;
  end if;

  return query
    update bookings
       set booking_date = p_new_date, "window" = p_new_window, updated_at = now()
     where id = p_booking_id and tenant_id = p_tenant_id
       and status in ('proposed', 'confirmed')
     returning *;
end
$$;


-- Web Push subscriptions — PWA notifications (2026-07-26)
-- ──────────────────────────────────────────────────────────────
-- One row per subscribed browser/device (src/app/push_routes.py stores the
-- PushSubscription the browser mints; src/app/push.py fans "Draft ready" out to a
-- tenant's rows). endpoint is the natural key: the same device re-subscribing
-- — or a DIFFERENT user signing in on that device — takes the row over, so a
-- device only ever receives the notifications of whoever is currently signed
-- in on it (that cross-user upsert is why the app writes via service role).
-- A 404/410 from the push service deletes the row (device unsubscribed).
create table if not exists push_subscriptions (
  id           uuid primary key default gen_random_uuid(),
  tenant_id    uuid not null references tenants(id) on delete cascade,
  user_id      uuid not null,
  endpoint     text not null unique,
  keys_p256dh  text not null,
  keys_auth    text not null,
  created_at   timestamptz not null default now(),
  last_used_at timestamptz
);
create index if not exists idx_push_subscriptions_tenant
  on push_subscriptions (tenant_id);

alter table push_subscriptions enable row level security;
-- Per-USER isolation (not per-tenant): another member of the same tenant has
-- no business reading — or deleting — the push credentials of my devices.
drop policy if exists "own subscriptions - push_subscriptions" on push_subscriptions;
create policy "own subscriptions - push_subscriptions" on push_subscriptions
  for all using (user_id = auth.uid())
  with check (user_id = auth.uid() and tenant_id in (select auth_tenant_ids()));


-- Auto-categorisation — smart inbox labels (2026-07-26)
-- ──────────────────────────────────────────────────────────────
-- Every email_log row carries the conversation's category at that point in its
-- life; the inbox aggregates latest-row-wins per contact (same contract as
-- thread_state). Values: needs_reply | draft_ready | quote_sent |
-- booking_pending | fyi | automated. category_reason is the classifier's
-- one-line why (shown in the thread header); category_source is 'auto'
-- (pipeline/LLM) or 'manual' (the owner picked the label — sticks until the
-- next pipeline event restamps it). Legacy rows keep null and the app derives
-- a display category from status/thread_state, so no backfill is needed.
alter table email_log add column if not exists category text;
alter table email_log add column if not exists category_reason text;
alter table email_log add column if not exists category_source text not null default 'auto';
create index if not exists email_log_category_idx on email_log (tenant_id, category);


-- Customer Portal — public self-service page per business (2026-07-26)
-- ──────────────────────────────────────────────────────────────
-- blam8.com/c/{portal_slug}: customers book, check/reschedule bookings, get
-- quotes and ask questions — same brain (KB, diary, CRM), customer-facing side.
--
-- portal_slug is the tenant's public handle (lowercase/alnum/hyphens, derived
-- from the business name, editable in settings; unique — the URL derives the
-- tenant, NEVER the request body). Null = never visited portal settings yet;
-- the app mints one lazily.
alter table tenants add column if not exists portal_slug text;
create unique index if not exists tenants_portal_slug_idx
  on tenants (portal_slug) where portal_slug is not null;

-- Per-contact reference code ({PREFIX}-{4 alnum, no O/0/I/1}, e.g. ACME-7K3M):
-- the customer-facing handle quoted in every reply's sign-off and typed into
-- the portal to pull up their bookings. Minted lazily (first send / first
-- portal use), unique per tenant. The code is a lookup capability like
-- bookings.track_token, only human-typeable — rate limits on the portal
-- endpoints are what keep it un-guessable in practice.
alter table crm_contacts add column if not exists reference_code text;
create unique index if not exists crm_contacts_reference_code_idx
  on crm_contacts (tenant_id, reference_code) where reference_code is not null;

-- Portal settings ride tenant_config (one row per tenant, like every other
-- config field). portal_enabled defaults ON — the page is the product's public
-- front door; owners can switch it off in Settings → Customer Portal.
alter table tenant_config add column if not exists portal_enabled boolean not null default true;
alter table tenant_config add column if not exists portal_description text;
alter table tenant_config add column if not exists portal_service_area text;
alter table tenant_config add column if not exists portal_welcome text;
alter table tenant_config add column if not exists portal_show_prices boolean not null default true;
-- Customer reschedules (2026-07-31): 'auto' applies a portal move on the spot
-- (the original behaviour); 'approve' turns it into a needs-attention inbox
-- request the owner confirms first (docs/modules/module-portal.md §Reschedules).
alter table tenant_config add column if not exists portal_reschedule_mode text not null default 'auto';


-- Agent trace — execution-narrative observability (2026-07-26)
-- ──────────────────────────────────────────────────────────────
-- One row per AI action/decision the curated Agent Log and llm_calls don't
-- capture: concierge proposal lifecycle, portal chat turns + identity
-- verification attempts, KB retrievals, smart-label transitions, push
-- delivery, voice-learning runs. src/app/trace.py writes best-effort (a trace
-- failure never breaks the action). This is the operator's debug trail —
-- complementary to concierge_proposals (the compliance audit, untouched) and
-- agent_events (the tenant-visible narrative).
--   trace_type   what happened (concierge_propose, portal_chat, categorisation…)
--   outcome      'ok' | feature-specific failures (expired, denied, not_found,
--                limit, error…) — indexed so "show me every failure" is one query
--   request_id   joins llm_calls.request_id — the LLM turn(s) behind this action
--   session_id   opaque per-visitor handle (portal: sha256 prefix of the chat
--                session token — never the token itself)
--   detail       the trace payload (jsonb bag; may carry message snippets, so
--                rows age out with llm_calls on the same retention sweep)
-- SERVICE-ROLE ONLY (RLS on, no tenant policy) — same stance as llm_calls:
-- traces carry internals the tenant page doesn't show.
create table if not exists agent_trace (
  id           uuid primary key default gen_random_uuid(),
  created_at   timestamptz not null default now(),
  tenant_id    uuid references tenants(id) on delete cascade,
  trace_type   text not null,
  outcome      text not null default 'ok',
  request_id   uuid,
  session_id   text,
  user_id      uuid,
  contact      text,
  email_log_id uuid,
  booking_id   uuid,
  proposal_id  uuid,
  latency_ms   int,
  detail       jsonb not null default '{}'::jsonb
);
create index if not exists agent_trace_tenant_idx   on agent_trace (tenant_id, created_at desc);
create index if not exists agent_trace_type_idx     on agent_trace (trace_type, created_at desc);
create index if not exists agent_trace_outcome_idx  on agent_trace (outcome, created_at desc);
create index if not exists agent_trace_request_idx  on agent_trace (request_id);
create index if not exists agent_trace_email_idx    on agent_trace (email_log_id);
alter table agent_trace enable row level security;


-- Draft suppression — stop drafting for contacts the owner ignores (2026-07-26)
-- ──────────────────────────────────────────────────────────────
-- docs/modules/module-draft-suppression.md. One row per (tenant, sender) tracking how many
-- CONSECUTIVE drafts the owner never sent. At 3 the agent stops drafting for that
-- sender (status 'suppressed'): inbound still arrives + files under FYI, but the
-- draft-generation LLM call is skipped, and the thread shows an escalation banner
-- (Resume drafting / Ignore permanently / Draft now). Two further inbounds with no
-- owner action auto-escalate to 'permanently_ignored' (silent — no banner). Sending
-- a draft resets the streak and reactivates a 'suppressed' contact; a permanent
-- ignore only lifts via Resume/Un-ignore. Distinct from ignored_contacts (the hard
-- ignore: thread hidden, inbound not even logged) — a suppressed contact stays
-- visible, just undrafted. Re-engagement safety valve: an unusual email (urgency
-- keywords / 3x the contact's average length) still gets a one-off draft.
create table if not exists contact_draft_stats (
  id                        uuid primary key default gen_random_uuid(),
  tenant_id                 uuid not null references tenants(id) on delete cascade,
  address                   text not null,
  status                    text not null default 'active',  -- 'active' | 'suppressed' | 'permanently_ignored'
  consecutive_unsent_drafts int  not null default 0,         -- the streak; reset by a send / Resume
  total_unsent_drafts       int  not null default 0,         -- lifetime, for the banner copy
  total_sent_drafts         int  not null default 0,
  emails_since_suppressed   int  not null default 0,         -- inbounds while 'suppressed' (auto-escalation fuse)
  suppressed_at             timestamptz,
  suppressed_by             text,                            -- 'owner' | 'auto'
  suppression_reason        text,                            -- 'threshold_reached' | 'permanently_ignored'
  first_unsent_draft_at     timestamptz,
  last_unsent_draft_at      timestamptz,
  created_at                timestamptz not null default now(),
  updated_at                timestamptz not null default now(),
  unique (tenant_id, address)
);
create index if not exists contact_draft_stats_status_idx
  on contact_draft_stats (tenant_id, status);
alter table contact_draft_stats enable row level security;
drop policy if exists "tenant isolation - contact_draft_stats" on contact_draft_stats;
create policy "tenant isolation - contact_draft_stats" on contact_draft_stats
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

-- ──────────────────────────────────────────────────────────────
-- Invoicing & Payments enhancement (docs/modules/module-invoicing.md)
-- ──────────────────────────────────────────────────────────────
-- Invoicing extracted into a standalone core module for EVERY tenant (no longer
-- implicit with tms/fsm) + a payments layer: issue/due dates, CRM links, Stripe
-- Connect "Pay Now", payment tracking, reminder chasing and overdue milestones.
-- Statuses grow beyond draft|sent|paid: viewed is a stamp (viewed_at), and
-- overdue|partially_paid|cancelled|void are stamped by app code / the daily job.
alter table invoices add column if not exists contact_id  uuid references crm_contacts(id) on delete set null;
alter table invoices add column if not exists deal_id     uuid references crm_deals(id)    on delete set null;
alter table invoices add column if not exists booking_id  uuid references bookings(id)     on delete set null;
alter table invoices add column if not exists issue_date  date;
alter table invoices add column if not exists due_date    date;
alter table invoices add column if not exists notes       text;
alter table invoices add column if not exists created_by  text not null default 'owner';   -- 'ai' | 'owner'
alter table invoices add column if not exists amount_paid numeric(12,2) not null default 0;
alter table invoices add column if not exists payment_method text;                          -- card|bank_transfer|cash|other
alter table invoices add column if not exists stripe_payment_intent_id text;
alter table invoices add column if not exists payment_link text;                            -- our /pay/{id} URL, stamped on send
alter table invoices add column if not exists viewed_at timestamptz;                        -- first customer open (pay page / portal)
alter table invoices add column if not exists overdue_notified_days integer not null default 0;  -- last overdue milestone pushed (0|7|14|30)
alter table invoices add column if not exists reminders_sent integer not null default 0;    -- payment reminders sent (escalation signal)
alter table invoices add column if not exists last_reminder_at timestamptz;
create index if not exists invoices_customer_idx on invoices (tenant_id, customer_email);
-- Refs were always minted by the atomic next_invoice_ref; this makes uniqueness a
-- guarantee, not a convention. Partial: legacy rows may have a null ref.
create unique index if not exists invoices_tenant_ref_uniq
  on invoices (tenant_id, invoice_ref) where invoice_ref is not null;

-- Lines grow quantity/unit price (amount stays the line total, so every existing
-- reader is unchanged) + a pointer back to the KB pricing entry that priced it.
alter table invoice_lines add column if not exists quantity   numeric(10,2) not null default 1;
alter table invoice_lines add column if not exists unit_price numeric(12,2);
alter table invoice_lines add column if not exists kb_entry_id text;

-- The tenant's OWN Stripe account (Connect OAuth) — where their customers' invoice
-- payments land. Distinct from tenants.stripe_customer_id (the tenant as BLAM8's payer).
alter table tenants add column if not exists stripe_account_id text;

-- Bank-transfer fallback for tenants without Stripe — shown on the invoice + pay page.
alter table invoice_settings add column if not exists bank_account_name   text;

-- ──────────────────────────────────────────────────────────────
-- Tenant visits (operator journey card — src/app/journey.py)
-- ──────────────────────────────────────────────────────────────
-- One row per distinct visit to the app: a request more than 30 minutes after
-- the tenant's previous one starts a new row; anything sooner extends the
-- current one (writes throttled in-process, so last_seen_at is ~a minute
-- coarse). Powers the /admin tenant page's "who came back, when, for how long"
-- — never read by tenant-facing code. Service-role only: RLS on, no policy.
create table if not exists tenant_visits (
  id           uuid primary key default gen_random_uuid(),
  tenant_id    uuid not null references tenants(id) on delete cascade,
  user_id      uuid,
  started_at   timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  requests     integer not null default 1
);
create index if not exists tenant_visits_tenant_idx
  on tenant_visits (tenant_id, last_seen_at desc);
alter table tenant_visits enable row level security;
alter table invoice_settings add column if not exists bank_sort_code     text;
alter table invoice_settings add column if not exists bank_account_number text;
-- VAT now defaults OFF for tenants with no settings row (most BLAM8 tenants are not
-- VAT registered; a registered business flips it on in invoice settings).
alter table invoice_settings alter column show_vat set default false;

-- "Invoice viewed" push is opt-in (some owners find it noisy).
alter table tenant_config add column if not exists notify_invoice_viewed boolean not null default false;


-- Concierge transparency & proactivity (docs/modules/module-concierge-transparency.md, 2026-07-30)
-- ──────────────────────────────────────────────────────────────
-- trail: the ghost trail — which lookups the assistant ran for THIS answer,
-- [{name, label, detail}], persisted on the assistant row so history replays
-- with its pills. detail is a short sanitised summary (a search query), never
-- raw tool input.
alter table concierge_messages add column if not exists trail jsonb not null default '[]'::jsonb;

-- draft_sources: what a draft was grounded on — [{source, chunk_id, similarity}]
-- captured at retrieval time (src/app/pipeline.py). Null = drafted before this
-- existed; [] = retrieval ran and found nothing (the honest "inferred, not from
-- your data" signal). Chunk content is fetched live on click, never duplicated.
alter table email_log add column if not exists draft_sources jsonb;

-- The in-app notification feed behind the concierge bubble's badge/glow. One
-- row per proactive signal, written wherever a web push fires (src/app/push.py) —
-- push is delivery, this row is the record, so it exists even with push off.
-- seen_at stamps when the owner opened the panel; rows age out after 30 days
-- (purge rides src/app/jobs/purge_email_log.py).
create table if not exists concierge_notifications (
  id         uuid primary key default gen_random_uuid(),
  tenant_id  uuid not null references tenants(id) on delete cascade,
  kind       text not null,
  title      text not null,
  body       text not null default '',
  url        text not null default '',
  created_at timestamptz not null default now(),
  seen_at    timestamptz
);
create index if not exists idx_concierge_notifications_tenant
  on concierge_notifications (tenant_id, seen_at, created_at desc);

alter table concierge_notifications enable row level security;
drop policy if exists "tenant isolation - concierge_notifications" on concierge_notifications;
create policy "tenant isolation - concierge_notifications" on concierge_notifications
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));


-- ──────────────────────────────────────────────────────────────
-- Quotes module (docs/modules/module-quotes.md, 2026-07-30)
-- ──────────────────────────────────────────────────────────────
-- The client-facing quote document: generated from the enquiry (or by the
-- owner), sent as a /q/{id} link from the tenant's own mailbox, accepted
-- online with a typed-name signature. Acceptance freezes acceptance_snapshot
-- (the exact selected lines + totals as agreed — the scope-dispute shield) and
-- stamps bookings.quoted_amount so every existing invoicing reader works
-- unchanged. Shares invoice_settings for branding/VAT so the two documents can
-- never drift apart visually. "viewed" is a stamp, not a status (mirror
-- invoices.viewed_at). status: draft | sent | accepted | declined | expired |
-- invoiced | cancelled.
create table if not exists quotes (
  id                  uuid primary key default gen_random_uuid(),
  tenant_id           uuid not null references tenants(id) on delete cascade,
  quote_ref           text,
  customer_email      text,
  customer_name       text,
  contact_id          uuid references crm_contacts(id) on delete set null,
  deal_id             uuid references crm_deals(id)    on delete set null,
  booking_id          uuid references bookings(id)     on delete set null,
  email_log_id        uuid references email_log(id)    on delete set null,  -- the enquiry it answers
  title               text,
  intro               text,          -- short note above the lines on /q + the email
  notes               text,          -- small print under the lines
  status              text not null default 'draft',
  issue_date          date,
  valid_until         date,          -- default issue + 30d (db.DEFAULT_QUOTE_VALID_DAYS)
  subtotal            numeric(12,2) not null default 0,   -- selected lines only
  vat                 numeric(12,2) not null default 0,
  total               numeric(12,2) not null default 0,
  show_item_prices    boolean not null default true,      -- false = lump-sum display
  deposit_percent     numeric(5,2),                       -- null = no deposit at acceptance
  version             integer not null default 1,         -- bumped on any post-send edit
  sent_at             timestamptz,
  viewed_at           timestamptz,                        -- first /q open
  view_count          integer not null default 0,         -- the "they keep looking" signal
  accepted_at         timestamptz,
  accepted_name       text,                               -- typed-name e-sign
  acceptance_snapshot jsonb,                              -- frozen {version, lines, subtotal, vat, total}
  declined_at         timestamptz,
  decline_reason      text,
  deposit_invoice_id  uuid references invoices(id) on delete set null,
  invoice_id          uuid references invoices(id) on delete set null,      -- what it became
  reminders_sent      integer not null default 0,
  last_reminder_at    timestamptz,
  created_by          text not null default 'owner',      -- 'ai' | 'owner'
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);
create index if not exists quotes_tenant_idx on quotes (tenant_id, created_at);
create index if not exists quotes_status_idx on quotes (tenant_id, status);

create table if not exists quote_lines (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references tenants(id) on delete cascade,  -- denormalised for RLS
  quote_id    uuid not null references quotes(id) on delete cascade,
  booking_id  uuid references bookings(id) on delete set null,
  description text not null,
  quantity    numeric(10,2) not null default 1,
  unit_price  numeric(12,2) not null default 0,
  amount      numeric(12,2) not null default 0,   -- the line TOTAL (invoice_lines contract)
  is_optional boolean not null default false,     -- client can tick/untick on /q
  is_selected boolean not null default true,      -- server-side state of that tick
  line_order  integer not null default 0,
  kb_entry_id uuid,
  created_at  timestamptz not null default now()
);
create index if not exists quote_lines_quote_idx on quote_lines (quote_id);

-- Quote ref counter (mirrors invoice_seq → next_invoice_ref). QUO-1001…
alter table tenant_counters add column if not exists quote_seq integer not null default 0;
create or replace function next_quote_ref (p_tenant_id uuid)
returns text
language plpgsql volatile
set search_path = public
as $$
declare
  v_n integer;
begin
  insert into tenant_counters (tenant_id, quote_seq) values (p_tenant_id, 1)
  on conflict (tenant_id) do update set quote_seq = tenant_counters.quote_seq + 1
  returning quote_seq into v_n;
  return 'QUO-' || (1000 + v_n);
end $$;

alter table quotes      enable row level security;
alter table quote_lines enable row level security;
drop policy if exists "tenant isolation - quotes" on quotes;
create policy "tenant isolation - quotes" on quotes
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));
drop policy if exists "tenant isolation - quote_lines" on quote_lines;
create policy "tenant isolation - quote_lines" on quote_lines
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

-- A line can count towards a deposit rule on part of a quote ("ticked" basis), and the
-- quote then keeps the net sum those lines make: deposit = deposit_base x deposit_percent.
-- null deposit_base = the whole subtotal (every quote before this).
alter table quote_lines add column if not exists counts_to_deposit_rule boolean not null default false;
alter table quotes add column if not exists deposit_base numeric(12,2);

-- A document remembers whether its prices include VAT (the owner's own price basis when it
-- was made). null = VAT added on top, which is every document before this.
alter table quotes add column if not exists prices_include_vat boolean;
alter table invoices add column if not exists prices_include_vat boolean;

-- Quote defaults ride invoice_settings (one settings surface for both documents).
alter table invoice_settings add column if not exists default_quote_valid_months integer not null default 30;
alter table invoice_settings add column if not exists default_deposit_percent  numeric(5,2);

-- "Quote viewed" push is opt-in-out per tenant. Default ON — the research's
-- single most-loved feature; the invoice sibling stays opt-in (its default
-- predates this and flipping it would surprise live tenants).
alter table tenant_config add column if not exists notify_quote_viewed boolean not null default true;


-- Quotes Phase 3 (docs/modules/module-quotes.md, 2026-07-30) — the quote writes itself
-- ──────────────────────────────────────────────────────────────
-- When the reply drafter prices a booking from an enquiry, BLAM8 also drafts
-- the quote DOC (zero extra LLM — the pricing work already happened).
--   off      never auto-draft
--   propose  draft the quote + notify; the owner reviews and sends it (default)
--   auto     draft AND weave the /q accept link into the outgoing reply at
--            send time (approve or auto-send) — the quote goes out with the
--            answer, "sent before leaving the driveway"
alter table tenant_config add column if not exists quote_auto_draft text not null default 'propose';


-- Smart booking: offered-slot holds (docs/modules/module-smart-booking-offers.md, 2026-07-31)
-- ──────────────────────────────────────────────────────────────
-- The soft state between "free" and "held": every slot the agent OFFERS in a
-- reply becomes a bookings row with status='offered' and a TTL, so it counts
-- against capacity (exclusive offers — no two enquiries get the same slot) and
-- releases automatically when the customer goes quiet. The deadline is stated
-- in the customer's email, so it is a promise the sweep keeps.
-- The hold_booking_if_capacity / move_booking_if_capacity definitions above
-- carry the offered-aware capacity count + p_expires_at; the migration script
-- (scripts/db/migrations/apply_offered_slots_migration.py) re-runs them for existing DBs
-- after dropping the old 9-arg signature (avoids a PostgREST overload clash).
alter table bookings add column if not exists expires_at timestamptz;
-- How long an offered slot stays reserved before auto-release, tenant-tunable
-- (Diary settings). Capped per-slot at the slot's own start in app code.
alter table scheduling_config add column if not exists offer_ttl_hours int not null default 24;

-- Smart booking Phase 3: the pre-expiry offer nudge (docs/modules/module-smart-booking-offers.md)
-- ──────────────────────────────────────────────────────────────
-- One friendly chase before an offer set releases ("still want Tuesday 10:00?
-- It's released at 17:00 today"). offer_nudged_at is the per-row dedupe stamp
-- (the interval sweep would otherwise re-fire every tick inside the lead
-- window); offer_nudge_enabled is the tenant's Diary-settings toggle —
-- default ON, the draft-first/auto-send ladder still decides whether the
-- nudge SENDS or waits for approval.
alter table bookings add column if not exists offer_nudged_at timestamptz;
alter table scheduling_config add column if not exists offer_nudge_enabled boolean not null default true;

-- Full-product trial (2026-07-31, Julius): new tenants get the Pro module set
-- from sign-up (seeded in app/db.ensure_tenant_for_user via apply_plan_modules),
-- so the trial shows the diary + self-filling CRM instead of an email-only
-- slice. LLM cost stays bounded by TRIAL_EMAIL_CAP plus this concierge counter
-- (cap compared in src/app/trial.py, TRIAL_CONCIERGE_CAP, enforcement behind the
-- same require_subscription_gate flag as the email pot).
alter table tenants add column if not exists trial_concierge_used int not null default 0;

-- Atomic concierge-counter increment — the concierge sibling of
-- increment_trial_email, minus the status flip (the route refuses politely
-- instead; a converted tenant is never counted).
create or replace function increment_trial_concierge (p_tenant_id uuid)
returns integer
language sql volatile
as $$
  update tenants
     set trial_concierge_used = coalesce(trial_concierge_used, 0) + 1
   where id = p_tenant_id
  returning trial_concierge_used;
$$;

-- Morning debrief (docs/modules/module-morning-debrief.md, agreed 2026-08-02)
-- ──────────────────────────────────────────────────────────────
-- The scheduled "what does today need from me?" brief: zero-LLM, composed by
-- src/app/jobs/morning_debrief.py on a 15-min sweep, delivered as one push + one
-- concierge feed row. debrief_time is HH:MM in diary time (Europe/London);
-- debrief_on_closed_days overrides the working_days/blackouts skip (D6);
-- debrief_last_sent_on is the once-per-day dedupe stamp (stamped BEFORE
-- sending — a crash costs one brief, never a double-push).
alter table tenant_config add column if not exists debrief_enabled        boolean not null default true;
alter table tenant_config add column if not exists debrief_time           text    not null default '07:30';
alter table tenant_config add column if not exists debrief_on_closed_days boolean not null default false;
alter table tenant_config add column if not exists debrief_last_sent_on   date;

-- Notification control (Kris 2026-09-02: "total control over every type")
-- ──────────────────────────────────────────────────────────────
-- Before this, push.notify() pushed all twenty kinds to every subscribed
-- device and the only switch in the app was debrief_enabled above — one kind
-- out of twenty. The kinds group into five families a person has an opinion
-- about (src/app/notify_prefs.py holds the map); each takes 'now', 'brief' or
-- 'never'.
--   now   — push it, unless quiet hours are running
--   brief — no push; the morning brief mentions it
--   never — no push, no mention
-- The concierge_notifications FEED row is written in every case: the feed is
-- the record, and a customer accepting a quote is a fact about the business,
-- not a message you can decline.
--
-- quiet_start/quiet_end are HH:MM in diary time and wrap midnight (22:00 to
-- 07:00 is ONE window). Both null = no quiet hours. Anything held overnight
-- rides into the morning brief rather than being dropped.
alter table tenant_config add column if not exists notify_drafts    text not null default 'now';
alter table tenant_config add column if not exists notify_bookings  text not null default 'now';
alter table tenant_config add column if not exists notify_questions text not null default 'now';
alter table tenant_config add column if not exists notify_quotes    text not null default 'now';
alter table tenant_config add column if not exists notify_invoices  text not null default 'now';
alter table tenant_config add column if not exists quiet_start      text;
alter table tenant_config add column if not exists quiet_end        text;

-- Email as a second channel (Kris 2026-09-02: "Add the email channel next")
-- ──────────────────────────────────────────────────────────────
-- Push only reaches devices that installed the PWA and granted permission, so
-- an operator who lives in their inbox got nothing but a bubble they had to go
-- looking for. notify_email is where the same notifications land as mail; blank
-- means the channel is off.
--
-- It obeys the SAME family settings as push — "in the brief" and quiet hours
-- silence both, because they are both interruptions.
--
-- The mail is sent from the tenant's own connected mailbox (there is no
-- transactional sender in the product), which is why it must never be
-- addressed to the mailbox BLAM8 is monitoring: that would drop a notification
-- into the inbox the agent reads and invite it to draft a reply. The send site
-- refuses that address outright.
alter table tenant_config add column if not exists notify_email text;

-- The sixth family: Problems (Kris 2026-09-04)
-- ──────────────────────────────────────────────────────────────
-- Every other family is about the world — a customer did something. This one
-- is about the product: your mailbox has stopped being readable, your calendar
-- has stopped syncing, you have used up your emails (src/app/health.py).
--
-- It is the only family with no 'never'. An operator who switches off "your
-- mailbox has stopped" is buying a week of silent downtime they will find out
-- about from a customer. 'brief' stays available — overnight, tomorrow morning
-- is soon enough, and that is a preference rather than an off switch — and
-- notify_prefs.mode() resolves anything else to 'now', so a hand-made POST
-- cannot do what the form refuses to.
alter table tenant_config add column if not exists notify_problems text not null default 'now';

-- "In the brief" stops being a black hole (Kris 2026-09-04)
-- ──────────────────────────────────────────────────────────────
-- The brief picked its held notifications with a rolling `now - 24h` window,
-- and only on a morning it actually composed one. Three ways a notification
-- promised a mention never got one:
--   * a CLOSED day — the sweep stamps and skips before it gathers, so a Friday
--     evening held item was already outside Monday's window;
--   * the brief switched OFF — "in the brief" quietly meant "never";
--   * any gap over 24h between two briefs.
-- So the window goes. briefed_at stamps the rows a brief actually MENTIONED,
-- and the brief reads what is still unstamped: a held notification now waits
-- for the next real brief however many closed days that takes.
alter table concierge_notifications add column if not exists briefed_at timestamptz;
create index if not exists idx_concierge_notifications_unbriefed
  on concierge_notifications (tenant_id, created_at desc)
  where briefed_at is null;

-- Today is fed by notifications (Kris 2026-09-04)
-- ──────────────────────────────────────────────────────────────
-- Today's deck answered "who needs you?" out of pure state — drafts waiting,
-- gone quiet, overdue — with no time axis and no record of what had actually
-- HAPPENED. The feed had that all along and only the concierge bubble showed
-- it, twenty rows deep, where one glance marked the lot seen.
--
-- So the deck reads the feed. For a notification to land on the right person's
-- card it has to say who it is about: contact is the identity the deck keys on
-- (lower-cased email, the same key the inbox and CRM use), contact_name the
-- label to show when nothing else on the card knows them. Both nullable —
-- plenty of notifications are about the business, not a person, and those
-- simply do not join the deck.
--
-- dismissed_at is the swipe. Distinct from seen_at on purpose: seen means the
-- glow is off, dismissed means "I have dealt with this, take it off my Today".
alter table concierge_notifications add column if not exists contact      text;
alter table concierge_notifications add column if not exists contact_name text;
alter table concierge_notifications add column if not exists dismissed_at timestamptz;
create index if not exists idx_concierge_notifications_live
  on concierge_notifications (tenant_id, created_at desc)
  where dismissed_at is null;

-- The operator orders their own Today (Kris 2026-09-04)
-- ──────────────────────────────────────────────────────────────
-- "Maybe there should be settings for users to pick what they deem most
-- important?" A plasterer chasing money and a clinic chasing no-shows do not
-- agree about what belongs at the top of the morning, and neither is wrong.
--
-- {topic: 'first'|'normal'|'last'} over src/app/deck.py's eight topics — the same
-- three-way shape the notification families use one screen away. Empty is the
-- default order, which is the order the deck already shipped with, so applying
-- this changes nothing for anybody until they open Settings. A junk value
-- reads as 'normal': a broken setting must leave the deck in its default
-- order, never empty it or invert it.
alter table tenant_config add column if not exists deck_priority jsonb not null default '{}'::jsonb;

-- Concierge cost controls (docs/modules/module-concierge-cost-controls.md, 2026-08-02)
-- ──────────────────────────────────────────────────────────────
-- Part B: per-plan DAILY fair-use allowance for concierge business chat —
-- always-on (protects paid margins, unlike the gate-flagged trial pots). The
-- RPC atomically rolls the counter over when the stored date isn't p_today
-- (no sweep needed) and returns the new total; the cap comparison lives in
-- src/app/fairuse.py, day boundary Europe/London. Part A (prompt caching) is
-- request-shaping only — no schema.
alter table tenants add column if not exists concierge_daily_used int not null default 0;
alter table tenants add column if not exists concierge_daily_date date;

create or replace function increment_concierge_daily (p_tenant_id uuid, p_today date)
returns integer
language sql volatile
as $$
  update tenants
     set concierge_daily_used = case
           when concierge_daily_date is distinct from p_today then 1
           else coalesce(concierge_daily_used, 0) + 1
         end,
         concierge_daily_date = p_today
   where id = p_tenant_id
  returning concierge_daily_used;
$$;

-- Concierge live connection allowance (2026-10-14). The rollout and tier caps
-- are operator-managed; interval reservations/settlement are per tenant/user/
-- Europe-London date. An unconfirmed disconnect is charged through its bounded
-- 90-second lease. Audio and transcript content are never stored here.
create table if not exists concierge_live_tier_limits (
  tier text primary key check (tier in ('starter','pro','core','enterprise')),
  minutes integer not null check (minutes between 0 and 1440),
  updated_at timestamptz not null default now(), updated_by uuid
);
insert into concierge_live_tier_limits(tier,minutes) values
 ('starter',10),('pro',10),('core',10),('enterprise',10) on conflict(tier) do nothing;
alter table concierge_live_tier_limits enable row level security;
create table if not exists concierge_live_rollout (
  id boolean primary key default true check(id), enabled boolean not null default false,
  updated_at timestamptz not null default now(), updated_by uuid
);
insert into concierge_live_rollout(id,enabled) values(true,true) on conflict(id) do nothing;
alter table concierge_live_rollout enable row level security;
create table if not exists concierge_live_sessions (
  id uuid primary key, tenant_id uuid not null references tenants(id) on delete cascade,
  user_id uuid not null, london_day date not null, started_at timestamptz not null default now(),
  last_heartbeat timestamptz not null default now(), reserved_until timestamptz not null,
  connected_at timestamptz,
  closed_at timestamptz, charged_seconds integer, created_at timestamptz not null default now(),
  constraint concierge_live_closed_charge check
    ((closed_at is null and charged_seconds is null) or (closed_at is not null and charged_seconds >= 0))
);
create index if not exists concierge_live_sessions_user_day
 on concierge_live_sessions(tenant_id,user_id,london_day);
alter table concierge_live_sessions enable row level security;

create or replace function concierge_live_used_seconds(p_tenant_id uuid,p_user_id uuid,p_day date)
returns integer language sql stable security definer set search_path=public as $$
 select coalesce(sum(case when closed_at is not null then charged_seconds else
   case when connected_at is null then 0 else greatest(0,extract(epoch from
    (least(reserved_until,now())-connected_at))::integer) end end),0)::integer
 from concierge_live_sessions where tenant_id=p_tenant_id and user_id=p_user_id and london_day=p_day;
$$;
create or replace function concierge_live_reserve(p_id uuid,p_tenant_id uuid,p_user_id uuid,
 p_day date,p_limit_seconds integer,p_lease_seconds integer default 90)
returns jsonb language plpgsql volatile security definer set search_path=public as $$
declare used integer; lease integer := greatest(15,least(120,p_lease_seconds)); lease_end timestamptz;
begin
 perform pg_advisory_xact_lock(hashtextextended(p_tenant_id::text||':'||p_user_id::text||':'||p_day::text,0));
 if exists(select 1 from concierge_live_sessions where id=p_id and tenant_id=p_tenant_id
   and user_id=p_user_id and london_day=p_day and closed_at is null) then
  used := concierge_live_used_seconds(p_tenant_id,p_user_id,p_day);
  return jsonb_build_object('ok',true,'used_seconds',used,'limit_seconds',p_limit_seconds,'lease_seconds',lease);
 end if;
 if exists(select 1 from concierge_live_sessions where id=p_id) then
  used := concierge_live_used_seconds(p_tenant_id,p_user_id,p_day);
  return jsonb_build_object('ok',false,'replayed',true,'used_seconds',used,'limit_seconds',p_limit_seconds);
 end if;
 update concierge_live_sessions set closed_at=reserved_until,
   charged_seconds=case when connected_at is null then 0 else
    greatest(0,extract(epoch from(reserved_until-connected_at))::integer) end
 where tenant_id=p_tenant_id and user_id=p_user_id and london_day=p_day
   and closed_at is null and reserved_until < now();
 used := concierge_live_used_seconds(p_tenant_id,p_user_id,p_day);
 if p_limit_seconds > 0 and used >= p_limit_seconds then
   return jsonb_build_object('ok',false,'used_seconds',used,'limit_seconds',p_limit_seconds);
 end if;
 lease_end := least(now()+make_interval(secs=>lease),((p_day+1)::timestamp at time zone 'Europe/London'));
 insert into concierge_live_sessions(id,tenant_id,user_id,london_day,reserved_until)
  values(p_id,p_tenant_id,p_user_id,p_day,lease_end);
 return jsonb_build_object('ok',true,'used_seconds',used,'limit_seconds',p_limit_seconds,'lease_seconds',lease);
end $$;
create or replace function concierge_live_heartbeat(p_id uuid,p_tenant_id uuid,p_user_id uuid,p_limit_seconds integer,p_lease_seconds integer default 90)
returns jsonb language plpgsql volatile security definer set search_path=public as $$
declare r concierge_live_sessions%rowtype; lease integer := greatest(15,least(120,p_lease_seconds));
 used_other integer; remaining integer; elapsed integer; settled_at timestamptz; day_end timestamptz;
begin
 perform pg_advisory_xact_lock(hashtextextended(p_tenant_id::text||':'||p_user_id::text||':'||
  coalesce((select london_day::text from concierge_live_sessions where id=p_id and tenant_id=p_tenant_id and user_id=p_user_id),''),0));
 select * into r from concierge_live_sessions where id=p_id and tenant_id=p_tenant_id and user_id=p_user_id and closed_at is null for update;
 if not found then return jsonb_build_object('ok',false); end if;
 if r.reserved_until < now() then
  update concierge_live_sessions set closed_at=reserved_until,
   charged_seconds=case when connected_at is null then 0 else greatest(0,extract(epoch from(reserved_until-connected_at))::integer) end where id=p_id;
  return jsonb_build_object('ok',false,'ended',true,'reason','lease_expired');
 end if;
 day_end := ((r.london_day+1)::timestamp at time zone 'Europe/London');
 if (now() at time zone 'Europe/London')::date > r.london_day then
  settled_at := least(day_end,r.reserved_until,now());
  update concierge_live_sessions set closed_at=settled_at,
   charged_seconds=case when connected_at is null then 0 else greatest(0,extract(epoch from(settled_at-connected_at))::integer) end where id=p_id;
  return jsonb_build_object('ok',false,'used_seconds',concierge_live_used_seconds(r.tenant_id,r.user_id,r.london_day),'ended',true,'reason','day_rollover');
 end if;
 select coalesce(sum(case when closed_at is not null then charged_seconds else
  case when connected_at is null then 0 else greatest(0,extract(epoch from
   (least(reserved_until,now())-connected_at))::integer) end end),0)::integer into used_other
 from concierge_live_sessions where tenant_id=p_tenant_id and user_id=p_user_id and london_day=r.london_day and id<>p_id;
 if p_limit_seconds > 0 then
  remaining := greatest(0,p_limit_seconds-used_other);
  elapsed := case when r.connected_at is null then 0 else greatest(0,extract(epoch from(now()-r.connected_at))::integer) end;
  if elapsed >= remaining then
   settled_at := coalesce(r.connected_at,now())+make_interval(secs=>remaining);
   update concierge_live_sessions set closed_at=settled_at,charged_seconds=remaining where id=p_id;
   return jsonb_build_object('ok',false,'used_seconds',p_limit_seconds,'ended',true,'reason','limit_reached');
  end if;
 end if;
 update concierge_live_sessions set last_heartbeat=now(),connected_at=coalesce(connected_at,now()),
   reserved_until=least(now()+make_interval(secs=>lease),day_end)
 where id=p_id and tenant_id=p_tenant_id and user_id=p_user_id and closed_at is null returning * into r;
 if not found then return jsonb_build_object('ok',false); end if;
 return jsonb_build_object('ok',true,'used_seconds',concierge_live_used_seconds(r.tenant_id,r.user_id,r.london_day));
end $$;
create or replace function concierge_live_close(p_id uuid,p_tenant_id uuid,p_user_id uuid)
returns jsonb language plpgsql volatile security definer set search_path=public as $$
declare r concierge_live_sessions%rowtype; stamp timestamptz := now();
begin
 select * into r from concierge_live_sessions where id=p_id and tenant_id=p_tenant_id and user_id=p_user_id for update;
 if not found then return jsonb_build_object('ok',false); end if;
 if r.closed_at is null then
  stamp := least(stamp,r.reserved_until);
  update concierge_live_sessions set closed_at=stamp,
   charged_seconds=case when r.connected_at is null then 0 else
    greatest(0,extract(epoch from(stamp-r.connected_at))::integer) end
  where id=p_id returning * into r;
 end if;
 return jsonb_build_object('ok',true,'charged_seconds',r.charged_seconds,
  'used_seconds',concierge_live_used_seconds(r.tenant_id,r.user_id,r.london_day));
end $$;
create or replace function concierge_live_status(p_tenant_id uuid,p_user_id uuid,p_day date,p_limit_seconds integer)
returns jsonb language sql stable security definer set search_path=public as $$
 select jsonb_build_object('used_seconds',concierge_live_used_seconds(p_tenant_id,p_user_id,p_day),
  'limit_seconds',p_limit_seconds,'remaining_seconds',case when p_limit_seconds=0 then null else
   greatest(0,p_limit_seconds-concierge_live_used_seconds(p_tenant_id,p_user_id,p_day)) end);
$$;
revoke all on function concierge_live_used_seconds(uuid,uuid,date) from public, anon, authenticated;
revoke all on function concierge_live_reserve(uuid,uuid,uuid,date,integer,integer) from public, anon, authenticated;
revoke all on function concierge_live_heartbeat(uuid,uuid,uuid,integer,integer) from public, anon, authenticated;
revoke all on function concierge_live_close(uuid,uuid,uuid) from public, anon, authenticated;
revoke all on function concierge_live_status(uuid,uuid,date,integer) from public, anon, authenticated;
grant execute on function concierge_live_used_seconds(uuid,uuid,date) to service_role;
grant execute on function concierge_live_reserve(uuid,uuid,uuid,date,integer,integer) to service_role;
grant execute on function concierge_live_heartbeat(uuid,uuid,uuid,integer,integer) to service_role;
grant execute on function concierge_live_close(uuid,uuid,uuid) to service_role;
grant execute on function concierge_live_status(uuid,uuid,date,integer) to service_role;

-- Customer Portal banner branding (2026-08-02, Julius)
-- ──────────────────────────────────────────────────────────────
-- A banner image the business uploads (onboarding About-you step, or Settings →
-- Customer Portal) shown across the top of their public page /c/{slug}. The
-- image itself lives in the PUBLIC `portal-branding` Storage bucket
-- (src/app/storage.py — public by design: the banner is the public page's artwork,
-- and a plain public URL beats proxying every portal hit through the app).
-- This column holds the object path; NULL = no banner. Paths are versioned per
-- upload (banner-<token>.<ext>) so a replaced banner busts CDN caches.
alter table tenant_config add column if not exists portal_banner_path text;

-- Post-onboarding guided-settings hand-off (2026-08-02, Julius)
-- ──────────────────────────────────────────────────────────────
-- After the wizard, the concierge widget keeps guiding: a persistent chip strip
-- walks the fresh tenant through Diary → CRM → Invoice settings (src/app/setup_handoff.py
-- decides when it shows). This stamp records an explicit "I'll finish these myself"
-- dismiss so the strip never nags across devices; NULL = never dismissed. The other
-- exit — actually configuring the settings — is derived live from the settings rows.
alter table tenant_config add column if not exists settings_handoff_dismissed_at timestamptz;

-- Voice CRM (founder tool) — vcrm_* (2026-08-03, Kris)
-- ──────────────────────────────────────────────────────────────
-- Kris's personal cold-call debrief tool at /admin/voice-crm (voice-crm-build-spec.md):
-- talk after a call + paste the Google Maps link → Whisper transcript → Claude
-- extraction → reviewed + approved into these tables. Deliberately SEPARATE from
-- the tenant-facing crm_* product tables and deliberately WITHOUT tenant_id —
-- founder tooling, service-role only (RLS on, no policies, like platform_admins).
-- Removal: drop both tables + the /admin/voice-crm route block + templates.
create table if not exists vcrm_prospects (
  id uuid primary key default gen_random_uuid(),
  business_name text not null,
  description text,                -- what the business does
  location text,                   -- town/area shorthand
  address text,
  phone text,
  website text,
  maps_url text,                   -- the pasted Google Maps link (resolved form)
  lat double precision,
  lng double precision,
  contact_name text,               -- person spoken to / decision maker
  status text not null default 'NEW',  -- NEW|CALLED|CALLBACK|INTERESTED|WON|LOST|JUNK
  next_action text,
  next_action_date date,
  notes text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists vcrm_calls (
  id uuid primary key default gen_random_uuid(),
  prospect_id uuid not null references vcrm_prospects(id) on delete cascade,
  called_at timestamptz not null default now(),
  outcome text,                    -- no_answer|gatekeeper|decision_maker|callback_arranged|not_interested|interested
  summary text,                    -- short AI summary of the debrief
  transcript text,                 -- full Whisper transcript (the audio itself is never stored)
  extraction_json jsonb,           -- raw Claude extraction output, for auditability
  created_at timestamptz not null default now()
);

alter table vcrm_prospects enable row level security;
alter table vcrm_calls enable row level security;
-- no policies: service-role only, same as platform_admins
create index if not exists idx_vcrm_calls_prospect on vcrm_calls(prospect_id);
create index if not exists idx_vcrm_prospects_status on vcrm_prospects(status);

-- Voice CRM partner pipeline (2026-10-01, Kris) — vcrm_prospects gets a kind + outreach fields
-- ──────────────────────────────────────────────────────────────
-- The tool was built for cold PHONE calls to customers. Kris's next channel is partners
-- (bookkeepers/accountants who refer clients for the 20% programme), reached by email, forum,
-- event or LinkedIn, so a row now says what it is (kind), who (role), where it came from
-- (channel/source) and has an email. Additive: existing rows stay kind='customer'.
-- Partner statuses (plain text, no CHECK, same as the call statuses):
--   IDENTIFIED | EMAILED | REPLIED | CALL_BOOKED | SIGNED_UP | NOT_NOW
alter table vcrm_prospects add column if not exists kind text not null default 'customer';  -- customer | partner
alter table vcrm_prospects add column if not exists role text;      -- bookkeeper | accountant | plumber ...
alter table vcrm_prospects add column if not exists channel text;   -- email | forum | event | linkedin | phone | other
alter table vcrm_prospects add column if not exists source text;    -- where Kris found them (ICB directory, Taunton network ...)
alter table vcrm_prospects add column if not exists email text;
create index if not exists idx_vcrm_prospects_kind on vcrm_prospects(kind);
-- /vcrm partner fields

-- HRT review-time (2026-08-31) — email_log.review_seconds
-- Visible on-screen seconds a human spent on the draft before hitting send
-- (client-measured dwell, clamped 0..1800 server-side; null = auto send or
-- pre-tracking row). Raw material for the Inbox month faces' HRT average.
alter table email_log add column if not exists review_seconds integer;

-- Searched-for (2026-08-31) — email_log.searched_for
-- The drafting model's own statement of what it needed to look up to answer
-- ('Thursday availability, Starter plan mailboxes'); null = pre-feature row
-- or nothing needed. Shown on the thread stage's decision ghost.
alter table email_log add column if not exists searched_for text;

-- Portal chat transcript (2026-08-31, Kris: "the inbox has no idea of the
-- Portal conversation"). Every IDENTIFIED customer's portal chat turn — both
-- sides — keyed by the same contact email the inbox threads on, so the thread
-- can interleave the portal conversation and the drafter can replay it as
-- context. Anonymous turns are never stored; reference codes are masked out
-- before insert (same rule as the chat wire and the CRM note).
create table if not exists portal_messages (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null,
  contact text not null,           -- the customer's email, inbox thread key
  role text not null,              -- 'customer' | 'assistant'
  content text not null,
  created_at timestamptz not null default now()
);
alter table portal_messages enable row level security;
-- no policies: service-role only, same as email_log
create index if not exists idx_portal_messages_thread
  on portal_messages (tenant_id, contact, created_at);

-- ─────────────────────────────────────────────────────────────────────────────
-- Email top-up credit (Kris 2026-09-01)
-- Prepaid emails, bought one-off, spent ONLY once the monthly plan allowance is
-- exhausted, and carried over until used -- "one-off" means the purchase does
-- not recur, not that the credit evaporates at the month boundary. Denominated
-- in EMAILS, not money: the tenant bought a quantity of work, so a later change
-- to the per-email rate can never retroactively shrink what they already paid
-- for.
--
-- tenants.email_credits is the live balance (read on every quota check, so it
-- has to be a column, not a sum over the ledger); email_credit_ledger is the
-- audit trail that explains it. Both are written by the two functions below and
-- nowhere else, so they cannot drift apart.
alter table tenants add column if not exists email_credits integer not null default 0;

create table if not exists email_credit_ledger (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null,
  delta integer not null,            -- +bought / -spent, in emails
  reason text not null,              -- 'topup' | 'spend' | 'refund' | 'adjustment'
  balance_after integer not null,    -- the balance this row produced
  stripe_payment_intent text,        -- set on 'topup' -- the idempotency key
  email_log_id uuid,                 -- set on 'spend' -- what consumed it
  created_at timestamptz not null default now()
);
alter table email_credit_ledger enable row level security;
-- no policies: service-role only, same as email_log

-- One ledger row per Stripe payment, enforced by the database rather than by
-- remembering to check. Stripe retries webhooks; a double-delivered
-- checkout.session.completed must not credit twice.
create unique index if not exists email_credit_ledger_payment_key
  on email_credit_ledger (stripe_payment_intent)
  where stripe_payment_intent is not null;
create index if not exists idx_email_credit_ledger_tenant
  on email_credit_ledger (tenant_id, created_at desc);

-- Grant credit for a paid top-up. Idempotent on the payment intent: a repeated
-- webhook takes the ON CONFLICT path, credits nothing, and returns the balance
-- unchanged. Returns the balance after the call either way.
create or replace function grant_email_credit (
  p_tenant_id uuid,
  p_emails    integer,
  p_payment   text
)
returns integer
language plpgsql volatile
as $$
declare
  v_balance integer;
begin
  -- Claim the payment first. If this payment already credited, do nothing and
  -- report the balance as it stands.
  insert into email_credit_ledger (tenant_id, delta, reason, balance_after, stripe_payment_intent)
  values (p_tenant_id, p_emails, 'topup', 0, p_payment)
  -- The arbiter is the PARTIAL index above, so its predicate has to be repeated
  -- here or Postgres refuses to match it.
  on conflict (stripe_payment_intent) where stripe_payment_intent is not null
  do nothing;

  if not found then
    select coalesce(email_credits, 0) into v_balance from tenants where id = p_tenant_id;
    return coalesce(v_balance, 0);
  end if;

  update tenants
     set email_credits = coalesce(email_credits, 0) + p_emails
   where id = p_tenant_id
  returning email_credits into v_balance;

  update email_credit_ledger
     set balance_after = v_balance
   where stripe_payment_intent = p_payment;

  return coalesce(v_balance, 0);
end;
$$;

-- Spend ONE email of credit, atomically. Returns the balance after spending, or
-- -1 when there was nothing to spend -- the caller uses that to decide whether
-- the tenant is genuinely out. The guard is in the WHERE clause, so two
-- concurrent drafts can never take the same last credit.
create or replace function spend_email_credit (
  p_tenant_id    uuid,
  p_email_log_id uuid default null
)
returns integer
language plpgsql volatile
as $$
declare
  v_balance integer;
begin
  update tenants
     set email_credits = email_credits - 1
   where id = p_tenant_id and coalesce(email_credits, 0) > 0
  returning email_credits into v_balance;

  if v_balance is null then
    return -1;
  end if;

  insert into email_credit_ledger (tenant_id, delta, reason, balance_after, email_log_id)
  values (p_tenant_id, -1, 'spend', v_balance, p_email_log_id);

  return v_balance;
end;
$$;

-- Voice learning: dismissed as an onboarding step (Kris 2026-09-01)
-- Voice is one of onboarding.STEPS, and a tenant can leave it undone — not
-- enough sent mail to learn from, a run that failed, or simply never started.
-- Settings surfaces that as a pending onboarding step rather than letting it
-- sit there looking optional. This column is the operator saying "I know, leave
-- me alone": set it and the step stops asking, without pretending it was done.
-- Deliberately separate from voice_learning_status, which records what the
-- LEARNER did; this records what the PERSON decided.
alter table tenant_config add column if not exists voice_learning_ignored boolean not null default false;

-- Go-live hold: drafting is OFF until the owner says go (Kris 2026-09-01)
-- Onboarding finishes and the agent immediately starts drafting replies to
-- everything that arrives — before the owner has had any chance to walk their
-- loaded history and mark the senders they never want answered. Tokens get
-- spent on newsletters, suppliers and no-reply robots that were always going to
-- be ignored, and the first impression of the product is a pile of drafts
-- nobody wanted.
--
-- So a new tenant lands HELD: mail is received, threaded and filed as normal and
-- shows up in the inbox, but nothing is drafted. The owner triages, ignores whom
-- they like, and then releases drafting themselves — the last step of
-- onboarding, and the first rung of the sending ladder (held → drafts for
-- review → full-auto).
--
-- DEFAULT TRUE so any tenant_config row created from here on starts held, and
-- backfilled FALSE immediately below so no EXISTING tenant's agent goes quiet on
-- deploy. That pairing is the whole point: new behaviour for new tenants only.
-- The backfill that pairs with this lives in
-- scripts/db/migrations/apply_draft_hold_migration.py, NOT here: it must run exactly once, on
-- the deploy that introduces the column. Re-running it later would release a
-- tenant who is legitimately still held, and tenant_config has no created_at to
-- tell the two apart -- so the script checks whether the column already existed
-- and only backfills when it did not.
alter table tenant_config add column if not exists draft_hold boolean not null default true;

-- Mood belongs to the thread it was read from (Kris 2026-09-01)
-- crm_contacts.sentiment is a rolling read on the PERSON, which is right for
-- the CRM — "this customer has been getting angrier" is the thing a single
-- email cannot say. It is wrong for the Inbox, where it tinted every future
-- message that person sent: a refund row's temper washing red over a cheerful
-- boiler enquiry sent hours later.
--
-- So the read records WHICH conversation produced it. The Inbox shows a mood
-- only when the row it is colouring is that same thread; the CRM keeps using
-- the rolling read exactly as before, untouched.
--
-- NULL means "read before this column existed, thread unknown" and deliberately
-- shows NO mood: a read we cannot place is one we cannot honestly attribute,
-- and the next inbound from that contact re-stamps it anyway.
alter table crm_contacts add column if not exists sentiment_thread_id text;

-- ── Dependants (docs/modules/module-requirement-groups.md) ──

-- The definition: what a "Horse" is, for THIS tenant. Labels are the
-- operator's words; the mechanics (ask, remember, prefill, gate) are fixed.
CREATE TABLE IF NOT EXISTS requirement_groups (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id      uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  label          text NOT NULL,                      -- 'Horse'
  label_plural   text NOT NULL DEFAULT '',            -- 'Horses'
  cardinality    text NOT NULL DEFAULT 'many',        -- 'one' | 'many' per contact
  fields         jsonb NOT NULL DEFAULT '[]'::jsonb,  -- [{key,label,type,options,required,hint,durability,gate}]
  display_order  int NOT NULL DEFAULT 0,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_requirement_groups_tenant
  ON requirement_groups (tenant_id, display_order);

-- A filled copy: "Murphy", on Kris's record.
CREATE TABLE IF NOT EXISTS requirement_instances (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  group_id    uuid NOT NULL REFERENCES requirement_groups(id) ON DELETE CASCADE,
  contact_id  uuid NOT NULL REFERENCES crm_contacts(id) ON DELETE CASCADE,
  name        text NOT NULL,
  data        jsonb NOT NULL DEFAULT '{}'::jsonb,   -- {key: value}
  field_meta  jsonb NOT NULL DEFAULT '{}'::jsonb,   -- {key: {source, at, email_log_id}}
  archived_at timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_requirement_instances_unique_name
  ON requirement_instances (tenant_id, group_id, contact_id, lower(name));
CREATE INDEX IF NOT EXISTS idx_requirement_instances_contact
  ON requirement_instances (tenant_id, contact_id);

-- Which instance(s) a booking is about.
CREATE TABLE IF NOT EXISTS booking_subjects (
  booking_id   uuid NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
  instance_id  uuid NOT NULL REFERENCES requirement_instances(id) ON DELETE CASCADE,
  tenant_id    uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  created_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (booking_id, instance_id)
);
CREATE INDEX IF NOT EXISTS idx_booking_subjects_instance
  ON booking_subjects (tenant_id, instance_id);

ALTER TABLE requirement_groups ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "tenant isolation - requirement_groups" ON requirement_groups;
CREATE POLICY "tenant isolation - requirement_groups" ON requirement_groups
  FOR ALL USING (tenant_id IN (SELECT auth_tenant_ids()))
  WITH CHECK (tenant_id IN (SELECT auth_tenant_ids()));

ALTER TABLE requirement_instances ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "tenant isolation - requirement_instances" ON requirement_instances;
CREATE POLICY "tenant isolation - requirement_instances" ON requirement_instances
  FOR ALL USING (tenant_id IN (SELECT auth_tenant_ids()))
  WITH CHECK (tenant_id IN (SELECT auth_tenant_ids()));

ALTER TABLE booking_subjects ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "tenant isolation - booking_subjects" ON booking_subjects;
CREATE POLICY "tenant isolation - booking_subjects" ON booking_subjects
  FOR ALL USING (tenant_id IN (SELECT auth_tenant_ids()))
  WITH CHECK (tenant_id IN (SELECT auth_tenant_ids()));

-- Onboarding clarity (2026-09-13): the website address given at the interview
-- step, read automatically at the Knowledge step (scripts/db/migrations/apply_onboarding_website_migration.py).
alter table tenant_config add column if not exists website text;

-- Held for later (2026-09-13, src/app/setup_held.py): facts the owner gives at one
-- setup step that belong to another page — [{page, name, value, step, at, used_at?}]
-- (scripts/db/migrations/apply_setup_held_migration.py).
alter table tenant_config add column if not exists setup_held jsonb not null default '[]'::jsonb;

-- First walks (2026-09-13, src/app/static/tour.js): which pages a tenant has been
-- shown around once setup was done — {"today": "<iso>"} — so a walk shows once
-- per account, not per browser (scripts/db/migrations/apply_tours_migration.py).
alter table tenant_config add column if not exists tours jsonb not null default '{}'::jsonb;

-- Generic ignoring by kind (2026-09-13, src/app/inbox_rules.py): which kinds of
-- automated mail the agent files as Automated and never answers —
-- {"noreply": true, "bulk": true, "auto": true}; absent = on
-- (scripts/db/migrations/apply_inbox_ignore_migration.py).
alter table tenant_config add column if not exists inbox_ignore jsonb not null default '{}'::jsonb;

-- The front desk rule (Kris 2026-09-13): may an appointment overlap a
-- drop-off or a collection on a stays diary? {"enabled": bool, "minutes": int}
-- (app/diary_config.clean_front_desk). Applied to prod by
-- scripts/db/migrations/apply_front_desk_migration.py.
alter table scheduling_config add column if not exists front_desk jsonb not null default '{}'::jsonb;

-- People — partners and staff on one BLAM8 (docs/plan-people.md, Kris
-- 2026-09-13). tenant_members was built as the RLS mapping and nothing more;
-- a person needs a role that means something, the doors they may open, and a
-- way to stop being here without being deleted out of the history. Applied to
-- prod by scripts/db/migrations/apply_people_migration.py.
alter table tenant_members add column if not exists scopes       jsonb not null default '{}'::jsonb;
alter table tenant_members add column if not exists display_name text;
alter table tenant_members add column if not exists email        text;
alter table tenant_members add column if not exists active       boolean not null default true;
alter table tenant_members add column if not exists last_seen_at timestamptz;
-- role is now 'owner' | 'manager' | 'staff' | 'accountant'; every pre-People
-- row carries the legacy 'member', which app/people.normalise_role reads as a
-- manager (it saw everything, and an upgrade must not take that away).

-- One unaccepted invitation. The token is the /join/{token} link and the QR.
-- Service role only (RLS on, no policies): a token is a credential and never
-- reaches a browser that isn't following the link.
create table if not exists member_invites (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id) on delete cascade,
  token         text not null unique,
  email         text,
  display_name  text,
  role          text not null default 'staff',
  scopes        jsonb not null default '{}'::jsonb,
  created_by    uuid,
  created_at    timestamptz not null default now(),
  expires_at    timestamptz not null default (now() + interval '7 days'),
  accepted_at   timestamptz,
  accepted_by   uuid,
  revoked_at    timestamptz
);
create index if not exists member_invites_tenant_idx on member_invites (tenant_id);
alter table member_invites enable row level security;
-- "Give them a login" (docs/plan-people-team-and-jobs.md step 1): the team
-- person this invitation is for, so accepting joins the login to their row.
alter table member_invites add column if not exists team_person_id uuid;

-- team_people (docs/plan-people-team-and-jobs.md step 1) — everyone who works
-- here, with or without a login. user_id set = they sign in (their doors stay
-- on tenant_members; this row mirrors name/role/kinds). engineer_id links the
-- Field Service roster row while the FSM code still reads engineers. A row
-- with no user_id grants no access. Service role only. Applied to prod (with
-- the back-fill) by scripts/db/migrations/apply_team_people_migration.py.
create table if not exists team_people (
  id             uuid primary key default gen_random_uuid(),
  tenant_id      uuid not null references tenants(id) on delete cascade,
  name           text not null,
  role           text not null default 'staff',
  user_id        uuid,
  engineer_id    uuid references engineers(id) on delete set null,
  email          text,
  phone          text,
  does           text[] not null default '{}',
  skills         text[] not null default '{}',
  daily_minutes  int,
  active         boolean not null default true,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now()
);
create index if not exists team_people_tenant_idx on team_people (tenant_id);
create unique index if not exists team_people_user_uidx
  on team_people (tenant_id, user_id) where user_id is not null;
create unique index if not exists team_people_engineer_uidx
  on team_people (tenant_id, engineer_id) where engineer_id is not null;
alter table team_people enable row level security;

-- booking_people (docs/plan-people-team-and-jobs.md step 2) — who's on the
-- job. person_id is the team_people row (with or without a login); part is the
-- business's own word for their share ("drop-off", "collection"), optional.
-- scheduling_config.people_per_job: 'one' (putting someone on replaces whoever
-- was on it) or 'several'. Applied by scripts/db/migrations/apply_booking_people_migration.py.
create table if not exists booking_people (
  id          uuid primary key default gen_random_uuid(),
  tenant_id   uuid not null references tenants(id) on delete cascade,
  booking_id  uuid not null references bookings(id) on delete cascade,
  person_id   uuid not null references team_people(id) on delete cascade,
  part        text,
  added_by    uuid,
  created_at  timestamptz not null default now(),
  unique (booking_id, person_id)
);
create index if not exists booking_people_tenant_idx on booking_people (tenant_id);
create index if not exists booking_people_person_idx on booking_people (person_id);
alter table booking_people enable row level security;
alter table scheduling_config add column if not exists people_per_job text not null default 'one';

-- job_sheets (src/app/job_sheets.py; Kris 2026-09-25) — a person on a job who
-- doesn't sign in is sent the job: a text or email carrying a private link
-- (/j/{token}) with On my way and Done. The row outlives their coming off the
-- job so they can be told; closed_at ends the link. Applied by
-- scripts/db/migrations/apply_job_sheets_migration.py.
create table if not exists job_sheets (
  id             uuid primary key default gen_random_uuid(),
  tenant_id      uuid not null references tenants(id) on delete cascade,
  booking_id     uuid not null references bookings(id) on delete cascade,
  person_id      uuid not null references team_people(id) on delete cascade,
  token          text not null unique,
  channel        text,
  sent_to        text,
  sent_at        timestamptz,
  facts_hash     text,
  last_notice    text,
  last_notice_at timestamptz,
  opened_at      timestamptz,
  on_my_way_at   timestamptz,
  done_at        timestamptz,
  closed_at      timestamptz,
  created_at     timestamptz not null default now(),
  unique (booking_id, person_id)
);
create index if not exists job_sheets_tenant_idx on job_sheets (tenant_id);
create index if not exists job_sheets_open_idx on job_sheets (closed_at) where closed_at is null;
alter table job_sheets enable row level security;

-- staff_notes (docs/plan-people.md slice 3) — telling a person something,
-- fastened to the work it is about. The thing a WhatsApp group cannot do: the
-- message is attached to the booking, customer or thread it concerns, it lands
-- on that person's Today, and whether they saw it is kept. Names ride beside
-- the user ids so the record still reads after somebody leaves (People
-- deactivates, it never deletes). Applied to prod by
-- scripts/db/migrations/apply_staff_notes_migration.py.
create table if not exists staff_notes (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id) on delete cascade,
  from_user     uuid,
  from_name     text,
  to_user       uuid,
  to_name       text,
  body          text not null,
  about_kind    text,                 -- booking | contact | thread
  about_id      text,
  about_label   text,
  about_href    text,
  created_at    timestamptz not null default now(),
  seen_at       timestamptz,
  done_at       timestamptz,
  cancelled_at  timestamptz
);
create index if not exists staff_notes_to_idx on staff_notes (tenant_id, to_user, seen_at);
create index if not exists staff_notes_about_idx on staff_notes (tenant_id, about_kind, about_id);
alter table staff_notes enable row level security;

-- ──────────────────────────────────────────────────────────────
-- A person's own calendar (People slice 5 — docs/plan-people.md)
-- ──────────────────────────────────────────────────────────────
-- The diary already syncs to ONE business calendar (the oldest mailbox with
-- calendar_connected_at). This is the second half Kris asked for after watching
-- his wife accept an invitation: "she can see the business Calendar in BLAM8
-- while having her own Calendar to have events synced to… mostly used to
-- delegate work from the main calendar."
--
-- One row per person who has connected theirs. Only the WORK DELEGATED TO THEM
-- by kind is written there (people.does_booking) — an empty delegation pushes
-- nothing, because a calendar is somebody's own property and filling it by
-- default is worse than filling it by omission.
--
-- The grant asks for calendar events ONLY (no mail scope). That is a SENSITIVE
-- scope rather than a restricted one, but it still draws on the same 100
-- pre-CASA grants — Google's unverified-app cap counts users who authorise an
-- unapproved sensitive *or* restricted scope — so src/app/slots.py meters this
-- table alongside email_connections. Tokens are Fernet-encrypted by
-- src/app/crypto.py exactly as the mailbox's are.
create table if not exists member_calendars (
  tenant_id       uuid not null references tenants(id) on delete cascade,
  user_id         uuid not null,
  provider        text not null default 'google',
  access_token    text,
  refresh_token   text,
  token_expires   timestamptz,
  calendar_email  text,               -- which account they consented with
  connected_at    timestamptz not null default now(),
  last_error      text,               -- last push failure, shown on their page
  primary key (tenant_id, user_id)
);
alter table member_calendars enable row level security;

-- The event id THIS person's calendar got for a booking. Kept apart from
-- bookings.calendar_event_id (the business calendar's own event) so removing
-- one never touches the other, and so a person disconnecting takes their events
-- with them and leaves the business diary alone.
create table if not exists member_booking_events (
  tenant_id   uuid not null references tenants(id) on delete cascade,
  user_id     uuid not null,
  booking_id  uuid not null,
  event_id    text not null,
  created_at  timestamptz not null default now(),
  primary key (tenant_id, user_id, booking_id)
);
create index if not exists member_booking_events_booking_idx
  on member_booking_events (tenant_id, booking_id);
alter table member_booking_events enable row level security;

-- Whose calendar a consent round-trip is for (mode='mycal'). Bound server-side
-- next to the tenant, so the callback never trusts a user id from the URL.
alter table oauth_states add column if not exists user_id uuid;

-- ──────────────────────────────────────────────────────────────
-- Grounded booking (docs/plan-grounded-booking.md, 2026-09-22): the model may
-- only place a hold/offer for a date it can point to — this email, an earlier
-- email from the customer, a sent reply, a live booking, or a date the diary
-- just returned free — and never a hold without a check this draft. Enforced
-- in scheduling.make_executor; the prompt only explains it. Default OFF: an
-- opted-out tenant's executor is byte-identical to before. On for the rig
-- first, then LittleDogs, then the default flips.
alter table scheduling_config add column if not exists grounded_dates boolean not null default false;

-- Draft claims (docs/plan-grounded-booking.md, 2026-09-22): one draft per
-- inbound message, across processes. `pipeline._drafts_in_flight` is an
-- in-memory set whose comment reasons it is atomic because every caller shares
-- one event loop — true inside one process, and a duplicate webhook delivery
-- lands in a different one. Both then draft: double the tokens, two trails of
-- which only the winner's is stored, and on 2026-09-22 a loser that inserted
-- first with a placeholder reply. The primary key is the lock: insert to
-- claim, walk away on conflict, delete when the draft finishes. `claimed_at`
-- exists so a process that died mid-draft cannot strand a message forever —
-- a claim older than the grace period is taken over.
create table if not exists draft_claims (
  tenant_id  uuid        not null references tenants(id) on delete cascade,
  message_id text        not null,
  claimed_at timestamptz not null default now(),
  primary key (tenant_id, message_id)
);
create index if not exists draft_claims_claimed_at_idx on draft_claims (claimed_at);
alter table draft_claims enable row level security;
drop policy if exists "tenant isolation - draft_claims" on draft_claims;
create policy "tenant isolation - draft_claims" on draft_claims
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));

-- ──────────────────────────────────────────────────────────────
-- Sent time (2026-09-23) — email_log.sent_at: when the reply actually went,
-- stamped by db.mark_email_log_sent. The thread's sent bubble shows it (time
-- today, date + time after — Kris). created_at is when the email came in, so
-- it can't stand in; rows sent before this column show no time.
alter table email_log add column if not exists sent_at timestamptz;

-- Spoken time (2026-09-24) — email_log.spoken_seconds: how long the owner
-- spoke asking the voice Concierge for this draft. Human time that
-- src/app/time_saved.py takes off the email's saving; null = not asked by voice.
alter table email_log add column if not exists spoken_seconds integer;

-- bank_holidays (src/app/bank_holidays.py; D1, docs/plan-service-urgency.md) — the
-- gov.uk list per nation (england-and-wales / scotland / northern-ireland),
-- refreshed daily. A business picks its nation in Diary settings; its bank
-- holidays close normal work and open an out-of-hours service at its price.
-- Applied by scripts/db/migrations/apply_bank_holidays_migration.py.
create table if not exists bank_holidays (
  nation  text not null,
  day     date not null,
  title   text not null,
  primary key (nation, day)
);
alter table bank_holidays enable row level security;
alter table scheduling_config add column if not exists bank_holiday_nation text;
alter table scheduling_config add column if not exists bank_holidays_open date[] not null default '{}';

-- After-job tasks (docs/plan-after-job-tasks.md, 2026-09-26): a job done is a
-- STAMP (bookings.done_at), never a status; its service's tasks land in
-- crm_tasks (source 'after_job'), one per booking + task.
alter table bookings add column if not exists done_at timestamptz;
alter table bookings add column if not exists done_by text;
alter table scheduling_config add column if not exists job_done_when text not null default 'marked';
alter table crm_tasks add column if not exists booking_id uuid references bookings(id) on delete cascade;
alter table crm_tasks add column if not exists task_key text;
alter table crm_tasks add column if not exists evidence_doc_id uuid references crm_artifacts(id) on delete set null;
alter table crm_tasks add column if not exists reminded text;   -- null | soon | overdue (Today sent)
create unique index if not exists crm_tasks_booking_task_key on crm_tasks (booking_id, task_key) where booking_id is not null and task_key is not null;
-- The owner's "Always tell me when an email mentions" words (agnostic: their own list).
alter table tenant_config add column if not exists alert_words text;

-- ── Operator audit trail (security review 2026-09, M16/M17) ────────────────
-- Every /admin change and every email-body view (src/app/admin_auth.py _audit).
-- Service role only: RLS on, no policies.
create table if not exists admin_audit (
  id uuid primary key default gen_random_uuid(),
  at timestamptz not null default now(),
  admin_user_id uuid not null,
  admin_email text,
  method text not null,
  path text not null,
  tenant_id uuid,
  detail jsonb not null default '{}'::jsonb
);
create index if not exists idx_admin_audit_at on admin_audit (at desc);
create index if not exists idx_admin_audit_tenant on admin_audit (tenant_id, at desc);
alter table admin_audit enable row level security;

-- portal_messages gets its tenant key (M15) — scripts/db/migrations/apply_closure_orphans_migration.py
-- adds it after clearing orphan rows:
--   alter table portal_messages add constraint portal_messages_tenant_fk
--     foreign key (tenant_id) references tenants(id) on delete cascade;

-- Booking reliability foundation (2026-09-27); standalone migration in db/migrations.
-- Apply before deploying the matching application changes. No provider calls.
-- All office booking writers share one tenant lock, including multi-day spans,
-- moves, direct/manual inserts, and expired offers becoming proposed bookings.
-- Existing over-capacity rows are not modified by this migration.
begin;

-- Persist the existing stay toggle once; capacity checks never inspect labels.
alter table scheduling_config add column if not exists booking_mode text;
update scheduling_config c set booking_mode = case when exists (
  select 1 from jsonb_array_elements(coalesce(c.intake_fields, '[]')) f
  where trim(regexp_replace(lower(f->>'label'), '[^a-z0-9]+', ' ', 'g')) =
        'departure collection date time') then 'stay' else 'appointment' end
where booking_mode is null;
alter table scheduling_config alter column booking_mode set default 'appointment';
alter table scheduling_config alter column booking_mode set not null;

create or replace function booking_span_bounds(b bookings, c scheduling_config)
returns int8range language plpgsql stable set search_path = public as $$
declare
  base bigint := (b.booking_date - date '2000-01-01')::bigint * 96;
  lo integer;
  hi integer;
begin
  if b.start_block is not null and b.length_blocks is not null then
    if b.length_blocks <= 0 then
      raise exception 'BLAM8_CAPACITY_CONFLICT: invalid reservation length';
    end if;
    return int8range(base + b.start_block, base + b.start_block + b.length_blocks, '[)');
  end if;
  lo := floor(extract(epoch from coalesce(c.open_time, time '09:00')) / 900)::int;
  hi := ceil(extract(epoch from coalesce(c.close_time, time '17:00')) / 900)::int;
  if hi <= lo then lo := 0; hi := 96; end if;
  if b."window" = 'am' and lo < 48 then hi := least(hi, 48);
  elsif b."window" = 'pm' and hi > 48 then lo := greatest(lo, 48);
  elsif b."window" in ('am', 'pm') then null;
  elsif b."window" ~ '^\d{2}:\d{2}$' then
    lo := floor(extract(epoch from b."window"::time) / 900)::int;
    hi := lo + ceil(coalesce(c.slot_minutes, 60) / 15.0)::int;
  elsif b."window" is null or b."window" <> 'day' then
    return null;
  end if;
  return int8range(base + lo, base + greatest(lo + 1, hi), '[)');
end $$;

create or replace function enforce_booking_capacity()
returns trigger language plpgsql volatile set search_path = public as $$
declare
  c scheduling_config;
  span int8range;
  cap integer;
  quantity integer := coalesce(new.quantity, 1);
  gap integer;
  peak bigint;
  svc jsonb;
  max_day integer;
  used_day integer;
  stays boolean;
begin
  if tg_op = 'UPDATE' and new.tenant_id <> old.tenant_id then
    raise exception 'A booking cannot change business';
  end if;
  -- Non-reservations and metadata updates must never queue behind a reservation.
  if new.spaces is not null or new.engineer_id is not null or new.source = 'calendar'
     or exists (select 1 from asset_units u where u.tenant_id = new.tenant_id
                and u.id::text = new.resource_type)
     or new.status not in ('offered', 'proposed', 'confirmed')
     or (new.status = 'offered' and new.expires_at <= clock_timestamp()) then
    return new;
  end if;
  if tg_op = 'UPDATE' and
     row(new.booking_date, new."window", new.start_block, new.length_blocks,
         new.quantity, new.resource_type, new.status, new.expires_at,
         new.spaces, new.engineer_id, new.source,
         new.details->>'Service') is not distinct from
     row(old.booking_date, old."window", old.start_block, old.length_blocks,
         old.quantity, old.resource_type, old.status, old.expires_at,
         old.spaces, old.engineer_id, old.source,
         old.details->>'Service') then
    return new;
  end if;
  -- Live status transitions do not acquire capacity again. Expired offers do.
  if tg_op = 'UPDATE' and old.status in ('offered', 'proposed', 'confirmed')
     and (old.status <> 'offered' or old.expires_at is null or old.expires_at > clock_timestamp())
     and row(new.booking_date, new."window", new.start_block, new.length_blocks,
             new.quantity, new.resource_type, new.spaces, new.engineer_id, new.source,
             new.details->>'Service') is not distinct from
         row(old.booking_date, old."window", old.start_block, old.length_blocks,
             old.quantity, old.resource_type, old.spaces, old.engineer_id, old.source,
             old.details->>'Service') then
    return new;
  end if;
  perform pg_advisory_xact_lock(hashtextextended('blam8:office:' || new.tenant_id::text, 0));
  select * into c from scheduling_config where tenant_id = new.tenant_id;
  cap := coalesce(c.capacity_per_window, 4);
  if nullif(new.resource_type, '') is not null then
    select greatest(1, coalesce((r->>'count')::integer, 1)) into cap
    from jsonb_array_elements(coalesce(c.resource_types, '[]')) r
    where lower(r->>'name') = lower(new.resource_type);
    if cap is null then
      -- A new request must choose a configured pool. An existing booking may
      -- retain its old label when that pool is renamed/removed.
      if tg_op <> 'UPDATE' or new.resource_type is distinct from old.resource_type then
        raise exception 'BLAM8_CAPACITY_CONFLICT: unknown resource';
      end if;
      cap := coalesce(c.capacity_per_window, 4);
    end if;
  end if;
  if quantity < 1 or cap < 1 then
    raise exception 'BLAM8_CAPACITY_CONFLICT: invalid capacity';
  end if;
  select s into svc from jsonb_array_elements(coalesce(c.service_types, '[]')) s
  where lower(s->>'name') = lower(new.details->>'Service') limit 1;
  gap := ceil(coalesce(nullif((svc->>'buffer')::integer, 0), c.buffer_minutes, 0) / 15.0)::int;
  max_day := coalesce((svc->>'max_per_day')::integer, 0);
  if max_day > 0 then
    select count(*) into used_day from bookings b
    where b.tenant_id = new.tenant_id and b.id <> new.id
      and b.booking_date = new.booking_date
      and lower(b.details->>'Service') = lower(new.details->>'Service')
      and (b.status in ('proposed', 'confirmed') or
           (b.status = 'offered' and (b.expires_at is null or b.expires_at > clock_timestamp())));
    if used_day >= max_day then
      raise exception 'BLAM8_CAPACITY_CONFLICT: daily service limit';
    end if;
  end if;
  stays := c.booking_mode = 'stay';
  span := booking_span_bounds(new, c);
  if span is null then
    raise exception 'BLAM8_CAPACITY_CONFLICT: unknown reservation window';
  end if;
  -- Sweep interval endpoints, summing quantities rather than counting every
  -- overlapping booking as concurrent. Endpoints are grouped before summing:
  -- a departure and arrival at the same time do not overlap.
  with busy as (
    select booking_span_bounds(b, c) as r, coalesce(b.quantity, 1) as q
    from bookings b
    where b.tenant_id = new.tenant_id and b.id <> new.id
      and b.spaces is null and b.engineer_id is null
      and (b.status in ('proposed', 'confirmed') or
           (b.status = 'offered' and (b.expires_at is null or b.expires_at > clock_timestamp())))
      and (case when nullif(new.resource_type, '') is not null
                then b.resource_type = new.resource_type
                when stays then nullif(b.resource_type, '') is null
                else true end)
  ), clipped as (
    select greatest(lower(span), lower(r) - gap) as a,
           least(upper(span), upper(r) + gap) as z, q
    from busy where lower(r) - gap < upper(span) and upper(r) + gap > lower(span)
  ), events as (
    select a as t, q as delta from clipped
    union all select z, -q from clipped
  ), totals as (
    select t, sum(delta) as delta from events group by t
  ), running as (
    select sum(delta) over (order by t) as used from totals
  ) select case when new.start_block is null or new.length_blocks is null
                then (select count(*) from clipped)
                else coalesce(max(used), 0) end into peak from running;
  if peak + quantity > cap then
    raise exception 'BLAM8_CAPACITY_CONFLICT: no capacity for this reservation';
  end if;
  return new;
end $$;

drop trigger if exists bookings_capacity_guard on bookings;
create trigger bookings_capacity_guard before insert or update on bookings
for each row execute function enforce_booking_capacity();

-- Evidence identifies the exact requirement on its already-linked subject.
-- One possible proof field preserves the existing proof behaviour. Several require classification.
alter table crm_artifacts add column if not exists requirement_keys text[] not null default '{}';

create or replace function classify_single_proof()
returns trigger language plpgsql set search_path = public as $$
declare keys text[];
begin
  if cardinality(new.requirement_keys) = 0 and new.instance_id is not null then
    select array_agg(f->>'key') into keys
    from requirement_instances i
    join requirement_groups g on g.id = i.group_id and g.tenant_id = i.tenant_id
    cross join lateral jsonb_array_elements(g.fields) f
    where i.id = new.instance_id and i.tenant_id = new.tenant_id
      and (new.contact_id is null or new.contact_id = i.contact_id)
      and f->>'proof' = 'true' and nullif(f->>'key', '') is not null;
    if cardinality(keys) = 1 then new.requirement_keys := keys; end if;
  end if;
  return new;
end $$;
drop trigger if exists artifacts_single_proof on crm_artifacts;
create trigger artifacts_single_proof before insert or update of instance_id, requirement_keys
on crm_artifacts for each row execute function classify_single_proof();
update crm_artifacts set requirement_keys = requirement_keys
where cardinality(requirement_keys) = 0 and instance_id is not null;


-- Snapshot the previous shared out-of-hours assumptions into existing tenant
-- data before removing them from prompts. New policies inherit none of these
-- terms. Presence of price_basis makes this step safe to re-run.
update scheduling_config c set service_types = (
  select jsonb_agg(case
    when jsonb_typeof(s->'out_of_hours') = 'object'
         and not (s->'out_of_hours' ? 'price_basis')
    then jsonb_set(s, '{out_of_hours}',
      '{"price_basis":"first_hour","terms":"Parts are extra. The charge applies even if the work cannot be finished during the visit."}'::jsonb
      || (s->'out_of_hours')
      || jsonb_build_object('carried_over_review', case
           when s->'out_of_hours' ? 'terms' then '["price_basis"]'::jsonb
           else '["price_basis","terms"]'::jsonb end))
    else s end order by n)
  from jsonb_array_elements(c.service_types) with ordinality as items(s,n)
)
where jsonb_typeof(c.service_types) = 'array' and jsonb_array_length(c.service_types) > 0;

-- Moves reserve their final span in one write through the same guard.
create or replace function move_booking_if_capacity(
  p_tenant_id uuid, p_booking_id uuid, p_new_date date, p_new_window text)
returns setof bookings language plpgsql volatile set search_path = public as $$
declare b bookings; c scheduling_config; bounds int8range;
begin
  select * into b from bookings where id=p_booking_id and tenant_id=p_tenant_id
    and status in ('proposed','confirmed');
  if not found then return; end if;
  select * into c from scheduling_config where tenant_id=p_tenant_id;
  b.booking_date := p_new_date; b."window" := p_new_window;
  b.start_block := null; b.length_blocks := null;
  bounds := booking_span_bounds(b,c);
  if bounds is null then raise exception 'BLAM8_CAPACITY_CONFLICT: invalid window'; end if;
  return query update bookings set booking_date=p_new_date, "window"=p_new_window,
    start_block=(lower(bounds) - (p_new_date-date '2000-01-01')::bigint*96)::int,
    length_blocks=(upper(bounds)-lower(bounds))::int, updated_at=now()
    where id=p_booking_id and tenant_id=p_tenant_id and status in ('proposed','confirmed')
    returning *;
end $$;

commit;

-- No-defaults booking policy (2026-09-27).
-- Apply after booking_guarantees; never run by application startup.
begin;
-- Private rollback material. It is not accessible through tenant APIs.
create table if not exists booking_settings_rollback (
  key text primary key, value jsonb not null
);
alter table booking_settings_rollback enable row level security;
revoke all on booking_settings_rollback from public;
do $$ declare r text; begin
  foreach r in array array['anon','authenticated','service_role'] loop
    if exists(select 1 from pg_roles where rolname=r) then
      execute format('revoke all on booking_settings_rollback from %I', r);
    end if;
  end loop;
end $$;
-- Stop on reader disagreements: the old slot generator and grid interpret
-- inverted hours differently. NULL standard hours also disagree before this slice.
do $$ begin
  if to_regprocedure('enforce_booking_capacity()') is null then
    raise exception 'Apply booking_guarantees before no_defaults';
  end if;
  if not exists(select 1 from booking_settings_rollback where key='snapshot_complete')
     and exists(select 1 from scheduling_config c where exists(select 1 from subscriptions s where s.tenant_id=c.tenant_id and s.status='active' and s.module in ('scheduling','tms','fsm'))
            and (open_time is null or close_time is null or open_time >= close_time)) then
    raise exception 'BLAM8_SETTINGS_CONFLICT: existing hours disagree between readers; review before migration';
  end if;
end $$;
insert into booking_settings_rollback
select 'row:' || tenant_id, to_jsonb(c) from scheduling_config c
where exists(select 1 from subscriptions s where s.tenant_id=c.tenant_id and s.status='active' and s.module in ('scheduling','tms','fsm'))
  and not exists(select 1 from booking_settings_rollback where key='snapshot_complete')
on conflict do nothing;
insert into booking_settings_rollback
select 'function:' || oid::regprocedure::text, to_jsonb(pg_get_functiondef(oid))
from pg_proc where pronamespace='public'::regnamespace and proname in
 ('booking_span_bounds','enforce_booking_capacity','hold_booking_if_capacity','move_booking_if_capacity')
on conflict do nothing;
-- Snapshot only businesses with an active booking module; other missing rows stay missing.
insert into scheduling_config(tenant_id)
select id from tenants t where exists(select 1 from subscriptions s
  where s.tenant_id=t.id and s.status='active' and s.module in ('scheduling','tms','fsm'))
  and not exists(select 1 from booking_settings_rollback where key='snapshot_complete')
on conflict do nothing;
alter table scheduling_config add column if not exists timezone text;
alter table scheduling_config add column if not exists booking_setting_review jsonb not null default '{}';
alter table scheduling_config add column if not exists booking_revision bigint not null default 0;
update scheduling_config c set
 capacity_per_window=coalesce(c.capacity_per_window,4),
 working_days=coalesce(c.working_days,'mon,tue,wed,thu,fri'),
 open_time=coalesce(c.open_time,time '09:00'), close_time=coalesce(c.close_time,time '17:00'),
 timezone='Europe/London', booking_revision=1,
 booking_setting_review=(select jsonb_object_agg(k, jsonb_build_object(
   'state','set','review_status','carried_over_review','revision',1,
   'snapshot_version','20260927_no_defaults',
   'source',case when not exists(select 1 from booking_settings_rollback where key='row:'||c.tenant_id)
      then 'missing_row_fallback' when k='timezone' then 'engine_timezone'
      when (select value->>(case k when 'capacity' then 'capacity_per_window'
                                  when 'opening_hours' then 'open_time' else k end)
            from booking_settings_rollback where key='row:'||c.tenant_id) is null then 'runtime_fallback'
      else 'stored_value' end))
 from unnest(array['capacity','working_days','opening_hours','timezone']) k)
where exists(select 1 from subscriptions s where s.tenant_id=c.tenant_id and s.status='active' and s.module in ('scheduling','tms','fsm'))
  and booking_revision=0 and not exists(select 1 from booking_settings_rollback where key='snapshot_complete');
insert into booking_settings_rollback values('snapshot_complete','true') on conflict do nothing;
alter table scheduling_config alter column capacity_per_window drop default;
alter table scheduling_config alter column capacity_per_window drop not null;
alter table scheduling_config alter column working_days drop default;
alter table scheduling_config alter column working_days drop not null;
alter table scheduling_config alter column open_time drop default;
alter table scheduling_config alter column open_time drop not null;
alter table scheduling_config alter column close_time drop default;
alter table scheduling_config alter column close_time drop not null;
-- Existing scheduling_config row policies also cover these columns.
-- No SECURITY DEFINER: the authenticated caller must own the row under RLS.
create or replace function apply_booking_settings(p_tenant uuid, p_revision bigint, p_patch jsonb)
returns void language plpgsql security invoker set search_path=public as $$
declare current_row scheduling_config; assignments text;
begin
  perform pg_advisory_xact_lock(hashtextextended('blam8:office:' || p_tenant::text, 0));
  insert into scheduling_config(tenant_id) values(p_tenant) on conflict do nothing;
  select * into current_row from scheduling_config where tenant_id=p_tenant for update;
  if not found or current_row.booking_revision <> p_revision then
    raise exception 'BLAM8_SETTINGS_STALE: reload settings';
  end if;
  if (p_patch->>'booking_revision')::bigint is distinct from p_revision+1
     or p_patch ? 'tenant_id' then raise exception 'Invalid settings revision'; end if;
  select string_agg(format('%I = r.%I', key, key), ',') into assignments
    from jsonb_object_keys(p_patch) key;
  execute 'update scheduling_config c set ' || assignments ||
    ' from jsonb_populate_record($1, $2) r where c.tenant_id=$3'
    using current_row, p_patch, p_tenant;
end $$;
revoke all on function apply_booking_settings(uuid,bigint,jsonb) from public;
do $$ declare r text; begin
 foreach r in array array['authenticated','service_role'] loop
   if exists(select 1 from pg_roles where rolname=r) then
     execute format('grant execute on function apply_booking_settings(uuid,bigint,jsonb) to %I',r);
   end if;
 end loop;
end $$;
-- Replace the existing functions in place; rollback restores their exact bodies.
do $$ declare f record; body text; patterns text[]; replacements text[]; i integer; begin
 for f in select oid, proname from pg_proc where pronamespace='public'::regnamespace
   and proname in ('enforce_booking_capacity','booking_span_bounds','hold_booking_if_capacity') loop
   select value #>> '{}' into body from booking_settings_rollback
     where key='function:' || f.oid::regprocedure::text;
   if body is null then raise exception 'Missing rollback function: %', f.proname; end if;
   if f.proname='enforce_booking_capacity' then
     patterns := array['coalesce(c.capacity_per_window, 4)',
       'select * into c from scheduling_config where tenant_id = new.tenant_id;', 'if quantity < 1 or cap < 1 then'];
     replacements := array['c.capacity_per_window',
       'select * into c from scheduling_config where tenant_id = new.tenant_id;
       if c.timezone is distinct from ''Europe/London'' or c.working_days is null or c.working_days = ''''
          or c.open_time is null or c.close_time is null or c.open_time >= c.close_time
          or exists(select 1 from jsonb_each(c.booking_setting_review) m
             where m.key in (''working_days'',''opening_hours'',''timezone'')
               and m.value->>''state'' is distinct from ''set'')
          or (c.booking_setting_review->''capacity''->>''state'') in (''unset'',''off'')
          or (c.capacity_per_window is null and
              (c.booking_setting_review->''capacity''->>''state'') is distinct from ''not_applicable'')
       then raise exception ''BLAM8_BOOKING_UNSUPPORTED: booking settings unresolved''; end if;',
       'if cap is null or cap < 1 then raise exception ''BLAM8_BOOKING_UNSUPPORTED: capacity is not set or off''; end if;
  if quantity < 1 or cap < 1 then'];
   elsif f.proname='booking_span_bounds' then
     patterns := array['coalesce(c.open_time, time ''09:00'')', 'coalesce(c.close_time, time ''17:00'')'];
     replacements := array['c.open_time', 'c.close_time'];
   elsif f.proname='hold_booking_if_capacity' then
     patterns := array['v_capacity := coalesce(v_capacity, 4);'];
     replacements := array['if v_capacity is null then raise exception ''BLAM8_BOOKING_UNSUPPORTED: capacity is not set''; end if;'];
   end if;
   for i in 1..array_length(patterns, 1) loop
     if strpos(body, patterns[i]) = 0 then
       raise exception 'BLAM8_FUNCTION_DRIFT: function %, missing pattern %', f.proname, patterns[i];
     end if;
     -- The readiness block belongs immediately after the single config read.
     if patterns[i] = 'select * into c from scheduling_config where tenant_id = new.tenant_id;'
        and strpos(substr(body, strpos(body, patterns[i]) + length(patterns[i])), patterns[i]) > 0 then
       raise exception 'BLAM8_FUNCTION_DRIFT: function %, repeated pattern %', f.proname, patterns[i];
     end if;
     body := replace(body, patterns[i], replacements[i]);
   end loop;
   execute body;
 end loop;
end $$;
commit;
-- Offer deadlines: optional, unset means off for a new business.
alter table scheduling_config add column if not exists offer_last_call time;
alter table scheduling_config add column if not exists offer_last_call_review text;

-- Astra: atomic choice. Two taps (including different siblings) have one winner.
create or replace function public.choose_offered_booking(
    p_booking_id uuid, p_tenant_id uuid, p_details jsonb default null,
    p_exclude_ids uuid[] default array[]::uuid[]
) returns setof public.bookings
language plpgsql security invoker set search_path = public as $$
declare chosen bookings%rowtype; c scheduling_config;
begin
    select * into chosen from bookings
      where id = p_booking_id and tenant_id = p_tenant_id;
    if not found or chosen.contact is null then return; end if;
    perform pg_advisory_xact_lock(hashtextextended(
      p_tenant_id::text || ':offer:' || chosen.contact, 0));
    -- Same day lock as the existing capacity-checked office hold RPC. Recheck
    -- the expiry after waiting: a queue must not extend the customer's offer.
    perform pg_advisory_xact_lock(hashtextextended(
      p_tenant_id::text || ':' || chosen.booking_date::text, 0));
    -- Share the settings/capacity lock before checking readiness and expiry.
    -- The capacity trigger keeps an unchanged live hold's existing reservation.
    perform pg_advisory_xact_lock(hashtextextended('blam8:office:' || p_tenant_id::text, 0));
    select * into c from scheduling_config where tenant_id = p_tenant_id;
    if not found or c.timezone is distinct from 'Europe/London'
       or c.working_days is null or c.working_days = ''
       or c.open_time is null or c.close_time is null or c.open_time >= c.close_time
       or exists(select 1 from jsonb_each(c.booking_setting_review) m
          where m.key in ('working_days','opening_hours','timezone')
            and m.value->>'state' is distinct from 'set')
       or c.capacity_per_window <= 0
       or (c.booking_setting_review->'capacity'->>'state') in ('unset','off')
       or (c.capacity_per_window is null and
           (c.booking_setting_review->'capacity'->>'state') is distinct from 'not_applicable')
    then raise exception 'BLAM8_BOOKING_UNSUPPORTED: booking settings unresolved'; end if;
    select * into chosen from bookings
      where id = p_booking_id and tenant_id = p_tenant_id for update;
    if chosen.status <> 'offered' or chosen.expires_at is null
       or chosen.expires_at <= clock_timestamp() then return; end if;
    update bookings set status = 'confirmed', expires_at = null,
      details = coalesce(p_details, details), updated_at = clock_timestamp()
      where id = p_booking_id and tenant_id = p_tenant_id
      returning * into chosen;
    update bookings set status = 'cancelled', expires_at = null,
      updated_at = clock_timestamp()
      where tenant_id = p_tenant_id and contact = chosen.contact
        and status = 'offered' and id <> p_booking_id
        and not (id = any(p_exclude_ids));
    return next chosen;
end $$;
revoke all on function public.choose_offered_booking(uuid, uuid, jsonb, uuid[]) from public;
grant execute on function public.choose_offered_booking(uuid, uuid, jsonb, uuid[]) to service_role;

-- S1f versioned, content-free cost evidence (no historical inference).
-- S1f: additive receipts only. No historical backfill or tenant-policy change.
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS raw_usage jsonb;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS cost_version text;
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS raw_usage jsonb;
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS cost_version text;

-- Edit review (2026-09-29) — email_log.draft_original: the agent's own text of a
-- draft the owner changed before sending (only kept when the change was
-- substantive). The send overwrites draft_reply with the edit; the post-send
-- "add to knowledge / make it a rule?" card compares the two.
alter table email_log add column if not exists draft_original text;


-- Admin outreach server jobs (2026-10-10)
-- Founder outreach only. Apply before enabling the server job; no tenant policies.
begin;
alter table vcrm_prospects add column if not exists company_number text;
alter table vcrm_prospects add column if not exists email_source text;
alter table vcrm_prospects add column if not exists email_verified boolean not null default false;
alter table vcrm_prospects add column if not exists soft_match boolean not null default true;

create table if not exists admin_outreach_config (
  id integer primary key check (id=1),
  enabled boolean not null default false,
  config jsonb not null default '{}'::jsonb,
  lease_token uuid,
  lease_until timestamptz,
  last_attempt_at timestamptz,
  last_sync_at timestamptz,
  last_result jsonb not null default '{}'::jsonb
);
insert into admin_outreach_config(id) values(1) on conflict do nothing;
create table if not exists admin_outreach_suppressions (
  email text primary key check (email=lower(trim(email))),
  reason text not null,
  created_at timestamptz not null default now()
);
create table if not exists admin_outreach_messages (
  id uuid primary key default gen_random_uuid(),
  prospect_id uuid references vcrm_prospects(id) on delete set null,
  email text not null,
  step integer not null check (step in (0,1)),
  status text not null check (status in ('draft','approved','sending','sent','unknown','cancelled')),
  payload jsonb not null default '{}'::jsonb,
  config_snapshot jsonb not null default '{}'::jsonb,
  provider_id text,
  error text,
  created_at timestamptz not null default now(),
  attempted_at timestamptz,
  sent_at timestamptz,
  unique(email,step)
);
create table if not exists admin_outreach_events (
  id text primary key,
  email text not null,
  kind text not null,
  created_at timestamptz not null default now()
);
create table if not exists admin_outreach_digests (
  day date primary key,
  summary jsonb not null,
  updated_at timestamptz not null default now()
);
alter table admin_outreach_config enable row level security;
alter table admin_outreach_suppressions enable row level security;
alter table admin_outreach_messages enable row level security;
alter table admin_outreach_events enable row level security;
alter table admin_outreach_digests enable row level security;
revoke all on admin_outreach_config,admin_outreach_suppressions,admin_outreach_messages,
  admin_outreach_events,admin_outreach_digests from anon,authenticated;
grant all on admin_outreach_config,admin_outreach_suppressions,admin_outreach_messages,
  admin_outreach_events,admin_outreach_digests to service_role;

-- Preserve legacy touches, including duplicate prospect rows sharing a mailbox.
insert into admin_outreach_messages(prospect_id,email,step,status,sent_at,attempted_at,provider_id)
select distinct on (lower(trim(p.email)), case when c.summary like 'Follow-up sent%' then 1 else 0 end)
 p.id,lower(trim(p.email)),case when c.summary like 'Follow-up sent%' then 1 else 0 end,
 'sent',c.created_at,c.created_at,(regexp_match(c.summary,'Resend id ([A-Za-z0-9-]+)'))[1]
from vcrm_calls c join vcrm_prospects p on p.id=c.prospect_id
where p.email is not null and trim(p.email)<>'' and
 (c.summary like 'Emailed %' or c.summary like 'Follow-up sent%' or c.summary like 'SENT%' or c.summary like 'Confirmed%')
order by lower(trim(p.email)),case when c.summary like 'Follow-up sent%' then 1 else 0 end,c.created_at
on conflict(email,step) do nothing;
insert into admin_outreach_suppressions(email,reason)
select distinct on (lower(trim(email))) lower(trim(email)),coalesce(next_action,'Legacy do-not-contact')
from vcrm_prospects where email is not null and trim(email)<>'' and
 (status='NOT_NOW' or coalesce(next_action,'') ~* '(STOP REQUEST|BOUNCED|SPAM COMPLAINT|never contact)')
on conflict do nothing;

create or replace function admin_outreach_lock(p_token uuid) returns boolean
language plpgsql security invoker set search_path=public as $$
begin
 update admin_outreach_config set lease_token=p_token,lease_until=now()+interval '5 minutes'
 where id=1 and (lease_until is null or lease_until<now());
 return found;
end $$;

-- Serialize ALL workers at the irreversible send boundary. A reservation is never
-- retried automatically, even after a restart or an ambiguous provider timeout.
create or replace function admin_outreach_claim(p_id uuid,p_token uuid,p_config jsonb,
 p_observed integer,p_provider_ids text[]) returns boolean
language plpgsql security invoker set search_path=public as $$
declare cfg admin_outreach_config; msg admin_outreach_messages; p vcrm_prospects;
 local_now timestamp; used integer; planned integer; completed integer; plan_start date;
begin
 select * into cfg from admin_outreach_config where id=1 for update;
 if not cfg.enabled or cfg.config<>p_config or cfg.lease_token is distinct from p_token or cfg.lease_until<now()
    or cfg.last_sync_at is null or cfg.last_sync_at<now()-interval '2 minutes' then return false; end if;
 local_now := now() at time zone (p_config->>'timezone');
 if not (p_config->'working_days') @> to_jsonb((extract(isodow from local_now)::integer-1))
    or (p_config->'holidays') @> to_jsonb(to_char(local_now,'YYYY-MM-DD'))
    or local_now::time<(p_config->>'start_time')::time or local_now::time>=(p_config->>'end_time')::time
    or cfg.last_attempt_at>now()-make_interval(secs=>(p_config->>'min_gap_seconds')::integer) then return false; end if;
 select * into msg from admin_outreach_messages where id=p_id for update;
 if not found or msg.config_snapshot<>p_config or
    not (msg.status='approved' or (msg.status='draft' and p_config->>'mode'='auto')) then return false; end if;
 if exists(select 1 from admin_outreach_suppressions where email=msg.email) then return false; end if;
 if exists(select 1 from vcrm_prospects where lower(trim(email))=msg.email and status in ('NOT_NOW','REPLIED','CALL_BOOKED','SIGNED_UP')) then return false; end if;
 if exists(select 1 from admin_outreach_events where email=msg.email and kind='replied') then return false; end if;
 if exists(select 1 from admin_outreach_messages where status in ('sending','unknown')) then return false; end if;
 select * into p from vcrm_prospects where id=msg.prospect_id;
 if not found or lower(trim(p.email)) is distinct from msg.email or p.kind is distinct from (p_config->>'kind') or
    not p.email_verified or p.soft_match or coalesce(p.company_number,'')='' or coalesce(p.email_source,'')='' or
    coalesce(p.next_action,'') ~* '(STOP REQUEST|BOUNCED|SPAM COMPLAINT|never contact)' then return false; end if;
 if (msg.step=0 and p.status is distinct from 'IDENTIFIED') or (msg.step=1 and
    (p.status is distinct from 'EMAILED' or p.next_action_date is null or p.next_action_date>local_now::date)) then return false; end if;
 if msg.step=1 and not exists(select 1 from admin_outreach_messages where email=msg.email and step=0 and status='sent') then return false; end if;
 if msg.step=0 then
   select coalesce(sum(value::integer),0),min(key::date) into planned,plan_start
     from jsonb_each_text(p_config->'plan') where key::date<=local_now::date;
   select count(*) into completed from admin_outreach_messages where step=0 and status='sent'
     and (sent_at at time zone (p_config->>'timezone'))::date>=plan_start;
   if completed>=planned then return false; end if;
 end if;
 -- Rolling 24h is deliberately stricter than a provider's midnight reset.
 select count(*) into used from admin_outreach_messages where attempted_at>now()-interval '24 hours'
   and (provider_id is null or not(provider_id=any(p_provider_ids)));
 if p_observed+used >= (p_config->>'daily_cap')::integer then return false; end if;
 update admin_outreach_messages set status='sending',attempted_at=now() where id=p_id;
 update admin_outreach_config set last_attempt_at=now() where id=1;
 return true;
end $$;

create or replace function admin_outreach_complete(p_id uuid,p_provider_id text,p_due date)
returns void language plpgsql security invoker set search_path=public as $$
declare msg admin_outreach_messages;
begin
 select * into msg from admin_outreach_messages where id=p_id for update;
 if msg.status not in ('sending','unknown') then return; end if;
 update admin_outreach_messages set status='sent',provider_id=p_provider_id,sent_at=now() where id=p_id;
 update vcrm_prospects set status=case when msg.step=0 then 'EMAILED' else status end,
   next_action=case when msg.step=0 then 'One short follow-up, then stop' else 'Follow-up sent. No further email.' end,
   next_action_date=case when msg.step=0 then p_due else null end,updated_at=now()
 where id=msg.prospect_id and status in ('IDENTIFIED','EMAILED')
   and not exists(select 1 from admin_outreach_suppressions where email=msg.email);
 if msg.prospect_id is not null then
 insert into vcrm_calls(prospect_id,summary) values(msg.prospect_id,
   case when msg.step=0 then 'Emailed via Resend' else 'Follow-up sent' end || ' (server outreach). Resend id ' || p_provider_id);
 end if;
end $$;
revoke all on function admin_outreach_lock(uuid) from public,anon,authenticated;
revoke all on function admin_outreach_claim(uuid,uuid,jsonb,integer,text[]) from public,anon,authenticated;
revoke all on function admin_outreach_complete(uuid,text,date) from public,anon,authenticated;
grant execute on function admin_outreach_lock(uuid) to service_role;
grant execute on function admin_outreach_claim(uuid,uuid,jsonb,integer,text[]) to service_role;
grant execute on function admin_outreach_complete(uuid,text,date) to service_role;

-- Compact draft invalidation for the founder job (2026-10-18).
create or replace function public.admin_outreach_stale_drafts(p_config jsonb)
returns table(id uuid)
language sql stable security invoker set search_path = public
as $$
  select m.id from public.admin_outreach_messages m
  where m.status in ('draft', 'approved') and m.config_snapshot <> p_config;
$$;
revoke all on function public.admin_outreach_stale_drafts(jsonb) from public, anon, authenticated;
grant execute on function public.admin_outreach_stale_drafts(jsonb) to service_role;
commit;

-- Phase 2: list/campaign policy around the existing CRM sequence engine.
begin;
alter table crm_sequences add column if not exists outreach_managed boolean not null default false;
alter table crm_sequence_enrollments add column if not exists outreach_once boolean not null default false;
create unique index if not exists outreach_enrollment_once on crm_sequence_enrollments(sequence_id,contact_id) where outreach_once;
create unique index if not exists crm_sequences_tenant_id_id on crm_sequences(tenant_id,id);
create unique index if not exists crm_contacts_tenant_id_id on crm_contacts(tenant_id,id);
create unique index if not exists email_connections_tenant_id_id on email_connections(tenant_id,id);
create unique index if not exists crm_enrollments_tenant_id_id on crm_sequence_enrollments(tenant_id,id);
create unique index if not exists email_log_tenant_id_id on email_log(tenant_id,id);

create table if not exists outreach_settings (
 tenant_id uuid primary key references tenants(id) on delete cascade,
 enabled boolean not null default false,
 daily_cap integer not null default 100 check(daily_cap between 1 and 1000),
 last_sync_at timestamptz,
 last_attempt_at timestamptz
);
create table if not exists outreach_lists (
 id uuid primary key default gen_random_uuid(),
 tenant_id uuid not null references tenants(id) on delete cascade,
 name text not null check(length(name) between 1 and 100),
 created_at timestamptz not null default now(), unique(tenant_id,id)
);
create table if not exists outreach_targets (
 id uuid primary key default gen_random_uuid(),
 tenant_id uuid not null references tenants(id) on delete cascade,
 list_id uuid not null, contact_id uuid not null, email text not null,
 basis text not null check(basis in ('permission','corporate')),
 evidence text not null check(length(evidence)>0),
 email_source text not null check(length(email_source)>0),
 company_number text not null default '',
 email_verified boolean not null default false, soft_match boolean not null default true,
 created_at timestamptz not null default now(),
 check(email=lower(trim(email))),
 check(basis<>'corporate' or length(company_number)>0),
 unique(tenant_id,list_id,email), unique(tenant_id,id),
 foreign key(tenant_id,list_id) references outreach_lists(tenant_id,id),
 foreign key(tenant_id,contact_id) references crm_contacts(tenant_id,id)
);
create table if not exists outreach_campaigns (
 id uuid primary key default gen_random_uuid(),
 tenant_id uuid not null references tenants(id) on delete cascade,
 list_id uuid not null, sequence_id uuid not null unique, mailbox_id uuid not null,
 name text not null, enabled boolean not null default false,
 config jsonb not null, last_result text not null default 'Paused',
 created_at timestamptz not null default now(), unique(tenant_id,id),
 foreign key(tenant_id,list_id) references outreach_lists(tenant_id,id),
 foreign key(tenant_id,sequence_id) references crm_sequences(tenant_id,id),
 foreign key(tenant_id,mailbox_id) references email_connections(tenant_id,id)
);
create table if not exists outreach_suppressions (
 tenant_id uuid not null references tenants(id) on delete cascade,
 email text not null check(email=lower(trim(email))), reason text not null,
 created_at timestamptz not null default now(), primary key(tenant_id,email)
);
create table if not exists outreach_messages (
 id uuid primary key default gen_random_uuid(),
 tenant_id uuid not null references tenants(id) on delete cascade,
 campaign_id uuid not null, enrollment_id uuid not null, target_id uuid not null,
 step_index integer not null, step_snapshot jsonb not null, config_snapshot jsonb not null,
 email text not null, mailbox_id uuid not null, sender_address text not null,
 status text not null default 'preparing' check(status in ('preparing','draft','sending','sent','unknown','cancelled')),
 work_token uuid, lease_until timestamptz, email_log_id uuid,
 created_at timestamptz not null default now(), attempted_at timestamptz, sent_at timestamptz,
 error text, unique(enrollment_id,step_index), unique(email_log_id),
 foreign key(tenant_id,campaign_id) references outreach_campaigns(tenant_id,id),
 foreign key(tenant_id,enrollment_id) references crm_sequence_enrollments(tenant_id,id),
 foreign key(tenant_id,target_id) references outreach_targets(tenant_id,id),
 foreign key(tenant_id,email_log_id) references email_log(tenant_id,id)
);
create index if not exists outreach_messages_capacity on outreach_messages(tenant_id,attempted_at);

-- Browser clients can inspect their business, but cannot forge reservations,
-- suppressions, verification evidence or campaign settings. App writes use the
-- service role after owner/manager, CRM entitlement and tenant checks.
do $$ declare t text; begin
 foreach t in array array['outreach_settings','outreach_lists','outreach_targets','outreach_campaigns','outreach_suppressions','outreach_messages'] loop
   execute format('alter table %I enable row level security',t);
   execute format('drop policy if exists outreach_tenant_read on %I',t);
   execute format('create policy outreach_tenant_read on %I for select to authenticated using (tenant_id in (select auth_tenant_ids()))',t);
   execute format('revoke all on %I from anon,authenticated',t);
   execute format('grant select on %I to authenticated',t);
   execute format('grant all on %I to service_role',t);
 end loop;
end $$;

create or replace function outreach_protect_sequence() returns trigger
language plpgsql set search_path=public as $$ begin
 if current_user not in ('postgres','service_role') and
    ((TG_OP='INSERT' and new.outreach_managed) or
     (TG_OP='UPDATE' and new.outreach_managed is distinct from old.outreach_managed)) then
   raise exception 'Outreach ownership is managed by the application';
 end if;
 return new;
end $$;
drop trigger if exists outreach_sequence_guard on crm_sequences;
create trigger outreach_sequence_guard before insert or update on crm_sequences
 for each row execute function outreach_protect_sequence();

create or replace function outreach_mark_enrollment() returns trigger
language plpgsql set search_path=public as $$
declare managed boolean;
begin
 -- Serialize ordinary enrollment with conversion to a managed campaign.
 select outreach_managed into managed from crm_sequences
 where id=new.sequence_id and tenant_id=new.tenant_id for share;
 if managed then
   new.outreach_once:=true;
 elsif TG_OP='UPDATE' and old.outreach_once then
   raise exception 'An outreach enrollment cannot be reassigned';
 end if;
 return new;
end $$;
drop trigger if exists outreach_enrollment_guard on crm_sequence_enrollments;
create trigger outreach_enrollment_guard before insert or update on crm_sequence_enrollments
 for each row execute function outreach_mark_enrollment();

-- Reserve composing work. Expired composing work is recoverable; an attempted
-- send is NEVER automatically retried. Cadence/advancement stays in crm_sequences.
create or replace function outreach_create_campaign(p_tenant uuid,p_name text,p_list uuid,p_sequence uuid,p_box uuid,p_config jsonb)
returns setof outreach_campaigns language plpgsql security invoker set search_path=public as $$
declare s crm_sequences;
begin
 select * into s from crm_sequences where id=p_sequence and tenant_id=p_tenant for update;
 if not found or s.outreach_managed or s.trigger_type<>'manual'
    or exists(select 1 from crm_sequence_enrollments where sequence_id=s.id)
    or (select count(*) from crm_sequence_steps where sequence_id=s.id and tenant_id=p_tenant) not between 1 and 7
    or exists(select 1 from crm_sequence_steps where sequence_id=s.id and tenant_id=p_tenant
      and (action_type<>'send_email' or not exit_on_reply or coalesce(trim(template_body),'')='' or coalesce(trim(template_subject),'')='')) then return; end if;
 update crm_sequences set outreach_managed=true where id=s.id;
 return query insert into outreach_campaigns(tenant_id,name,list_id,sequence_id,mailbox_id,config)
   values(p_tenant,p_name,p_list,p_sequence,p_box,p_config) returning *;
end $$;

create or replace function outreach_begin_step(p_tenant uuid,p_campaign uuid,p_enrollment uuid,
 p_target uuid,p_step integer,p_snapshot jsonb,p_config jsonb,p_email text,p_box uuid,p_sender text,p_token uuid)
returns setof outreach_messages language plpgsql security invoker set search_path=public as $$
begin
 insert into outreach_messages(tenant_id,campaign_id,enrollment_id,target_id,step_index,
   step_snapshot,config_snapshot,email,mailbox_id,sender_address,work_token,lease_until)
 values(p_tenant,p_campaign,p_enrollment,p_target,p_step,p_snapshot,p_config,p_email,p_box,p_sender,p_token,now()+interval '5 minutes')
 on conflict(enrollment_id,step_index) do nothing;
 update outreach_messages set work_token=p_token,lease_until=now()+interval '5 minutes',
   step_snapshot=p_snapshot,config_snapshot=p_config,email=p_email,mailbox_id=p_box,sender_address=p_sender
 where tenant_id=p_tenant and enrollment_id=p_enrollment and step_index=p_step
   and status='preparing' and (lease_until<now() or work_token=p_token);
 return query select * from outreach_messages
 where tenant_id=p_tenant and enrollment_id=p_enrollment and step_index=p_step;
end $$;

create or replace function outreach_finish_draft(p_tenant uuid,p_id uuid,p_token uuid,p_subject text,p_body text)
returns uuid language plpgsql security invoker set search_path=public as $$
declare m outreach_messages; log_id uuid;
begin
 select * into m from outreach_messages where id=p_id and tenant_id=p_tenant for update;
 if not found or m.status<>'preparing' or m.work_token is distinct from p_token or m.lease_until<now() then return null; end if;
 log_id:=m.email_log_id;
 if log_id is null then
   insert into email_log(tenant_id,from_address,subject,body_in,draft_reply,status,kind,mailbox_id,source_message_id)
   values(p_tenant,m.email,p_subject,'[outreach sequence]',p_body,'pending','tenant_outreach',m.mailbox_id,'outreach:'||m.id::text)
   returning id into log_id;
 else
   update email_log set from_address=m.email,subject=p_subject,draft_reply=p_body,status='pending',mailbox_id=m.mailbox_id
   where id=log_id and tenant_id=p_tenant;
 end if;
 update outreach_messages set status='draft',email_log_id=log_id,work_token=null,lease_until=null,error=null where id=m.id;
 return log_id;
end $$;

-- One tenant row serializes ALL campaign sends, including manual approvals.
create or replace function outreach_claim(p_tenant uuid,p_id uuid,p_auto boolean)
returns text language plpgsql security invoker set search_path=public as $$
declare s outreach_settings; c outreach_campaigns; m outreach_messages;
 e crm_sequence_enrollments; t outreach_targets; p crm_contacts; b email_connections;
 step crm_sequence_steps; local_now timestamp; used integer; can_auto boolean;
begin
 select * into s from outreach_settings where tenant_id=p_tenant for update;
 if not found or not s.enabled then return 'Outreach is switched off'; end if;
 if not exists(select 1 from subscriptions where tenant_id=p_tenant and module='crm' and status='active') then return 'CRM is not active'; end if;
 select * into m from outreach_messages where id=p_id and tenant_id=p_tenant for update;
 if not found or m.status<>'draft' then return 'This draft is not available to send'; end if;
 select * into c from outreach_campaigns where id=m.campaign_id and tenant_id=p_tenant;
 if not found or not c.enabled then return 'Campaign is paused'; end if;
 if c.config<>m.config_snapshot or c.mailbox_id<>m.mailbox_id then return 'Campaign changed; refresh this draft'; end if;
 if s.last_sync_at is null or s.last_sync_at<now()-interval '2 minutes' then return 'Reply sync is not current'; end if;
 select * into e from crm_sequence_enrollments where id=m.enrollment_id and tenant_id=p_tenant;
 if not found or e.status<>'active' or e.sequence_id<>c.sequence_id or e.current_step<>m.step_index
    or e.next_action_at>now() then return 'Enrollment is not due or active'; end if;
 if not exists(select 1 from crm_sequences where id=e.sequence_id and tenant_id=p_tenant and status='active' and outreach_managed) then return 'Sequence is paused'; end if;
 select * into step from crm_sequence_steps where tenant_id=p_tenant and sequence_id=e.sequence_id order by step_order limit 1 offset m.step_index;
 if not found or to_jsonb(step)<>m.step_snapshot or step.action_type<>'send_email' or not step.exit_on_reply then return 'Sequence changed; refresh this draft'; end if;
 select * into t from outreach_targets where id=m.target_id and tenant_id=p_tenant;
 if not found or t.list_id<>c.list_id or t.email<>m.email or not t.email_verified or t.soft_match
    or t.email_source='' or t.evidence='' or (t.basis='corporate' and t.company_number='') then return 'Recipient needs verification'; end if;
 select * into p from crm_contacts where id=e.contact_id and tenant_id=p_tenant;
 if not found or p.id<>t.contact_id or lower(trim(p.email)) is distinct from m.email or p.do_not_sequence then return 'Recipient is blocked or changed'; end if;
 if exists(select 1 from outreach_suppressions where tenant_id=p_tenant and email=m.email)
    or exists(select 1 from ignored_contacts where tenant_id=p_tenant and lower(trim(address))=m.email)
    or exists(select 1 from crm_contacts where tenant_id=p_tenant and lower(trim(email))=m.email and do_not_sequence) then return 'Recipient must not be contacted'; end if;
 if exists(select 1 from email_log where tenant_id=p_tenant and kind='inbound'
    and lower(trim(from_address))=m.email and created_at>=e.enrolled_at) then return 'Recipient replied'; end if;
 select * into b from email_connections where id=c.mailbox_id and tenant_id=p_tenant;
 if not found or b.sync_failing_since is not null or lower(trim(coalesce(nullif(b.send_as_address,''),b.address))) is distinct from m.sender_address then return 'Sending mailbox is unavailable or changed'; end if;
 if not exists(select 1 from email_log where id=m.email_log_id and tenant_id=p_tenant
    and kind='tenant_outreach' and status='pending' and from_address=m.email and mailbox_id=m.mailbox_id) then return 'Draft is no longer pending'; end if;
 if p_auto then
   select coalesce((select auto_send from mailbox_config where mailbox_id=c.mailbox_id and tenant_id=p_tenant),
                   (select auto_send from tenant_config where tenant_id=p_tenant),false) into can_auto;
   if c.config->>'mode'<>'auto' or not can_auto then return 'Approval required'; end if;
 end if;
 local_now:=now() at time zone (c.config->>'timezone');
 if not (c.config->'working_days') @> to_jsonb(extract(isodow from local_now)::integer-1)
    or (c.config->'holidays') @> to_jsonb(to_char(local_now,'YYYY-MM-DD'))
    or local_now::time<(c.config->>'start_time')::time or local_now::time>=(c.config->>'end_time')::time then return 'Outside working window'; end if;
 if exists(select 1 from outreach_messages where tenant_id=p_tenant and status in ('sending','unknown')) then return 'Reconcile an uncertain send first'; end if;
 if s.last_attempt_at>now()-make_interval(secs=>(c.config->>'min_gap_seconds')::integer) then return 'Minimum gap between emails'; end if;
 select count(*) into used from outreach_messages where tenant_id=p_tenant and attempted_at>now()-interval '24 hours';
 if used>=s.daily_cap then return 'Shared outreach cap reached'; end if;
 select count(*) into used from outreach_messages where tenant_id=p_tenant and campaign_id=c.id and attempted_at>now()-interval '24 hours';
 if used>=(c.config->>'daily_cap')::integer then return 'Campaign cap reached'; end if;
 if exists(select 1 from outreach_messages where tenant_id=p_tenant and email=m.email and attempted_at>now()-interval '24 hours') then return 'Recipient already contacted in the last 24 hours'; end if;
 update outreach_messages set status='sending',attempted_at=now(),error=null where id=m.id;
 update outreach_settings set last_attempt_at=now() where tenant_id=p_tenant;
 return 'claimed';
end $$;

-- Durable completion plus inbox status in ONE transaction. A failed response can
-- be recovered by reading this row; it never causes a second provider request.
create or replace function outreach_complete(p_tenant uuid,p_id uuid,p_body text,p_auto boolean)
returns void language plpgsql security invoker set search_path=public as $$
declare m outreach_messages;
begin
 select * into m from outreach_messages where id=p_id and tenant_id=p_tenant for update;
 if not found or m.status<>'sending' then return; end if;
 update outreach_messages set status='sent',sent_at=now(),error=null where id=m.id;
 update email_log set status='sent',draft_reply=p_body,sent_at=now(),sent_via=case when p_auto then 'auto' else 'human' end
 where id=m.email_log_id and tenant_id=p_tenant;
end $$;
revoke all on function outreach_begin_step(uuid,uuid,uuid,uuid,integer,jsonb,jsonb,text,uuid,text,uuid),outreach_finish_draft(uuid,uuid,uuid,text,text),outreach_claim(uuid,uuid,boolean),outreach_complete(uuid,uuid,text,boolean) from public,anon,authenticated;
grant execute on function outreach_begin_step(uuid,uuid,uuid,uuid,integer,jsonb,jsonb,text,uuid,text,uuid),outreach_finish_draft(uuid,uuid,uuid,text,text),outreach_claim(uuid,uuid,boolean),outreach_complete(uuid,uuid,text,boolean) to service_role;
revoke all on function outreach_create_campaign(uuid,text,uuid,uuid,uuid,jsonb) from public,anon,authenticated;
grant execute on function outreach_create_campaign(uuid,text,uuid,uuid,uuid,jsonb) to service_role;
commit;

-- Revision-aware Concierge delegation (20261011).
-- C3-01/04/07/09/10: durable revision/attempt/delivery ownership.
-- Additive; apply before enabling CONCIERGE_DELEGATION_ENABLED. Never replay effects.
begin;
alter table concierge_tasks add column if not exists revision integer not null default 0;
alter table concierge_tasks add column if not exists row_version bigint not null default 0;
alter table concierge_tasks add column if not exists attempt_id uuid;
alter table concierge_tasks add column if not exists source_turn text;
alter table concierge_tasks add column if not exists voice_session uuid;
alter table concierge_tasks add column if not exists commit_state text not null default 'preparation';
alter table concierge_tasks add column if not exists result jsonb not null default '{}';
alter table concierge_tasks add column if not exists event_id uuid;
alter table concierge_tasks add column if not exists delivery jsonb not null default '{}';
create unique index if not exists idx_concierge_task_source on concierge_tasks
 (tenant_id, coalesce(user_id,'00000000-0000-0000-0000-000000000000'::uuid),
  source_turn, coalesce(voice_session,'00000000-0000-0000-0000-000000000000'::uuid), position)
 where revision > 0;
-- Existing tenant-wide ALL policies must not permit forging/downgrading the
-- new authority. Legacy rows keep their old write behavior; new rows are
-- service-role-only writes, requesting-user-only reads.
drop policy if exists "tenant isolation - concierge_tasks" on concierge_tasks;
drop policy if exists "concierge tasks read" on concierge_tasks;
create policy "concierge tasks read" on concierge_tasks for select using
 (tenant_id in (select auth_tenant_ids()) and (revision=0 or user_id=auth.uid()));
drop policy if exists "concierge tasks legacy insert" on concierge_tasks;
create policy "concierge tasks legacy insert" on concierge_tasks for insert with check
 (tenant_id in (select auth_tenant_ids()) and revision=0);
drop policy if exists "concierge tasks legacy update" on concierge_tasks;
create policy "concierge tasks legacy update" on concierge_tasks for update using
 (tenant_id in (select auth_tenant_ids()) and revision=0) with check
 (tenant_id in (select auth_tenant_ids()) and revision=0);
drop policy if exists "concierge tasks legacy delete" on concierge_tasks;
create policy "concierge tasks legacy delete" on concierge_tasks for delete using
 (tenant_id in (select auth_tenant_ids()) and revision=0);
drop policy if exists "tenant isolation - concierge_proposals" on concierge_proposals;
drop policy if exists "concierge proposals read" on concierge_proposals;
create policy "concierge proposals read" on concierge_proposals for select using
 (tenant_id in (select auth_tenant_ids()) and (not (payload ? '_delegation') or user_id=auth.uid()));
drop policy if exists "concierge proposals legacy insert" on concierge_proposals;
create policy "concierge proposals legacy insert" on concierge_proposals for insert with check
 (tenant_id in (select auth_tenant_ids()) and not (payload ? '_delegation'));
drop policy if exists "concierge proposals legacy update" on concierge_proposals;
create policy "concierge proposals legacy update" on concierge_proposals for update using
 (tenant_id in (select auth_tenant_ids()) and not (payload ? '_delegation')) with check
 (tenant_id in (select auth_tenant_ids()) and not (payload ? '_delegation'));
drop policy if exists "concierge proposals legacy delete" on concierge_proposals;
create policy "concierge proposals legacy delete" on concierge_proposals for delete using
 (tenant_id in (select auth_tenant_ids()) and not (payload ? '_delegation'));
-- Sign-off record (db/migrations/20261016_concierge_sign_off.sql): who settled a card, how,
-- and when; reprepared_from links a card prepared again from an older parked one.
alter table concierge_proposals add column if not exists decided_at timestamptz;
alter table concierge_proposals add column if not exists decided_by uuid;
alter table concierge_proposals add column if not exists decided_via text;
alter table concierge_proposals add column if not exists reprepared_from uuid;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'concierge_proposals_decided_via_check') then
    alter table concierge_proposals add constraint concierge_proposals_decided_via_check
      check (decided_via is null or decided_via in ('tap','words','unknown','system'));
  end if;
end $$;
create table if not exists concierge_task_history (
 id bigint generated always as identity primary key,
 tenant_id uuid not null references tenants(id) on delete cascade,
 task_id uuid not null references concierge_tasks(id) on delete cascade,
 revision integer not null,
 recorded_at timestamptz not null default now(),
 reason text not null,
 snapshot jsonb not null
);
alter table concierge_task_history enable row level security;
drop policy if exists "tenant isolation - concierge_task_history" on concierge_task_history;
create policy "tenant isolation - concierge_task_history" on concierge_task_history
 for select using (tenant_id in (select auth_tenant_ids()) and exists
   (select 1 from concierge_tasks t where t.id=task_id and t.user_id=auth.uid()));
-- Writes are through an invoker function and existing task permissions.
drop policy if exists "tenant history insert" on concierge_task_history;
create policy "tenant history insert" on concierge_task_history
 for insert with check (tenant_id in (select auth_tenant_ids()) and exists
   (select 1 from concierge_tasks t where t.id=task_id and t.user_id=auth.uid()));

create or replace function concierge_task_transition(
 p_operation text, p_tenant uuid, p_user uuid, p_task uuid default null,
 p_revision integer default null, p_attempt uuid default null, p_data jsonb default '{}'
) returns jsonb language plpgsql security invoker set search_path=public as $$
declare
 t concierge_tasks%rowtype;
 p concierge_proposals%rowtype;
 r jsonb;
 binding jsonb;
 candidate jsonb;
 new_id uuid;
 rows_out jsonb;
begin
 -- Authenticated callers cannot impersonate another requester; service workers
 -- have no auth.uid and are still constrained by all explicit row predicates.
 if auth.uid() is not null and auth.uid() is distinct from p_user then
   return jsonb_build_object('ok',false,'state','unavailable');
 end if;
 if auth.uid() is not null and p_operation in
   ('claim','recover','stage','finish','approval_claim','approval_finish') then
   return jsonb_build_object('ok',false,'state','worker_only');
 end if;
 if p_operation='create' then
   if nullif(p_data->>'source_turn','') is null then raise exception 'source turn required'; end if;
   -- Idempotency covers the WHOLE source turn, not individual positions: a
   -- retried model split must not append extra autonomous work to that turn.
   perform pg_advisory_xact_lock(hashtextextended(concat_ws(':','concierge-delegation',p_tenant::text,
      coalesce(p_user::text,''),p_data->>'source_turn',coalesce(p_data->>'voice_session','')),0));
   select jsonb_agg(to_jsonb(c) order by position) into rows_out from concierge_tasks c
    where tenant_id=p_tenant and user_id is not distinct from p_user
      and source_turn=p_data->>'source_turn'
      and voice_session is not distinct from nullif(p_data->>'voice_session','')::uuid;
   if rows_out is not null then return jsonb_build_object('ok',true,'tasks',rows_out); end if;
   for candidate in select value from jsonb_array_elements(p_data->'tasks') loop
     insert into concierge_tasks(tenant_id,user_id,batch_id,position,title,instruction,surface,
        revision,source_turn,voice_session,result)
       values(p_tenant,p_user,(p_data->>'batch_id')::uuid,(candidate->>'position')::integer,
        candidate->>'title',candidate->>'instruction',p_data->>'surface',1,
        p_data->>'source_turn',nullif(p_data->>'voice_session','')::uuid,jsonb_build_object('scope',p_data->>'scope'))
       on conflict do nothing;
   end loop;
   select jsonb_agg(to_jsonb(c) order by position) into rows_out from concierge_tasks c
    where tenant_id=p_tenant and user_id is not distinct from p_user
      and source_turn=p_data->>'source_turn'
      and voice_session is not distinct from nullif(p_data->>'voice_session','')::uuid;
   return jsonb_build_object('ok',rows_out is not null,'tasks',coalesce(rows_out,'[]'));
 end if;
 -- Linked proposal operations resolve the immutable task binding first, then
 -- lock TASK before PROPOSAL in every path, including revise and resurface.
 if p_operation in ('approval_claim','approval_finish','resurface') then
   select * into p from concierge_proposals where id=(p_data->>'proposal_id')::uuid and tenant_id=p_tenant;
   if not found then return jsonb_build_object('ok',false,'state','unavailable'); end if;
   binding=p.payload->'_delegation';
   if binding is null then return jsonb_build_object('ok',false,'state','unavailable'); end if;
   p_task=(binding->>'task_id')::uuid;
 end if;
 select * into t from concierge_tasks where id=p_task and tenant_id=p_tenant
   and user_id is not distinct from p_user and revision>0 for update;
 if not found then return jsonb_build_object('ok',false,'state','unavailable'); end if;
 if p_operation in ('approval_claim','approval_finish','resurface') then
   select * into p from concierge_proposals where id=p.id and tenant_id=p_tenant for update;
   if t.revision<>(binding->>'revision')::integer or t.proposal_id is distinct from p.id
      or binding->>'digest' is distinct from md5((p.payload-'_delegation')::text) then
     return jsonb_build_object('ok',false,'state','superseded','task',to_jsonb(t));
   end if;
 else
   if p_revision is distinct from t.revision then
     return jsonb_build_object('ok',false,'state','stale','task',to_jsonb(t));
   end if;
 end if;
 if p_operation in ('revise','cancel') then
   if t.commit_state in ('executing','committed','unknown') then
     return jsonb_build_object('ok',false,'state','too_late','task',to_jsonb(t));
   end if;
   if t.status not in ('queued','working','waiting','needs_you') then
     return jsonb_build_object('ok',false,'state','closed','task',to_jsonb(t));
   end if;
   if t.proposal_id is not null then
     select * into p from concierge_proposals where id=t.proposal_id and tenant_id=p_tenant for update;
     if found and p.status not in ('pending','superseded','expired') then
       return jsonb_build_object('ok',false,'state','too_late','task',to_jsonb(t));
     end if;
     update concierge_proposals set status='superseded' where id=t.proposal_id and tenant_id=p_tenant;
   end if;
   insert into concierge_task_history(tenant_id,task_id,revision,reason,snapshot)
     values(p_tenant,t.id,t.revision,p_operation,to_jsonb(t));
   update concierge_tasks set row_version=row_version+1, revision=revision+1, attempt_id=null, proposal_id=null,
      status=case when p_operation='cancel' then 'cancelled' else 'queued' end,
      instruction=case when p_operation='revise' then p_data->>'instruction' else instruction end,
      outcome=case when p_operation='cancel' then 'Task cancelled before execution.' else null end,
      result=case when p_operation='cancel' then '{"kind":"cancelled","summary":"Task cancelled before execution."}'::jsonb else jsonb_build_object('scope',result->>'scope') end,
      commit_state='preparation',started_at=null,finished_at=case when p_operation='cancel' then now() else null end,
      event_id=gen_random_uuid(),delivery='{}' where id=t.id returning * into t;
 elsif p_operation='claim' then
   if t.status<>'queued' then return jsonb_build_object('ok',false,'state','busy','task',to_jsonb(t)); end if;
   insert into concierge_task_history(tenant_id,task_id,revision,reason,snapshot)
     values(p_tenant,t.id,t.revision,'attempt',to_jsonb(t));
   update concierge_tasks set row_version=row_version+1, status='working',attempt_id=p_attempt,started_at=now()
     where id=t.id returning * into t;
 elsif p_operation='recover' then
   if t.status='working' and t.commit_state='preparation' then
     insert into concierge_task_history(tenant_id,task_id,revision,reason,snapshot)
       values(p_tenant,t.id,t.revision,'restart',to_jsonb(t));
     update concierge_tasks set row_version=row_version+1, status='queued',attempt_id=null where id=t.id returning * into t;
   elsif t.commit_state='executing' then
     -- The proposal receipt remains Cycle2-owned. Never re-enter an effect.
     update concierge_tasks set row_version=row_version+1, status='needs_you',commit_state='unknown',
       outcome='Outcome unconfirmed. Check the original action.',event_id=gen_random_uuid(),
       result='{"kind":"unknown","summary":"Outcome unconfirmed. Check the original action."}',delivery='{}'
       where id=t.id returning * into t;
   end if;
 elsif p_operation in ('finish','stage') then
   if t.status<>'working' or t.commit_state<>'preparation' or t.attempt_id is distinct from p_attempt then
     return jsonb_build_object('ok',false,'state','superseded','task',to_jsonb(t));
   end if;
   if p_operation='stage' then
     new_id=gen_random_uuid();
     r=coalesce(p_data->'payload','{}');
     binding=jsonb_build_object('task_id',t.id,'revision',t.revision,'digest',md5(r::text));
     insert into concierge_proposals(id,tenant_id,user_id,kind,summary,payload,status)
       values(new_id,p_tenant,p_user,p_data->>'kind',p_data->>'summary',
        r||jsonb_build_object('_delegation',binding),'pending');
     update concierge_tasks set row_version=row_version+1, status='waiting',proposal_id=new_id,outcome=p_data->>'summary',
       result=jsonb_build_object('kind','waiting','summary','Prepared for your approval: '||(p_data->>'summary'),'scope',t.result->>'scope'),
       finished_at=now(),event_id=gen_random_uuid(),delivery='{}' where id=t.id returning * into t;
   else
     update concierge_tasks set row_version=row_version+1, status=p_data->>'status',outcome=p_data->>'summary',
       result=p_data->'result',finished_at=now(),event_id=gen_random_uuid(),delivery='{}'
       where id=t.id returning * into t;
   end if;
 elsif p_operation in ('approval_claim','resurface') and p.status='pending' and t.status='waiting'
       and p.created_at < coalesce((p_data->>'cutoff')::timestamptz,now()-interval '5 minutes') then
   update concierge_proposals set status='expired' where id=p.id returning * into p;
   update concierge_tasks set row_version=row_version+1,status='needs_you',
     result=jsonb_build_object('kind','failed','summary','This approval expired. Revise the request to prepare a current card.','scope',t.result->>'scope'),
     outcome='This approval expired. Revise the request to prepare a current card.',
     event_id=gen_random_uuid(),delivery='{}',finished_at=now() where id=t.id returning * into t;
   return jsonb_build_object('ok',false,'state','expired','task',to_jsonb(t),'proposal',to_jsonb(p));
 elsif p_operation='approval_claim' then
   if p.status<>'pending' or t.status<>'waiting' or t.commit_state<>'preparation'
      or p.created_at < (p_data->>'cutoff')::timestamptz then
     return jsonb_build_object('ok',false,'state','unavailable','task',to_jsonb(t));
   end if;
   update concierge_proposals set status='executing',result=p_data->'receipt',executed_at=null
     where id=p.id returning * into p;
   update concierge_tasks set row_version=row_version+1, status='working',commit_state='executing' where id=t.id returning * into t;
   return jsonb_build_object('ok',true,'task',to_jsonb(t),'proposal',to_jsonb(p));
 elsif p_operation='approval_finish' then
   if p.status not in ('executing','unknown') or p.result->'execution'->>'id' is distinct from p_data->>'execution_id' then
     return jsonb_build_object('ok',false,'state','stale','task',to_jsonb(t));
   end if;
   update concierge_proposals set status=p_data->>'status',result=p_data->'receipt',
     executed_at=case when p_data->>'status'='executed' then now() else executed_at end
     where id=p.id returning * into p;
   update concierge_tasks set row_version=row_version+1, status=case when p.status='executed' then 'done' else 'needs_you' end,
     commit_state=case when p.status='executed' then 'committed' else 'unknown' end,
     outcome=p.result->>'summary',result=jsonb_build_object('kind','action_receipt','proposal_id',p.id),
     finished_at=now(),event_id=gen_random_uuid(),delivery='{}' where id=t.id returning * into t;
 elsif p_operation='resurface' then
   if p.status<>'pending' or t.status<>'waiting' or t.commit_state<>'preparation' then
     return jsonb_build_object('ok',false,'state','unavailable','task',to_jsonb(t));
   end if;
   -- Return the SAME immutable prepared card; never stage a second copy.
   return jsonb_build_object('ok',true,'task',to_jsonb(t),'proposal',to_jsonb(p));
 elsif p_operation='notice_claim' then
   if t.event_id is null or t.event_id is distinct from (p_data->>'event_id')::uuid
      or t.status not in ('waiting','done','needs_you','failed','cancelled') then
     return jsonb_build_object('ok',false,'state','stale');
   end if;
   if exists(select 1 from concierge_task_history where task_id=t.id and revision=t.revision
             and reason='notice:'||t.event_id::text) then
     return jsonb_build_object('ok',false,'state','claimed');
   end if;
   insert into concierge_task_history(tenant_id,task_id,revision,reason,snapshot)
     values(p_tenant,t.id,t.revision,'notice:'||t.event_id::text,to_jsonb(t));
 elsif p_operation in ('delivery_claim','delivery_start','delivery_ack','delivery_release') then
   if t.event_id is distinct from (p_data->>'event_id')::uuid or t.voice_session is distinct from (p_data->>'voice_session')::uuid
      or t.status not in ('waiting','done','needs_you','failed','cancelled') then
     return jsonb_build_object('ok',false,'state','stale','task',to_jsonb(t));
   end if;
   if p_operation='delivery_claim' then
     if t.delivery<>'{}'::jsonb then return jsonb_build_object('ok',false,'state','claimed','task',to_jsonb(t)); end if;
     update concierge_tasks set row_version=row_version+1, delivery=jsonb_build_object('id',p_data->>'delivery_id','state','claimed','at',now())
       where id=t.id returning * into t;
   else
     if t.delivery->>'id' is distinct from p_data->>'delivery_id' then
       return jsonb_build_object('ok',false,'state','unavailable');
     end if;
     if p_operation='delivery_release' then
       if t.delivery->>'state'<>'claimed' then return jsonb_build_object('ok',false,'state','stale'); end if;
       update concierge_tasks set row_version=row_version+1, delivery='{}' where id=t.id returning * into t;
     elsif p_operation='delivery_start' then
       if t.delivery->>'state'<>'claimed' then return jsonb_build_object('ok',false,'state','stale'); end if;
       update concierge_tasks set row_version=row_version+1, delivery=delivery||'{"state":"started"}'::jsonb where id=t.id returning * into t;
     else
       if t.delivery->>'state' not in ('claimed','started') then return jsonb_build_object('ok',false,'state','stale'); end if;
       update concierge_tasks set row_version=row_version+1, delivery=delivery||jsonb_build_object('state',
         case when p_data->>'outcome'='completed' and delivery->>'state'='started' then 'completed' else 'interrupted' end)
         where id=t.id returning * into t;
     end if;
   end if;
 else
   raise exception 'unsupported task transition';
 end if;
 return jsonb_build_object('ok',true,'task',to_jsonb(t));
end $$;
revoke all on function concierge_task_transition(text,uuid,uuid,uuid,integer,uuid,jsonb) from public;
revoke all on function concierge_task_transition(text,uuid,uuid,uuid,integer,uuid,jsonb) from authenticated;
grant execute on function concierge_task_transition(text,uuid,uuid,uuid,integer,uuid,jsonb) to service_role;
revoke all on concierge_task_history from authenticated;
grant select,insert on concierge_task_history to service_role;
grant usage,select on sequence concierge_task_history_id_seq to service_role;

-- Phase 3: durable occurrences around existing application actions.
begin;
create table if not exists schedule_settings (
 tenant_id uuid primary key references tenants(id) on delete cascade,
 enabled boolean not null default false,
 config jsonb not null default '{"timezone":"Europe/London","working_days":[0,1,2,3,4],"holidays":[],"start_time":"09:00","end_time":"17:00","daily_cap":50,"min_gap_seconds":60}',
 last_attempt_at timestamptz
);
create table if not exists schedule_items (
 id uuid primary key default gen_random_uuid(), tenant_id uuid not null references tenants(id) on delete cascade,
 title text not null check(length(title) between 1 and 150),
 kind text not null check(kind in ('email','sms','quote','invoice','booking','task','followup','outreach')),
 mode text not null default 'draft' check(mode in ('draft','auto')),
 status text not null default 'active' check(status in ('active','paused','cancelled')),
 next_at timestamptz, timezone text not null default 'Europe/London',
 recurrence jsonb not null default '{"frequency":"once"}', catch_up text not null default 'run_late' check(catch_up in ('run_late','skip')),
 payload jsonb not null default '{}', dependency jsonb not null default '{}',
 revision integer not null default 1, created_by uuid not null,
 created_at timestamptz not null default now(), unique(tenant_id,id)
);
create index if not exists schedule_due on schedule_items(next_at) where status='active';
create table if not exists schedule_runs (
 id uuid primary key default gen_random_uuid(), tenant_id uuid not null references tenants(id) on delete cascade,
 item_id uuid not null, scheduled_at timestamptz not null, snapshot jsonb not null,
 status text not null default 'preparing' check(status in ('preparing','ready','executing','done','blocked','skipped','unknown')),
 token uuid, lease_until timestamptz, prepared jsonb not null default '{}',
 email_log_id uuid, result text, attempted_at timestamptz, finished_at timestamptz,
 created_at timestamptz not null default now(), last_checked_at timestamptz not null default now(),
 unique(item_id,scheduled_at), unique(tenant_id,id),
 foreign key(tenant_id,item_id) references schedule_items(tenant_id,id),
 foreign key(tenant_id,email_log_id) references email_log(tenant_id,id)
);
create table if not exists schedule_summaries (
 tenant_id uuid not null references tenants(id) on delete cascade, day date not null,
 counts jsonb not null, updated_at timestamptz not null default now(), primary key(tenant_id,day)
);
do $$ declare t text; begin
 foreach t in array array['schedule_settings','schedule_items','schedule_runs','schedule_summaries'] loop
  execute format('alter table %I enable row level security',t);
  execute format('drop policy if exists tenant_read on %I',t);
  execute format('create policy tenant_read on %I for select to authenticated using(tenant_id in (select auth_tenant_ids()))',t);
  execute format('revoke all on %I from anon, authenticated',t);
  execute format('grant select on %I to authenticated',t);
  execute format('grant all on %I to service_role',t);
 end loop;
end $$;

create or replace function schedule_reserve(p_tenant uuid,p_item uuid,p_due timestamptz,p_next timestamptz,p_token uuid)
returns setof schedule_runs language plpgsql security invoker set search_path=public as $$
declare i schedule_items;
begin
 perform 1 from schedule_settings where tenant_id=p_tenant and enabled for update;
 if not found then return; end if;
 select * into i from schedule_items where tenant_id=p_tenant and id=p_item for update;
 if not found or i.status<>'active' or i.next_at is distinct from p_due or i.next_at>now() then return; end if;
 if p_next is not null and p_next<=p_due then raise exception 'Recurrence must advance'; end if;
 return query insert into schedule_runs(tenant_id,item_id,scheduled_at,snapshot,token,lease_until)
 values(p_tenant,i.id,p_due,to_jsonb(i),p_token,now()+interval '5 minutes')
 on conflict(item_id,scheduled_at) do nothing returning *;
 update schedule_items set next_at=p_next where id=i.id;
end $$;

create or replace function schedule_prepare(p_tenant uuid,p_run uuid,p_token uuid,p_data jsonb)
returns boolean language plpgsql security invoker set search_path=public as $$
declare r schedule_runs; lid uuid;
begin
 select * into r from schedule_runs where tenant_id=p_tenant and id=p_run for update;
 if not found or r.status<>'preparing' or r.token<>p_token or r.lease_until<now() then return false; end if;
 if r.snapshot->>'kind' in ('email','sms') then
  insert into email_log(tenant_id,from_address,subject,body_in,draft_reply,status,kind,mailbox_id,source_message_id)
  values(p_tenant,p_data->>'to',p_data->>'subject','[scheduled plan]',p_data->>'body','pending','scheduled_plan',
    nullif(p_data->>'mailbox_id','')::uuid,'schedule:'||r.id::text) returning id into lid;
 end if;
 update schedule_runs set status='ready',prepared=p_data,email_log_id=lid,token=null,lease_until=null where id=r.id;
 return true;
end $$;

create or replace function schedule_skip_future(p_tenant uuid,p_item uuid,p_due timestamptz,p_next timestamptz)
returns boolean language plpgsql security invoker set search_path=public as $$
declare i schedule_items;
begin
 select * into i from schedule_items where tenant_id=p_tenant and id=p_item for update;
 if not found or i.next_at is distinct from p_due or p_due is null then return false; end if;
 if p_next is not null and p_next<=p_due then raise exception 'Recurrence must advance'; end if;
 insert into schedule_runs(tenant_id,item_id,scheduled_at,snapshot,status,result,finished_at)
 values(p_tenant,i.id,p_due,to_jsonb(i),'skipped','Skipped by the owner',now()) on conflict(item_id,scheduled_at) do nothing;
 update schedule_items set next_at=p_next where id=i.id;
 return true;
end $$;

create or replace function schedule_claim(p_tenant uuid,p_run uuid,p_auto boolean)
returns text language plpgsql security invoker set search_path=public as $$
declare s schedule_settings; r schedule_runs; i schedule_items; local_now timestamp; cap integer;
begin
 select * into s from schedule_settings where tenant_id=p_tenant for update;
 if not found or not s.enabled then return 'Scheduled plan is paused'; end if;
 select * into r from schedule_runs where tenant_id=p_tenant and id=p_run;
 if not found then return 'Run not found'; end if;
 select * into i from schedule_items where tenant_id=p_tenant and id=r.item_id for update;
 select * into r from schedule_runs where tenant_id=p_tenant and id=p_run for update;
 if r.status<>'ready' then return 'This occurrence is not ready'; end if;
 if i.status<>'active' or i.revision<>(r.snapshot->>'revision')::integer then return 'Item paused or changed'; end if;
 if not exists(select 1 from tenant_members where tenant_id=p_tenant and user_id=i.created_by
    and coalesce(active,true) and role in ('owner','manager','member')) then return 'Planner owner no longer has permission'; end if;
 if i.kind in ('task','followup','outreach') and not exists(select 1 from subscriptions
    where tenant_id=p_tenant and module='crm' and status='active') then return 'CRM is not active'; end if;
 if i.kind='booking' and not exists(select 1 from subscriptions where tenant_id=p_tenant
    and module='scheduling' and status='active') then return 'Scheduling is not active'; end if;
 if i.dependency->>'kind'='after_payment' and not exists(select 1 from invoices
    where tenant_id=p_tenant and id::text=i.dependency->>'invoice_id' and status='paid') then return 'Waiting for payment.'; end if;
 if i.dependency->>'kind'='if_no_reply' and exists(select 1 from email_log l join crm_contacts c
    on c.tenant_id=p_tenant and c.id::text=i.payload->>'contact_id'
    where l.tenant_id=p_tenant and l.kind='inbound' and lower(l.from_address)=lower(c.email)
    and l.created_at>=(i.dependency->>'since')::timestamptz) then return 'A reply has arrived; review this occurrence.'; end if;
 if p_auto and (i.mode<>'auto' or i.kind not in ('email','sms')) then return 'Approval is required'; end if;
 if r.email_log_id is not null and not exists(select 1 from email_log where id=r.email_log_id and tenant_id=p_tenant and status='pending') then return 'Draft is no longer pending'; end if;
 if p_auto and not coalesce((select coalesce(mc.auto_send,tc.auto_send,false) from tenant_config tc
    left join mailbox_config mc on mc.tenant_id=tc.tenant_id and mc.mailbox_id=nullif(r.prepared->>'mailbox_id','')::uuid
    where tc.tenant_id=p_tenant),false) then return 'Mailbox requires approval'; end if;
 if i.kind='email' and not exists(select 1 from email_connections where tenant_id=p_tenant
    and id=nullif(r.prepared->>'mailbox_id','')::uuid and sync_failing_since is null
    and lower(coalesce(nullif(send_as_address,''),address))=r.prepared->>'sender') then return 'Mailbox changed or needs attention'; end if;
 if exists(select 1 from crm_contacts where tenant_id=p_tenant and do_not_sequence
    and (lower(email)=lower(r.prepared->>'to') or lower(email)=lower(r.prepared->'payload'->>'to')
         or id::text=i.payload->>'contact_id')) then return 'Contact is stopped'; end if;
 if exists(select 1 from outreach_suppressions where tenant_id=p_tenant
    and (email=lower(r.prepared->>'to') or email=lower(r.prepared->'payload'->>'to'))) then return 'Contact is stopped'; end if;
 if exists(select 1 from ignored_contacts where tenant_id=p_tenant
    and (lower(address)=lower(r.prepared->>'to') or lower(address)=lower(r.prepared->'payload'->>'to'))) then return 'Contact is ignored'; end if;
 if exists(select 1 from schedule_runs where tenant_id=p_tenant and status in ('executing','unknown')) then return 'Another outcome needs review'; end if;
 local_now:=now() at time zone (s.config->>'timezone');
 if not (s.config->'working_days' @> to_jsonb(array[(extract(isodow from local_now)::integer-1)]))
  or s.config->'holidays' ? to_char(local_now,'YYYY-MM-DD')
  or local_now::time<(s.config->>'start_time')::time or local_now::time>=(s.config->>'end_time')::time then return 'Outside working window'; end if;
 cap:=(s.config->>'daily_cap')::integer;
 if (select count(*) from schedule_runs where tenant_id=p_tenant and attempted_at>now()-interval '24 hours')>=cap then return 'Daily cap reached'; end if;
 if s.last_attempt_at>now()-make_interval(secs=>(s.config->>'min_gap_seconds')::integer) then return 'Minimum gap'; end if;
 update schedule_runs set status='executing',attempted_at=now(),result=null where id=r.id;
 update schedule_settings set last_attempt_at=now() where tenant_id=p_tenant;
 return 'claimed';
end $$;

-- Only preparation that has never created a draft/action may be reclaimed.
create or replace function schedule_retry_prepare(p_tenant uuid,p_run uuid,p_token uuid)
returns setof schedule_runs language plpgsql security invoker set search_path=public as $$
begin
 perform 1 from schedule_settings where tenant_id=p_tenant and enabled for update;
 if not found then return; end if;
 return query update schedule_runs r set status='preparing',token=p_token,lease_until=now()+interval '5 minutes',result=null,finished_at=null
 from schedule_items i where r.tenant_id=p_tenant and r.id=p_run and r.status='blocked' and r.email_log_id is null
 and i.id=r.item_id and i.tenant_id=p_tenant and i.status='active' and i.revision=(r.snapshot->>'revision')::integer
 returning r.*;
end $$;

create or replace function schedule_complete(p_tenant uuid,p_run uuid,p_result text,p_auto boolean)
returns boolean language plpgsql security invoker set search_path=public as $$
declare r schedule_runs;
begin
 select * into r from schedule_runs where tenant_id=p_tenant and id=p_run for update;
 if not found or r.status<>'executing' then return false; end if;
 update schedule_runs set status='done',result=p_result,finished_at=now() where id=r.id;
 if r.email_log_id is not null then
  update email_log set status='sent',sent_at=now(),sent_via=case when p_auto then 'auto' else 'human' end
  where id=r.email_log_id and tenant_id=p_tenant;
 end if;
 return true;
end $$;
create or replace function schedule_reconcile(p_tenant uuid,p_run uuid,p_done boolean)
returns boolean language plpgsql security invoker set search_path=public as $$
declare r schedule_runs;
begin
 perform 1 from schedule_settings where tenant_id=p_tenant and not enabled for update;
 if not found then return false; end if;
 select * into r from schedule_runs where tenant_id=p_tenant and id=p_run for update;
 if not found or r.status not in ('executing','unknown') or r.attempted_at is null
    or r.attempted_at>now()-interval '5 minutes' then return false; end if;
 update schedule_runs set status=case when p_done then 'done' else 'skipped' end,finished_at=now(),
 result=case when p_done then 'Owner verified completion' else 'Closed without retry after review' end where id=r.id;
 if r.email_log_id is not null then
  update email_log set status=case when p_done then 'sent' else 'skipped' end,
   sent_at=case when p_done then r.attempted_at else sent_at end,
   sent_via=case when p_done then 'human' else sent_via end
  where tenant_id=p_tenant and id=r.email_log_id and status='pending';
 end if;
 return true;
end $$;
do $$ declare f regprocedure; begin
 for f in select p.oid::regprocedure from pg_proc p join pg_namespace n on n.oid=p.pronamespace
 where n.nspname='public' and p.proname in ('schedule_reserve','schedule_prepare','schedule_claim','schedule_complete','schedule_retry_prepare','schedule_skip_future','schedule_reconcile') loop
  execute format('revoke all on function %s from public,anon,authenticated',f);
  execute format('grant execute on function %s to service_role',f);
 end loop;
end $$;
commit;

-- Build-request loop
-- Requests approved in /admin/requests are dispatched to a GitHub issue that the
-- build-request workflow picks up. `source` drives review depth (owner builds
-- straight away; tenant/agent get a plan-approval step first). Status is plain
-- text, no CHECK: approved|building|pr_open join new|reviewing|planned|done.
alter table feature_requests add column if not exists source text not null default 'tenant';  -- tenant|agent|owner
alter table feature_requests add column if not exists github_issue integer;
alter table feature_requests add column if not exists github_pr integer;
alter table feature_requests add column if not exists build_events jsonb not null default '[]'::jsonb;  -- storyline: [{at, stage, note, url}]

-- Shared Gmail/Microsoft import state. Apply before deploying the application.
alter table public.email_connections add column if not exists backlog_import jsonb;

-- Atomic across workers and entry points. Only the service role can claim work.
create or replace function public.claim_mailbox_import(p_tenant uuid, p_connection uuid)
returns jsonb language sql set search_path = public as $$
  update public.email_connections
  set backlog_import = jsonb_build_object(
    'state', 'running', 'run_id', gen_random_uuid(),
    'started_at', now(), 'expires_at', now() + interval '15 minutes')
  where tenant_id = p_tenant and id = p_connection
    and (coalesce(backlog_import->>'state', '') <> 'running'
         or (backlog_import->>'expires_at')::timestamptz <= now())
  returning backlog_import;
$$;
revoke all on function public.claim_mailbox_import(uuid, uuid) from public, anon, authenticated;
grant execute on function public.claim_mailbox_import(uuid, uuid) to service_role;

begin;
create table if not exists public.knowledge_reviews (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references public.tenants(id) on delete cascade,
  state text not null default 'pending' check(state in ('pending','saved','discarded')),
  payload jsonb not null,
  created_at timestamptz not null default now()
);
create index if not exists knowledge_reviews_tenant_created on public.knowledge_reviews(tenant_id,created_at,id);
alter table public.knowledge_reviews enable row level security;
revoke all on public.knowledge_reviews from anon, authenticated;
grant select, insert, update on public.knowledge_reviews to service_role;

create or replace function public.knowledge_review_context(p_tenant uuid)
returns jsonb language sql stable set search_path=public as $$
  with context as (select jsonb_build_object(
    'brief', coalesce((select business_brief from tenant_config where tenant_id=p_tenant),''),
    'sources', coalesce((select jsonb_agg(s order by s->>'source',s->>'mailbox_id') from
      (select jsonb_build_object('source',source,'mailbox_id',mailbox_id,'chunks',count(*),
        'content',string_agg(content,E'\n\n' order by created_at,source_chunk_index,id)) s
       from knowledge_chunks where tenant_id=p_tenant group by source,mailbox_id) sources),'[]'::jsonb),
    'reviews', coalesce((select jsonb_agg(to_jsonb(r) order by created_at,id)
      from knowledge_reviews r where tenant_id=p_tenant),'[]'::jsonb)) as data)
  select data || jsonb_build_object('version',md5(data::text)) from context;
$$;

create or replace function public.admit_knowledge_review(
  p_tenant uuid,p_review uuid,p_expected text,p_payload jsonb,p_notes jsonb,p_discard boolean default false)
returns boolean language plpgsql set search_path=public as $$
declare n jsonb; rows jsonb;
begin
  -- Match existing knowledge writers' lock order; the decision and all notes commit together.
  perform 1 from tenant_config where tenant_id=p_tenant for update;
  lock table knowledge_chunks in share row exclusive mode;
  lock table knowledge_reviews in share row exclusive mode;
  if not exists(select 1 from knowledge_reviews where id=p_review and tenant_id=p_tenant and state='pending')
    or knowledge_review_context(p_tenant)->>'version' is distinct from p_expected then
    raise exception 'BLAM8_KNOWLEDGE_REVIEW_STALE';
  end if;
  if p_discard and jsonb_array_length(p_notes)<>0 then raise exception 'BLAM8_SETUP_INVALID'; end if;
  for n in select value from jsonb_array_elements(p_notes) loop
    select jsonb_agg(c || jsonb_build_object('tenant_id',p_tenant,'source',n->>'label',
      'mailbox_id',n->'mailbox_id')) into rows from jsonb_array_elements(n->'chunks') c;
    perform replace_knowledge_source_atomic(p_tenant,n->>'label',(n->>'mailbox_id')::uuid,rows);
  end loop;
  update knowledge_reviews set state=case when p_discard then 'discarded' else 'saved' end,
    payload=p_payload where id=p_review and tenant_id=p_tenant;
  return true;
end $$;
revoke all on function public.knowledge_review_context(uuid) from public,anon,authenticated;
revoke all on function public.admit_knowledge_review(uuid,uuid,text,jsonb,jsonb,boolean) from public,anon,authenticated;
grant execute on function public.knowledge_review_context(uuid) to service_role;
grant execute on function public.admit_knowledge_review(uuid,uuid,text,jsonb,jsonb,boolean) to service_role;
commit;
