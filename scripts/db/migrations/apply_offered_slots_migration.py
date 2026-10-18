"""One-shot: apply the offered-slot holds migration (docs/modules/module-smart-booking-offers.md)
to prod via the Supabase session pooler, then verify. Three parts:

  1. the appended DDL block (bookings.expires_at + scheduling_config.offer_ttl_hours),
  2. drop the OLD 9-arg hold_booking_if_capacity signature — `create or replace` with an
     extra defaulted arg would leave both versions live and PostgREST refuses the
     ambiguous RPC call,
  3. re-run the in-place hold_booking_if_capacity + move_booking_if_capacity definitions
     from db/schema.sql (now offered-aware, with p_expires_at).

Purely additive on data — no existing row touched. Safe to re-run. Needs DB_PASSWORD in
.env (the session-pooler password). Mirrors scripts/db/migrations/apply_slotgrid_migration.py."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Smart booking: offered-slot holds (docs/modules/module-smart-booking-offers.md, 2026-07-31)"


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


def function_ddl(schema: str, name: str) -> str:
    """Extract one `create or replace function <name> ... end $$;` block."""
    start = schema.index(f"create or replace function {name}")
    end = schema.index("$$;", start) + len("$$;")
    return schema[start:end]


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)

schema = open(SCHEMA_PATH, encoding="utf-8").read()
block = schema[schema.index(MARKER):]
hold_fn = function_ddl(schema, "hold_booking_if_capacity")
move_fn = function_ddl(schema, "move_booking_if_capacity")
print(f"migration block: {len(block)} chars; hold fn: {len(hold_fn)}; move fn: {len(move_fn)}")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(block)
print("columns applied")
cur.execute("""
  drop function if exists hold_booking_if_capacity(
    uuid, date, text, text, jsonb, text, text, text, text)
""")
cur.execute(hold_fn)
cur.execute(move_fn)
print("functions replaced")

cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'bookings' and column_name = 'expires_at'
""")
print("bookings col:", [r[0] for r in cur.fetchall()])
cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'scheduling_config' and column_name = 'offer_ttl_hours'
""")
print("scheduling_config col:", [r[0] for r in cur.fetchall()])
cur.execute("""
  select count(*) from pg_proc where proname = 'hold_booking_if_capacity'
""")
print("hold_booking_if_capacity overloads (must be 1):", cur.fetchone()[0])
conn.close()
print("DONE")
