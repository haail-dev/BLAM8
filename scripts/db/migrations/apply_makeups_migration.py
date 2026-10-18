"""One-shot: make-ups (src/app/makeups.py, slice 6, 2026-09-05) — the `makeups`
table (a one-off booking bound to a missed occurrence) and
scheduling_config.makeups jsonb. Applied via the Supabase session pooler like
scripts/db/migrations/apply_customer_log_migration.py; safe to re-run."""
import re

import psycopg2

ENV_PATH = ".env"
DDL = """
create table if not exists makeups (
  id                uuid primary key default gen_random_uuid(),
  tenant_id         uuid not null references tenants(id) on delete cascade,
  contact_email     text,
  instance_id       uuid,
  missed_booking_id uuid not null,
  missed_ref        text,
  makeup_booking_id uuid not null,
  makeup_ref        text,
  term_key          text,
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

alter table scheduling_config add column if not exists makeups jsonb not null default '{}'::jsonb;
"""


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)
conn = psycopg2.connect(host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
                        user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require")
conn.autocommit = True
cur = conn.cursor()
cur.execute(DDL)
cur.execute("select to_regclass('public.makeups')")
print("makeups", cur.fetchone())
cur.execute("select column_name from information_schema.columns where table_name='scheduling_config' and column_name='makeups'")
print("scheduling_config:", cur.fetchall())
cur.close(); conn.close()
