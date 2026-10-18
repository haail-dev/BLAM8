"""Changing a stay's dates — extend, shorten, move — one plan and one apply
for the job page's form and the Concierge's change_stay card
(docs/plan-stay-date-changes.md; Kris 2026-09-23: "Let's build and test
extending and shortening stays").
"""
import asyncio
from datetime import datetime

import pytest

from app import concierge, concierge_actions as ca, record_edits as re_, slotgrid

# Bella: Mon 12 Oct 08:30 → Mon 26 Oct 17:00 (14 nights), £30.50 a night, invoiced.
BELLA = {"id": "b-bella", "booking_ref": "BK-1038", "status": "confirmed", "booking_date": "2026-10-12",
         "start_block": slotgrid.block_at(datetime(2026, 10, 12, 8, 30).time()),
         "length_blocks": slotgrid.datetime_to_abs(datetime(2026, 10, 26, 17, 0)) - slotgrid.datetime_to_abs(datetime(2026, 10, 12, 8, 30)),
         "contact": "hannah@x.com", "summary": "Boarding for Bella", "quoted_amount": 427.0,
         "details": {"Drop-off date and time": "Monday 12 October 2026, 08:30",
                     "Collection date and time": "Monday 26 October 2026, 17:00"}}
CFG = {"capacity_per_window": 2, "stay_pricing": {"rate": 30.5, "rate_unit": "night"},
       "day_hours": {"checkout_before": "18:00"}}


class _Now(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 9, 24, 12, 0, tzinfo=tz)


def _stub(monkeypatch, *, others=(), invoiced=False, subjects=None, groups=None):
    monkeypatch.setattr(re_, "datetime", _Now)
    monkeypatch.setattr(re_.db, "get_scheduling_config", lambda t, client=None: carried_config(CFG))
    monkeypatch.setattr(re_.db, "list_active_bookings", lambda t, a, b, client=None: [BELLA, *others])
    monkeypatch.setattr(re_.db, "invoiced_booking_ids", lambda t, ids, client=None: {"b-bella"} if invoiced else set())
    monkeypatch.setattr(re_.db, "subjects_for_bookings", lambda t, ids, client=None: {"b-bella": subjects or []})
    monkeypatch.setattr(re_.db, "list_requirement_groups", lambda t, client=None: groups or [])


def _other(start: datetime, end: datetime):
    return {"id": "b-other", "status": "confirmed", "booking_date": start.date().isoformat(),
            "start_block": slotgrid.block_at(start.time()),
            "length_blocks": slotgrid.datetime_to_abs(end) - slotgrid.datetime_to_abs(start)}


def test_extend_by_two_nights(monkeypatch):
    _stub(monkeypatch)
    p = re_.plan_stay_change("t1", BELLA, new_departure="2026-10-28")
    assert p["kind"] == "extend" and p["nights"] == 16 and p["delta_nights"] == 2
    assert p["when"] == "12th Oct 26 08:30 → 28th Oct 26 17:00"        # the collection time is kept
    assert p["price_old"] == 427.0 and p["price_new"] == 488.0 and p["extra_amount"] is None


def test_shorten_and_move(monkeypatch):
    _stub(monkeypatch)
    p = re_.plan_stay_change("t1", BELLA, new_departure="2026-10-20T16:00")
    assert p["kind"] == "shorten" and p["nights"] == 8 and p["departure"] == "2026-10-20T16:00"
    p = re_.plan_stay_change("t1", BELLA, new_arrival="2026-10-19", new_departure="2026-11-02")
    assert p["kind"] == "move" and p["nights"] == 14 and p["price_new"] == 427.0


def test_an_invoiced_stay_that_grows_prices_the_extra_nights(monkeypatch):
    _stub(monkeypatch, invoiced=True)
    p = re_.plan_stay_change("t1", BELLA, new_departure="2026-10-28")
    assert p["invoiced"] is True and p["extra_amount"] == 61.0
    p = re_.plan_stay_change("t1", BELLA, new_departure="2026-10-20")
    assert p["invoiced"] is True and p["extra_amount"] is None       # no automatic refund


def test_a_full_night_is_named_and_the_stays_own_row_never_blocks_it(monkeypatch):
    # Two spaces; two other dogs in on the 27th → no room for Bella's extra night.
    a = _other(datetime(2026, 10, 27, 8, 0), datetime(2026, 10, 29, 17, 0))
    b = dict(_other(datetime(2026, 10, 26, 8, 0), datetime(2026, 10, 30, 17, 0)), id="b-third")
    _stub(monkeypatch, others=[a, b])
    with pytest.raises(re_.EditError, match="no room for every night .* the night of 27th Oct 26 is full"):
        re_.plan_stay_change("t1", BELLA, new_departure="2026-10-28")
    # Bella's own 14 nights are in `booked` too — they must not count against her.
    _stub(monkeypatch, others=[dict(_other(datetime(2026, 10, 12, 8, 0), datetime(2026, 10, 26, 17, 0)), id="b-x")])
    assert re_.plan_stay_change("t1", BELLA, new_departure="2026-10-27")["nights"] == 15


