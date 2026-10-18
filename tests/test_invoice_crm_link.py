"""Invoices linked to their CRM contact, the client-history view, and customer
selection on generate.

Why these exist together: the bulk generate flow was the ONE creation path that never
passed contact_id (manual /invoices/new and the concierge path both did), so every
invoice it produced was orphaned from the CRM — no client history, and nothing for the
assistant to reason over. Linking it also makes the contact page's invoice card
possible, and the customer-selection form is what stops "generate" being all-or-nothing.

Run: python -m pytest tests/test_invoice_crm_link.py -q
"""
import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import invoicing
from app.auth import TenantCtx, require_onboarded
from app.dashboard import require_crm
from app.main import app
from app.security import require_csrf

DRAFTS = [
    {"customer_email": "a@example.com", "customer_name": "Client A", "count": 2,
     "total": 240.0, "lines": [{"booking_id": "bk-a", "description": "Job", "amount": 240.0}]},
    {"customer_email": "b@example.com", "customer_name": "Client B", "count": 1,
     "total": 80.0, "lines": [{"booking_id": "bk-b", "description": "Job", "amount": 80.0}]},
]


# ── draft selection (pure) ────────────────────────────────────────────────────
def test_draft_key_prefers_email_and_lower_cases_it():
    assert invoicing.draft_key({"customer_email": "A@Example.COM"}) == "a@example.com"


def test_draft_key_falls_back_to_the_name_when_there_is_no_email():
    # An office booking can carry a contact name with no address.
    assert invoicing.draft_key({"customer_email": None,
                                "customer_name": "Mrs Patel"}) == "mrs patel"


def test_select_drafts_keeps_only_the_ticked_customers():
    got = invoicing.select_drafts(DRAFTS, ["b@example.com"])
    assert [d["customer_name"] for d in got] == ["Client B"]


def test_select_drafts_is_case_insensitive():
    assert len(invoicing.select_drafts(DRAFTS, ["A@EXAMPLE.COM"])) == 1


def test_empty_selection_means_everything_not_nothing():
    # Back-compat: a caller posting no selection still generates the whole period.
    assert invoicing.select_drafts(DRAFTS, []) == DRAFTS
    assert invoicing.select_drafts(DRAFTS, None) == DRAFTS
    assert invoicing.select_drafts(DRAFTS, ["", "  "]) == DRAFTS


def test_selection_of_an_unknown_customer_yields_nothing():
    assert invoicing.select_drafts(DRAFTS, ["nobody@example.com"]) == []


