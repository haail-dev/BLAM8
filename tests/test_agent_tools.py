"""Agent tool loop + scheduling prompt section (build step 3).

draft_reply(tools=, tool_executor=) runs a manual tool-use loop: call the model;
while stop_reason == 'tool_use', execute each tool_use block via the executor,
append the assistant content + tool_result blocks, and call again — capped at
TOOL_LOOP_MAX_CALLS API calls, with tool_choice 'none' on the final permitted
call so the loop always ends in a text reply. The final output keeps the
existing structured-output schema {reply, deferred, missing}; usage is SUMMED
across every call in the loop into one dict (usage.record_anthropic accepts it).

The scheduling prompt section appears ONLY when tools are active: it injects
today's date (daily granularity — cache-friendly), the diary rules, and
mode-aware confirmation wording (draft-only: approving confirms the hold as it
sends, so the reply IS the confirmation; full-auto: a successful send confirms).
Either way the customer only ever reads a confirmed booking, so hedges like
"we'll confirm shortly" are banned. Platform rule 1's "you cannot take actions" gains a
carve-out for the real diary hold.

Run: python -m pytest tests/ -q
"""
import asyncio
import json
from datetime import date
from types import SimpleNamespace

from app import agent
from app.agent import TOOL_LOOP_MAX_CALLS, build_system_prompt

TODAY = date(2026, 6, 16)  # Tuesday

TOOLS = [{"name": "check_availability", "description": "x", "input_schema": {"type": "object"}}]


def usage(n):
    return SimpleNamespace(input_tokens=n, output_tokens=n * 2,
                           cache_read_input_tokens=n * 3, cache_creation_input_tokens=0)


def text_response(payload, n=10):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(payload))],
        stop_reason="end_turn", usage=usage(n), model="fake-model",
    )


def tool_response(name, args, tool_id="tu-1", n=10):
    return SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", id=tool_id, name=name, input=args)],
        stop_reason="tool_use", usage=usage(n), model="fake-model",
    )


