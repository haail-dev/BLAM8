"""A visit the business's own rules make free (a return visit under its guarantee) carries no price.

Live 2026-09-30 (long-form rig run): a customer reported a fault with the shower the business had just fitted; the reply
correctly said "no charge, it's covered by our 12-month guarantee" but the hold was stamped with the service's 85 GBP
callout price and the quote auto-draft then minted two stray 85 GBP quotes. The hold/offer tools now take `no_charge`,
which the agent sets only when the business's rules say so.
"""
from datetime import datetime

from tests.booking_fixtures import carried_config

CFG = carried_config({
    "service_types": [{"name": "Repair visit", "minutes": 60, "price": 85}],
    "capacity_per_window": 4, "working_days": "mon,tue,wed,thu,fri", "open_time": "08:00", "close_time": "17:00",
    "slot_minutes": 30, "min_notice_hours": 0, "horizon_days": 60})


def _executor(monkeypatch, offered=None):
    from app import db, scheduling
    monkeypatch.setattr(db, "get_scheduling_config", lambda *a, **k: carried_config(CFG))
    monkeypatch.setattr(db, "list_active_bookings", lambda *a, **k: [])
    monkeypatch.setattr(db, "list_requirement_instances", lambda *a, **k: [])
    monkeypatch.setattr(db, "crm_contact_by_email", lambda *a, **k: {"id": "c1", "email": "ruth@x.co"})
    monkeypatch.setattr(db, "release_offered_for_contact", lambda *a, **k: None)
    monkeypatch.setattr(db, "offered_bookings_for_contact", lambda *a, **k: list(offered or []))
    monkeypatch.setattr(db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(db, "bookings_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(db, "crm_list_field_definitions", lambda *a, **k: [])
    quoted, details, placed = {}, {}, []
    monkeypatch.setattr(db, "set_booking_quote", lambda bid, t, amt, client=None: quoted.__setitem__(bid, amt))
    monkeypatch.setattr(db, "update_booking_details",
                        lambda bid, t, extra, client=None: details.setdefault(bid, {}).update(extra))

    def fake_insert(tenant_id, day, sb, length, summary, **k):
        row = {"id": f"new{len(placed) + 1}", "booking_ref": f"BK-{1040 + len(placed)}", "booking_date": day,
               "window": k.get("window"), "status": k.get("status") or "proposed", "details": k.get("details"),
               "expires_at": k.get("expires_at")}
        placed.append(row)
        return row
    monkeypatch.setattr(db, "insert_stay_booking", fake_insert)
    execute, state = scheduling.make_executor("t1", contact="ruth@x.co", service_types=CFG["service_types"],
                                              now=datetime(2026, 10, 12, 9, 0, tzinfo=scheduling.DIARY_TZ))
    return execute, quoted, details, placed


def test_a_normal_hold_carries_the_service_price(monkeypatch):
    execute, quoted, details, placed = _executor(monkeypatch)
    out = execute("hold_booking", {"date": "2026-10-14", "window": "09:00", "service": "Repair visit", "summary": "Repair"})
    assert out["held"] and out["price"] == 85
    assert quoted == {"new1": 85.0} and details == {}


def test_a_no_charge_hold_is_stamped_free_and_told_so(monkeypatch):
    execute, quoted, details, placed = _executor(monkeypatch)
    out = execute("hold_booking", {"date": "2026-10-14", "window": "09:00", "service": "Repair visit",
                                   "summary": "Return visit: shower screen seal", "no_charge": True})
    assert out["held"] and out["price"] == 0 and "NO CHARGE" in out["message"]
    assert quoted["new1"] == 0.0                                    # the last stamp wins: free, not 85
    assert details["new1"] == {"Charge": "No charge"}


def test_a_no_charge_offer_is_marked_and_priced_nothing(monkeypatch):
    execute, quoted, details, placed = _executor(monkeypatch)
    out = execute("offer_slots", {"slots": [{"date": "2026-10-14", "window": "09:00"},
                                            {"date": "2026-10-14", "window": "13:00"}],
                                  "service": "Repair visit", "summary": "Return visit", "no_charge": True})
    assert out.get("price") == 0 and "NO CHARGE" in (out.get("message") or "")
    assert placed and all(r["status"] == "offered" for r in placed)
    assert all(details[r["id"]] == {"Charge": "No charge"} for r in placed)
    assert quoted == {}                                              # offers carry no price until accepted


def test_accepting_a_no_charge_offer_keeps_it_free(monkeypatch):
    from app import db
    offered = [{"id": "off1", "booking_date": "2026-10-14", "window": "09:00", "status": "offered",
                "details": {"Service": "Repair visit", "Charge": "No charge"}, "booking_ref": "BK-1090"}]
    execute, quoted, details, placed = _executor(monkeypatch, offered=offered)
    monkeypatch.setattr(db, "accept_offered_booking", lambda bid, t, details=None, **k: {
        "id": bid, "booking_ref": "BK-1090", "booking_date": "2026-10-14", "window": "09:00", "status": "proposed",
        "details": details})
    out = execute("hold_booking", {"date": "2026-10-14", "window": "09:00", "service": "Repair visit", "summary": "Return visit"})
    assert out.get("held") or out.get("accepted_offer") or "error" not in out
    assert quoted.get("off1") == 0.0                                 # not the 85 GBP service price


def test_the_tools_describe_the_flag_without_naming_a_trade():
    from app import scheduling
    for name in ("hold_booking", "offer_slots"):
        tool = next(t for t in scheduling.SERVICE_TOOLS if t["name"] == name)
        prop = tool["input_schema"]["properties"]["no_charge"]
        assert prop["type"] == "boolean" and "no rule, no flag" in prop["description"]
