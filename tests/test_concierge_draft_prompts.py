"""What the model is told about editing and deleting drafts (15 October 2026).

N20 on the rig: the Concierge said deleting "needs to be done in the main app" (the main
app has no invoice delete) and offered a "£200 update" (there was no edit kind). Now the
four kinds exist (tests/test_concierge_draft_edit_delete*.py), and the model-facing text
has to say exactly what they are for, and nothing more: a DRAFT invoice or quote, after
the owner taps Confirm; never a sent one, a contact, a booking or a setting.

Run: python -m pytest tests/test_concierge_draft_prompts.py -q
"""
import json
import re

import pytest

from app import concierge, concierge_data, gptlive

FOUR = ("invoice_edit_draft", "invoice_delete_draft", "quote_edit_draft", "quote_delete_draft")


def _flat(text: str) -> str:
    return " ".join(text.split())


# ── the kinds the model may propose ───────────────────────────────────────────
def test_the_four_kinds_are_proposable_and_nothing_else_deletes():
    assert set(FOUR) <= set(concierge.BUSINESS_ACTION_KINDS)
    assert {k for k in concierge.BUSINESS_ACTION_KINDS if "delete" in k} == {
        "invoice_delete_draft", "quote_delete_draft"}
    # the structured-output schema and the answer tool both carry them
    assert set(FOUR) <= set(concierge.BUSINESS_SCHEMA["properties"]["action"]["properties"]["kind"]["enum"])
    tool = concierge.answer_tool(actions=True)["input_schema"]["properties"]["action"]
    assert set(FOUR) <= set(tool["properties"]["kind"]["enum"])
    # ...and a bulk card may hold them, since they are not 'bulk' themselves
    assert set(FOUR) <= set(tool["properties"]["items"]["items"]["properties"]["kind"]["enum"])
    # a surface that cannot act carries no action property at all
    assert "action" not in concierge.answer_tool(actions=False)["input_schema"]["properties"]


def test_the_new_lines_field_reaches_the_stager_as_text_whatever_shape_the_model_writes_it():
    assert "lines" in concierge._ACTION_FIELDS
    assert "lines" in concierge.BUSINESS_SCHEMA["properties"]["action"]["properties"]["fields"]["description"]
    props = concierge.answer_tool(actions=True)["input_schema"]["properties"]["action"]["properties"]
    assert props["lines"] == {"type": "string"}
    as_text = {"reply": "r", "links": [], "action": {"kind": "invoice_edit_draft", "fields": json.dumps(
        {"invoice_id": "INV-1002", "lines": "Parts | 120.50\nLabour | 79.50"})}}
    as_list = {"reply": "r", "links": [], "action": {"kind": "invoice_edit_draft", "fields": json.dumps(
        {"invoice_id": "INV-1002", "lines": [{"description": "Parts", "amount": "120.50"}]})}}
    _, _, a = concierge._parse_business(json.dumps(as_text))
    assert a["kind"] == "invoice_edit_draft" and a["lines"] == "Parts | 120.50\nLabour | 79.50"
    _, _, b = concierge._parse_business(json.dumps(as_list))
    assert json.loads(b["lines"]) == [{"description": "Parts", "amount": "120.50"}]   # real JSON, not a Python repr
    # inside a bulk card too
    bulk = {"reply": "r", "links": [], "action": {"kind": "bulk", "items": [
        {"kind": "quote_edit_draft", "quote_id": "QUO-1", "lines": [{"description": "A", "amount": 1}]}]}}
    _, _, c = concierge._parse_business(json.dumps(bulk))
    assert json.loads(c["items"][0]["lines"]) == [{"description": "A", "amount": 1}]


