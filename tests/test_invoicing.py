"""Invoicing — grouping delivered jobs into invoices (TMS baseline).

No DB: delivered booking dicts + a rate card in, draft invoices out.

Run: python -m pytest tests/ -q
"""
from app.invoicing import _amount_for, drafts

RATE = {"base_charge": 20, "per_mile": 2, "per_pallet": 10, "per_drop": 5,
        "min_charge": 50, "fuel_pct": 10}


def _bk(bid, contact, spaces=2, quoted=None, **kw):
    return {"id": bid, "booking_ref": f"BK-{bid}", "booking_date": "2026-06-20",
            "summary": "job", "spaces": spaces, "contact": contact,
            "quoted_amount": quoted, **kw}


def test_amount_prefers_the_stored_quote():
    assert _amount_for(_bk("a", "x", quoted=99.5), RATE, {}) == 99.5


def test_amount_recomputes_when_never_quoted():
    # no postcodes → no miles: 20 base + 20 pallets + 5 drop = 45, +10% = 49.5, min floors to 50
    assert _amount_for(_bk("a", "x", spaces=2), RATE, {}) == 50.0


def test_drafts_group_by_customer_with_vat_biggest_first():
    out = drafts([
        _bk("a", "ops@acme.co", quoted=100),
        _bk("b", "ops@acme.co", quoted=50),
        _bk("c", "jo@beta.co", quoted=200),
        {"id": "office", "spaces": None, "contact": "x", "booking_date": "2026-06-20"},
    ], RATE, {})
    assert [d["customer_email"] for d in out] == ["jo@beta.co", "ops@acme.co"]   # by total desc
    acme = next(d for d in out if d["customer_email"] == "ops@acme.co")
    assert acme["subtotal"] == 150.0 and acme["vat"] == 30.0 and acme["total"] == 180.0
    assert acme["count"] == 2
    assert all(d["customer_email"] for d in out)   # the office row (spaces None) was dropped
