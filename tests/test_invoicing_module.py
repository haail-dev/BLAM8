"""Invoicing module — Move 1: shared surface + line-builder registry + modular settings
(docs/modules/module-invoicing.md). Pins the config logic, the per-vertical dispatch, and that the
shared pages render.

Run: python -m pytest tests/test_invoicing_module.py -q
"""
import asyncio
import base64

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import invoice_config, invoice_pdf, invoicing
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf


# ── modular invoice config ────────────────────────────────────────────────────
def test_settings_for_defaults_vat_off_for_fresh_tenant():
    # Since the standalone extraction (2026-07-26): VAT defaults OFF — most BLAM8
    # tenants aren't VAT registered; a registered one flips it on (rate pre-set 20%).
    s = invoice_config.settings_for(None)
    assert s["show_vat"] is False and s["vat_rate"] == 20.0
    assert invoice_config.effective_vat_rate(s) == 0.0


def test_settings_for_stored_row_still_wins():
    s = invoice_config.settings_for({"show_vat": True, "vat_rate": 20})
    assert invoice_config.effective_vat_rate(s) == 20.0      # a VAT-registered tenant


def test_effective_vat_rate_off_when_vat_toggled_off():
    s = invoice_config.settings_for({"show_vat": False, "vat_rate": 20})
    assert invoice_config.effective_vat_rate(s) == 0.0


def test_clean_form_types_toggles_and_rate():
    out = invoice_config.clean_form({"show_vat": "1", "vat_rate": "5",
                                     "business_name": "  Acme  ", "show_payment_terms": ""})
    assert out["show_vat"] is True and out["vat_rate"] == 5.0
    assert out["business_name"] == "Acme"
    assert out["show_payment_terms"] is False          # unticked checkbox → False


# ── invoice layout: section order (preview + drag-to-reorder, 2026-07-26) ────
def test_normalize_section_order_never_breaks():
    n = invoice_config.normalize_section_order
    assert n(None) == invoice_config.DEFAULT_SECTION_ORDER
    assert n("garbage,nonsense") == invoice_config.DEFAULT_SECTION_ORDER
    # a partial/reordered CSV keeps its order and appends what's missing
    assert n("footer_note,core") == [
        "footer_note", "core", "business_header", "payment_terms", "bank_details"]
    assert n(["bank_details", "bank_details", "core"])[:2] == ["bank_details", "core"]


def test_clean_form_persists_section_order_into_extras():
    out = invoice_config.clean_form({"section_order": "payment_terms,core,junk"})
    assert out["extras"]["section_order"][:2] == ["payment_terms", "core"]
    assert "junk" not in out["extras"]["section_order"]
    assert "extras" not in invoice_config.clean_form({})   # absent field → untouched


def test_settings_for_exposes_complete_section_order():
    s = invoice_config.settings_for({"extras": {"section_order": ["footer_note"]}})
    assert s["section_order"][0] == "footer_note"
    assert set(s["section_order"]) == set(invoice_config.SECTIONS)
    assert invoice_config.settings_for(None)["section_order"] == \
        invoice_config.DEFAULT_SECTION_ORDER


def test_pdf_honours_section_order_and_bank_details():
    inv = {"invoice_ref": "INV-1", "customer_email": "x@y.co", "subtotal": 100,
           "vat": 0, "total": 100}
    lines = [{"description": "job", "amount": 100, "date": "2026-06-10"}]
    s = invoice_config.settings_for({
        "bank_sort_code": "01-02-03", "bank_account_number": "12345678",
        "extras": {"section_order": ["footer_note", "core", "bank_details"]}})
    out = invoice_pdf.render(inv, lines, s)
    assert out[:4] == b"%PDF" and len(out) > 800


def _plain_invoice():
    inv = {"invoice_ref": "INV-1", "customer_email": "x@y.co", "subtotal": 100,
           "vat": 0, "total": 100}
    return inv, [{"description": "job", "amount": 100, "date": "2026-06-10"}]