# ── the guidance ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("kwargs", [{}, {"full_qa": False}, {"voice_mode": True}], ids=["pro", "starter", "voice"])
def test_the_prompt_teaches_the_four_kinds_in_one_line_each(kwargs):
    p = _flat(concierge.build_business_prompt(**kwargs))
    for kind in FOUR:
        assert kind in p, kind
    assert "to change an amount on a draft, replace its lines" in p
    assert "to remove a draft, delete it" in p
    assert "never for a sent one" in p
    assert "ask which draft" in p and "naming each by number, date and amount" in p
    assert "if they have not decided between changing it and deleting it, ask that in the same question" in p
    # the model is told where the current lines are, so it never writes them blind
    assert "list_invoices" in p and "its current lines" in p


def test_rule_one_no_longer_sends_deletes_to_the_main_app():
    p = _flat(concierge.build_business_prompt())
    assert "deletes belong in the main app" not in p
    assert "deleting anything other than a DRAFT invoice or quote" in p
    # it still never claims a change before the tap
    assert "never say you've done something, only that it's ready to confirm" in p
    assert "ONLY their Confirm button executes it" in p


def test_the_capability_text_says_what_it_can_and_cannot_change():
    p = _flat(concierge.build_business_prompt())
    assert "You cannot: delete records" not in p and "delete records" not in p
    assert "You can edit or delete a DRAFT invoice or quote once they tap Confirm on its card" in p
    assert "you cannot change or delete a sent one, a contact, a booking or a setting" in p
    assert "a deleted draft cannot be brought back" in p
    # the sentences the earlier waves pinned are all still there
    for phrase in ("WHAT YOU CAN DO", "NOTHING happens until they tap Confirm", "There is no undo button",
                   "cannot be unsent", "VAT or tax", "Invent no capability",
                   "send anything without their approval (a card, or the inbox's own approve-to-send setting)",
                   "change the app's settings (prices, hours, plan)"):
        assert phrase in p, phrase
    # starter tier: the kinds ride every tier, so the claim does too
    assert "You can edit or delete a DRAFT invoice or quote" in _flat(concierge.build_business_prompt(full_qa=False))


def test_a_surface_that_cannot_act_makes_no_such_claim():
    q = _flat(concierge.build_business_prompt(actions=False))
    assert "Changes are made from the Concierge page" in q
    assert "You can edit or delete" not in q and "delete records" not in q
    assert "invoice_edit_draft" not in q and "quote_delete_draft" not in q
    assert "You cannot: delete or change records from here" in q


def test_the_native_voice_summary_says_the_same_thing_in_a_sentence():
    prompt = _flat(gptlive.PROMPT)
    assert "delete records" not in prompt
    assert ("you can edit or delete a draft invoice or quote, but only once they tap Confirm on its card"
            in prompt)
    assert "you do not change or delete a sent one, a customer or a booking" in prompt
    for phrase in ("accounts, VAT or tax", "take payments", "send texts",
                   "change the app's settings (prices, hours, plan)", "no undo", "cannot be unsent"):
        assert phrase in prompt, phrase
    assert gptlive.config()["instructions"] == gptlive.PROMPT


def test_the_read_tools_say_a_drafts_lines_are_included():
    tools = {t["name"]: t["description"] for t in concierge_data.TOOLS}
    assert "invoice_edit_draft" in tools["list_invoices"] and "lines" in tools["list_invoices"]
    assert "quote_edit_draft" in tools["list_quotes"] and "lines" in tools["list_quotes"]


def test_nothing_in_the_new_text_names_a_trade_or_a_customer():
    """The agnostic rule is a hard rule: settings, not types. The guard scans every source
    string; this reads the new model-facing text itself against its word list."""
    from tests import agnostic_guard as guard
    text = " ".join([concierge.build_business_prompt(), concierge.build_business_prompt(actions=False),
                     gptlive.PROMPT] + [t["description"] for t in concierge_data.TOOLS])
    new = [s for s in re.split(r"(?<=[.;])\s+", text)
           if any(k in s for k in FOUR) or "DRAFT invoice" in s or "draft invoice" in s]
    assert any(k in s for s in new for k in FOUR) and any("DRAFT invoice or quote" in s for s in new)
    for sentence in new:
        assert not guard.RX.search(sentence), sentence
        assert not re.search(r"\b(Priya|Tom|Corvale|Foxholes)\b", sentence)
