"""One-shot: tenant_config.discretion (Kris 2026-09-22) — the filming mode.

A real personal email address reached two published videos because phone
screen-recordings never pass through the recorder's redaction map. Redacting in
CapCut afterwards is the same class of answer as blur bars: manual, per clip,
and one miss is permanent.

This is the upstream fix. When a tenant has `discretion` on, the app masks
email addresses, phone numbers, postcodes and street addresses on screen, so
there is nothing to redact in the first place. Default FALSE: every real tenant
gets the ordinary app and never sees a hint that the mode exists.

Guarded (ADD COLUMN IF NOT EXISTS); safe to re-run. psycopg2 via the session
pooler, DB_PASSWORD read from .env (a commented-out line still counts).
"""
import re

import psycopg2

ENV_PATH = ".env"

MIGRATION_SQL = """
ALTER TABLE tenant_config
  ADD COLUMN IF NOT EXISTS discretion boolean NOT NULL DEFAULT false;
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
  SELECT column_name, data_type, column_default FROM information_schema.columns
  WHERE table_name = 'tenant_config' AND column_name = 'discretion';
""")
print("tenant_config.discretion:", cur.fetchall() or "MISSING")

cur.execute("SELECT count(*) FROM tenant_config WHERE discretion;")
print("tenants with it ON:", cur.fetchone()[0])

cur.close()
conn.close()
