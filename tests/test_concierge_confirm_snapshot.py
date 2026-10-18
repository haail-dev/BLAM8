"""A send or settle card refuses if the document changed after the card was prepared
(docs/concierge-confirm-audit-2026-10-16.md, finding 2). Numbers only in the card's
'shown' snapshot; legacy cards with no snapshot behave as before.

Run: python tests/run_offline.py tests/test_concierge_confirm_snapshot.py -q
"""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from app import concierge_actions as ca, db, invoice_send, quote_send, payments

T = "tenant-1"
SECRET = "Pemberton-Gray roof job"


class Docs:
    def __init__(self):
        self.inv = {"id": "i1", "invoice_ref": "INV-1", "status": "draft", "total": 100.0,
                    "customer_email": "a@example.com", "customer_name": SECRET}
        self.quote = {"id": "q1", "quote_ref": "QUO-1", "status": "draft", "total": 100.0,
                      "customer_email": "a@example.com"}
        self.inv_lines = [{"description": "Work", "amount": 100.0}]
        self.quote_lines = [{"description": "Work", "amount": 100.0}]
        self.sent, self.settled = [], []


@pytest.fixture
def docs(monkeypatch):
    d = Docs()
    monkeypatch.setattr(db, "get_invoice", lambda i, t, client=None: d.inv)
    monkeypatch.setattr(db, "get_quote", lambda i, t, client=None: d.quote)
    monkeypatch.setattr(db, "list_invoice_lines", lambda i, t, client=None: d.inv_lines)
    monkeypatch.setattr(db, "list_quote_lines", lambda i, t, client=None: d.quote_lines)
    monkeypatch.setattr(db, "log_agent_event", lambda *a, **k: None)

    async def send_inv(*a, **k):
        d.sent.append("inv")

    async def send_q(*a, **k):
        d.sent.append("quote")

    def paid(i, t, **k):
        d.settled.append(i)
        return {"id": i, "customer_email": "a@example.com"}

    monkeypatch.setattr(invoice_send, "send_invoice_email", send_inv)
    monkeypatch.setattr(quote_send, "send_quote_email", send_q)
    monkeypatch.setattr(db, "mark_invoice_paid", paid)
    monkeypatch.setattr(payments, "invoice_paid_followup", lambda *a, **k: None)
    return d


def _card(d, kind):
    if kind == "invoice_send":
        return {"invoice_id": "i1", "ref": "INV-1", "to": "a@example.com", "total": 100.0,
                "draft_text": "hi", "shown": ca._shown(d.inv, d.inv_lines)}
    if kind == "quote_send":
        return {"quote_id": "q1", "ref": "QUO-1", "to": "a@example.com", "total": 100.0,
                "draft_text": "hi", "shown": ca._shown(d.quote, d.quote_lines)}
    return {"invoice_id": "i1", "ref": "INV-1", "total": 100.0, "payment_method": "cash",
            "who": "x", "shown": ca._shown(d.inv, d.inv_lines)}


def _run(kind, payload):
    return asyncio.run(ca.execute(T, None, kind, payload))


def _acted(d):
    return d.sent or d.settled


KINDS = ("invoice_send", "quote_send", "invoice_mark_paid")


@pytest.mark.parametrize("kind", KINDS)
def test_unchanged_document_goes_ahead(docs, kind):
    out = _run(kind, _card(docs, kind))
    assert out.get("state") != "failed" and _acted(docs)


@pytest.mark.parametrize("kind", KINDS)
def test_a_new_total_is_refused_and_says_so(docs, kind):
    payload = _card(docs, kind)
    doc, lines = (docs.quote, docs.quote_lines) if kind == "quote_send" else (docs.inv, docs.inv_lines)
    doc["total"] = 250.0
    lines[0]["amount"] = 250.0
    out = _run(kind, payload)
    assert out["state"] == "failed" and not _acted(docs)
    assert "changed after I prepared this (it was £100.00, it is now £250.00)" in out["summary"]
    assert "I have not" in out["summary"] and "prepare it again" in out["summary"]


