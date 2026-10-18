"""The Concierge's email cards carry the configured sign-off (src/app/sign_off.py).

The Concierge model writes the text of a card itself (a reply to a customer, a
document renewal request, an overdue-payment reminder, a quote follow-up) and
Confirm sends it with no composer in between. So the sign-off is applied when the
card is STAGED: the owner reviews exactly the text that sends, Confirm does not
add anything, and an owner who deletes the sign-off in the card keeps it deleted.

Pinned here:
  * every email card's text ends with the configured sign-off exactly once, and a
    closing the model wrote is replaced rather than doubled;
  * the reminder cards sign BEFORE the payment / quote link is appended, so the
    link stays last;
  * a config read failure leaves the text as written and never bounces the card;
  * an empty draft still bounces without a config read;
  * Confirm sends the approved text as it is (no re-append);
  * the voice draft's agent-log event says when no sign-off is set.

Run: python -m pytest tests/test_concierge_signoff.py -q
"""
import asyncio
import logging

import pytest

from app import concierge_actions as ca
from app import db, payments
from app.config import settings

TENANT = "11111111-1111-1111-1111-111111111111"
INV_ID = "33333333-3333-3333-3333-333333333333"
QUOTE_ID = "22222222-2222-2222-2222-222222222222"
SIGN = "Cheers,\r\nSam "          # as stored by a browser textarea, with stray whitespace
SIGNED = "Cheers,\nSam"            # what the card carries


def _config(monkeypatch, sign_off=SIGN):
    monkeypatch.setattr(ca.db, "effective_config",
                        lambda tenant_id, mailbox_id=None, client=None:
                        {"sign_off": sign_off} if sign_off is not None else {})


