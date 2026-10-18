"""quote_edit_draft and quote_delete_draft - the quote twins of the invoice kinds.

Same contract as tests/test_concierge_draft_edit_delete.py (read its header): a DRAFT
quote only, always on a confirm card, the same db.update_quote_lines the main app's
editor calls, refused in plain words once it has been sent, re-checked at Confirm.

What is different about a quote: its lines carry flags the owner set (optional,
ticked, linked to a job or a price entry, counts towards the deposit rule) which an
edit must keep; its total counts the ticked lines only; and a SENT quote CAN be
cancelled from the Concierge (quote_cancel), so the refusal says that rather than
claiming it cannot.

Run: python -m pytest tests/test_concierge_draft_edit_delete_quotes.py -q
"""
import asyncio
import copy

import pytest

from app import concierge_actions, concierge_data, db
from tests.test_concierge_draft_edit_delete import (  # noqa: F401  (fixtures and helpers)
    BOOKING, FOUR_KINDS, INV_1002, QUOTE_KINDS, T1, T2, QUO_1003, QUO_1005, QUO_9001,
    _Store, _invoice, _line, _person, _quote, _stage, _run, client, rig)
from app import people

KB = "00000000-0000-4000-8000-00000000c001"


def _qline(parent, desc, amount, order, tenant=T1, **over):
    return {"id": f"ql-{parent}-{desc}", "tenant_id": tenant, "quote_id": parent, "description": desc,
            "amount": amount, "quantity": 1, "unit_price": amount, "is_optional": False,
            "is_selected": True, "line_order": order, "booking_id": None, "kb_entry_id": None,
            "counts_to_deposit_rule": False, **over}


@pytest.fixture
def qrig(rig):
    s = rig.store.s
    s["quotes"] += [_quote(QUO_1003, "QUO-1003", who="Tom Hale"),
                    _quote(QUO_1005, "QUO-1005", total=400.0, who="Tom Hale"),
                    _quote(QUO_9001, "QUO-9001", tenant=T2, who="Someone Else")]
    s["quote_lines"] += [
        _qline(QUO_1003, "Work as discussed", 252.5, 0),
        _qline(QUO_1005, "Parts", 150.0, 0, booking_id=BOOKING, kb_entry_id=KB, counts_to_deposit_rule=True),
        _qline(QUO_1005, "Labour", 250.0, 1),
        _qline(QUO_1005, "Extras", 100.0, 2, is_optional=True, is_selected=False),
        _qline(QUO_9001, "Work as discussed", 252.5, 0, tenant=T2)]
    return rig


def _quote_row(rig, id_):
    return next((r for r in rig.store.s["quotes"] if r["id"] == id_), None)


def _qlines(rig, id_):
    return sorted((l["description"], l["amount"]) for l in rig.store.s["quote_lines"] if l["quote_id"] == id_)


def _qline_rows(rig, id_):
    return {l["description"]: l for l in rig.store.s["quote_lines"] if l["quote_id"] == id_}


# ═════════════════════════════════════════════════════════════════════════════
# the database helpers
# ═════════════════════════════════════════════════════════════════════════════
def test_update_draft_quote_lines_replaces_lines_totals_and_keeps_the_flags(qrig):
    qrig.settings.update(show_vat=True, vat_rate=20)
    lines = [{"description": "Parts", "amount": 150.0, "booking_id": BOOKING, "kb_entry_id": KB,
              "is_optional": False, "is_selected": True, "line_order": 0, "counts_to_deposit_rule": True},
             {"description": "Labour", "amount": 200.0, "is_optional": False, "is_selected": True, "line_order": 1},
             {"description": "Extras", "amount": 100.0, "is_optional": True, "is_selected": False, "line_order": 2}]
    assert db.update_draft_quote_lines(QUO_1005, T1, lines, client=qrig.store) is True
    row = _quote_row(qrig, QUO_1005)
    assert (row["subtotal"], row["vat"], row["total"]) == (350.0, 70.0, 420.0)     # the ticked lines only
    assert row["version"] == 1                                                      # a draft edit is not a revision
    rows = _qline_rows(qrig, QUO_1005)
    assert rows["Parts"]["booking_id"] == BOOKING and rows["Parts"]["kb_entry_id"] == KB
    assert rows["Parts"]["counts_to_deposit_rule"] is True
    assert rows["Extras"]["is_optional"] is True and rows["Extras"]["is_selected"] is False
    assert _qlines(qrig, QUO_1003) == [("Work as discussed", 252.5)]               # the neighbour is untouched


