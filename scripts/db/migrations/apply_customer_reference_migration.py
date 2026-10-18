# AI-Generated | Claude (Anthropic) | ForgeGuard | 2026-09-01
"""One-shot: the customer's own reference (PO / job ticket number) on quotes
and invoices. The competitive research's named leak: FM-company clients issue
PO numbers and every trades app drops them between quote and invoice, forcing
re-keying — here the reference is captured once and rides quote → deposit
invoice → final invoice → PDF → accountant export.
Safe to re-run (ADD COLUMN IF NOT EXISTS throughout)."""
import re

import psycopg2

ENV_PATH = ".env"

MIGRATION_SQL = """
-- The customer's reference, not ours: their PO / works-order / ticket number.
ALTER TABLE quotes ADD COLUMN IF NOT EXISTS customer_reference text;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS customer_reference text;
"""


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)

print(f"customer_reference migration: {len(MIGRATION_SQL)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(MIGRATION_SQL)
print("DDL applied")

for table in ("quotes", "invoices"):
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = 'customer_reference';", (table,))
    assert cur.fetchone(), f"customer_reference column missing on {table}"

print("customer_reference schema verified OK")
cur.close()
conn.close()
