"""One-shot: add tenant_config.voice_learning_ignored to prod.

The operator's "leave me alone" on the voice onboarding step, kept apart from
voice_learning_status (which records what the learner did, not what the person
decided). Idempotent. Mirrors the other apply_*_migration scripts.
"""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
START = "-- Voice learning: dismissed as an onboarding step (Kris 2026-09-01)"


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)
schema = open(SCHEMA_PATH, encoding="utf-8").read()
sql = schema[schema.index(START):]
print(f"migration block: {len(sql)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(sql)
cur.execute("select count(*) from tenant_config where voice_learning_ignored is false")
print("DDL applied; tenants not ignoring the voice step:", cur.fetchone()[0])
cur.close(); conn.close()
