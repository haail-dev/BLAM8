"""One-shot: the customer-writable log (src/app/customer_log.py, slice 5,
2026-09-05) — `booking_notes` and the portal switch
tenant_config.portal_customer_notes (default ON). Applied via the Supabase
session pooler like scripts/db/migrations/apply_waitlist_migration.py; safe to re-run."""
import re

import psycopg2

ENV_PATH = ".env"
DDL = """
create table if not exists booking_notes (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id) on delete cascade,
  booking_id    uuid not null,
  booking_ref   text,
  contact_email text,
  author        text not null default 'customer',
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

alter table tenant_config add column if not exists portal_customer_notes boolean not null default true;
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
cur.execute("select to_regclass('public.booking_notes')")
print("booking_notes", cur.fetchone())
cur.execute("select column_name from information_schema.columns where table_name='tenant_config' and column_name='portal_customer_notes'")
print("tenant_config:", cur.fetchall())
cur.close(); conn.close()
