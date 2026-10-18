"""Phase 2B — day-before booking reminders (the reverse hand-off).

What this pins:
  - agent.compose_outbound: voice + facts → a short message via structured output; the
    prompt carries the tenant voice and the honesty rule (facts only)
  - the TMS specialist frames a transport booking (capacity/refs, NEVER the vehicle);
    office bookings frame directly
  - the job composes, queues a kind='reminder' pending draft, dedupes (marks the booking
    reminded), logs an OUTBOUND agent event, and respects the tenant's auto_send (drafts
    vs sends)
  - approving a reminder sends it via send_mail (a clean new email), not send_reply

Run: python -m pytest tests/ -q
"""
import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
import app.jobs.send_reminders as job
from app import agent
from app.agents import tms
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf


# ── compose_outbound ──────────────────────────────────────────────────────────
class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(self.payload))],
            stop_reason="end_turn", model="fake-model",
            usage=SimpleNamespace(input_tokens=5, output_tokens=7,
                                  cache_read_input_tokens=0, cache_creation_input_tokens=0))


def test_build_outbound_prompt_has_voice_and_honesty():
    cfg = {"business_brief": "Pawsome boarding kennels", "tone": "friendly",
           "agent_name": "the Pawsome team", "sign_off": "Woof, Pawsome"}
    p = agent.build_outbound_prompt(cfg, "A day-before reminder.", "Booking ref BK-9 on 2026-07-02.")
    assert "Pawsome boarding kennels" in p and "the Pawsome team" in p
    assert "Woof, Pawsome" in p
    assert "reaching out first" in p and "Use ONLY the facts" in p
    assert "Booking ref BK-9" in p


def test_compose_outbound_returns_message(monkeypatch):
    fake = FakeClient({"message": "Hi! Just a reminder your booking is tomorrow. Ref BK-9."})
    monkeypatch.setattr(agent, "_client", fake)
    result = asyncio.run(agent.compose_outbound({"tone": "warm"}, "reminder", "Ref BK-9 tomorrow."))
    assert result.text.startswith("Hi! Just a reminder")
    assert result.usage["input_tokens"] == 5 and result.model == "fake-model"
    assert fake.calls[0]["output_config"]["format"]["schema"] is agent.OUTBOUND_SCHEMA


def test_build_prompts_tell_the_model_the_sign_off_is_added_for_it():
    """The sign-off is appended by code (src/app/sign_off.py): the prompt gives the signer
    as data, says the system adds it, and asks the model not to write a closing."""
    cfg = {"sign_off": "Cheers,\r\nThe Example Team "}
    reply = agent.build_system_prompt(cfg)
    assert "The system adds this exact text after your reply" in reply
    assert "do NOT write it" in reply
    assert "Cheers,\nThe Example Team" in reply and "\r" not in reply
    assert "End every reply with this sign-off" not in reply
    outbound = agent.build_outbound_prompt(cfg, "A reminder.", "Ref 1.")
    assert "The system adds this exact text after your message" in outbound
    assert "Cheers,\nThe Example Team" in outbound and "\r" not in outbound
    assert "End the message with this sign-off" not in outbound
    # SMS keeps its own register: no email sign-off reaches an SMS prompt.
    assert "The system adds this exact text" not in agent.build_system_prompt(cfg, channel="sms")
    assert "The Example Team" not in agent.build_system_prompt(cfg, channel="sms")
    assert "The Example Team" not in agent.build_outbound_prompt(cfg, "A reminder.", "Ref 1.",
                                                                 channel="sms")
    # Nothing configured: no sentence, the model closes as it always did.
    for blank in ({"tone": "warm"}, {"sign_off": " \r\n "}, {"sign_off": None}):
        assert "The system adds this exact text" not in agent.build_system_prompt(blank)
        assert "The system adds this exact text" not in agent.build_outbound_prompt(
            blank, "A reminder.", "Ref 1.")


def test_the_outbound_sign_off_sentence_does_not_mention_examples_it_does_not_have():
    """build_outbound_prompt has no examples block; only the reply prompt does."""
    cfg = {"sign_off": "Cheers,\nThe Example Team"}
    outbound = agent.build_outbound_prompt(cfg, "A reminder.", "Ref 1.")
    assert "examples" not in outbound.lower()
    assert ("(no 'Kind regards', no 'Thanks', no name): end on your last sentence to the "
            "customer.\nCheers,\nThe Example Team") in outbound
    reply = agent.build_system_prompt(cfg)
    assert "even if the examples below end with one" in reply


def _compose(monkeypatch, message, cfg, channel="email"):
    monkeypatch.setattr(agent, "_client", FakeClient({"message": message}))
    return asyncio.run(agent.compose_outbound(cfg, "reminder", "Ref 1 tomorrow.", channel=channel))


