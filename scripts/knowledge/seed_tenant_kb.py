"""Seed the BLAM8 platform knowledge sources into a REGULAR tenant's KB.

The concierge's platform KB lives under a reserved sentinel tenant, which the
per-tenant email agent never reads. For BLAM8's own company tenant (the one
answering hello@blam8.com via the connected admin.blam8@gmail.com mailbox), we
want the same product knowledge available to the email agent — so this ingests
app/platform_kb_content.SOURCES into that tenant's own knowledge base.

Idempotent (each source is replaced by name). Re-run whenever
src/app/platform_kb_content.py changes.

    python scripts/knowledge/seed_tenant_kb.py <tenant-id | mailbox-address | owner-email>
"""
import asyncio
import re
import sys

import psycopg2

from app import knowledge as kb
from app.platform_kb_content import SOURCES

ENV_PATH = ".env"


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


def dsn() -> dict:
    ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)
    return dict(
        host="aws-1-eu-west-2.pooler.supabase.com", port=5432, dbname="postgres",
        user=f"postgres.{ref}", password=env_value("DB_PASSWORD"), sslmode="require",
    )


def resolve_tenant(needle: str) -> tuple[str, str] | None:
    """Return (tenant_id, tenant_name) from an id, mailbox address or owner email."""
    conn = psycopg2.connect(**dsn())
    cur = conn.cursor()
    try:
        cur.execute("select id, name from tenants where id::text = %s", (needle,))
        if row := cur.fetchone():
            return str(row[0]), row[1]
        cur.execute(
            "select t.id, t.name from tenants t "
            "join email_connections ec on ec.tenant_id = t.id "
            "where lower(ec.address) = lower(%s) limit 1", (needle,))
        if row := cur.fetchone():
            return str(row[0]), row[1]
        cur.execute(
            "select t.id, t.name from tenants t "
            "join tenant_members tm on tm.tenant_id = t.id "
            "join auth.users u on u.id = tm.user_id "
            "where lower(u.email) = lower(%s) limit 1", (needle,))
        if row := cur.fetchone():
            return str(row[0]), row[1]
        return None
    finally:
        conn.close()


async def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        return
    if not kb.is_configured():
        print("VOYAGE_API_KEY missing in this environment — cannot embed. Abort.")
        return
    found = resolve_tenant(sys.argv[1])
    if not found:
        print(f"No tenant found for {sys.argv[1]!r}.")
        return
    tenant_id, name = found
    print(f"Seeding {len(SOURCES)} platform sources into tenant {name!r} ({tenant_id})…")
    total = 0
    for source, text in SOURCES.items():
        n = await kb.ingest(tenant_id, source, text)
        total += n
        print(f"  • {source}: {n} chunk{'' if n == 1 else 's'}")
        await asyncio.sleep(0.6)  # gentle pacing — the embeddings API rate-limits bursts
    print(f"Done — {total} chunks.")


if __name__ == "__main__":
    asyncio.run(main())
