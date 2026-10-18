"""Invoice and quote emails close with the business's own sign-off (src/app/sign_off.py).

These two emails are written by code with no model in between, and used to end
"Thank you,\n<business name>" whatever sign-off the business had set at onboarding.
Now the configured sign-off is the closing when there is one, and the old plain
closing remains the fallback when there is none (or the config cannot be read).

Run: python -m pytest tests/test_document_email_sign_off.py -q
"""
import asyncio

import pytest

from app import concierge_actions as ca
from app import db, invoice_send, payments, quote_send
from app.config import settings

TENANT = "11111111-1111-1111-1111-111111111111"
INV_ID = "33333333-3333-3333-3333-333333333333"
QUOTE_ID = "22222222-2222-2222-2222-222222222222"
SIGN = "Cheers,\r\nSam "
SIGNED = "Cheers,\nSam"
INV = {"id": INV_ID, "invoice_ref": "INV-1001", "total": 180.0, "due_date": "2026-08-11",
       "customer_email": "alex@example.invalid", "status": "sent"}
QUOTE = {"id": QUOTE_ID, "quote_ref": "QUO-1001", "total": 120.0, "title": "Example job",
         "customer_email": "alex@example.invalid", "status": "sent"}


# ── the body builders ─────────────────────────────────────────────────────────
def test_invoice_body_ends_with_the_configured_sign_off():
    body = invoice_send.invoice_email_body(INV, {"business_name": "Example Team"}, None, SIGN)
    assert body.rstrip().endswith("\n" + SIGNED)
    assert "Thank you," not in body and "\r" not in body
    # No sign-off set: unchanged from before.
    plain = invoice_send.invoice_email_body(INV, {"business_name": "Example Team"}, None)
    assert plain.rstrip().endswith("Thank you,\nExample Team")
    assert invoice_send.invoice_email_body(INV, {"business_name": ""}, None, "").rstrip().endswith(
        "Thank you")
    # A blank sign-off is not a sign-off.
    assert invoice_send.invoice_email_body(
        INV, {"business_name": "Example Team"}, None, " \r\n ").rstrip().endswith(
        "Thank you,\nExample Team")


def test_quote_body_ends_with_the_configured_sign_off():
    link = f"https://example.invalid/q/{QUOTE_ID}"
    body = quote_send.quote_email_body(QUOTE, {"business_name": "Example Team"}, link, SIGN)
    assert body.rstrip().endswith("\n" + SIGNED)
    assert "Thank you," not in body and body.index(link) < body.index(SIGNED)
    plain = quote_send.quote_email_body(QUOTE, {"business_name": "Example Team"}, link)
    assert plain.rstrip().endswith("Thank you,\nExample Team")


# ── the configured value is read once, best-effort ────────────────────────────
def test_configured_sign_off_is_canonical_and_never_raises(monkeypatch):
    monkeypatch.setattr(db, "get_tenant_config", lambda *a, **k: {"sign_off": SIGN})
    assert invoice_send.configured_sign_off(TENANT) == SIGNED
    monkeypatch.setattr(db, "get_tenant_config", lambda *a, **k: None)
    assert invoice_send.configured_sign_off(TENANT) == ""

    def broken(*a, **k):
        raise RuntimeError("config unavailable")
    monkeypatch.setattr(db, "get_tenant_config", broken)
    assert invoice_send.configured_sign_off(TENANT) == ""


# ── the shared senders ────────────────────────────────────────────────────────
def _wire_invoice_send(monkeypatch, config):
    sent = {}

    async def fake_send(t, to, subject, body, attachments=None, **k):
        sent.update(to=to, subject=subject, body=body)
    monkeypatch.setattr(invoice_send.mailbox, "send_mail", fake_send)
    monkeypatch.setattr(db, "list_invoice_lines", lambda *a, **k: [
        {"description": "Example work", "amount": 180.0, "date": "2026-07-20"}])
    monkeypatch.setattr(db, "get_invoice_settings", lambda *a, **k: {
        "business_name": "Example Team", "bank_sort_code": "01-02-03",
        "bank_account_number": "12345678"})
    monkeypatch.setattr(db, "set_invoice_payment_link", lambda *a, **k: None)
    monkeypatch.setattr(db, "set_invoice_status", lambda *a, **k: None)
    monkeypatch.setattr(db, "insert_email_log", lambda *a, **k: ("l1", True))
    monkeypatch.setattr(db, "mark_email_log_sent", lambda *a, **k: None)
    monkeypatch.setattr(db, "crm_contact_by_email", lambda *a, **k: None)
    monkeypatch.setattr(db, "crm_insert_activity", lambda *a, **k: True)
    monkeypatch.setattr(payments, "tenant_can_take_payments", lambda *a, **k: False)
    monkeypatch.setattr(invoice_send, "_portal_footer", lambda *a, **k: "\n\nPORTAL-FOOTER")
    monkeypatch.setattr(db, "get_tenant_config", config)
    return sent


