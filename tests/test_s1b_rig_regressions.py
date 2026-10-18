"""Offline replay of S1b's two rig failures and the VB107 name lookup.

Model turns are scripted; these prove guards, not live model compliance.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app import agent, concierge_data, db
from tests.test_draft_says_only_what_it_knows import _env, _script, _text, _tool


@pytest.fixture
def business():
    return {
        "cfg": {"business_name": "Fixture Services"},
        "email": {"subject": "Work enquiry", "body": "Do you fit wet underfloor heating?"},
        "knowledge": "\n".join([
            "[Services] Boiler servicing and repairs. Surveys can be booked.",
            "[Coverage] Bath and within 15 miles.",
            "[Contact] Reply to this email.", "[Payment] Payment by bank transfer.",
            "[Price] A survey costs £40.", "[Hours] Tuesday to Saturday.",
            "[Access] Please give access instructions.", "[Address] Ask for the site address.",
            "[People] Ask who will be present.", "[Receipt] A receipt follows payment.",
        ]),
    }


REFUSAL = "Wet underfloor heating isn't something we're able to help with — it's outside what we do."


def draft(business, **kwargs):
    return asyncio.run(agent.draft_reply(business["cfg"], business["email"],
                                         knowledge=business["knowledge"], **kwargs))


def test_vb94_missing_work_returns_named_hold_and_does_not_restore_refusal(monkeypatch, business):
    held = {**_env(""), "deferred": True, "missing": "Whether wet underfloor heating is offered"}
    seen = _script(monkeypatch, [_text(_env(REFUSAL)), _text(held)])
    out = draft(business)
    assert out.text == "" and out.deferred and "underfloor heating" in out.missing
    assert not out.blocked
    assert len(seen) == 2
    assert "booking catalogue is not exhaustive" in seen[-1]["messages"][-1]["content"]


def test_vb94_full_auto_stores_a_knowledge_hold_and_sends_nothing(monkeypatch, business):
    from app import pipeline
    from tests.test_push import EMAIL, Env
    real_draft = agent.draft_reply
    env = Env(monkeypatch, auto_send=True)
    monkeypatch.setattr(agent, "draft_reply", real_draft)
    monkeypatch.setattr(pipeline.kb, "render_for_prompt", lambda *a: business["knowledge"])
    stored = []
    monkeypatch.setattr(db, "insert_email_log",
                        lambda *a, **k: stored.append({**k, "draft_reply": a[4]}) or ("log-1", True))
    _script(monkeypatch, [_text(_env(REFUSAL)), _text({**_env(""), "deferred": True,
        "missing": "Whether wet underfloor heating is offered"})])
    asyncio.run(pipeline.draft_for_email("t1", {**EMAIL, **business["email"]},
                                        source_message_id="m1", auto_send_ok=True))
    assert not env.sent
    assert not stored[0]["draft_reply"]
    assert "underfloor heating" in stored[0]["knowledge_gap"]
    assert stored[0]["thread_state"] == "needs_attention"


@pytest.mark.parametrize("repair", [REFUSAL, ""])
def test_vb94_bad_or_empty_repair_still_holds(monkeypatch, business, repair):
    _script(monkeypatch, [_text(_env(REFUSAL)), _text(_env(repair))])
    out = draft(business)
    assert out.text == "" and out.deferred and "underfloor heating" in out.missing
    assert not out.blocked


def test_vb84_known_work_is_answered(monkeypatch, business):
    business["knowledge"] += "\n[Additional work] We fit wet underfloor heating."
    _script(monkeypatch, [_text(_env("Yes, we fit wet underfloor heating."))])
    out = draft(business)
    assert out.text == "Yes, we fit wet underfloor heating."
    assert not out.deferred and not out.blocked


def test_an_explicit_negative_fact_is_answered(monkeypatch, business):
    refusal = "We do not fit wet underfloor heating."
    business["knowledge"] += "\n[Scope] " + refusal
    _script(monkeypatch, [_text(_env(refusal))])
    out = draft(business)
    assert out.text == refusal and not out.deferred


def test_a_negative_fact_in_owner_settings_is_also_sourced(monkeypatch, business):
    refusal = "We do not fit wet underfloor heating."
    business["cfg"]["custom_instructions"] = refusal
    _script(monkeypatch, [_text(_env(refusal))])
    assert draft(business).text == refusal


def test_catalogue_words_cannot_source_a_negative_claim(business):
    # All the subject words exist, but the negative business fact does not.
    kb = business["knowledge"] + "\nWe fit wet underfloor heating."
    assert agent._unsourced_bits("We do not fit wet underfloor heating.", kb)


def test_refusal_variants_and_positive_control(business):
    for refusal in (REFUSAL, REFUSAL.replace("'", "’"),
                    "That is outside our scope.", "We don't provide wet underfloor heating."):
        assert agent._unsourced_service_refusals(refusal, business["knowledge"])
    assert not agent._unsourced_service_refusals("We can fit wet underfloor heating.", "")


SUMMARY = [{"date": f"2026-09-{day}", "free": 4} for day in (29, 30)] + [
    {"date": f"2026-10-0{day}", "free": 4} for day in (1, 2, 3)]
BAD_DAYS = "We have slots available every day this week (Monday through Saturday)."
GOOD_DAYS = "Tuesday has 4 slots free, Wednesday 4, Thursday 4, Friday 4 and Saturday 4."
TOOLS = [{"name": "check_availability", "description": "", "input_schema": {"type": "object"}}]


@pytest.mark.parametrize("reply,missing", [
    (BAD_DAYS, ["Monday"]),
    ("Slots are available Monday–Saturday.", ["Monday"]),
    ("We have availability Monday to Saturday.", ["Monday"]),
    ("We have availability Mon-Sat.", ["Monday"]),
    ("Monday and Tuesday have free slots.", ["Monday"]),
    (GOOD_DAYS, []),
    ("Slots are available Tuesday through Saturday.", []),
    ("Monday is not available; Tuesday has free slots.", []),
])
def test_vb85_day_claims_are_checked_against_summary(reply, missing):
    assert agent._unsupported_availability_days(reply, {"day_summary": SUMMARY}) == missing


def test_range_checks_interior_days_not_just_endpoints():
    assert agent._unsupported_availability_days("Slots available Tuesday through Saturday.",
        {"day_summary": [SUMMARY[0], SUMMARY[-1]]}) == ["Wednesday", "Thursday", "Friday"]


def test_dated_range_checks_intervening_days():
    assert agent._unsupported_availability_days(
        "Slots available Tuesday 29 September through Saturday 3 October.",
        {"day_summary": [SUMMARY[0], SUMMARY[-1]]}) == ["Wednesday", "Thursday", "Friday"]


def test_same_weekday_in_another_week_does_not_back_an_explicit_date():
    assert agent._unsupported_availability_days("Tuesday 6 October has free slots.",
                                                {"day_summary": SUMMARY}) == ["Tuesday 6 October"]


@pytest.mark.parametrize("result", [{"available": []}, {"error": "tool_failed"},
                                     {"day_summary": [{"date": "2026-09-28", "free": 0}]}])
def test_empty_failed_and_zero_results_never_back_monday(result):
    assert agent._unsupported_availability_days("Monday has free slots.", result) == ["Monday"]


def test_unqualified_every_day_needs_explicit_dates():
    assert agent._unsupported_availability_days("We have slots every day this week.",
                                                {"day_summary": SUMMARY})


@pytest.mark.parametrize("repair", [GOOD_DAYS, BAD_DAYS])
def test_vb85_loop_repairs_or_holds_repeated_overclaim(monkeypatch, business, repair):
    seen = _script(monkeypatch, [
        _tool("check_availability", {"date_from": "2026-09-28", "date_to": "2026-10-03"}),
        _tool(agent.READY_TOOL["name"], {}, tid="t2"),
        _text(_env(BAD_DAYS)), _text(_env(repair)),
    ])
    out = draft(business, tools=TOOLS, scheduling=True,
                tool_executor=lambda *a: {"day_summary": SUMMARY})
    if repair == GOOD_DAYS:
        assert out.text == GOOD_DAYS and not out.blocked
    else:
        assert out.text == "" and "Monday" in out.blocked
        assert not out.deferred and not out.missing
    assert len(seen) == 4


def test_vb85_exhausted_loop_cannot_bypass_day_guard(monkeypatch, business):
    monkeypatch.setattr(agent, "TOOL_LOOP_MAX_CALLS", 3)
    seen = _script(monkeypatch, [
        _tool("check_availability", {"date_from": "2026-09-28", "date_to": "2026-10-03"}),
        _tool(agent.READY_TOOL["name"], {}, tid="t2"), _text(_env(BAD_DAYS)),
    ])
    out = draft(business, tools=TOOLS, scheduling=True,
                tool_executor=lambda *a: {"day_summary": SUMMARY})
    assert out.text == "" and "Monday" in out.blocked and len(seen) == 3


def test_vb85_a_later_rewrite_cannot_reintroduce_the_overclaim(monkeypatch, business):
    _script(monkeypatch, [
        _tool("check_availability", {"date_from": "2026-09-28", "date_to": "2026-10-03"}),
        _tool(agent.READY_TOOL["name"], {}, tid="t2"),
        _text(_env("Tuesday has free slots. It usually points to a fault.")),
        _text(_env(BAD_DAYS)),
    ])
    out = draft(business, tools=TOOLS, scheduling=True,
                tool_executor=lambda *a: {"day_summary": SUMMARY})
    assert out.text == "" and "Monday" in out.blocked


def test_vb107_search_uses_historical_name_without_renaming_contact(monkeypatch):
    calls = []
    monkeypatch.setattr(db, "crm_list_contacts", lambda *a, **k: [])
    monkeypatch.setattr(db, "crm_contacts_by_dependant_name", lambda *a, **k: [])
    def known(tenant, q, **kwargs):
        calls.append((tenant, q, kwargs))
        return [{"id": "c1", "name": "Martin", "email": "m123@example.test"}]
    monkeypatch.setattr(db, "crm_contacts_by_known_name", known)
    out = concierge_data._search_contacts("tenant-a", "scoped-client", {"query": "Martin Keane"})
    assert out["contacts"][0]["name"] == "Martin"
    assert out["contacts"][0]["email"] == "m123@example.test"
    assert calls[0][:2] == ("tenant-a", "Martin Keane")
    assert calls[0][2]["client"] == "scoped-client"


class Query:
    def __init__(self, table, calls):
        self.table, self.calls = table, calls
    def __getattr__(self, method):
        def chain(*args, **kwargs):
            self.calls.append((self.table, method, args, kwargs))
            return self
        return chain
    def execute(self):
        rows = ([{"from_address": "M123@example.test"}, {"from_address": "m123@example.test"}]
                if self.table == "email_log" else [{"id": "c1", "name": "Martin"}])
        return SimpleNamespace(data=rows)


def test_known_name_query_scopes_both_tables_and_only_searches_identity():
    calls = []
    client = SimpleNamespace(table=lambda table: Query(table, calls))
    assert db.crm_contacts_by_known_name("tenant-a", "Martin Keane", client=client)[0]["id"] == "c1"
    for table in ("email_log", "crm_contacts"):
        assert (table, "eq", ("tenant_id", "tenant-a"), {}) in calls
    assert ("crm_contacts", "in_", ("email", ["m123@example.test"]), {}) in calls
    filters = [c[2][0] for c in calls if c[1] == "or_"]
    assert filters == ["from_name.ilike.%Martin Keane%,contact_name.ilike.%Martin Keane%"]
