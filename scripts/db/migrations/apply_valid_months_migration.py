"""One-shot (run #8, 2026-09-05): `valid_months` on crm_field_definitions — how
long an expiring field runs from the date on record (0 = the date IS the
expiry; 24 = a theory test pass that lasts two years from the pass date, in months).
Applied via the Supabase session pooler; safe to re-run."""
import re

import psycopg2

ENV_PATH = ".env"
DDL = """
alter table crm_field_definitions add column if not exists valid_months integer not null default 0;
"""


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)
conn = psycopg2.connect(host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
                        user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require")
conn.autocommit = True
cur = conn.cursor()
cur.execute(DDL)
cur.execute("select column_name from information_schema.columns where table_name='crm_field_definitions' and column_name in ('valid_months')")
print("crm_field_definitions:", cur.fetchall())
cur.close(); conn.close()