def test_send_invoice_email_signs_with_the_configured_sign_off_before_the_footer(monkeypatch):
    sent = _wire_invoice_send(monkeypatch, lambda *a, **k: {"sign_off": SIGN})
    asyncio.run(invoice_send.send_invoice_email(TENANT, dict(INV)))
    assert sent["body"].endswith(SIGNED + "\n\nPORTAL-FOOTER")
    assert "Thank you," not in sent["body"]


def test_send_invoice_email_falls_back_without_a_sign_off_or_a_config_read(monkeypatch):
    sent = _wire_invoice_send(monkeypatch, lambda *a, **k: {"sign_off": None})
    asyncio.run(invoice_send.send_invoice_email(TENANT, dict(INV)))
    assert sent["body"].endswith("Thank you,\nExample Team\n\nPORTAL-FOOTER")

    def broken(*a, **k):
        raise RuntimeError("config unavailable")
    sent = _wire_invoice_send(monkeypatch, broken)
    asyncio.run(invoice_send.send_invoice_email(TENANT, dict(INV)))
    assert sent["body"].endswith("Thank you,\nExample Team\n\nPORTAL-FOOTER")   # still sent


def test_an_operator_edited_body_is_sent_as_it_is_without_a_config_read(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("an edited body needs no sign-off lookup")
    sent = _wire_invoice_send(monkeypatch, forbidden)
    monkeypatch.setattr(invoice_send, "configured_sign_off", forbidden)
    asyncio.run(invoice_send.send_invoice_email(
        TENANT, dict(INV), body_override="Hello, edited text."))
    assert sent["body"] == "Hello, edited text.\n\nPORTAL-FOOTER"


def test_send_quote_email_signs_with_the_configured_sign_off(monkeypatch):
    from app import crm_intel, job_parties
    sent = {}

    async def fake_send(t, to, subject, body, **k):
        sent.update(subject=subject, body=body)

    async def fake_resolve(*a, **k):
        return {}
    monkeypatch.setattr(quote_send.mailbox, "send_mail", fake_send)
    monkeypatch.setattr(crm_intel, "resolve_from_action", fake_resolve)
    monkeypatch.setattr(job_parties, "cc_for_document", lambda *a, **k: [])
    monkeypatch.setattr(db, "get_invoice_settings", lambda *a, **k: {"business_name": "Example Team"})
    monkeypatch.setattr(db, "list_quote_lines", lambda *a, **k: [
        {"description": "Work", "amount": 10.0, "is_selected": True}])
    monkeypatch.setattr(db, "set_quote_status", lambda *a, **k: None)
    monkeypatch.setattr(db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(quote_send, "_log_sent_email", lambda *a, **k: None)
    monkeypatch.setattr(quote_send, "_crm_quote_activity", lambda *a, **k: None)
    monkeypatch.setattr(quote_send, "_portal_footer", lambda *a, **k: "\n\nPORTAL-FOOTER")
    monkeypatch.setattr(db, "get_tenant_config", lambda *a, **k: {"sign_off": SIGN})
    asyncio.run(quote_send.send_quote_email(TENANT, dict(QUOTE)))
    assert sent["body"].endswith(SIGNED + "\n\nPORTAL-FOOTER")
    monkeypatch.setattr(db, "get_tenant_config", lambda *a, **k: {})
    asyncio.run(quote_send.send_quote_email(TENANT, dict(QUOTE)))
    assert sent["body"].endswith("Thank you,\nExample Team\n\nPORTAL-FOOTER")


# ── the Concierge previews show the text that will be sent ────────────────────
def test_concierge_invoice_and_quote_previews_carry_the_sign_off(monkeypatch):
    monkeypatch.setattr(settings, "app_base_url", "https://example.invalid")
    monkeypatch.setattr(db, "get_invoice", lambda i, t, client=None: dict(INV))
    monkeypatch.setattr(db, "get_quote", lambda i, t, client=None: dict(QUOTE))
    monkeypatch.setattr(db, "list_invoice_lines", lambda *a, **k: [])
    monkeypatch.setattr(db, "list_quote_lines", lambda *a, **k: [])
    monkeypatch.setattr(db, "get_invoice_settings",
                        lambda *a, **k: {"business_name": "Example Team"})
    monkeypatch.setattr(payments, "tenant_can_take_payments", lambda *a, **k: False)
    monkeypatch.setattr(db, "get_tenant_config", lambda *a, **k: {"sign_off": SIGN})
    _, payload, _, body = ca._stage_invoice_send(TENANT, None, {"invoice_id": INV_ID})
    assert body.rstrip().endswith(SIGNED) and payload["draft_text"] == body
    _, payload, _, body = ca._stage_quote_send(TENANT, None, {"quote_id": QUOTE_ID})
    assert body.rstrip().endswith(SIGNED) and payload["draft_text"] == body
    # No sign-off set: the previews keep their plain closing.
    monkeypatch.setattr(db, "get_tenant_config", lambda *a, **k: {})
    _, _, _, body = ca._stage_invoice_send(TENANT, None, {"invoice_id": INV_ID})
    assert body.rstrip().endswith("Thank you,\nExample Team")
    _, _, _, body = ca._stage_quote_send(TENANT, None, {"quote_id": QUOTE_ID})
    assert body.rstrip().endswith("Thank you,\nExample Team")
