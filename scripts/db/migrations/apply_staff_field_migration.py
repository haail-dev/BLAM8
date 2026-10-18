"""One-shot: "Staff can see" per CRM field (docs/plan-people.md slice 3).

Kris, when the roles were settled: default *Staff can see* off "for anything
onboarding marked as access information (codes, keys), on for name, phone, the
dependants' names and notes."

A field definition carries no such marker — the tenant types a label and a hint
in their own words — so the default is read from those words by
src/app/crm_visibility.py, and the owner can flip any of them on /app/crm/fields.

EXISTING rows are back-filled through the SAME function rather than defaulting
to visible: a live tenant that already has an "Alarm code" field must not have
it open to a staff member the moment their CRM door is ticked. The word list is
deliberately tight (see that module); a false positive is a tick box, a false
negative is somebody's alarm code.

Applied via the Supabase session pooler; safe to re-run — it only ever
re-computes the default for rows that have never been set by hand, which is
recorded by staff_can_see_set.
"""
import re
import sys

import psycopg2

sys.path.insert(0, ".")
from app.crm_visibility import default_staff_can_see   # noqa: E402  (one source of truth)

ENV_PATH = ".env"
DDL = """
alter table crm_field_definitions
  add column if not exists staff_can_see boolean not null default true;
-- Has a human decided this one? Until they have, the value is the heuristic's
-- and may be re-derived; after, it is theirs and nothing touches it.
alter table crm_field_definitions
  add column if not exists staff_can_see_set boolean not null default false;
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

cur.execute("select id, label, extraction_hint, key, staff_can_see "
            "from crm_field_definitions where staff_can_see_set = false")
rows = cur.fetchall()
shut = 0
for fid, label, hint, key, current in rows:
    want = default_staff_can_see({"label": label, "extraction_hint": hint, "key": key})
    if want != current:
        cur.execute("update crm_field_definitions set staff_can_see = %s where id = %s",
                    (want, fid))
    if not want:
        shut += 1
        print(f"  closed to staff: {label!r}")
print(f"{len(rows)} definitions read, {shut} closed to staff by default")

cur.execute("select column_name from information_schema.columns "
            "where table_name='crm_field_definitions' order by ordinal_position")
print("crm_field_definitions:", [r[0] for r in cur.fetchall()])
cur.close(); conn.close()
