"""No-booking guardrail + the scheduling-gap upgrade signal (src/app/agent.py).

When no diary module is active, the agent must not offer/imply a booking, and it
flags the request so the human sees the demand (surfaced as "add Scheduling").

Run: python -m pytest tests/test_scheduling_gap.py -q
"""
from app.agent import build_system_prompt, _parse_draft, DRAFT_SCHEMA


def test_no_booking_guardrail_only_when_no_diary():
    plain = build_system_prompt({})
    assert "<booking>" in plain
    assert "cannot make, hold, check, or confirm any booking" in plain
    assert "scheduling_requested" in plain               # told to flag the request
    # with a diary active, the no-booking guardrail is absent (it CAN book)
    assert "<booking>" not in build_system_prompt({}, scheduling=True)


def test_schema_requires_scheduling_requested():
    assert "scheduling_requested" in DRAFT_SCHEMA["properties"]
    assert "scheduling_requested" in DRAFT_SCHEMA["required"]


def test_parse_draft_returns_scheduling_requested():
    out = _parse_draft(
        '{"reply":"A colleague will be in touch.","deferred":true,'
        '"missing":"","scheduling_requested":true,"thread_state":"needs_attention",'
        '"request_summary":"Wants a quote","action_summary":"Said a colleague will follow up"}')
    assert out["reply"].startswith("A colleague") and out["deferred"] is True
    assert out["scheduling_requested"] is True
    assert out["thread_state"] == "needs_attention"
    assert out["request_summary"] == "Wants a quote"
    # false + non-JSON both yield scheduling_requested False; an unknown/absent
    # thread_state falls back to in_progress. Non-JSON is an EMPTY reply since
    # 2026-09-02 (the raw-text fallback shipped leaked deliberation as a draft)
    # — the caller's corrective retry re-asks for the envelope.
    assert _parse_draft('{"reply":"hi","deferred":false,"missing":"","scheduling_requested":false}')["scheduling_requested"] is False
    fallback = _parse_draft("not json")
    assert fallback["reply"] == "" and fallback["thread_state"] == "in_progress"


def test_prompt_teaches_second_person_and_defer_over_colleague():
    # Live e2e 2026-09-02 (Willowbrook): the draft opened "Hi Kris" then wrote
    # "Kris mentioned…" — and papered over a pricing gap with "a colleague will
    # confirm the exact rate" instead of the rule-5 hold.
    p = build_system_prompt({})
    assert "never 'Kris mentioned'" in p
    assert "defer it under rule 5 rather than guessing" in p


def test_schema_requires_thread_state_and_summaries():
    for f in ("thread_state", "request_summary", "action_summary"):
        assert f in DRAFT_SCHEMA["properties"]
        assert f in DRAFT_SCHEMA["required"]
