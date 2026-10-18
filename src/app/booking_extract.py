"""Perception with citations — dates a customer wrote, each with its quote.

docs/plan-grounded-booking.md. Runs once per draft, only when
`scheduling_config.grounded_dates` is on, and only ever WIDENS the allowed set
in src/app/grounding.py — it exists for the long tail the deterministic parser
will not resolve ("the weekend after next", "same dates as last year").

The citation is the whole point. A date is admitted only with the exact span
it came from, and `admit()` — pure, tested — drops any item whose quote is not
in the source. So this is not a second place the model can invent a date: it
cannot quote what is not there. Mirrors src/app/dependants_extract.py in how it
calls the model and meters the tokens.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta

from app import grounding

log = logging.getLogger(__name__)

# Structured output: no `format`/`pattern` keywords — the API rejects them
# (see the dependants extractor). ISO validity is checked in admit().
SCHEMA = {
    "type": "object",
    "properties": {
        "dates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"iso": {"type": "string"}, "quote": {"type": "string"}},
                "required": ["iso", "quote"], "additionalProperties": False,
            },
        },
        "ranges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"from_iso": {"type": "string"}, "to_iso": {"type": "string"},
                               "quote": {"type": "string"}},
                "required": ["from_iso", "to_iso", "quote"], "additionalProperties": False,
            },
        },
    },
    "required": ["dates", "ranges"],
    "additionalProperties": False,
}

SYSTEM = (
    "You read the DATES a customer wrote in one email to a business, for a booking. "
    "Today's date is given. Return every calendar date or date range the customer "
    "themselves wrote, resolved to ISO (YYYY-MM-DD), and for EACH one the exact "
    "words it came from, copied character-for-character from the email as `quote`. "
    "A time of day alone (8am, 5pm, 17:00) is NOT a date — leave it out. Do not "
    "infer dates the customer did not write; if the email names none, return "
    "empty lists. Never invent. A quote that is not in the email will be discarded."
)

MAX_BODY_CHARS = 4000
MAX_RANGE_DAYS = 62


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def admit(items: dict, source_text: str, today: date | None = None) -> list[tuple[date, str]]:
    """(date, quote) pairs the model may add — ONLY those whose quote is
    genuinely in the source and whose ISO parses. Pure."""
    src = _norm(source_text)
    out: list[tuple[date, str]] = []
    seen: set[tuple[str, str]] = set()

    def _ok_quote(q: str) -> bool:
        q = _norm(q)
        return bool(q) and len(q) >= 2 and q in src

    def _iso(s: str) -> date | None:
        try:
            return date.fromisoformat(str(s or "").strip()[:10])
        except ValueError:
            return None

    for it in (items or {}).get("dates") or []:
        if not isinstance(it, dict):
            continue
        d, q = _iso(it.get("iso")), str(it.get("quote") or "")
        if d and _ok_quote(q) and (d.isoformat(), _norm(q)) not in seen:
            seen.add((d.isoformat(), _norm(q)))
            out.append((d, q.strip()))
    for it in (items or {}).get("ranges") or []:
        if not isinstance(it, dict):
            continue
        a, b, q = _iso(it.get("from_iso")), _iso(it.get("to_iso")), str(it.get("quote") or "")
        if not (a and b and _ok_quote(q)) or b < a or (b - a).days > MAX_RANGE_DAYS:
            continue
        for i in range((b - a).days + 1):
            d = a + timedelta(days=i)
            if (d.isoformat(), _norm(q)) not in seen:
                seen.add((d.isoformat(), _norm(q)))
                out.append((d, q.strip()))
    return out


def _user_prompt(subject: str, body: str, today: date) -> str:
    return (f"Today is {today.isoformat()} ({today.strftime('%A')}).\n\n"
            f"Subject: {subject or '(none)'}\n"
            f"Email:\n{(body or '')[:MAX_BODY_CHARS]}")


async def enrich(tenant_id: str, g: grounding.Grounding, *, subject: str, body: str,
                 today: date, email_log_id: str | None = None) -> int:
    """Widen `g` with cited dates from the model. Returns how many were added.
    Best-effort: any failure leaves the deterministic set exactly as it was —
    a perception hiccup must never block a draft, and the gate is still there."""
    if g is None or not g.on:
        return 0
    try:
        from app import agent, llm, usage
        from app.config import settings
        msg = await llm.create(
            client=agent.client(), tenant_id=tenant_id, operation="booking_extract",
            model=settings.anthropic_model, max_tokens=600, system=SYSTEM,
            messages=[{"role": "user", "content": _user_prompt(subject, body, today)}],
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
        usage.record_anthropic(tenant_id, msg.model, msg.usage, operation="booking_extract",
                               email_log_id=email_log_id)
        try:
            items = json.loads(text)
        except json.JSONDecodeError:
            return 0
        added = 0
        for d, q in admit(items, f"{subject or ''}\n{body or ''}", today):
            before = d.isoformat() in g.dates
            g.allow(d, f"extract: '{q}'")
            added += 0 if before else 1
        return added
    except Exception:
        log.warning("booking extract failed (tenant=%s) — deterministic grounding stands",
                    tenant_id, exc_info=True)
        return 0
