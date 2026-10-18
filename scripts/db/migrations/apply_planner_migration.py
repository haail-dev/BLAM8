"""One-shot: apply the transport-planner migration (db/schema.sql planner
section) to prod via the Supabase session pooler, then verify objects and
exercise apply_plan inside a rolled-back transaction — including the
fails-whole-when-overloaded path. Safe to re-run (DDL guarded)."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Transport planner module"


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)

schema = open(SCHEMA_PATH, encoding="utf-8").read()
planner_sql = schema[schema.index(MARKER):]
print(f"planner migration block: {len(planner_sql)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(planner_sql)
print("DDL applied")

# ── verify objects ───────────────────────────────────────────────────────────
cur.execute("select to_regclass('public.postcodes')")
print("postcodes table:", cur.fetchone())
cur.execute("""
  select column_name, is_nullable from information_schema.columns
  where table_name = 'postcodes' and column_name in ('lat','lng')
  order by column_name
""")
print("postcodes lat/lng (nullable for cached misses):", cur.fetchall())
cur.execute("""
  select column_name, column_default from information_schema.columns
  where table_name = 'tenant_config' and column_name = 'planner_auto_apply'
""")
print("tenant_config.planner_auto_apply:", cur.fetchall())
cur.execute("select proname from pg_proc where proname = 'apply_plan'")
print("apply_plan function:", cur.fetchall())
cur.execute("select relrowsecurity from pg_class where relname = 'postcodes'")
print("postcodes RLS on (no policy => service-role only):", cur.fetchone())

# ── exercise apply_plan in rolled-back transactions ──────────────────────────
# Placement note: best-fit picks the SMALLEST remaining space that fits, so a
# 4-space load lands on the empty box (free 6 < 10) and the 5.5 then has only
# the 18t. That gives box 4/6 + 18t 5.5/10 — a real consolidation candidate.
def seed(name):
    cur.execute("insert into tenants (name) values (%s) returning id", (name,))
    tid = cur.fetchone()[0]
    cur.execute("""
      insert into vehicles (tenant_id, name, capacity_spaces)
      values (%s, '18t', 10), (%s, 'box', 6) returning id
    """, (tid, tid))
    big, small = [r[0] for r in cur.fetchall()]
    cur.execute("select id, vehicle_id from hold_fleet_booking(%s, '2026-06-22', 'box load', 4)", (tid,))
    b_small, v1 = cur.fetchone()
    cur.execute("select id, vehicle_id from hold_fleet_booking(%s, '2026-06-22', 'big load', 5.5)", (tid,))
    b_big, v2 = cur.fetchone()
    assert v1 == small and v2 == big, (v1, v2)
    return tid, big, small, b_small, b_big


def plan_for(tid, booking, target, summary):
    cur.execute("""
      insert into plans (tenant_id, plan_date, summary, moves)
      values (%s, '2026-06-22', %s,
              jsonb_build_array(jsonb_build_object('booking_id', %s::text, 'to_vehicle_id', %s::text)))
      returning id
    """, (tid, summary, booking, target))
    return cur.fetchone()[0]


conn.autocommit = False
try:
    try:
        tid, big, small, b_small, b_big = seed('planner-smoke')
        # Good plan: move the box's 4 onto the 18t (5.5+4=9.5 fits) → applied.
        plan_id = plan_for(tid, b_small, big, 'consolidate onto the 18t')
        cur.execute("select status from apply_plan(%s, %s)", (plan_id, tid))
        print("good plan applied:", cur.fetchone())
        cur.execute("select vehicle_id from bookings where id = %s", (b_small,))
        assert cur.fetchone()[0] == big
        cur.execute("select status from apply_plan(%s, %s)", (plan_id, tid))
        raise SystemExit("re-applying an applied plan should have raised")
    except psycopg2.errors.RaiseException as e:
        print("re-apply correctly rejected:", str(e).splitlines()[0])
        conn.rollback()

    # Overload: a plan that no longer fits must fail whole and change nothing.
    try:
        tid, big, small, b_small, b_big = seed('planner-smoke-2')
        plan_id = plan_for(tid, b_big, small, 'would overload the box')
        cur.execute("select status from apply_plan(%s, %s)", (plan_id, tid))
        raise SystemExit("overloading plan should have raised")
    except psycopg2.errors.RaiseException as e:
        print("overload correctly rejected whole:", str(e).splitlines()[0])
        conn.rollback()
finally:
    conn.rollback()
    print("rolled back — prod untouched by the smoke test")

conn.close()
print("DONE")
