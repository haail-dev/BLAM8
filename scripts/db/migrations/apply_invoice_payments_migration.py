"""One-shot: apply the Invoicing & Payments enhancement migration (db/schema.sql —
invoices payment columns, invoice_lines qty/unit price, tenants.stripe_account_id,
invoice_settings bank details + VAT-off default, tenant_config.notify_invoice_viewed)
to prod via the Supabase session pooler, then verify. Safe to re-run (guarded).
Mirrors scripts/db/migrations/apply_invoicing_migration.py."""
import re

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- Invoicing & Payments enhancement"


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
print(f"Invoice payments migration block: {len(sql)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(sql)
print("DDL applied")

cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'invoices' and column_name in
    ('contact_id','deal_id','booking_id','issue_date','due_date','notes','created_by',
     'amount_paid','payment_method','stripe_payment_intent_id','payment_link',
     'viewed_at','overdue_notified_days','reminders_sent','last_reminder_at')
  order by column_name
""")
print("invoices cols:", [r[0] for r in cur.fetchall()])
cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'invoice_lines' and column_name in ('quantity','unit_price','kb_entry_id')
""")
print("invoice_lines cols:", [r[0] for r in cur.fetchall()])
cur.execute("select column_name from information_schema.columns "
            "where table_name = 'tenants' and column_name = 'stripe_account_id'")
print("tenants col:", [r[0] for r in cur.fetchall()])
cur.execute("""
  select column_name, column_default from information_schema.columns
  where table_name = 'invoice_settings' and column_name in
    ('bank_account_name','bank_sort_code','bank_account_number','show_vat')
""")
print("invoice_settings:", cur.fetchall())
cur.execute("select column_name from information_schema.columns "
            "where table_name = 'tenant_config' and column_name = 'notify_invoice_viewed'")
print("tenant_config col:", [r[0] for r in cur.fetchall()])
cur.execute("select indexname from pg_indexes where tablename = 'invoices' "
            "and indexname in ('invoices_tenant_ref_uniq','invoices_customer_idx')")
print("indexes:", [r[0] for r in cur.fetchall()])

conn.close()
print("DONE")