@pytest.mark.parametrize("status", ["sent", "accepted", "declined", "expired", "invoiced", "cancelled"])
def test_update_draft_quote_lines_refuses_anything_but_a_draft_and_writes_nothing(qrig, status):
    _quote_row(qrig, QUO_1003)["status"] = status
    before = copy.deepcopy(qrig.store.s)
    assert db.update_draft_quote_lines(QUO_1003, T1, [{"description": "X", "amount": 1.0}],
                                       client=qrig.store) is False
    assert qrig.store.s == before


def test_update_draft_quote_lines_is_tenant_scoped(qrig):
    before = copy.deepcopy(qrig.store.s)
    assert db.update_draft_quote_lines(QUO_9001, T1, [{"description": "X", "amount": 1.0}],
                                       client=qrig.store) is False
    assert qrig.store.s == before


def test_the_quote_preview_totals_are_what_the_save_stores(qrig):
    for include in (None, True):
        qrig.settings.update(show_vat=True, vat_rate=20, extras={"prices_include_vat": include})
        row = _quote_row(qrig, QUO_1005)
        row["prices_include_vat"] = True if include else None
        lines = [{"description": "A", "amount": 100.01, "is_selected": True},
                 {"description": "B", "amount": 45.0, "is_optional": True, "is_selected": False}]
        preview = db.quote_totals(T1, lines, include=row.get("prices_include_vat") is True, client=qrig.store)
        db.update_quote_lines(QUO_1005, T1, lines, client=qrig.store)
        row = _quote_row(qrig, QUO_1005)
        assert preview == (row["subtotal"], row["vat"], row["total"])


def test_delete_draft_quote_takes_the_row_and_its_lines_together(qrig):
    assert db.delete_draft_quote(QUO_1005, T1, client=qrig.store) is True
    assert _quote_row(qrig, QUO_1005) is None and _qlines(qrig, QUO_1005) == []
    assert _quote_row(qrig, QUO_1003) is not None and _qlines(qrig, QUO_1003)
    assert _quote_row(qrig, QUO_9001) is not None
    assert [w for w in qrig.store.writes if w[1] == "delete"] == [("quotes", "delete", 1)]


@pytest.mark.parametrize("over", [
    {"status": "sent"}, {"status": "accepted"}, {"status": "cancelled"},
    {"sent_at": "2026-10-06T09:00:00+00:00"},        # a sent quote that was put back
    {"viewed_at": "2026-10-06T10:00:00+00:00"},
    {"view_count": 2},
    {"accepted_at": "2026-10-07T09:00:00+00:00"},
    {"declined_at": "2026-10-07T09:00:00+00:00"},
    {"invoice_id": INV_1002},
    {"deposit_invoice_id": INV_1002},
    {"reminders_sent": 1},
], ids=lambda o: next(iter(o)) + "=" + str(next(iter(o.values())))[:12])
def test_delete_draft_quote_refuses_anything_that_ever_left_the_building(qrig, over):
    _quote_row(qrig, QUO_1003).update(over)
    assert db.delete_draft_quote(QUO_1003, T1, client=qrig.store) is False
    assert _quote_row(qrig, QUO_1003) is not None and _qlines(qrig, QUO_1003)


def test_delete_draft_quote_is_tenant_scoped(qrig):
    assert db.delete_draft_quote(QUO_9001, T1, client=qrig.store) is False
    assert _quote_row(qrig, QUO_9001) is not None and _qlines(qrig, QUO_9001)


def test_a_deleted_quote_number_is_never_issued_again(qrig):
    first = db.create_quote(T1, "k@example.test", "K", [{"description": "A", "amount": 10.0}], client=qrig.store)
    second = db.create_quote(T1, "k@example.test", "K", [{"description": "B", "amount": 20.0}], client=qrig.store)
    assert (first["quote_ref"], second["quote_ref"]) == ("QUO-1001", "QUO-1002")
    assert db.delete_draft_quote(second["id"], T1, client=qrig.store) is True
    third = db.create_quote(T1, "k@example.test", "K", [{"description": "C", "amount": 30.0}], client=qrig.store)
    assert third["quote_ref"] == "QUO-1003"                       # not QUO-1002 again


