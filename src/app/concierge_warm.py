"""Prime the voice Concierge after a deploy (Kris 2026-09-24: "is there any
way we can prime the schema & redefined diary lookup?").

The first voice question after a change to the answer schema or to a lookup's
definition sat 13-20 s in its first round (tools/voice_latency.py: COLD
STALL) while Anthropic prepared the constrained format — the answer schema
together with the tool definitions. That preparation is cached, and it
depends only on the schema and the tools, never on the business or the
question. So on start-up one tiny request per tool set that voice actually
sends — the owner's (Pro and Starter) and the usual Staff doors — pays it
before anyone is waiting. A one-line system prompt and a few output tokens:
a fraction of a penny per deploy.

Render only (the RENDER env var); never under tests or on a laptop.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

log = logging.getLogger(__name__)


def tool_sets() -> list[tuple[str, list[dict]]]:
    """The tool lists live voice sends (concierge_routes.voice_chat): the
    catalogue for the asker plus the two drafting tools and the task list."""
    from app import concierge_data, concierge_draft, concierge_tasks, people
    extra = [concierge_draft.DRAFT_TOOL, concierge_draft.SHOW_TOOL, *concierge_tasks.foreground_tools()]
    staff = people.member({"user_id": "warm", "role": "staff",
                           "scopes": {"pages": ["today", "diary"]}})
    return [("owner", concierge_data.tools_for(True, None) + extra),
            ("owner-starter", concierge_data.tools_for(False, None) + extra),
            ("staff", concierge_data.tools_for(True, staff) + extra)]


async def warm() -> None:
    from app import concierge, llm
    from app.config import settings
    for label, tools in tool_sets():
        t0 = time.monotonic()
        try:
            await llm.create(
                client=concierge.client(), tenant_id=None, operation="concierge_warm",
                module="concierge", model=settings.anthropic_model, max_tokens=60,
                system="Warm-up. Reply with an empty action and the word ok.",
                messages=[{"role": "user", "content": "ok"}],
                tools=tools,
                output_config={"format": {"type": "json_schema",
                                          "schema": concierge.BUSINESS_SCHEMA}})
            log.info("concierge warm %s: %.1fs", label, time.monotonic() - t0)
        except Exception:
            log.warning("concierge warm %s failed", label, exc_info=True)


def should_warm() -> bool:
    from app.config import settings
    return bool(os.environ.get("RENDER")) and bool(settings.anthropic_api_key)


def start() -> None:
    if should_warm():
        asyncio.get_event_loop().create_task(warm())
