"""Seed / refresh the platform knowledge base — the concierge's recall layer.

Ingests app/platform_kb_content.SOURCES into the reserved sentinel platform tenant
so the website + in-app assistant can answer "what can BLAM8 do?" and "how do I…?"
from real retrieved knowledge.

Run once after deploy, and again whenever src/app/platform_kb_content.py changes.
Needs the environment's VOYAGE_API_KEY (embeddings) + Supabase service creds — so
run it against the env whose KB you want seeded (e.g. prod). Idempotent: each
source is replaced by name, so re-running is safe.

    python scripts/knowledge/seed_platform_kb.py
"""
import asyncio

from app import concierge, knowledge as kb


async def main() -> None:
    if not kb.is_configured():
        print("Voyage embeddings are not configured (VOYAGE_API_KEY missing) — "
              "cannot seed the platform KB. Set it in this environment and retry.")
        return
    print("Seeding the platform knowledge base…")
    result = await concierge.seed_platform_kb()
    total = sum(result.values())
    print(f"\nDone — {len(result)} sources, {total} chunks under the platform tenant:")
    for source, n in result.items():
        print(f"  • {source}: {n} chunk{'' if n == 1 else 's'}")


if __name__ == "__main__":
    asyncio.run(main())
