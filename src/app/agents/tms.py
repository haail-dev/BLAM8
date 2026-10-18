"""TMS (Logistics) specialist agent — the transport hand-off (docs/modules/module-agent-handoff.md).

The base email agent owns the customer relationship and the reply (one voice). When
a tenant's `tms` module is active, the base agent gains ONE hand-off tool,
`ask_transport`, whose executor invokes THIS agent. The TMS agent owns everything
transport — and nothing else lives here that the base needs:

  * Its own system prompt: the fleet + routing tool-discipline rules, lifted from the
    old single-prompt agent so the BEHAVIOUR (always check before promising, never
    invent a date, ask which branch, one hold only, state assumed pallet types, no
    split orders, "too short notice" ≠ "fully booked", hold before claiming held) is
    preserved verbatim — only the framing changes from "write the reply" to "fill the
    structured result", because the base agent writes the reply.
  * The existing capacity tools + executor: scheduling.FLEET_ROUTING_TOOLS and
    make_executor(fleet=True, routing=True) — the proven atomic-hold machinery,
    UNCHANGED. Holds are placed exactly as before (same RPC, same booking_ref, same
    capacity enforcement) and recorded in the executor's state.
  * The transport-specific extraction (pallet quantities/types, branch, postcodes) —
    the base agent never sees vehicles/capacity internals.

The hand-off contract (D6): in = the customer's transport ask (the base passes the
tenant/contact/thread/mailbox out-of-band via the closure, so a follow-up in the same
thread can't double-hold and the model can't spoof them); out = a structured result
{status, date, spaces, weight_kg, booking_ref, branch, assumed, alternatives, ask,
message}. A specialist error returns {status:"error"} and the base says a colleague
will confirm (D7) — it never invents a booking the desk didn't return.
"""
from app import metering

import json
from datetime import date, datetime

from anthropic import AsyncAnthropic

from app import agent, db, llm, scheduling
from app.config import settings
from app.scheduling import DIARY_TZ

_client: AsyncAnthropic | None = None

# Cap on API calls in the TMS tool loop (check availability → maybe hold → report),
# mirroring the base agent's TOOL_LOOP_MAX_CALLS: the loop normally ends on its own;
# the cap is the backstop and the final permitted call forces a structured result via
# tool_choice 'none' so an answer always comes out.
TMS_TOOL_LOOP_MAX_CALLS = 5


# ── The hand-off tool the BASE agent sees (D3) ───────────────────────────────
# Injected onto the base agent's draft call ONLY when the tms module is active. Its
# executor (make_handoff, below) invokes this specialist. Deliberately minimal: the
# base passes the customer's words; the transport desk does the extraction.
ASK_TRANSPORT_TOOL = {
    "name": "ask_transport",
    "description": (
        "Ask the transport/logistics desk about a delivery, collection, freight or "
        "haulage request — anything about shipping goods, pallet capacity, or booking "
        "a vehicle date. Use this for ANY transport matter BEFORE answering: the desk "
        "checks real capacity and, when the customer asks to book and it fits, places "
        "a provisional hold. Pass the customer's transport request in their own words. "
        "Returns a structured answer (a hold with a reference, dates to offer, a no-fit, "
        "a question to ask the customer, or an error) that you compose into your reply. "
        "Never guess transport availability, capacity or a booking yourself — always ask."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "request": {
                "type": "string",
                "description": "The customer's transport request, in their own words — "
                               "what they want shipped, quantities/pallet types, the date "
                               "they want, and any postcodes or reference they stated.",
            },
        },
        "required": ["request"],
        "additionalProperties": False,
    },
}


