"""One-shot: the promise cleared-by columns (Kris 2026-09-20) — resolved_by_kind /
resolved_by_id / resolved_by_label on crm_promises, so a cleared promise can
show WHAT cleared it (the email, booking, invoice or quote) and link to it.
Guarded (ADD COLUMN IF NOT EXISTS); safe to re-run. Mirrors
scripts/db/migrations/apply_crm_phase35_migration.py: psycopg2 via the session pooler,
DB_PASSWORD read from .env (a commented-out line still counts)."""
import re

import psycopg2

ENV_PATH = ".env"

MIGRATION_SQL = """
ALTER TABLE crm_promises
  ADD COLUMN IF NOT EXISTS resolved_by_kind  text,
  ADD COLUMN IF NOT EXISTS resolved_by_id    text,
  ADD COLUMN IF NOT EXISTS resolved_by_label text;
"""


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(MIGRATION_SQL)
print("DDL applied")

cur.execute("""
  SELECT column_name FROM information_schema.columns
  WHERE table_name = 'crm_promises' AND column_name LIKE 'resolved_by_%'
  ORDER BY column_name;
""")
cols = [r[0] for r in cur.fetchall()]
print("crm_promises cleared-by columns:", cols)
assert cols == ["resolved_by_id", "resolved_by_kind", "resolved_by_label"], cols

cur.execute("""
  SELECT status, resolution_type, count(*) FROM crm_promises
  GROUP BY 1, 2 ORDER BY 1, 2;
""")
print("promise rows by status/resolution_type:", cur.fetchall())
cur.close()
conn.close()
print("DONE")