def test_the_quote_words_and_the_delete_guard_read_the_same_facts(qrig):
    assert set(concierge_actions._QUOTE_TOUCHED) == set(db.UNTOUCHED_QUOTE_NULLS) | {"view_count", "reminders_sent"}
    fresh = _quote("x", "QUO-1")
    assert db.quote_is_untouched_draft(fresh) and concierge_actions._quote_touched_clause(fresh) is None
    for col in db.UNTOUCHED_QUOTE_NULLS:
        touched = {**fresh, col: "something"}
        assert not db.quote_is_untouched_draft(touched), col
        assert concierge_actions._quote_touched_clause(touched) == concierge_actions._QUOTE_TOUCHED[col]
    for col in ("view_count", "reminders_sent"):
        touched = {**fresh, col: 1}
        assert not db.quote_is_untouched_draft(touched), col
        assert concierge_actions._quote_touched_clause(touched) == concierge_actions._QUOTE_TOUCHED[col]
    assert not db.quote_is_untouched_draft({**fresh, "status": "sent"})


# ═════════════════════════════════════════════════════════════════════════════
# staging
# ═════════════════════════════════════════════════════════════════════════════
def test_edit_stages_a_confirm_card_that_shows_before_and_after(qrig):
    card = _stage(qrig, {"kind": "quote_edit_draft", "quote_id": "QUO-1003", "amount": "200"})
    assert card["kind"] == "quote_edit_draft" and card["approval"] == "confirm"
    assert card["summary"] == "Change draft quote QUO-1003 for Tom Hale: £252.50 → £200.00"
    shown = " | ".join(card["details"])
    assert "Work as discussed: £252.50 → £200.00" in shown and "Total: £252.50 → £200.00" in shown
    assert "nothing has been sent" in shown.lower() and "draft" in shown.lower()
    saved = qrig.proposals[0]
    assert saved["kind"] == "quote_edit_draft" and saved["payload"]["quote_id"] == QUO_1003
    assert _qlines(qrig, QUO_1003) == [("Work as discussed", 252.5)] and _quote_row(qrig, QUO_1003)["total"] == 252.5
    assert not [w for w in qrig.store.writes if w[0].startswith("quote")]


def test_edit_keeps_the_flags_an_owner_set_and_counts_ticked_lines_only(qrig):
    card = _stage(qrig, {"kind": "quote_edit_draft", "quote_id": QUO_1005,
                         "lines": "Parts | 150\nLabour | 200\nExtras | 100"})
    lines = {l["description"]: l for l in qrig.proposals[0]["payload"]["lines"]}
    assert lines["Parts"]["booking_id"] == BOOKING and lines["Parts"]["kb_entry_id"] == KB
    assert lines["Parts"]["counts_to_deposit_rule"] is True
    assert lines["Extras"]["is_optional"] is True and lines["Extras"]["is_selected"] is False
    assert lines["Labour"]["is_optional"] is False
    assert "Total: £400.00 → £350.00" in " | ".join(card["details"])     # the unticked extra never counts
    assert "Labour: £250.00 → £200.00" in " | ".join(card["details"])


def test_edit_adds_a_required_ticked_line(qrig):
    card = _stage(qrig, {"kind": "quote_edit_draft", "quote_id": QUO_1005,
                         "lines": "Parts | 150\nLabour | 250\nExtras | 100\nTravel | 30"})
    lines = {l["description"]: l for l in qrig.proposals[0]["payload"]["lines"]}
    assert lines["Travel"]["is_optional"] is False and lines["Travel"]["is_selected"] is True
    assert lines["Travel"]["booking_id"] is None and lines["Travel"]["kb_entry_id"] is None
    assert "Add: Travel — £30.00" in " | ".join(card["details"])
    assert "Total: £400.00 → £430.00" in " | ".join(card["details"])


def test_edit_shows_a_removed_line(qrig):
    card = _stage(qrig, {"kind": "quote_edit_draft", "quote_id": QUO_1005, "lines": "Parts | 150\nLabour | 250"})
    assert "Remove: Extras — £100.00" in " | ".join(card["details"])