def _config_fails(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("config read failed")
    monkeypatch.setattr(ca.db, "effective_config", boom)


def _thread(monkeypatch):
    monkeypatch.setattr(ca.db, "get_conversation", lambda tenant_id, contact, client=None: [
        {"id": "log-1", "subject": "Thursday?", "mailbox_id": None}])


# ── a reply card ──────────────────────────────────────────────────────────────
def test_stage_draft_reply_carries_the_configured_sign_off(monkeypatch):
    _thread(monkeypatch)
    _config(monkeypatch)
    summary, payload, details, text = ca._stage_draft_reply(
        TENANT, None, {"contact": "Alex@Example.invalid",
                       "draft_text": "Hi Alex, Thursday works.\n\nThanks,\nSam"})
    assert text == "Hi Alex, Thursday works.\n\n" + SIGNED
    assert payload["draft_text"] == text                 # the card's editable text is the same
    assert "Thanks,\nSam" not in text


def test_stage_draft_reply_without_a_closing_gets_the_sign_off_appended(monkeypatch):
    _thread(monkeypatch)
    _config(monkeypatch)
    _, payload, _, text = ca._stage_draft_reply(
        TENANT, None, {"contact": "alex@example.invalid", "draft_text": "Hi Alex, Thursday works."})
    assert text == "Hi Alex, Thursday works.\n\n" + SIGNED
    # Already signed: left exactly as it is.
    _, _, _, again = ca._stage_draft_reply(
        TENANT, None, {"contact": "alex@example.invalid", "draft_text": text})
    assert again == text


def test_stage_draft_reply_with_no_sign_off_set_is_unchanged(monkeypatch):
    _thread(monkeypatch)
    _config(monkeypatch, None)
    _, _, _, text = ca._stage_draft_reply(
        TENANT, None, {"contact": "alex@example.invalid",
                       "draft_text": "Hi Alex, Thursday works.\n\nThanks,\nSam"})
    assert text == "Hi Alex, Thursday works.\n\nThanks,\nSam"


def test_stage_draft_reply_survives_a_config_read_failure(monkeypatch, caplog):
    _thread(monkeypatch)
    _config_fails(monkeypatch)
    with caplog.at_level(logging.WARNING, logger=ca.log.name):
        _, payload, _, text = ca._stage_draft_reply(
            TENANT, None, {"contact": "alex@example.invalid",
                           "draft_text": "Hi Alex, Thursday works."})
    assert text == "Hi Alex, Thursday works." and payload["draft_text"] == text
    assert any("sign-off config read failed" in r.getMessage() for r in caplog.records)


def test_stage_draft_reply_still_bounces_an_empty_draft_without_reading_config(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("an empty draft must not reach the config read")
    monkeypatch.setattr(ca.db, "effective_config", forbidden)
    with pytest.raises(ca.ActionError, match="any draft text"):
        ca._stage_draft_reply(TENANT, None, {"contact": "alex@example.invalid", "draft_text": "  "})


def test_confirm_sends_the_approved_text_without_adding_the_sign_off(monkeypatch):
    """The owner may delete the sign-off in the card; Confirm must not put it back."""
    writes = {}
    _config_fails(monkeypatch)       # any re-append would have to read the config
    monkeypatch.setattr(ca.db, "get_email_log_entry", lambda log_id, tenant_id, client=None: {"id": log_id})
    monkeypatch.setattr(ca.db, "update_email_log_draft",
                        lambda log_id, text, tenant_id, client=None: writes.update(text=text))
    monkeypatch.setattr(ca.db, "set_email_log_status", lambda *a, **k: None)
    monkeypatch.setattr(ca.db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(ca.events, "publish", lambda *a, **k: None)
    asyncio.run(ca.execute(
        TENANT, None, "draft_reply",
        {"log_id": "log-1", "contact": "alex@example.invalid", "subject": "s",
         "draft_text": "Hi Alex, Thursday works.\n\n" + SIGNED},
        edited_draft_text="Hi Alex, Thursday works."))
    assert writes["text"] == "Hi Alex, Thursday works."


# ── a document renewal request ────────────────────────────────────────────────
def _dependant(monkeypatch):
    inst = {"id": "inst-1", "contact_id": "c-1", "data": {"renew": "2026-12-01"}}
    group = {"fields": [{"key": "renew", "label": "Renewal", "durability": "expires"}]}
    owner = {"name": "Alex Doe", "email": "alex@example.invalid"}
    monkeypatch.setattr(ca, "_find_dependant", lambda *a, **k: (inst, group, owner))
    monkeypatch.setattr(ca.db, "get_invoice_settings",
                        lambda *a, **k: {"business_name": "Example Team"})


def test_stage_request_renewal_carries_the_sign_off(monkeypatch):
    _dependant(monkeypatch)
    _config(monkeypatch)
    _, payload, _, text = ca._stage_request_renewal(
        TENANT, None, {"name": "Sam", "title": "Renewal",
                       "draft_text": "Hi Alex,\n\nCould you send the new one?\n\nKind regards,\nExample Team"})
    assert text == "Hi Alex,\n\nCould you send the new one?\n\n" + SIGNED
    assert payload["draft_text"] == text
    # The built-in fallback wording ends "Thank you,\n<business>": replaced the same way.
    _, _, _, fallback = ca._stage_request_renewal(
        TENANT, None, {"name": "Sam", "title": "Renewal"})
    assert fallback.endswith("\n\n" + SIGNED)
    assert "Thank you," not in fallback and fallback.count(SIGNED) == 1


# ── the reminder cards sign before the link is appended ───────────────────────
def _invoice(monkeypatch):
    monkeypatch.setattr(ca.db, "get_invoice", lambda i, t, client=None: {
        "id": INV_ID, "invoice_ref": "INV-1001", "status": "overdue", "total": 180.0,
        "customer_email": "alex@example.invalid", "due_date": "2026-09-01", "reminders_sent": 0})
    monkeypatch.setattr(payments, "tenant_can_take_payments", lambda *a, **k: True)
    monkeypatch.setattr(settings, "app_base_url", "https://example.invalid")


def test_invoice_and_quote_reminder_cards_sign_before_the_link(monkeypatch):
    _invoice(monkeypatch)
    _config(monkeypatch)
    _, payload, _, text = ca._stage_invoice_reminder_draft(
        TENANT, None, {"invoice_id": INV_ID,
                       "draft_text": "Hi Alex, a gentle nudge about INV-1001.\n\nThanks,\nSam"})
    link = f"https://example.invalid/pay/{INV_ID}"
    assert text == ("Hi Alex, a gentle nudge about INV-1001.\n\n" + SIGNED
                    + f"\n\nYou can pay online here: {link}")
    assert text.endswith(link) and payload["draft_text"] == text
    # The model already included the link: it stays where the model put it, once.
    _, _, _, with_link = ca._stage_invoice_reminder_draft(
        TENANT, None, {"invoice_id": INV_ID,
                       "draft_text": f"Hi Alex, pay here: {link}\n\nThanks,\nSam"})
    assert with_link.count(link) == 1 and with_link.endswith(SIGNED)

    monkeypatch.setattr(ca.db, "get_quote", lambda i, t, client=None: {
        "id": QUOTE_ID, "quote_ref": "QUO-1001", "status": "sent", "total": 120.0,
        "customer_email": "alex@example.invalid", "reminders_sent": 0})
    qlink = f"https://example.invalid/q/{QUOTE_ID}"
    _, qpayload, _, qtext = ca._stage_quote_reminder_draft(
        TENANT, None, {"quote_id": QUOTE_ID,
                       "draft_text": "Hi Alex, just checking in on QUO-1001.\n\nKind regards,\nSam"})
    assert qtext == ("Hi Alex, just checking in on QUO-1001.\n\n" + SIGNED
                     + f"\n\nYou can view and accept the quote here:\n{qlink}")
    assert qpayload["draft_text"] == qtext
    # The fallback wording ends "Thank you,\n<business>": replaced, link still last.
    monkeypatch.setattr(ca.db, "get_invoice_settings",
                        lambda *a, **k: {"business_name": "Example Team"})
    _, _, _, qfallback = ca._stage_quote_reminder_draft(TENANT, None, {"quote_id": QUOTE_ID})
    assert "Thank you," not in qfallback and qfallback.endswith(qlink)
    assert qfallback.count(SIGNED) == 1
    assert qfallback.index(SIGNED) < qfallback.index(qlink)


def test_reminder_cards_survive_a_config_read_failure(monkeypatch):
    _invoice(monkeypatch)
    _config_fails(monkeypatch)
    link = f"https://example.invalid/pay/{INV_ID}"
    _, _, _, text = ca._stage_invoice_reminder_draft(
        TENANT, None, {"invoice_id": INV_ID, "draft_text": "Hi Alex, a nudge.\n\nThanks,\nSam"})
    assert text == f"Hi Alex, a nudge.\n\nThanks,\nSam\n\nYou can pay online here: {link}"


# ── The built-in fallback wording closes with the configured sign-off ─────────
# A fallback card used to end "Thank you,\n<business name>" and rely on the sign-off
# code to recognise that name line as a second closing. A name ending in "." or running
# long is not recognisable, so the card carried both closings. It now closes with the
# configured sign-off directly, exactly as the invoice and quote emails do.
BUSINESS_NAMES = [
    "Example Works Ltd.", "J. Smith & Co.", "Example & Sons Trading & Services Ltd.",
    "The Old Mill Counter and Coffee House", "Example Works, Ltd.", "Example Works Ltd. (UK)",
]


def _fallback_cards(monkeypatch, business):
    """The renewal, invoice-reminder and quote-reminder cards built from the built-in
    fallback wording (no draft_text from the model) for a business called `business`."""
    _dependant(monkeypatch)
    _invoice(monkeypatch)
    monkeypatch.setattr(ca.db, "get_quote", lambda i, t, client=None: {
        "id": QUOTE_ID, "quote_ref": "QUO-1001", "status": "sent", "total": 120.0,
        "customer_email": "alex@example.invalid", "reminders_sent": 0})
    monkeypatch.setattr(ca.db, "get_invoice_settings",
                        lambda *a, **k: {"business_name": business})
    return {
        "renewal": ca._stage_request_renewal(
            TENANT, None, {"name": "Sam", "title": "Renewal"})[3],
        "invoice": ca._stage_invoice_reminder_draft(
            TENANT, None, {"invoice_id": INV_ID})[3],
        "quote": ca._stage_quote_reminder_draft(
            TENANT, None, {"quote_id": QUOTE_ID})[3],
    }


@pytest.mark.parametrize("business", BUSINESS_NAMES)
def test_fallback_cards_close_once_with_the_configured_sign_off(monkeypatch, business):
    monkeypatch.setattr(ca.db, "get_tenant_config", lambda *a, **k: {"sign_off": SIGN})
    for kind, text in _fallback_cards(monkeypatch, business).items():
        assert text.count(SIGNED) == 1, kind
        assert "Thank you" not in text and business not in text, kind
        assert text.count("Cheers,") == 1 and text.count("\nSam") == 1, kind
        if kind == "renewal":
            assert text.endswith("\n\n" + SIGNED)
        else:                       # the pay / accept link is still last
            assert text.index(SIGNED) < text.index("https://example.invalid")


@pytest.mark.parametrize("business", BUSINESS_NAMES)
def test_fallback_cards_keep_their_plain_closing_without_a_sign_off(monkeypatch, business):
    monkeypatch.setattr(ca.db, "get_tenant_config", lambda *a, **k: {"sign_off": None})
    for kind, text in _fallback_cards(monkeypatch, business).items():
        assert text.count("Thank you,") == 1 and text.count(business) == 1, kind
        assert ("Thank you,\n" + business) in text, kind


def test_fallback_cards_survive_an_unreadable_config(monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("config unavailable")
    monkeypatch.setattr(ca.db, "get_tenant_config", broken)
    for kind, text in _fallback_cards(monkeypatch, "Example Works Ltd.").items():
        assert "Thank you,\nExample Works Ltd." in text, kind
