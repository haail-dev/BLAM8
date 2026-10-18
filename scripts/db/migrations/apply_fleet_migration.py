"""One-shot: apply the fleet-module migration (db/schema.sql fleet section) to prod
via the Supabase session pooler, then verify objects + smoke-test the RPC inside a
rolled-back transaction. Safe to re-run (DDL is guarded / create-or-replace)."""
import re
import sys

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Fleet / pallet capacity module"


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)

schema = open(SCHEMA_PATH, encoding="utf-8").read()
fleet_sql = schema[schema.index(MARKER):]
print(f"fleet migration block: {len(fleet_sql)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(fleet_sql)
print("DDL applied")

# ── verify objects ───────────────────────────────────────────────────────────
cur.execute("""
  select to_regclass('public.branches'), to_regclass('public.vehicles'),
         to_regclass('public.pallet_types')
""")
print("tables:", cur.fetchone())
cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'bookings' and column_name in ('vehicle_id','spaces','weight_kg')
  order by column_name
""")
print("bookings cols:", [r[0] for r in cur.fetchall()])
cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'mailbox_config' and column_name = 'default_branch_id'
""")
print("mailbox_config col:", [r[0] for r in cur.fetchall()])
cur.execute("select proname from pg_proc where proname = 'hold_fleet_booking'")
print("rpc:", [r[0] for r in cur.fetchall()])
cur.execute("""
  select tablename, policyname from pg_policies
  where tablename in ('branches','vehicles','pallet_types') order by tablename
""")
print("policies:", cur.fetchall())

# ── RPC smoke test inside one rolled-back transaction ───────────────────────
conn.autocommit = False
try:
    cur.execute("insert into tenants (name) values ('fleet-smoke') returning id")
    tid = cur.fetchone()[0]
    cur.execute("""
      insert into branches (tenant_id, name) values (%s, 'Leeds') returning id
    """, (tid,))
    br = cur.fetchone()[0]
    cur.execute("""
      insert into vehicles (tenant_id, branch_id, name, capacity_spaces, max_payload_kg)
      values (%s, %s, 'small', 4, 1000), (%s, %s, 'big', 10, 9000)
      returning id, name
    """, (tid, br, tid, br))
    vans = {name: vid for vid, name in cur.fetchall()}

    def hold(spaces, weight=None, branch=None, status='proposed'):
        cur.execute("""
          select vehicle_id, spaces from hold_fleet_booking(
            %s, '2026-06-19', 'smoke', %s, %s, %s, null, 'ops@x.co', 'email', null, %s)
        """, (tid, spaces, weight, branch, status))
        return cur.fetchall()

    r1 = hold(3)                      # best-fit → small (4 free < 10 free)
    assert r1 and str(r1[0][0]) == str(vans["small"]), f"best-fit picked {r1}"
    r2 = hold(3)                      # small now 1 free → big
    assert r2 and str(r2[0][0]) == str(vans["big"]), f"second hold picked {r2}"
    r3 = hold(8)                      # big has 7 free, small 1 → nothing fits
    assert r3 == [], f"overflow should be empty, got {r3}"
    r4 = hold(1, weight=950)          # small 1 free but 1000kg limit already... no prior weight: fits small
    assert r4 and str(r4[0][0]) == str(vans["small"]), f"weight hold picked {r4}"
    r5 = hold(1, weight=200)          # small full on kg (950 used) and spaces (4 used)... → big
    assert r5 and str(r5[0][0]) == str(vans["big"]), f"kg-constrained hold picked {r5}"
    cur.execute("update vehicles set active = false where id = %s", (vans["big"],))
    r6 = hold(2)                      # big inactive, small full → nothing
    assert r6 == [], f"inactive vehicle must take no new holds, got {r6}"
    print("RPC smoke test: best-fit, overflow, weight cap, deactivation all OK")
finally:
    conn.rollback()
    print("rolled back — prod untouched by the smoke test")

conn.close()
print("DONE")
