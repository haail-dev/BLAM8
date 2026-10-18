"""One-shot: apply the mailbox add-on billing migration (db/schema.sql — the
email_connections.grandfathered column) to prod via the Supabase session pooler,
then grandfather every mailbox that exists NOW so switching on the £39/mo
extra-mailbox add-on never retro-charges an existing customer. Safe to re-run:
the ALTER is `if not exists`, and the backfill only runs when no rows are yet
grandfathered (so a re-run can't grandfather mailboxes added after launch).
Mirrors scripts/db/migrations/apply_invoicing_migration.py."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Mailbox add-on billing — grandfathered flag"


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)

schema = open(SCHEMA_PATH, encoding="utf-8").read()
sql = schema[schema.index(MARKER):]
print(f"Mailbox add-on migration block: {len(sql)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(sql)
print("DDL applied (email_connections.grandfathered)")

# One-time grandfathering: only when nothing is grandfathered yet, so a re-run
# after launch (once billable mailboxes exist) can't sweep them in.
cur.execute("select count(*) from email_connections where grandfathered = true")
already = cur.fetchone()[0]
if already == 0:
    cur.execute("update email_connections set grandfathered = true")
    print(f"backfill: grandfathered {cur.rowcount} existing mailbox(es)")
else:
    print(f"backfill skipped — {already} mailbox(es) already grandfathered")

cur.execute("""
  select column_name, data_type, column_default from information_schema.columns
  where table_name = 'email_connections' and column_name = 'grandfathered'
""")
print("column:", cur.fetchone())
cur.execute("select grandfathered, count(*) from email_connections group by grandfathered order by 1")
print("counts by grandfathered:", cur.fetchall())

conn.close()
print("DONE")
