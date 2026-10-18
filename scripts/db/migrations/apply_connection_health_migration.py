"""Apply the connection-health columns to prod.

Three nullable columns on email_connections so a dead mailbox stops being
invisible: sync_failing_since (when the current failure streak began),
sync_error (the last failure, for the operator to read) and health_alerted_at
(when the owner was told). They live in the database rather than in
src/app/sync_backoff.py's process memory because a Render redeploy must not reset
an hour-old outage back to zero, and "we already told them" must survive a
restart or every deploy re-buzzes the same dead mailbox.

Extracts the idempotent block from db/schema.sql and runs it over psycopg2
(direct host, DB_PASSWORD commented in .env) — the house pattern. Safe to
re-run.

Applying this changes nothing for anybody: every existing connection reads
healthy (all three null), and the first failing sweep after the deploy starts
its own clock.

Run: python scripts/db/migrations/apply_connection_health_migration.py
"""
import re

import psycopg2

ENV = ".env"
env_text = open(ENV, encoding="utf-8").read()
pw = re.search(r"^#?\s*DB_PASSWORD\s*[:=]?\s*(\S+)", env_text, re.M).group(1)
ref = re.search(r"https?://([a-z0-9]+)\.supabase\.co", env_text).group(1)

schema = open("db/schema.sql", encoding="utf-8").read()


def _block(marker: str, tail: str) -> str:
    start = schema.index(marker)
    return schema[start:schema.index(";", schema.index(tail, start)) + 1]


# Two blocks, two tables, one deploy. The health columns are where the streak
# lives; notify_problems is the settings row that governs whether the alert
# interrupts you — without it the notification settings form 500s on save,
# because the handler writes every family it renders.
sql = "\n".join((
    _block("-- Somebody is told when a mailbox stops (Kris 2026-09-04)",
           "alter table email_connections add column if not exists health_alerted_at"),
    _block("-- The sixth family: Problems (Kris 2026-09-04)",
           "alter table tenant_config add column if not exists notify_problems"),
))

COLS = ("sync_failing_since", "sync_error", "health_alerted_at")
assert all(c in sql for c in COLS) and "notify_problems" in sql
assert sql.count("alter table email_connections") == 3
assert sql.count("alter table tenant_config") == 1
assert not re.search(r"\bdrop\b|\btruncate\b|\bdelete from\b|\bupdate\b", sql, re.I), \
    "this migration only ADDS columns"

conn = psycopg2.connect(host=f"db.{ref}.supabase.co", port=5432, dbname="postgres",
                        user="postgres", password=pw, sslmode="require",
                        connect_timeout=10)
cur = conn.cursor()

cur.execute("""select column_name from information_schema.columns
               where (table_name = 'email_connections' and column_name in %s)
                  or (table_name = 'tenant_config'
                      and column_name = 'notify_problems')""", (COLS,))
print("already present:", sorted(r[0] for r in cur.fetchall()) or "none")

cur.execute(sql)
conn.commit()

cur.execute("""select column_name, is_nullable, data_type
               from information_schema.columns
               where table_name = 'email_connections' and column_name in %s
               order by column_name""", (COLS,))
for row in cur.fetchall():
    print("  %-20s nullable=%s %s" % row)

# Nothing was failing before this could record it, so nothing may claim to be —
# and nobody may already be stamped as told.
cur.execute("""select count(*) from email_connections
               where sync_failing_since is not null or health_alerted_at is not null""")
stale = cur.fetchone()[0]
print("  connections carrying a streak or an alert:", stale)
assert stale == 0, "fresh columns should mark nobody"

cur.execute("select count(*) from email_connections")
print("  connections that will start reporting their health:", cur.fetchone()[0])

# The new family defaults to 'now' for everybody, which is the behaviour they
# would have had anyway — an unset family reads 'now'. Nobody is opted into
# anything by this migration; they can only opt down to 'brief'.
cur.execute("select count(*) from tenant_config where notify_problems <> 'now'")
assert cur.fetchone()[0] == 0, "a fresh column should hold nothing but the default"
cur.execute("""select column_name, column_default from information_schema.columns
               where table_name = 'tenant_config' and column_name = 'notify_problems'""")
for row in cur.fetchall():
    print("  %-20s default %s" % row)

cur.close()
conn.close()
print("OK")
