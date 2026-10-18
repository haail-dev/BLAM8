"""One-shot: apply the agent_events migration (db/schema.sql 'Agent activity log'
section) to prod via the Supabase session pooler, verify the objects, then exercise
the table inside a rolled-back transaction. Safe to re-run (DDL is guarded)."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Agent activity log"


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)

schema = open(SCHEMA_PATH, encoding="utf-8").read()
events_sql = schema[schema.index(MARKER):]
print(f"agent_events migration block: {len(events_sql)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(events_sql)
print("DDL applied")

# ── verify objects ───────────────────────────────────────────────────────────
cur.execute("select to_regclass('public.agent_events')")
print("agent_events table:", cur.fetchone())
cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'agent_events' order by ordinal_position
""")
print("columns:", [r[0] for r in cur.fetchall()])
cur.execute("select indexname from pg_indexes where tablename = 'agent_events' order by indexname")
print("indexes:", [r[0] for r in cur.fetchall()])
cur.execute("select policyname from pg_policies where tablename = 'agent_events'")
print("policies:", cur.fetchall())

# ── exercise the table in one rolled-back transaction ─────────────────────────
conn.autocommit = False
try:
    cur.execute("insert into tenants (name) values ('agent-events-smoke') returning id")
    tid = cur.fetchone()[0]
    cur.execute("""
      insert into agent_events (tenant_id, kind, direction, contact, subject, summary,
                                event_date, booking_ref, detail)
      values (%s, 'transport', 'inbound', 'sam@x.co', 'Pallet collection',
              'Asked the transport desk: held BK-9 for 2026-07-02', '2026-07-02', 'BK-9',
              '{"status": "held", "spaces": 6}'::jsonb)
      returning id, kind, event_date, booking_ref, detail
    """, (tid,))
    print("insert round-trip:", cur.fetchone())
    cur.execute("select count(*) from agent_events where tenant_id = %s", (tid,))
    print("rows for tenant:", cur.fetchone()[0])
finally:
    conn.rollback()
    print("rolled back — prod untouched by the smoke test")

conn.close()
print("DONE")
