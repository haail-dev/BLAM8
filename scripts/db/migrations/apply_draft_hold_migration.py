"""One-shot: add tenant_config.draft_hold, held-by-default for NEW tenants only.

The column defaults TRUE so every tenant_config row created from here on starts
with drafting held — a new tenant walks their loaded history and releases the
agent themselves. Existing tenants must NOT be held: their agents are working
today and a deploy must not silence them.

The backfill therefore has to run exactly once, on the deploy that adds the
column. tenant_config has no created_at, so there is no way afterwards to tell
"this row predates the feature" from "this tenant is genuinely still held" —
which means a second run would release someone mid-onboarding. So the script
checks whether the column already existed and only backfills when it did not.
"""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
START = "-- Go-live hold: drafting is OFF until the owner says go (Kris 2026-09-01)"


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

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()

cur.execute("""select 1 from information_schema.columns
                where table_name = 'tenant_config' and column_name = 'draft_hold'""")
existed = cur.fetchone() is not None

cur.execute(sql)
if existed:
    print("column already present — backfill SKIPPED (it would release held tenants)")
else:
    cur.execute("update tenant_config set draft_hold = false")
    print(f"backfilled {cur.rowcount} existing tenants to not-held")

cur.execute("select draft_hold, count(*) from tenant_config group by draft_hold")
print("draft_hold now:", dict(cur.fetchall()))
cur.close()
conn.close()