def test_compose_outbound_appends_the_configured_sign_off(monkeypatch):
    cfg = {"tone": "warm", "sign_off": "Cheers,\r\nThe Example Team"}
    result = _compose(monkeypatch, "Hi! Reminder for tomorrow.", cfg)
    assert result.text == "Hi! Reminder for tomorrow.\n\nCheers,\nThe Example Team"
    assert result.sign_off == "Cheers,\nThe Example Team"
    # A closing the model wrote anyway is replaced, not doubled.
    result = _compose(monkeypatch, "Hi! Reminder for tomorrow.\n\nThanks,\nSam", cfg)
    assert result.text == "Hi! Reminder for tomorrow.\n\nCheers,\nThe Example Team"
    # A line that is not clearly a name is kept ("The team" has a lowercase word): a
    # doubled closing is cosmetic, deleting what was written for the customer is not.
    result = _compose(monkeypatch, "Hi! Reminder for tomorrow.\n\nThanks,\nThe team", cfg)
    assert result.text == ("Hi! Reminder for tomorrow.\n\nThanks,\nThe team"
                           "\n\nCheers,\nThe Example Team")
    # Already ending with it: left as it is.
    result = _compose(monkeypatch, "Hi!\n\nCheers,\r\nThe Example Team", cfg)
    assert result.text == "Hi!\n\nCheers,\nThe Example Team"
    # SMS keeps the text the model wrote; nothing configured appends nothing.
    result = _compose(monkeypatch, "Hi! Reminder for tomorrow.", cfg, channel="sms")
    assert (result.text, result.sign_off) == ("Hi! Reminder for tomorrow.", "")
    result = _compose(monkeypatch, "Hi! Reminder for tomorrow.", {"tone": "warm"})
    assert (result.text, result.sign_off) == ("Hi! Reminder for tomorrow.", "")


def test_compose_outbound_never_signs_an_empty_message(monkeypatch):
    result = _compose(monkeypatch, "", {"sign_off": "Cheers,\nSam"})
    assert (result.text, result.sign_off) == ("", "")


# ── the specialist framing (reverse contract) ─────────────────────────────────
def test_tms_outbound_reminder_frames_capacity_not_vehicle():
    b = {"booking_date": "2026-07-02", "booking_ref": "BK-1042", "spaces": 6.0,
         "summary": "6 pallets, M17 -> LS9", "vehicle_id": "v-secret",
         "collection_postcode": "M17 1AA", "delivery_postcode": "LS9 8AS", "reference": "PO-77"}
    f = tms.outbound_reminder(b)
    assert "BK-1042" in f and "6 pallet spaces" in f and "2026-07-02" in f
    assert "M17 1AA" in f and "LS9 8AS" in f and "PO-77" in f
    assert "v-secret" not in f and "vehicle" not in f.lower()


def test_office_facts_and_routing():
    office = {"booking_date": "2026-07-02", "window": "am", "booking_ref": "BK-5",
              "summary": "Grooming", "spaces": None}
    f = job._office_facts(office)
    assert "appointment" in f and "BK-5" in f and "Morning" in f
    # routing: sized → TMS specialist, unsized → office
    assert job._facts_for({"spaces": 3.0, "booking_date": "2026-07-02"}).startswith("The customer has a confirmed transport")
    assert job._facts_for(office).startswith("The customer has a confirmed appointment")


# ── the job ───────────────────────────────────────────────────────────────────
OFFICE = {"id": "bk-o", "contact": "sam@x.co", "booking_date": "2026-07-02", "window": "am",
          "booking_ref": "BK-5", "summary": "Grooming", "spaces": None, "thread_id": "th-1"}
TRANSPORT = {"id": "bk-t", "contact": "ops@y.co", "booking_date": "2026-07-02",
             "booking_ref": "BK-9", "summary": "6 pallets", "spaces": 6.0, "thread_id": None}


def _wire_job(monkeypatch, auto_send, bookings):
    calls = {"inserted": [], "reminded": [], "events": [], "sent": []}

    async def fake_compose(cfg, purpose, facts, channel="email", **kwargs):
        return agent.DraftResult(text="Reminder body.", usage={"input_tokens": 1}, model="m")

    async def fake_send_mail(tenant_id, to, subject, body, mailbox_id=None, cc=None):
        calls["sent"].append((to, subject))
        calls.setdefault("cc", []).append(cc)

    monkeypatch.setattr(job.db, "list_job_parties", lambda *a, **k: [])
    monkeypatch.setattr(job.db, "tenants_with_modules", lambda *a, **k: ["t1"])
    monkeypatch.setattr(job.db, "list_bookings_for_reminder", lambda *a, **k: [dict(b) for b in bookings])
    monkeypatch.setattr(job.db, "effective_config", lambda *a, **k: {"auto_send": auto_send, "tone": "warm"})
    monkeypatch.setattr(job.agent, "compose_outbound", fake_compose)
    monkeypatch.setattr(job.db, "insert_email_log",
                        lambda *a, **k: (calls["inserted"].append(k.get("kind")) or "log-1", True))
    monkeypatch.setattr(job.db, "mark_booking_reminded",
                        lambda bid, tid, **k: calls["reminded"].append(bid))
    monkeypatch.setattr(job.db, "log_agent_event",
                        lambda tid, kind, summary, **k: calls["events"].append((kind, k.get("direction"))))
    monkeypatch.setattr(job.db, "mark_email_log_sent", lambda *a, **k: None)
    monkeypatch.setattr(job.usage, "record_anthropic", lambda *a, **k: None)
    monkeypatch.setattr(job.mailbox, "send_mail", fake_send_mail)
    return calls


