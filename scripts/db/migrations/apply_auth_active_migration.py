"""One-shot (security review 2026-09, M1): row-level security admits only
ACTIVE memberships, so a switched-off person's own database session reads
nothing of the business. Idempotent."""
import re

import psycopg2

ENV_PATH = ".env"
DDL = """
create or replace function auth_tenant_ids()
returns setof uuid
language sql stable security definer set search_path = public
as $$
  select tenant_id from tenant_members where user_id = auth.uid() and active
$$;
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
print("auth_tenant_ids now admits active memberships only")
