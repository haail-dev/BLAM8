"""Unlink a dependant from a booking and there is a way back — one tap.

Kris, 2026-09-21: "When in the Dependants, you can click 'Unlink', there's no
way obvious to undo it if it was a mistake, i think we built something for
this case."

We had: the job page's "Link to this booking" dropdown puts any of the
customer's dependants back. It just wasn't offered at the moment of the
mistake. Unlink now lands on the page with a notice naming what went and an
Undo button — the same POST the dropdown makes — that holds until tapped
rather than fading mid-read.

Run: python -m pytest tests/test_unlink_undo.py -q
"""
from pathlib import Path

from tests.test_requirement_groups import HORSE, client  # noqa: F401  (the fixture)

ROOT = Path(__file__).resolve().parents[1]
BOOKING = {"id": "7b1e0000-0000-4000-8000-000000000001", "contact": "kris@example.com", "summary": "Shoeing",
           "booking_date": "2026-09-18", "window": "am", "status": "confirmed", "booking_ref": "BK-9"}
SAID = "Tilly unlinked from this booking."


def _wire(monkeypatch):
    import app.dashboard as dashboard
    monkeypatch.setattr(dashboard.db, "get_booking", lambda *a, **k: dict(BOOKING))
    monkeypatch.setattr(dashboard.db, "list_job_parties", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "list_requirement_groups", lambda t, client=None: [dict(HORSE)])
    monkeypatch.setattr(dashboard.db, "subjects_for_bookings", lambda t, ids, client=None: {
        BOOKING["id"]: [{"id": "i-murphy", "group_id": "g-horse", "name": "Murphy", "data": {}}]})
    monkeypatch.setattr(dashboard.db, "crm_contact_by_email", lambda *a, **k: {"id": "c-1"})
    monkeypatch.setattr(dashboard.db, "list_requirement_instances", lambda *a, **k: [
        {"id": "i-murphy", "group_id": "g-horse", "name": "Murphy", "data": {}},
        {"id": "i-tilly", "group_id": "g-horse", "name": "Tilly", "data": {}}])
    return dashboard


def test_unlink_lands_on_the_page_naming_what_went(client, monkeypatch):
    dashboard = _wire(monkeypatch)
    gone = []
    monkeypatch.setattr(dashboard.db, "unlink_booking_subject",
                        lambda t, b, i, client=None: gone.append((b, i)))
    r = client.post(f"/app/diary/{BOOKING['id']}/subjects/i-murphy/unlink")
    assert r.status_code == 303
    assert r.headers["location"] == f"/app/diary/{BOOKING['id']}?msg=unlinked&undo=i-murphy"
    assert gone == [(BOOKING["id"], "i-murphy")]


def test_the_notice_offers_the_one_tap_back_and_holds_until_tapped(client, monkeypatch):
    _wire(monkeypatch)
    # Tilly is the customer's and not on the booking — exactly what an unlink leaves.
    r = client.get(f"/app/diary/{BOOKING['id']}?msg=unlinked&undo=i-tilly")
    assert r.status_code == 200 and SAID in r.text
    before, after = r.text.split(SAID)
    notice = before.rsplit("<form", 1)[1]
    assert f'action="/app/diary/{BOOKING["id"]}/subjects"' in notice     # the link route itself
    assert 'name="instance_id" value="i-tilly"' in notice
    assert "data-flash-hold" in notice                                   # stays until tapped
    assert ">Undo</button>" in after[:200]
    # The dropdown still lists her too — the undo is a shortcut, not a new path.
    assert 'option value="i-tilly"' in r.text


def test_a_stray_id_offers_nothing(client, monkeypatch):
    _wire(monkeypatch)
    r = client.get(f"/app/diary/{BOOKING['id']}?msg=unlinked&undo=i-nobody")
    assert r.status_code == 200
    assert "unlinked from this booking" not in r.text and ">Undo<" not in r.text


def test_the_address_bar_forgets_the_undo_like_any_notice():
    js = (ROOT / "src/app/static/flash.js").read_text(encoding="utf-8")
    assert "'undo'" in js.split("var NOTICE = ")[1].split("];")[0]
