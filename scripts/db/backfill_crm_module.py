"""Backfill the 'crm' module rows for tenants who keep the CRM (2026-07-24).

The CRM became a Core-tier unlock (Julius's tier decision, docs/
crm-vs-os-architecture.md §1): a 'crm' subscriptions row now gates the UI, the
API, the pipeline's CRM LLM passes and the sequence sweep. Until today the CRM
was ungated, so EVERY existing tenant has it — this backfill preserves it for
exactly the tenants whose entitlement says so, before the gating code deploys:

  - any tenant on a plan that bundles it (starter / professional / scale /
    enterprise — plans.BASE_PLAN_MODULES), and
  - any tenant with an ACTIVE 'scheduling' row (operator-granted Core-grade:
    demo/dogfood tenants without a Stripe plan keep the CRM they demo with).

Everyone else (the £39 email tier, plan-less pilots) is email-first by design —
they lose the CRM surfaces and stop paying its LLM costs; the zero-LLM ingest
keeps pre-filling so upgrade day lands on a full CRM.

Idempotent (anti-join on an existing 'crm' row). Direct-host psycopg2, the
house pattern (DB_PASSWORD comment in .env). Run BEFORE deploying the gating
commit so no entitled tenant ever sees the CRM vanish.

Run: python scripts/db/backfill_crm_module.py
"""
import re
import sys

import psycopg2

ENV = ".env"
env_text = open(ENV, encoding="utf-8").read()
pw = re.search(r"^#\s*DB_PASSWORD\s*[:=]?\s*(\S+)", env_text, re.M).group(1)
ref = re.search(r"https?://([a-z0-9]+)\.supabase\.co", env_text).group(1)

SQL = """
insert into subscriptions (tenant_id, module, status)
select t.id, 'crm', 'active'
from tenants t
where (t.plan in ('starter', 'professional', 'scale', 'enterprise')
       or exists (select 1 from subscriptions s
                  where s.tenant_id = t.id
                    and s.module = 'scheduling' and s.status = 'active'))
  and not exists (select 1 from subscriptions s2
                  where s2.tenant_id = t.id and s2.module = 'crm')
returning tenant_id
"""

conn = psycopg2.connect(host=f"db.{ref}.supabase.co", port=5432, dbname="postgres",
                        user="postgres", password=pw, sslmode="require",
                        connect_timeout=10)
cur = conn.cursor()
cur.execute(SQL)
granted = [r[0] for r in cur.fetchall()]
conn.commit()
print(f"granted 'crm' to {len(granted)} tenant(s)")

# Verify + show the resulting split so the run log says who kept what.
cur.execute("""
select coalesce(t.plan, '(no plan)') as plan,
       count(*) filter (where s.tenant_id is not null) as with_crm,
       count(*) filter (where s.tenant_id is null) as without_crm
from tenants t
left join subscriptions s
  on s.tenant_id = t.id and s.module = 'crm' and s.status = 'active'
group by 1 order by 1
""")
print(f"{'plan':<16}{'with crm':>10}{'without':>10}")
for plan, with_crm, without in cur.fetchall():
    print(f"{plan:<16}{with_crm:>10}{without:>10}")
conn.close()
sys.exit(0)
