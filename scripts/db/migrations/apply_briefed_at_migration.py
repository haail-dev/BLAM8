"""Apply the held-notification stamp to prod.

One nullable column plus a partial index on concierge_notifications. It closes
the "in the brief" black hole: the brief used to pick held notifications with a
rolling 24h window, so anything held over a closed weekend was already out of
range by the morning that would have mentioned it. briefed_at stamps what a
brief actually said, and the brief reads what is still unstamped.

Extracts the idempotent block from db/schema.sql and runs it over psycopg2
(direct host, DB_PASSWORD commented in .env) — the house pattern. Safe to
re-run.

Applying this changes nothing for anybody: every existing row has briefed_at
NULL, and the first brief after the deploy stamps the lot in one update.

Run: python scripts/db/migrations/apply_briefed_at_migration.py
"""
import re

import psycopg2

ENV = ".env"
env_text = open(ENV, encoding="utf-8").read()
pw = re.search(r"^#?\s*DB_PASSWORD\s*[:=]?\s*(\S+)", env_text, re.M).group(1)
ref = re.search(r"https?://([a-z0-9]+)\.supabase\.co", env_text).group(1)

schema = open("db/schema.sql", encoding="utf-8").read()
start = schema.index('-- "In the brief" stops being a black hole (Kris 2026-09-04)')
end = schema.index("where briefed_at is null;", start)
sql = schema[start:end + len("where briefed_at is null;")]

assert "briefed_at" in sql and "idx_concierge_notifications_unbriefed" in sql
assert sql.count("alter table") == 1 and sql.count("create index") == 1
assert not re.search(r"\bdrop\b|\btruncate\b|\bdelete from\b|\bupdate\b", sql, re.I), \
    "this migration only ADDS a column and an index"

conn = psycopg2.connect(host=f"db.{ref}.supabase.co", port=5432, dbname="postgres",
                        user="postgres", password=pw, sslmode="require",
                        connect_timeout=10)
cur = conn.cursor()

cur.execute("""select column_name from information_schema.columns
               where table_name = 'concierge_notifications'
                 and column_name = 'briefed_at'""")
print("already present:", [r[0] for r in cur.fetchall()] or "none")

cur.execute(sql)
conn.commit()

cur.execute("""select column_name, is_nullable, data_type
               from information_schema.columns
               where table_name = 'concierge_notifications'
                 and column_name = 'briefed_at'""")
for row in cur.fetchall():
    print("  column: %s nullable=%s %s" % row)

cur.execute("""select indexname from pg_indexes
               where tablename = 'concierge_notifications'
                 and indexname = 'idx_concierge_notifications_unbriefed'""")
print("  index:", [r[0] for r in cur.fetchall()] or "MISSING")

# Nothing was mentioned before this existed, so nothing may claim to have been.
cur.execute("select count(*) from concierge_notifications where briefed_at is not null")
stamped = cur.fetchone()[0]
print("  rows already stamped:", stamped)
assert stamped == 0, "a fresh column should stamp nobody"

cur.execute("select count(*) from concierge_notifications where briefed_at is null")
print("  rows in the held queue (all of them, by definition):", cur.fetchone()[0])

cur.close()
conn.close()
print("OK")
