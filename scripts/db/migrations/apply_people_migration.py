"""One-shot: People — partners and staff on one BLAM8 (docs/plan-people.md,
Kris 2026-09-13). `tenant_members` gains the columns a person needs beyond the
RLS mapping it was built for, and `member_invites` holds an unaccepted
invitation. Applied via the Supabase session pooler like
scripts/db/migrations/apply_front_desk_migration.py; safe to re-run.

  tenant_members.role         — 'owner' | 'manager' | 'staff' | 'accountant'
                                (was 'owner' | 'member'; existing 'member'
                                rows are read as managers, never demoted)
  tenant_members.scopes       — {"pages": [...], "does": [...], "mailboxes": [...]}
  tenant_members.display_name — what the business calls them
  tenant_members.email        — the address they signed in with, so the People
                                page can name them without reading auth.users
  tenant_members.active       — deactivated, never deleted: history keeps the name
  tenant_members.last_seen_at — the People page's quiet "still with us" column

  member_invites              — one row per unaccepted invitation; the token is
                                the /join/{token} link and the QR. Service role
                                only (RLS on, no policies): a token is a
                                credential and never reaches a browser that
                                isn't following the link.
"""
import re

import psycopg2

ENV_PATH = ".env"
DDL = """
alter table tenant_members add column if not exists scopes       jsonb not null default '{}'::jsonb;
alter table tenant_members add column if not exists display_name text;
alter table tenant_members add column if not exists email        text;
alter table tenant_members add column if not exists active       boolean not null default true;
alter table tenant_members add column if not exists last_seen_at timestamptz;

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
            "where table_name='tenant_members' order by ordinal_position")
print("tenant_members:", [r[0] for r in cur.fetchall()])
cur.execute("select column_name from information_schema.columns "
            "where table_name='member_invites' order by ordinal_position")
print("member_invites:", [r[0] for r in cur.fetchall()])
cur.close(); conn.close()
