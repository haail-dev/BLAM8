"""One-shot: the team list — people with or without a login (docs/plan-people-team-and-jobs.md step 1).

Kris, 2026-09-24: "Yes build it" (people without a login), and "the Field
Service engineers list should become People". A person on the team no longer
has to be a sign-in: a partner who works the floor but never opens BLAM8, or
an engineer the Field Service diary sends out, is a named person who can be
given work. Signing in becomes something a person HAS, not what makes them one.

  team_people      — one row per person who works here. user_id is set when
                     they have a login (tenant_members keeps doors and scopes;
                     this row mirrors name/role/kinds so every person has ONE
                     id jobs can point at). engineer_id links the Field
                     Service roster row while the FSM code still reads it.
                     A row with no user_id grants no access, ever — there is
                     nothing to sign in with. Service role only.
  member_invites.team_person_id — "Give them a login": the invitation says
                     which team person it is for, so accepting it joins the
                     login to the existing row and their history stays.

Back-fill: every tenant_members row and every engineers row gets its team row.
Idempotent (partial unique indexes + on conflict do nothing); safe to re-run.
Applied via the Supabase session pooler like the other People migrations.
"""
import re

import psycopg2

ENV_PATH = ".env"
DDL = """
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

alter table member_invites add column if not exists team_person_id uuid;

insert into team_people (tenant_id, name, role, user_id, email, does, active)
select m.tenant_id,
       coalesce(nullif(trim(m.display_name), ''), nullif(trim(m.email), ''), 'Owner'),
       coalesce(nullif(m.role, ''), 'staff'),
       m.user_id,
       nullif(trim(m.email), ''),
       coalesce(array(select jsonb_array_elements_text(
                  case when jsonb_typeof(m.scopes->'does') = 'array' then m.scopes->'does'
                       else '[]'::jsonb end)), '{}'),
       coalesce(m.active, true)
from tenant_members m
on conflict (tenant_id, user_id) where user_id is not null do nothing;

insert into team_people (tenant_id, name, role, engineer_id, skills, daily_minutes, active)
select e.tenant_id, e.name, 'staff', e.id, e.skills, e.daily_minutes, e.active
from engineers e
on conflict (tenant_id, engineer_id) where engineer_id is not null do nothing;
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
cur.execute("select column_name from information_schema.columns where table_name='team_people' "
            "order by ordinal_position")
print("team_people:", [r[0] for r in cur.fetchall()])
cur.execute("select count(*), count(user_id), count(engineer_id) from team_people")
print("rows (all, with login, engineers):", cur.fetchone())
cur.execute("select count(*) from tenant_members")
print("tenant_members:", cur.fetchone()[0])
cur.execute("select count(*) from engineers")
print("engineers:", cur.fetchone()[0])
cur.close(); conn.close()
