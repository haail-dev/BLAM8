"""Security review 2026-09 follow-ups: health values stay out of the
Concierge's model; switched-off people and other people's cards are refused;
a narrower member's notification swipe/seen touches only rows they were shown."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import concierge_actions, concierge_reads, concierge_routes, db

HEALTH = {"key": "allergies", "label": "Allergies / medical", "entity": "contact",
          "field_type": "text", "sensitive": True}
GATE = {"key": "gate", "label": "Gate code", "entity": "contact", "field_type": "text"}
CONTACT = {"id": "c1", "name": "Hannah", "email": "hannah@x.co",
           "custom_data": {"allergies": "on warfarin", "gate": "4071"}}


def _ctx(user_id="u1"):
    return SimpleNamespace(tenant_id="t1", client=None, user_id=user_id, viewing_as=None)


def _as(monkeypatch, row):
    monkeypatch.setattr(db, "get_member", lambda *a, **k: row)


def _row(role="staff", active=True, pages=("today", "diary")):
    return {"user_id": "u1", "role": role, "active": active,
            "scopes": {"pages": list(pages)}}


def _contact_stub(monkeypatch):
    monkeypatch.setattr(concierge_reads, "_resolve_contact", lambda *a: (dict(CONTACT), []))
    monkeypatch.setattr(db, "crm_list_field_definitions", lambda *a, **k: [HEALTH, GATE])


def test_the_lookup_says_a_health_detail_is_on_record_but_never_what(monkeypatch):
    _contact_stub(monkeypatch)
    out = concierge_reads.get_contact_record("t1", None, {"contact": "hannah"})
    assert "warfarin" not in str(out)
    health = next(f for f in out["fields"] if f["label"] == "Allergies / medical")
    assert health["on_record"] is True
    assert any(f["value"] == "4071" for f in out["fields"])   # ordinary fields unchanged


def test_the_assistant_will_not_stage_a_health_field_edit(monkeypatch):
    _contact_stub(monkeypatch)
    with pytest.raises(concierge_actions.ActionError) as e:
        concierge_actions._stage_update_contact(
            "t1", None, {"contact": "hannah", "content": "no allergies", "title": "Allergies / medical"})
    assert "health detail" in str(e.value)


def _proposal(monkeypatch, owner="someone-else"):
    monkeypatch.setattr(db, "get_concierge_proposal",
                        lambda *a, **k: {"id": "p1", "user_id": owner, "status": "pending", "kind": "x"})
    claimed = []
    monkeypatch.setattr(db, "claim_concierge_proposal", lambda *a, **k: claimed.append(1))
    statuses = []
    monkeypatch.setattr(db, "set_concierge_proposal_status", lambda *a, **k: statuses.append(a))
    return claimed, statuses


def test_a_switched_off_member_cannot_confirm_or_bin_a_card(monkeypatch):
    claimed, statuses = _proposal(monkeypatch, owner="u1")
    _as(monkeypatch, _row(active=False))
    body = concierge_routes.BusinessExecuteRequest(proposal_id="p1")
    with pytest.raises(HTTPException) as e:
        asyncio.run(concierge_routes.business_execute(body, _ctx()))
    assert e.value.status_code == 403 and not claimed
    with pytest.raises(HTTPException):
        concierge_routes.business_dismiss(
            concierge_routes.BusinessWaitingRequest(proposal_id="p1"), _ctx())
    assert not statuses


def test_staff_cannot_confirm_or_bin_someone_elses_card(monkeypatch):
    claimed, statuses = _proposal(monkeypatch)
    _as(monkeypatch, _row())
    r = asyncio.run(concierge_routes.business_execute(
        concierge_routes.BusinessExecuteRequest(proposal_id="p1"), _ctx()))
    assert r.ok is False and r.state == "unknown" and "not available" in r.summary and not claimed
    d = concierge_routes.business_dismiss(
        concierge_routes.BusinessWaitingRequest(proposal_id="p1"), _ctx())
    assert d == {"ok": False} and not statuses


def test_an_owner_may_still_act_on_any_card(monkeypatch):
    _proposal(monkeypatch)
    _as(monkeypatch, _row(role="owner"))
    assert concierge_routes._not_their_proposal(_ctx(), "p1") is None
    _as(monkeypatch, None)
    assert concierge_routes._not_their_proposal(_ctx(), "p1") is None


FEED = [{"id": "n1", "kind": "invoice_paid", "title": "INV-1 paid", "seen_at": None},
        {"id": "n2", "kind": "portal_booking_created", "title": "Booked", "seen_at": None}]


def test_seen_marks_only_the_rows_a_narrower_member_was_shown(monkeypatch):
    monkeypatch.setattr(db, "list_concierge_notifications", lambda *a, **k: FEED)
    marked = []
    monkeypatch.setattr(db, "mark_concierge_notifications_seen",
                        lambda t, ids=None, client=None: marked.append(ids))
    _as(monkeypatch, _row(pages=["today", "diary"]))
    concierge_routes.notifications_seen(_ctx())
    assert marked == [["n2"]]
    marked.clear()
    _as(monkeypatch, None)
    concierge_routes.notifications_seen(_ctx())
    assert marked == [None]


def test_a_swipe_on_a_row_they_cannot_see_changes_nothing(monkeypatch):
    monkeypatch.setattr(db, "get_concierge_notification", lambda t, i, client=None:
                        next(n for n in FEED if n["id"] == i))
    dismissed = []
    monkeypatch.setattr(db, "dismiss_concierge_notification",
                        lambda t, i, client=None: dismissed.append(i))
    _as(monkeypatch, _row(pages=["today", "diary"]))
    concierge_routes.dismiss_notification("n1", _ctx())
    concierge_routes.dismiss_notification("n2", _ctx())
    assert dismissed == ["n2"]


def test_a_switched_off_member_cannot_swipe_or_mark_seen(monkeypatch):
    _as(monkeypatch, _row(active=False))
    with pytest.raises(HTTPException):
        concierge_routes.notifications_seen(_ctx())
    with pytest.raises(HTTPException):
        concierge_routes.dismiss_notification("n1", _ctx())
