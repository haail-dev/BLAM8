"""TMS baseline surfaces — route + template render smoke tests.

These catch Jinja errors and route wiring the pure view-model tests don't: each
surface is hit through its real route with db monkeypatched, asserting 200 + a
date-independent heading. (No live DB — the demo seed/migration is a prod step.)

Run: python -m pytest tests/ -q
"""
import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf

VEH = {"id": "v1", "name": "7.5t box", "registration": "YX24 ABC",
       "capacity_spaces": 6, "active": True, "branch_id": None,
       "mot_expiry": "2027-01-01", "tax_expiry": "2027-01-01",
       "service_due": "2027-01-01", "insurance_expiry": "2027-01-01"}


def _bk(bid, status="confirmed", spaces=3, **kw):
    return {"id": bid, "tenant_id": "t1", "booking_ref": f"BK-{bid}",
            "booking_date": "2026-06-20", "summary": "3 pallets", "spaces": spaces,
            "status": status, "vehicle_id": "v1", "contact": "ops@acme.co",
            "collection_postcode": "WF17 9LU", "delivery_postcode": "LS9 8AS", **kw}


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)   # no `with`: startup events must not run
    app.dependency_overrides.clear()


@pytest.fixture
def env(monkeypatch):
    """Patch every db.* the TMS surfaces touch (db is the one shared module)."""
    d = dashboard.db
    completed = [_bk("b2", status="completed", quoted_amount=120.0)]
    patches = {
        "list_email_log": lambda *a, **k: [],
        "active_modules": lambda *a, **k: {"tms"},
        "module_active": lambda tid, m, **k: m == "tms",
        "is_enterprise": lambda *a, **k: False,
        "get_rate_card": lambda *a, **k: None,
        "list_bookings": lambda *a, **k: [_bk("b1"), *completed],
        "list_completed_bookings": lambda *a, **k: completed,
        # The duplicate-invoice guard asks which bookings are already billed before
        # any draft is built (invoicing._unbilled); nothing is billed in this fixture.
        "invoiced_booking_ids": lambda *a, **k: set(),
        "job_parties_for_bookings": lambda *a, **k: {},
        "get_postcodes": lambda codes: [],
        "list_shipment_events": lambda *a, **k: [
            {"booking_id": "b2", "kind": "delivered", "signed_by": "J. Smith",
             "created_at": "2026-06-20T14:00:00+00:00"}],
        "list_vehicles": lambda *a, **k: [dict(VEH)],
        "list_branches": lambda *a, **k: [{"id": "br1", "name": "Leeds", "postcode": "LS9 8AS"}],
        "list_vehicle_day_bookings": lambda *a, **k: [_bk("b1")],
        "list_drivers": lambda *a, **k: [
            {"id": "d1", "name": "Dave", "licence_expiry": "2027-01-01", "cpc_expiry": "2027-01-01"}],
        "list_driver_hours": lambda *a, **k: [{"driver_id": "d1", "work_date": "2026-06-15", "hours": 40}],
        "list_invoices": lambda *a, **k: [
            {"id": "i1", "invoice_ref": "INV-1001", "customer_email": "ops@acme.co",
             "period_from": "2026-06-01", "period_to": "2026-06-20", "total": 180.0, "status": "draft"}],
        "get_invoice_settings": lambda *a, **k: None,
    }
    for name, fn in patches.items():
        monkeypatch.setattr(d, name, fn)
    return d


@pytest.mark.parametrize("path,needle", [
    ("/app/tms/pricing", "rate card"),
    ("/app/tms/pod", "proof of delivery"),
    ("/app/invoices", "generate invoices"),   # invoicing is now one shared surface
    ("/app/tms/tracking", "tracking"),
    ("/app/tms/compliance", "compliance"),
    ("/app/driver", "driver"),
])
def test_surface_renders(client, env, path, needle):
    r = client.get(path)
    assert r.status_code == 200, f"{path} → {r.status_code}"
    assert needle in r.text.lower()


def test_home_hub_renders_core_and_addons(client, env, monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"mailbox": "ops@haail.dev"})
    monkeypatch.setattr(dashboard.db, "get_connection_status", lambda *a, **k: {"sub_expires": None})
    r = client.get("/app/modules")
    assert r.status_code == 200
    # the grouped hub, now under the Field OS umbrella — both editions by name
    assert "Core" in r.text
    assert "Field OS · Transport" in r.text and "Field OS · Trades" in r.text
    # the retired user-facing names are gone
    assert "TMS — Transport Management" not in r.text
    assert "Transport Management" not in r.text
    assert ">Field Service<" not in r.text


def test_invoice_detail_renders(client, env, monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_invoice", lambda iid, tid, **k: {
        "id": iid, "invoice_ref": "INV-1001", "customer_email": "ops@acme.co",
        "period_from": "2026-06-01", "period_to": "2026-06-20",
        "subtotal": 150.0, "vat": 30.0, "total": 180.0, "status": "draft"})
    monkeypatch.setattr(dashboard.db, "list_invoice_lines", lambda iid, tid, **k: [
        {"description": "BK-1 · job", "amount": 150.0, "date": "2026-06-18"}])
    r = client.get("/app/invoices/i1")
    assert r.status_code == 200 and "INV-1001" in r.text


def test_public_track_portal_renders(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_booking_by_track_token",
                        lambda tok, **k: _bk("b9", booking_ref="BK-9", track_token=tok))
    monkeypatch.setattr(dashboard.db, "list_events_for_booking",
                        lambda bid, **k: [{"booking_id": bid, "kind": "out_for_delivery",
                                           "created_at": "2026-06-20T10:00:00+00:00"}])
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda tid, **k: {"name": "Haail Ltd"})
    r = client.get("/track/trk-abc")
    assert r.status_code == 200
    assert "Track your delivery" in r.text and "Haail Ltd" in r.text


def test_public_track_unknown_token_is_friendly_404(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_booking_by_track_token", lambda tok, **k: None)
    r = client.get("/track/nope")
    assert r.status_code == 404 and "not found" in r.text.lower()
