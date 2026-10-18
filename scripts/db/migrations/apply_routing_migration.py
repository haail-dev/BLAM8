"""One-shot: apply the route-planning migration (db/schema.sql routing section)
to prod via the Supabase session pooler, then verify objects and exercise the
new columns inside a rolled-back transaction. Safe to re-run (DDL guarded)."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Route planning module"


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)

schema = open(SCHEMA_PATH, encoding="utf-8").read()
routing_sql = schema[schema.index(MARKER):]
print(f"routing migration block: {len(routing_sql)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(routing_sql)
print("DDL applied")

# ── verify objects ───────────────────────────────────────────────────────────
cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'vehicles' and column_name = 'registration'
""")
print("vehicles col:", [r[0] for r in cur.fetchall()])
cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'branches' and column_name in ('address','postcode')
  order by column_name
""")
print("branches cols:", [r[0] for r in cur.fetchall()])
cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'bookings'
    and column_name in ('collection_postcode','delivery_postcode','reference')
  order by column_name
""")
print("bookings cols:", [r[0] for r in cur.fetchall()])
cur.execute("select to_regclass('public.plans')")
print("plans table:", cur.fetchone())
cur.execute("select policyname from pg_policies where tablename = 'plans'")
print("plans policies:", cur.fetchall())

# ── exercise the new columns in one rolled-back transaction ──────────────────
conn.autocommit = False
try:
    cur.execute("insert into tenants (name) values ('routing-smoke') returning id")
    tid = cur.fetchone()[0]
    cur.execute("""
      insert into branches (tenant_id, name, address, postcode)
      values (%s, 'Leeds', '1 Depot Way', 'LS9 8AS') returning id
    """, (tid,))
    br = cur.fetchone()[0]
    cur.execute("""
      insert into vehicles (tenant_id, branch_id, name, registration, capacity_spaces)
      values (%s, %s, '7.5t box', 'YX24 ABC', 6) returning id
    """, (tid, br))
    vid = cur.fetchone()[0]
    cur.execute("""
      select id from hold_fleet_booking(%s, '2026-06-19', 'smoke', 2)
    """, (tid,))
    bid = cur.fetchone()[0]
    cur.execute("""
      update bookings set collection_postcode = 'WF17 9LU',
                          delivery_postcode = 'LS9 8AS', reference = 'PO-1042'
      where id = %s returning collection_postcode, delivery_postcode, reference
    """, (bid,))
    print("booking routing cols round-trip:", cur.fetchone())
    cur.execute("""
      insert into plans (tenant_id, plan_date, summary, moves)
      values (%s, '2026-06-19', 'smoke plan',
              '[{"booking_id": "%s", "to_vehicle_id": "%s"}]'::jsonb)
      returning status
    """ % ("%s", bid, vid), (tid,))
    print("plans insert:", cur.fetchone())
finally:
    conn.rollback()
    print("rolled back — prod untouched by the smoke test")

conn.close()
print("DONE")
