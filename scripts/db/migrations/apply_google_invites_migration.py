"""One-shot: apply the google_invites table migration (db/schema.sql
'-- Google sign-in invites' section) to prod via the Supabase session pooler,
then verify. Single-use invite codes for the Google sign-in gate, minted in the
operator console (/admin/invites). Safe to re-run ('create table if not exists')."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Google sign-in invites"


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
print(f"google-invites migration block: {len(block)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(block)
print("DDL applied")

cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'google_invites' order by ordinal_position
""")
print("columns:", [r[0] for r in cur.fetchall()])
cur.execute("select relrowsecurity from pg_class where relname = 'google_invites'")
print("RLS enabled:", cur.fetchone()[0])

conn.close()
print("DONE")
