"""Office/scheduling invoicing — the appointment+stay line-builder (app/invoicing.office_*),
built but kept behind OFFICE_INVOICING_ENABLED so it stays greyed/off until switched on."""
from app import invoicing


def _b(id, contact, ref, amount, summary="Booking", **extra):
    return {"id": id, "contact": contact, "booking_ref": ref, "quoted_amount": amount,
            "summary": summary, "booking_date": "2026-06-20", "spaces": None,
            "engineer_id": None, **extra}


def test_office_drafts_group_by_customer_at_their_quote():
    rows = [
        _b("1", "sarah@x.co", "BK-1", 25.0, details={"Service": "Cut"}),
        _b("2", "sarah@x.co", "BK-2", 90.0, summary="Boarding Rex"),
        _b("3", "tom@x.co", "BK-3", 40.0),
        _b("4", "tom@x.co", "BK-4", None),                      # unpriced → line at 0
    ]
    drafts = invoicing.office_drafts(rows, vat_rate=0.20)
    by = {d["customer_email"]: d for d in drafts}
    assert by["sarah@x.co"]["subtotal"] == 115.0
    assert by["sarah@x.co"]["vat"] == 23.0 and by["sarah@x.co"]["total"] == 138.0
    assert by["sarah@x.co"]["count"] == 2
    # the service name from details drives the line description
    assert any("Cut" in l["description"] for l in by["sarah@x.co"]["lines"])
    assert by["tom@x.co"]["subtotal"] == 40.0                   # unpriced line adds 0
    assert drafts[0]["total"] >= drafts[-1]["total"]           # biggest first


def test_office_drafts_ignore_transport_and_engineer_rows():
    rows = [
        _b("1", "a@x.co", "BK-1", 10.0),
        {**_b("2", "a@x.co", "BK-2", 10.0), "spaces": 3},       # transport → skipped
        {**_b("3", "a@x.co", "BK-3", 10.0), "engineer_id": "e1"},  # engineer → skipped
    ]
    drafts = invoicing.office_drafts(rows, vat_rate=0.20)
    assert len(drafts) == 1 and drafts[0]["count"] == 1


def test_office_invoicing_is_live_since_the_extraction():
    # Flipped ON 2026-07-26 when invoicing became a standalone core module.
    assert invoicing.OFFICE_INVOICING_ENABLED is True
    assert invoicing.billable_module({"scheduling"}) == "scheduling"
    assert invoicing.office_invoicing_pending({"scheduling"}) is False
    # a real vertical still wins the dispatch order
    assert invoicing.billable_module({"scheduling", "fsm"}) == "fsm"
    assert invoicing._BUILDERS.get("scheduling") == "office_drafts_view"
