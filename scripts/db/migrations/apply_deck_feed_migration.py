"""Apply Today's notification feed columns to prod.

Two blocks from db/schema.sql:
  * concierge_notifications gains contact / contact_name (who a notification is
    ABOUT, so it can land on that person's card on Today) and dismissed_at (the
    swipe — distinct from seen_at, which only kills the glow), plus a partial
    index for the live read;
  * tenant_config gains deck_priority jsonb, the eight-topic order the operator
    picks in Settings.

Runs over psycopg2 (direct host, DB_PASSWORD commented in .env) — the house
pattern. Safe to re-run.

Applying this changes nothing for anybody: existing notifications keep a null
contact and simply do not join the deck, and an empty deck_priority is the
default order the deck already shipped with.

Run: python scripts/db/migrations/apply_deck_feed_migration.py
"""
import re

import psycopg2

ENV = ".env"
env_text = open(ENV, encoding="utf-8").read()
pw = re.search(r"^#?\s*DB_PASSWORD\s*[:=]?\s*(\S+)", env_text, re.M).group(1)
ref = re.search(r"https?://([a-z0-9]+)\.supabase\.co", env_text).group(1)

schema = open("db/schema.sql", encoding="utf-8").read()
start = schema.index("-- Today is fed by notifications (Kris 2026-09-04)")
tail = "alter table tenant_config add column if not exists deck_priority"
end = schema.index(";", schema.index(tail))
sql = schema[start:end + 1]

assert "contact_name" in sql and "dismissed_at" in sql and "deck_priority" in sql
assert sql.count("alter table concierge_notifications") == 3
assert sql.count("alter table tenant_config") == 1
assert not re.search(r"\bdrop\b|\btruncate\b|\bdelete from\b|\bupdate\b", sql, re.I), \
    "this migration only ADDS columns and an index"

conn = psycopg2.connect(host=f"db.{ref}.supabase.co", port=5432, dbname="postgres",
                        user="postgres", password=pw, sslmode="require",
                        connect_timeout=10)
cur = conn.cursor()

cur.execute("""select column_name from information_schema.columns
               where (table_name = 'concierge_notifications'
                      and column_name in ('contact','contact_name','dismissed_at'))
                  or (table_name = 'tenant_config' and column_name = 'deck_priority')""")
print("already present:", sorted(r[0] for r in cur.fetchall()) or "none")

cur.execute(sql)
conn.commit()

cur.execute("""select table_name, column_name, is_nullable, column_default
               from information_schema.columns
               where (table_name = 'concierge_notifications'
                      and column_name in ('contact','contact_name','dismissed_at'))
                  or (table_name = 'tenant_config' and column_name = 'deck_priority')
               order by table_name, column_name""")
for row in cur.fetchall():
    print("  %-24s %-13s nullable=%s default=%s" % row)

cur.execute("""select indexname from pg_indexes
               where tablename = 'concierge_notifications'
                 and indexname = 'idx_concierge_notifications_live'""")
print("  index:", [r[0] for r in cur.fetchall()] or "MISSING")

# Nobody's Today changed: no row is dismissed, and no tenant has an order yet.
cur.execute("select count(*) from concierge_notifications where dismissed_at is not null")
assert cur.fetchone()[0] == 0, "a fresh column should dismiss nobody"
cur.execute("select count(*) from tenant_config where deck_priority <> '{}'::jsonb")
assert cur.fetchone()[0] == 0, "a fresh column should reorder nobody"
print("  nothing dismissed, nobody reordered — as it should be")

cur.close()
conn.close()
print("OK")
