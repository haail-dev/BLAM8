"""Quotes module Phase 1 (docs/modules/module-quotes.md) — the quote doc + accept loop.

Pins the surfaces that matter:
  * quoting.py pure maths: booking → draft lines, deposit net (VAT never
    compounds), snapshot → invoice lines with the deposit credit
  * invoice_config quote defaults parse (blank = default, 0/garbage = none)
  * the public /q/{id} page: capability lookup (drafts/cancelled 404), viewed
    stamps once + push default ON, accept requires a typed name, acceptance
    races lose silently, deposit acceptance lands on /pay, decline + question
    → inbox needs_reply, expired quotes are read-only
  * the owner surface: line parser keeps six posted lists aligned and preserves
    the customer's optional-line ticks, convert copies the SNAPSHOT and credits
    the deposit, manual accept runs the same guarded path

Run: python -m pytest tests/test_quotes.py -q
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import FormData

import app.dashboard as dashboard
from app import db, invoice_config, push, quote_public, quoting
from app.auth import TenantCtx, require_onboarded
from app.config import settings
from app.main import app
from app.security import require_csrf

TENANT = "11111111-1111-1111-1111-111111111111"
QID = "22222222-2222-2222-2222-222222222222"
TOMORROW = (date.today() + timedelta(days=7)).isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()


def _quote(**over) -> dict:
    base = {"id": QID, "tenant_id": TENANT, "quote_ref": "QUO-1001",
            "customer_email": "pat@example.com", "customer_name": "Pat Verner",
            "status": "sent", "title": "Bathroom refit",
            "subtotal": 100.0, "vat": 20.0, "total": 120.0,
            "show_item_prices": True, "deposit_percent": None, "version": 1,
            "issue_date": "2026-07-30", "valid_until": TOMORROW,
            "viewed_at": None, "view_count": 0, "accepted_at": None,
            "accepted_name": None, "acceptance_snapshot": None,
            "declined_at": None, "decline_reason": None,
            "deposit_invoice_id": None, "invoice_id": None,
            "booking_id": None, "contact_id": None, "deal_id": None,
            "email_log_id": None, "intro": None, "notes": None,
            "reminders_sent": 0, "created_by": "owner"}
    return {**base, **over}


def _lines() -> list[dict]:
    return [
        {"id": "l1", "quote_id": QID, "description": "Supply and fit suite",
         "quantity": 1, "unit_price": 80.0, "amount": 80.0,
         "is_optional": False, "is_selected": True, "line_order": 0,
         "booking_id": None, "kb_entry_id": None},
        {"id": "l2", "quote_id": QID, "description": "Heated towel rail",
         "quantity": 1, "unit_price": 20.0, "amount": 20.0,
         "is_optional": True, "is_selected": True, "line_order": 1,
         "booking_id": None, "kb_entry_id": None},
    ]


SNAPSHOT = {"version": 1, "subtotal": 100.0, "vat": 20.0, "total": 120.0,
            "lines": [{"description": "Supply and fit suite", "quantity": 1,
                       "unit_price": 80.0, "amount": 80.0, "booking_id": "bk1",
                       "kb_entry_id": None, "is_optional": False},
                      {"description": "Heated towel rail", "quantity": 1,
                       "unit_price": 20.0, "amount": 20.0, "booking_id": None,
                       "kb_entry_id": None, "is_optional": True}]}


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    monkeypatch.setattr(db, "get_invoice_settings",
                        lambda t, client=None: {"business_name": "Thornbeck Ltd",
                                                "show_vat": True, "vat_rate": 20})
    monkeypatch.setattr(db, "get_tenant_config", lambda t, client=None: {})
    return TestClient(app, follow_redirects=False)


# ── quoting.py pure maths ─────────────────────────────────────────────────────
def test_lines_from_booking_uses_stored_quote():
    lines = quoting.lines_from_booking(
        {"id": "bk1", "job_type": "Boiler service", "booking_ref": "BK-1001",
         "quoted_amount": 95.5})
    assert lines == [{"description": "Boiler service (BK-1001)", "quantity": 1,
                      "unit_price": 95.5, "amount": 95.5, "booking_id": "bk1"}]


def test_lines_from_booking_without_price_never_invents_one():
    lines = quoting.lines_from_booking({"id": "bk2", "job_type": "Repair"})
    assert lines[0]["amount"] == 0.0


def test_deposit_net_prefers_snapshot_and_rejects_garbage():
    q = _quote(deposit_percent=25, subtotal=200.0,
               acceptance_snapshot={"subtotal": 100.0})
    assert quoting.deposit_net_amount(q) == 25.0        # snapshot, not live
    assert quoting.deposit_net_amount(_quote(deposit_percent=None)) is None
    assert quoting.deposit_net_amount(_quote(deposit_percent=0)) is None
    assert quoting.deposit_net_amount(_quote(deposit_percent=120)) is None
    assert quoting.deposit_net_amount(_quote(deposit_percent="nope")) is None


def test_invoice_lines_from_quote_reads_snapshot_and_credits_deposit():
    q = _quote(status="accepted", acceptance_snapshot=SNAPSHOT,
               deposit_invoice_ref="INV-1004")
    lines = quoting.invoice_lines_from_quote(q, deposit_net=30.0)
    assert [l["amount"] for l in lines] == [80.0, 20.0, -30.0]
    assert lines[0]["booking_id"] == "bk1"              # job link survives
    assert "INV-1004" in lines[-1]["description"]
    # No snapshot = not accepted = nothing to convert.
    assert quoting.invoice_lines_from_quote(_quote()) == []


def test_quote_defaults_clean_form():
    out = invoice_config.clean_form({"default_quote_valid_days": "14",
                                     "default_deposit_percent": "25"})
    assert out["default_quote_valid_days"] == 14
    assert out["default_deposit_percent"] == 25.0
    out = invoice_config.clean_form({})
    assert out["default_quote_valid_days"] == 30
    assert out["default_deposit_percent"] is None
    out = invoice_config.clean_form({"default_deposit_percent": "0"})
    assert out["default_deposit_percent"] is None       # 0 = "none", not 0%
    out = invoice_config.clean_form({"default_deposit_percent": "150"})
    assert out["default_deposit_percent"] is None


# ── the public /q page ────────────────────────────────────────────────────────
def test_q_404s_unknown_draft_cancelled_and_bad_ids(web, monkeypatch):
    monkeypatch.setattr(db, "get_quote_any_tenant", lambda i: None)
    assert web.get(f"/q/{QID}").status_code == 404
    monkeypatch.setattr(db, "get_quote_any_tenant", lambda i: _quote(status="draft"))
    assert web.get(f"/q/{QID}").status_code == 404
    monkeypatch.setattr(db, "get_quote_any_tenant", lambda i: _quote(status="cancelled"))
    assert web.get(f"/q/{QID}").status_code == 404
    assert web.get("/q/not-a-uuid").status_code == 404


def test_q_renders_and_stamps_viewed_with_push_default_on(web, monkeypatch):
    stamped, sent = [], []
    monkeypatch.setattr(db, "get_quote_any_tenant", lambda i: _quote())
    monkeypatch.setattr(db, "set_quote_viewed", lambda i, t: stamped.append(i) or True)
    monkeypatch.setattr(db, "list_quote_lines", lambda i, t, client=None: _lines())
    monkeypatch.setattr(push, "notify", lambda *a, **k: sent.append(k))
    r = web.get(f"/q/{QID}")
    assert r.status_code == 200
    assert "QUO-1001" in r.text and "Thornbeck Ltd" in r.text
    assert "Accept quote" in r.text and "Heated towel rail" in r.text
    assert stamped == [QID]
    # The research's most-loved feature ships ON by default (no config row).
    assert len(sent) == 1 and sent[0]["kind"] == "quote_viewed"


def test_q_viewed_is_always_recorded_and_the_opt_out_only_stops_the_buzz(
        web, monkeypatch):
    """The opt-out used to wrap this call, so a tenant with it off had no push
    AND no feed row — no record their customer had ever opened the quote (Kris
    2026-09-04). The page reports the view either way now; notify_prefs decides
    whether it interrupts anybody."""
    from app import notify_prefs

    sent = []
    monkeypatch.setattr(db, "get_quote_any_tenant", lambda i: _quote())
    monkeypatch.setattr(db, "set_quote_viewed", lambda i, t: True)
    monkeypatch.setattr(db, "list_quote_lines", lambda i, t, client=None: _lines())
    monkeypatch.setattr(push, "notify", lambda *a, **k: sent.append(k))
    monkeypatch.setattr(db, "get_tenant_config",
                        lambda t, client=None: {"notify_quote_viewed": False})
    web.get(f"/q/{QID}")
    assert len(sent) == 1 and sent[0]["kind"] == "quote_viewed"

    # On by default here, unlike invoices: a customer opening a quote is how you
    # know to follow up.
    assert notify_prefs.should_push({}, "quote_viewed") is True
    assert notify_prefs.should_push({"notify_quote_viewed": False},
                                    "quote_viewed") is False


def test_q_accepted_renders_snapshot_not_live_lines(web, monkeypatch):
    monkeypatch.setattr(db, "get_quote_any_tenant", lambda i: _quote(
        status="accepted", accepted_name="Pat Verner",
        accepted_at="2026-07-30T10:00:00+00:00",
        acceptance_snapshot={**SNAPSHOT, "total": 999.0}))
    called = []
    monkeypatch.setattr(db, "list_quote_lines",
                        lambda *a, **k: called.append(1) or [])
    r = web.get(f"/q/{QID}")
    assert r.status_code == 200
    assert "Accepted" in r.text and "Pat Verner" in r.text
    assert "£999.00" in r.text                 # snapshot totals, not the row's
    assert "Accept quote" not in r.text        # no second acceptance
    assert called == []                        # live lines never read


def test_q_expired_by_date_is_read_only(web, monkeypatch):
    monkeypatch.setattr(db, "get_quote_any_tenant",
                        lambda i: _quote(valid_until=YESTERDAY))
    monkeypatch.setattr(db, "set_quote_viewed", lambda i, t: False)
    monkeypatch.setattr(db, "list_quote_lines", lambda i, t, client=None: _lines())
    r = web.get(f"/q/{QID}")
    assert "expired" in r.text and "Accept quote" not in r.text
    accepted = []
    monkeypatch.setattr(db, "accept_quote", lambda *a: accepted.append(a))
    r = web.post(f"/q/{QID}/accept", data={"accepted_name": "Pat"})
    assert r.status_code == 303 and accepted == []


def test_q_accept_requires_name_then_freezes_and_notifies(web, monkeypatch):
    monkeypatch.setattr(db, "get_quote_any_tenant", lambda i: _quote())
    r = web.post(f"/q/{QID}/accept", data={"accepted_name": "  "})
    assert "name_required" in r.headers["location"]

    accepted_row = _quote(status="accepted", accepted_name="Pat Verner",
                          acceptance_snapshot=SNAPSHOT, booking_id="bk1")
    calls, pushes, stamps = [], [], []
    monkeypatch.setattr(db, "accept_quote",
                        lambda i, t, n: calls.append((i, t, n)) or accepted_row)
    monkeypatch.setattr(db, "set_booking_quote",
                        lambda b, t, a, **k: stamps.append((b, a)))
    monkeypatch.setattr(push, "notify", lambda *a, **k: pushes.append(k))
    monkeypatch.setattr(db, "crm_contact_by_email", lambda *a, **k: None)
    monkeypatch.setattr(db, "crm_insert_activity", lambda *a, **k: None)
    r = web.post(f"/q/{QID}/accept", data={"accepted_name": "Pat Verner"})
    assert r.status_code == 303 and "msg=accepted" in r.headers["location"]
    assert calls == [(QID, TENANT, "Pat Verner")]
    assert stamps == [("bk1", 100.0)]          # net subtotal → quoted_amount
    assert any(p["kind"] == "quote_accepted" for p in pushes)


def test_q_accepted_page_says_what_happens_next(web, monkeypatch):
    # An acceptance is a commitment; the page must say what it set in motion
    # (live e2e 2026-09-01: "the acceptance screen looks a bit bare").
    accepted = _quote(status="accepted", accepted_name="Pat Verner",
                      accepted_at="2026-09-01T20:00:00+00:00",
                      deposit_invoice_id="dep-1", customer_reference="PO 4471",
                      acceptance_snapshot={"lines": [], "subtotal": 100.0,
                                           "vat": 20.0, "total": 120.0})
    monkeypatch.setattr(db, "get_quote_any_tenant", lambda i: accepted)
    monkeypatch.setattr(db, "get_invoice", lambda i, t, client=None: {
        "id": "dep-1", "invoice_ref": "INV-1007", "status": "sent",
        "total": 30.0})
    r = web.get(f"/q/{QID}")
    assert r.status_code == 200
    assert "Accepted by Pat Verner on 1st Sep 26" in r.text   # pretty, not ISO
    assert "What happens next" in r.text
    assert "INV-1007" in r.text and "/pay/dep-1" in r.text
    assert "your ref PO 4471" in r.text


def test_q_accepted_page_without_deposit_still_guides(web, monkeypatch):
    accepted = _quote(status="accepted", accepted_name="Pat Verner",
                      accepted_at="2026-09-01T20:00:00+00:00",
                      acceptance_snapshot={"lines": [], "subtotal": 100.0,
                                           "vat": 20.0, "total": 120.0})
    monkeypatch.setattr(db, "get_quote_any_tenant", lambda i: accepted)
    r = web.get(f"/q/{QID}")
    assert "What happens next" in r.text
    assert "will be in touch to arrange" in r.text
    assert "/pay/" not in r.text


def test_q_accept_race_loses_silently(web, monkeypatch):
    monkeypatch.setattr(db, "get_quote_any_tenant", lambda i: _quote())
    monkeypatch.setattr(db, "accept_quote", lambda *a: None)
    r = web.post(f"/q/{QID}/accept", data={"accepted_name": "Pat"})
    assert r.status_code == 303
    assert r.headers["location"] == f"/q/{QID}"


def test_q_accept_with_deposit_lands_on_pay(web, monkeypatch):
    accepted_row = _quote(status="accepted", accepted_name="Pat",
                          deposit_percent=25, acceptance_snapshot=SNAPSHOT)
    made, linked = [], []
    monkeypatch.setattr(db, "get_quote_any_tenant", lambda i: _quote(deposit_percent=25))
    monkeypatch.setattr(db, "accept_quote", lambda *a: accepted_row)
    monkeypatch.setattr(db, "crm_contact_by_email", lambda *a, **k: None)
    monkeypatch.setattr(db, "crm_insert_activity", lambda *a, **k: None)
    monkeypatch.setattr(db, "create_invoice",
                        lambda *a, **k: made.append((a, k)) or {"id": "dep-inv-1"})
    monkeypatch.setattr(db, "set_invoice_status", lambda *a, **k: None)
    monkeypatch.setattr(db, "set_invoice_payment_link", lambda *a, **k: None)
    monkeypatch.setattr(db, "link_quote_invoice",
                        lambda i, t, inv, **k: linked.append((inv, k)))
    r = web.post(f"/q/{QID}/accept", data={"accepted_name": "Pat"})
    assert r.status_code == 303 and r.headers["location"].endswith("/pay/dep-inv-1")
    (args, kwargs) = made[0]
    dep_lines = args[5]
    assert dep_lines[0]["amount"] == 25.0      # 25% of the net 100
    assert "Deposit" in dep_lines[0]["description"]
    assert linked == [("dep-inv-1", {"deposit": True})]


def test_q_decline_records_and_notifies(web, monkeypatch):
    pushes = []
    monkeypatch.setattr(db, "get_quote_any_tenant", lambda i: _quote())
    monkeypatch.setattr(db, "decline_quote",
                        lambda i, t, r: _quote(status="declined", decline_reason=r))
    monkeypatch.setattr(push, "notify", lambda *a, **k: pushes.append(k))
    r = web.post(f"/q/{QID}/decline", data={"reason": "went elsewhere"})
    assert "msg=declined" in r.headers["location"]
    assert pushes and pushes[0]["kind"] == "quote_declined"
    assert "went elsewhere" in pushes[0]["body"]


def test_q_question_lands_in_inbox_as_needs_reply(web, monkeypatch):
    logged, published = [], []
    monkeypatch.setattr(db, "get_quote_any_tenant", lambda i: _quote())
    monkeypatch.setattr(db, "insert_email_log",
                        lambda *a, **k: logged.append((a, k)) or ("log-1", True))
    monkeypatch.setattr(quote_public.events, "publish",
                        lambda *a, **k: published.append(a))
    r = web.post(f"/q/{QID}/question", data={"message": "Does that include tiling?"})
    assert "msg=question_sent" in r.headers["location"]
    (args, kwargs) = logged[0]
    assert kwargs["category"] == "needs_reply"
    assert "QUO-1001" in args[2]                       # subject names the quote
    assert "Does that include tiling?" in args[3]
    assert published                                    # live inbox pinged


def test_q_select_toggles_only_while_sent(web, monkeypatch):
    toggles = []
    monkeypatch.setattr(db, "get_quote_any_tenant", lambda i: _quote())
    monkeypatch.setattr(db, "set_quote_line_selected",
                        lambda i, t, l, s: toggles.append((l, s)) or True)
    web.post(f"/q/{QID}/select", data={"line_id": "l2", "selected": "1"})
    assert toggles == [("l2", True)]
    monkeypatch.setattr(db, "get_quote_any_tenant",
                        lambda i: _quote(status="accepted",
                                         acceptance_snapshot=SNAPSHOT))
    web.post(f"/q/{QID}/select", data={"line_id": "l2", "selected": ""})
    assert toggles == [("l2", True)]                   # accepted → untouched


# ── the owner surface ─────────────────────────────────────────────────────────
@pytest.fixture
def owner(monkeypatch):
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"fsm"})
    monkeypatch.setattr(dashboard.db, "visible_modules", lambda *a, **k: {"fsm"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    app.dependency_overrides[require_onboarded] = \
        lambda: TenantCtx(tenant_id=TENANT, client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app, follow_redirects=False)
    app.dependency_overrides.clear()


def test_quotes_list_renders_with_outstanding_strip(owner, monkeypatch):
    monkeypatch.setattr(dashboard.db, "list_quotes", lambda t, client=None: [
        _quote(), _quote(id="q2", quote_ref="QUO-1002", status="accepted",
                         total=80.0)])
    r = owner.get("/app/quotes")
    assert r.status_code == 200
    assert "QUO-1001" in r.text and "QUO-1002" in r.text
    assert "£120.00" in r.text and "waiting on 1 sent quote" in r.text


def test_quote_detail_renders_public_link_once_sent(owner, monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_quote", lambda i, t, client=None: _quote())
    monkeypatch.setattr(dashboard.db, "list_quote_lines",
                        lambda i, t, client=None: _lines())
    monkeypatch.setattr(dashboard.db, "get_invoice_settings",
                        lambda t, client=None: {"show_vat": True, "vat_rate": 20})
    r = owner.get(f"/app/quotes/{QID}")
    assert r.status_code == 200
    assert f"/q/{QID}" in r.text and "Resend quote" in r.text


def test_line_parser_keeps_alignment_and_customer_ticks():
    form = FormData([
        ("description", "Main job"), ("amount", "80"),
        ("optionality", "required"), ("booking_id", "bk1"),
        ("kb_entry_id", ""), ("is_selected", "1"),
        # A customer-deselected optional extra — the tick must survive an edit.
        ("description", "Towel rail"), ("amount", "20"),
        ("optionality", "optional"), ("booking_id", ""),
        ("kb_entry_id", "kb9"), ("is_selected", "0"),
        # Blank add-a-line row: dropped, but its hidden fields keep alignment.
        ("description", ""), ("amount", ""),
        ("optionality", "required"), ("booking_id", ""),
        ("kb_entry_id", ""), ("is_selected", "1"),
    ])
    lines = dashboard._quote_lines_from_form(form)
    assert len(lines) == 2
    assert lines[0] == {"description": "Main job", "amount": 80.0,
                        "is_optional": False, "is_selected": True,
                        "booking_id": "bk1", "kb_entry_id": None, "line_order": 0}
    assert lines[1]["is_optional"] and lines[1]["is_selected"] is False
    assert lines[1]["kb_entry_id"] == "kb9"


def test_convert_copies_snapshot_and_credits_deposit(owner, monkeypatch):
    q = _quote(status="accepted", acceptance_snapshot=SNAPSHOT,
               deposit_invoice_id="dep-1", contact_id="c1", deal_id="d1",
               customer_reference="PO 4471")
    made, linked = [], []
    monkeypatch.setattr(dashboard.db, "get_quote", lambda i, t, client=None: q)
    monkeypatch.setattr(dashboard.db, "get_invoice", lambda i, t, client=None: {
        "id": "dep-1", "invoice_ref": "INV-1004", "status": "paid",
        "subtotal": 25.0})
    monkeypatch.setattr(dashboard.db, "create_invoice",
                        lambda *a, **k: made.append((a, k)) or {"id": "new-inv"})
    monkeypatch.setattr(dashboard.db, "link_quote_invoice",
                        lambda i, t, inv, **k: linked.append(inv))
    r = owner.post(f"/app/quotes/{QID}/convert")
    assert r.status_code == 303
    assert r.headers["location"] == "/app/invoices/new-inv"
    (args, kwargs) = made[0]
    lines = args[5]
    assert [l["amount"] for l in lines] == [80.0, 20.0, -25.0]
    assert kwargs["contact_id"] == "c1" and kwargs["deal_id"] == "d1"
    assert "QUO-1001" in kwargs["notes"]
    # The named leak from the competitive research: the customer's PO rides
    # quote → invoice without being re-keyed.
    assert kwargs["customer_reference"] == "PO 4471"
    assert linked == ["new-inv"]


def test_their_reference_is_editable_in_any_state(owner, monkeypatch):
    # The PO usually arrives AFTER the quote goes out — locking the field to
    # draft would recreate the re-keying leak it exists to close.
    saved = []
    monkeypatch.setattr(dashboard.db, "get_quote",
                        lambda i, t, client=None: _quote(status="accepted"))
    monkeypatch.setattr(dashboard.db, "set_quote_reference",
                        lambda i, t, ref, client=None: saved.append(ref))
    r = owner.post(f"/app/quotes/{QID}/reference",
                   data={"customer_reference": "PO 4471"})
    assert r.status_code == 303 and saved == ["PO 4471"]


def test_convert_refuses_unaccepted(owner, monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_quote", lambda i, t, client=None: _quote())
    r = owner.post(f"/app/quotes/{QID}/convert")
    assert "msg=notaccepted" in r.headers["location"]


def test_manual_accept_runs_the_same_guarded_path(owner, monkeypatch):
    calls, effects, minted = [], [], []
    row = _quote(status="accepted", acceptance_snapshot=SNAPSHOT)
    monkeypatch.setattr(dashboard.db, "accept_quote",
                        lambda i, t, n: calls.append(n) or row)
    monkeypatch.setattr(quote_public, "accepted_side_effects",
                        lambda t, q: effects.append(q["id"]))
    monkeypatch.setattr(quote_public, "mint_deposit_invoice",
                        lambda t, q: minted.append(q["id"]) or None)
    r = owner.post(f"/app/quotes/{QID}/status",
                   data={"status": "accepted", "accepted_name": "Sam — by phone"})
    assert r.status_code == 303
    assert calls == ["Sam — by phone"]
    assert effects == [QID] and minted == [QID]


def test_edit_lines_locked_after_acceptance(owner, monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_quote", lambda i, t, client=None: _quote(
        status="accepted", acceptance_snapshot=SNAPSHOT))
    r = owner.post(f"/app/quotes/{QID}/lines", data={"description": "sneaky",
                                                     "amount": "999"})
    assert "msg=locked" in r.headers["location"]


def test_accept_emails_the_deposit_invoice(web, monkeypatch):
    """Live 2026-09-03 (Stone Mason): the deposit invoice was minted, stamped
    'sent', and never emailed — the customer's only sight of it was the /pay
    redirect. Acceptance now hands the minted invoice to the ONE invoice send
    path (PDF, bank details, footer, CRM line, promise hook)."""
    import asyncio
    from app import invoice_send
    sent = []
    monkeypatch.setattr(db, "get_quote",
                        lambda i, t, client=None: _quote(deposit_invoice_id="dep-1"))
    monkeypatch.setattr(db, "get_invoice",
                        lambda i, t, client=None: {"id": i, "invoice_ref": "INV-7",
                                                   "customer_email": "pat@x.co"})

    async def fake_send(tenant_id, inv, **k):
        sent.append((tenant_id, inv["id"], k.get("via")))
    monkeypatch.setattr(invoice_send, "send_invoice_email", fake_send)
    assert asyncio.run(quote_public.email_deposit_invoice(TENANT, QID)) is True
    assert sent == [(TENANT, "dep-1", "ai")]
    # no deposit configured → nothing to send, quietly
    monkeypatch.setattr(db, "get_quote", lambda i, t, client=None: _quote())
    assert asyncio.run(quote_public.email_deposit_invoice(TENANT, QID)) is False
    # a mail failure never unwinds the acceptance
    monkeypatch.setattr(db, "get_quote",
                        lambda i, t, client=None: _quote(deposit_invoice_id="dep-1"))

    async def boom(*a, **k):
        raise RuntimeError("smtp down")
    monkeypatch.setattr(invoice_send, "send_invoice_email", boom)
    assert asyncio.run(quote_public.email_deposit_invoice(TENANT, QID)) is False

    # and the public accept route calls it once a deposit was minted
    accepted_row = _quote(status="accepted", accepted_name="Pat", acceptance_snapshot=SNAPSHOT)
    monkeypatch.setattr(db, "get_quote_any_tenant", lambda i: _quote())
    monkeypatch.setattr(db, "accept_quote", lambda i, t, n: accepted_row)
    monkeypatch.setattr(quote_public, "accepted_side_effects", lambda t, q: None)
    monkeypatch.setattr(quote_public, "mint_deposit_invoice", lambda t, q: "/pay/dep-1")
    emailed = []

    async def fake_email(t, qid):
        emailed.append(qid); return True
    monkeypatch.setattr(quote_public, "email_deposit_invoice", fake_email)
    r = web.post(f"/q/{QID}/accept", data={"accepted_name": "Pat"})
    assert r.status_code == 303 and r.headers["location"] == "/pay/dep-1"
    assert emailed == [QID]


def test_quotes_and_invoices_inherit_the_contacts_single_open_deal(monkeypatch):
    """Live 2026-09-03: QUO-1001 was created from the form for a contact with one
    open deal and carried deal_id NULL, so the accepted-quote stage nudge never
    fired and the deal sat at 'New enquiry'. One open deal → inherited; none or
    several → left to the owner."""
    deals = {"c1": [{"id": "d1", "status": "open"}, {"id": "d0", "status": "won"}],
             "c2": [{"id": "d2", "status": "open"}, {"id": "d3", "status": "open"}],
             "c3": []}
    monkeypatch.setattr(db, "crm_list_deals",
                        lambda t, contact_id=None, company_id=None, client=None: deals[contact_id])
    assert db.open_deal_id_for_contact(TENANT, "c1") == "d1"
    assert db.open_deal_id_for_contact(TENANT, "c2") is None      # ambiguous
    assert db.open_deal_id_for_contact(TENANT, "c3") is None
    assert db.open_deal_id_for_contact(TENANT, None) is None

    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "crm_list_deals", boom)
    assert db.open_deal_id_for_contact(TENANT, "c1") is None      # never blocks a quote


class _Activities:
    """A crm_activities stand-in: .table().select().eq().eq().not_.is_().limit().execute()"""

    def __init__(self, rows, boom=False):
        self.rows, self.boom, self.source_id = rows, boom, None

    def table(self, name):
        assert name == "crm_activities"
        return self

    def select(self, *a):
        return self

    def eq(self, col, val):
        if col == "source_id":
            self.source_id = val
        return self

    @property
    def not_(self):
        return self

    def is_(self, *a):
        return self

    def limit(self, n):
        return self

    def execute(self):
        if self.boom:
            raise RuntimeError("postgrest down")
        from types import SimpleNamespace
        return SimpleNamespace(data=self.rows.get(self.source_id, []))


def test_a_job_finds_its_own_deal_by_the_email_that_created_it(monkeypatch):
    """The walk the contact-wide guess could never do: quote → booking → the
    inbound that created it → the deal that email was clustered onto. Live
    2026-09-04, a plumbing customer with a repair job AND a gas-safety job got a
    quote with no deal at all, because "does this contact have exactly one open
    deal?" is false for every repeat customer."""
    acts = _Activities({"log-9": [{"deal_id": "d-cp12"}]})
    monkeypatch.setattr(db, "get_booking",
                        lambda bid, t, client=None: {"email_log_id": "log-9"}
                        if bid == "bk-3" else {"email_log_id": None})
    # straight from the email (the AI auto-draft path passes email_log_id itself)
    assert db.deal_id_for_job(TENANT, email_log_id="log-9", client=acts) == "d-cp12"
    # …or via the booking the quote is for
    assert db.deal_id_for_job(TENANT, booking_id="bk-3", client=acts) == "d-cp12"


