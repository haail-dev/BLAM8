# AI-Generated | Claude (Anthropic) | ForgeGuard | 2026-09-03
"""One-shot: tenant_config.portal_booking_confirmation — the Portal-settings
toggle for the self-serve booking confirmation email (Julius: the assistant
promised one before it existed). NULL/absent = ON, same contract as
portal_show_prices. Safe to re-run."""
import re

import psycopg2

ENV_PATH = ".env"

MIGRATION_SQL = """
ALTER TABLE tenant_config
    ADD COLUMN IF NOT EXISTS portal_booking_confirmation boolean;
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
            "WHERE table_name = 'tenant_config' "
            "AND column_name = 'portal_booking_confirmation';")
assert cur.fetchone(), "portal_booking_confirmation column missing"
print("tenant_config.portal_booking_confirmation verified OK")
cur.close()
conn.close()
