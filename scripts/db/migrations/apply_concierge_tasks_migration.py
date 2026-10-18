"""The Concierge's task list (docs/plan-concierge-task-list.md, 2026-09-25):
one row per task she works in the background. Idempotent.

    python scripts/db/migrations/apply_concierge_tasks_migration.py
"""
import re

import psycopg2

SQL = """
create table if not exists concierge_tasks (
  id           uuid primary key default gen_random_uuid(),
  tenant_id    uuid not null references tenants(id) on delete cascade,
  user_id      uuid,
  batch_id     uuid not null,
  position     int  not null default 0,
  title        text not null,
  instruction  text not null,
  status       text not null default 'queued',
  outcome      text,
  proposal_id  uuid,
  surface      text not null default 'typed',
  created_at   timestamptz not null default now(),
  started_at   timestamptz,
  finished_at  timestamptz
);
create index if not exists idx_concierge_tasks_batch on concierge_tasks (batch_id, position);
create index if not exists idx_concierge_tasks_open on concierge_tasks (status, created_at);
alter table concierge_tasks enable row level security;
drop policy if exists "tenant isolation - concierge_tasks" on concierge_tasks;
create policy "tenant isolation - concierge_tasks" on concierge_tasks
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));
notify pgrst, 'reload schema';
"""


def env_value(key: str) -> str:
    for line in open(".env", encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in .env")


ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)
conn = psycopg2.connect(host="aws-1-eu-west-2.pooler.supabase.com", port=5432, dbname="postgres",
                        user=f"postgres.{ref}", password=env_value("DB_PASSWORD"), sslmode="require")
conn.autocommit = True
cur = conn.cursor()
cur.execute(SQL)
cur.execute("select count(*) from information_schema.columns where table_name='concierge_tasks'")
print("concierge_tasks columns:", cur.fetchone()[0])