def test_the_walk_declines_rather_than_guesses(monkeypatch):
    acts = _Activities({"log-9": [{"deal_id": "d-cp12"}]})
    monkeypatch.setattr(db, "get_booking",
                        lambda bid, t, client=None: {"email_log_id": None})
    # A portal or manually-added booking never came from an email.
    assert db.deal_id_for_job(TENANT, booking_id="bk-portal", client=acts) is None
    # Nothing to walk from at all.
    assert db.deal_id_for_job(TENANT, client=acts) is None
    assert db.deal_id_for_job(TENANT, email_log_id="   ", client=acts) is None
    # The email was never clustered onto a deal (action='none' — routine admin).
    assert db.deal_id_for_job(TENANT, email_log_id="log-nodeal", client=acts) is None
    # And a read failure never blocks the quote, like every other money-path read.
    assert db.deal_id_for_job(TENANT, email_log_id="log-9",
                              client=_Activities({}, boom=True)) is None


def test_the_job_deal_beats_the_contact_guess_and_falls_back_to_it(monkeypatch):
    """Ordering matters both ways: the job's own deal wins when there is one, and
    the old contact-wide inheritance still applies when the job can't be walked."""
    calls = []
    monkeypatch.setattr(db, "deal_id_for_job",
                        lambda t, booking_id=None, email_log_id=None, client=None:
                        calls.append(booking_id) or ("d-job" if booking_id == "bk-3" else None))
    monkeypatch.setattr(db, "open_deal_id_for_contact",
                        lambda t, cid, client=None: "d-contact")
    assert _quote_deal(monkeypatch, booking_id="bk-3") == "d-job"
    assert _quote_deal(monkeypatch, booking_id="bk-portal") == "d-contact"
    assert calls == ["bk-3", "bk-portal"]