def test_a_reworded_line_keeps_its_job_link_but_never_inherits_anothers_flags(qrig):
    """A line in the same place under a new name stands in for the old one and keeps its
    link to the job. The owner's optional / ticked flags belong to the line they were
    set on: a NEW line that lands where the unticked extra was does not become unticked."""
    card = _stage(qrig, {"kind": "quote_edit_draft", "quote_id": QUO_1005,
                         "lines": "Parts | 150\nLabour | 250\nTravel | 30"})
    shown = " | ".join(card["details"])
    lines = {l["description"]: l for l in qrig.proposals[0]["payload"]["lines"]}
    assert "Extras (£100.00) → Travel (£30.00)" in shown
    assert lines["Travel"]["is_optional"] is False and lines["Travel"]["is_selected"] is True
    assert "Total: £400.00 → £430.00" in shown                      # Travel counts: it is not the unticked extra
    # ...whereas a reworded Parts keeps the job it bills
    _stage(qrig, {"kind": "quote_edit_draft", "quote_id": QUO_1005,
                  "lines": "Replacement parts | 150\nLabour | 250\nExtras | 100"})
    reworded = {l["description"]: l for l in qrig.proposals[1]["payload"]["lines"]}
    assert reworded["Replacement parts"]["booking_id"] == BOOKING
    assert reworded["Replacement parts"]["kb_entry_id"] is None
    assert not reworded["Replacement parts"].get("counts_to_deposit_rule")


def test_edit_with_only_an_amount_needs_the_full_lines_when_there_are_several(qrig):
    with pytest.raises(concierge_actions.ActionError) as e:
        _stage(qrig, {"kind": "quote_edit_draft", "quote_id": QUO_1005, "amount": "200"})
    assert "3 lines" in str(e.value) and "Labour" in str(e.value)


@pytest.mark.parametrize("action, why", [
    ({"quote_id": QUO_1003}, "lines"),
    ({"quote_id": QUO_1003, "lines": "Work | lots"}, "amount"),
    ({"quote_id": QUO_1003, "lines": "Work | 0"}, "£0.00"),
    ({"quote_id": QUO_1003, "amount": "252.50"}, "nothing to change"),
    ({"quote_id": QUO_1005, "lines": "Parts | 0"}, "£0.00"),
], ids=lambda v: str(v)[:40])
def test_edit_refuses_what_it_cannot_prepare_in_plain_words(qrig, action, why):
    with pytest.raises(concierge_actions.ActionError) as e:
        _stage(qrig, {"kind": "quote_edit_draft", **action})
    assert why in str(e.value) and not qrig.proposals


@pytest.mark.parametrize("status, expect", [
    ("sent", "already sent"), ("accepted", "is accepted"), ("declined", "is declined"),
    ("expired", "is expired"), ("invoiced", "is invoiced"), ("cancelled", "is cancelled")])
@pytest.mark.parametrize("kind", ["quote_edit_draft", "quote_delete_draft"])
def test_a_quote_that_is_not_a_draft_is_refused_in_true_words(qrig, kind, status, expect):
    _quote_row(qrig, QUO_1003)["status"] = status
    did = "edited" if kind == "quote_edit_draft" else "deleted"
    with pytest.raises(concierge_actions.ActionError) as e:
        _stage(qrig, {"kind": kind, "quote_id": "QUO-1003", "amount": "200"})
    text = str(e.value)
    assert "QUO-1003" in text and expect in text and f"can't be {did} from here" in text
    if status == "sent":
        # a sent quote CAN be cancelled from here (quote_cancel): never claim it cannot
        assert "cancel it instead" in text and "isn't available" not in text
    assert not qrig.proposals


def test_a_quote_is_found_by_ref_or_id_and_only_in_this_business(qrig):
    assert _stage(qrig, {"kind": "quote_edit_draft", "quote_id": "quo-1003", "amount": "200"})
    assert qrig.proposals[-1]["payload"]["quote_id"] == QUO_1003
    for kind in QUOTE_KINDS:
        for ref in ("QUO-9001", QUO_9001, "QUO-4040", ""):
            with pytest.raises(concierge_actions.ActionError, match="couldn't"):
                _stage(qrig, {"kind": kind, "quote_id": ref, "amount": "200"})
    assert _qlines(qrig, QUO_9001) == [("Work as discussed", 252.5)]


