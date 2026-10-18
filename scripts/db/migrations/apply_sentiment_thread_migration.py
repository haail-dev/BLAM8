"""One-shot: add crm_contacts.sentiment_thread_id to prod.

Which conversation a mood read came from, so the Inbox can decline to colour a
different one with it. Idempotent; no backfill — NULL is meaningful here (the
read predates the column, so its thread is genuinely unknown and no mood shows
until the next inbound re-stamps it).
"""
import re

import psycopg2

ENV_PATH, SCHEMA_PATH = ".env", "db/schema.sql"
START = "-- Mood belongs to the thread it was read from (Kris 2026-09-01)"


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)
sql = open(SCHEMA_PATH, encoding="utf-8").read()
sql = sql[sql.index(START):]

conn = psycopg2.connect(host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
                        user=f"postgres.{ref}", password=env_value("DB_PASSWORD"),
                        dbname="postgres", sslmode="require")
conn.autocommit = True
cur = conn.cursor()
cur.execute(sql)
cur.execute("""select count(*) from crm_contacts
                where sentiment in ('concerned','frustrated','angry')
                  and sentiment_thread_id is null""")
print("DDL applied. Existing moods with no thread recorded (these stop colouring "
      "the inbox until the next inbound re-stamps them):", cur.fetchone()[0])
cur.close(); conn.close()