def _quote_deal(monkeypatch, *, booking_id):
    """Run create_quote far enough to read the deal_id it decided on."""
    from types import SimpleNamespace
    written = {}

    class _Ins:
        def insert(self, row):
            if isinstance(row, dict):      # the record itself; a list = its lines
                written.update(row)
            return self

        def execute(self):
            return SimpleNamespace(data=[{**written, "id": "q1"}])

    class _Client:
        def table(self, name):
            return _Ins()

    monkeypatch.setattr(db, "get_invoice_settings", lambda t, client=None: {})
    monkeypatch.setattr(db, "_quote_totals", lambda t, lines, client=None: (10.0, 2.0, 12.0))
    monkeypatch.setattr(db, "next_quote_ref", lambda t, client=None: "QUO-1001")
    db.create_quote(TENANT, "k@x.co", "Kris", [], client=_Client(),
                    contact_id="c1", booking_id=booking_id)
    return written.get("deal_id")


def test_an_invoice_walks_the_same_chain(monkeypatch):
    """create_invoice carried the identical two lines, so it had the identical
    gap — the final invoice for a job with a sibling job got no deal either."""
    seen = []
    monkeypatch.setattr(db, "deal_id_for_job",
                        lambda t, booking_id=None, email_log_id=None, client=None:
                        seen.append(booking_id) or "d-job")
    monkeypatch.setattr(db, "open_deal_id_for_contact",
                        lambda t, cid, client=None: pytest.fail("the job's deal wins"))
    from types import SimpleNamespace
    written = {}

    class _Ins:
        def insert(self, row):
            if isinstance(row, dict):      # the record itself; a list = its lines
                written.update(row)
            return self

        def execute(self):
            return SimpleNamespace(data=[{**written, "id": "i1"}])

    class _Client:
        def table(self, name):
            return _Ins()

    monkeypatch.setattr(db, "get_invoice_settings", lambda t, client=None: {})
    monkeypatch.setattr(db, "next_invoice_ref", lambda t, client=None: "INV-1001")
    db.create_invoice(TENANT, "k@x.co", "Kris", None, None,
                      [{"description": "Job", "amount": 10.0}], client=_Client(),
                      contact_id="c1", booking_id="bk-3")
    assert seen == ["bk-3"] and written["deal_id"] == "d-job"


