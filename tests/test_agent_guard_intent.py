"""The reservation guard reads claims, not intent — and a redo that talks about
the reply never becomes the reply (run #6, Foxholes Garage, 2026-09-04).

Live: the first-contact reply asked for two cars' details and its own
action_summary said "asked for vehicle details to book both". The guard's verb
match caught "book", forced a redo, and the redo's text — "My reply did not
offer or mention any specific times… Reproducing original reply unchanged." —
was stored as the customer draft, with "test" in three summary fields.

Run: python -m pytest tests/test_agent_guard_intent.py -q
"""
import json
from pathlib import Path
from types import SimpleNamespace

from app import agent


def _msg(envelope: dict):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(envelope))])


def test_intent_in_the_summary_is_not_a_claim():
    m = _msg({"reply": "Could you send me the registration and mileage for both cars?",
              "action_summary": "Answered early-MOT question, prices, fleet process; "
                                "asked for vehicle details to book both",
              "thread_state": "in_progress"})
    assert agent._unreserved_booking_claim(m) is False


def test_a_real_claim_still_fires():
    m = _msg({"reply": "I've pencilled you in for Tuesday.",
              "action_summary": "Offered three slots and held Tuesday",
              "thread_state": "in_progress"})
    assert agent._unreserved_booking_claim(m) is True
    m2 = _msg({"reply": "All booked.", "action_summary": "", "thread_state": "booked"})
    assert agent._unreserved_booking_claim(m2) is True


def test_a_first_person_promise_to_hold_is_a_claim():
    """Live (same run, next redraft): the turn released the customer's hold,
    checked the diary twice and ended with a stub — nothing held, and the
    guard read only the summary and the clock times."""
    m = _msg({"reply": "Both slots look good for Tuesday 15 September. Let me hold them both now.",
              "action_summary": "Checking availability for both cars on 15 Sep",
              "thread_state": "in_progress"})
    assert agent._unreserved_booking_claim(m) is True
    for body in ("I'll get those booked in now.", "I'm going to reserve that for you.",
                 "Let me lock in the Fiesta's MOT.", "I’ll hold the 8am slot now."):
        assert agent._unreserved_booking_claim(_msg({"reply": body, "action_summary": "",
                                                     "thread_state": "in_progress"})) is True, body


def test_a_conditional_future_is_not_a_claim():
    m = _msg({"reply": "Once you send the Fiesta's reg I can look at getting both booked in "
                       "together — send it over and I'll get a date sorted.",
              "action_summary": "asked for the reg", "thread_state": "in_progress"})
    assert agent._unreserved_booking_claim(m) is False


def test_two_clock_times_still_fire():
    m = _msg({"reply": "We could do 9:00 or 11:30 on Tuesday.", "action_summary": "asked",
              "thread_state": "in_progress"})
    assert agent._unreserved_booking_claim(m) is True


def test_meta_reply_is_recognised():
    assert agent._meta_reply("My reply did not offer or mention any specific times, dates, or "
                             "slots to the customer. No offer_slots or hold_booking call is "
                             "needed at this stage. Reproducing original reply unchanged.")
    assert agent._meta_reply("Outputting the same envelope again unchanged.")
    assert not agent._meta_reply("Hi Kris, thanks for getting in touch — you can book the test "
                                 "any time from 29 August and keep the 28 September date.")
    assert not agent._meta_reply("")


def test_the_say_vs_do_guard_counts_every_tool_that_really_reserves():
    """A booking tool has to join THREE lists, and nothing in the types says so.
    Every one of them bit on 2026-09-22, the day offer_stay shipped:

      1. `pipeline.py`'s combined-executor whitelist — miss it and the tool
         answers "unknown_tool" (guarded in test_appointments_on_stays).
      2. `requirement_groups.with_subjects` — miss it and the intake gate
         demands a field the schema forbids, which is unwinnable, and the draft
         never finishes (guarded in test_stay_executor).
      3. this one: the say-vs-do guard's set of tools that count as having
         RESERVED something. Miss it and the guard calls the model a liar for
         telling the truth — offer_stay reserved BK-1049, `reserved` stayed
         False, and the nudge insisted "NOTHING is reserved", costing a forced
         tool call and a rewritten reply: a third of that draft's output
         written and thrown away.

    A FOURTH list — the waitlist hook in `scheduling.py` — deliberately does
    NOT include offer_stay: an offer is not a booking, so it must not close a
    waiting-list row until the customer accepts.
    """
    src = Path("src/app/agent.py").read_text(encoding="utf-8")
    line = next(l for l in src.splitlines() if "reserved = True" in l or
                ('tu.name in (' in l and "hold_booking" in l))
    for name in ("hold_booking", "offer_slots", "offer_stay"):
        assert f'"{name}"' in line, f"{name} does not count as reserving: {line.strip()}"


def test_the_schema_filled_in_as_a_template_is_not_a_reply():
    """Live 2026-09-22 (K10 rehearsal): a duplicate webhook delivery ran two
    drafts. One did the work; the other answered in one call, with no tools, by
    filling the schema's shape — reply "Hi Tessa...", every prose field "..." —
    and inserted first, so the owner's approval box held a placeholder."""
    stub = agent._parse_draft(json.dumps({
        "reply": "Hi Tessa...", "deferred": False, "missing": "",
        "scheduling_requested": True, "thread_state": "in_progress",
        "request_summary": "...", "action_summary": "...", "searched_for": "...",
        "contact_name": "", "contact_company": "", "blocked": "",
        "category": "needs_reply", "category_reason": "", "booking_details": []}))
    assert stub["reply"] == ""          # → the corrective retry, then draft-failed

    # one ellipsis is a shrug, not a template — a real reply survives it
    real = agent._parse_draft(json.dumps({
        "reply": "Hi Tessa, November is looking good — which dates did you need?",
        "request_summary": "Wants boarding for Ziggy in November",
        "action_summary": "Checked November and asked for dates",
        "searched_for": "...", "category": "needs_reply", "thread_state": "in_progress"}))
    assert real["reply"].startswith("Hi Tessa, November is looking good")

    # and a deliberate knowledge-hold (no reply, a stated gap) is untouched
    held = agent._parse_draft(json.dumps({
        "reply": "", "deferred": True, "missing": "the vaccination expiry date",
        "request_summary": "...", "action_summary": "...", "searched_for": "...",
        "category": "needs_reply", "thread_state": "in_progress"}))
    assert held["reply"] == "" and held["deferred"] and held["missing"]


def test_placeholders_are_scrubbed_from_the_envelope():
    p = agent._parse_draft(json.dumps({
        "reply": "Hi Kris, thanks.", "request_summary": "test", "action_summary": "TBD",
        "category_reason": "n/a", "searched_for": "-", "category": "needs_reply",
        "thread_state": "in_progress"}))
    assert p["reply"] == "Hi Kris, thanks."
    assert p["request_summary"] == "" and p["action_summary"] == ""
    assert p["category_reason"] == "" and p["searched_for"] == ""
    ok = agent._parse_draft(json.dumps({"reply": "x", "action_summary": "Asked for the reg",
                                        "category_reason": "a booking question"}))
    assert ok["action_summary"] == "Asked for the reg" and ok["category_reason"] == "a booking question"
