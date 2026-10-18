"""Backfill scheduling + crm for the £39 tier (2026-07-24 two-tier collapse).

Julius's landing overhaul made the £39 tier ("email", now sold as Starter)
carry the WHOLE loop: scheduling, calendar sync and the self-filling CRM moved
down from £99 (docs/archive/2026-07/landing-overhaul-2026-07-24.md — deliberately superseding
the same-day email-first rule in docs/crm-vs-os-architecture.md §1).
plans.BASE_PLAN_MODULES now bundles {scheduling, crm} on 'email', but billing
only reconciles modules when a subscription EVENT arrives — an existing £39
tenant would stay locked out until their next webhook. This grants the two
modules to every tenant already on the 'email' plan, so the page never
advertises what a paying tenant doesn't have.

Idempotent: inserts missing rows, reactivates non-active ones (a previously
paused row must not shadow the new entitlement). Direct-host psycopg2, the
house pattern (DB_PASSWORD comment in .env). Run WITH (before or right after)
deploying the entitlement commit.

Run: python scripts/db/backfill_email_tier_modules.py
"""
import re

import psycopg2

ENV = ".env"
env_text = open(ENV, encoding="utf-8").read()
pw = re.search(r"^#\s*DB_PASSWORD\s*[:=]?\s*(\S+)", env_text, re.M).group(1)
ref = re.search(r"https?://([a-z0-9]+)\.supabase\.co", env_text).group(1)

INSERT_SQL = """
insert into subscriptions (tenant_id, module, status)
select t.id, m.module, 'active'
from tenants t
cross join (values ('scheduling'), ('crm')) as m(module)
where t.plan = 'email'
  and not exists (select 1 from subscriptions s
                  where s.tenant_id = t.id and s.module = m.module)
returning tenant_id, module
"""

REACTIVATE_SQL = """
update subscriptions s
set status = 'active'
from tenants t
where t.id = s.tenant_id
  and t.plan = 'email'
  and s.module in ('scheduling', 'crm')
  and s.status <> 'active'
returning s.tenant_id, s.module
"""

conn = psycopg2.connect(host=f"db.{ref}.supabase.co", port=5432, dbname="postgres",
                        user="postgres", password=pw, sslmode="require",
                        connect_timeout=10)
cur = conn.cursor()
cur.execute(INSERT_SQL)
inserted = cur.fetchall()
cur.execute(REACTIVATE_SQL)
reactivated = cur.fetchall()
conn.commit()
print(f"inserted {len(inserted)} module row(s), reactivated {len(reactivated)}")

# Show the resulting state so the run log says who has what.
cur.execute("""
select coalesce(t.plan, '(no plan)') as plan, s.module,
       count(*) filter (where s.status = 'active') as active
from tenants t
join subscriptions s on s.tenant_id = t.id
where s.module in ('scheduling', 'crm')
group by 1, 2
order by 1, 2
""")
for plan, module, active in cur.fetchall():
    print(f"  {plan:14s} {module:11s} active={active}")
cur.close()
conn.close()
