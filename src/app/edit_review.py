"""Edit review (Kris 2026-09-29): when the owner changes a draft before sending
it, ask — once, after the send, never blocking it — whether the change was a
missing FACT (knowledge), a standing RULE (always / never), or just this once.

Two halves:
  substantive()  — the gate. A typo, a greeting swap or a re-punctuation asks
                   nothing; a sentence added or reworded does.
  suggest()      — one small structured-output call: the original draft and
                   what was sent, back comes what to save and where.
"""
from __future__ import annotations

import difflib
import json
import logging
import re

from app import agent, llm, usage
from app.config import settings

log = logging.getLogger(__name__)

# Words that changed (added + removed, case and punctuation ignored) before the
# edit is worth a question. Below this it is a typo fix or a swapped greeting.
MIN_CHANGED_WORDS = 6

KINDS = ("knowledge", "always", "never", "none")

SUGGESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "title": {"type": "string"},
        "text": {"type": "string"},
        "why": {"type": "string"},
    },
    "required": ["kind", "title", "text", "why"],
    "additionalProperties": False,
}

_SYSTEM = (
    "A small business's AI agent drafted an email reply and the owner edited it before "
    "sending. Work out WHY they changed it and whether anything is worth teaching the agent.\n"
    "kind:\n"
    "- knowledge: the owner added or corrected a FACT about the business (a price, a policy, "
    "hours, what is included). text = the fact, written as a standalone statement the agent "
    "could look up later. title = a short label.\n"
    "- always: the owner keeps adding the same thing or doing it the same way — a standing "
    "instruction. text = one short imperative line, e.g. 'Ask which entrance to use'.\n"
    "- never: the owner removed something the agent should not have said or done. text = one "
    "short imperative line starting with a verb, e.g. 'Quote a firm price before seeing the job'.\n"
    "- none: a one-off (this customer's circumstances), a wording or tone preference, or a "
    "change too small to teach anything. Leave text and title empty.\n"
    "why = one plain sentence to the owner saying what you noticed ('You added the late "
    "collection fee.'). Never invent facts the owner did not write; take them from the sent "
    "text. Prefer none when unsure."
)


def _words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9£$€%.:/@-]+", (s or "").lower().replace("\r\n", "\n"))


def changed_words(original: str, edited: str) -> int:
    """Words added or removed between the two (a replace counts both sides)."""
    a, b = _words(original), _words(edited)
    n = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag != "equal":
            n += (i2 - i1) + (j2 - j1)
    return n


def substantive(original: str | None, edited: str | None) -> bool:
    """Worth asking about? An empty original (the owner wrote it outright) never is."""
    if not (original or "").strip() or not (edited or "").strip():
        return False
    return changed_words(original, edited) >= MIN_CHANGED_WORDS


async def suggest(tenant_id: str, original: str, sent: str) -> dict:
    """{kind, title, text, why} — kind 'none' when there's nothing to teach or the
    call failed (the card simply doesn't appear)."""
    none = {"kind": "none", "title": "", "text": "", "why": ""}
    try:
        msg = await llm.create(
            client=agent.client(), tenant_id=tenant_id,
            operation="edit_review", module="inbox", cache=False,
            model=settings.anthropic_model, max_tokens=400, system=_SYSTEM,
            messages=[{"role": "user", "content":
                       f"The agent's draft:\n{original[:4000]}\n\nWhat the owner sent:\n{sent[:4000]}"}],
            output_config={"format": {"type": "json_schema", "schema": SUGGESTION_SCHEMA}},
        )
        usage.record_anthropic(tenant_id, settings.anthropic_model, msg.usage,
                               operation="edit_review", module="inbox")
        text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
        out = json.loads(text)
        if out.get("kind") not in KINDS or not (out.get("text") or "").strip():
            return none
        return {k: str(out.get(k) or "").strip() for k in ("kind", "title", "text", "why")}
    except Exception:
        log.warning("edit review suggestion failed for tenant %s", tenant_id, exc_info=True)
        return none
