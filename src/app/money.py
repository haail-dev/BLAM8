"""Money — the owner's cashflow read: what's owed, what's late, who owes it.

Deliberately NOT accounting. It reports on invoices BLAM8 already holds; it does not
keep a ledger, reconcile a bank feed or go near a VAT return. That boundary is the
whole strategy (docs/modules/module-invoicing.md, "integrate where we don't compete"): the
customer's bookkeeper keeps the books, we hand over clean source documents and show
the owner the one view they actually ask for — "who hasn't paid me?"

⚠️ Do not rename this surface to "Accounting". The word creates a compliance
expectation (HMRC-recognised software, VAT filing) that this deliberately does not meet.

Pure functions over invoice rows, so the maths is testable without a database. Totals
reuse db.summarize_invoice_rows — the same bucketing the admin revenue card and the CRM
contact card use, so three surfaces can never disagree about what "outstanding" means.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta

# Issued and not settled. 'draft' is excluded on purpose — an unissued invoice is not
# money anyone owes yet — and cancelled/void are not debts at all.
OWING_STATUSES = ("sent", "overdue", "partially_paid")


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _outstanding(inv: dict) -> float:
    """What's still owed on one invoice: total less anything already paid, floored at
    zero so an overpayment can never show as negative debt."""
    return max(round(_f(inv.get("total")) - _f(inv.get("amount_paid")), 2), 0.0)


def days_late(inv: dict, today: date) -> int:
    """Days past the due date, 0 if not yet due or undated. Read from due_date rather
    than the 'overdue' status, because the status only flips when the 08:45 job runs —
    this view must be right the moment it's opened."""
    due = (inv.get("due_date") or "")[:10]
    if not due:
        return 0
    try:
        return max((today - date.fromisoformat(due)).days, 0)
    except ValueError:
        return 0


def owing(invoices: list[dict]) -> list[dict]:
    return [i for i in invoices if (i.get("status") or "") in OWING_STATUSES]


def overdue_rows(invoices: list[dict], today: date) -> list[dict]:
    """Unsettled invoices past their due date, latest first — the chase list."""
    rows = []
    for inv in owing(invoices):
        late = days_late(inv, today)
        if late <= 0:
            continue
        rows.append({"id": inv.get("id"), "invoice_ref": inv.get("invoice_ref"),
                     "customer": inv.get("customer_name") or inv.get("customer_email") or "—",
                     "email": (inv.get("customer_email") or "").strip().lower(),
                     "due_date": (inv.get("due_date") or "")[:10],
                     "days_late": late, "outstanding": _outstanding(inv),
                     # The chase trail: how many reminders have gone and when
                     # the last one went — the page shows the machinery working.
                     "reminders_sent": int(inv.get("reminders_sent") or 0),
                     "last_reminder_at": (inv.get("last_reminder_at") or "")[:10],
                     # Which milestones already fired — the overdue board reads
                     # the ladder's next move from this stamp.
                     "overdue_notified_days": int(inv.get("overdue_notified_days") or 0)})
    rows.sort(key=lambda r: (-r["days_late"], -r["outstanding"]))
    return rows


def outstanding_by_customer(invoices: list[dict], limit: int = 10) -> list[dict]:
    """Who owes what, biggest first. Grouped on email when there is one, else the
    display name — the same key rule as the generate flow's draft_key, so a customer
    can't appear twice under two spellings of nothing."""
    groups: dict[str, dict] = {}
    for inv in owing(invoices):
        email = (inv.get("customer_email") or "").strip().lower()
        name = (inv.get("customer_name") or "").strip()
        key = email or name.lower() or "—"
        g = groups.setdefault(key, {"customer": name or email or "—", "email": email,
                                    "outstanding": 0.0, "count": 0, "worst_days_late": 0})
        g["outstanding"] = round(g["outstanding"] + _outstanding(inv), 2)
        g["count"] += 1
    rows = sorted(groups.values(), key=lambda g: -g["outstanding"])
    return rows[:limit]


def annotate_worst_late(rows: list[dict], overdue: list[dict]) -> list[dict]:
    """Stamp each customer row with its oldest overdue invoice, so the list shows not
    just who owes most but who has been sitting on it longest. Matched on the SAME
    key the grouping uses — email first, name as the fallback: matching by name
    alone understated a customer whose invoices carry two spellings of them
    (live e2e 2026-09-01: one email, names 'Kris'/'Dave' → 19d shown, 23d true)."""
    worst: dict[str, int] = {}
    for o in overdue:
        key = o.get("email") or (o.get("customer") or "").strip().lower()
        worst[key] = max(worst.get(key, 0), o["days_late"])
    for r in rows:
        key = r.get("email") or (r["customer"] or "").strip().lower()
        r["worst_days_late"] = worst.get(key, 0)
    return rows


