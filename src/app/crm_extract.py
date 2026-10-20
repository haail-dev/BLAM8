"""CRM custom-field extraction with typed validation owned by crm_fields.

The general model remains the default. Explicitly reviewed ordinary fields may
use a server-enabled candidate; those results can only become suggestions.
"""
import json
import logging
import re
from datetime import date

from app import agent, llm, usage, model_routing
from app.config import settings

log = logging.getLogger(__name__)

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string", "enum": ["company", "contact", "deal"]},
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["entity", "key", "value", "confidence"],
                "additionalProperties": False,   # the API requires this explicitly
            },
        }
    },
    "required": ["fields"],
    "additionalProperties": False,
}

MAX_BODY_CHARS = 4000   # plenty for an SMB email; keeps the call cheap and fast

SYSTEM = (
    "You extract CRM field values from one business email. You are given the "
    "fields a company tracks (each with an entity, key, type and a hint about "
    "what to look for) and the email. Return ONLY values the email actually "
    "supports — never guess, never fill from general knowledge. confidence is "
    "your certainty the value is correct for THIS sender (1.0 = stated "
    "outright, 0.5 = implied, lower = speculative). Omit fields the email "
    "says nothing about. Values are strings: numbers as digits, dates as "
    "YYYY-MM-DD, booleans as true/false, dropdowns as one of the listed options."
)


def build_user_prompt(definitions: list[dict], subject: str | None, body: str | None,
                      from_name: str | None = None, today: date | None = None) -> str:
    today = today or date.today()
    # Run #8 (2026-09-05): "my test is on 14 December" landed as 2024-12-14 —
    # the prompt carried no today and no rule for a date given without a year.
    lines = [f"Today is {today.isoformat()} ({today.strftime('%A')}). A date the email "
             "gives WITHOUT a year is the next such date from today for anything ahead "
             "(a test, an appointment, a booking, a due or expiry date) and the nearest "
             "such date to today otherwise; never a year the email does not state.",
             "Fields to look for:"]
    for d in definitions:
        opts = f" options={d['options']}" if d.get("options") else ""
        hint = f" — {d['extraction_hint']}" if d.get("extraction_hint") else ""
        lines.append(f"- entity={d['entity']} key={d['key']} "
                     f"({d['label']}, type={d['field_type']}{opts}){hint}")
    lines.append("")
    lines.append(f"Email from: {from_name or 'unknown'}")
    lines.append(f"Subject: {subject or '(none)'}")
    body = agent.strip_quoted(body or "")[:MAX_BODY_CHARS]
    lines.append(f"Body:\n{body}")
    return "\n".join(lines)


def parse_response(text: str) -> list[dict]:
    """The schema-constrained reply as a list of extraction dicts; anything
    unparseable is an empty extraction, never an error."""
    try:
        data = json.loads(text)
        fields = data.get("fields") or []
        return [f for f in fields if isinstance(f, dict)]
    except (json.JSONDecodeError, AttributeError, TypeError):
        return []


async def extract(tenant_id: str, definitions: list[dict], *, subject: str | None,
                  body: str | None, from_name: str | None = None,
                  email_log_id: str | None = None) -> list[dict]:
    """Run one extraction call. Returns raw extractions for
    crm_fields.plan_application; [] when there's nothing to ask about."""
    from app import crm_fields
    # Health / medical / allergy fields are never read from email (security
    # review M39) — so their labels and hints never reach the model either.
    definitions = [d for d in definitions
                   if (d.get("extraction_hint") or d.get("label"))
                   and not crm_fields.is_special_category(d)]
    if not definitions:
        return []
    authority = (model_routing.Authority.SUGGESTION if
                 model_routing.ordinary_crm_fields(definitions) else
                 model_routing.Authority.RECORD_WRITE)
    decision = model_routing.resolve_model(model_routing.Operation.CRM_FIELDS,
                                          authority=authority,
                                          fallback_model=settings.anthropic_model)
    request = dict(
        tenant_id=tenant_id, operation="crm_extract",
        max_tokens=600,
        system=SYSTEM,
        messages=[{"role": "user",
                   "content": build_user_prompt(definitions, subject, body, from_name)}],
        output_config={"format": {"type": "json_schema", "schema": EXTRACT_SCHEMA}},
    )
    candidate_result = False
    if decision.candidate:
        msg, fields, totals, candidate_result = await model_routing.create_candidate(
            decision=decision, client=agent.client(),
            validate=lambda m: _validated_fields(m, definitions), **request)
    else:
        msg = await llm.create(client=agent.client(), model=decision.model,
                               route_metadata=decision.metadata(), **request)
        fields = parse_response(_message_text(msg))
        totals = msg.usage
    # The marker is assigned by trusted code after parsing, never accepted from JSON.
    for field in fields:
        field.pop("_suggestion_only", None)
        if candidate_result:
            field["_suggestion_only"] = True
    usage.record_anthropic(tenant_id, msg.model, totals, operation="crm_extract",
                           email_log_id=email_log_id)
    return resolve_date_years(definitions, fields,
                              f"{subject or ''}\n{body or ''}")


