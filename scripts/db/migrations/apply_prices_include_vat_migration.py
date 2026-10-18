"""One-shot: a document remembers whether its prices include VAT.

  quotes.prices_include_vat, invoices.prices_include_vat — true when the
  document was made under the owner's "my prices already include VAT" choice
  (its lines are the prices the customer was told, and VAT is carved out of
  their total). null = VAT added on top, which is every document that exists
  today. Written only when true.

Idempotent. Additive only: nothing existing changes.
"""
import re

import psycopg2

ENV_PATH = ".env"
DDL = """
alter table quotes add column if not exists prices_include_vat boolean;
alter table invoices add column if not exists prices_include_vat boolean;
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
for table in ("quotes", "invoices"):
    cur.execute("select column_name from information_schema.columns where table_name=%s "
                "and column_name='prices_include_vat'", (table,))
    print(table + ":", [r[0] for r in cur.fetchall()])
cur.close(); conn.close()