# ── The structured result the specialist returns (D6) ────────────────────────
# Strict structured output: every field required (empties carry no-op values) so the
# base agent always gets the same shape to compose from.
TMS_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string",
                   "enum": ["held", "offered", "no_fit", "needs_info", "error"]},
        "date": {"type": "string"},          # held/primary date YYYY-MM-DD, else ""
        "spaces": {"type": "number"},        # load size in pallet spaces, else 0
        "weight_kg": {"type": "number"},     # stated weight, else 0
        "booking_ref": {"type": "string"},   # the hold's reference when held, else ""
        "branch": {"type": "string"},        # branch the booking is for, else ""
        "assumed": {"type": "array", "items": {"type": "string"}},      # assumed-full types
        "alternatives": {"type": "array", "items": {"type": "string"}},  # dates to offer
        "ask": {"type": "string"},           # the single clarifying question, else ""
        "message": {"type": "string"},       # short internal note for the base agent
    },
    "required": ["status", "date", "spaces", "weight_kg", "booking_ref", "branch",
                 "assumed", "alternatives", "ask", "message"],
    "additionalProperties": False,
}

_ERROR_RESULT = {"status": "error", "date": "", "spaces": 0, "weight_kg": 0,
                 "booking_ref": "", "branch": "", "assumed": [], "alternatives": [],
                 "ask": "", "message": "The transport desk could not be reached."}


# ── The specialist's tool-discipline rules ───────────────────────────────────
# Lifted from the old agent._FLEET_DIARY_RULES (+ routing capture): the SAME rules
# that produced today's holds, reframed from "write the reply" to "fill the result"
# (the base agent writes the reply now). Capacity is measured in pallet spaces.
_TRANSPORT_RULES = (
    "Booking and capacity facts come ONLY from the tools, never from memory. Capacity "
    "is measured in pallet spaces:\n"
    "- ALWAYS call check_availability before deciding or reporting any date. Pass the "
    "customer's load — `pallets` (counts by type, e.g. 6 full + 2 half) and `weight_kg` "
    "when they state a weight — so the answer reflects what they actually want to ship.\n"
    "- Never report a date the tool did not return for that load; when the customer's "
    "date doesn't fit, put the nearest dates the tool returned in `alternatives`.\n"
    "- If the customer hasn't said how many pallets (or what type), do NOT guess — "
    "return status 'needs_info' with a question in `ask` for the pallet counts.\n"
    "- When the customer asks to book and the tool shows their load fits, you MUST call "
    "hold_booking BEFORE finishing — saying it is available is not enough, the hold is "
    "what reserves the capacity. One hold only. The hold is REAL, so a held result is "
    "true.\n"
    "- Only return status 'held' if hold_booking returned held:true in THIS "
    "conversation. If you did not place a hold, do not imply one — use status 'offered' "
    "with the fitting dates in `alternatives`.\n"
    "- hold_booking returns a booking_ref (e.g. 'BK-1042'). When you hold, ALWAYS carry "
    "it back in `booking_ref` — it is the customer's reference for any follow-up.\n"
    "- If the tool says the load is too big to ever go as one booking, return status "
    "'no_fit' — NEVER split an order across days, vehicles or deliveries.\n"
    "- If the tool result notes assumed pallet types (assumed_full_pallets), carry them "
    "back in `assumed` so the assumption can be stated to the customer.\n"
    "- If the tool asks which branch (branch_required), return status 'needs_info' with "
    "a question in `ask` naming the branch options the tool listed; pass `branch` to the "
    "tools when the customer named one.\n"
    "- Only treat a date as fully booked when the tool showed its capacity is actually "
    "used. If a date is unavailable for another reason the tool gives (too short notice, "
    "closed that day), report THAT in `message` and put the earliest possible dates the "
    "tool gives in `alternatives` — do not call it 'fully booked'.\n"
    "- NEVER plan to 'check and get back later' — do all checking NOW with the tools.\n"
    "- Talk in dates and pallet capacity only. NEVER name vehicles, vans or fleet size "
    "in any field — how the business organises its capacity is internal.\n"
    "- When the email states collection or delivery postcodes, or the customer's own "
    "order/PO reference, pass them to hold_booking (collection_postcode / "
    "delivery_postcode / reference) — exactly as stated, never guessed or invented.\n"
)

