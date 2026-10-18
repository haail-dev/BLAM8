"""One-shot: ?src= attribution (Kris 2026-09-22) — so a short on YouTube can be
joined to the clicks and the signups it produced.

Two halves, mirroring how partner referrals already work (src/app/partners.py):

- `src_hits` counts anonymous landings carrying ?src=, per source per day. The
  visitor has no tenant yet, so tenant_visits cannot hold this.
- `tenants.signup_src` is first-touch attribution, written once when a NEW
  tenant signs up carrying the cookie.

`bump_src_hit` is a function rather than a read-then-write from the app: two
landings in the same second would otherwise race and lose a count.

Guarded throughout; safe to re-run. psycopg2 via the session pooler,
DB_PASSWORD read from .env (a commented-out line still counts).
"""
import re

import psycopg2

ENV_PATH = ".env"

MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS src_hits (
  src  text    not null,
  day  date    not null,
  hits integer not null default 0,
  primary key (src, day)
);
-- No policies: operator analytics, service-role only (same shape as the other
-- platform tables). RLS on with no policy = nothing but the service key reads it.
ALTER TABLE src_hits ENABLE ROW LEVEL SECURITY;

CREATE OR REPLACE FUNCTION bump_src_hit(p_src text)
RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path = public AS $fn$
  INSERT INTO src_hits (src, day, hits) VALUES (p_src, current_date, 1)
  ON CONFLICT (src, day) DO UPDATE SET hits = src_hits.hits + 1;
$fn$;

ALTER TABLE tenants ADD COLUMN IF NOT EXISTS signup_src text;
CREATE INDEX IF NOT EXISTS tenants_signup_src_idx
  ON tenants (signup_src) WHERE signup_src IS NOT NULL;
"""


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(MIGRATION_SQL)
print("DDL applied")

cur.execute("""
  SELECT column_name FROM information_schema.columns
  WHERE table_name = 'src_hits' ORDER BY ordinal_position;
""")
print("src_hits:", [r[0] for r in cur.fetchall()])

cur.execute("""
  SELECT column_name FROM information_schema.columns
  WHERE table_name = 'tenants' AND column_name = 'signup_src';
""")
print("tenants.signup_src:", [r[0] for r in cur.fetchall()] or "MISSING")

cur.execute("SELECT proname FROM pg_proc WHERE proname = 'bump_src_hit';")
print("function:", [r[0] for r in cur.fetchall()] or "MISSING")

cur.close()
conn.close()
