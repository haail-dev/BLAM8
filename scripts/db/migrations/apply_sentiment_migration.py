# AI-Generated | Claude (Anthropic) | ForgeGuard | 2026-07-18
"""One-shot: apply the CRM sentiment schema — the customer's emotional
temperature, self-filled from every inbound email (rolling state on the
contact + a per-email stamp on the activity timeline).
Safe to re-run (ADD COLUMN IF NOT EXISTS throughout)."""
import re

import psycopg2

ENV_PATH = ".env"

MIGRATION_SQL = """
-- Sentiment (CRM addendum): rolling emotional state on the contact.
-- sentiment: 'positive' | 'neutral' | 'concerned' | 'frustrated' | 'angry'
-- sentiment_trend: 'improving' | 'steady' | 'worsening' (vs the previous read)
-- sentiment_reason: the short trigger phrase that earned the read
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS sentiment text;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS sentiment_trend text;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS sentiment_reason text;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS sentiment_updated_at timestamptz;

-- Per-email stamp: the timeline shows the emotional arc, not just the state.
ALTER TABLE crm_activities ADD COLUMN IF NOT EXISTS sentiment text;
"""


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)

print(f"CRM sentiment migration: {len(MIGRATION_SQL)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(MIGRATION_SQL)
print("DDL applied")

# Verify
for table, column in (("crm_contacts", "sentiment"),
                      ("crm_contacts", "sentiment_trend"),
                      ("crm_contacts", "sentiment_reason"),
                      ("crm_contacts", "sentiment_updated_at"),
                      ("crm_activities", "sentiment")):
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s;", (table, column))
    assert cur.fetchone(), f"{column} column missing on {table}"

print("Sentiment schema verified OK")
cur.close()
conn.close()
