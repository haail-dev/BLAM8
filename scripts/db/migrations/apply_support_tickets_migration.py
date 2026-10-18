"""One-shot: apply the support-tickets migration (db/schema.sql 'Support tickets'
section) to prod via the Supabase session pooler, then verify. Extends
feature_requests into the chatbot ticket pipeline: nullable tenant_id (anonymous
website tickets), BLAM8-XXXX references (global sequence + next_ticket_reference()),
contact/triage/status-note columns, and a reference backfill. Safe to re-run."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Support tickets — the chatbot raises tracked tickets"


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
print(f"support-tickets migration block: {len(block)} chars")

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
  select column_name from information_schema.columns
  where table_name = 'feature_requests'
    and column_name in ('reference','user_email','account_type','error_message',
                        'severity','status_note','page_url','user_agent','redacted_at')
  order by column_name
""")
print("new columns:", [r[0] for r in cur.fetchall()])
cur.execute("select is_nullable from information_schema.columns "
            "where table_name = 'feature_requests' and column_name = 'tenant_id'")
print("tenant_id nullable:", cur.fetchone())
cur.execute("select next_ticket_reference()")
print("sample reference (sequence live):", cur.fetchone())
cur.execute("select count(*) from feature_requests where reference is null")
print("rows still missing a reference:", cur.fetchone())

conn.close()
print("DONE")