def test_accepting_a_quote_runs_the_promise_resolver(web, monkeypatch):
    """The 'provide a full written quote' promise stayed open through two
    quotes sent and accepted (live 2026-09-03). Send and accept now feed the
    same action-side resolver invoices and bookings use."""
    import asyncio
    from app import crm_intel
    seen = []

    async def fake_resolve(tenant_id, contact, action_text, client=None, ref=None):
        seen.append((contact, action_text, ref)); return {}
    monkeypatch.setattr(crm_intel, "resolve_from_action", fake_resolve)
    row = _quote(status="accepted", accepted_name="Pat Verner", title="Bathroom refit")
    asyncio.run(quote_public.resolve_accept_promises(TENANT, row))
    assert seen and seen[0][0] == "pat@example.com"
    assert "accepted quote QUO-1001" in seen[0][1] and "Pat Verner" in seen[0][1]
    # the quote itself rides along, so a cleared promise links to it (2026-09-20)
    assert seen[0][2][0] == "quote" and seen[0][2][2] == "QUO-1001" and seen[0][2][1]
    # a resolver failure never unwinds the acceptance
    async def boom(*a, **k):
        raise RuntimeError("llm down")
    monkeypatch.setattr(crm_intel, "resolve_from_action", boom)
    asyncio.run(quote_public.resolve_accept_promises(TENANT, row))   # no raise

    # the public accept route awaits it
    monkeypatch.setattr(db, "get_quote_any_tenant", lambda i: _quote())
    monkeypatch.setattr(db, "accept_quote", lambda i, t, n: row)
    monkeypatch.setattr(quote_public, "accepted_side_effects", lambda t, q: None)
    monkeypatch.setattr(quote_public, "mint_deposit_invoice", lambda t, q: None)
    called = []

    async def fake_hook(t, q):
        called.append(q["id"])
    monkeypatch.setattr(quote_public, "resolve_accept_promises", fake_hook)
    r = web.post(f"/q/{QID}/accept", data={"accepted_name": "Pat"})
    assert r.status_code == 303 and called == [QID]


