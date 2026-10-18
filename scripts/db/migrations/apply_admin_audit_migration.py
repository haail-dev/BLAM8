"""One-shot (security review 2026-09, M16/M17): the operator audit trail.
Every console change and every email-body view writes a row (src/app/admin_auth.py
_audit). Service role only — RLS on with no policies. Idempotent."""
import re

import psycopg2

ENV_PATH = ".env"
DDL = """
create table if not exists admin_audit (
  id uuid primary key default gen_random_uuid(),
  at timestamptz not null default now(),
  admin_user_id uuid not null,
  admin_email text,
  method text not null,
  path text not null,
  tenant_id uuid,
  detail jsonb not null default '{}'::jsonb
);
create index if not exists idx_admin_audit_at on admin_audit (at desc);
create index if not exists idx_admin_audit_tenant on admin_audit (tenant_id, at desc);
alter table admin_audit enable row level security;
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
print("admin_audit ready")
