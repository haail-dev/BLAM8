"""One-shot: apply the Field Service (Trades) module migration (db/schema.sql FSM
section) to prod via the Supabase session pooler, then verify objects + smoke-test
the hold_engineer_booking RPC inside a rolled-back transaction. Safe to re-run (DDL is
guarded / create-or-replace). Mirrors scripts/db/migrations/apply_fleet_migration.py."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Field Service (Trades) module"


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)

schema = open(SCHEMA_PATH, encoding="utf-8").read()
fsm_sql = schema[schema.index(MARKER):]
print(f"FSM migration block: {len(fsm_sql)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(fsm_sql)
print("DDL applied")

# ── verify objects ───────────────────────────────────────────────────────────
cur.execute("""
  select to_regclass('public.engineers'), to_regclass('public.job_types')
""")
print("tables:", cur.fetchone())
cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'bookings'
    and column_name in ('engineer_id','duration_minutes','job_type','required_skill','priority','service_due_date')
  order by column_name
""")
print("bookings cols:", [r[0] for r in cur.fetchall()])
cur.execute("select proname from pg_proc where proname = 'hold_engineer_booking'")
print("rpc:", [r[0] for r in cur.fetchall()])
cur.execute("""
  select tablename, policyname from pg_policies
  where tablename in ('engineers','job_types') order by tablename
""")
print("policies:", cur.fetchall())

# ── RPC smoke test inside one rolled-back transaction ───────────────────────
conn.autocommit = False
try:
    cur.execute("insert into tenants (name) values ('fsm-smoke') returning id")
    tid = cur.fetchone()[0]
    cur.execute("insert into branches (tenant_id, name) values (%s, 'Bradford') returning id", (tid,))
    br = cur.fetchone()[0]
    cur.execute("""
      insert into engineers (tenant_id, branch_id, name, skills, daily_minutes) values
        (%s, %s, 'Sam (gas)',  '{gas,plumbing}', 480),
        (%s, %s, 'Alex (gen)', '{plumbing}',     240)
      returning id, name
    """, (tid, br, tid, br))
    eng = {name: eid for eid, name in cur.fetchall()}

    def hold(duration, skill=None, status='proposed'):
        cur.execute("""
          select engineer_id, duration_minutes from hold_engineer_booking(
            %s, '2026-07-02', 'job', %s, %s, 'service', 'standard', null, null,
            'cust@x.co', 'email', null, %s)
        """, (tid, duration, skill, status))
        return cur.fetchall()

    # 240 min, plumbing: both qualify; best-fit picks the TIGHTER day (Alex, 240 free).
    r1 = hold(240, 'plumbing')
    assert r1 and str(r1[0][0]) == str(eng["Alex (gen)"]), f"best-fit picked {r1}"
    # Alex now full (0 free); another 60-min plumbing job → Sam.
    r2 = hold(60, 'plumbing')
    assert r2 and str(r2[0][0]) == str(eng["Sam (gas)"]), f"second hold picked {r2}"
    # A gas job: only Sam holds the skill; Sam has 420 free → Sam.
    r3 = hold(120, 'gas')
    assert r3 and str(r3[0][0]) == str(eng["Sam (gas)"]), f"skill-gated hold picked {r3}"
    # A skill nobody has → no engineer fits.
    r4 = hold(60, 'electrical')
    assert r4 == [], f"unmatched skill should be empty, got {r4}"
    # Sam now has 420-120=300 free; a 360-min job exceeds every engineer → empty.
    r5 = hold(360, 'plumbing')
    assert r5 == [], f"over-capacity job should be empty, got {r5}"
    # Deactivate Sam; a 60-min gas job now has no active skilled engineer → empty.
    cur.execute("update engineers set active = false where id = %s", (eng["Sam (gas)"],))
    r6 = hold(60, 'gas')
    assert r6 == [], f"inactive engineer must take no new holds, got {r6}"
    print("RPC smoke test: best-fit, skill gating, capacity, deactivation all OK")
finally:
    conn.rollback()
    print("rolled back — prod untouched by the smoke test")

conn.close()
print("DONE")
