"""One-shot: apply the Stripe billing section of db/schema.sql to prod (the tenants
stripe_* columns + grandfather backfill + stripe_events table). These shipped in
schema.sql with the billing/pricing commits but were never applied to the prod
`tenants` table, so db.get_tenant (which SELECTs them) 500s on every dashboard
page. Idempotent (add column if not exists / create if not exists). Mirrors the
other apply_*_migration scripts."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
START = "-- Stripe billing (subscription lifecycle"
END = "-- Transport planner module"


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)

schema = open(SCHEMA_PATH, encoding="utf-8").read()
billing_sql = schema[schema.index(START):schema.index(END)]
print(f"billing migration block: {len(billing_sql)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(billing_sql)
print("DDL applied")

cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'tenants'
    and column_name in ('stripe_customer_id','stripe_subscription_id','subscription_status',
                        'subscription_price_id','trial_ends_at','current_period_end','billing_provider')
  order by column_name
""")
print("tenants billing cols:", [r[0] for r in cur.fetchall()])
cur.execute("select to_regclass('public.stripe_events')")
print("stripe_events:", cur.fetchone()[0])
cur.execute("select subscription_status, billing_provider, count(*) from tenants group by 1,2 order by 1,2")
print("tenants by status/provider:", cur.fetchall())

conn.close()
print("DONE")
