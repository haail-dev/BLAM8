"""Money — the cashflow view's maths and page.

Deliberately not accounting: it reports the invoices BLAM8 raised and what came back.
The totals reuse db.summarize_invoice_rows so this surface, the CRM contact card and the
admin revenue card can never disagree about what "outstanding" means.

Run: python -m pytest tests/test_money.py -q
"""
from datetime import date

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import money
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf

TODAY = date(2026, 7, 30)


def _inv(ref, status, total, due, **kw):
    return {"id": f"i-{ref}", "invoice_ref": ref, "status": status, "total": total,
            "due_date": due, "customer_name": kw.pop("name", "Client"),
            "customer_email": kw.pop("email", "c@example.com"), **kw}


# ── what counts as owed ───────────────────────────────────────────────────────
def test_draft_invoices_are_not_money_anyone_owes():
    # A draft has not been issued, so it is not a debt.
    rows = [_inv("INV-1", "draft", 100.0, "2026-07-01")]
    assert money.owing(rows) == []


def test_cancelled_and_void_are_not_debts():
    rows = [_inv("INV-1", "cancelled", 100.0, "2026-07-01"),
            _inv("INV-2", "void", 100.0, "2026-07-01")]
    assert money.owing(rows) == []


def test_sent_overdue_and_partially_paid_all_count_as_owed():
    rows = [_inv("INV-1", "sent", 100.0, "2026-08-30"),
            _inv("INV-2", "overdue", 50.0, "2026-07-01"),
            _inv("INV-3", "partially_paid", 80.0, "2026-07-20", amount_paid=30.0)]
    assert len(money.owing(rows)) == 3


def test_partially_paid_only_owes_the_remainder():
    rows = [_inv("INV-3", "partially_paid", 80.0, "2026-07-20", amount_paid=30.0)]
    v = money.view(rows, {}, {}, TODAY)
    assert v["owed_total"] == 50.0


def test_overpayment_never_shows_as_negative_debt():
    rows = [_inv("INV-1", "partially_paid", 80.0, "2026-07-20", amount_paid=95.0)]
    assert money.view(rows, {}, {}, TODAY)["owed_total"] == 0.0


# ── lateness ──────────────────────────────────────────────────────────────────
def test_days_late_reads_the_due_date_not_the_status():
    # The 'overdue' status only flips when the 08:45 job runs; this view has to be
    # right the moment it's opened.
    assert money.days_late(_inv("INV-1", "sent", 10.0, "2026-07-20"), TODAY) == 10


def test_not_yet_due_is_zero_days_late_not_negative():
    assert money.days_late(_inv("INV-1", "sent", 10.0, "2026-08-20"), TODAY) == 0


def test_missing_or_unparseable_due_date_is_treated_as_not_late():
    assert money.days_late({"due_date": None}, TODAY) == 0
    assert money.days_late({"due_date": "not-a-date"}, TODAY) == 0


def test_a_sent_invoice_past_its_due_date_appears_in_the_chase_list():
    rows = [_inv("INV-1", "sent", 100.0, "2026-07-20")]     # status not yet flipped
    od = money.overdue_rows(rows, TODAY)
    assert [r["invoice_ref"] for r in od] == ["INV-1"] and od[0]["days_late"] == 10


def test_chase_list_is_ordered_latest_first_then_biggest():
    rows = [_inv("INV-A", "overdue", 100.0, "2026-07-25", email="a@x.com"),
            _inv("INV-B", "overdue", 100.0, "2026-07-01", email="b@x.com"),
            _inv("INV-C", "overdue", 500.0, "2026-07-25", email="c@x.com")]
    assert [r["invoice_ref"] for r in money.overdue_rows(rows, TODAY)] == \
        ["INV-B", "INV-C", "INV-A"]


def test_paid_invoices_never_appear_in_the_chase_list():
    rows = [_inv("INV-1", "paid", 100.0, "2026-07-01", amount_paid=100.0)]
    assert money.overdue_rows(rows, TODAY) == []


# ── who owes ──────────────────────────────────────────────────────────────────
def test_outstanding_groups_by_customer_biggest_first():
    rows = [_inv("INV-1", "sent", 100.0, "2026-08-30", email="a@x.com", name="A"),
            _inv("INV-2", "sent", 50.0, "2026-08-30", email="a@x.com", name="A"),
            _inv("INV-3", "sent", 400.0, "2026-08-30", email="b@x.com", name="B")]
    got = money.outstanding_by_customer(rows)
    assert [(g["customer"], g["outstanding"], g["count"]) for g in got] == \
        [("B", 400.0, 1), ("A", 150.0, 2)]