def test_delete_card_says_exactly_what_goes(qrig):
    card = _stage(qrig, {"kind": "quote_delete_draft", "quote_id": "QUO-1003"})
    assert card["kind"] == "quote_delete_draft" and card["approval"] == "confirm"
    assert card["summary"] + ". " + " ".join(card["details"]) == (
        "Delete draft QUO-1003 (£252.50 for Tom Hale). "
        "Nothing has been sent to the customer. It can't be brought back.")
    saved = qrig.proposals[0]["payload"]
    assert saved["quote_id"] == QUO_1003 and saved["total"] == 252.5
    assert saved["lines"] == [{"description": "Work as discussed", "amount": 252.5}]
    assert _quote_row(qrig, QUO_1003) is not None


@pytest.mark.parametrize("over, why", [
    ({"sent_at": "2026-10-06T09:00:00+00:00"}, "sent"),
    ({"viewed_at": "2026-10-06T10:00:00+00:00"}, "opened by the customer"),
    ({"view_count": 3}, "opened by the customer"),
    ({"accepted_at": "2026-10-07T09:00:00+00:00"}, "accepted"),
    ({"declined_at": "2026-10-07T09:00:00+00:00"}, "declined"),
    ({"invoice_id": INV_1002}, "invoice"),
    ({"deposit_invoice_id": INV_1002}, "invoice"),
    ({"reminders_sent": 1}, "follow-up"),
])
def test_delete_refuses_a_draft_that_ever_left_the_building(qrig, over, why):
    _quote_row(qrig, QUO_1003).update(over)
    with pytest.raises(concierge_actions.ActionError) as e:
        _stage(qrig, {"kind": "quote_delete_draft", "quote_id": QUO_1003})
    assert why in str(e.value) and "can't be deleted from here" in str(e.value) and not qrig.proposals


# ═════════════════════════════════════════════════════════════════════════════
# the door and the card
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("kind", QUOTE_KINDS)
def test_staff_without_the_money_tick_cannot_stage_a_quote_change(qrig, kind):
    action = {"kind": kind, "quote_id": QUO_1003, "amount": "200"}
    for who in (_person(pages=("today", "diary")), _person(can=()),
                _person(role="accountant", pages=("money",))):
        with pytest.raises(concierge_actions.ActionError) as e:
            _stage(qrig, action, member=who)
        assert "permission" in str(e.value)
    assert not qrig.proposals
    for who in (_person(can=("money:send",)), _person(role="owner", pages=people.PAGES),
                _person(role="manager", pages=people.PAGES), None):
        assert _stage(qrig, action, member=who)["kind"] == kind


# ═════════════════════════════════════════════════════════════════════════════
# running them (only after the owner's Confirm)
# ═════════════════════════════════════════════════════════════════════════════
def _edit_payload(rig, quote_id=QUO_1003, **action):
    _stage(rig, {"kind": "quote_edit_draft", "quote_id": quote_id, **(action or {"amount": "200"})})
    return rig.proposals[-1]["payload"]


def _delete_payload(rig, quote_id=QUO_1003):
    _stage(rig, {"kind": "quote_delete_draft", "quote_id": quote_id})
    return rig.proposals[-1]["payload"]


def test_confirming_an_edit_changes_exactly_that_draft(qrig):
    payload = _edit_payload(qrig, QUO_1005, lines="Parts | 150\nLabour | 200\nExtras | 100")
    out = _run(qrig, "quote_edit_draft", payload)
    assert out["summary"] == "Done — quote QUO-1005 now totals £350.00 (it was £400.00). It is still a draft."
    assert out["link"] == {"label": "Open the quote", "href": f"/app/quotes/{QUO_1005}"}
    assert out.get("state") is None
    assert _qlines(qrig, QUO_1005) == [("Extras", 100.0), ("Labour", 200.0), ("Parts", 150.0)]
    assert _quote_row(qrig, QUO_1005)["total"] == 350.0 and _quote_row(qrig, QUO_1005)["status"] == "draft"
    assert _quote_row(qrig, QUO_1005)["version"] == 1
    rows = _qline_rows(qrig, QUO_1005)
    assert rows["Parts"]["booking_id"] == BOOKING and rows["Extras"]["is_selected"] is False
    assert _qlines(qrig, QUO_1003) == [("Work as discussed", 252.5)]            # the other draft is untouched
    assert [e["kind"] for e in qrig.events] == ["quote_edited"]
    assert "QUO-1005" in qrig.events[0]["summary"] and "confirmed by you" in qrig.events[0]["summary"]
    assert qrig.events[0]["contact"] == "priya@example.test"


