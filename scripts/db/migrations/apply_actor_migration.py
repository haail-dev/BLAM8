"""One-shot: who did it (docs/plan-people.md slice 4, "names on actions").

The Agent Log has always been able to say WHAT happened and WHEN. With more
than one person on a business it has to say WHO — that is a third of the stated
scope ("delegation, record keeping & quick sharing info", Kris 2026-09-13), and
"someone cancelled BK-1042" is not a record.

  agent_events.actor_user_id  — the signed-in person whose request caused it.
                                NULL means BLAM8 itself did it, which is the
                                honest majority: drafting, the nightly jobs,
                                a webhook. Never guessed.
  agent_events.actor_name     — their name AS IT WAS, denormalised like
                                staff_notes does, so the record still reads
                                after they have left and their row is gone.

Both nullable, so every existing row stays exactly what it was: unattributed,
because nobody knows who did those and inventing an answer would be worse than
the gap.

Applied via the Supabase session pooler; safe to re-run.
"""
import re

import psycopg2

ENV_PATH = ".env"
DDL = """
alter table agent_events add column if not exists actor_user_id uuid;
alter table agent_events add column if not exists actor_name text;
create index if not exists agent_events_actor_idx
  on agent_events (tenant_id, actor_user_id, created_at desc);
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
cur.execute("select column_name from information_schema.columns "
            "where table_name='agent_events' order by ordinal_position")
print("agent_events:", [r[0] for r in cur.fetchall()])
cur.execute("select count(*) from agent_events where actor_user_id is not null")
print("already attributed:", cur.fetchone()[0])
cur.close(); conn.close()