@pytest.mark.parametrize("kw,says", [
    ({"new_departure": "2026-10-26T17:00"}, "how the stay already is"),
    ({"new_departure": "2026-10-12T08:00"}, "collection has to come after"),
    ({"new_departure": "2026-10-27T19:00"}, "Check-out is by 18:00"),
    ({"new_departure": "2026-09-01"}, "already passed"),
    ({"new_departure": "next week"}, "isn't a date I can place"),
])
def test_what_the_plan_refuses(monkeypatch, kw, says):
    _stub(monkeypatch)
    with pytest.raises(re_.EditError, match=says):
        re_.plan_stay_change("t1", BELLA, **kw)


def test_a_jab_that_runs_out_before_the_new_collection_blocks_it(monkeypatch):
    dog = {"id": "g", "label": "Dog", "fields": [
        {"key": "vaccinations", "label": "Vaccinations", "type": "date", "durability": "expires", "gate": True}]}
    bella = {"id": "i-bella", "group_id": "g", "name": "Bella", "data": {"vaccinations": "2026-10-27"}}
    _stub(monkeypatch, subjects=[bella], groups=[dog])
    with pytest.raises(re_.EditError, match="Bella can't be booked to 28th Oct 26 — Vaccinations"):
        re_.plan_stay_change("t1", BELLA, new_departure="2026-10-28")
    assert re_.plan_stay_change("t1", BELLA, new_departure="2026-10-27")["nights"] == 15


def test_a_move_refuses_a_stay():
    with pytest.raises(re_.EditError, match="That's a stay"):
        re_.plan_booking_move("t1", BELLA, "2026-10-13", "day")


def test_apply_writes_the_span_the_words_the_price_and_the_lines(monkeypatch):
    calls = {}
    monkeypatch.setattr(re_.db, "move_booking_in_place",
                        lambda bid, t, d, sb, lb, w, client=None: calls.update(span=(d, sb, lb, w)) or {"status": "confirmed"})
    monkeypatch.setattr(re_.db, "update_booking_details", lambda bid, t, extra, client=None: calls.update(details=extra))
    monkeypatch.setattr(re_.db, "set_booking_quote", lambda bid, t, amount, client=None: calls.update(quote=amount))
    monkeypatch.setattr(re_.db, "create_invoice", lambda *a, **k: calls.update(invoice=(a, k)) or {"invoice_ref": "INV-1099"})
    monkeypatch.setattr(re_.db, "log_agent_event", lambda t, kind, summary, **k: calls.update(event=(kind, summary)))
    monkeypatch.setattr(re_.db, "crm_contact_by_email", lambda t, e, client=None: {"id": "c-h"})
    monkeypatch.setattr(re_, "_stamp", lambda t, cid, subj, line: calls.update(stamp=(subj, line)))
    from app import calendar_sync

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(calendar_sync, "remove_pushed_event", _noop)
    monkeypatch.setattr(calendar_sync, "push_bookings", _noop)
    _stub(monkeypatch)
    plan = re_.plan_stay_change("t1", BELLA, new_departure="2026-10-28")
    done = asyncio.run(re_.apply_stay_change("t1", BELLA, plan, via="the Concierge"))
    assert calls["span"][0] == "2026-10-12" and calls["span"][2] == plan["length_blocks"] and calls["span"][3] == "day"
    assert calls["details"] == {"Drop-off date and time": "Monday 12 October 2026, 08:30",
                                "Collection date and time": "Wednesday 28 October 2026, 17:00"}
    assert calls["quote"] == 488.0 and "invoice" not in calls
    assert calls["event"][0] == "stay_changed" and "extended BK-1038" in calls["event"][1]
    assert calls["stamp"] == ("Stay extended", "BK-1038: 12th Oct 26 08:30 → 26th Oct 26 17:00 → "
                                                "12th Oct 26 08:30 → 28th Oct 26 17:00 — by you, from the Concierge")
    assert done == "Done — Boarding for Bella now runs 12th Oct 26 08:30 → 28th Oct 26 17:00 (16 nights). Price £427.00 → £488.00."
    # invoiced + grown: a draft invoice for the extra nights; the stay's own
    # price follows its nights all the same (test run 3.1, 2026-09-24)
    calls.clear()
    _stub(monkeypatch, invoiced=True)
    plan = re_.plan_stay_change("t1", BELLA, new_departure="2026-10-28")
    done = asyncio.run(re_.apply_stay_change("t1", BELLA, plan))
    assert calls["quote"] == 488.0 and calls["invoice"][0][5][0]["amount"] == 61.0
    assert done.endswith("A draft invoice for the extra £61.00 is ready (INV-1099).")
    # …and shortened back: the extra-night draft is cancelled, nothing to refund by hand (3.2)
    calls.clear()
    cancelled = []
    monkeypatch.setattr(re_.db, "extra_night_drafts", lambda t, bid, client=None: [
        {"id": "inv-x", "invoice_ref": "INV-1099", "notes": "The stay (BK-1038) was extended by 2 night(s); this covers the extra nights only."}])
    monkeypatch.setattr(re_.db, "set_invoice_status", lambda iid, t, status, client=None: cancelled.append((iid, status)))
    longer = dict(BELLA, length_blocks=plan["length_blocks"])
    back = re_.plan_stay_change("t1", longer, new_departure="2026-10-26")
    done = asyncio.run(re_.apply_stay_change("t1", longer, back))
    assert cancelled == [("inv-x", "cancelled")]
    assert done.endswith("The extra-night draft invoice INV-1099 is cancelled.")
    assert "by hand" not in done