@pytest.mark.parametrize("status", ["sent", "accepted", "cancelled"])
def test_confirming_after_it_was_sent_in_the_meantime_refuses_and_changes_nothing(qrig, status):
    payload = _edit_payload(qrig)
    _quote_row(qrig, QUO_1003)["status"] = status
    before = copy.deepcopy(qrig.store.s)
    out = _run(qrig, "quote_edit_draft", payload)
    assert out["state"] == "failed" and "no longer a draft" in out["summary"]
    assert "nothing was changed" in out["summary"].lower()
    assert qrig.store.s == before and not qrig.events


def test_confirming_after_the_lines_were_changed_elsewhere_refuses(qrig):
    payload = _edit_payload(qrig)
    qrig.store.s["quote_lines"][0]["amount"] = 300.0
    out = _run(qrig, "quote_edit_draft", payload)
    assert out["state"] == "failed" and "changed since" in out["summary"] and not qrig.events
    assert qrig.store.s["quote_lines"][0]["amount"] == 300.0


def test_the_same_quote_edit_run_twice_applies_once(qrig):
    payload = _edit_payload(qrig)
    first = _run(qrig, "quote_edit_draft", payload)
    second = _run(qrig, "quote_edit_draft", payload)
    assert first.get("state") is None and second["state"] == "failed"
    assert _qlines(qrig, QUO_1003) == [("Work as discussed", 200.0)]
    assert [e["kind"] for e in qrig.events] == ["quote_edited"]


def test_a_send_landing_between_the_recheck_and_the_write_is_stopped_by_the_database(qrig, monkeypatch):
    payload = _edit_payload(qrig)
    stale = dict(_quote_row(qrig, QUO_1003))
    _quote_row(qrig, QUO_1003)["status"] = "sent"
    monkeypatch.setattr(concierge_actions.db, "get_quote", lambda *a, **k: stale)
    before = copy.deepcopy(qrig.store.s)
    out = _run(qrig, "quote_edit_draft", payload)
    assert out["state"] == "failed" and "no longer a draft" in out["summary"]
    assert qrig.store.s == before and not qrig.events


def test_a_quote_edit_payload_aimed_at_another_business_cannot_write(qrig):
    payload = {"quote_id": QUO_9001, "ref": "QUO-9001", "who": "Someone Else", "total_before": 252.5,
               "total_after": 1.0, "before": [{"description": "Work as discussed", "amount": 252.5}],
               "lines": [{"description": "X", "amount": 1.0}]}
    out = _run(qrig, "quote_edit_draft", payload, tenant=T1)
    assert out["state"] == "failed"
    assert _qlines(qrig, QUO_9001) == [("Work as discussed", 252.5)] and _quote_row(qrig, QUO_9001)["total"] == 252.5


def test_confirming_a_delete_removes_exactly_that_draft_and_records_it(qrig):
    payload = _delete_payload(qrig, QUO_1005)
    out = _run(qrig, "quote_delete_draft", payload)
    assert out["summary"] == ("Done — draft quote QUO-1005 (£400.00 for Tom Hale) is deleted. "
                              "It can't be brought back.")
    assert out["link"] == {"label": "Open your quotes", "href": "/app/quotes"}
    assert _quote_row(qrig, QUO_1005) is None and _qlines(qrig, QUO_1005) == []
    assert _quote_row(qrig, QUO_1003) is not None and _qlines(qrig, QUO_1003)
    assert _quote_row(qrig, QUO_9001) is not None
    event = qrig.events[0]
    assert event["kind"] == "quote_deleted" and "QUO-1005" in event["summary"]
    assert "£400.00" in event["summary"] and "confirmed by you" in event["summary"]


def test_the_same_quote_delete_run_twice_deletes_once(qrig):
    payload = _delete_payload(qrig)
    first = _run(qrig, "quote_delete_draft", payload)
    second = _run(qrig, "quote_delete_draft", payload)
    assert first.get("state") is None
    assert second["state"] == "failed" and "isn't there any more" in second["summary"]
    assert len(qrig.events) == 1
    assert [w for w in qrig.store.writes if w[1] == "delete"] == [("quotes", "delete", 1)]


