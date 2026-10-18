"""One-shot: courses, the waiting list and re-enrolment (src/app/courses.py,
2026-09-05) — `waitlist`, `course_notices`; `courses` jsonb on scheduling_config.
Applied via the Supabase session pooler like scripts/db/migrations/apply_wallet_migration.py;
safe to re-run."""
import re

import psycopg2

ENV_PATH = ".env"
DDL = """
create table if not exists waitlist (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id) on delete cascade,
  contact_id    uuid,
  contact_email text not null,
  instance_id   uuid,
  participant   text,
  series_key    text not null,
  label         text,
  status        text not null default 'waiting',
  note          text,
  source        text not null default 'email',
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

alter table scheduling_config add column if not exists courses jsonb not null default '{}'::jsonb;
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
for t in ("waitlist", "course_notices"):
    cur.execute("select to_regclass(%s)", (f"public.{t}",))
    print(t, cur.fetchone())
cur.execute("select column_name from information_schema.columns where table_name='scheduling_config' and column_name='courses'")
print("scheduling_config:", cur.fetchall())
cur.close(); conn.close()