# ── the Concierge card ──────────────────────────────────────────────────────
def test_the_card_finds_the_stay_by_the_pets_name_and_shows_the_change(monkeypatch):
    _stub(monkeypatch, invoiced=True)
    monkeypatch.setattr(ca, "_find_dependant", lambda t, c, name, who="": (
        {"id": "i-bella", "name": "Bella"}, {"id": "g"}, {"id": "c-h", "name": "Hannah", "email": "hannah@x.com"}))
    trimmed = {k: v for k, v in BELLA.items() if k not in ("quoted_amount", "details")}
    monkeypatch.setattr(ca.db, "bookings_for_contact", lambda t, e, client=None: [trimmed])
    monkeypatch.setattr(ca.db, "get_booking", lambda bid, t, client=None: BELLA)   # the full row, priced
    monkeypatch.setattr(ca.db, "subjects_for_bookings", lambda t, ids, client=None: {"b-bella": [{"id": "i-bella"}]})
    monkeypatch.setattr(ca, "datetime", _Now)
    summary, payload, details, _ = ca._stage_change_stay("t1", None, {"name": "Bella", "end_date": "2026-10-28", "notify": "yes"})
    assert summary == "Extend Boarding for Bella (BK-1038): 12th Oct 26 08:30 → 26th Oct 26 17:00 → 12th Oct 26 08:30 → 28th Oct 26 17:00"
    assert details[1] == "Nights: +2 nights, 16 in all"
    assert details[2] == "Room: every night fits"
    assert "already invoiced, so a draft invoice for the extra £61.00" in details[3]
    assert details[-1].startswith("Customer: a short note goes to your approval queue")
    assert payload["booking_id"] == "b-bella" and payload["departure"] == "2026-10-28T17:00" and payload["notify"] is True


def test_the_card_needs_a_change_and_says_when_it_cant(monkeypatch):
    monkeypatch.setattr(ca.db, "get_booking_by_ref", lambda t, ref, client=None: BELLA)
    with pytest.raises(ca.ActionError, match="What should change"):
        ca._stage_change_stay("t1", None, {"booking_id": "BK-1038"})
    _stub(monkeypatch)
    with pytest.raises(ca.ActionError, match="already passed"):
        ca._stage_change_stay("t1", None, {"booking_id": "BK-1038", "end_date": "2026-09-01"})


def test_confirm_replans_against_the_diary_now(monkeypatch):
    seen = {}
    monkeypatch.setattr(ca.db, "get_booking", lambda bid, t, client=None: BELLA)
    monkeypatch.setattr(re_, "plan_stay_change",
                        lambda t, b, new_arrival=None, new_departure=None, client=None:
                        seen.update(arr=new_arrival, dep=new_departure) or {"new_date": "2026-10-12", "when": "x", "nights": 16, "old_when": "y"})

    async def apply(t, b, plan, via=None, client=None):
        seen["via"] = via
        return "Done — extended."
    monkeypatch.setattr(re_, "apply_stay_change", apply)
    out = asyncio.run(ca._exec_change_stay("t1", None, {"booking_id": "b-bella", "arrival": "2026-10-12T08:30",
                                                         "departure": "2026-10-28T17:00", "notify": False}, None))
    assert seen == {"arr": "2026-10-12T08:30", "dep": "2026-10-28T17:00", "via": "the Concierge"}
    assert out["summary"] == "Done — extended." and out["link"]["href"] == "/app/diary?date=2026-10-12"


def test_change_stay_is_offered_with_the_diary():
    assert "change_stay" in concierge.BUSINESS_ACTION_KINDS
    assert "change_stay" in ca.allowed_kinds(full_qa=False, scheduling_on=True)
    assert "change_stay" not in ca.allowed_kinds(full_qa=True, scheduling_on=False)
    for full in (True, False):
        p = concierge.build_business_prompt(business_name="Little Paws", today="x", full_qa=full)
        assert "change_stay (a STAY's dates" in p and "NEVER move_booking on a stay" in p
    tpl = open("src/app/templates/app/diary_job.html", encoding="utf-8").read()
    assert 'name="departure"' in tpl and "Change the dates or rename" in tpl
    src = open("src/app/dashboard.py", encoding="utf-8").read()
    assert "record_edits.plan_stay_change(ctx.tenant_id, b, new_arrival=arrival or None" in src

from tests.booking_fixtures import carried_config
