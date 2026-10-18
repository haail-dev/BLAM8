"""The draft says what goes out with it (app/deposits.preview_for_log; Kris
2026-09-24: Sophie's confirmation carried a £7.63 deposit invoice that the
draft he approved never showed)."""
from app import db, deposits, payments


def _rig(monkeypatch, *, can_pay=True, on=True, invoiced=()):
    monkeypatch.setattr(db, "get_invoice_settings", lambda tid, client=None: {
        "default_deposit_percent": 25, "extras": {"deposit_on_confirm": on}})
    monkeypatch.setattr(db, "bookings_for_log", lambda lid, tid, client=None: [
        {"id": "b1", "booking_ref": "BK-1072", "status": "proposed", "quoted_amount": None},
        {"id": "b2", "booking_ref": "BK-1073", "status": "proposed", "quoted_amount": 30.50}])
    monkeypatch.setattr(db, "invoiced_booking_ids", lambda tid, ids, client=None: list(invoiced))
    monkeypatch.setattr(payments, "tenant_can_take_payments", lambda tid, client=None: can_pay)
    created = []
    monkeypatch.setattr(db, "create_invoice", lambda *a, **k: created.append(a) or {"id": "x"})
    monkeypatch.setattr(db, "log_agent_event", lambda *a, **k: created.append(("event", a)))
    return created


def test_the_draft_shows_the_deposit_that_will_ride_along_and_creates_nothing(monkeypatch):
    created = _rig(monkeypatch)
    got = deposits.preview_for_log("t1", "L1", "sophie@example.com")
    assert got == {"total": 7.63, "refs": "BK-1073"}      # 25% of £30.50, the meet & greet has no price
    assert created == []                                   # a preview never raises the invoice


def test_nothing_shown_when_nothing_would_go(monkeypatch):
    _rig(monkeypatch, on=False)
    assert deposits.preview_for_log("t1", "L1", "s@example.com") is None
    _rig(monkeypatch, invoiced=("b2",))
    assert deposits.preview_for_log("t1", "L1", "s@example.com") is None
    monkeypatch.setattr(deposits.invoice_config, "bank_details", lambda s: "")
    _rig(monkeypatch, can_pay=False)
    assert deposits.preview_for_log("t1", "L1", "s@example.com") is None


def test_the_thread_card_carries_it():
    tpl = open("src/app/templates/app/_thread.html", encoding="utf-8").read()
    assert "data-sends-with" in tpl and "Sends with a deposit invoice" in tpl
