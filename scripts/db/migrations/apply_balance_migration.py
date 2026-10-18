"""One-shot: billing the balance (Kris 2026-09-24, "how would a real kennels
deal with payments?" → deposit to secure, balance on collection).

  bookings.balance_invoice_id / balance_invoiced_at — the balance invoice the
  daily job (src/app/jobs/raise_balances.py) raised for a booking, so it never
  raises one twice.

The rig (Little Paws) takes the kennels preset in invoice_settings.extras:
quote_booking_priced=false (no automatic quote restating a diary price) and
balance_mode='draft' (a balance invoice drafted the day before collection).
Every other tenant keeps today's behaviour (the defaults). Idempotent.
"""
import re

import psycopg2

ENV_PATH = ".env"
DDL = """
alter table bookings add column if not exists balance_invoice_id uuid;
alter table bookings add column if not exists balance_invoiced_at timestamptz;
update invoice_settings
   set extras = coalesce(extras, '{}'::jsonb)
              || '{"quote_booking_priced": false, "balance_mode": "draft"}'::jsonb
 where tenant_id = 'fb4081e6-4786-4a93-b090-bf85c5142727';
"""


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")




password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)[.]supabase[.]co", env_value("SUPABASE_URL")).group(1)
conn = psycopg2.connect(host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
                        user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require")
conn.autocommit = True
cur = conn.cursor()
cur.execute(DDL)
cur.execute("select column_name from information_schema.columns where table_name='bookings' "
            "and column_name like 'balance%'")
print("bookings:", [r[0] for r in cur.fetchall()])
cur.execute("select extras from invoice_settings where tenant_id='fb4081e6-4786-4a93-b090-bf85c5142727'")
print("rig extras:", cur.fetchone())
cur.close(); conn.close()
