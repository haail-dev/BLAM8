"""One-shot: who's on the job (docs/plan-people-team-and-jobs.md step 2).

Kris, 2026-09-24: "more than one per job (should be configurable — agnostic)",
and the test: "delegate the next meet & greet to Em".

  booking_people  — a person on a booking: person_id is the team_people row
                    (with or without a login), part is the business's own
                    word for their share ("drop-off", "collection", "on site"),
                    optional. One row per person per booking. Service role.
  scheduling_config.people_per_job — 'one' (default: putting someone on a job
                    replaces whoever was on it) or 'several'.

The rig (Little Paws) is set to 'several' — the kennels preset: Kristie on
the drop-off, Em on the collection. Idempotent; safe to re-run.
"""
import re

import psycopg2

ENV_PATH = ".env"
DDL = """
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
update scheduling_config set people_per_job = 'several'
  where tenant_id = 'fb4081e6-4786-4a93-b090-bf85c5142727';
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
cur.execute("select column_name from information_schema.columns where table_name='booking_people' "
            "order by ordinal_position")
print("booking_people:", [r[0] for r in cur.fetchall()])
cur.execute("select tenant_id, people_per_job from scheduling_config where people_per_job <> 'one'")
print("several:", cur.fetchall())
cur.close(); conn.close()
