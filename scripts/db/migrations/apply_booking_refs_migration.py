"""One-shot: apply the booking-references migration to prod via the pooler —
the new counter table/column/function section, then re-apply BOTH hold RPCs
(their inserts now mint booking_ref). Smoke-tests ref minting + concurrency
shape in a rolled-back transaction. Safe to re-run."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


def block(schema: str, start_marker: str) -> str:
    i = schema.index(start_marker)
    j = schema.index("end $$;", i) + len("end $$;")
    return schema[i:j]


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)
schema = open(SCHEMA_PATH, encoding="utf-8").read()

refs_section = block(schema, "-- Booking references (normal-use fix, 2026-06-11)")
windows_rpc = block(schema, "create or replace function hold_booking_if_capacity")
fleet_rpc = block(schema, "create or replace function hold_fleet_booking")
print(f"sections: refs={len(refs_section)} windows_rpc={len(windows_rpc)} fleet_rpc={len(fleet_rpc)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(refs_section)
cur.execute(windows_rpc)
cur.execute(fleet_rpc)
print("DDL applied")

cur.execute("select to_regclass('public.tenant_counters')")
print("counter table:", cur.fetchone())
cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'bookings' and column_name = 'booking_ref'
""")
print("bookings col:", [r[0] for r in cur.fetchall()])
cur.execute("select proname from pg_proc where proname = 'next_booking_ref'")
print("fn:", [r[0] for r in cur.fetchall()])

# ── smoke test in one rolled-back transaction ────────────────────────────────
conn.autocommit = False
try:
    cur.execute("insert into tenants (name) values ('refs-smoke') returning id")
    tid = cur.fetchone()[0]
    cur.execute("select next_booking_ref(%s), next_booking_ref(%s)", (tid, tid))
    print("first refs:", cur.fetchone())          # BK-1001, BK-1002
    cur.execute("""
      insert into vehicles (tenant_id, name, capacity_spaces) values (%s, 'van', 6)
    """, (tid,))
    cur.execute("select booking_ref from hold_fleet_booking(%s, '2026-06-19', 'smoke', 2)", (tid,))
    print("fleet hold ref:", cur.fetchone())      # BK-1003
    cur.execute("""
      select booking_ref from hold_booking_if_capacity(%s, '2026-06-22', 'day', 'smoke2')
    """, (tid,))
    print("windows hold ref:", cur.fetchone())    # BK-1004
finally:
    conn.rollback()
    print("rolled back — prod untouched by the smoke test")

conn.close()
print("DONE")
