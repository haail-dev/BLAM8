"""One-shot (security review 2026-09, M15): what account closure left behind.

  1. portal_messages had no foreign key, so a closed business's portal chats
     stayed. Deletes the orphan rows, then adds the key with ON DELETE CASCADE.
  2. Stored files under a tenant id that no longer exists (documents,
     condition photos, the PUBLIC portal banner) are removed.

Dry run by default — prints what it WOULD delete. `--apply` deletes.
Idempotent."""
import re
import sys

import psycopg2

APPLY = "--apply" in sys.argv
ENV_PATH = ".env"


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)
conn = psycopg2.connect(host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
                        user=f"postgres.{ref}", password=env_value("DB_PASSWORD"),
                        dbname="postgres", sslmode="require")
conn.autocommit = False
cur = conn.cursor()
cur.execute("select count(*) from portal_messages m where not exists "
            "(select 1 from tenants t where t.id = m.tenant_id)")
orphan_msgs = cur.fetchone()[0]
cur.execute("select id::text from tenants")
live = {r[0] for r in cur.fetchall()}
print(f"portal_messages orphan rows: {orphan_msgs}")

if APPLY:
    cur.execute("delete from portal_messages m where not exists "
                "(select 1 from tenants t where t.id = m.tenant_id)")
    cur.execute("""
      do $$ begin
        if not exists (select 1 from pg_constraint where conname = 'portal_messages_tenant_fk') then
          alter table portal_messages add constraint portal_messages_tenant_fk
            foreign key (tenant_id) references tenants(id) on delete cascade;
        end if;
      end $$;""")
    cur.execute("notify pgrst, 'reload schema'")
    conn.commit()
    print("portal_messages: orphans deleted, foreign key in place")
cur.close(); conn.close()

# Files: top-level folders in each bucket are tenant ids.
from app import storage  # noqa: E402  (after .env is known to exist)
from app.db import db  # noqa: E402

for name in storage.TENANT_BUCKETS:
    try:
        top = db().storage.from_(name).list("", {"limit": 1000}) or []
    except Exception as e:  # noqa: BLE001
        print(f"{name}: can't list ({type(e).__name__})")
        continue
    gone = [e["name"] for e in top if e.get("id") is None and e.get("name") not in live]
    for tid in gone:
        counts = storage.remove_tenant_files(tid, dry_run=not APPLY)
        print(f"{name}: closed business {tid[:8]}… {counts.get(name)} object(s)"
              f" {'deleted' if APPLY else 'would be deleted'}")
print("dry run — nothing deleted; re-run with --apply" if not APPLY else "done")
