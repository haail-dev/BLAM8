"""One-shot: after-job tasks (docs/plan-after-job-tasks.md) + the owner's alert
words (Settings > Rules & escalation). Idempotent."""
import re

import psycopg2

ENV_PATH = ".env"
DDL = """
alter table bookings add column if not exists done_at timestamptz;
alter table bookings add column if not exists done_by text;
alter table scheduling_config add column if not exists job_done_when text not null default 'marked';
alter table crm_tasks add column if not exists booking_id uuid references bookings(id) on delete cascade;
alter table crm_tasks add column if not exists task_key text;
alter table crm_tasks add column if not exists evidence_doc_id uuid references crm_artifacts(id) on delete set null;
alter table crm_tasks add column if not exists reminded text;
create unique index if not exists crm_tasks_booking_task_key on crm_tasks (booking_id, task_key) where booking_id is not null and task_key is not null;
alter table tenant_config add column if not exists alert_words text;
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
print("after-job migration applied")