def test_job_drafts_for_approval_when_not_auto(monkeypatch):
    calls = _wire_job(monkeypatch, auto_send=False, bookings=[OFFICE, TRANSPORT])
    out = asyncio.run(job.run())
    assert out["drafted"] == 2 and out["sent"] == 0
    assert calls["inserted"] == ["reminder", "reminder"]     # queued as reminder drafts
    assert set(calls["reminded"]) == {"bk-o", "bk-t"}        # both deduped
    assert calls["sent"] == []                               # nothing sent
    assert ("reminder", "outbound") in calls["events"]       # outbound agent-log event


def test_job_auto_sends_for_full_auto_tenant(monkeypatch):
    calls = _wire_job(monkeypatch, auto_send=True, bookings=[OFFICE])
    out = asyncio.run(job.run())
    assert out["sent"] == 1 and out["drafted"] == 0
    assert calls["sent"] == [("sam@x.co", "Reminder: your booking tomorrow (BK-5)")]
    assert ("sent", "outbound") in calls["events"]


def test_job_reminds_the_on_site_party_and_ccs_the_informed(monkeypatch):
    # Job parties (docs/modules/module-job-parties.md): the day-before reminder goes to
    # whoever opens the door, cc to the opted-in informed, never the payer.
    calls = _wire_job(monkeypatch, auto_send=True, bookings=[OFFICE])
    monkeypatch.setattr(job.db, "list_job_parties", lambda *a, **k: [
        {"label": "tenant", "email": "tenant@x.co", "on_site": True,
         "source": "owner"},
        {"label": "agent", "email": "agent@x.co", "informed": True,
         "source": "owner"},
        {"label": "scam", "email": "evil@x.co", "on_site": True,
         "source": "extracted"},                     # D4: never resolves
    ])
    asyncio.run(job.run())
    assert calls["sent"] == [("tenant@x.co", "Reminder: your booking tomorrow (BK-5)")]
    assert calls["cc"] == [["agent@x.co"]]


def test_reminder_carries_the_recipients_portal_footer_unless_ccd(monkeypatch):
    """Phase 3 (docs/modules/module-job-parties.md): the reminder is the ON-SITE
    party's portal door — their own reference code rides the email (minted on
    first need) so the person who attends can reschedule themselves. Dropped
    when a cc rides along: the code is an identity capability and must never
    reach a cc'd party (the Phase 1 rule)."""
    import app.portal as portal
    calls = _wire_job(monkeypatch, auto_send=True, bookings=[OFFICE])
    bodies = []

    async def capture_send(tenant_id, to, subject, body, mailbox_id=None, cc=None):
        bodies.append((body, cc))
    monkeypatch.setattr(job.mailbox, "send_mail", capture_send)
    monkeypatch.setattr(job.db, "get_tenant_config",
                        lambda *a, **k: {"portal_enabled": True})
    monkeypatch.setattr(portal, "email_footer",
                        lambda tid, cfg, to: f"\n---\nYour reference: T1-AAAA ({to})")
    asyncio.run(job.run())
    body, cc = bodies[0]
    assert cc is None and "Your reference: T1-AAAA (sam@x.co)" in body
    # With an informed cc on the send, the footer stays off.
    bodies.clear()
    monkeypatch.setattr(job.db, "list_job_parties", lambda *a, **k: [
        {"label": "agent", "email": "agent@x.co", "informed": True,
         "source": "owner"}])
    asyncio.run(job.run())
    body, cc = bodies[0]
    assert cc == ["agent@x.co"] and "Your reference" not in body


# ── approving a reminder sends a fresh email, not a reply ─────────────────────
@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_approving_reminder_uses_send_mail_not_send_reply(client, monkeypatch):
    entry = {"id": "log-1", "from_address": "sam@x.co", "subject": "Reminder: your booking tomorrow",
             "draft_reply": "Reminder body.", "kind": "reminder", "mailbox_id": None,
             "source_message_id": None}
    sent = {}

    async def fake_send_mail(tid, to, subject, body, mailbox_id=None):
        sent.update(to=to, subject=subject, body=body)

    async def fake_send_reply(*a, **k):
        sent["reply_called"] = True

    async def fake_push(*a, **k):
        pass

    monkeypatch.setattr(dashboard.db, "get_email_log_entry", lambda *a, **k: entry)
    monkeypatch.setattr(dashboard.db, "update_email_log_draft", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.db, "mark_email_log_sent", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.db, "resolve_holds_for_log", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.mailbox, "send_mail", fake_send_mail)
    monkeypatch.setattr(dashboard.mailbox, "send_reply", fake_send_reply)
    monkeypatch.setattr(dashboard.calendar_sync, "push_bookings", fake_push)

    r = client.post("/app/drafts/log-1/send", follow_redirects=False,
                    data={"draft_reply": "Reminder body.", "return_to": "/app/drafts"})
    assert r.status_code == 303
    assert sent.get("to") == "sam@x.co" and "reply_called" not in sent   # send_mail, not send_reply
