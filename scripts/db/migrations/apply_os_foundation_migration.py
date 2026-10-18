# AI-Generated | Claude (Anthropic) | ForgeGuard | 2026-07-05
"""One-shot: apply the OS-foundation schema — the `operating_system` "shape"
column on tenants + an idempotent backfill from industry.
Safe to re-run (ADD COLUMN IF NOT EXISTS + WHERE operating_system IS NULL)."""
import re

import psycopg2

ENV_PATH = ".env"

MIGRATION_SQL = """
-- Business "shape" derived from industry (docs/BLAM8_operating_systems.md §6).
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS operating_system text;

-- Idempotent backfill — only fills the gap, never clobbers an already-set OS.
UPDATE tenants SET operating_system = CASE
  WHEN industry IN ('logistics','trades','construction') THEN 'field'
  WHEN industry = 'recruitment' THEN 'match'
  WHEN industry = 'wholesale'   THEN 'wholesale'
  WHEN industry IN ('lettings','hire') THEN 'assets'
  WHEN industry = 'events'      THEN 'events'
  ELSE operating_system END
WHERE operating_system IS NULL;
"""


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)

print(f"OS-foundation migration: {len(MIGRATION_SQL)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(MIGRATION_SQL)
print("DDL applied")

# Verify
cur.execute("""
  SELECT column_name FROM information_schema.columns
  WHERE table_name = 'tenants' AND column_name = 'operating_system';
""")
assert cur.fetchone(), "operating_system column missing on tenants"

print("OS-foundation schema verified OK")
cur.close()
conn.close()
