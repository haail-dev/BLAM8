"""One-shot: UK bank holidays per nation + a business's nation (D1 stage 3,
docs/plan-service-urgency.md). The table is filled from gov.uk by the daily
job (app/bank_holidays.refresh); this also fills it once now. Idempotent."""
import asyncio
import re

import psycopg2

ENV_PATH = ".env"
DDL = """
create table if not exists bank_holidays (
  nation  text not null,
  day     date not null,
  title   text not null,
  primary key (nation, day)
);
alter table bank_holidays enable row level security;
alter table scheduling_config add column if not exists bank_holiday_nation text;
alter table scheduling_config add column if not exists bank_holidays_open date[] not null default '{}';
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
cur.execute("notify pgrst, 'reload schema'")
cur.close(); conn.close()

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ENV_PATH)
from app import bank_holidays  # noqa: E402
print("rows:", asyncio.run(bank_holidays.refresh()))
