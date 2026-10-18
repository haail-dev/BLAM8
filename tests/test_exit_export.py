"""The exit door (src/app/exit_export.py) + the price promise.

The research's two trust vacuums: nobody in the sector is praised for price
stability, and nobody for a dignified exit (the horror stories are deleted
histories on cancellation). These tests pin the promises:
  * the bundle carries every section, and names — never silently omits —
    anything it couldn't read
  * THE claim: /app/export.zip still works when the subscription gate is ON
    and the tenant has lapsed, while the rest of /app redirects to billing
  * the promise is stated where buyers read: pricing section + FAQ + Settings

Run: python -m pytest tests/test_exit_export.py -q
"""
from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import db, exit_export, trial
from app.auth import TenantCtx, require_onboarded
from app.config import settings
from app.main import app

TENANT = "11111111-1111-1111-1111-111111111111"


# ── the pure builders ─────────────────────────────────────────────────────────
def test_quotes_csv_carries_the_acceptance_record_and_the_po():
    body = exit_export.quotes_csv([
        {"quote_ref": "QUO-1001", "status": "accepted", "issue_date": "2026-08-01",
         "customer_name": "Harbour & Co", "customer_reference": "PO 4471",
         "total": 420.0, "accepted_name": "Sam Patel",
         "accepted_at": "2026-08-04T10:00:00+00:00"}])
    rows = body.splitlines()
    assert "customer_reference" in rows[0] and "accepted_name" in rows[0]
    assert "PO 4471" in rows[1] and "Sam Patel" in rows[1] and "2026-08-04" in rows[1]


def test_bundle_contains_readme_manifest_and_sections():
    blob = exit_export.build_bundle(
        {"invoices.csv": "a,b\r\n", "crm.json": "{}"}, skipped=[])
    z = zipfile.ZipFile(io.BytesIO(blob))
    assert set(z.namelist()) == {"README.txt", "MANIFEST.txt",
                                 "invoices.csv", "crm.json"}
    manifest = z.read("MANIFEST.txt").decode()
    assert "included: invoices.csv" in manifest and "UNAVAILABLE" not in manifest


def test_a_failed_section_is_declared_never_silent():
    blob = exit_export.build_bundle({"quotes.csv": "x\r\n"},
                                    skipped=["knowledge.csv"])
    manifest = zipfile.ZipFile(io.BytesIO(blob)).read("MANIFEST.txt").decode()
    assert "UNAVAILABLE: knowledge.csv" in manifest


# ── the route ─────────────────────────────────────────────────────────────────
def _stub_bundle_reads(monkeypatch):
    monkeypatch.setattr(db, "list_invoices", lambda *a, **k: [])
    monkeypatch.setattr(db, "invoice_lines_by_invoice", lambda *a, **k: {})
    monkeypatch.setattr(db, "get_invoice_settings", lambda *a, **k: {})
    monkeypatch.setattr(db, "list_quotes", lambda *a, **k: [])
    monkeypatch.setattr(db, "crm_list_contacts", lambda *a, **k: [
        {"name": "Rob Smith", "email": "rob@acme.example",
         "crm_companies": {"name": "Acme"}}])
    monkeypatch.setattr(db, "crm_export", lambda *a, **k: {"crm_deals": []})
    monkeypatch.setattr(db, "list_bookings", lambda *a, **k: [])
    monkeypatch.setattr(db, "export_knowledge_chunks", lambda *a, **k: [
        {"source": "prices.pdf", "content": "Callout £80", "created_at": "2026-06-01"}])


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(
        tenant_id=TENANT, client=None)
    yield TestClient(app, follow_redirects=False)
    app.dependency_overrides.clear()


def test_export_returns_a_zip_with_every_section(client, monkeypatch):
    _stub_bundle_reads(monkeypatch)
    r = client.get("/app/export.zip")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/zip")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    for name in ("README.txt", "MANIFEST.txt", "invoices.csv", "quotes.csv",
                 "contacts.csv", "crm.json", "bookings.json", "knowledge.csv"):
        assert name in z.namelist(), name
    assert "Rob Smith" in z.read("contacts.csv").decode()
    assert "Callout" in z.read("knowledge.csv").decode()


def test_the_export_outlives_the_subscription(client, monkeypatch):
    # THE promise. Gate ON, subscription lapsed, trial dead: the rest of /app
    # bounces to billing — the exit door still opens.
    _stub_bundle_reads(monkeypatch)
    monkeypatch.setattr(settings, "require_subscription_gate", True)
    monkeypatch.setattr(db, "get_tenant",
                        lambda *a, **k: {"subscription_status": "canceled"})
    monkeypatch.setattr(trial, "is_active", lambda tenant: False)
    monkeypatch.setattr(db, "list_email_log", lambda *a, **k: [])
    gated = client.get("/app/money")
    assert gated.status_code in (302, 303, 307)
    assert "/billing" in gated.headers.get("location", "")
    r = client.get("/app/export.zip")
    assert r.status_code == 200
    assert zipfile.ZipFile(io.BytesIO(r.content)).namelist()


def test_a_broken_section_does_not_sink_the_bundle(client, monkeypatch):
    _stub_bundle_reads(monkeypatch)
    def boom(*a, **k):
        raise RuntimeError("table gone")
    monkeypatch.setattr(db, "export_knowledge_chunks", boom)
    r = client.get("/app/export.zip")
    assert r.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert "knowledge.csv" not in z.namelist()
    assert "UNAVAILABLE: knowledge.csv" in z.read("MANIFEST.txt").decode()


# ── the promise is stated where buyers read ───────────────────────────────────
def test_landing_pricing_states_the_price_promise():
    r = TestClient(app).get("/")
    assert r.status_code == 200
    assert "The price promise" in r.text
    assert "The price you join on is the price you keep" in r.text
    assert "Leave with everything" in r.text
    assert "No per-seat pricing" in r.text
