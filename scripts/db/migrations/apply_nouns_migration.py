"""One-shot (2026-09-05): scheduling_config.nouns — the business's own words for
a booking and for the person who attends ("lesson" / "pupil", "class" / "child",
"session" / "client"), read by every customer-facing rule the class-and-lesson
programme added so nothing says "class" to a driving school. Applied via the
Supabase session pooler; safe to re-run."""
import re

import psycopg2

ENV_PATH = ".env"
DDL = """
alter table scheduling_config add column if not exists nouns jsonb not null default '{}'::jsonb;
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
cur.execute("select column_name from information_schema.columns where table_name='scheduling_config' and column_name='nouns'")
print("scheduling_config:", cur.fetchall())
cur.close(); conn.close()
