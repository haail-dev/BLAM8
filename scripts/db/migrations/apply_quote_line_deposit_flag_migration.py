"""One-shot: a deposit rule on part of a quote.

  quote_lines.counts_to_deposit_rule — a line the owner marked "Counts towards
  the deposit rule" (default false);
  quotes.deposit_base — the net sum of those lines when a "ticked" rule decided
  the quote's deposit (null = the percentage is of the whole subtotal, which is
  every quote that exists today).

Idempotent. Additive only: nothing existing changes.
"""
import re

import psycopg2

ENV_PATH = ".env"
DDL = """
alter table quote_lines add column if not exists counts_to_deposit_rule boolean not null default false;
alter table quotes add column if not exists deposit_base numeric(12,2);
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
cur.execute("select column_name from information_schema.columns where table_name='quote_lines' "
            "and column_name='counts_to_deposit_rule'")
print("quote_lines:", [r[0] for r in cur.fetchall()])
cur.execute("select column_name from information_schema.columns where table_name='quotes' "
            "and column_name='deposit_base'")
print("quotes:", [r[0] for r in cur.fetchall()])
cur.close(); conn.close()
