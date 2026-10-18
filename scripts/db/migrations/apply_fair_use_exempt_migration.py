"""One-shot: tenants.fair_use_exempt (Kris 2026-09-23, after hitting his own
Concierge daily limit on the rig).

A per-account switch, set from the admin console beside the comp grant, that
lifts the Concierge's daily fair-use allowance (src/app/fairuse.py). It is NOT the
comp grant itself: comp is free access, and LittleDogs (a real customer) is
comped too — lifting the cost guard for every comped account would lift it for
them. Off by default; nothing changes until an operator ticks it.

Additive and guarded; safe to re-run. Apply BEFORE the code that selects the
column deploys (db.get_tenant names its columns). psycopg2 via the session
pooler, DB_PASSWORD read from .env (a commented-out line still counts).
"""
import re

import psycopg2

ENV_PATH = ".env"

MIGRATION_SQL = """
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS fair_use_exempt boolean NOT NULL DEFAULT false;
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
  WHERE table_name = 'tenants' AND column_name = 'fair_use_exempt';
""")
print("tenants.fair_use_exempt:", cur.fetchall())
cur.execute("SELECT count(*) FROM tenants WHERE fair_use_exempt")
print("exempt tenants:", cur.fetchone()[0])
