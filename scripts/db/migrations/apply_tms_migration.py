"""One-shot: apply the TMS-baseline migration (db/schema.sql TMS section) to prod
via the Supabase session pooler, then verify objects + smoke-test the new tables
and the next_invoice_ref RPC inside a rolled-back transaction. Safe to re-run
(DDL is guarded / create-or-replace; the fleet+routing→tms subscription
conversion is idempotent).

Mirrors scripts/db/migrations/apply_fleet_migration.py."""
import re
import sys

import psycopg2

ENV_PATH = ".env"
SCHEMA_PATH = "db/schema.sql"
MARKER = "-- TMS baseline"


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)

schema = open(SCHEMA_PATH, encoding="utf-8").read()
tms_sql = schema[schema.index(MARKER):]
print(f"TMS migration block: {len(tms_sql)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(tms_sql)
print("DDL applied")

# ── verify objects ───────────────────────────────────────────────────────────
cur.execute("""
  select to_regclass('public.drivers'), to_regclass('public.rate_cards'),
         to_regclass('public.shipment_events'), to_regclass('public.invoices'),
         to_regclass('public.invoice_lines'), to_regclass('public.driver_hours'),
         to_regclass('public.feature_requests')
""")
print("tables:", cur.fetchone())
cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'vehicles'
    and column_name in ('driver_id','mot_expiry','tax_expiry','service_due','insurance_expiry')
  order by column_name
""")
print("vehicles cols:", [r[0] for r in cur.fetchall()])
cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'bookings' and column_name in ('quoted_amount','track_token')
  order by column_name
""")
print("bookings cols:", [r[0] for r in cur.fetchall()])
cur.execute("""
  select column_name from information_schema.columns
  where table_name = 'tenant_counters' and column_name = 'invoice_seq'
""")
print("tenant_counters col:", [r[0] for r in cur.fetchall()])
cur.execute("select proname from pg_proc where proname = 'next_invoice_ref'")
print("rpc:", [r[0] for r in cur.fetchall()])
cur.execute("""
  select tablename, count(*) from pg_policies
  where tablename in ('drivers','rate_cards','shipment_events','invoices',
                      'invoice_lines','driver_hours','feature_requests')
  group by tablename order by tablename
""")
print("policies:", cur.fetchall())

# ── the merge: no fleet/routing subscription rows should remain ──────────────
cur.execute("select module, count(*) from subscriptions group by module order by module")
print("subscriptions by module:", cur.fetchall())

# ── smoke test inside one rolled-back transaction ────────────────────────────
conn.autocommit = False
try:
    cur.execute("insert into tenants (name) values ('tms-smoke') returning id")
    tid = cur.fetchone()[0]
    cur.execute("""
      insert into rate_cards (tenant_id, base_charge, per_mile, per_pallet, per_drop, min_charge, fuel_pct)
      values (%s, 25, 1.20, 8, 5, 45, 6) returning id
    """, (tid,))
    assert cur.fetchone(), "rate_card insert failed"
    cur.execute("insert into drivers (tenant_id, name, licence_expiry) values (%s, 'A. Driver', '2027-01-01') returning id", (tid,))
    drv = cur.fetchone()[0]
    cur.execute("""
      insert into bookings (tenant_id, summary, booking_date, status, spaces, contact)
      values (%s, '4 pallets WF17->LS9', '2026-06-22', 'completed', 4, 'ops@acme.co') returning id
    """, (tid,))
    bk = cur.fetchone()[0]
    cur.execute("update bookings set track_token = 'tok-smoke', quoted_amount = 123.45 where id = %s", (bk,))
    cur.execute("""
      insert into shipment_events (tenant_id, booking_id, kind, signed_by, driver_id)
      values (%s, %s, 'delivered', 'J. Smith', %s) returning id
    """, (tid, bk, drv))
    assert cur.fetchone(), "shipment_event insert failed"

    cur.execute("select next_invoice_ref(%s)", (tid,))
    r1 = cur.fetchone()[0]
    cur.execute("select next_invoice_ref(%s)", (tid,))
    r2 = cur.fetchone()[0]
    assert r1 == "INV-1001" and r2 == "INV-1002", f"invoice ref sequence wrong: {r1}, {r2}"

    cur.execute("""
      insert into invoices (tenant_id, invoice_ref, customer_email, subtotal, vat, total)
      values (%s, %s, 'ops@acme.co', 123.45, 24.69, 148.14) returning id
    """, (tid, r1))
    inv = cur.fetchone()[0]
    cur.execute("""
      insert into invoice_lines (tenant_id, invoice_id, booking_id, description, amount)
      values (%s, %s, %s, '4 pallets WF17->LS9', 123.45) returning id
    """, (tid, inv, bk))
    assert cur.fetchone(), "invoice_line insert failed"

    cur.execute("insert into driver_hours (tenant_id, driver_id, work_date, hours) values (%s, %s, '2026-06-22', 9.5)", (tid, drv))
    cur.execute("insert into feature_requests (tenant_id, body, category, industry) values (%s, 'Add live ETA', 'tracking', 'logistics')", (tid,))
    print("smoke test: drivers, rate_cards, shipment_events, invoices(+lines), "
          "driver_hours, feature_requests, next_invoice_ref all OK")
finally:
    conn.rollback()
    print("rolled back — prod untouched by the smoke test")

conn.close()
print("DONE")
