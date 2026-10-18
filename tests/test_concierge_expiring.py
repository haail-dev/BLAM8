"""Checking what's running out across every customer (Kris 2026-09-23: "a way
to monitor/check/remind for expiring documents").

The daily sweep (dependants_sweep) already puts a card on Today for a gated
date that lapses before a booking; list_expiring is the owner ASKING — "whose
vaccinations run out this month?" — across the whole book at once.
"""
from datetime import date, timedelta

from app import concierge_data, concierge_reads, scheduling

TODAY = date(2026, 9, 23)
DOG = {"id": "g-dog", "label": "Dog", "fields": [
    {"key": "vaccinations", "label": "Vaccinations", "type": "date", "durability": "expires", "gate": True},
    {"key": "breed", "label": "Breed", "type": "text"},
]}


def _inst(iid, name, jab, cid="c1"):
    return {"id": iid, "group_id": "g-dog", "contact_id": cid, "name": name,
            "data": {"vaccinations": jab, "breed": "Lab"}}


def _stub(monkeypatch, instances, bookings=None, contacts=None, defs=None):
    class FakeNow:
        @staticmethod
        def now(tz=None):
            from datetime import datetime
            return datetime(2026, 9, 23, 12, 0, tzinfo=tz)
    monkeypatch.setattr(concierge_reads, "datetime", FakeNow)
    monkeypatch.setattr(concierge_reads.db, "list_requirement_groups", lambda t, client=None: [DOG])
    monkeypatch.setattr(concierge_reads.db, "list_gated_instances", lambda t, client=None: instances)
    monkeypatch.setattr(concierge_reads.db, "upcoming_bookings_by_instance",
                        lambda t, ids, since, client=None: bookings or {})
    monkeypatch.setattr(concierge_reads.db, "crm_get_contact",
                        lambda cid, t, client=None: {"id": cid, "name": "Nadia", "email": "n@x.com"})
    monkeypatch.setattr(concierge_reads.db, "crm_list_field_definitions",
                        lambda t, entity=None, client=None: defs or [])
    monkeypatch.setattr(concierge_reads.db, "crm_contacts_with_data", lambda t, client=None: contacts or [])


def test_whats_running_out_soonest_first_with_the_owner(monkeypatch):
    _stub(monkeypatch, [_inst("a", "Bruno", "2026-10-18"), _inst("b", "Pip", "2026-09-01"),
                        _inst("c", "Bella", "2027-09-21")])
    out = concierge_reads.list_expiring("t1", None, {"within_days": 30})
    assert [r["name"] for r in out["expiring"]] == ["Pip", "Bruno"]      # Bella is a year off
    pip, bruno = out["expiring"]
    assert pip["lapsed"] is True and pip["days"] == -22
    assert bruno["lapsed"] is False and bruno["days"] == 25
    assert bruno["owner"] == "Nadia" and bruno["detail"] == "Vaccinations"


def test_a_jab_that_runs_out_during_the_stay_blocks_it(monkeypatch):
    # Bruno's stay: Sat 17 Oct 08:30 → Mon 19 Oct 17:00; his jab runs out on the 18th.
    stay = {"booking_ref": "BK-1016", "booking_date": "2026-10-17", "start_block": 34, "length_blocks": 2 * 96 + 34}
    _stub(monkeypatch, [_inst("a", "Bruno", "2026-10-18")], bookings={"a": [stay]})
    row = concierge_reads.list_expiring("t1", None, {})["expiring"][0]
    assert row["next_booking"] == {"reference": "BK-1016", "date": "2026-10-17", "ends": "2026-10-19"}
    assert row["blocks_next_booking"] is True
    # ...and one that covers the whole stay doesn't
    _stub(monkeypatch, [_inst("a", "Bruno", "2026-10-20")], bookings={"a": [stay]})
    assert concierge_reads.list_expiring("t1", None, {})["expiring"][0]["blocks_next_booking"] is False


def test_detail_narrows_and_a_customers_own_certificate_counts(monkeypatch):
    defs = [{"entity": "contact", "key": "first_aid", "label": "First aid certificate",
             "field_type": "date", "durability": "expires", "valid_months": 0}]
    contacts = [{"id": "c9", "name": "Sam", "email": "s@x.com", "custom_data": {"first_aid": "2026-10-01"}}]
    _stub(monkeypatch, [_inst("a", "Bruno", "2026-10-18")], contacts=contacts, defs=defs)
    both = concierge_reads.list_expiring("t1", None, {})
    assert [r["name"] for r in both["expiring"]] == ["Sam", "Bruno"]
    only = concierge_reads.list_expiring("t1", None, {"detail": "vaccinations"})
    assert [r["name"] for r in only["expiring"]] == ["Bruno"]