def _message_text(msg) -> str:
    return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()


def _validated_fields(msg, definitions) -> list[dict]:
    from app import crm_fields
    data = json.loads(_message_text(msg))
    if not isinstance(data, dict) or set(data) != {"fields"} or not isinstance(data["fields"], list):
        raise ValueError("invalid fields envelope")
    by_key = {(d["entity"], d["key"]): d for d in definitions}
    seen = set()
    for f in data["fields"]:
        if not isinstance(f, dict) or set(f) != {"entity", "key", "value", "confidence"}:
            raise ValueError("invalid field shape")
        identity = (f["entity"], f["key"])
        d = by_key.get(identity)
        c = f["confidence"]
        if (not d or identity in seen or not isinstance(f["value"], str)
                or isinstance(c, bool) or not isinstance(c, (int, float)) or not 0 <= c <= 1
                or not crm_fields.coerce_value(d["field_type"], f["value"], d.get("options"))[0]):
            raise ValueError("invalid field identity or typed value")
        seen.add(identity)
    return data["fields"]


_AHEAD_WORDS = re.compile(r"\b(?:test|exam|appointment|booking|visit|due|expir\w*|renew\w*|"
                          r"start|deadline|next)\b", re.IGNORECASE)


def _year_stated(year: int, text: str) -> bool:
    """True when the email itself carries that year — as four digits, or as
    the two-digit tail of a written date (14/12/24, 14-12-24, '24)."""
    if re.search(rf"\b{year}\b", text):
        return True
    yy = f"{year % 100:02d}"
    return bool(re.search(rf"(?:\d{{1,2}}[/.\-]\d{{1,2}}[/.\-]|')\s*{yy}\b", text))


def resolve_date_years(definitions: list[dict], fields: list[dict], text: str,
                       today: date | None = None) -> list[dict]:
    """A date field whose year the email never stated gets the year the sender
    meant, not the one the model reached for: the next such day-month from
    today for a field that looks ahead (an expiring date, a test / appointment /
    due label), the nearest such day-month otherwise. A year the email does
    state — a date of birth, "back in 2024" — is left exactly as extracted."""
    today = today or date.today()
    by_key = {(d.get("entity"), d.get("key")): d for d in definitions}
    for f in fields:
        d = by_key.get((f.get("entity"), f.get("key")))
        if not d or d.get("field_type") != "date":
            continue
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(f.get("value") or ""))
        if not m:
            continue
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if _year_stated(year, text or ""):
            continue
        ahead = d.get("durability") == "expires" or bool(_AHEAD_WORDS.search(d.get("label") or ""))
        candidates = []
        for y in (today.year - 1, today.year, today.year + 1):
            try:
                candidates.append(date(y, month, day))
            except ValueError:          # 29 Feb in a non-leap year
                continue
        if not candidates:
            continue
        if ahead:
            pick = next((c for c in candidates if c >= today), candidates[-1])
        else:
            pick = min(candidates, key=lambda c: abs((c - today).days))
        f["value"] = pick.isoformat()
    return fields