# The chase ladder's milestones — mirrored from app/jobs/check_overdue_invoices
# (a cross-check test keeps the two equal) so this module stays pure and
# import-light. The overdue page STATES the machine's next move from these.
CHASE_MILESTONES = (7, 14, 30)

AGE_BANDS = ((1, 7, "1–7 days late"), (8, 14, "8–14 days late"),
             (15, 30, "15–30 days late"), (31, None, "over a month late"))


def age_buckets(od_rows: list[dict]) -> list[dict]:
    """The overdue book by age: how much of the debt is fresh and how much has
    been sitting — the page's one-glance risk read. Empty bands are dropped."""
    out = []
    for lo, hi, label in AGE_BANDS:
        rows = [r for r in od_rows
                if r["days_late"] >= lo and (hi is None or r["days_late"] <= hi)]
        if rows:
            out.append({"label": label, "count": len(rows),
                        "total": round(sum(r["outstanding"] for r in rows), 2)})
    return out


def next_chase(row: dict, today: date) -> dict:
    """What the proactive engine will do NEXT about one overdue invoice, and
    when — the platform's promise made legible ('reminders are obsolete': the
    page doesn't ask you to remember, it tells you what's already scheduled).
    Mirrors check_overdue_invoices exactly: ≥3 reminders escalates to a phone
    call, else the first un-notified milestone fires the morning after it is
    crossed."""
    if int(row.get("reminders_sent") or 0) >= 3:
        return {"kind": "escalate",
                "label": "3 reminders sent — this one's a phone call"}
    stamp = int(row.get("overdue_notified_days") or 0)
    nxt = next((m for m in CHASE_MILESTONES if m > stamp), None)
    if nxt is None:
        return {"kind": "ladder_done",
                "label": "every milestone chased"}
    if row["days_late"] >= nxt:
        return {"kind": "imminent", "label": "next morning run"}
    try:
        when = date.fromisoformat(row["due_date"]) + timedelta(days=nxt)
    except (TypeError, ValueError):
        return {"kind": "imminent", "label": "next morning run"}
    return {"kind": "scheduled", "date": when.isoformat(), "milestone": nxt}


def overdue_view(invoices: list[dict], today: date) -> dict:
    """Everything /app/money/overdue renders: the chase board — age buckets,
    each debt's chase trail and the engine's stated next move."""
    rows = overdue_rows(invoices, today)
    for r in rows:
        r["next_chase"] = next_chase(r, today)
    return {"rows": rows, "buckets": age_buckets(rows),
            "total": round(sum(r["outstanding"] for r in rows), 2),
            "count": len(rows),
            "worst": max((r["days_late"] for r in rows), default=0)}


def _receipts(invoices: list[dict]) -> list[dict]:
    """Money actually received: one row per SETTLED invoice, dated by paid_at.
    Partial payments have no payment date of their own, so they appear here
    when the invoice settles — the collected pages say so. days_to_pay uses
    the same sent_at→paid_at rule as db.summarize_invoice_rows."""
    out = []
    for inv in invoices:
        if (inv.get("status") or "") != "paid":
            continue
        paid = (inv.get("paid_at") or "")[:10]
        if len(paid) < 7:
            continue
        days = None
        if inv.get("sent_at") and inv.get("paid_at"):
            try:
                days = max((datetime.fromisoformat(inv["paid_at"])
                            - datetime.fromisoformat(inv["sent_at"])).days, 0)
            except ValueError:
                pass
        out.append({"id": inv.get("id"), "invoice_ref": inv.get("invoice_ref"),
                    "customer": inv.get("customer_name")
                    or inv.get("customer_email") or "—",
                    "email": (inv.get("customer_email") or "").strip().lower(),
                    "paid_on": paid, "month": paid[:7],
                    "amount": _f(inv.get("amount_paid")) or _f(inv.get("total")),
                    "days_to_pay": days})
    out.sort(key=lambda r: r["paid_on"], reverse=True)
    return out


def _receipt_totals(rows: list[dict]) -> dict:
    days = [r["days_to_pay"] for r in rows if r["days_to_pay"] is not None]
    return {"collected": round(sum(r["amount"] for r in rows), 2),
            "count": len(rows),
            "avg_days": round(sum(days) / len(days), 1) if days else None}


