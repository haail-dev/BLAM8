"""One-shot: apply the partner-referral section of db/schema.sql to prod (the
partners table + the tenants attribution columns). Idempotent (create/add column
if not exists). Mirrors the other apply_*_migration scripts."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
START = "-- Partner referral programme (partners + tenant attribution) START"
END = "-- Partner referral programme END"


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)

schema = open(SCHEMA_PATH, encoding="utf-8").read()
sql = schema[schema.index(START):schema.index(END)]
print(f"partner referral migration block: {len(sql)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(sql)
print("DDL applied")

for t in ("partners", "partner_leads", "partner_payouts", "partner_commissions"):
    cur.execute(f"select to_regclass('public.{t}')")
    print(f"{t}:", cur.fetchone()[0])
cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'tenants'
    and column_name in ('referred_by_partner_id','referral_code_used','referred_at')
  order by column_name
""")
print("tenants referral cols:", [r[0] for r in cur.fetchall()])

conn.close()
print("DONE")
