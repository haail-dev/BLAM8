"""Recorded evidence, using a fictional fixture modelled on the rig enquiry."""
import asyncio
import re
from types import SimpleNamespace

import pytest

from app import knowledge, pipeline, working
from test_thread_decision import ROW, _env, client  # noqa: F401
from test_knowledge_floor import _wire


QUERY = "Dripping kitchen tap\nMy kitchen tap keeps dripping. Can you come on Monday?"


def rig_working():
    result = SimpleNamespace(text="Could you send your address?", model="fixture",
                             reasoning='→ hold_booking({"date":"2026-09-28"})',
                             deferred=False, thread_state="waiting_customer",
                             searched_for="Bathroom plumbing availability")
    calls = [("check_availability", {"date_from": "2026-09-28", "date_to": "2026-09-28"},
              {"available": [{"window": "am"}, {"window": "pm"}]}),
             ("hold_booking", {"date": "2026-09-28"},
              {"error": "missing_info", "missing": ["Address"]})]
    lookup = working.retrieval_record(knowledge.RetrievalRows(
        [{"source": "Tap repairs"}, {"source": "Arrival times"},
         {"source": "Coverage", "_context": True}], outcome="ok", query=QUERY), QUERY, True)
    return pipeline._working_panel({"calls": calls}, result, lookup)


@pytest.mark.parametrize("status", ["pending", "sent"])
def test_rig_fixture_shows_actual_query_and_plain_panel(client, monkeypatch, status):
    _env(monkeypatch, [dict(ROW, status=status, subject="Dripping kitchen tap",
                           body_in=QUERY.split("\n")[1], draft_reasoning=rig_working(),
                           searched_for="Bathroom plumbing availability",
                           draft_sources=[{"source": "Tap repairs"}, {"source": "Arrival times"}])])
    html = client.get("/app/conversations/view/fragment?contact=sam@x.co").text
    visible = re.sub(r'<pre hidden data-working-technical>.*?</pre>', '', html, flags=re.S)
    assert "Dripping kitchen tap" in visible and "2 knowledge matches" in visible
    assert "Bathroom plumbing" not in visible
    assert "Also retrieved 1 other knowledge note." in visible
    assert all(heading in visible for heading in ("Looked at", "Found", "Decided"))
    assert "more customer details needed: Address" in visible
    assert "hold_booking" not in visible and "missing_info" not in visible
    assert "hold_booking" in html  # evidence retained, hidden from the owner
    assert "Needs you" not in visible


def test_retrieval_reports_failure_separately_from_empty_without_changing_results(monkeypatch):
    _wire(monkeypatch, [])
    empty = asyncio.run(knowledge.retrieve("t1", QUERY))
    assert empty == [] and empty.outcome == "ok"

    async def fail(*args, **kwargs):
        raise RuntimeError("fixture unavailable")

    async def no_wait(*args):
        pass

    monkeypatch.setattr(knowledge.embeddings, "embed_query", fail)
    monkeypatch.setattr(knowledge.asyncio, "sleep", no_wait)
    failed = asyncio.run(knowledge.retrieve("t1", QUERY))
    assert failed == [] and failed.outcome == "failed"
    assert working.knowledge_line(working.retrieval_record(failed, QUERY, True)) == \
        "The knowledge search could not be completed."
    monkeypatch.setattr(knowledge, "is_configured", lambda: False)
    off = asyncio.run(knowledge.retrieve("t1", QUERY))
    assert off == [] and off.outcome == "not_run"
    assert working.knowledge_line(working.retrieval_record(off, QUERY, False)) == ""


def test_search_records_the_exact_embedding_query_and_context(monkeypatch):
    _wire(monkeypatch, [{"source": "Other note", "content": "unrelated", "similarity": 0.1}])
    query = QUERY * 1000
    rows = asyncio.run(knowledge.retrieve("t1", query, whole_if_small=True))
    record = working.retrieval_record(rows, query, True)
    assert record["query"] == query[:knowledge._MAX_QUERY_CHARS]
    assert record["matches"] == 0 and record["context"] == 1
    assert len(rows) == 1 and rows[0]["_context"]  # prompt still receives the same note


@pytest.mark.parametrize("result, phrase", [
    ({"error": "missing_info", "missing": ["Address"]}, "more customer details needed"),
    ({"error": "closed"}, "closed that day"),
    ({"error": "tool_failed", "message": "hold_booking broke"}, "could not be confirmed"),
    ({"error": "unsupported"}, "arranged by you"),
    ({}, "could not be confirmed"), (None, "could not be confirmed"),
])
def test_four_outcomes_remain_distinct_and_unknown_is_never_success(result, phrase):
    line = working.outcome("hold_booking", {}, result)
    assert phrase in line
    assert "Held" not in line and "hold_booking" not in line and "tool_failed" not in line


def test_partial_offer_counts_only_actual_reservations_and_does_not_claim_delivery():
    result = {"status": "offered", "offered": [{"booking_ref": "BK-12"}],
              "rejected": [{"date": "2026-09-29", "error": "full"}],
              "hold_until_phrase": "tomorrow at noon"}
    line = working.outcome("offer_slots", {"slots": [{}, {}]}, result)
    assert line == "Reserved 1 option (BK-12) until tomorrow at noon. Nothing booked yet."
    assert "offered" not in working.outcome("check_availability", {}, {"slots": [{}, {}]})


def test_customer_detail_reservation_is_reported_even_when_details_are_missing():
    assert working.outcome("hold_booking", {}, {"error": "missing_info", "reserved": True}) == \
        "Reserved time while waiting for more customer details."


def test_final_pipeline_hold_overrides_the_model_reply_in_the_panel():
    result = SimpleNamespace(text="I booked it", reasoning="", model="fixture", deferred=False)
    saved = pipeline._working_panel({}, result, draft="", needs_owner=True)
    text = working.owner_panel(saved)
    assert "No reply was prepared." in text and "Needs you" in text


def test_legacy_machine_text_is_hidden_without_inventing_a_summary(client, monkeypatch):
    _env(monkeypatch, [dict(ROW, draft_reasoning='→ hold_booking({"error":"missing_info"})')])
    html = client.get("/app/conversations/view/fragment?contact=sam@x.co").text
    visible = re.sub(r'<pre hidden data-working-technical>.*?</pre>', '', html, flags=re.S)
    assert "plain-English account was not recorded" in visible
    assert "hold_booking" not in visible and "missing_info" not in visible