def test_customers_group_case_insensitively_on_email():
    rows = [_inv("INV-1", "sent", 100.0, "2026-08-30", email="A@X.com", name="A"),
            _inv("INV-2", "sent", 50.0, "2026-08-30", email="a@x.com", name="A")]
    assert len(money.outstanding_by_customer(rows)) == 1


def test_customer_with_no_email_groups_on_name():
    rows = [_inv("INV-1", "sent", 100.0, "2026-08-30", email=None, name="Mrs Patel"),
            _inv("INV-2", "sent", 25.0, "2026-08-30", email=None, name="Mrs Patel")]
    got = money.outstanding_by_customer(rows)
    assert len(got) == 1 and got[0]["outstanding"] == 125.0


def test_who_owes_is_capped_by_limit():
    rows = [_inv(f"INV-{i}", "sent", 10.0 + i, "2026-08-30", email=f"{i}@x.com")
            for i in range(15)]
    assert len(money.outstanding_by_customer(rows, limit=5)) == 5


def test_worst_late_matches_on_email_not_name():
    # Live e2e 2026-09-01: two invoices, ONE email, two spellings of the
    # customer — grouping keys on email, so the worst-late stamp must too.
    rows = [_inv("INV-1", "overdue", 100.0, "2026-07-07", name="Dave Pearson",
                 email="one@x.com"),                                   # 23d late
            _inv("INV-2", "overdue", 50.0, "2026-07-11", name="Kris Deane",
                 email="one@x.com")]                                   # 19d late
    v = money.view(rows, {}, {}, TODAY)
    (g,) = v["by_customer"]
    assert g["worst_days_late"] == 23


def test_customer_rows_carry_their_worst_overdue_age():
    rows = [_inv("INV-1", "overdue", 100.0, "2026-07-01", email="a@x.com", name="A"),
            _inv("INV-2", "sent", 50.0, "2026-08-30", email="a@x.com", name="A")]
    v = money.view(rows, {}, {}, TODAY)
    assert v["by_customer"][0]["worst_days_late"] == 29


def test_a_customer_with_nothing_overdue_shows_zero_days_late():
    rows = [_inv("INV-1", "sent", 100.0, "2026-08-30", email="a@x.com", name="A")]
    v = money.view(rows, {}, {}, TODAY)
    assert v["by_customer"][0]["worst_days_late"] == 0


def test_view_totals_only_the_overdue_subset_for_the_overdue_tile():
    rows = [_inv("INV-1", "overdue", 100.0, "2026-07-01", email="a@x.com"),
            _inv("INV-2", "sent", 900.0, "2026-08-30", email="b@x.com")]
    v = money.view(rows, {}, {}, TODAY)
    assert v["overdue_total"] == 100.0 and v["owed_total"] == 1000.0


def test_view_is_empty_and_safe_with_no_invoices():
    v = money.view([], {}, {}, TODAY)
    assert v["owed_total"] == 0 and v["overdue"] == [] and v["by_customer"] == []
    assert v["months"] == []


# ── the overdue board (/app/money/overdue) ────────────────────────────────────
def test_chase_milestones_mirror_the_engine():
    # money.py stays pure, so it carries its own copy of the ladder — this
    # pins the two together: change one, this fails until you change both.
    from app.jobs.check_overdue_invoices import MILESTONES
    assert tuple(sorted(MILESTONES)) == money.CHASE_MILESTONES


def test_next_chase_states_the_engines_next_move():
    # 3 days late, nothing fired yet → the 7d milestone, dated due+7.
    row = {"days_late": 3, "due_date": "2026-07-27", "reminders_sent": 0,
           "overdue_notified_days": 0}
    nc = money.next_chase(row, TODAY)
    assert nc == {"kind": "scheduled", "date": "2026-08-03", "milestone": 7}
    # 20 days late with the 7d already stamped → 14 is crossed and unfired:
    # it stages on the next morning run.
    row = {"days_late": 20, "due_date": "2026-07-10", "reminders_sent": 1,
           "overdue_notified_days": 7}
    assert money.next_chase(row, TODAY)["kind"] == "imminent"
    # Three reminders sent → the engine's own rule: no fourth email.
    row = {"days_late": 40, "due_date": "2026-06-20", "reminders_sent": 3,
           "overdue_notified_days": 30}
    assert money.next_chase(row, TODAY)["kind"] == "escalate"
    # Ladder complete, still unpaid, under 3 reminders → says so honestly.
    row = {"days_late": 40, "due_date": "2026-06-20", "reminders_sent": 2,
           "overdue_notified_days": 30}
    assert money.next_chase(row, TODAY)["kind"] == "ladder_done"