@pytest.mark.parametrize("over, why", [
    ({"status": "sent"}, "no longer a draft"),
    ({"status": "accepted"}, "no longer a draft"),
    ({"sent_at": "2026-10-06T09:00:00+00:00"}, "sent"),
    ({"viewed_at": "2026-10-06T10:00:00+00:00"}, "opened by the customer"),
    ({"invoice_id": INV_1002}, "invoice"),
    ({"total": 300.0}, "changed since"),
])
def test_confirming_a_delete_after_it_changed_refuses_and_keeps_the_draft(qrig, over, why):
    payload = _delete_payload(qrig)
    _quote_row(qrig, QUO_1003).update(over)
    out = _run(qrig, "quote_delete_draft", payload)
    assert out["state"] == "failed" and why in out["summary"]
    assert "nothing was deleted" in out["summary"].lower()
    assert _quote_row(qrig, QUO_1003) is not None and _qlines(qrig, QUO_1003) and not qrig.events


def test_a_send_landing_between_the_recheck_and_the_delete_is_stopped_by_the_database(qrig, monkeypatch):
    payload = _delete_payload(qrig)
    stale = dict(_quote_row(qrig, QUO_1003))
    _quote_row(qrig, QUO_1003).update(status="sent", sent_at="2026-10-15T10:00:00+00:00")
    monkeypatch.setattr(concierge_actions.db, "get_quote", lambda *a, **k: stale)
    out = _run(qrig, "quote_delete_draft", payload)
    assert out["state"] == "failed" and "nothing was deleted" in out["summary"]
    assert _quote_row(qrig, QUO_1003) is not None and _qlines(qrig, QUO_1003) and not qrig.events


def test_a_quote_delete_payload_aimed_at_another_business_cannot_delete(qrig):
    payload = {"quote_id": QUO_9001, "ref": "QUO-9001", "who": "Someone Else", "total": 252.5, "lines": []}
    out = _run(qrig, "quote_delete_draft", payload, tenant=T1)
    assert out["state"] == "failed"
    assert _quote_row(qrig, QUO_9001) is not None and _qlines(qrig, QUO_9001)


# ═════════════════════════════════════════════════════════════════════════════
# the model can see what it is editing
# ═════════════════════════════════════════════════════════════════════════════
def test_list_quotes_hands_the_model_a_drafts_lines_and_nobody_elses(qrig, monkeypatch):
    qrig.store.s["quotes"] = [q for q in qrig.store.s["quotes"] if q["tenant_id"] == T1]
    _quote_row(qrig, QUO_1003)["status"] = "sent"
    monkeypatch.setattr(db, "list_quotes", lambda tenant_id, client=None: qrig.store.s["quotes"])
    out = concierge_data._list_quotes(T1, qrig.store, {})
    by_ref = {q["ref"]: q for q in out["quotes"]}
    assert [(l["description"], l["amount"]) for l in by_ref["QUO-1005"]["lines"]] == \
        [("Parts", 150.0), ("Labour", 250.0), ("Extras", 100.0)]                  # in the quote's own order
    extras = by_ref["QUO-1005"]["lines"][2]
    assert extras.get("optional") is True and "optional" not in by_ref["QUO-1005"]["lines"][0]
    assert "lines" not in by_ref["QUO-1003"]                                     # a sent quote's are not offered


def test_a_bulk_card_can_carry_a_quote_delete_and_runs_it_as_one_of_its_items(qrig):
    card = _stage(qrig, {"kind": "bulk", "items": [
        {"kind": "quote_delete_draft", "quote_id": QUO_1003},
        {"kind": "invoice_delete_draft", "invoice_id": INV_1002}]})
    assert card["kind"] == "bulk" and card["approval"] == "confirm" and [i["ok"] for i in card["items"]] == [True, True]
    payload = {"items": qrig.proposals[0]["payload"]["items"]}
    out = asyncio.run(concierge_actions.execute(T1, qrig.store, "bulk", payload))
    assert [(r["ok"], r["state"]) for r in out["results"]] == [(True, "completed"), (True, "completed")]
    assert _quote_row(qrig, QUO_1003) is None and qrig.store.s["invoices"][0]["id"] != INV_1002