def test_the_tool_is_wired_and_routed():
    assert "list_expiring" in concierge_data.TOOL_NAMES
    assert concierge_data.tool_door("list_expiring") == "crm"
    assert "list_expiring" in concierge_data.WHOLE_BOOK_TOOLS
    from app import concierge
    src = concierge.build_business_prompt()
    assert "what is expiring' → list_expiring" in src


# ── the reminder: ask the owner for the renewed record ───────────────────────
import asyncio  # noqa: E402

import pytest  # noqa: E402

from app import concierge, concierge_actions as ca  # noqa: E402

BRUNO = {"id": "i-bruno", "group_id": "g-dog", "contact_id": "c-nadia", "name": "Bruno",
         "data": {"vaccinations": "2026-10-18"}}
NADIA = {"id": "c-nadia", "name": "Nadia Kowalski", "email": "nadia@x.com"}


def _stub_card(monkeypatch, contact=NADIA):
    monkeypatch.setattr(ca.db, "list_requirement_instances_all", lambda t, client=None, **k: [BRUNO])
    monkeypatch.setattr(ca.db, "list_requirement_groups", lambda t, client=None: [DOG])
    monkeypatch.setattr(ca.db, "crm_get_contact", lambda cid, t, client=None: contact)
    monkeypatch.setattr(ca.db, "get_invoice_settings", lambda t, client=None: {"business_name": "Little Paws"})


def test_the_card_asks_the_owner_and_says_whats_on_file(monkeypatch):
    _stub_card(monkeypatch)
    summary, payload, details, text = ca._stage_request_renewal("t1", None, {"name": "Bruno"})
    assert summary == "Ask Nadia Kowalski for Bruno's up-to-date Vaccinations (on file: 18th Oct 26)"
    assert details == ["To nadia@x.com"]   # just who it goes to (Kris 2026-09-24)
    assert "runs out on 18th Oct 26" in text and text.endswith("Little Paws")
    assert payload["subject"] == "Bruno's vaccinations — could you send the latest?"
    # the model's own words win when it wrote them
    _, _, _, own = ca._stage_request_renewal("t1", None, {"name": "Bruno", "draft_text": "Hi Nadia — quick one…"})
    assert own == "Hi Nadia — quick one…"


def test_the_card_refuses_what_it_cant_ask_for(monkeypatch):
    _stub_card(monkeypatch)
    with pytest.raises(ca.ActionError, match="dates that run out: Vaccinations"):
        ca._stage_request_renewal("t1", None, {"name": "Bruno", "title": "Breed"})
    _stub_card(monkeypatch, contact={"id": "c-nadia", "name": "Nadia", "email": ""})
    with pytest.raises(ca.ActionError, match="no email address"):
        ca._stage_request_renewal("t1", None, {"name": "Bruno"})


def test_confirm_sends_logs_and_notes_the_timeline(monkeypatch):
    sent, logged, stamps = [], [], []

    async def send_mail(t, to, subject, text):
        sent.append((to, subject, text))
    from app import mailbox, record_edits
    monkeypatch.setattr(mailbox, "send_mail", send_mail)
    monkeypatch.setattr(ca.db, "insert_email_log", lambda *a, **k: logged.append(k.get("kind")) or ("log-1", True))
    monkeypatch.setattr(ca.db, "mark_email_log_sent", lambda *a, **k: None)
    monkeypatch.setattr(ca.db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(record_edits, "_stamp", lambda t, cid, subj, line: stamps.append(line))
    out = asyncio.run(ca._exec_request_renewal("t1", None, {
        "instance_id": "i-bruno", "contact_id": "c-nadia", "to": "nadia@x.com", "name": "Bruno",
        "owner": "Nadia Kowalski", "label": "Vaccinations", "subject": "Bruno's vaccinations",
        "draft_text": "Hi Nadia"}, "Hi Nadia (edited)"))
    assert sent == [("nadia@x.com", "Bruno's vaccinations", "Hi Nadia (edited)")]   # the edit wins
    assert logged == ["renewal_request"]
    assert stamps == ["Bruno: asked for the latest Vaccinations — by you, from the Concierge"]
    assert out["summary"] == "Sent — asked Nadia Kowalski for Bruno's up-to-date Vaccinations."


def test_a_failed_send_says_nothing_went_out(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("token expired")
    from app import mailbox
    monkeypatch.setattr(mailbox, "send_mail", boom)
    with pytest.raises(ca.ActionError, match="Nothing went out"):
        asyncio.run(ca._exec_request_renewal("t1", None, {"instance_id": "i", "to": "n@x.com",
                                                          "subject": "s", "draft_text": "t"}, None))


def test_request_renewal_is_offered_everywhere():
    assert "request_renewal" in concierge.BUSINESS_ACTION_KINDS
    assert "request_renewal" in ca.allowed_kinds(full_qa=False, scheduling_on=False)
    for full in (True, False):
        p = concierge.build_business_prompt(business_name="Little Paws", today="x", full_qa=full)
        assert "request_renewal (email ONE owner" in p