@pytest.mark.parametrize("kind", KINDS)
def test_same_total_but_different_lines_is_refused(docs, kind):
    payload = _card(docs, kind)
    lines = docs.quote_lines if kind == "quote_send" else docs.inv_lines
    lines[:] = [{"description": "Parts", "amount": 40.0}, {"description": "Labour", "amount": 60.0}]
    out = _run(kind, payload)
    assert out["state"] == "failed" and not _acted(docs)
    assert "lines or the customer's address" in out["summary"]


def test_a_changed_address_is_refused(docs):
    payload = _card(docs, "invoice_send")
    docs.inv["customer_email"] = "other@example.com"
    out = _run("invoice_send", payload)
    assert out["state"] == "failed" and not docs.sent


@pytest.mark.parametrize("kind", KINDS)
def test_a_legacy_card_without_a_snapshot_behaves_as_before(docs, kind):
    payload = _card(docs, kind)
    payload.pop("shown")
    (docs.quote if kind == "quote_send" else docs.inv)["total"] = 999.0
    out = _run(kind, payload)
    assert out.get("state") != "failed" and _acted(docs)


@pytest.mark.parametrize("kind", ("invoice_delete_draft", "quote_delete_draft"))
def test_delete_compares_lines_not_just_the_total(docs, monkeypatch, kind):
    doc, lines = (docs.inv, docs.inv_lines) if kind.startswith("invoice") else (docs.quote, docs.quote_lines)
    monkeypatch.setattr(db, "invoice_linked_quote_ref", lambda *a, **k: None)
    monkeypatch.setattr(ca, "_invoice_touched_clause", lambda i: None)
    monkeypatch.setattr(ca, "_quote_touched_clause", lambda q: None)
    deleted = []
    monkeypatch.setattr(db, "delete_draft_invoice", lambda *a, **k: deleted.append(1) or True)
    monkeypatch.setattr(db, "delete_draft_quote", lambda *a, **k: deleted.append(1) or True)
    ref = "ref"
    payload = {("invoice_id" if kind.startswith("invoice") else "quote_id"): doc["id"],
               "ref": ref, "total": 100.0, "who": "x", "lines": [{"description": "Work", "amount": 100.0}]}
    lines[:] = [{"description": "Other", "amount": 100.0}]       # same total, different line
    out = _run(kind, payload)
    assert out["state"] == "failed" and not deleted and "was changed since I prepared this" in out["summary"]
    lines[:] = [{"description": "Work", "amount": 100.0}]
    assert _run(kind, payload).get("state") != "failed" and deleted


def test_stagers_store_the_snapshot_and_no_text_in_it(docs, monkeypatch, caplog):
    monkeypatch.setattr(ca, "_resolve_invoice", lambda t, c, raw: docs.inv)
    monkeypatch.setattr(ca, "_resolve_quote", lambda t, c, raw: docs.quote)
    monkeypatch.setattr(payments, "tenant_can_take_payments", lambda *a, **k: False)
    monkeypatch.setattr(db, "get_invoice_settings", lambda *a, **k: {})
    monkeypatch.setattr(invoice_send, "invoice_email_body", lambda *a, **k: "body")
    monkeypatch.setattr(invoice_send, "configured_sign_off", lambda *a, **k: "")
    monkeypatch.setattr(quote_send, "quote_email_body", lambda *a, **k: "body")
    monkeypatch.setattr(quote_send, "quote_url", lambda i: "u")
    caplog.set_level(logging.DEBUG)
    for stager, action in ((ca._stage_invoice_send, {"invoice_id": "i1"}),
                           (ca._stage_quote_send, {"quote_id": "q1"}),
                           (ca._stage_invoice_mark_paid, {"invoice_id": "i1", "payment_method": "cash"})):
        _, payload, _, _ = stager(T, None, action)
        assert set(payload["shown"]) == {"total", "lines_n", "lines_hash", "to"}
        assert SECRET not in repr(payload["shown"])
    assert SECRET not in caplog.text
