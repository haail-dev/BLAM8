"""One-shot: packages and the wallet (src/app/wallet.py, 2026-09-05) — wallet_credits,
wallet_spend, booking_attendance; package_key / instance_id on invoice_lines;
credits jsonb on scheduling_config. Applied via the Supabase session pooler
like scripts/db/migrations/apply_agent_events_migration.py; safe to re-run."""
import re

import psycopg2

ENV_PATH = ".env"
DDL = """
create table if not exists wallet_credits (
  id             uuid primary key default gen_random_uuid(),
  tenant_id      uuid not null references tenants(id) on delete cascade,
  contact_id     uuid not null,
  contact_email  text,
  package_key    text,
  package_name   text,
  unit           text not null default 'session',
  quantity       numeric(10,2) not null default 0,
  used           numeric(10,2) not null default 0,
  price          numeric(12,2),
  expires_at     date,
  paid           boolean not null default false,
  invoice_id     uuid,
  instance_id    uuid,
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
  reason     text not null default 'attended',
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
  status     text not null,
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
alter table scheduling_config add column if not exists credits jsonb not null default '{}'::jsonb;
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
for t in ("wallet_credits", "wallet_spend", "booking_attendance"):
    cur.execute("select to_regclass(%s)", (f"public.{t}",))
    print(t, cur.fetchone())
cur.execute("select column_name from information_schema.columns where table_name='invoice_lines' and column_name in ('package_key','instance_id')")
print("invoice_lines:", cur.fetchall())
cur.execute("select column_name from information_schema.columns where table_name='scheduling_config' and column_name='credits'")
print("scheduling_config:", cur.fetchall())
cur.close(); conn.close()