def test_age_buckets_band_the_debt_and_drop_empty_bands():
    rows = [_inv("INV-1", "overdue", 100.0, "2026-07-27", email="a@x.com"),   # 3d
            _inv("INV-2", "overdue", 50.0, "2026-07-20", email="b@x.com"),    # 10d
            _inv("INV-3", "overdue", 500.0, "2026-06-01", email="c@x.com")]   # 59d
    ov = money.overdue_view(rows, TODAY)
    assert [(b["label"], b["total"]) for b in ov["buckets"]] == [
        ("1–7 days late", 100.0), ("8–14 days late", 50.0),
        ("over a month late", 500.0)]
    assert ov["total"] == 650.0 and ov["worst"] == 59
    assert ov["rows"][0]["next_chase"]["kind"] == "imminent"   # 59d, stamp 0


# ── the collected pages (/app/money/collected) ────────────────────────────────
def _paid(ref, total, paid_at, sent_at=None, **kw):
    return _inv(ref, "paid", total, "2026-07-01", paid_at=paid_at,
                sent_at=sent_at, amount_paid=kw.pop("amount_paid", total), **kw)


def test_collections_tell_the_story_month_by_month():
    rows = [_paid("INV-1", 100.0, "2026-07-14T10:00:00+00:00",
                  sent_at="2026-07-04T09:00:00+00:00", email="a@x.com", name="A"),
            _paid("INV-2", 50.0, "2026-07-20T10:00:00+00:00",
                  sent_at="2026-07-18T09:00:00+00:00", email="a@x.com", name="A"),
            _paid("INV-3", 400.0, "2026-06-02T10:00:00+00:00", email="b@x.com",
                  name="B"),
            _inv("INV-4", "sent", 900.0, "2026-08-30")]         # unpaid: not a receipt
    c = money.collections_view(rows)
    assert c["mode"] == "all"
    assert [(m["key"], m["collected"], m["count"]) for m in c["months"]] == [
        ("2026-07", 150.0, 2), ("2026-06", 400.0, 1)]
    assert c["months"][0]["avg_days"] == 6.0                    # (10 + 2) / 2
    assert c["totals"]["collected"] == 550.0
    # Top payers group on email, biggest first.
    assert [(p["customer"], p["collected"], p["count"]) for p in c["top_payers"]] == [
        ("B", 400.0, 1), ("A", 150.0, 2)]


def test_collections_month_mode_pages_between_real_months_only():
    rows = [_paid("INV-1", 100.0, "2026-07-14T10:00:00+00:00", email="a@x.com"),
            _paid("INV-2", 400.0, "2026-05-02T10:00:00+00:00", email="b@x.com")]
    c = money.collections_view(rows, month="2026-07")
    assert c["mode"] == "month" and c["label"] == "July 2026"
    assert [r["invoice_ref"] for r in c["rows"]] == ["INV-1"]
    assert c["prev"] == "2026-05" and c["next"] is None         # no empty June link
    # A month with no receipts still renders, empty and honest.
    c2 = money.collections_view(rows, month="2026-06")
    assert c2["rows"] == [] and c2["prev"] == "2026-05" and c2["next"] == "2026-07"


def test_partial_payment_is_not_a_receipt_until_it_settles():
    rows = [_inv("INV-1", "partially_paid", 80.0, "2026-07-20", amount_paid=30.0,
                 paid_at="2026-07-25T10:00:00+00:00")]
    assert money.collections_view(rows)["totals"]["count"] == 0


