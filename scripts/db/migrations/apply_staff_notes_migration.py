"""One-shot: staff_notes — telling a person something, attached to the work it
is about (docs/plan-people.md slice 3; Kris 2026-09-13: People is "work
delegation and record keeping & quick sharing info").

The thing a WhatsApp group cannot do: the message is fastened to the booking,
the customer or the thread it concerns, it lands on that person's Today, and
the fact that they saw it is kept. Names are denormalised alongside the user
ids so the record still reads properly after somebody leaves — history keeps
the name (src/app/people.py: deactivate, never delete).

Applied via the Supabase session pooler like scripts/db/migrations/apply_people_migration.py;
safe to re-run.
"""
import re

import psycopg2

ENV_PATH = ".env"
DDL = """
create table if not exists staff_notes (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id) on delete cascade,
  from_user     uuid,
  from_name     text,
  to_user       uuid,
  to_name       text,
  body          text not null,
  about_kind    text,                 -- booking | contact | thread
  about_id      text,                 -- booking id / contact email / email_log id
  about_label   text,                 -- what it was about, in words, kept for the record
  about_href    text,                 -- where to open it
  created_at    timestamptz not null default now(),
  seen_at       timestamptz,
  done_at       timestamptz,
  cancelled_at  timestamptz
);
create index if not exists staff_notes_to_idx on staff_notes (tenant_id, to_user, seen_at);
create index if not exists staff_notes_about_idx on staff_notes (tenant_id, about_kind, about_id);
alter table staff_notes enable row level security;
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
cur.execute("select column_name from information_schema.columns "
            "where table_name='staff_notes' order by ordinal_position")
print("staff_notes:", [r[0] for r in cur.fetchall()])
cur.close(); conn.close()
