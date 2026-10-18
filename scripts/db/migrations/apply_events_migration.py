# AI-Generated | Claude (Anthropic) | ForgeGuard | 2026-07-05
"""One-shot: apply the Events OS schema (docs/os/events.md §4) — the FULL set now,
so the later phases (the multi-stakeholder chase engine, run-of-show, integrations)
need no new migration. Only Phase A (crm_events + the self-filling capture hook +
the board) has an engine behind it today; crm_event_guests / crm_event_stakeholders
are created additively and their engines ship in Phases B/C.

An `event` IS a crm_deals row (1:1) enriched — the entire shipped CRM machine
(activity feed, promises, drafter context, nudges, reporting) works on events for
free. These three tables add the event-shaped fields, the guest list (NOT contacts —
§14) and the stakeholder set the chase engine will drive (§12).

Safe to re-run (CREATE TABLE IF NOT EXISTS / CREATE UNIQUE INDEX IF NOT EXISTS —
never a table-level unique(lower(...)) constraint, which Postgres rejects; the
Storefront migration was bitten by that, so the 1:1 deal_id uniqueness is a
CREATE UNIQUE INDEX below, not a UNIQUE table constraint)."""
import re

import psycopg2

ENV_PATH = ".env"

MIGRATION_SQL = """
-- ── Events OS §4: the event primitive (1:1 with a crm_deals row) ───────────────
CREATE TABLE IF NOT EXISTS crm_events (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  deal_id           uuid NOT NULL REFERENCES crm_deals(id) ON DELETE CASCADE,
  event_date        date,                        -- the spine field; NULL until pinned
  event_type        text,                        -- wedding | corporate | conference | private | other
  guest_count       integer,                     -- current best headcount (firmed over planning)
  guest_count_final boolean NOT NULL DEFAULT false,
  timings           jsonb NOT NULL DEFAULT '[]'::jsonb,  -- [{"label":"Arrival","time":"13:00"}, ...] run-of-show spine
  drink_licence     text,                        -- 'held' | 'applied' | 'not_required' | NULL(unknown)
  contingency_notes text,                        -- wet-weather plan, backup supplier, etc.
  run_of_show       jsonb NOT NULL DEFAULT '{}'::jsonb,  -- assembled sheet: {"lines":[...], "gaps":[...], "assembled_at":...}
  status            text NOT NULL DEFAULT 'planning', -- mirrors the deal; convenience for the board query
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now()
);
-- One event per deal (1:1). Functional/plain unique keys are a CREATE UNIQUE INDEX,
-- never a table-level unique(...) constraint (Postgres rejects unique(lower(...));
-- Storefront was bitten) — so the deal_id uniqueness is this index, not a constraint.
CREATE UNIQUE INDEX IF NOT EXISTS uq_crm_events_deal ON crm_events (deal_id);
-- The "this week" range query keys entirely off event_date.
CREATE INDEX IF NOT EXISTS idx_crm_events_tenant_date
  ON crm_events (tenant_id, event_date);

-- ── Guest list (NOT crm_contacts — guests aren't the venue's customers, §14) ───
CREATE TABLE IF NOT EXISTS crm_event_guests (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  event_id      uuid NOT NULL REFERENCES crm_events(id) ON DELETE CASCADE,
  name          text,
  party_size    integer NOT NULL DEFAULT 1,      -- "+1", a family of 4, a table
  rsvp          text NOT NULL DEFAULT 'unknown', -- unknown | yes | no | maybe
  dietary       text,                            -- free text: 'vegan', 'nut allergy', 'GF'
  dietary_tags  jsonb NOT NULL DEFAULT '[]'::jsonb,  -- normalised: ["vegan"], ["gluten_free","nut_allergy"]
  table_name    text,                            -- optional seating hint (v1: freeform)
  source_email  text,                            -- email_log id the guest/dietary was extracted from
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_crm_event_guests_event
  ON crm_event_guests (tenant_id, event_id);
-- Idempotent guest upsert keys on (event_id, lower(name)) so re-extraction from a
-- later email reinforces one guest rather than duplicating — a functional unique
-- key, hence a CREATE UNIQUE INDEX (never a table-level unique(lower(...))). A NULL
-- name (an anonymous dietary count) is exempt: Postgres treats NULLs as distinct.
CREATE UNIQUE INDEX IF NOT EXISTS uq_crm_event_guests_name
  ON crm_event_guests (tenant_id, event_id, lower(name));

-- ── Stakeholders: the suppliers + assigned staff the chase engine drives (§12) ─
-- Created now (the Phase-B chase substrate); Phase A only stores + lists them.
CREATE TABLE IF NOT EXISTS crm_event_stakeholders (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id      uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  event_id       uuid NOT NULL REFERENCES crm_events(id) ON DELETE CASCADE,
  kind           text NOT NULL,                  -- supplier | staff
  role           text NOT NULL,                  -- 'caterer' | 'bar' | 'florist' | 'AV' | 'waiting_staff' | ...
  name           text,
  email          text,                           -- who we chase (supplier contact or staff member)
  contact_id     uuid REFERENCES crm_contacts(id) ON DELETE SET NULL, -- if they're already a CRM contact
  confirmation   text NOT NULL DEFAULT 'pending',-- pending | confirmed | declined | needs_info
  outstanding    text,                           -- the item we're waiting on: 'final numbers', 'licence', 'call time'
  outstanding_promise_id uuid REFERENCES crm_promises(id) ON DELETE SET NULL, -- the tracked chase (§12)
  last_chased_at timestamptz,
  chase_count    integer NOT NULL DEFAULT 0,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_crm_event_stakeholders_event
  ON crm_event_stakeholders (tenant_id, event_id, confirmation);
-- Idempotent stakeholder upsert keys on (event_id, kind, lower(role)) so re-extraction
-- of the same supplier/staff role from a later email reinforces one row rather than
-- duplicating — a functional unique key, hence a CREATE UNIQUE INDEX (never a table-
-- level unique(lower(...))), matching the guest-name index above.
CREATE UNIQUE INDEX IF NOT EXISTS uq_crm_event_stakeholders_role
  ON crm_event_stakeholders (tenant_id, event_id, kind, lower(role));

-- ── RLS on all three (identical predicate to every crm_* table) ────────────────
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
"""


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)

print(f"Events OS migration: {len(MIGRATION_SQL)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(MIGRATION_SQL)
print("DDL applied")

# Verify — every new table + the crm_events.deal_id 1:1 column.
for table in ("crm_events", "crm_event_guests", "crm_event_stakeholders"):
    cur.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_name = %s;", (table,))
    assert cur.fetchone(), f"{table} table missing"

cur.execute("""
  SELECT column_name FROM information_schema.columns
  WHERE table_name = 'crm_events' AND column_name = 'deal_id';
""")
assert cur.fetchone(), "deal_id column missing on crm_events"

print("Events OS schema verified OK")
cur.close()
conn.close()
