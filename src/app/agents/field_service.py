"""Field Service (Trades) specialist agent — the engineer hand-off
(docs/modules/module-field-service.md). The trades twin of src/app/agents/tms.py.

The base email agent owns the customer relationship and the reply (one voice). When
a tenant's `fsm` module is active, the base agent gains ONE hand-off tool,
`ask_field_service`, whose executor invokes THIS agent. The field-service desk owns
everything about the job — and nothing else lives here that the base needs:

  * Its own system prompt: the trades tool-discipline rules (size the job from its
    type, emergency triage, skill-match, always check before promising, never invent a
    date, one hold only, "too short notice" ≠ "fully booked") — the same shape as the
    TMS desk's rules, reframed to fill a structured result because the base writes the
    reply.
  * The engineer capacity tools + executor: scheduling.FSM_TOOLS and
    make_engineer_executor — the atomic best-fit hold machinery (the engineer sibling
    of the fleet path). Holds are placed exactly as a fleet hold is, recorded in the
    executor's state.
  * The job-specific extraction (which job type, the site postcode, priority) — the
    base agent never sees the individual engineer or capacity internals.

The hand-off contract (mirrors TMS D6): in = the customer's job request (the base
passes the tenant/contact/thread/mailbox out-of-band via the closure, so a follow-up
in the same thread can't double-hold and the model can't spoof them); out = a
structured result {status, date, job_type, duration_minutes, required_skill, priority,
booking_ref, alternatives, assumed, ask, message}. A specialist error returns
{status:"error"} and the base says a colleague will confirm (D7) — it never invents a
booking the desk didn't return.
"""
from app import metering

import json
from datetime import date, datetime

from anthropic import AsyncAnthropic

from app import agent, db, llm, scheduling
from app.config import settings
from app.scheduling import DIARY_TZ

_client: AsyncAnthropic | None = None

# Cap on API calls in the FSM tool loop (check availability → maybe hold → report),
# mirroring the TMS desk + the base agent: the loop normally ends on its own; the cap
# is the backstop and the final permitted call forces a structured result via
# tool_choice 'none' so an answer always comes out.
FSM_TOOL_LOOP_MAX_CALLS = 5


# ── The hand-off tool the BASE agent sees ────────────────────────────────────
# Injected onto the base agent's draft call ONLY when the fsm module is active. Its
# executor (make_handoff, below) invokes this specialist. Deliberately minimal: the
# base passes the customer's words; the field-service desk does the extraction.
ASK_FIELD_SERVICE_TOOL = {
    "name": "ask_field_service",
    "description": (
        "Ask the field-service desk about a job, callout, repair, service, installation "
        "or site visit — anything about sending an engineer to do work, what it would "
        "cost in time, or booking a date. Use this for ANY job matter BEFORE answering: "
        "the desk checks which engineer is free and skilled, and, when the customer asks "
        "to book and it fits, places a provisional hold. Pass the customer's request in "
        "their own words, including any urgency (no heating, leak, no power). Returns a "
        "structured answer (a hold with a reference, dates to offer, can't-be-done, a "
        "question to ask the customer, or an error) that you compose into your reply. "
        "Never guess engineer availability, how long a job takes, or a booking yourself "
        "— always ask."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "request": {
                "type": "string",
                "description": "The customer's job request, in their own words — what "
                               "needs doing, any urgency, the date they want, and any "
                               "postcode or reference they stated.",
            },
        },
        "required": ["request"],
        "additionalProperties": False,
    },
}


# ── The structured result the specialist returns ─────────────────────────────
# Strict structured output: every field required (empties carry no-op values) so the
# base agent always gets the same shape to compose from.
FSM_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string",
                   "enum": ["held", "offered", "no_fit", "needs_info", "error"]},
        "date": {"type": "string"},            # held/primary date YYYY-MM-DD, else ""
        "job_type": {"type": "string"},        # the matched job type, else ""
        "duration_minutes": {"type": "number"},  # the job's size in minutes, else 0
        "required_skill": {"type": "string"},  # the skill the job needs, else ""
        "priority": {"type": "string", "enum": ["standard", "emergency", ""]},
        "booking_ref": {"type": "string"},     # the hold's reference when held, else ""
        # The cost estimate when quoted; all zeros / "" when not (total includes VAT).
        "quote": {
            "type": "object",
            "properties": {
                "callout": {"type": "number"},
                "labour": {"type": "number"},
                "parts": {"type": "number"},
                "total": {"type": "number"},
                "currency": {"type": "string"},
                "vat_pct": {"type": "number"},
            },
            "required": ["callout", "labour", "parts", "total", "currency", "vat_pct"],
            "additionalProperties": False,
        },
        "alternatives": {"type": "array", "items": {"type": "string"}},  # dates to offer
        "assumed": {"type": "array", "items": {"type": "string"}},       # stated assumptions
        "ask": {"type": "string"},             # the single clarifying question, else ""
        "message": {"type": "string"},         # short internal note for the base agent
    },
    "required": ["status", "date", "job_type", "duration_minutes", "required_skill",
                 "priority", "booking_ref", "quote", "alternatives", "assumed", "ask", "message"],
    "additionalProperties": False,
}

