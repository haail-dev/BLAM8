"""The fix assistant sees the customer's live bookings (run #7, 2026-09-05).

Live: "Something's wrong" on a draft that said "his Saturday 08:30 assessment"
-> the assistant answered "the agent invented a specific time (08:30). Nothing
in your data mentions that time" and asked the owner "is 08:30 actually Finn's
assessment time?" -- BK-1003 sat at 08:30 in the diary; the fix context carried
the email and the draft but no bookings (run #6 P2 #9). The context now carries
{ref, date, window, status, summary} for the contact's live bookings and the
prompt says to check a time against them before calling it invented.

Run: python -m pytest tests/test_fix_context_live_bookings.py -q
"""
from app import concierge, concierge_routes as cr


def test_fix_context_carries_the_contacts_live_bookings(monkeypatch):
    monkeypatch.setattr(cr.db, "get_email_log_entry", lambda *a, **k: {
        "from_address": "kris@x.co", "subject": "Re: lessons", "body_in": "sick Thursday",
        "draft_reply": "his Saturday 08:30 assessment will need to move", "mailbox_id": "m1"})
    monkeypatch.setattr(cr.db, "get_tenant_config", lambda *a, **k: {})
    monkeypatch.setattr(cr.db, "get_scheduling_config", lambda *a, **k: {"intake_fields": []})
    seen = {}

    def fake_bookings(tenant_id, contact, client=None):
        seen["contact"] = contact
        return [{"booking_ref": "BK-1003", "booking_date": "2026-09-12", "window": "08:30",
                 "status": "confirmed", "summary": "Assessment swim - Finn Deane"}]
    monkeypatch.setattr(cr.db, "bookings_for_contact", fake_bookings)

    class Ctx:
        tenant_id = "t1"; client = None
    draft_ctx, *_ = cr._fix_context(Ctx(), "log-1")
    assert seen["contact"] == "kris@x.co"
    assert draft_ctx["live_bookings"] == [{"ref": "BK-1003", "date": "2026-09-12", "window": "08:30",
                                          "status": "confirmed", "summary": "Assessment swim - Finn Deane"}]


def test_fix_context_survives_a_diary_error(monkeypatch):
    monkeypatch.setattr(cr.db, "get_email_log_entry", lambda *a, **k: {"from_address": "a@b.c"})
    monkeypatch.setattr(cr.db, "get_tenant_config", lambda *a, **k: {})
    monkeypatch.setattr(cr.db, "get_scheduling_config", lambda *a, **k: {})

    def boom(*a, **k):
        raise RuntimeError("diary down")
    monkeypatch.setattr(cr.db, "bookings_for_contact", boom)

    class Ctx:
        tenant_id = "t1"; client = None
    draft_ctx, *_ = cr._fix_context(Ctx(), "log-1")
    assert draft_ctx["live_bookings"] == []


def test_fix_prompt_says_check_the_diary_before_blaming():
    text = concierge.build_fix_prompt({"live_bookings": [{"ref": "BK-1003", "window": "08:30"}]})
    assert "live_bookings" in text and "never ask the owner whether it is real" in text
    assert "BK-1003" in text
