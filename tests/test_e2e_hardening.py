"""Regression tests for the 2026-07-26 e2e hardening batch.

The live run (docs/e2e-run-2026-07-26-email-booking-invoice.md) surfaced:
raw structured-output JSON landing in the approval box, the self-loop guard
missing send-as identities, unpayable invoice emails signed 'Thank you, us',
and the concierge shipping a literal one-word 'test' reply. Each fix gets a
test here; the send-as pipeline test lives with its siblings in
tests/test_multi_mailbox.py.
"""
import json

from app import agent, concierge, invoice_send


# ── agent._parse_draft: the JSON-in-draft-box bug ────────────────────────────
def test_parsed_envelope_with_empty_reply_never_leaks_json():
    # Valid envelope, empty reply — exactly the live failure. The old code fell
    # back to the whole raw text, mailing the customer the JSON envelope.
    raw = json.dumps({"reply": "", "deferred": False, "missing": "",
                      "scheduling_requested": True, "thread_state": "in_progress",
                      "request_summary": "Wants setup call",
                      "action_summary": "x", "contact_name": "Velox",
                      "contact_company": "", "category": "needs_reply",
                      "category_reason": "y", "booking_details": []})
    parsed = agent._parse_draft(raw)
    assert parsed["reply"] == ""
    assert parsed["request_summary"] == "Wants setup call"   # metadata survives


def test_parsed_envelope_with_reply_uses_it():
    raw = json.dumps({"reply": "Hi Velox,\n\nTuesday works.", "thread_state": "in_progress"})
    assert agent._parse_draft(raw)["reply"] == "Hi Velox,\n\nTuesday works."


def test_non_json_prose_never_becomes_the_reply():
    # The raw-text fallback is GONE (live e2e 2026-09-02): a turn where the
    # model wrote deliberation instead of the envelope — "Actually wait — let
    # me look at what I know… so I cannot hold_booking…" — was stored as the
    # customer-facing draft. Prose now parses as an empty reply; the caller's
    # corrective retry re-asks for the envelope (a safety refusal takes the
    # same path and surfaces as draft-failed, not as a mailed refusal).
    assert agent._parse_draft("I can't help with that request.")["reply"] == ""
    assert agent._parse_draft(
        "The diary tool has a hard rule. Actually wait - let me think..."
    )["reply"] == ""


def test_truncated_json_never_reaches_the_customer():
    # Cut off mid-envelope: unparseable AND json-looking — must yield an empty
    # reply (draft-failed path), never half a JSON blob.
    assert agent._parse_draft('{"reply": "Hi Velox')["reply"] == ""


# ── invoice email body: sign-off + payment route ─────────────────────────────
_INV = {"invoice_ref": "INV-9001", "total": 99.0, "due_date": "2026-08-09"}


def test_invoice_body_signs_off_without_placeholder():
    body = invoice_send.invoice_email_body(_INV, {"business_name": ""}, None)
    assert "us" not in body.split("Thank you")[-1]
    assert body.rstrip().endswith("Thank you")


def test_invoice_body_signs_off_with_business_name():
    body = invoice_send.invoice_email_body(_INV, {"business_name": "Acme Ltd"}, None)
    assert body.rstrip().endswith("Thank you,\nAcme Ltd")


def test_payment_route_gap_flags_no_route():
    assert invoice_send.payment_route_gap({}, None)                    # nothing → warn
    assert invoice_send.payment_route_gap({}, "https://pay/x") is None  # Pay Now link
    bank = {"bank_sort_code": "00-00-00", "bank_account_number": "12345678"}
    assert invoice_send.payment_route_gap(bank, None) is None           # bank details


def test_invoice_body_carries_bank_details_when_set():
    s = {"business_name": "Acme Ltd", "bank_account_name": "Acme Ltd",
         "bank_sort_code": "00-00-00", "bank_account_number": "12345678"}
    body = invoice_send.invoice_email_body(_INV, s, None)
    assert "Sort code: 00-00-00" in body
    assert "Reference: INV-9001" in body


# ── concierge: degenerate replies + stray leading punctuation ────────────────
def test_one_word_reply_is_degenerate():
    assert concierge._degenerate_reply("test", [], None) is True


def test_real_answers_and_action_turns_are_not_degenerate():
    assert concierge._degenerate_reply("I checked the diary — Monday needs 24h notice.",
                                       [], None) is False
    # A short reply that carries an action card is a useful turn.
    assert concierge._degenerate_reply("Ready.", [], {"kind": "invoice_send"}) is False


def test_stray_leading_period_is_trimmed():
    text = json.dumps({"reply": ". INV-1003 isn't overdue yet.", "links": [], "action": None})
    reply, links, action = concierge._parse_business(text)
    assert reply == "INV-1003 isn't overdue yet."
    assert links == [] and action is None