_EMPTY_QUOTE = {"callout": 0, "labour": 0, "parts": 0, "total": 0, "currency": "", "vat_pct": 0}
_ERROR_RESULT = {"status": "error", "date": "", "job_type": "", "duration_minutes": 0,
                 "required_skill": "", "priority": "", "booking_ref": "",
                 "quote": dict(_EMPTY_QUOTE), "alternatives": [], "assumed": [], "ask": "",
                 "message": "The field-service desk could not be reached."}


# ── The specialist's tool-discipline rules ───────────────────────────────────
# The trades sibling of TMS's _TRANSPORT_RULES: the discipline that produces honest
# holds, framed as "fill the structured result" (the base agent writes the reply).
_FIELD_SERVICE_RULES = (
    "Availability, job duration and bookings come ONLY from the tools, never from "
    "memory:\n"
    "- Map the customer's request to ONE of the configured job types (listed in the "
    "diary shape) and pass it as `job_type` — that is how the right duration and skill "
    "are used. If nothing matches, return status 'needs_info' with a question in `ask` "
    "asking which service they need (name the options).\n"
    "- EMERGENCY triage: no heating, no hot water, a leak, a gas smell, no power, or a "
    "lockout is an emergency — set priority 'emergency' and offer the EARLIEST date the "
    "tools return. A job type that is itself an emergency callout is always priority. "
    "Never promise 'today' or 'same day' unless the tool actually returns capacity that "
    "soon.\n"
    "- ALWAYS call check_engineer_availability before deciding or reporting any date, "
    "passing the job_type so capacity reflects the real job. Never report a date the "
    "tool did not return for that job; when the customer's date doesn't fit, put the "
    "nearest dates the tool returned in `alternatives`.\n"
    "- SKILL: if the tool shows no engineer holds the skill the job needs, return status "
    "'no_fit' — a colleague will arrange it; never promise a date you can't staff.\n"
    "- When the customer asks to book and the tool shows the job fits, you MUST call "
    "book_job BEFORE finishing — saying it is available is not enough, the hold is what "
    "reserves the engineer's time. One hold only. The hold is REAL, so a held result is "
    "true.\n"
    "- Only return status 'held' if book_job returned held:true in THIS conversation. If "
    "you did not place a hold, do not imply one — use status 'offered' with the fitting "
    "dates in `alternatives`.\n"
    "- book_job returns a booking_ref (e.g. 'BK-1042'). When you hold, ALWAYS carry it "
    "back in `booking_ref` — it is the customer's reference for any follow-up.\n"
    "- Only treat a date as fully booked when the tool showed capacity is actually used. "
    "If a date is unavailable for another reason the tool gives (too short notice, closed "
    "that day), report THAT in `message` and put the earliest possible dates in "
    "`alternatives` — do not call it 'fully booked'.\n"
    "- NEVER plan to 'check and get back later' — do all checking NOW with the tools.\n"
    "- QUOTING: both tools return a `quote` (callout + labour or a fixed job price, with "
    "the total INCLUDING VAT) computed from the business's rate card. When the customer "
    "asks the cost, or whenever you place a hold, copy that quote into the result's "
    "`quote` field so the assistant can state it. It is an ESTIMATE — never invent or "
    "round prices; use only the tool's figures.\n"
    "- Talk in dates, job types and references only. NEVER name the individual engineer "
    "or how many engineers there are — how the business staffs the work is internal.\n"
    "- When the email states the job-site postcode or the customer's own reference, pass "
    "them to book_job (site_postcode / in details) — exactly as stated, never invented.\n"
)