@pytest.mark.parametrize("order", [None, ["footer_note", "payment_terms", "core"]])
def test_pdf_renders_payment_terms_and_footer_note_together(order):
    # fpdf2 2.8 leaves the cursor at the right margin after multi_cell, so two of
    # them back to back (no bank details between) raised "Not enough horizontal
    # space to render a single character" and the invoice email could not be sent
    # (live 2026-10-14, a bulk send of two invoices).
    inv, lines = _plain_invoice()
    raw = {"payment_terms": "Pay within 14 days.", "footer_note": "Thank you."}
    if order:
        raw["extras"] = {"section_order": order}
    out = invoice_pdf.render(inv, lines, invoice_config.settings_for(raw))
    assert out[:4] == b"%PDF" and len(out) > 800


@pytest.mark.parametrize("field", ["payment_terms", "footer_note"])
def test_pdf_renders_payment_terms_and_footer_note_each_alone(field):
    inv, lines = _plain_invoice()
    out = invoice_pdf.render(inv, lines, invoice_config.settings_for({field: "A short note."}))
    assert out[:4] == b"%PDF" and len(out) > 800


# ── the line-builder registry ─────────────────────────────────────────────────
def test_billable_module_picks_the_tenants_vertical():
    assert invoicing.billable_module({"scheduling", "tms"}) == "tms"
    assert invoicing.billable_module({"scheduling", "fsm"}) == "fsm"
    # Office invoicing is ON since the standalone extraction — scheduling tenants
    # generate from completed appointments/stays.
    assert invoicing.billable_module({"scheduling"}) == "scheduling"
    assert invoicing.billable_module({"email"}) is None      # no generate flow, surface still on


def test_draft_view_for_dispatches_to_the_right_builder(monkeypatch):
    monkeypatch.setattr(invoicing, "drafts_view", lambda t, pf, pt, client=None: ["TMS"])
    monkeypatch.setattr(invoicing, "fsm_drafts_view", lambda t, pf, pt, client=None: ["FSM"])
    monkeypatch.setattr(invoicing, "office_drafts_view", lambda t, pf, pt, client=None: ["OFFICE"])
    assert invoicing.draft_view_for("t", {"tms"}, "a", "b") == ["TMS"]
    assert invoicing.draft_view_for("t", {"fsm"}, "a", "b") == ["FSM"]
    assert invoicing.draft_view_for("t", {"scheduling"}, "a", "b") == ["OFFICE"]
    assert invoicing.draft_view_for("t", {"email"}, "a", "b") == []   # standalone-only tenant


# ── the shared surface renders ─────────────────────────────────────────────────
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"fsm"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    # The settings page reads tenant_config too (quote_auto_draft, Phase 3).
    monkeypatch.setattr(dashboard.db, "get_tenant_config", lambda *a, **k: {})
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_invoices_page_renders(client, monkeypatch):
    monkeypatch.setattr(dashboard.invoicing, "draft_view_for", lambda *a, **k: [
        {"customer_name": "Mrs Patel", "count": 2, "total": 276.0}])
    monkeypatch.setattr(dashboard.db, "list_invoices", lambda *a, **k: [])
    r = client.get("/app/invoices")
    assert r.status_code == 200 and "Mrs Patel" in r.text and "Generate" in r.text


def test_invoice_settings_page_renders(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_invoice_settings", lambda *a, **k: None)
    r = client.get("/app/invoices/settings")
    assert r.status_code == 200 and "Show VAT" in r.text


def test_invoice_detail_hides_vat_row_when_off(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_invoice", lambda *a, **k: {
        "id": "i1", "invoice_ref": "INV-1001", "customer_email": "x@y.co",
        "period_from": "2026-06-01", "period_to": "2026-06-30",
        "subtotal": 100, "vat": 0, "total": 100, "status": "draft"})
    monkeypatch.setattr(dashboard.db, "list_invoice_lines", lambda *a, **k: [
        {"description": "BK-1 · job", "amount": 100, "date": "2026-06-10"}])
    monkeypatch.setattr(dashboard.db, "get_invoice_settings", lambda *a, **k: {"show_vat": False})
    r = client.get("/app/invoices/i1")
    assert r.status_code == 200 and "INV-1001" in r.text
    assert "VAT (" not in r.text          # VAT row hidden when the tenant toggled it off


