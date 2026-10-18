"""One-shot: apply the AI-disclosure migration (db/schema.sql 'AI disclosure'
section) to prod via the Supabase session pooler, then verify. Adds
tenant_config.ai_disclosure (boolean, default TRUE) — auto-sent replies carry
an AI-written disclosure footer unless the tenant turns it off (EU AI Act
Article 50; see docs/security/eu-ai-act-compliance.md). Safe to re-run."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- AI disclosure (EU AI Act Article 50"


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
print(f"ai-disclosure migration block: {len(block)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
with conn.cursor() as cur:
    cur.execute(block)
    cur.execute("select column_name, column_default from information_schema.columns "
                "where table_name = 'tenant_config' and column_name = 'ai_disclosure'")
    print("verify:", cur.fetchall())
conn.close()
print("done")