_RESULT_RULES = (
    "Return your answer ONLY in the structured result:\n"
    "- status: 'held' (you placed a hold), 'offered' (dates fit but you placed no hold — "
    "list them in `alternatives`), 'no_fit' (cannot be staffed/done), 'needs_info' (you "
    "must ask the customer something — put the ONE question in `ask`), or 'error'.\n"
    "- date: the held/primary date (YYYY-MM-DD) when status is 'held'; otherwise ''.\n"
    "- job_type: the matched job type when known, otherwise ''.\n"
    "- duration_minutes: the job's size in minutes when known, otherwise 0.\n"
    "- required_skill: the skill the job needs when known, otherwise ''.\n"
    "- priority: 'emergency' or 'standard' when known, otherwise ''.\n"
    "- booking_ref: the hold's reference when status is 'held', otherwise ''.\n"
    "- quote: the tool's cost estimate (callout, labour, parts, total — total includes "
    "VAT) when you have one to give the customer; otherwise all zeros.\n"
    "- alternatives: dates (YYYY-MM-DD) to offer when the asked date couldn't be booked, "
    "else [].\n"
    "- assumed: any assumption you made worth stating to the customer, else [].\n"
    "- ask: the single clarifying question for the customer, else ''.\n"
    "- message: a short internal note for the email assistant if useful, else ''.\n"
    "Never invent availability, a date, or a booking the tools did not return."
)


def client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


def build_system_prompt(diary_shape: str | None = None, today: date | None = None) -> str:
    """The field-service desk's prompt: role + the trades tool-discipline rules + how to
    fill the structured result. Agnostic of the tenant's voice (the base agent owns
    voice); the desk only returns facts. `today` is injected at DAY granularity so the
    prompt (and its cache) changes once a day, never per request."""
    day = today or datetime.now(DIARY_TZ).date()
    sections = [
        "You are the field-service desk for a trades business (plumbing, electrical, "
        "heating, and similar). The email assistant has asked you about a customer's "
        "job, callout or site visit. Work out the real answer using your tools and "
        "return it as a STRUCTURED result — you do NOT write to the customer; the email "
        "assistant composes the reply from what you return.",

        agent.date_anchor_line(day),
    ]
    if diary_shape:
        sections.append(f"Diary shape: {diary_shape}")
    sections.append(_FIELD_SERVICE_RULES)
    sections.append(_RESULT_RULES)
    return "\n\n".join(sections)


def _parse(text: str) -> dict:
    """Unpack the structured result, falling back to an error result on the rare
    non-JSON (a safety refusal or truncation) — the base agent then says a colleague
    will confirm rather than letting a parse error break drafting (D7)."""
    try:
        data = json.loads(text)
        if not isinstance(data, dict) or "status" not in data:
            return dict(_ERROR_RESULT)
        return {**_ERROR_RESULT, **{k: data[k] for k in FSM_SCHEMA["properties"] if k in data}}
    except (json.JSONDecodeError, AttributeError, TypeError):
        return dict(_ERROR_RESULT)


@llm.with_request_scope   # no-op when nested in a draft's scope — inherits the draft's id
async def answer(request: str, *, executor, diary_shape: str | None = None,
                 today: date | None = None, tenant_id: str | None = None) -> tuple[dict, dict]:
    """Run the field-service desk on one request. `executor` is the engineer capacity
    executor (scheduling.make_engineer_executor) bound to this email's tenant/contact/
    thread — it is what actually places the hold. Returns (structured result, usage
    totals). Mirrors the base/TMS tool loop: call the model; while it asks for a tool,
    execute it and feed the result back; cap the calls and force a structured answer on
    the last permitted call."""
    system = build_system_prompt(diary_shape=diary_shape, today=today)
    msgs: list[dict] = [{"role": "user", "content": request or ""}]
    totals = metering.Totals()
    msg = None
    for i in range(FSM_TOOL_LOOP_MAX_CALLS):
        kwargs = {"tools": scheduling.FSM_TOOLS}
        if i == FSM_TOOL_LOOP_MAX_CALLS - 1:
            kwargs["tool_choice"] = {"type": "none"}   # budget spent — answer now
        msg = await llm.create(
            client=client(), tenant_id=tenant_id, operation="fsm_handoff",
            model=settings.anthropic_booking_model,   # always a booking flow — use the stronger model
            max_tokens=2048,   # raised from 1024 with the base agent (truncated tool calls)
            system=system,
            messages=msgs,
            output_config={"format": {"type": "json_schema", "schema": FSM_SCHEMA}},
            **kwargs,
        )
        metering.add(totals, msg.usage)
        tool_uses = [b for b in msg.content if getattr(b, "type", None) == "tool_use"]
        # Truncation guard (mirrors the base agent, live e2e 2026-07-31): a
        # max_tokens stop is not an answer — a complete tool_use still executes,
        # otherwise the model redoes the turn concisely.
        if (msg.stop_reason == "max_tokens" and not tool_uses
                and i < FSM_TOOL_LOOP_MAX_CALLS - 1):
            msgs.append({"role": "assistant", "content": msg.content})
            msgs.append({"role": "user", "content":
                         "Your response was cut off at the length limit. Redo the turn "
                         "CONCISELY: tool call first, no preamble, then the JSON answer."})
            continue
        if msg.stop_reason not in ("tool_use", "max_tokens") or not tool_uses:
            break
        msgs.append({"role": "assistant", "content": msg.content})
        results = []
        for tu in tool_uses:
            try:
                out = executor(tu.name, dict(tu.input or {}))
            except Exception:
                # A capacity-tool failure must not kill the hand-off — tell the model
                # the truth so it returns an honest error/needs-info, and carry on.
                out = {"error": "tool_failed",
                       "message": "The diary is temporarily unavailable. Do not promise "
                                  "any booking; return status 'error'."}
            results.append({"type": "tool_result", "tool_use_id": tu.id,
                            "content": json.dumps(out)})
        msgs.append({"role": "user", "content": results})
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
    return _parse(text), totals


