# AI-Generated | Claude (Anthropic) | ForgeGuard | 2026-09-02
"""One-shot: job-parties Phase 2 columns (docs/modules/module-job-parties.md).
`evidence` — the sentence the extractor read the party from, shown on the
confirm card so the operator judges the claim, not a summary of it.
`email_log_id` — which email made the claim (provenance for the card + audit).
`redirect_ccd` — D-principle 4's receipt: the old payer is cc'd on the FIRST
invoice that goes to a confirmed re-routed payer, exactly once.
Safe to re-run (ADD COLUMN IF NOT EXISTS throughout)."""
import re

import psycopg2

ENV_PATH = ".env"

MIGRATION_SQL = """
ALTER TABLE job_parties ADD COLUMN IF NOT EXISTS evidence text;
ALTER TABLE job_parties ADD COLUMN IF NOT EXISTS email_log_id uuid;
ALTER TABLE job_parties ADD COLUMN IF NOT EXISTS redirect_ccd boolean NOT NULL DEFAULT false;
"""


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)

print(f"job_parties phase-2 migration: {len(MIGRATION_SQL)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(MIGRATION_SQL)
print("DDL applied")

for column in ("evidence", "email_log_id", "redirect_ccd"):
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'job_parties' AND column_name = %s;", (column,))
    assert cur.fetchone(), f"{column} column missing on job_parties"

print("job_parties phase-2 schema verified OK")
cur.close()
conn.close()