def test_old_vertical_invoice_urls_redirect(client):
    for url in ("/app/tms/invoices", "/app/fsm/invoices"):
        r = client.get(url, follow_redirects=False)
        assert r.status_code in (307, 308) and r.headers["location"] == "/app/invoices"


# ── Move 2: PDF + email send + edit ───────────────────────────────────────────
def test_pdf_renders_with_and_without_vat():
    inv = {"invoice_ref": "INV-1", "customer_email": "x@y.co", "subtotal": 100,
           "vat": 20, "total": 120, "period_from": "2026-06-01", "period_to": "2026-06-30"}
    lines = [{"description": "job", "amount": 100, "date": "2026-06-10"}]
    on = invoice_pdf.render(inv, lines, invoice_config.settings_for({"show_vat": True}))
    off = invoice_pdf.render(inv, lines, invoice_config.settings_for({"show_vat": False}))
    assert on[:4] == b"%PDF" and off[:4] == b"%PDF" and len(on) > 800


def test_send_mail_graph_attaches_the_pdf(monkeypatch):
    from app import mailbox
    captured = {}

    async def fake_access(tid, mid):
        return ("tok", "microsoft", {})

    async def fake_post(access, path, json=None):
        captured.update(json or {})
    monkeypatch.setattr(mailbox, "_access", fake_access)
    monkeypatch.setattr(mailbox.graph, "graph_post", fake_post)

    asyncio.run(mailbox.send_mail("t", "c@x.co", "Invoice INV-1", "body",
                                  attachments=[("INV-1.pdf", b"%PDF-1", "application/pdf")]))
    att = captured["message"]["attachments"][0]
    assert att["name"] == "INV-1.pdf" and att["@odata.type"] == "#microsoft.graph.fileAttachment"
    assert base64.b64decode(att["contentBytes"]) == b"%PDF-1"


def test_invoice_send_emails_pdf_and_marks_sent(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_invoice", lambda iid, tid, **k: {
        "id": iid, "invoice_ref": "INV-1001", "customer_email": "j@x.co", "status": "draft",
        "subtotal": 115, "vat": 23, "total": 138,
        "period_from": "2026-06-01", "period_to": "2026-06-30"})
    monkeypatch.setattr(dashboard.db, "list_invoice_lines", lambda *a, **k: [
        {"description": "BK-1 job", "amount": 115, "date": "2026-06-20"}])
    monkeypatch.setattr(dashboard.db, "get_invoice_settings", lambda *a, **k: None)
    sent = {}

    async def fake_send(tid, to, subject, body, mailbox_id=None, attachments=None,
                        cc=None):
        sent.update(to=to, subject=subject, att=attachments)
    monkeypatch.setattr(dashboard.mailbox, "send_mail", fake_send)
    statuses = []
    monkeypatch.setattr(dashboard.db, "set_invoice_status",
                        lambda iid, tid, st, **k: statuses.append(st))
    monkeypatch.setattr(dashboard.db, "log_agent_event", lambda *a, **k: None)

    r = client.post("/app/invoices/i1/send", follow_redirects=False)
    assert r.status_code == 303 and "msg=sent" in r.headers["location"]
    assert sent["to"] == "j@x.co" and sent["att"][0][0] == "INV-1001.pdf"
    assert sent["att"][0][1][:4] == b"%PDF"            # a real PDF was attached
    assert statuses == ["sent"]


def test_invoice_send_without_email_is_flagged(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_invoice", lambda iid, tid, **k: {
        "id": iid, "invoice_ref": "INV-2", "customer_email": None, "status": "draft"})
    r = client.post("/app/invoices/i2/send", follow_redirects=False)
    assert r.status_code == 303 and "msg=noemail" in r.headers["location"]