def test_manual_accept_also_resolves_and_emails_the_deposit(owner, monkeypatch):
    row = _quote(status="accepted", acceptance_snapshot=SNAPSHOT)
    monkeypatch.setattr(dashboard.db, "accept_quote", lambda i, t, n: row)
    monkeypatch.setattr(quote_public, "accepted_side_effects", lambda t, q: None)
    monkeypatch.setattr(quote_public, "mint_deposit_invoice", lambda t, q: "/pay/dep-1")
    events = []

    async def fake_resolve(t, q):
        events.append("resolve")

    async def fake_email(t, qid):
        events.append("email"); return True
    monkeypatch.setattr(quote_public, "resolve_accept_promises", fake_resolve)
    monkeypatch.setattr(quote_public, "email_deposit_invoice", fake_email)
    r = owner.post(f"/app/quotes/{QID}/status", data={"status": "accepted", "accepted_name": "by phone"})
    assert r.status_code == 303 and events == ["resolve", "email"]


def test_sending_a_quote_runs_the_promise_resolver(monkeypatch):
    import asyncio
    from app import quote_send, crm_intel, mailbox, job_parties
    seen = []

    async def fake_resolve(tenant_id, contact, action_text, client=None, ref=None):
        seen.append((action_text, ref)); return {}

    async def fake_send(*a, **k):
        return None
    monkeypatch.setattr(crm_intel, "resolve_from_action", fake_resolve)
    monkeypatch.setattr(mailbox, "send_mail", fake_send)
    monkeypatch.setattr(job_parties, "cc_for_document", lambda *a, **k: [])
    monkeypatch.setattr(quote_send.db, "get_invoice_settings", lambda *a, **k: {})
    # a quote is only sent when it has lines (the no-lines guard, 1 Oct 2026)
    monkeypatch.setattr(quote_send.db, "list_quote_lines",
                        lambda *a, **k: [{"description": "Work", "amount": 10.0, "is_selected": True}])
    monkeypatch.setattr(quote_send.db, "set_quote_status", lambda *a, **k: None)
    monkeypatch.setattr(quote_send.db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(quote_send, "_log_sent_email", lambda *a, **k: None)
    monkeypatch.setattr(quote_send, "_crm_quote_activity", lambda *a, **k: None)
    monkeypatch.setattr(quote_send, "_portal_footer", lambda *a, **k: "")
    asyncio.run(quote_send.send_quote_email(TENANT, _quote(title="Bathroom refit")))
    assert seen and "Sent written quote QUO-1001" in seen[0][0] and "Bathroom refit" in seen[0][0]
    assert seen[0][1][0] == "quote" and seen[0][1][2] == "QUO-1001"   # the cleared-by link


def test_new_invoice_form_hint_reads_the_tenants_terms(owner, monkeypatch):
    """Kris 2026-09-04: the New invoice form still said "(blank = 14 days)" whatever
    the terms sentence said."""
    monkeypatch.setattr(dashboard, "_require_invoicing", lambda ctx: None)
    monkeypatch.setattr(dashboard.db, "get_invoice_settings",
                        lambda t, client=None: {"show_payment_terms": True,
                                                "payment_terms": "Payment due within 30 days of the invoice date."})
    r = owner.get("/app/invoices/new")
    assert r.status_code == 200 and "blank = 30 days, per your payment terms" in r.text
    monkeypatch.setattr(dashboard.db, "get_invoice_settings",
                        lambda t, client=None: {"show_payment_terms": True, "payment_terms": "on receipt"})
    assert "blank = on receipt" in owner.get("/app/invoices/new").text
