"""No-defaults rule; first-walk re-run, 1 Oct 2026.

A business that never stated its payment terms printed "Payment due within 14
days of the invoice date." on every invoice, and when the setup assistant cleared
the terms the page fell back to that sentence. Terms are now unset until the
owner sets them: with none, nothing about terms prints on the invoice page, PDF,
customer pay page or invoice email, and the settings page shows an empty box.
A business that HAS saved terms is unaffected.

Run: python -m pytest tests/test_no_default_payment_terms.py -q
"""
import io
import re

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import invoice_config, invoice_pdf, invoice_send, payments
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf

OLD_DEFAULT = "Payment due within 14 days of the invoice date."
SAVED = "Pay by bank transfer within 21 days; deposits are non-refundable."

_INV = {"id": "i1", "invoice_ref": "INV-1001", "customer_name": "Mrs Patel",
        "customer_email": "x@y.co", "status": "sent", "issue_date": "2026-10-01",
        "due_date": "2026-10-15", "subtotal": 100.0, "vat": 0.0, "total": 100.0}
_LINES = [{"description": "A line", "amount": 100.0}]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"fsm"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "get_tenant_config", lambda *a, **k: {})
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def _pdf_text(settings) -> str:
    from pypdf import PdfReader
    data = invoice_pdf.render(_INV, _LINES, settings)
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(data)).pages)


def _pay_page(settings) -> str:
    return payments._pay_page_html(_INV, settings, paid=False, bank=None,
                                   pay_now=False, lines=_LINES)


def _invoice_page(client, monkeypatch, row) -> str:
    monkeypatch.setattr(dashboard.db, "get_invoice", lambda *a, **k: dict(_INV))
    monkeypatch.setattr(dashboard.db, "list_invoice_lines", lambda *a, **k: list(_LINES))
    monkeypatch.setattr(dashboard.db, "get_invoice_settings", lambda *a, **k: row)
    return client.get("/app/invoices/i1").text


# ── the setting ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("row", [None, {}, {"payment_terms": None}, {"payment_terms": ""},
                                 {"payment_terms": "   "}])
def test_no_terms_set_means_no_terms(row):
    s = invoice_config.settings_for(row)
    assert invoice_config.payment_terms_text(s["payment_terms"]) is None
    assert not s["payment_terms"]
    assert OLD_DEFAULT not in repr(s)
    assert not invoice_config.DEFAULTS["payment_terms"]


def test_cleared_terms_do_not_fall_back_to_a_sentence():
    # the setup assistant (or the owner) clears the box and saves
    saved = invoice_config.clean_form({"payment_terms": "", "show_payment_terms": "1"})
    assert saved["payment_terms"] is None
    assert not invoice_config.settings_for(saved)["payment_terms"]


def test_saved_terms_are_kept_exactly():
    s = invoice_config.settings_for({"payment_terms": SAVED, "show_payment_terms": True})
    assert s["payment_terms"] == SAVED
    assert invoice_config.settings_for({"payment_terms": "14"})["payment_terms"] == OLD_DEFAULT


# ── every document ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("row", [None, {"show_payment_terms": True},
                                 {"show_payment_terms": True, "payment_terms": ""}])
def test_no_terms_prints_nothing_anywhere(client, monkeypatch, row):
    s = invoice_config.settings_for(row)
    html = _invoice_page(client, monkeypatch, row)
    assert OLD_DEFAULT not in html and "Payment due within" not in html
    assert "Payment terms" not in html
    pdf = _pdf_text(s)
    assert "Payment due" not in pdf and "terms" not in pdf.lower()
    pay = _pay_page(s)
    assert "Payment due" not in pay and "terms" not in pay.lower()
    mail = invoice_send.invoice_email_body(_INV, s, None)
    assert "Payment due" not in mail and "terms" not in mail.lower()


def test_saved_terms_print_everywhere(client, monkeypatch):
    row = {"show_payment_terms": True, "payment_terms": SAVED}
    s = invoice_config.settings_for(row)
    assert SAVED in _invoice_page(client, monkeypatch, row)
    assert SAVED in " ".join(_pdf_text(s).split())
    assert SAVED in _pay_page(s)
    assert f"\n{SAVED}\n" in invoice_send.invoice_email_body(_INV, s, None)


def test_saved_terms_hidden_still_print_nowhere(client, monkeypatch):
    row = {"show_payment_terms": False, "payment_terms": SAVED}
    s = invoice_config.settings_for(row)
    assert SAVED not in _invoice_page(client, monkeypatch, row)
    assert SAVED not in _pdf_text(s)
    assert SAVED not in _pay_page(s)
    assert SAVED not in invoice_send.invoice_email_body(_INV, s, None)


def test_quote_documents_never_read_the_terms_setting():
    """Quotes share the settings row but print no payment terms; if one ever
    starts to, it must also omit the section when the terms are empty."""
    import pathlib
    root = pathlib.Path(dashboard.__file__).parent
    readers = {str(p.relative_to(root)).replace("\\", "/")
               for p in list(root.rglob("*.py")) + list(root.rglob("*.html"))
               if "quote" in p.name.lower() and "payment_terms" in p.read_text("utf-8")}
    assert readers == set()


# ── the settings page ─────────────────────────────────────────────────────────
def test_settings_page_shows_an_empty_box_for_a_business_that_never_set_terms(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_invoice_settings", lambda *a, **k: None)
    html = client.get("/app/invoices/settings").text
    box = re.search(r'<input name="payment_terms"[^>]*>', html).group(0)
    assert 'value=""' in box
    assert "14 days" not in box and OLD_DEFAULT not in html
    assert "placeholder=" in box


def test_settings_page_shows_saved_terms(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_invoice_settings",
                        lambda *a, **k: {"payment_terms": SAVED, "show_payment_terms": True})
    html = client.get("/app/invoices/settings").text
    assert f'name="payment_terms" value="{SAVED}"' in html