def _month_label(key: str) -> str:
    try:
        return date(int(key[:4]), int(key[5:7]), 1).strftime("%B %Y")
    except (TypeError, ValueError):
        return key


def collections_view(invoices: list[dict], month: str | None = None) -> dict:
    """Everything /app/money/collected renders. All-time mode: the collection
    story month by month plus who pays you most. Month mode (`month` =
    'YYYY-MM'): that month's payments one by one, with prev/next set only
    where a neighbouring month actually holds receipts."""
    receipts = _receipts(invoices)
    if month:
        rows = [r for r in receipts if r["month"] == month]
        keys = sorted({r["month"] for r in receipts})
        older = [k for k in keys if k < month]
        newer = [k for k in keys if k > month]
        return {"mode": "month", "month": month,
                "label": _month_label(month), "rows": rows,
                "totals": _receipt_totals(rows),
                "prev": older[-1] if older else None,
                "next": newer[0] if newer else None}
    by_month: dict[str, list[dict]] = {}
    for r in receipts:
        by_month.setdefault(r["month"], []).append(r)
    months = [{"key": k, "label": _month_label(k), **_receipt_totals(by_month[k])}
              for k in sorted(by_month, reverse=True)]
    payers: dict[str, dict] = {}
    for r in receipts:
        key = r["email"] or r["customer"].lower()
        g = payers.setdefault(key, {"customer": r["customer"],
                                    "collected": 0.0, "count": 0})
        g["collected"] = round(g["collected"] + r["amount"], 2)
        g["count"] += 1
    top = sorted(payers.values(), key=lambda g: -g["collected"])[:5]
    return {"mode": "all", "months": months, "top_payers": top,
            "totals": _receipt_totals(receipts)}


def quote_pipeline(quotes: list[dict], invoices: list[dict] | None = None) -> dict:
    """Money not yet won (docs/modules/module-quotes.md Phase 2): sent quotes awaiting an
    answer, and accepted work that hasn't been invoiced yet — the two buckets
    upstream of everything else on this page. Pure over quote rows.

    "Not yet invoiced" means the BALANCE: a deposit invoice already raised
    against an accepted quote comes off its total (live 2026-09-03: two accepted
    quotes with deposits out read as £1,455 still to invoice when £727.50 of it
    was already on invoices). A full conversion flips the quote to `invoiced`
    upstream, so only deposits need netting here. Cancelled invoices don't
    count — that money still has to be billed."""
    awaiting = [q for q in quotes or [] if (q.get("status") or "") == "sent"]
    accepted = [q for q in quotes or [] if (q.get("status") or "") == "accepted"]
    inv_total = {i.get("id"): _f(i.get("total")) for i in invoices or []
                 if (i.get("status") or "") not in ("cancelled", "void")}
    deposits = 0.0
    balance = 0.0
    for q in accepted:
        dep = inv_total.get(q.get("deposit_invoice_id"), 0.0) if q.get("deposit_invoice_id") else 0.0
        deposits += dep
        balance += max(_f(q.get("total")) - dep, 0.0)
    return {
        "awaiting_count": len(awaiting),
        "awaiting_value": round(sum(_f(q.get("total")) for q in awaiting), 2),
        "accepted_count": len(accepted),
        "accepted_value": round(balance, 2),
        "accepted_deposits": round(deposits, 2),
    }


def accountant_months(invoices: list[dict], limit: int = 6) -> list[dict]:
    """The month-by-month handover: which months hold invoices worth giving the
    bookkeeper, and the export window for each. Grouped on issue_date — the same
    field invoice_export.filter_invoices cuts on — so the row's count describes
    exactly what its download will contain. Invoices with no issue_date can't be
    bucketed into a month and are left to the all-invoices export on /app/invoices."""
    groups: dict[str, dict] = {}
    for inv in invoices:
        issued = (inv.get("issue_date") or "")[:10]
        if len(issued) < 7:
            continue
        g = groups.setdefault(issued[:7], {"count": 0, "invoiced": 0.0})
        g["count"] += 1
        g["invoiced"] = round(g["invoiced"] + _f(inv.get("total")), 2)
    rows = []
    for key in sorted(groups, reverse=True)[:limit]:
        try:
            y, m = int(key[:4]), int(key[5:7])
            first = date(y, m, 1)
        except ValueError:
            continue
        rows.append({"key": key,
                     # Full month + full year: "September 2026" — "Sep 26" reads
                     # like a date on a page that is full of dates.
                     "label": first.strftime("%B %Y"),
                     "date_from": first.isoformat(),
                     "date_to": date(y, m, monthrange(y, m)[1]).isoformat(),
                     **groups[key]})
    return rows