def test_view_month_receipts_date_by_settlement_not_issue():
    # An August invoice paid on 3rd July IS July's money — the row and its
    # page share money.collections_view, so they can never disagree.
    rows = [_paid("INV-1", 120.0, "2026-07-03T10:00:00+00:00",
                  issue_date="2026-06-14")]
    v = money.view(rows, {}, {}, TODAY)
    assert v["month_receipts"]["collected"] == 120.0
    assert v["this_month_key"] == "2026-07"


# ── the accountant handover ───────────────────────────────────────────────────
def test_months_group_on_issue_date_newest_first():
    rows = [_inv("INV-1", "paid", 100.0, "2026-07-14", issue_date="2026-06-30"),
            _inv("INV-2", "sent", 50.0, "2026-08-14", issue_date="2026-07-31"),
            _inv("INV-3", "paid", 25.0, "2026-08-01", issue_date="2026-07-01")]
    months = money.accountant_months(rows)
    assert [m["label"] for m in months] == ["July 2026", "June 2026"]
    assert months[0]["count"] == 2 and months[0]["invoiced"] == 75.0


def test_month_rows_carry_the_exact_export_window():
    # The row's from/to must be the same window invoice_export.filter_invoices
    # cuts on, so the count in the row describes exactly what downloads.
    rows = [_inv("INV-1", "paid", 100.0, "2026-03-01", issue_date="2026-02-11")]
    (m,) = money.accountant_months(rows)
    assert m["date_from"] == "2026-02-01" and m["date_to"] == "2026-02-28"  # not the 30th


def test_invoices_without_an_issue_date_do_not_invent_a_month():
    rows = [_inv("INV-1", "draft", 100.0, "2026-07-14", issue_date=None),
            _inv("INV-2", "draft", 100.0, "2026-07-14")]
    assert money.accountant_months(rows) == []


def test_tax_quarters_run_sixth_to_fifth():
    from datetime import date
    # 5 Jul is the last day of Q1; 6 Jul opens Q2.
    assert money.tax_quarter(date(2026, 7, 5)) == (date(2026, 4, 6), date(2026, 7, 5))
    assert money.tax_quarter(date(2026, 7, 6)) == (date(2026, 7, 6), date(2026, 10, 5))
    # Q3 crosses the calendar year; 6 Jan opens Q4 of the SAME tax year.
    assert money.tax_quarter(date(2026, 12, 15)) == (date(2026, 10, 6), date(2027, 1, 5))
    assert money.tax_quarter(date(2027, 1, 5)) == (date(2026, 10, 6), date(2027, 1, 5))
    assert money.tax_quarter(date(2027, 1, 6)) == (date(2027, 1, 6), date(2027, 4, 5))
    # Early April belongs to the PREVIOUS tax year's Q4.
    assert money.tax_quarter(date(2026, 4, 5)) == (date(2026, 1, 6), date(2026, 4, 5))


def test_quarter_deadline_is_the_seventh_of_the_next_month():
    from datetime import date
    assert money.quarter_deadline(date(2026, 7, 5)) == date(2026, 8, 7)
    assert money.quarter_deadline(date(2027, 1, 5)) == date(2027, 2, 7)   # year held


def test_mtd_quarters_group_on_issue_date_newest_first():
    rows = [_inv("INV-1", "paid", 100.0, "2026-08-14", issue_date="2026-07-04"),   # Q1
            _inv("INV-2", "sent", 50.0, "2026-09-14", issue_date="2026-07-06"),    # Q2
            _inv("INV-3", "paid", 25.0, "2026-09-01", issue_date="2026-08-20")]    # Q2
    quarters = money.mtd_quarters(rows, TODAY)
    assert [q["label"] for q in quarters] == ["Q2 2026-27", "Q1 2026-27"]
    assert quarters[0]["count"] == 2 and quarters[0]["invoiced"] == 75.0
    assert quarters[0]["date_from"] == "2026-07-06" and quarters[0]["date_to"] == "2026-10-05"


def test_an_ended_quarter_shows_its_filing_window():
    from datetime import date
    rows = [_inv("INV-1", "paid", 100.0, "2026-08-14", issue_date="2026-07-04")]   # Q1, ended 5 Jul
    (q,) = money.mtd_quarters(rows, date(2026, 8, 1))     # inside the window
    assert q["ended"] and q["due_open"] and q["deadline"] == "2026-08-07"
    (q2,) = money.mtd_quarters(rows, date(2026, 8, 20))   # deadline passed
    assert q2["ended"] and not q2["due_open"]
    (q3,) = money.mtd_quarters(rows, date(2026, 6, 1))    # quarter still running
    assert not q3["ended"] and not q3["due_open"]


