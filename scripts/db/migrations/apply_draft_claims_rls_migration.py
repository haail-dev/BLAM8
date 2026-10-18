"""One-shot: row-level security on draft_claims (Supabase Security Advisor, 2026-10-14).
Applied via the Supabase session pooler like scripts/db/migrations/apply_waitlist_migration.py; safe to
re-run. Run from the folder holding .env."""
import re

import psycopg2

ENV_PATH = ".env"
DDL = """
alter table draft_claims enable row level security;
drop policy if exists "tenant isolation - draft_claims" on draft_claims;
create policy "tenant isolation - draft_claims" on draft_claims
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));
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
cur.execute("select relrowsecurity from pg_class where oid = 'public.draft_claims'::regclass")
print("rls before:", cur.fetchone()[0])
cur.execute(DDL)
cur.execute("select relrowsecurity from pg_class where oid = 'public.draft_claims'::regclass")
print("rls after:", cur.fetchone()[0])
cur.execute("select policyname, cmd from pg_policies where tablename = 'draft_claims'")
print("policies:", cur.fetchall())
cur.execute("select count(*) from draft_claims")
print("rows still readable by owner role:", cur.fetchone()[0])
cur.execute("select c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace "
            "where n.nspname='public' and c.relkind='r' and not c.relrowsecurity order by 1")
print("public tables still without RLS:", [r[0] for r in cur.fetchall()])
conn.close()