def tax_quarter(d: date) -> tuple[date, date]:
    """The MTD Income Tax standard quarter containing `d`: 6 Apr–5 Jul,
    6 Jul–5 Oct, 6 Oct–5 Jan, 6 Jan–5 Apr. Shifting back 5 days turns the
    6th-to-5th windows into whole calendar quarters of the shifted date, so the
    arithmetic needs no boundary cases."""
    s = d - timedelta(days=5)
    tax_year = s.year if s.month >= 4 else s.year - 1
    qi = ((s.month - 4) % 12) // 3                      # 0..3 within the tax year
    start_month = (4, 7, 10, 1)[qi]
    start = date(tax_year + (1 if qi == 3 else 0), start_month, 6)
    end_month = (7, 10, 1, 4)[qi]
    end = date(start.year + (1 if end_month < start_month else 0), end_month, 5)
    return start, end


def quarter_deadline(end: date) -> date:
    """When the quarterly update is due: the 7th of the month after the quarter
    ends (5 Jul → 7 Aug, 5 Jan → 7 Feb)."""
    return date(end.year + (1 if end.month == 12 else 0),
                1 if end.month == 12 else end.month + 1, 7)


def mtd_quarters(invoices: list[dict], today: date, limit: int = 4) -> list[dict]:
    """The quarterly handover: invoices bucketed into MTD Income Tax standard
    quarters, newest first — the income side of the update, cut on issue_date
    exactly like accountant_months so each row's download matches its face.
    BLAM8 deliberately does NOT file: the bookkeeper (or bridging software)
    takes the export from here — see src/app/money.py's module docstring for why
    this surface never promises compliance."""
    groups: dict[str, dict] = {}
    for inv in invoices:
        issued = (inv.get("issue_date") or "")[:10]
        try:
            start, end = tax_quarter(date.fromisoformat(issued))
        except ValueError:
            continue
        g = groups.setdefault(start.isoformat(), {
            "count": 0, "invoiced": 0.0, "start": start, "end": end})
        g["count"] += 1
        g["invoiced"] = round(g["invoiced"] + _f(inv.get("total")), 2)
    rows = []
    for key in sorted(groups, reverse=True)[:limit]:
        g = groups[key]
        start, end = g["start"], g["end"]
        tax_year = start.year if start.month >= 4 else start.year - 1
        qi = {4: 1, 7: 2, 10: 3, 1: 4}[start.month]
        rows.append({
            "key": key,
            "label": f"Q{qi} {tax_year}-{(tax_year + 1) % 100:02d}",
            "date_from": start.isoformat(), "date_to": end.isoformat(),
            "deadline": quarter_deadline(end).isoformat(),
            # ended → the filing window is open and the deadline is the story;
            # still running → say when it closes instead.
            "ended": end < today,
            "due_open": end < today <= quarter_deadline(end),
            "count": g["count"], "invoiced": g["invoiced"]})
    return rows


def view(invoices: list[dict], summary_all: dict, summary_month: dict,
         today: date, quotes: list[dict] | None = None) -> dict:
    """Everything /app/money renders. `summary_*` come from db.summarize_invoice_rows
    so this module never re-implements the totals."""
    od = overdue_rows(invoices, today)
    by_customer = annotate_worst_late(outstanding_by_customer(invoices), od)
    this_month = today.isoformat()[:7]
    return {
        "month": summary_month, "all_time": summary_all,
        # Cash RECEIVED this month (paid_at-dated) — the truer read for the
        # "Collected this month" row: an August invoice paid on 3rd Sep is
        # September's money. Same maths as /app/money/collected, so the row
        # and its page can never disagree.
        "month_receipts": collections_view(invoices, month=this_month)["totals"],
        "this_month_key": this_month,
        "overdue": od,
        "overdue_total": round(sum(r["outstanding"] for r in od), 2),
        "by_customer": by_customer,
        "owed_total": round(sum(_outstanding(i) for i in owing(invoices)), 2),
        "owing_count": len(owing(invoices)),
        "quotes": quote_pipeline(quotes, invoices) if quotes is not None else None,
        "months": accountant_months(invoices),
        "quarters": mtd_quarters(invoices, today),
    }