def test_the_handover_list_is_capped_at_recent_months():
    rows = [_inv(f"INV-{n}", "paid", 10.0, "2026-07-14", issue_date=f"2026-{n:02d}-05")
            for n in range(1, 10)]
    months = money.accountant_months(rows)
    assert len(months) == 6
    assert months[0]["key"] == "2026-09" and months[-1]["key"] == "2026-04"


# ── the page ──────────────────────────────────────────────────────────────────
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"fsm"})
    monkeypatch.setattr(dashboard.db, "visible_modules", lambda *a, **k: {"fsm"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    # The pipeline strip (docs/modules/module-quotes.md Phase 2) reads quotes too.
    monkeypatch.setattr(dashboard.db, "list_quotes", lambda *a, **k: [])
    # The chase column reads staged-but-unapproved chase drafts.
    monkeypatch.setattr(dashboard.db, "list_pending_invoice_chases",
                        lambda *a, **k: [])
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_money_page_renders_the_chase_list(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "list_invoices", lambda *a, **k: [
        _inv("INV-1001", "overdue", 240.0, "2026-07-04", name="J. Whitlock",
             created_at="2026-06-20T09:00:00+00:00")])
    r = client.get("/app/money")
    assert r.status_code == 200
    assert "INV-1001" in r.text and "J. Whitlock" in r.text
    assert "Overdue" in r.text and "Who owes you" in r.text


def test_money_page_has_an_all_clear_state(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "list_invoices", lambda *a, **k: [])
    r = client.get("/app/money")
    assert r.status_code == 200
    assert "Nothing overdue" in r.text and "Nobody owes you anything" in r.text


def test_money_page_never_calls_itself_accounting(client, monkeypatch):
    # The word promises VAT filing / HMRC-recognised software. Keep it out.
    monkeypatch.setattr(dashboard.db, "list_invoices", lambda *a, **k: [])
    r = client.get("/app/money")
    assert "ccounting" not in r.text


def test_money_page_hands_each_month_to_the_accountant(client, monkeypatch):
    # The handover lives ON Money now, not buried under the invoices table: one
    # ruled row per month, each a plain GET of the existing export endpoint.
    monkeypatch.setattr(dashboard.db, "list_invoices", lambda *a, **k: [
        _inv("INV-1", "paid", 120.0, "2026-08-14", issue_date="2026-07-04",
             created_at="2026-07-04T09:00:00+00:00")])
    r = client.get("/app/money")
    assert "Hand to your accountant" in r.text and "July 2026" in r.text
    assert "/app/invoices/export.csv?format=accountant&from=2026-07-01&to=2026-07-31" in r.text
    assert "Xero" in r.text  # the format choice is on the page


def test_money_page_offers_the_mtd_quarter(client, monkeypatch):
    # The bookkeeper-channel promise: the income side of the quarterly update,
    # quarter-aligned, one tap — with the filing deadline stated.
    monkeypatch.setattr(dashboard.db, "list_invoices", lambda *a, **k: [
        _inv("INV-1", "paid", 120.0, "2026-08-14", issue_date="2026-07-10",
             created_at="2026-07-10T09:00:00+00:00")])
    r = client.get("/app/money")
    assert "MTD Income Tax quarters" in r.text and "Q2 2026-27" in r.text
    assert "/app/invoices/export.csv?format=accountant&from=2026-07-06&to=2026-10-05" in r.text
    # The boundary honesty line: BLAM8 holds income; someone else files.
    assert "your accountant or bridging software files from it" in r.text


def test_money_page_handover_has_an_empty_state(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "list_invoices", lambda *a, **k: [])
    r = client.get("/app/money")
    assert "Nothing to hand over yet" in r.text


def test_money_headline_rows_are_all_doors(client, monkeypatch):
    # Julius 2026-09-02: "Overdue, Collected this month & Collected all time
    # don't have their own pages to nav to." Now every headline row opens out.
    monkeypatch.setattr(dashboard.db, "list_invoices", lambda *a, **k: [])
    r = client.get("/app/money")
    assert 'href="/app/money/overdue"' in r.text
    assert 'href="/app/money/collected"' in r.text
    assert 'href="/app/money/collected?month=' in r.text
    assert "payments landed" in r.text or "payment landed" in r.text or \
        "0 payments landed" in r.text


def test_overdue_page_is_the_chase_board(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "list_invoices", lambda *a, **k: [
        _inv("INV-1001", "overdue", 240.0, "2026-07-04", name="J. Whitlock",
             reminders_sent=1, last_reminder_at="2026-07-12",
             overdue_notified_days=7)])
    r = client.get("/app/money/overdue")
    assert r.status_code == 200
    assert "What happens next" in r.text and "INV-1001" in r.text
    # 26d late with 7 stamped → 14 crossed and unfired: the board says the
    # machine moves at the next morning run.
    assert "next morning run" in r.text
    # The route runs on the REAL today, so the fixed 2026-07-04 due date only
    # ever gets later — the one band it stays in forever is the oldest.
    assert "over a month late" in r.text               # the age bands
    assert "no fourth email goes" in r.text            # the ladder, stated
    assert "ccounting" not in r.text


def test_overdue_page_has_an_all_clear_state(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "list_invoices", lambda *a, **k: [])
    r = client.get("/app/money/overdue")
    assert r.status_code == 200 and "Nothing overdue" in r.text


def test_collected_page_renders_both_modes(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "list_invoices", lambda *a, **k: [
        _inv("INV-1", "paid", 120.0, "2026-07-01", amount_paid=120.0,
             paid_at="2026-07-14T10:00:00+00:00",
             sent_at="2026-07-04T09:00:00+00:00", name="A. Payer")])
    r = client.get("/app/money/collected")
    assert r.status_code == 200
    assert "Collected all time" in r.text and "July 2026" in r.text
    assert "Who pays you most" in r.text and "A. Payer" in r.text
    assert "Typical time to get paid" in r.text
    r2 = client.get("/app/money/collected?month=2026-07")
    assert "Payments received" in r2.text and "INV-1" in r2.text
    assert "14th Jul 26" in r2.text                    # pretty dates, never ISO
    assert "10d" in r2.text                            # time to pay
    # A garbage month param falls back to the all-time view, never a 500.
    assert "Collected all time" in client.get(
        "/app/money/collected?month=drop-table").text


def test_money_is_in_the_app_menu(client, monkeypatch):
    # Hidden 07-31, RESTORED 2026-08-02 (Julius): the only nav door to quotes
    # + invoices — viewing/working money-in stays inside the quote-to-cash
    # boundary (never ledger/MTD, the bookkeeper channel's turf).
    monkeypatch.setattr(dashboard.db, "list_invoices", lambda *a, **k: [])
    r = client.get("/app/money")
    assert r.status_code == 200
    assert 'href="/app/money"' in r.text


def _settings_env(monkeypatch):
    # Enough of the Settings page's reads to render the Workspace hub.
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "module_active", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"provider": "microsoft"})
    monkeypatch.setattr(dashboard.db, "calendar_connected", lambda *a, **k: True)
    monkeypatch.setattr(dashboard.db, "get_tenant_config", lambda *a, **k: {})
    monkeypatch.setattr(dashboard.db, "list_plans", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.db, "get_connection_status", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.db, "list_mailboxes", lambda *a, **k: [])


def test_refer_a_business_entry_is_on_the_settings_hub(client, monkeypatch):
    # The funnel gap stays closed (an in-app door to /partner) — it moved from
    # the masthead menu to the Settings Workspace hub in the 2026-08-30 menu diet.
    _settings_env(monkeypatch)
    r = client.get("/app/settings")
    assert r.status_code == 200
    assert 'href="/partner"' in r.text and "Refer a business" in r.text
    # The exit door sits on the same hub (research 09-01: a dignified exit is
    # a trust feature nobody in the sector offers).
    assert 'href="/app/export.zip"' in r.text and "Leave with everything" in r.text
    # ...and the rest of the plumbing that left the masthead menu:
    for door in ("/app/knowledge", "/app/usage", "/app/agent-log",
                 "/app/requests", "/app/ignored"):
        assert f'href="{door}"' in r.text, door


def test_settings_hub_quotes_the_commission_rate_from_one_place(client, monkeypatch):
    from app import partners
    _settings_env(monkeypatch)
    r = client.get("/app/settings")
    assert f">{round(partners.DEFAULT_COMMISSION_RATE * 100)}%<" in r.text
