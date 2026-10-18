"""Scheduling module — agent tool definitions + executor (build step 2, second half).

The two tools are the locked platform pattern (D3): check_availability and
hold_booking, injected into the draft call only when the tenant's scheduling
module is active. These tests pin:

  - tool definitions: names, strict schema on check_availability, window enum
  - the executor: routes tool calls to the availability/hold functions, returns
    JSON-able dicts, maps error codes to prescriptive messages the model can act
    on, and enforces AT MOST ONE hold per email in code (not in the prompt)
  - executor state: placed holds are recorded so the pipeline can link them to
    the email_log row afterwards (step 4)

Run: python -m pytest tests/ -q
"""
from tests.booking_fixtures import carried_config
from datetime import datetime

import pytest

from app import scheduling
from app.scheduling import DIARY_TZ, SCHEDULING_TOOLS, make_executor

NOW = datetime(2026, 6, 16, 10, 0, tzinfo=DIARY_TZ)  # Tuesday


@pytest.fixture
def no_db(monkeypatch):
    """Default diary config, no bookings, no dangling offers, holds always
    succeed (id b-1)."""
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(None))
    monkeypatch.setattr(scheduling.db, "list_active_bookings", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "offered_bookings_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "release_offered_for_contact", lambda *a, **k: 0)
    monkeypatch.setattr(
        scheduling.db, "hold_booking_if_capacity",
        lambda tid, d, w, s, **kw: {"id": "b-1", "booking_date": d, "window": w,
                                    "status": kw.pop("status", None) or "proposed", **kw},
    )


# ── tool definitions ──────────────────────────────────────────────────────────
def test_tool_definitions_shape():
    names = [t["name"] for t in SCHEDULING_TOOLS]
    assert names == ["check_availability", "hold_booking", "offer_slots"]
    check, hold, offer = SCHEDULING_TOOLS
    assert check["strict"] is True
    assert check["input_schema"]["additionalProperties"] is False
    assert check["input_schema"]["required"] == ["date_from", "date_to"]
    # No enum on window — slots mode passes start times like '14:00'; the executor
    # validates against the mode's generated windows instead.
    assert "enum" not in hold["input_schema"]["properties"]["window"]
    assert "check_availability" in hold["input_schema"]["properties"]["window"]["description"]
    assert set(hold["input_schema"]["required"]) == {"date", "window", "summary"}
    # The descriptions are the agent's contract — keep them prescriptive.
    assert "BEFORE" in check["description"]
    assert "One hold per visit" in hold["description"]
    # offer_slots (docs/modules/module-smart-booking-offers.md): max 3 slots, deadline contract.
    assert offer["input_schema"]["properties"]["slots"]["maxItems"] == 3
    assert set(offer["input_schema"]["required"]) == {"slots", "summary"}
    assert "expires_at" in offer["description"]
    assert "deadline_line" in offer["description"]
    assert "deadline" in offer["description"]


# ── check_availability via the executor ───────────────────────────────────────
def test_executor_check_availability(no_db):
    execute, state = make_executor("t1", now=NOW)
    out = execute("check_availability", {"date_from": "2026-06-16", "date_to": "2026-06-19"})
    assert out["available"] == [
        {"date": "2026-06-18", "window": "day", "remaining": 4},
        {"date": "2026-06-19", "window": "day", "remaining": 4},
    ]
    assert state["bookings"] == []


def test_executor_check_availability_empty_range_says_so(no_db):
    execute, _ = make_executor("t1", now=NOW)
    out = execute("check_availability", {"date_from": "2026-06-20", "date_to": "2026-06-21"})
    assert out["available"] == []
    assert "No bookable" in out["message"]


def test_executor_invalid_dates(no_db):
    execute, _ = make_executor("t1", now=NOW)
    out = execute("check_availability", {"date_from": "next friday", "date_to": "2026-06-19"})
    assert out["error"] == "invalid_date"
    out = execute("hold_booking", {"date": "junk", "window": "day", "summary": "x"})
    assert out["error"] == "invalid_date"


# ── hold_booking via the executor ─────────────────────────────────────────────
def test_executor_hold_success_records_state(no_db):
    execute, state = make_executor("t1", contact="ops@acme.co.uk", thread_id="th-9", now=NOW)
    out = execute("hold_booking", {
        "date": "2026-06-18", "window": "day",
        "summary": "3 pallets, M17 -> LS9", "details": {"pallets": 3},
    })
    assert out == {"held": True, "booking_id": "b-1", "booking_ref": None,
                   "date": "2026-06-18",
                   "window": "day", "status": "proposed"}
    assert len(state["bookings"]) == 1
    row = state["bookings"][0]
    assert row["contact"] == "ops@acme.co.uk"
    assert row["thread_id"] == "th-9"
    assert row["details"] == {"pallets": 3}
    assert row["source"] == "email"


def test_executor_one_hold_per_email(no_db):
    execute, state = make_executor("t1", now=NOW)
    assert execute("hold_booking", {"date": "2026-06-18", "window": "day", "summary": "a"})["held"]
    out = execute("hold_booking", {"date": "2026-06-19", "window": "day", "summary": "b"})
    assert out["error"] == "already_held"
    assert out["booking_id"] == "b-1"
    assert len(state["bookings"]) == 1  # the second hold never reached the diary


def test_executor_hold_full_and_validation_errors(no_db, monkeypatch):
    monkeypatch.setattr(scheduling.db, "hold_booking_if_capacity", lambda *a, **k: None)
    execute, state = make_executor("t1", now=NOW)
    out = execute("hold_booking", {"date": "2026-06-18", "window": "day", "summary": "x"})
    assert out["error"] == "full"
    assert "check_availability" in out["message"]
    assert state["bookings"] == []

    out = execute("hold_booking", {"date": "2026-06-20", "window": "day", "summary": "x"})
    assert out["error"] == "closed"  # Saturday — rejected before the RPC
    out = execute("hold_booking", {"date": "2026-06-18", "window": "am", "summary": "x"})
    assert out["error"] == "invalid_window"  # 'day' diary has no am window


def test_executor_unknown_tool(no_db):
    execute, _ = make_executor("t1", now=NOW)
    assert execute("send_invoice", {})["error"] == "unknown_tool"
