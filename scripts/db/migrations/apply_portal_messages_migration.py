"""Apply the portal_messages migration (2026-08-31) to the live database.

Creates the portal chat transcript table: every identified customer's portal
chat turn (both sides), keyed by tenant + contact email so the inbox thread
can interleave the conversation and the drafter can replay it as context.

Run: python scripts/db/migrations/apply_portal_messages_migration.py
"""
import re
import sys
from pathlib import Path

import psycopg2

ENV = Path(__file__).resolve().parents[3] / ".env"

SQL = """
create table if not exists portal_messages (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null,
  contact text not null,
  role text not null,
  content text not null,
  created_at timestamptz not null default now()
);
alter table portal_messages enable row level security;
create index if not exists idx_portal_messages_thread
  on portal_messages (tenant_id, contact, created_at);
"""


def env_value(key: str) -> str | None:
    for line in ENV.read_text(encoding="utf-8").splitlines():
        m = re.match(rf"\s*#?\s*{key}\s*=\s*(.+)", line)
        if m:
            return m.group(1).strip().strip("'\"")
    return None


def main() -> int:
    password = env_value("DB_PASSWORD")
    if not password:
        print("DB_PASSWORD not found in .env")
        return 1
    url = env_value("SUPABASE_URL") or ""
    ref = re.sub(r"^https?://", "", url).split(".")[0]
    conn = psycopg2.connect(
        host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
        dbname="postgres", user=f"postgres.{ref}", password=password)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(SQL)
        cur.execute("select count(*) from portal_messages")
        print(f"portal_messages ready — {cur.fetchone()[0]} rows")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