def outbound_reminder(booking: dict) -> str:
    """Reverse hand-off (D5 — Phase 3): frame a recurring-service-due job for a proactive
    rebook nudge — the job and when it was last done, NEVER the engineer (the same
    specialist→base split as ask_field_service, in reverse). Returns a plain facts string
    the base agent composes into the tenant's voice."""
    job = (booking.get("job_type") or "service").strip()
    parts = [f"The customer is due their recurring {job}."]
    if booking.get("service_due_date"):
        parts.append(f"It is due around {str(booking['service_due_date'])[:10]}.")
    if booking.get("booking_date"):
        parts.append(f"Their last {job} with us was on {str(booking['booking_date'])[:10]}.")
    if (booking.get("reference") or "").strip():
        parts.append(f"The customer's own reference: {booking['reference'].strip()}.")
    parts.append("Invite them to get back in touch to rebook — do NOT promise or suggest a "
                 "specific date or time (they have not asked yet).")
    return " ".join(parts)


def make_handoff(tenant_id: str, contact: str | None = None, thread_id: str | None = None,
                 mailbox_id: str | None = None, now: datetime | None = None, client=None):
    """Build the `ask_field_service` executor for ONE email — the bridge the base agent
    calls. Returns (execute, state):

      * execute(name, args) is ASYNC (it awaits the nested specialist call). It runs the
        field-service desk once per email — a second ask_field_service call returns the
        same cached answer, so the base can't trigger a second hold (mirrors the existing
        one-hold-per-email rule, enforced in code).
      * state['bookings'] is the SAME list the engineer executor appends its holds to, so
        the pipeline links/resolves them exactly as before. state['usage'] carries the
        desk's token totals so the pipeline can meter them onto the draft.

    The tenant/contact/thread/mailbox are bound HERE (not passed by the model), so a
    follow-up in the same thread can't double-hold and the model can't spoof them."""
    when = now or datetime.now(DIARY_TZ)
    eng_exec, eng_state = scheduling.make_engineer_executor(
        tenant_id, contact=contact, thread_id=thread_id, now=when, mailbox_id=mailbox_id)
    diary_shape = scheduling.fsm_diary_line(
        db.get_scheduling_config(tenant_id, client=client),
        db.list_engineers(tenant_id, active_only=True),
        db.list_branches(tenant_id),
        db.list_job_types(tenant_id))
    today = when.astimezone(DIARY_TZ).date()
    # state['bookings'] aliases the engineer executor's list (same object) so holds it
    # places show up here without copying.
    state: dict = {"bookings": eng_state["bookings"], "usage": {}, "result": None}

    async def execute(name: str, args: dict) -> dict:
        if state["result"] is not None:
            return state["result"]   # one hand-off per email — reuse the desk's answer
        request = str((args or {}).get("request") or "").strip()
        result, usage = await answer(request, executor=eng_exec,
                                     diary_shape=diary_shape, today=today,
                                     tenant_id=tenant_id)
        for k, v in usage.items():
            state["usage"][k] = state["usage"].get(k, 0) + v
        state["result"] = result
        return result

    return execute, state
