"""One-shot: job sheets for people who don't sign in (src/app/job_sheets.py).

Kris, 2026-09-25: "if they never touch the app, how do they get the info to
do the job?" — a person on a job with a mobile or an email is sent the job
sheet: a text or an email with a private link to the job (/j/{token}), where
they tap On my way and Done. One row per person per booking; the row outlives
their being taken off the job so the "you're off this one" can be sent.
Idempotent; safe to re-run.
"""
import re

import psycopg2

ENV_PATH = ".env"
DDL = """
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
cur.execute("select column_name from information_schema.columns where table_name='job_sheets' "
            "order by ordinal_position")
print("job_sheets:", [r[0] for r in cur.fetchall()])
cur.close(); conn.close()
