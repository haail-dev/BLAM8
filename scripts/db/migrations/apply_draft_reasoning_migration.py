# AI-Generated | Claude (Anthropic) | ForgeGuard | 2026-09-02
"""One-shot: email_log.draft_reasoning — the model's captured working (text
turns + tool calls) behind the draft box's ⓘ (Julius: "show that chain of
thought with every draft"). Safe to re-run."""
import re

import psycopg2

ENV_PATH = ".env"

MIGRATION_SQL = """
ALTER TABLE email_log ADD COLUMN IF NOT EXISTS draft_reasoning text;
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
cur.execute("SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'email_log' AND column_name = 'draft_reasoning';")
assert cur.fetchone(), "draft_reasoning column missing"
print("email_log.draft_reasoning verified OK")
cur.close()
conn.close()