_RESULT_RULES = (
    "Return your answer ONLY in the structured result:\n"
    "- status: 'held' (you placed a hold), 'offered' (dates fit but you placed no hold "
    "— list them in `alternatives`), 'no_fit' (cannot be done as one booking), "
    "'needs_info' (you must ask the customer something — put the ONE question in `ask`), "
    "or 'error'.\n"
    "- date: the held/primary date (YYYY-MM-DD) when status is 'held'; otherwise ''.\n"
    "- spaces / weight_kg: the load size when known, otherwise 0.\n"
    "- booking_ref: the hold's reference when status is 'held', otherwise ''.\n"
    "- branch: the branch the booking is for when known, otherwise ''.\n"
    "- assumed: pallet types counted as full because they weren't recognised, else [].\n"
    "- alternatives: dates (YYYY-MM-DD) to offer when the asked date couldn't be booked, "
    "else [].\n"
    "- ask: the single clarifying question for the customer, else ''.\n"
    "- message: a short internal note for the email assistant if useful, else ''.\n"
    "Never invent capacity, a date, or a booking the tools did not return."
)


def client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


def build_system_prompt(diary_shape: str | None = None, today: date | None = None) -> str:
    """The transport desk's prompt: role + the lifted tool-discipline rules + how to
    fill the structured result. Product-agnostic of the tenant's voice (the base agent
    owns voice); the desk only returns facts. `today` is injected at DAY granularity so
    the prompt (and its cache) changes once a day, never per request."""
    day = today or datetime.now(DIARY_TZ).date()
    sections = [
        "You are the transport/logistics desk for a haulage business. The email "
        "assistant has asked you about a customer's delivery, collection or freight "
        "request. Work out the real answer using your tools and return it as a "
        "STRUCTURED result — you do NOT write to the customer; the email assistant "
        "composes the reply from what you return.",

        agent.date_anchor_line(day),
    ]
    if diary_shape:
        sections.append(f"Diary shape: {diary_shape}")
    sections.append(_TRANSPORT_RULES)
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
        return {**_ERROR_RESULT, **{k: data[k] for k in TMS_SCHEMA["properties"] if k in data}}
    except (json.JSONDecodeError, AttributeError, TypeError):
        return dict(_ERROR_RESULT)