def test_invoice_edit_lines_parses_and_drops_blanks(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_invoice", lambda iid, tid, **k: {"id": iid, "status": "draft"})
    captured = {}
    monkeypatch.setattr(dashboard.db, "update_invoice_lines",
                        lambda iid, tid, lines, **k: captured.update(lines=lines))
    r = client.post("/app/invoices/i1/lines", follow_redirects=False, data={
        "description": ["Callout", "", "Parts"],         # middle blank → dropped
        "amount": ["60", "0", "abc"]})                   # junk amount → 0.0
    assert r.status_code == 303 and "msg=saved" in r.headers["location"]
    # booking_id is now preserved per row (the duplicate-invoice guard reads it); this
    # post carries no hidden booking_id fields, so both lines come through unlinked.
    assert captured["lines"] == [
        {"description": "Callout", "amount": 60.0, "booking_id": None},
        {"description": "Parts", "amount": 0.0, "booking_id": None}]


def test_invoice_edit_lines_blocked_when_not_draft(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_invoice", lambda iid, tid, **k: {"id": iid, "status": "sent"})
    r = client.post("/app/invoices/i1/lines", follow_redirects=False,
                    data=[("description", "x"), ("amount", "1")])
    assert r.status_code == 303 and "msg=locked" in r.headers["location"]


# ── settings-page preview (live pane + sample PDF, 2026-07-26) ───────────────
def test_invoice_detail_renders_sections_in_stored_order(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_invoice", lambda *a, **k: {
        "id": "i1", "invoice_ref": "INV-1001", "customer_email": "x@y.co",
        "subtotal": 100, "vat": 0, "total": 100, "status": "sent"})
    monkeypatch.setattr(dashboard.db, "list_invoice_lines", lambda *a, **k: [
        {"description": "BK-1 · job", "amount": 100}])
    monkeypatch.setattr(dashboard.db, "get_invoice_settings", lambda *a, **k: {
        "business_name": "Thornbeck Ltd",
        "extras": {"section_order": ["core", "footer_note", "business_header"]},
        "footer_note": "Thanks for your custom"})
    html = client.get("/app/invoices/i1").text
    # header dragged BELOW the footer note → renders after it
    assert html.index("Thanks for your custom") < html.index("Thornbeck Ltd")


def test_settings_preview_reflects_unsaved_form_state(client):
    r = client.post("/app/invoices/settings/preview", data={
        "business_name": "Unsaved Plumbing Co", "show_business_details": "1",
        "bank_sort_code": "01-02-03", "bank_account_number": "12345678",
        "section_order": "bank_details,core,business_header"})
    assert r.status_code == 200
    assert "Unsaved Plumbing Co" in r.text          # typed but never saved
    assert "INV-0000" in r.text                     # the sample invoice
    # the caption's "PDF invoice" submits the settings form to the PDF route,
    # so the PDF reflects unsaved changes exactly as the sample does
    assert "Sample invoice &amp;" in r.text and ">PDF invoice</button>" in r.text
    assert 'form="inv-settings-form" formaction="/app/invoices/settings/preview.pdf"' in r.text
    # bank details dragged first → render before the business header
    assert r.text.index("Pay by bank transfer") < r.text.index("Unsaved Plumbing Co")


def test_settings_preview_pdf_returns_a_pdf(client):
    r = client.post("/app/invoices/settings/preview.pdf",
                    data={"business_name": "Thornbeck"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/pdf")
    assert r.content[:4] == b"%PDF"


def test_settings_page_carries_layout_list(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_invoice_settings", lambda *a, **k: None)
    html = client.get("/app/invoices/settings").text
    assert "Invoice layout" in html and 'id="section-list"' in html
    assert 'name="section_order"' in html
    # Kris 2026-09-05: no separate "Sample PDF" button — the PDF is the
    # "PDF invoice" link in the preview caption (the fragment, below).
    assert "Sample PDF" not in html


def test_payment_terms_bare_number_reads_as_a_sentence():
    """Live 2026-09-03 (Stone Mason): payment_terms held "14" and the invoice
    page, PDF and email all printed a lone 14. A bare number (with or without
    'days') renders as the sentence it means, at read time AND at save time;
    the operator's own wording is never touched."""
    t = invoice_config.payment_terms_text
    assert t("14") == "Payment due within 14 days of the invoice date."
    assert t(" 30 days ") == "Payment due within 30 days of the invoice date."
    assert t("1 day") == "Payment due within 1 day of the invoice date."
    assert t("0") == "Payment due on receipt."
    assert t("Strictly 14 days, thank you.") == "Strictly 14 days, thank you."
    assert t("") is None and t(None) is None
    # read boundary: a stored "14" renders as words everywhere settings_for feeds
    assert invoice_config.settings_for({"payment_terms": "14"})["payment_terms"] == (
        "Payment due within 14 days of the invoice date.")
    # save boundary: the form's "14" is stored as the sentence
    assert invoice_config.clean_form({"payment_terms": "14"})["payment_terms"] == (
        "Payment due within 14 days of the invoice date.")
    assert invoice_config.clean_form({"payment_terms": ""})["payment_terms"] is None


def test_due_date_follows_the_payment_terms():
    """Live 2026-09-03: every invoice fell due 14 days out regardless of the
    terms sentence printed under its total. The due date is read from the
    terms: 'N days' → N, on receipt → today, hidden/blank/unparseable → 14."""
    from datetime import date
    issued = date(2026, 9, 3)
    s = lambda **k: invoice_config.settings_for({"show_payment_terms": True, **k})
    assert invoice_config.due_days(s(payment_terms="Payment due within 30 days of the invoice date.")) == 30
    assert invoice_config.due_days(s(payment_terms="7")) == 7          # bare number → sentence → 7
    assert invoice_config.due_days(s(payment_terms="Strictly 21 working days")) == 21
    assert invoice_config.due_days(s(payment_terms="Payment due on receipt.")) == 0
    assert invoice_config.due_days(s(payment_terms="Pay us when you can")) == 14
    assert invoice_config.due_days(s(payment_terms="")) == 14
    assert invoice_config.due_days(invoice_config.settings_for(
        {"show_payment_terms": False, "payment_terms": "60 days"})) == 14   # hidden terms don't bind
    assert invoice_config.due_days(None) == 14
    assert invoice_config.due_days(s(payment_terms="999 days")) == 365
    assert invoice_config.default_due_date(s(payment_terms="30 days"), issued) == "2026-10-03"
    assert invoice_config.default_due_date(s(payment_terms="on receipt"), issued) == "2026-09-03"
    # the settings-page sample invoice shows the same default
    inv, _ = invoice_config.sample_invoice(s(payment_terms="Payment due within 30 days of the invoice date."))         if hasattr(invoice_config, "sample_invoice") else (None, None)
    if inv:
        assert inv["due_date"] == (date.today() + __import__("datetime").timedelta(days=30)).isoformat()


def test_the_business_name_comes_prefilled_so_a_plain_save_finishes_the_step(client, monkeypatch):
    # Rehearsal 2026-09-25: step 9 "Getting paid" needs a name or bank details
    # to count as done; the name given at step 1 now fills the box.
    monkeypatch.setattr(dashboard.db, "get_invoice_settings", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"name": "Hartwell & Daughters Plumbing & Heating"})
    r = client.get("/app/invoices/settings")
    assert 'name="business_name" value="Hartwell &amp; Daughters Plumbing &amp; Heating"' in r.text
    monkeypatch.setattr(dashboard.db, "get_invoice_settings", lambda *a, **k: {"business_name": "Own Name Ltd"})
    assert 'value="Own Name Ltd"' in client.get("/app/invoices/settings").text   # a saved name always wins