# ── the generate route ────────────────────────────────────────────────────────
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"fsm"})
    monkeypatch.setattr(dashboard.db, "visible_modules", lambda *a, **k: {"fsm"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "list_invoices", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "get_invoice_settings", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.invoicing, "draft_view_for", lambda *a, **k: list(DRAFTS))
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def _capture_invoices(monkeypatch, contacts=None):
    created = []
    monkeypatch.setattr(dashboard.db, "crm_contact_by_email",
                        lambda tid, email, **k: (contacts or {}).get(email))
    monkeypatch.setattr(dashboard.db, "create_invoice",
                        lambda *a, **k: created.append((a, k)) or {"id": "i"})
    return created


def test_generated_invoice_is_linked_to_its_crm_contact(client, monkeypatch):
    created = _capture_invoices(monkeypatch, {"a@example.com": {"id": "c-a"},
                                              "b@example.com": {"id": "c-b"}})
    r = client.post("/app/invoices/generate",
                    data={"period_from": "2026-07-01", "period_to": "2026-07-31"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert [k["contact_id"] for _, k in created] == ["c-a", "c-b"]


def test_generated_invoice_survives_a_customer_with_no_crm_contact(client, monkeypatch):
    created = _capture_invoices(monkeypatch, {})           # nobody matches
    client.post("/app/invoices/generate",
                data={"period_from": "2026-07-01", "period_to": "2026-07-31"},
                follow_redirects=False)
    assert len(created) == 2 and all(k["contact_id"] is None for _, k in created)


def test_generate_only_bills_the_ticked_customers(client, monkeypatch):
    created = _capture_invoices(monkeypatch, {"b@example.com": {"id": "c-b"}})
    r = client.post("/app/invoices/generate",
                    data={"period_from": "2026-07-01", "period_to": "2026-07-31",
                          "customers": ["b@example.com"]}, follow_redirects=False)
    assert r.status_code == 303 and "n=1" in r.headers["location"]
    assert len(created) == 1 and created[0][0][1] == "b@example.com"


def test_generate_with_no_selection_still_bills_everyone(client, monkeypatch):
    created = _capture_invoices(monkeypatch)
    client.post("/app/invoices/generate",
                data={"period_from": "2026-07-01", "period_to": "2026-07-31"},
                follow_redirects=False)
    assert len(created) == 2


def test_generate_reports_none_when_the_selection_matches_nothing(client, monkeypatch):
    created = _capture_invoices(monkeypatch)
    r = client.post("/app/invoices/generate",
                    data={"period_from": "2026-07-01", "period_to": "2026-07-31",
                          "customers": ["ghost@example.com"]}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("msg=none")
    assert created == []


# ── the generate form ─────────────────────────────────────────────────────────
def test_preview_is_a_GET_so_it_cannot_disagree_with_generate(client):
    # The dates used to live in the POST form, so editing them without reloading left
    # the preview describing a different period from the one actually billed.
    r = client.get("/app/invoices")
    assert 'method="get" action="/app/invoices"' in r.text
    assert 'name="from"' in r.text and 'name="to"' in r.text
    # …and the POST carries the period the preview was BUILT from.
    assert 'name="period_from" value=' in r.text


def test_customer_checkboxes_render_ticked_by_default(client):
    r = client.get("/app/invoices")
    assert r.text.count('name="customers"') == 2
    assert 'value="a@example.com" checked' in r.text
    assert "untick anyone you&#39;re not billing yet" in r.text or "untick anyone" in r.text


def test_form_tells_the_user_double_billing_is_impossible(client):
    # Literal template text, so it is not autoescaped.
    assert "double-bill anyone" in client.get("/app/invoices").text


def test_crm_teaser_shows_for_a_non_crm_tenant(client):
    r = client.get("/app/invoices")
    assert "Pick customers from your CRM" in r.text and "Pro" in r.text


def test_crm_teaser_hidden_once_the_crm_is_active(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"fsm", "crm"})
    monkeypatch.setattr(dashboard.db, "visible_modules", lambda *a, **k: {"fsm", "crm"})
    assert "Pick customers from your CRM" not in client.get("/app/invoices").text


# ── the contact page's invoice history ────────────────────────────────────────
CONTACT = {"id": "c-a", "email": "a@example.com", "name": "Client A", "custom_data": {}}
CONTACT_INVOICES = [
    {"id": "i1", "invoice_ref": "INV-1001", "status": "paid", "total": 240.0,
     "amount_paid": 240.0, "issue_date": "2026-07-02", "paid_at": "2026-07-09",
     "sent_at": "2026-07-02"},
    {"id": "i2", "invoice_ref": "INV-1002", "status": "overdue", "total": 95.0,
     "issue_date": "2026-06-20", "paid_at": None},
]


@pytest.fixture
def crm_client(monkeypatch):
    d = dashboard.db
    monkeypatch.setattr(d, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(d, "active_modules", lambda *a, **k: {"crm"})
    monkeypatch.setattr(d, "visible_modules", lambda *a, **k: {"crm"})
    monkeypatch.setattr(d, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(d, "crm_get_contact", lambda *a, **k: dict(CONTACT))
    monkeypatch.setattr(d, "crm_list_deals", lambda *a, **k: [])
    monkeypatch.setattr(d, "crm_activities_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(d, "crm_list_field_definitions", lambda *a, **k: [])
    monkeypatch.setattr(d, "crm_pending_suggestions", lambda *a, **k: [])
    monkeypatch.setattr(d, "crm_open_promises", lambda *a, **k: [])
    monkeypatch.setattr(d, "crm_list_enrollments", lambda *a, **k: [])
    monkeypatch.setattr(d, "crm_list_sequences", lambda *a, **k: [])
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_crm] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_contact_page_shows_the_invoice_history_and_totals(crm_client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "invoices_for_crm_contact",
                        lambda *a, **k: list(CONTACT_INVOICES))
    r = crm_client.get("/app/crm/contacts/c-a")
    assert r.status_code == 200
    assert "INV-1001" in r.text and "INV-1002" in r.text
    assert "Billed" in r.text and "Paid" in r.text and "Outstanding" in r.text
    assert "£335.00" in r.text          # 240 paid + 95 outstanding = billed
    assert "£95.00" in r.text           # still owed


def test_contact_page_has_an_empty_state_when_they_have_never_been_invoiced(crm_client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "invoices_for_crm_contact", lambda *a, **k: [])
    r = crm_client.get("/app/crm/contacts/c-a")
    assert r.status_code == 200 and "No invoices yet" in r.text


def test_contact_page_still_renders_when_the_invoice_lookup_fails(crm_client, monkeypatch):
    # Best-effort by design: a tenant who has never touched invoicing must not get a
    # 500 on their contact page.
    def _boom(*a, **k):
        raise RuntimeError("invoices table unavailable")

    monkeypatch.setattr(dashboard.db, "invoices_for_crm_contact", _boom)
    r = crm_client.get("/app/crm/contacts/c-a")
    assert r.status_code == 200 and "Client A" in r.text
