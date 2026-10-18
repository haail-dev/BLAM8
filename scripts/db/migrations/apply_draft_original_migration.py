"""Apply email_log.draft_original to prod (edit review, 2026-09-29).

One nullable text column. Extracts the idempotent block from db/schema.sql and
runs it over psycopg2 (direct host, DB_PASSWORD commented in .env) — the house
pattern. Safe to re-run; changes nothing for anybody until a substantive edit
is sent. Until it is applied the send path simply skips keeping the original.

Run: python scripts/db/migrations/apply_draft_original_migration.py
"""
import re

import psycopg2

env_text = open(".env", encoding="utf-8").read()
pw = re.search(r"^#?\s*DB_PASSWORD\s*[:=]?\s*(\S+)", env_text, re.M).group(1)
ref = re.search(r"https?://([a-z0-9]+)\.supabase\.co", env_text).group(1)

schema = open("db/schema.sql", encoding="utf-8").read()
start = schema.index("-- Edit review (2026-09-29)")
sql = schema[start:schema.index("draft_original text;", start) + len("draft_original text;")]
assert sql.count("alter table") == 1
assert not re.search(r"\bdrop\b|\btruncate\b|\bdelete from\b|\bupdate\b", sql, re.I)

conn = psycopg2.connect(host=f"db.{ref}.supabase.co", port=5432, dbname="postgres",
                        user="postgres", password=pw, sslmode="require", connect_timeout=10)
cur = conn.cursor()
cur.execute(sql)
conn.commit()
cur.execute("""select column_name, is_nullable, data_type from information_schema.columns
               where table_name = 'email_log' and column_name = 'draft_original'""")
print(cur.fetchall() or "MISSING")
cur.close()
conn.close()
print("OK")
