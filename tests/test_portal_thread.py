"""The portal conversation reaches the inbox (Kris 2026-08-31: "the inbox has
no idea of the Portal conversation... the Portal chat should show in this
thread, maybe with a different colour... that text gets used in context for
the next emails").

Three contracts:
- portal chat turns persist per identified contact (codes masked, anonymous
  turns never stored) and interleave into the thread history as violet bubbles;
- the drafter replays them — _merge_portal_history splices the transcript into
  the email history by timestamp, in shapes thread_messages already speaks;
- a portal-raised exchange is actionable in place: the stage face says
  "Customer portal", and a mentioned booking resolves to a cancel button
  right above the reply pen (the follow-up email).

Run: python -m pytest tests/test_portal_thread.py -q
"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
import app.pipeline as pipeline
import app.portal as portal
from app import agent
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def _env(monkeypatch, items, portal_msgs=(), booking=None):
    monkeypatch.setattr(dashboard.db, "get_conversation", lambda *a, **k: [dict(i) for i in items])
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"name": "T"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "list_agent_trace", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "bookings_for_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "list_portal_messages",
                        lambda *a, **k: [dict(m) for m in portal_msgs])
    monkeypatch.setattr(dashboard.db, "booking_by_ref",
                        lambda *a, **k: dict(booking) if booking else None)


EMAIL_1 = {"kind": "inbound", "id": "e1", "status": "sent", "sent_via": "human",
           "body_in": "Can I book Friday?", "draft_reply": "Friday works — 10am?",
           "created_at": "2026-08-30T10:00:00", "subject": "Booking"}
PORTAL_Q = {"role": "customer", "content": "Can I cancel BK-1037 please?",
            "created_at": "2026-08-31T16:00:00"}
PORTAL_A = {"role": "assistant", "content": "I've passed that to the team.",
            "created_at": "2026-08-31T16:00:30"}
ENQUIRY = {"id": "e2", "status": "pending", "subject": "Portal enquiry from Kris",
           "body_in": "[Customer portal enquiry — reply goes to kris@x.co]\n\n"
                      "Please cancel BK-1037, plans changed.",
           "draft_reply": "", "created_at": "2026-08-31T16:01:00",
           "source_message_id": "portal:abc",
           "category_reason": "Portal enquiry the assistant couldn't answer"}


# ── the thread shows the portal conversation, in violet ───────────────────────
def test_portal_turns_interleave_between_the_emails(client, monkeypatch):
    _env(monkeypatch, [EMAIL_1, ENQUIRY], portal_msgs=[PORTAL_Q, PORTAL_A],
         booking={"id": "b1", "booking_ref": "BK-1037", "booking_date": "2026-09-07",
                  "window": "10:00", "status": "confirmed", "summary": "Service"})
    html = client.get("/app/conversations/view?contact=kris@x.co").text
    assert "Customer portal" in html and "Portal assistant" in html
    assert "Can I cancel BK-1037 please?" in html
    assert "bg-violet-50" in html          # the different colour Kris asked for
    # chronology: the email exchange first, the portal chat after it
    assert html.index("Can I book Friday?") < html.index("Can I cancel BK-1037 please?")
    assert (html.index("Can I cancel BK-1037 please?")
            < html.index("passed that to the team."))


def test_an_ordinary_email_thread_wears_no_portal_furniture(client, monkeypatch):
    _env(monkeypatch, [dict(EMAIL_1, status="pending")])
    html = client.get("/app/conversations/view?contact=kris@x.co").text
    assert "Customer portal" not in html
    assert "Portal request" not in html


# ── the portal request is actionable where the reply is written ───────────────
def test_portal_request_offers_the_cancel_beside_the_pen(client, monkeypatch):
    _env(monkeypatch, [EMAIL_1, ENQUIRY], portal_msgs=[PORTAL_Q, PORTAL_A],
         booking={"id": "b1", "booking_ref": "BK-1037", "booking_date": "2026-09-07",
                  "window": "10:00", "status": "confirmed", "summary": "Service"})
    html = client.get("/app/conversations/view?contact=kris@x.co").text
    assert "Portal request" in html
    assert "Cancel BK-1037" in html
    assert '/app/diary/b1/status' in html          # reuses the diary's manual door
    assert 'value="cancelled"' in html
    # the stage's resting face names the channel — no more bare timestamp
    assert ">Customer portal</span>" in html


def test_a_dead_booking_shows_its_state_not_a_button(client, monkeypatch):
    _env(monkeypatch, [ENQUIRY], portal_msgs=[PORTAL_Q],
         booking={"id": "b1", "booking_ref": "BK-1037", "booking_date": "2026-09-07",
                  "window": "10:00", "status": "cancelled", "summary": "Service"})
    html = client.get("/app/conversations/view?contact=kris@x.co").text
    assert "Portal request" in html
    assert "Already cancelled" in html
    assert "Cancel BK-1037" not in html


# ── the drafter reads the portal conversation ─────────────────────────────────
def test_merge_portal_history_splices_by_timestamp(monkeypatch):
    monkeypatch.setattr(pipeline.db, "list_portal_messages",
                        lambda *a, **k: [dict(PORTAL_Q), dict(PORTAL_A)])
    merged = pipeline._merge_portal_history("t1", "kris@x.co", [dict(EMAIL_1)])
    msgs = agent.thread_messages(merged, "Actually, keep the booking!")
    # email exchange → portal q → portal a → the new inbound
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant", "user"]
    assert msgs[2]["content"].startswith("[Said in the customer portal chat]")
    assert "cancel BK-1037" in msgs[2]["content"]
    assert msgs[3]["content"].startswith("[Portal assistant replied]")


def test_merge_survives_a_missing_table(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("no table")
    monkeypatch.setattr(pipeline.db, "list_portal_messages", _boom)
    history = [dict(EMAIL_1)]
    assert pipeline._merge_portal_history("t1", "kris@x.co", history) == history
    assert pipeline._merge_portal_history("t1", None, history) == history


# ── the portal stores the turns it should, and only those ─────────────────────
def test_identified_turns_persist_with_the_code_masked(monkeypatch):
    stored = []
    monkeypatch.setattr(portal.db, "insert_portal_messages",
                        lambda tid, contact, turns: stored.append((tid, contact, turns)))
    session = SimpleNamespace(tenant_id="t1",
                              contact={"email": "kris@x.co", "name": "Kris",
                                       "reference_code": "BLAM8-K66P"})
    portal._store_chat_turns(session, "My code is BLAM8-K66P, cancel my booking",
                             "Done — quote BLAM8-K66P if you call.")
    assert len(stored) == 1
    tid, contact, turns = stored[0]
    assert (tid, contact) == ("t1", "kris@x.co")
    assert [t["role"] for t in turns] == ["customer", "assistant"]
    assert "BLAM8-K66P" not in turns[0]["content"]     # masked, never readable
    assert "BLAM8-K66P" not in turns[1]["content"]


def test_anonymous_turns_never_persist(monkeypatch):
    stored = []
    monkeypatch.setattr(portal.db, "insert_portal_messages",
                        lambda *a, **k: stored.append(a))
    portal._store_chat_turns(SimpleNamespace(tenant_id="t1", contact=None),
                             "What are your prices?", "From £39 a month.")
    assert stored == []
