"""One-shot: backfill the CRM (crm_companies / crm_contacts / crm_activities +
default stages) from historical email_log — every tenant, or just one:
`python scripts/db/backfill_crm.py <tenant_id>`. Idempotent: unique keys dedupe, so
a re-run lands on the same rows. Run AFTER scripts/db/migrations/apply_crm_migration.py."""
import sys

from app import crm, db

if sys.argv[1:]:
    tenants = [{"id": t, "name": "(by id)"} for t in sys.argv[1:]]
else:
    tenants = db.db().table("tenants").select("id,name").execute().data or []

for t in tenants:
    tally = crm.backfill_tenant(t["id"])
    print(f"{t['id']} {t.get('name') or ''}: {tally['rows']} emails -> "
          f"{tally['activities']} new activities")
print("DONE")
