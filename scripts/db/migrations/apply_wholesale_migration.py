"""Apply the Wholesale OS migration (docs/os/wholesale.md §4) directly to prod.

Extracts the idempotent Wholesale block from db/schema.sql and executes it over
psycopg2 (direct host, DB_PASSWORD comment in .env) — the house pattern from
scripts/db/migrations/apply_crm_phase4b_migration.py / the SMS block apply. Safe to re-run.

Run: python scripts/db/migrations/apply_wholesale_migration.py
"""
import re
import sys

import psycopg2

ENV = ".env"
env_text = open(ENV, encoding="utf-8").read()
pw = re.search(r"^#\s*DB_PASSWORD\s*[:=]?\s*(\S+)", env_text, re.M).group(1)
ref = re.search(r"https?://([a-z0-9]+)\.supabase\.co", env_text).group(1)

schema = open("db/schema.sql", encoding="utf-8").read()
start = schema.index("-- Wholesale OS (docs/os/wholesale.md")
sql = schema[start:]
assert "wholesale_order_lines" in sql and "wholesale_standing_orders" in sql
assert not re.search(r"\bdrop table\b|\btruncate\b|\bdelete from\b", sql, re.I)

conn = psycopg2.connect(host=f"db.{ref}.supabase.co", port=5432, dbname="postgres",
                        user="postgres", password=pw, sslmode="require",
                        connect_timeout=10)
cur = conn.cursor()
cur.execute(sql)
conn.commit()

checks = [
    ("table", "wholesale_order_lines"),
    ("table", "wholesale_standing_orders"),
    ("column", ("crm_deals", "is_order")),
    ("column", ("crm_deals", "wholesale_status")),
    ("column", ("crm_deals", "required_by")),
    ("column", ("crm_deals", "standing_order_id")),
    ("column", ("crm_deals", "allocation_priority")),
]
ok = True
for kind, what in checks:
    if kind == "table":
        cur.execute("select 1 from information_schema.tables "
                    "where table_schema='public' and table_name=%s", (what,))
        present = bool(cur.fetchone())
        print(f"  table {what}: {'OK' if present else 'MISSING'}")
    else:
        tbl, col = what
        cur.execute("select 1 from information_schema.columns where table_schema='public' "
                    "and table_name=%s and column_name=%s", (tbl, col))
        present = bool(cur.fetchone())
        print(f"  column {tbl}.{col}: {'OK' if present else 'MISSING'}")
    ok = ok and present
conn.close()
print("Wholesale migration:", "APPLIED + VERIFIED" if ok else "INCOMPLETE")
sys.exit(0 if ok else 1)