class FakeClient:
    """Stands in for AsyncAnthropic: returns scripted responses, records calls."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses[min(len(self.calls), len(self.responses)) - 1]


def run_draft(monkeypatch, responses, executor=None, cfg=None, tools=TOOLS, **kw):
    fake = FakeClient(responses)
    monkeypatch.setattr(agent, "_client", fake)
    result = asyncio.run(agent.draft_reply(
        cfg or {}, {"subject": "Quote", "body": "Can you collect Friday?"},
        tools=tools, tool_executor=executor, **kw,
    ))
    return fake, result


# ── the loop ──────────────────────────────────────────────────────────────────
def test_tool_loop_executes_and_sums_usage(monkeypatch):
    executed = []

    def executor(name, args):
        executed.append((name, args))
        return {"available": [{"date": "2026-06-19", "window": "day", "remaining": 2}]}

    fake, result = run_draft(monkeypatch, [
        tool_response("check_availability", {"date_from": "2026-06-19", "date_to": "2026-06-19"}, n=10),
        text_response({"reply": "Friday works — held.", "deferred": False, "missing": ""}, n=5),
    ], executor)

    assert executed == [("check_availability", {"date_from": "2026-06-19", "date_to": "2026-06-19"})]
    assert result.text == "Friday works — held."
    assert result.usage == {"input_tokens": 15, "output_tokens": 30,
                            "cache_read_input_tokens": 45, "cache_creation_input_tokens": 0}

    first, second = fake.calls
    # Two-phase drafting (2026-09-22): the caller's tools, plus the sentinel
    # that is the only door into the writing turn — and phase 1 FORCES a tool
    # call, so the model cannot emit prose before it has acted.
    assert first["tools"][:-1] == TOOLS
    assert first["tools"][-1]["name"] == "ready_to_write"
    assert first["tool_choice"] == {"type": "any"}
    assert first["output_config"]["format"]["type"] == "json_schema"
    # The second call replays the assistant tool_use turn + our tool_result.
    assert second["messages"][-2]["role"] == "assistant"
    tool_result = second["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "tu-1"
    assert json.loads(tool_result["content"])["available"][0]["date"] == "2026-06-19"


def test_tool_loop_terminates_at_cap_with_final_forced_text(monkeypatch):
    # A model that never stops asking for tools: the loop must end anyway, and the
    # last permitted LOOP call must force a text reply via tool_choice 'none'.
    # This pathological model never writes one, so the empty-reply corrective
    # retry fires exactly once more (tool-less) and the draft comes back EMPTY —
    # the pipeline's draft-failed path — never raw JSON or a phantom reply.
    fake, result = run_draft(
        monkeypatch,
        [tool_response("check_availability", {"date_from": "x", "date_to": "y"})],
        lambda name, args: {"available": []},
    )
    assert len(fake.calls) == TOOL_LOOP_MAX_CALLS + 1        # loop cap + 1 retry
    # The cap still wins over phase 1: the last loop call forces TEXT, so a
    # model that would call tools for ever still has to produce a reply.
    assert fake.calls[TOOL_LOOP_MAX_CALLS - 1]["tool_choice"] == {"type": "none"}
    # ...and every call before it is phase 1, where writing is not allowed.
    assert all(c["tool_choice"] == {"type": "any"}
               for c in fake.calls[:TOOL_LOOP_MAX_CALLS - 1])
    assert "tools" not in fake.calls[-1]                     # the retry is tool-less
    assert result.text == ""                                 # draft-failed, not junk


def test_executor_exception_becomes_honest_error_result(monkeypatch):
    def executor(name, args):
        raise RuntimeError("db down")

    fake, result = run_draft(monkeypatch, [
        tool_response("check_availability", {"date_from": "a", "date_to": "b"}),
        text_response({"reply": "A colleague will confirm.", "deferred": True, "missing": "diary"}),
    ], executor)
    payload = json.loads(fake.calls[1]["messages"][-1]["content"][0]["content"])
    assert payload["error"] == "tool_failed"
    assert "colleague" in payload["message"]
    assert result.text == "A colleague will confirm."


def truncated_response(text="Great, let me reserve those three morning sl", n=10):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="max_tokens", usage=usage(n), model="fake-model",
    )


def test_truncated_turn_without_tool_use_is_redone_not_accepted(monkeypatch):
    # Live e2e 2026-07-31: an offer_slots call blew the old 1024 output cap, the
    # stump was silently accepted, and three times reached the customer with
    # NOTHING reserved. A max_tokens stop with no complete tool call must redo
    # the turn, not pass the stump downstream.
    executed = []

    def executor(name, args):
        executed.append(name)
        return {"offered": [{"date": "2026-06-18", "window": "10:00"}],
                "hold_until_phrase": "Wednesday 17 June, 10:00"}

    fake, result = run_draft(monkeypatch, [
        truncated_response(),
        tool_response("offer_slots", {"slots": [{"date": "2026-06-18", "window": "10:00"}],
                                      "summary": "Demo"}, tool_id="tu-9"),
        text_response({"reply": "Held for you until Wednesday.", "deferred": False, "missing": ""}),
    ], executor)
    assert executed == ["offer_slots"]            # the redone turn's tool actually ran
    assert result.text == "Held for you until Wednesday."
    # The corrective nudge went back to the model as a user turn (the messages
    # list is shared/mutated across calls, so inspect the final state).
    assert any(isinstance(m.get("content"), str) and "cut off" in m["content"]
               for m in fake.calls[-1]["messages"])
    # 4096 since 2026-08-31: the reservation-nudge redo turn — schema envelope
    # plus a full rewritten reply — blew the 2048 cap mid-JSON and a bookable
    # email queued with an empty draft.
    assert fake.calls[0]["max_tokens"] == 4096    # the raised cap rides every loop call


def test_complete_tool_call_under_max_tokens_stop_still_executes(monkeypatch):
    # A tool_use block that FIT under the cap executes even when the turn as a
    # whole stopped on max_tokens — discarding a complete call wastes a reservation.
    executed = []
    resp = SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", id="tu-2", name="hold_booking",
                                 input={"date": "2026-06-18", "window": "day", "summary": "x"})],
        stop_reason="max_tokens", usage=usage(10), model="fake-model")
    fake, result = run_draft(monkeypatch, [
        resp,
        text_response({"reply": "Booked.", "deferred": False, "missing": ""}),
    ], lambda name, args: executed.append(name) or {"held": True})
    assert executed == ["hold_booking"]
    assert result.text == "Booked."


# ── the reservation guard (live e2e 2026-07-31, finding #2) ─────────────────
def test_reply_offering_times_without_reserving_is_sent_back(monkeypatch):
    # The model twice listed "09:00 / 10:00 / 11:00" as options while never
    # calling offer_slots — the prompt mandate alone did not hold. The guard
    # sends it back once, tools live; here it complies on the second pass.
    executed = []

    def executor(name, args):
        executed.append(name)
        return {"offered": [{"date": "2026-06-18", "window": "09:00"}],
                "hold_until_phrase": "Wednesday 17 June, 18:00"}

    fake, result = run_draft(monkeypatch, [
        text_response({"reply": "We have 09:00, 10:00 or 11:00 — which suits?",
                       "deferred": False, "missing": ""}),
        tool_response("offer_slots", {"slots": [{"date": "2026-06-18", "window": "09:00"}],
                                      "summary": "Demo"}, tool_id="tu-7"),
        text_response({"reply": "Held for you until Wednesday 17 June, 18:00.",
                       "deferred": False, "missing": ""}),
    ], executor, scheduling=True)
    assert executed == ["offer_slots"]
    assert "Held for you" in result.text
    assert any(isinstance(m.get("content"), str) and "STOP" in m["content"]
               for m in fake.calls[-1]["messages"])
    # The redo turn is FORCED to call a tool — a repeat text-only promise
    # cannot end the turn again.
    assert fake.calls[1]["tool_choice"] == {"type": "any"}


def test_claimed_booking_action_without_tool_call_is_sent_back(monkeypatch):
    # Live e2e finding #3: reply had NO time tokens, just "I'll get those
    # reserved for you right now" — and the model's own action_summary declared
    # "Offering three Thursday morning slots". Say-vs-do: the claim triggers
    # the guard even with no times in the prose.
    executed = []

    def executor(name, args):
        executed.append(name)
        return {"offered": [{"date": "2026-08-06", "window": "09:00"}],
                "hold_until_phrase": "Wednesday 5 August, 18:00"}

    fake, result = run_draft(monkeypatch, [
        text_response({"reply": "No need to apologise — I'll get those reserved right now.",
                       "deferred": False, "missing": "",
                       "action_summary": "Offering three Thursday morning slots"}),
        tool_response("offer_slots", {"slots": [{"date": "2026-08-06", "window": "09:00"}],
                                      "summary": "Demo"}, tool_id="tu-8"),
        text_response({"reply": "Held until Wednesday 5 August, 18:00 — which suits?",
                       "deferred": False, "missing": ""}),
    ], executor, scheduling=True)
    assert executed == ["offer_slots"]
    assert "Held until" in result.text


def test_booked_thread_state_without_tool_call_is_sent_back(monkeypatch):
    # Live e2e finding #5: on ACCEPTANCE the model answered "pencilled you in,
    # ref BK-1029" with thread_state 'booked' straight from the offered_slots
    # context — one time token, so the times check passed — and never called
    # hold_booking. The offered rows would all have lapsed at the TTL while
    # the customer believed they were booked. thread_state 'booked' with no
    # successful reservation is now itself the trigger.
    executed = []

    def executor(name, args):
        executed.append(name)
        return {"held": True, "booking_ref": "BK-1029", "accepted_offer": True}

    fake, result = run_draft(monkeypatch, [
        text_response({"reply": "You're pencilled in for 10:00, ref BK-1029.",
                       "deferred": False, "missing": "",
                       "thread_state": "booked",
                       "action_summary": "Customer picked their slot"}),
        tool_response("hold_booking", {"date": "2026-08-06", "window": "10:00",
                                       "summary": "Demo"}, tool_id="tu-11"),
        text_response({"reply": "Booked: 10:00 Thursday, ref BK-1029.",
                       "deferred": False, "missing": ""}),
    ], executor, scheduling=True)
    assert executed == ["hold_booking"]
    assert "Booked" in result.text


def test_clarify_reply_summary_never_triggers_the_guard(monkeypatch):
    # A clarify-first reply legitimately says "I'll get a slot reserved once I
    # have those" in the PROSE — but its action_summary ("Asked for details")
    # carries no booking verb, so the guard stays quiet.
    fake, result = run_draft(monkeypatch, [
        text_response({"reply": "Which day suits? Once I have that I'll get a slot "
                                "reserved for you straight away.",
                       "deferred": False, "missing": "",
                       "action_summary": "Asked for preferred day and time of day"}),
    ], lambda name, args: {"available": []}, scheduling=True)
    assert len(fake.calls) == 1


def test_reserved_hold_means_no_guard_nudge(monkeypatch):
    # A successful hold/offer this draft = times in the reply are legitimate.
    fake, result = run_draft(monkeypatch, [
        tool_response("hold_booking", {"date": "2026-06-18", "window": "10:00",
                                       "summary": "x"}),
        text_response({"reply": "You're booked for 10:00; we're open 09:00-17:00.",
                       "deferred": False, "missing": ""}),
    ], lambda name, args: {"held": True, "booking_ref": "BK-1"}, scheduling=True)
    assert len(fake.calls) == 2                    # no third corrective call
    assert "booked" in result.text


def test_guard_refires_when_the_redo_only_checks_availability(monkeypatch):
    # Live e2e 2026-09-02 (Willowbrook): the forced redo turn satisfied
    # tool_choice 'any' with ANOTHER read-only check_availability, then the
    # next envelope claimed "I've placed provisional holds" — and the spent
    # one-shot latch waved it through with NOTHING in the diary. The guard now
    # fires a second time.
    executed = []

    def executor(name, args):
        executed.append(name)
        if name == "check_availability":
            return {"available": True, "nights": 1}
        return {"held": True, "booking_ref": "BK-7"}

    fake, result = run_draft(monkeypatch, [
        text_response({"reply": "We have 7.30am drop-off and 5.30pm collection free.",
                       "deferred": False, "missing": "",
                       "action_summary": "Confirmed availability for both dates"}),
        tool_response("check_availability", {"arrival": "a", "departure": "b"},
                      tool_id="tu-20"),
        text_response({"reply": "I've placed provisional holds for 7.30am and 5.30pm.",
                       "deferred": False, "missing": "",
                       "action_summary": "Placed provisional holds"}),
        tool_response("hold_booking", {"arrival": "a", "departure": "b",
                                       "summary": "Stay"}, tool_id="tu-21"),
        text_response({"reply": "Held for you — ref BK-7.",
                       "deferred": False, "missing": ""}),
    ], executor, scheduling=True)
    assert executed == ["check_availability", "hold_booking"]
    assert "BK-7" in result.text
    stops = [m for m in fake.calls[-1]["messages"]
             if isinstance(m.get("content"), str) and m["content"].startswith("STOP")]
    assert len(stops) == 2                       # the guard fired twice
    assert "reserves NOTHING" in stops[0]["content"]


def test_inline_colleague_deferral_is_sent_back_for_the_real_hold(monkeypatch):
    # Live e2e 2026-09-02: "a colleague will confirm the exact rate for two
    # dogs" written INTO the reply with deferred false — a knowledge gap
    # smuggled past the rule-5 hold. The corrective pass takes the hold.
    fake, result = run_draft(monkeypatch, [
        text_response({"reply": "The boarding night is £38 — a colleague will "
                                "confirm the exact rate for two dogs sharing.",
                       "deferred": False, "missing": "",
                       "action_summary": "Answered pricing"}),
        text_response({"reply": "", "deferred": True,
                       "missing": "rate for two dogs sharing a kennel"}),
    ], lambda name, args: {"available": []}, scheduling=True)
    assert len(fake.calls) == 2
    assert result.text == "" and result.deferred is True
    assert result.missing == "rate for two dogs sharing a kennel"
    assert "knowledge gap" in str(fake.calls[-1]["messages"][-1]["content"])


def test_instructed_colleague_mention_reemits_unchanged(monkeypatch):
    # A colleague line the instructions DIRECT (move/cancel an existing
    # booking) survives the nudge: the model re-emits the same envelope and
    # the one-shot latch accepts it.
    reply = "A colleague will sort out moving your booking to Friday."
    fake, result = run_draft(monkeypatch, [
        text_response({"reply": reply, "deferred": False, "missing": "",
                       "action_summary": "Passed the reschedule to the team"}),
    ], lambda name, args: {"available": []}, scheduling=True)
    assert len(fake.calls) == 2                  # one nudge, then accepted
    assert result.text == reply and result.deferred is False


def test_single_time_mention_never_triggers_the_guard(monkeypatch):
    fake, result = run_draft(monkeypatch, [
        text_response({"reply": "Your booking at 10:00 tomorrow is confirmed.",
                       "deferred": False, "missing": ""}),
    ], lambda name, args: {"available": []}, scheduling=True)
    assert len(fake.calls) == 1
    assert result.text == "Your booking at 10:00 tomorrow is confirmed."


def test_no_tools_is_the_existing_single_call(monkeypatch):
    fake, result = run_draft(
        monkeypatch,
        [text_response({"reply": "Hi", "deferred": False, "missing": ""})],
        tools=None,
    )
    assert len(fake.calls) == 1
    assert "tools" not in fake.calls[0] and "tool_choice" not in fake.calls[0]
    assert result.text == "Hi"
    assert result.usage["input_tokens"] == 10  # summed dict shape even for one call


def test_draft_reply_appends_after_the_gates_and_never_signs_a_hold(monkeypatch):
    """The configured sign-off is appended by code once every gate has read the
    model's own words; an empty reply (a hold) stays empty so it can never become
    a bare sign-off that auto-send would treat as a reply."""
    cfg = {"sign_off": "Cheers,\r\nSam"}
    ok = {"reply": "We can do Tuesday.", "deferred": False, "missing": ""}
    _, result = run_draft(monkeypatch, [text_response(ok)], tools=None, cfg=cfg)
    assert result.text == "We can do Tuesday.\n\nCheers,\nSam"
    assert result.sign_off == "Cheers,\nSam"
    # The model wrote a closing anyway: replaced, not doubled.
    closed = {**ok, "reply": "We can do Tuesday.\n\nThanks,\nSam"}
    _, result = run_draft(monkeypatch, [text_response(closed)], tools=None, cfg=cfg)
    assert result.text == "We can do Tuesday.\n\nCheers,\nSam"
    # A knowledge hold stays empty and unsigned.
    hold = {"reply": "", "deferred": True, "missing": "the price"}
    _, result = run_draft(monkeypatch, [text_response(hold)], tools=None, cfg=cfg)
    assert (result.text, result.sign_off) == ("", "")
    assert result.deferred and result.missing == "the price"
    # SMS keeps its register: the text as the model wrote it.
    _, result = run_draft(monkeypatch, [text_response(ok)], tools=None, cfg=cfg, channel="sms")
    assert (result.text, result.sign_off) == ("We can do Tuesday.", "")
    # Nothing configured: unchanged, nothing recorded.
    _, result = run_draft(monkeypatch, [text_response(ok)], tools=None, cfg={})
    assert (result.text, result.sign_off) == ("We can do Tuesday.", "")


def test_draft_reply_keeps_the_deadline_line_before_the_sign_off(monkeypatch):
    """The tool's own hold-deadline sentence is enforced into the reply first; the
    sign-off is appended after it, and replaces a closing the model wrote."""
    line = "Your space is held until Thursday 10:21am."
    held = {"reserved": True, "hold_until_phrase": "Thursday 24 September, 10:21",
            "deadline_line": line}
    said = {"reply": "Lovely, we have your enquiry.\n\nThanks,\nSam", "deferred": False, "missing": ""}
    _, result = run_draft(monkeypatch, [
        tool_response("hold_booking", {"arrival": "2026-11-06T08:00", "departure": "2026-11-08T17:00"}),
        text_response(said), text_response(said),
    ], lambda name, args: dict(held), cfg={"sign_off": "Cheers,\nSam"}, tools=HOLD_TOOLS)
    assert result.text.startswith("Lovely, we have your enquiry.")
    assert result.text.endswith("\n\nCheers,\nSam")
    assert line in result.text and result.text.index(line) < result.text.index("Cheers,")
    assert "Thanks,\nSam" not in result.text and result.text.count("Sam") == 1


def test_draft_reply_gates_do_not_see_the_sign_off(monkeypatch):
    """The booking-claim gate that turns a reply into a hold reads the model's reply,
    never the appended sign-off; a held reply is not signed."""
    cfg = {"sign_off": "Booked with care,\nSam"}
    policy = {"ready": False, "message": "Booking details are not set up yet."}
    plain = {"reply": "Thanks for getting in touch. We will look at this.",
             "deferred": False, "missing": ""}
    _, result = run_draft(monkeypatch, [text_response(plain)], tools=None, cfg=cfg,
                          booking_policy=policy)
    assert result.text == "Thanks for getting in touch. We will look at this.\n\nBooked with care,\nSam"
    assert not result.blocked
    claim = {"reply": "Your booking is confirmed for Tuesday at 10:00.",
             "deferred": False, "missing": ""}
    _, result = run_draft(monkeypatch, [text_response(claim)], tools=None, cfg=cfg,
                          booking_policy=policy)
    assert result.blocked == policy["message"]
    assert (result.text, result.sign_off) == ("", "")


def test_never_asks_the_customer_for_their_email_address():
    # 2026-09-04 (plumbing run): the CP12 draft asked for an email address. The
    # intake gate that answers an email FIELD from the sender never engaged —
    # this tenant had no email field at all — so the guard has to be a platform
    # rule in the prompt, present whatever the tenant configured.
    p = build_system_prompt({})
    assert "NEVER ask the customer for their email address" in p
    assert "you are replying to it" in p
    # Unconditional — not gated on scheduling, auto-send, or any intake config.
    for cfg, kw in (({"auto_send": True}, {}), ({}, {"scheduling": True, "today": TODAY})):
        assert "NEVER ask the customer for their email address" in build_system_prompt(cfg, **kw)


# ── the scheduling prompt section ─────────────────────────────────────────────
def test_scheduling_section_only_when_active():
    plain = build_system_prompt({})
    assert "<scheduling>" not in plain
    sched = build_system_prompt({}, scheduling=True, today=TODAY)
    assert "<scheduling>" in sched
    assert "Tuesday 16 June 2026" in sched and "2026-06-16" in sched
    assert "check_availability" in sched and "hold_booking" in sched
    assert "The hold is REAL" in sched


def test_scheduling_wording_is_mode_aware():
    draft_mode = build_system_prompt({}, scheduling=True, today=TODAY)
    assert "approving it confirms" in draft_mode
    # Approval confirms the hold in the same step that sends, so the customer
    # only ever reads a confirmed booking: the draft must speak as the
    # confirmation itself, never promise one to follow.
    assert "this email IS the confirmation" in draft_mode
    assert "never as already confirmed" not in draft_mode
    auto_mode = build_system_prompt({"auto_send": True}, scheduling=True, today=TODAY)
    assert "successful send confirms" in auto_mode


def test_rule_one_gains_hold_carveout_only_with_scheduling():
    # Without scheduling the platform rule stays byte-identical to the shipped text.
    assert "you cannot take actions (book, refund, schedule, update records)" \
        in build_system_prompt({"auto_send": True})
    auto_sched = build_system_prompt({"auto_send": True}, scheduling=True, today=TODAY)
    assert "(book, refund, schedule, update records)" not in auto_sched
    assert "diary hold" in auto_sched
    draft_sched = build_system_prompt({}, scheduling=True, today=TODAY)
    assert "a human reviews and approves every reply" in draft_sched
    assert "diary hold" in draft_sched


def test_a_call_repeated_verbatim_after_a_refusal_is_not_run_again(monkeypatch):
    """Run #8: offer_slots was refused missing_info and the model made the very
    same call again — a wasted turn. The second, identical call is answered
    from the first refusal (not executed) with a pointer at what to change; a
    changed call runs."""
    executed = []

    def executor(name, args):
        executed.append((name, dict(args)))
        if name == "offer_slots" and "Pick-up address" not in (args.get("details") or {}):
            return {"error": "missing_info", "missing": ["Pick-up address"],
                    "message": "Ask for: Pick-up address."}
        return {"offered": True}

    same = {"service": "Assessment", "slots": ["2026-09-12T08:00"]}
    fake, result = run_draft(monkeypatch, [
        tool_response("offer_slots", dict(same), tool_id="tu-1"),
        tool_response("offer_slots", dict(same), tool_id="tu-2"),
        tool_response("offer_slots", {**same, "details": {"Pick-up address": "14 Homend"}}, tool_id="tu-3"),
        text_response({"reply": "Offered.", "deferred": False, "missing": ""}),
    ], executor, tools=[{"name": "offer_slots", "description": "x", "input_schema": {"type": "object"}}])
    assert [e[0] for e in executed] == ["offer_slots", "offer_slots"]          # the verbatim repeat never ran
    second = json.loads(fake.calls[2]["messages"][-1]["content"][0]["content"])
    assert second["repeated"] is True and second["error"] == "missing_info"
    assert second["message"].startswith("Identical to a call already refused this turn (missing_info)")
    assert result.text == "Offered."


def test_the_diary_rules_gate_on_required_only_and_name_a_series_length():
    p = agent.build_system_prompt({}, scheduling=True)
    assert "Only REQUIRED booking details gate a hold" in p
    assert "confirmed with the SERVICE and its LENGTH named" in p



def test_the_sentinel_is_the_only_door_into_the_writing_turn(monkeypatch):
    """Two-phase drafting, Kris 2026-09-22: "why does the agent constantly start
    writing a reply before it's done any of the tasks. That really isn't
    efficient."

    It was measurable — 16-43% of a draft's output written and thrown away,
    because only the LAST turn's text becomes the reply and the model kept
    producing prose between tool calls. Phase 1 now forces a tool call, and the
    only way out is to say the diary work is finished.
    """
    executed = []
    fake, result = run_draft(monkeypatch, [
        tool_response("check_availability", {"date_from": "2026-06-19", "date_to": "2026-06-19"}),
        tool_response("ready_to_write", {}),
        text_response({"reply": "Friday works.", "deferred": False, "missing": ""}),
    ], lambda name, args: (executed.append(name), {"available": []})[1])

    # the sentinel never reaches the executor — it touches nothing and books
    # nothing, so it needs no place in the whitelists a real tool must join
    assert executed == ["check_availability"]
    assert result.text == "Friday works."

    # phase 1 forces a call; once the sentinel lands, the writing turn is free
    assert fake.calls[0]["tool_choice"] == {"type": "any"}
    assert fake.calls[1]["tool_choice"] == {"type": "any"}
    assert "tool_choice" not in fake.calls[2], "after ready_to_write it may write"


def test_an_email_needing_no_diary_work_costs_one_small_call(monkeypatch):
    """The cost of the sentinel, stated honestly: an email with nothing to book
    spends one tiny forced call instead of a discarded envelope. That is the
    trade, and it is a good one."""
    fake, result = run_draft(monkeypatch, [
        tool_response("ready_to_write", {}),
        text_response({"reply": "We're open 8 till 6.", "deferred": False, "missing": ""}),
    ], lambda name, args: {"available": []})

    assert result.text == "We're open 8 till 6."
    assert len(fake.calls) == 2, "one sentinel call, then the reply"


# ── a missing detail is never a block (K11 rehearsal, 2026-09-23) ─────────────
HOLD_TOOLS = TOOLS + [{"name": "hold_booking", "description": "x", "input_schema": {"type": "object"}}]
TIMES_MISSING = {"error": "missing_info", "missing": ["Drop-off time", "Collection time"],
                 "message": "Do not book yet — ask for: Drop-off time, Collection time.",
                 "reserved": True, "hold_until_phrase": "Thursday 24 September, 10:21"}
BLOCKED = {"reply": "", "deferred": False, "missing": "",
           "blocked": "The diary requires specific drop-off and collection times."}
ASKED = {"reply": "Lovely — Bruno's space is held for you until Thursday 10:21am. "
                  "What time would you like to drop him off and collect him?",
         "deferred": False, "missing": "", "blocked": ""}


def test_blocked_on_a_missing_detail_is_rewritten_as_the_ask(monkeypatch):
    """The model set `blocked` after a missing_info refusal and wrote nothing,
    so the owner got 'Waiting on the customer' and no reply to send."""
    fake, result = run_draft(monkeypatch, [
        tool_response("hold_booking", {"arrival": "2026-11-06T08:00", "departure": "2026-11-08T17:00"}),
        text_response(BLOCKED),
        text_response(ASKED),
    ], lambda name, args: dict(TIMES_MISSING), tools=HOLD_TOOLS)
    assert result.blocked == ""
    assert result.text.startswith("Lovely — Bruno's space is held")
    last = fake.calls[-1]["messages"][-1]["content"]
    nudge = last if isinstance(last, str) else " ".join(p.get("text", "") for p in last)
    assert "never a block" in nudge and "reserved" in nudge


def test_a_real_system_block_still_holds(monkeypatch):
    """A configuration refusal (not a customer detail) keeps rule 5b: no reply,
    the owner is shown the constraint."""
    fake, result = run_draft(monkeypatch, [
        tool_response("hold_booking", {"arrival": "2026-11-06T08:00", "departure": "2026-11-06T09:00"}),
        text_response({**BLOCKED, "blocked": "check-out is limited to 09:30"}),
    ], lambda name, args: {"error": "stay_rule", "message": "Check-out before 09:30 only."},
        tools=HOLD_TOOLS)
    assert result.blocked == "check-out is limited to 09:30" and result.text == ""
    assert len(fake.calls) == 2                                   # no corrective turn


# ── a reserved space is said out loud (K11, 2026-09-23) ───────────────────────
ASKED_NO_HOLD = {"reply": "Lovely — what time would you like to drop Bruno off and collect him?",
                 "deferred": False, "missing": "", "blocked": ""}


def test_a_reservation_the_reply_forgets_is_added(monkeypatch):
    fake, result = run_draft(monkeypatch, [
        tool_response("hold_booking", {"arrival": "2026-11-06T08:00", "departure": "2026-11-08T17:00"}),
        text_response(ASKED_NO_HOLD),
        text_response(ASKED),                                     # the one that says it's held
    ], lambda name, args: dict(TIMES_MISSING), tools=HOLD_TOOLS)
    assert "held for you until" in result.text
    last = fake.calls[-1]["messages"][-1]["content"]
    nudge = last if isinstance(last, str) else " ".join(p.get("text", "") for p in last)
    assert "Thursday 24 September, 10:21" in nudge and "do not say it is" in nudge


def test_a_reply_that_already_says_it_is_held_costs_nothing_extra(monkeypatch):
    fake, result = run_draft(monkeypatch, [
        tool_response("hold_booking", {"arrival": "2026-11-06T08:00", "departure": "2026-11-08T17:00"}),
        text_response(ASKED),
    ], lambda name, args: dict(TIMES_MISSING), tools=HOLD_TOOLS)
    assert len(fake.calls) == 2 and result.text == ASKED["reply"]


def test_a_worse_retry_never_replaces_a_good_first_reply(monkeypatch):
    fake, result = run_draft(monkeypatch, [
        tool_response("hold_booking", {"arrival": "2026-11-06T08:00", "departure": "2026-11-08T17:00"}),
        text_response(ASKED_NO_HOLD),
        text_response({"reply": "", "deferred": False, "missing": "", "blocked": ""}),
    ], lambda name, args: dict(TIMES_MISSING), tools=HOLD_TOOLS)
    assert result.text == ASKED_NO_HOLD["reply"]


def test_no_reservation_means_no_hold_line_asked_for(monkeypatch):
    fake, result = run_draft(monkeypatch, [
        tool_response("hold_booking", {"arrival": "2026-11-06T08:00", "departure": "2026-11-08T17:00"}),
        text_response(ASKED_NO_HOLD),
    ], lambda name, args: {**TIMES_MISSING, "reserved": False}, tools=HOLD_TOOLS)
    assert len(fake.calls) == 2


def test_the_bare_verb_is_not_saying_it_is_held():
    """K11 take 1: the reply said the opposite and the old check passed it."""
    assert not agent._says_held("Before I can hold the dates, I just need your drop-off and collection times.")
    assert not agent._says_held("Let me know and I'll hold it for you.")
    assert agent._says_held("I've popped the space on hold for you until Thursday 10:36am.")
    assert agent._says_held("Bruno's space is held for you until tomorrow morning.")
    assert agent._says_held("I've reserved 6–8 November for Bruno while you let me know the times.")


def test_a_reply_that_denies_the_hold_is_corrected(monkeypatch):
    denies = {"reply": "Before I can hold the dates, I just need your drop-off and collection times.",
              "deferred": False, "missing": "", "blocked": ""}
    fake, result = run_draft(monkeypatch, [
        tool_response("hold_booking", {"arrival": "2026-11-06T08:00", "departure": "2026-11-08T17:00"}),
        text_response(denies),
        text_response(ASKED),
    ], lambda name, args: dict(TIMES_MISSING), tools=HOLD_TOOLS)
    assert "held for you until" in result.text
