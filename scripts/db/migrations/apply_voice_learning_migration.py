"""One-shot: apply the voice-auto-learning migration (db/schema.sql 'Voice
auto-learning from sent mail' section) to prod via the Supabase session pooler,
then verify. Adds the tenant_config columns that hold the learned voice profile,
its lifecycle status/provenance, the sample draft, and voice_notes (the learned
style description the drafter consumes). Safe to re-run."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Voice auto-learning from sent mail (2026-07-25)"


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)

schema = open(SCHEMA_PATH, encoding="utf-8").read()
block = schema[schema.index(MARKER):]
print(f"voice-learning migration block: {len(block)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(block)
print("DDL applied")

# ── verify ────────────────────────────────────────────────────────────────────
cur.execute("""
  select column_name, data_type from information_schema.columns
  where table_name = 'tenant_config'
    and column_name in ('voice_profile','voice_profile_source','voice_learning_status',
                        'voice_sample_draft','voice_emails_analysed','voice_learned_at',
                        'voice_notes')
  order by column_name
""")
rows = cur.fetchall()
print("new columns:", rows)
assert len(rows) == 7, f"expected 7 columns, got {len(rows)}"

conn.close()
print("DONE")
