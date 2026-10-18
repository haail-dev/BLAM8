"""One-shot: apply the partner settlement migration (db/schema.sql, the
'-- Settlement preference' section through partner_withdrawals) to prod via the
Supabase session pooler, then verify.

Adds: partners.settlement_mode + partners.credit_tenant_id, partner_payouts.method,
and the partner_withdrawals table with its one-open-request-per-partner unique index.

Safe to re-run — every statement is 'add column if not exists' / 'create ... if not
exists', and nothing existing changes behaviour: settlement_mode defaults to 'cash'
(what every current partner signed up to) and payouts default to method 'bank'.

Run from the repo root:  python scripts/db/migrations/apply_partner_settlement_migration.py
"""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
START = "-- Settlement preference (2026-07-30)"
END = "-- Lead eligibility at registration time"


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)

schema = open(SCHEMA_PATH, encoding="utf-8").read()
block = schema[schema.index(START):schema.index(END)]
print(f"partner-settlement migration block: {len(block)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(block)
print("DDL applied")

cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'partner_withdrawals' order by ordinal_position
""")
print("partner_withdrawals columns:", [r[0] for r in cur.fetchall()])

cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'partners' and column_name in ('settlement_mode', 'credit_tenant_id')
""")
print("partners new columns:", sorted(r[0] for r in cur.fetchall()))

cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'partner_payouts' and column_name = 'method'
""")
print("partner_payouts.method present:", bool(cur.fetchall()))

# The unique partial index is what stops a double-submit opening two requests, so
# verify it landed rather than trusting the DDL ran.
cur.execute("""
  select indexname from pg_indexes
  where tablename = 'partner_withdrawals' and indexname = 'idx_partner_withdrawals_one_open'
""")
print("one-open-request index present:", bool(cur.fetchall()))

# Existing partners must be on cash — nobody gets silently switched into credit.
cur.execute("select settlement_mode, count(*) from partners group by settlement_mode")
print("partners by settlement_mode:", cur.fetchall())

conn.close()
print("DONE")
