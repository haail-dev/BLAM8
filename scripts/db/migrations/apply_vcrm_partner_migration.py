"""One-shot: apply the Voice CRM partner-pipeline columns (db/schema.sql block
'Voice CRM partner pipeline') to prod via the Supabase session pooler, then verify.
Additive (add column if not exists): existing prospects become kind='customer'.
Safe to re-run."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
START = "-- Voice CRM partner pipeline"
END = "-- /vcrm partner fields"


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)
schema = open(SCHEMA_PATH, encoding="utf-8").read()
block = schema[schema.index(START):schema.index(END)]
print(f"partner migration block: {len(block)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(block)
print("DDL applied")
cur.execute("select column_name from information_schema.columns where table_name='vcrm_prospects'")
cols = {r[0] for r in cur.fetchall()}
assert {"kind", "role", "channel", "source", "email"} <= cols, cols
cur.execute("select kind, count(*) from vcrm_prospects group by 1")
print("rows by kind:", cur.fetchall())
conn.close()
print("DONE")
