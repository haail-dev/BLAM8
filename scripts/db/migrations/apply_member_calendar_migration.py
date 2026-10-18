"""One-shot: a person's own calendar (docs/plan-people.md slice 5).

Kris, 2026-09-13, watching his wife accept an invitation and seeing Google name
Calendar on the consent screen: "she can see the business Calendar in BLAM8
while having her own Calendar to have events synced to… another calendar for
the business, mostly used to delegate work from the main calendar."

Today the diary syncs to ONE business calendar (email_connections.
calendar_connected_at, made by the owner's Connect), and a person's sign-in
tokens live in Supabase Auth where the app uses them for identity and nothing
else. This adds a per-person calendar connection so a staff member's own work
reaches the calendar they already live in.

  member_calendars    — one row per person who has connected one. Tokens are
                        encrypted at rest by src/app/crypto.py exactly as the
                        mailbox's are; this table is service-role only.
  oauth_states.user_id — whose calendar the consent round-trip is for, bound
                        server-side so the callback never trusts the URL.
  member_booking_events — the event id THIS person's calendar got for a
                        booking, kept apart from the business calendar's own
                        event id so removing one never touches the other.

⚠ Scope note: this flow asks Google for `calendar.events` ONLY — no mail scope
at all. That makes it SENSITIVE rather than restricted, but it still DRAWS ON
THE SAME 100 pre-CASA grants: Google's unverified-app cap counts users who
authorise an unapproved sensitive *or* restricted scope, over the project's
whole lifetime, and it cannot be reset. src/app/slots.py therefore meters
member_calendars alongside email_connections, and the connect is refused at a
full cap. (An earlier note in docs/plan-people.md said this cost no slot; that
was true of our own counter and not of Google's.)

Applied via the Supabase session pooler like the People migration; safe to
re-run.
"""
import re

import psycopg2

ENV_PATH = ".env"
DDL = """
create table if not exists member_calendars (
  tenant_id       uuid not null references tenants(id) on delete cascade,
  user_id         uuid not null,
  provider        text not null default 'google',
  access_token    text,
  refresh_token   text,
  token_expires   timestamptz,
  calendar_email  text,
  connected_at    timestamptz not null default now(),
  last_error      text,
  primary key (tenant_id, user_id)
);
alter table member_calendars enable row level security;

alter table oauth_states add column if not exists user_id uuid;
alter table oauth_states add column if not exists mode text not null default 'replace';

create table if not exists member_booking_events (
  tenant_id   uuid not null references tenants(id) on delete cascade,
  user_id     uuid not null,
  booking_id  uuid not null,
  event_id    text not null,
  created_at  timestamptz not null default now(),
  primary key (tenant_id, user_id, booking_id)
);
create index if not exists member_booking_events_booking_idx
  on member_booking_events (tenant_id, booking_id);
alter table member_booking_events enable row level security;
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
for t in ("member_calendars", "member_booking_events", "oauth_states"):
    cur.execute("select column_name from information_schema.columns "
                "where table_name=%s order by ordinal_position", (t,))
    print(f"{t}:", [r[0] for r in cur.fetchall()])
cur.close(); conn.close()