@llm.with_request_scope   # no-op when nested in a draft's scope — inherits the draft's id
async def answer(request: str, *, executor, diary_shape: str | None = None,
                 today: date | None = None, tenant_id: str | None = None) -> tuple[dict, dict]:
    """Run the transport desk on one request. `executor` is the fleet capacity
    executor (scheduling.make_executor fleet path) bound to this email's tenant/
    contact/thread — it is what actually places the hold. Returns (structured result,
    usage totals). Mirrors the base agent's tool loop: call the model; while it asks
    for a tool, execute it and feed the result back; cap the calls and force a
    structured answer on the last permitted call."""
    system = build_system_prompt(diary_shape=diary_shape, today=today)
    msgs: list[dict] = [{"role": "user", "content": request or ""}]
    totals = metering.Totals()
    msg = None
    for i in range(TMS_TOOL_LOOP_MAX_CALLS):
        kwargs = {"tools": scheduling.FLEET_ROUTING_TOOLS}
        if i == TMS_TOOL_LOOP_MAX_CALLS - 1:
            kwargs["tool_choice"] = {"type": "none"}   # budget spent — answer now
        msg = await llm.create(
            client=client(), tenant_id=tenant_id, operation="tms_handoff",
            model=settings.anthropic_booking_model,   # always a booking flow — use the stronger model
            max_tokens=2048,   # raised from 1024 with the base agent (truncated tool calls)
            system=system,
            messages=msgs,
            output_config={"format": {"type": "json_schema", "schema": TMS_SCHEMA}},
            **kwargs,
        )
        metering.add(totals, msg.usage)
        tool_uses = [b for b in msg.content if getattr(b, "type", None) == "tool_use"]
        # Truncation guard (mirrors the base agent, live e2e 2026-07-31): a
        # max_tokens stop is not an answer — a complete tool_use still executes,
        # otherwise the model redoes the turn concisely.
        if (msg.stop_reason == "max_tokens" and not tool_uses
                and i < TMS_TOOL_LOOP_MAX_CALLS - 1):
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
    """Reverse hand-off (Phase 2B, D10): the transport desk frames a CONFIRMED transport
    booking for a proactive day-before reminder — capacity, references and locations only,
    NEVER the vehicle (D5). Returns a plain facts string the base agent composes into the
    tenant's voice (the same specialist→base split as ask_transport, in reverse)."""
    date = str(booking.get("booking_date"))[:10]
    parts = [f"The customer has a confirmed transport booking for tomorrow, {date}."]
    if booking.get("booking_ref"):
        parts.append(f"Booking reference: {booking['booking_ref']}.")
    summary = (booking.get("summary") or "").strip()
    if summary:
        parts.append(f"Job: {summary}.")
    if booking.get("spaces"):
        parts.append(f"Load: {scheduling.fmt_spaces(booking['spaces'])} pallet spaces.")
    if booking.get("collection_postcode"):
        parts.append(f"Collection postcode: {booking['collection_postcode']}.")
    if booking.get("delivery_postcode"):
        parts.append(f"Delivery postcode: {booking['delivery_postcode']}.")
    if (booking.get("reference") or "").strip():
        parts.append(f"The customer's own reference: {booking['reference'].strip()}.")
    return " ".join(parts)


def make_handoff(tenant_id: str, contact: str | None = None, thread_id: str | None = None,
                 mailbox_id: str | None = None, now: datetime | None = None, client=None):
    """Build the `ask_transport` executor for ONE email — the bridge the base agent
    calls. Returns (execute, state):

      * execute(name, args) is ASYNC (it awaits the nested specialist call). It runs the
        transport desk once per email — a second ask_transport call returns the same
        cached answer, so the base can't trigger a second hold (mirrors the existing
        one-hold-per-email rule, enforced in code).
      * state['bookings'] is the SAME list the fleet executor appends its holds to, so
        the pipeline links/resolves them exactly as before (D5). state['usage'] carries
        the desk's token totals so the pipeline can meter them onto the draft.

    The tenant/contact/thread/mailbox are bound HERE (not passed by the model), so a
    follow-up in the same thread can't double-hold and the model can't spoof them (D6)."""
    when = now or datetime.now(DIARY_TZ)
    fleet_exec, fleet_state = scheduling.make_executor(
        tenant_id, contact=contact, thread_id=thread_id, now=when,
        fleet=True, routing=True, mailbox_id=mailbox_id)
    diary_shape = scheduling.fleet_diary_line(
        db.get_scheduling_config(tenant_id, client=client),
        db.list_vehicles(tenant_id, active_only=True),
        db.list_branches(tenant_id))
    today = when.astimezone(DIARY_TZ).date()
    # state['bookings'] aliases the fleet executor's list (same object) so holds it
    # places show up here without copying.
    state: dict = {"bookings": fleet_state["bookings"], "usage": {}, "result": None}

    async def execute(name: str, args: dict) -> dict:
        if state["result"] is not None:
            return state["result"]   # one hand-off per email — reuse the desk's answer
        request = str((args or {}).get("request") or "").strip()
        result, usage = await answer(request, executor=fleet_exec,
                                     diary_shape=diary_shape, today=today,
                                     tenant_id=tenant_id)
        for k, v in usage.items():
            state["usage"][k] = state["usage"].get(k, 0) + v
        state["result"] = result
        return result

    return execute, state
