"""Apply the notification-control columns to prod.

Five family settings plus a quiet-hours window on tenant_config. Extracts the
idempotent block from db/schema.sql and runs it over psycopg2 (direct host,
DB_PASSWORD commented in .env) — the house pattern. Safe to re-run.

Every column has a default, and the default is the behaviour that already
exists: 'now' for all five families, quiet hours unset. Applying this changes
nothing for anybody until they open Settings.

Run: python scripts/db/migrations/apply_notify_prefs_migration.py
"""
import re
import sys

import psycopg2

ENV = ".env"
env_text = open(ENV, encoding="utf-8").read()
pw = re.search(r"^#?\s*DB_PASSWORD\s*[:=]?\s*(\S+)", env_text, re.M).group(1)
ref = re.search(r"https?://([a-z0-9]+)\.supabase\.co", env_text).group(1)

schema = open("db/schema.sql", encoding="utf-8").read()
start = schema.index('-- Notification control (Kris 2026-09-02')
end = schema.index("alter table tenant_config add column if not exists quiet_end", start)
sql = schema[start:schema.index(";", end) + 1]

assert "notify_drafts" in sql and "quiet_start" in sql and "quiet_end" in sql
assert sql.count("alter table tenant_config") == 7, sql.count("alter table")
assert not re.search(r"\bdrop\b|\btruncate\b|\bdelete from\b|\bupdate\b", sql, re.I), \
    "this migration only ADDS columns"

conn = psycopg2.connect(host=f"db.{ref}.supabase.co", port=5432, dbname="postgres",
                        user="postgres", password=pw, sslmode="require",
                        connect_timeout=10)
cur = conn.cursor()

cur.execute("""select column_name from information_schema.columns
               where table_name = 'tenant_config'
                 and column_name in ('notify_drafts','notify_bookings',
                     'notify_questions','notify_quotes','notify_invoices',
                     'quiet_start','quiet_end')""")
before = {r[0] for r in cur.fetchall()}
print("already present:", sorted(before) or "none")

cur.execute(sql)
conn.commit()

cur.execute("""select column_name, column_default from information_schema.columns
               where table_name = 'tenant_config'
                 and column_name like 'notify_%' or column_name like 'quiet_%'
               order by column_name""")
for name, default in cur.fetchall():
    print("  %-18s default %s" % (name, default))

# Nobody's notifications changed: every family should read 'now' everywhere.
cur.execute("""select count(*) from tenant_config
               where notify_drafts <> 'now' or notify_bookings <> 'now'
                  or notify_questions <> 'now' or notify_quotes <> 'now'
                  or notify_invoices <> 'now'""")
changed = cur.fetchone()[0]
print("tenants not on the default:", changed)
if changed and not before:
    sys.exit("a fresh migration should leave every tenant on 'now'")

cur.close()
conn.close()
print("done")
