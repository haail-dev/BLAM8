"""SMS channel S0 (docs/modules/module-sms-channel.md §1–§4): the Twilio transport unit
logic (E.164, segments, request signing), the /webhook/sms receiver (signature →
route-by-number → STOP/START/HELP intercept → pipeline hand-off), send_sms's
opt-out compliance gate, the phone-keyed CRM ingest branch, and the channel
register in both prompts. In-process via TestClient, db mocked — no network."""
import asyncio
import base64
import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient

import app.sms as sms
import app.sms_webhook as sms_webhook
from app import agent, crm, db
from app.config import settings
from app.main import app


# ── Unit: phone shapes ────────────────────────────────────────────────────────
def test_normalize_e164_uk_shapes():
    assert sms.normalize_e164("07700 900123") == "+447700900123"
    assert sms.normalize_e164("+44 7700-900(123)") == "+447700900123"
    assert sms.normalize_e164("00447700900123") == "+447700900123"
    assert sms.normalize_e164("447700900123") == "+447700900123"
    assert sms.normalize_e164("not a number") is None
    assert sms.normalize_e164("") is None
    assert sms.normalize_e164(None) is None


def test_is_phone_address_never_collides_with_email():
    assert sms.is_phone_address("+447700900123")
    assert not sms.is_phone_address("kris@example.com")
    assert not sms.is_phone_address("07700900123")   # only E.164 marks an SMS thread
    assert not sms.is_phone_address(None)


# ── Unit: segment arithmetic ──────────────────────────────────────────────────
def test_segments_gsm7_boundaries():
    assert sms.segments("") == 0
    assert sms.segments("a" * 160) == 1
    assert sms.segments("a" * 161) == 2      # concatenated: 153/segment
    assert sms.segments("a" * 306) == 2
    assert sms.segments("a" * 307) == 3


def test_segments_extension_and_unicode():
    assert sms.segments("€" * 80) == 1       # ESC-extension: 2 septets each, 160 total
    assert sms.segments("€" * 81) == 2
    assert sms.segments("😀" + "a" * 69) == 1  # UCS-2: 70-char single segment
    assert sms.segments("😀" + "a" * 70) == 2


# ── Unit: request signing ─────────────────────────────────────────────────────
def _sign(url: str, params: dict, token: str) -> str:
    payload = url + "".join(k + str(v) for k, v in sorted(params.items()))
    return base64.b64encode(
        hmac.new(token.encode(), payload.encode("utf-8"), hashlib.sha1).digest()
    ).decode()


def test_validate_signature_round_trip(monkeypatch):
    monkeypatch.setattr(settings, "twilio_auth_token", "tok123")
    url = "http://localhost:8000/webhook/sms"
    params = {"From": "+447700900123", "Body": "hi", "To": "+447000000001"}
    good = _sign(url, params, "tok123")
    assert sms.validate_signature(url, params, good)
    assert not sms.validate_signature(url, params, _sign(url, params, "wrong"))
    assert not sms.validate_signature(url, {**params, "Body": "tampered"}, good)
    assert not sms.validate_signature(url, params, None)


def test_validate_signature_off_when_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "twilio_auth_token", "")
    assert not sms.validate_signature("http://x/webhook/sms", {}, "anything")


# ── Webhook: /webhook/sms ─────────────────────────────────────────────────────
TENANT = "11111111-1111-1111-1111-111111111111"
OUR_NUMBER = "+447000000001"
CALLER = "+447700900123"


@pytest.fixture
def hook(monkeypatch):
    """TestClient + the webhook's collaborators recorded. Returns (client, state);
    posts go through _post which signs like Twilio would."""
    monkeypatch.setattr(settings, "twilio_auth_token", "tok123")
    monkeypatch.setattr(settings, "app_base_url", "http://localhost:8000")
    state = {"drafts": [], "opt_outs": [], "opt_ins": []}

    monkeypatch.setattr(db, "channel_connection_by_number",
                        lambda n, **k: ({"id": "cc1", "tenant_id": TENANT,
                                         "channel_type": "sms", "phone_number": OUR_NUMBER}
                                        if n == OUR_NUMBER else None))
    monkeypatch.setattr(db, "sms_opt_out", lambda t, p: state["opt_outs"].append((t, p)))
    monkeypatch.setattr(db, "sms_opt_in", lambda t, p: state["opt_ins"].append((t, p)))
    monkeypatch.setattr(db, "get_tenant", lambda t, **k: {"id": t, "name": "Border Dental"})

    async def _draft(tenant_id, email, **kwargs):
        state["drafts"].append({"tenant_id": tenant_id, "email": email, **kwargs})
        return "log-1"
    monkeypatch.setattr(sms_webhook.pipeline, "draft_for_email", _draft)

    client = TestClient(app)

    def _post(params: dict, signature: str | None = None):
        sig = signature if signature is not None else _sign(
            "http://localhost:8000/webhook/sms", params, "tok123")
        return client.post("/webhook/sms", data=params,
                           headers={"X-Twilio-Signature": sig})
    return _post, state


def test_webhook_rejects_bad_signature(hook):
    post, state = hook
    r = post({"From": CALLER, "To": OUR_NUMBER, "Body": "hi"}, signature="forged")
    assert r.status_code == 403
    assert not state["drafts"]


def test_webhook_drafts_via_the_pipeline(hook):
    post, state = hook
    r = post({"From": CALLER, "To": OUR_NUMBER, "Body": "Do you have space Saturday?",
              "MessageSid": "SM123"})
    assert r.status_code == 200 and "<Response" in r.text
    (d,) = state["drafts"]
    assert d["tenant_id"] == TENANT
    assert d["channel"] == "sms"
    assert d["auto_send_ok"] is True
    assert d["source_message_id"] == "SM123"
    assert d["email"]["from_address"] == CALLER
    assert d["email"]["thread_id"] == f"sms:{CALLER}"
    assert d["email"]["subject"] == ""


def test_webhook_stop_is_handled_without_the_llm(hook):
    post, state = hook
    r = post({"From": CALLER, "To": OUR_NUMBER, "Body": " STOP! ", "MessageSid": "SM1"})
    assert r.status_code == 200
    assert "unsubscribed" in r.text
    assert state["opt_outs"] == [(TENANT, CALLER)]
    assert not state["drafts"]                      # STOP never reaches the pipeline


def test_webhook_start_clears_the_opt_out(hook):
    post, state = hook
    post({"From": CALLER, "To": OUR_NUMBER, "Body": "start"})
    assert state["opt_ins"] == [(TENANT, CALLER)]
    assert not state["drafts"]


def test_webhook_help_names_the_business(hook):
    post, state = hook
    r = post({"From": CALLER, "To": OUR_NUMBER, "Body": "HELP"})
    assert "Border Dental" in r.text
    assert not state["drafts"]


def test_webhook_unknown_number_acks_without_drafting(hook):
    post, state = hook
    r = post({"From": CALLER, "To": "+447999999999", "Body": "hi"})
    assert r.status_code == 200
    assert not state["drafts"] and not state["opt_outs"]


# ── send_sms: the compliance gate + the Twilio call ───────────────────────────
class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self._payload


class _FakeHttp:
    """Stands in for httpx.AsyncClient — records the one POST send_sms makes."""
    calls: list = []
    def __init__(self, *a, **k):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    async def post(self, url, data=None, auth=None):
        _FakeHttp.calls.append({"url": url, "data": data, "auth": auth})
        return _FakeResponse({"sid": "SM_sent"})


@pytest.fixture
def sendable(monkeypatch):
    monkeypatch.setattr(settings, "twilio_account_sid", "AC1")
    monkeypatch.setattr(settings, "twilio_auth_token", "tok123")
    monkeypatch.setattr(db, "sms_connection",
                        lambda t, **k: {"id": "cc1", "phone_number": OUR_NUMBER})
    monkeypatch.setattr(db, "sms_opted_out", lambda t, p, **k: False)
    events = []
    monkeypatch.setattr(db, "insert_usage_event", lambda e: events.append(e))
    monkeypatch.setattr(sms.httpx, "AsyncClient", _FakeHttp)
    _FakeHttp.calls = []
    return events


def test_send_sms_posts_and_meters(sendable):
    sid = asyncio.run(sms.send_sms(TENANT, "07700 900123", "See you at 2pm",
                                   email_log_id="log-1"))
    assert sid == "SM_sent"
    (call,) = _FakeHttp.calls
    assert call["data"] == {"From": OUR_NUMBER, "To": CALLER, "Body": "See you at 2pm"}
    assert call["auth"] == ("AC1", "tok123")
    (event,) = sendable
    assert event["provider"] == "twilio" and event["operation"] == "sms_send"
    assert event["output_tokens"] == 1                      # 1 segment
    assert event["cost_usd"] == pytest.approx(settings.twilio_sms_segment_usd)
    assert event["email_log_id"] == "log-1"


def test_send_sms_refuses_opted_out(sendable, monkeypatch):
    monkeypatch.setattr(db, "sms_opted_out", lambda t, p, **k: True)
    with pytest.raises(sms.SmsOptedOut):
        asyncio.run(sms.send_sms(TENANT, CALLER, "hello"))
    assert not _FakeHttp.calls                              # gate fires BEFORE the HTTP call


def test_send_sms_requires_a_connection(sendable, monkeypatch):
    monkeypatch.setattr(db, "sms_connection", lambda t, **k: None)
    with pytest.raises(sms.SmsNotConfigured):
        asyncio.run(sms.send_sms(TENANT, CALLER, "hello"))


# ── CRM ingest: the phone-keyed branch (spec §1) ──────────────────────────────
def test_crm_ingest_sms_row_keys_the_contact_by_phone(monkeypatch):
    created, activities, updates = [], [], []
    monkeypatch.setattr(db, "crm_get_or_create_contact_by_phone",
                        lambda t, phone, name=None: (created.append((t, phone, name))
                                                     or {"id": "ct1", "name": None,
                                                         "company_id": None,
                                                         "last_activity_at": None}))
    monkeypatch.setattr(db, "crm_update_contact",
                        lambda cid, t, fields: updates.append((cid, fields)))
    monkeypatch.setattr(db, "crm_insert_activity",
                        lambda t, **k: activities.append(k) or 1)
    out = crm.ingest_email_log_row(TENANT, {
        "id": "log-1", "from_address": CALLER, "contact_name": "Sarah",
        "subject": "", "status": "pending", "request_summary": "Wants Saturday slot",
        "created_at": "2026-07-05T10:00:00+00:00",
    })
    assert created == [(TENANT, CALLER, "Sarah")]
    assert out == {"activities": 1, "contact_id": "ct1", "company_id": None}
    (act,) = activities
    assert act["activity_type"] == "sms" and act["direction"] == "inbound"
    assert updates and updates[0][1]["name"] == "Sarah"     # stated name lands


def test_crm_ingest_still_skips_junk_addresses(monkeypatch):
    # Not an email, not a phone — the pre-SMS behaviour is unchanged.
    out = crm.ingest_email_log_row(TENANT, {"id": "x", "from_address": "banana"})
    assert out == {"activities": 0, "contact_id": None, "company_id": None}


# ── Prompts: the SMS register (spec D3) ───────────────────────────────────────
def test_system_prompt_gains_channel_style_for_sms():
    cfg = {"tone": "warm", "channel_config": {"sms": {"sign_as": "Sarah @ Border Dental"}}}
    p = agent.build_system_prompt(cfg, channel="sms")
    assert "<channel_style>" in p
    assert "SMS text-message conversation" in p
    assert "Sign as: Sarah @ Border Dental" in p
    email_p = agent.build_system_prompt(cfg)
    assert "<channel_style>" not in email_p
    assert "email conversation" in email_p


def test_outbound_prompt_gains_channel_style_for_sms():
    p = agent.build_outbound_prompt({"tone": "warm"}, "reminder", "2pm Tuesday",
                                    channel="sms")
    assert "<channel_style>" in p
    assert "<facts>" in p                                   # facts stay the last section
    assert "<channel_style>" not in agent.build_outbound_prompt(
        {"tone": "warm"}, "reminder", "2pm Tuesday")


# ── Missed-call text-back (spec §5) ───────────────────────────────────────────
@pytest.fixture
def voice(monkeypatch):
    """TestClient for /webhook/voice with the guards' collaborators patched to
    'allowed' and _schedule captured (the coroutine is closed, not run — the
    handler's own behaviour is tested directly below)."""
    monkeypatch.setattr(settings, "twilio_auth_token", "tok123")
    monkeypatch.setattr(settings, "app_base_url", "http://localhost:8000")
    state = {"scheduled": []}

    monkeypatch.setattr(db, "channel_connection_by_number",
                        lambda n, **k: ({"id": "cc1", "tenant_id": TENANT,
                                         "channel_type": "sms", "phone_number": OUR_NUMBER,
                                         "config": {}}
                                        if n == OUR_NUMBER else None))
    monkeypatch.setattr(db, "get_tenant", lambda t, **k: {"id": t, "name": "Koth & Co"})
    monkeypatch.setattr(db, "sms_opted_out", lambda t, p, **k: False)
    monkeypatch.setattr(db, "recent_missed_call_text", lambda t, p, s, **k: False)
    monkeypatch.setattr(db, "sms_thread_since", lambda t, p, s, **k: False)

    def _capture(coro):
        state["scheduled"].append(coro)
        coro.close()
    monkeypatch.setattr(sms_webhook, "_schedule", _capture)

    client = TestClient(app)

    def _post(params: dict, signature: str | None = None):
        sig = signature if signature is not None else _sign(
            "http://localhost:8000/webhook/voice", params, "tok123")
        return client.post("/webhook/voice", data=params,
                           headers={"X-Twilio-Signature": sig})
    return _post, state


def test_voice_rejects_bad_signature(voice):
    post, state = voice
    r = post({"From": CALLER, "To": OUR_NUMBER, "CallSid": "CA1"}, signature="forged")
    assert r.status_code == 403 and not state["scheduled"]


def test_voice_missed_call_promises_and_schedules_the_text(voice):
    post, state = voice
    r = post({"From": CALLER, "To": OUR_NUMBER, "CallSid": "CA1"})
    assert r.status_code == 200
    assert "Koth &amp; Co" in r.text                 # XML-escaped tenant name
    assert "text message" in r.text and "<Hangup/>" in r.text
    assert len(state["scheduled"]) == 1


def test_voice_withheld_caller_gets_no_text_promise(voice):
    post, state = voice
    r = post({"From": "anonymous", "To": OUR_NUMBER, "CallSid": "CA2"})
    assert r.status_code == 200
    assert "text message" not in r.text              # the <Say> must not overpromise
    assert not state["scheduled"]


def test_voice_opted_out_caller_is_guarded(voice, monkeypatch):
    monkeypatch.setattr(db, "sms_opted_out", lambda t, p, **k: True)
    post, state = voice
    r = post({"From": CALLER, "To": OUR_NUMBER, "CallSid": "CA3"})
    assert "text message" not in r.text and not state["scheduled"]


def test_voice_24h_dedupe_is_guarded(voice, monkeypatch):
    monkeypatch.setattr(db, "recent_missed_call_text", lambda t, p, s, **k: True)
    post, state = voice
    post({"From": CALLER, "To": OUR_NUMBER, "CallSid": "CA4"})
    assert not state["scheduled"]


def test_voice_open_thread_today_is_guarded(voice, monkeypatch):
    monkeypatch.setattr(db, "sms_thread_since", lambda t, p, s, **k: True)
    post, state = voice
    post({"From": CALLER, "To": OUR_NUMBER, "CallSid": "CA5"})
    assert not state["scheduled"]


def test_voice_platform_number_never_loops(voice, monkeypatch):
    # The "caller" is another BLAM8 provisioned number — never text the platform.
    monkeypatch.setattr(db, "channel_connection_by_number",
                        lambda n, **k: {"id": "cc", "tenant_id": TENANT,
                                        "channel_type": "sms", "phone_number": n,
                                        "config": {}})
    post, state = voice
    post({"From": CALLER, "To": OUR_NUMBER, "CallSid": "CA6"})
    assert not state["scheduled"]


def test_voice_text_back_disabled_by_config(voice, monkeypatch):
    monkeypatch.setattr(db, "channel_connection_by_number",
                        lambda n, **k: ({"id": "cc1", "tenant_id": TENANT,
                                         "channel_type": "sms", "phone_number": OUR_NUMBER,
                                         "config": {"text_back_enabled": False}}
                                        if n == OUR_NUMBER else None))
    post, state = voice
    post({"From": CALLER, "To": OUR_NUMBER, "CallSid": "CA7"})
    assert not state["scheduled"]


@pytest.fixture
def missed_call_env(monkeypatch):
    """Collaborators for _handle_missed_call itself, all recorded."""
    state = {"composed": [], "logs": [], "sent": [], "published": [], "marked": []}

    async def _compose(cfg, purpose, facts, channel="email", **kwargs):
        state["composed"].append({"purpose": purpose, "channel": channel})
        return agent.DraftResult(text="Sorry we missed you — how can we help?",
                                 usage={}, model="test-model")
    monkeypatch.setattr(sms_webhook.agent, "compose_outbound", _compose)
    monkeypatch.setattr(db, "effective_config",
                        lambda t, m, **k: {"auto_send": False, "tone": "warm"})
    monkeypatch.setattr(db, "insert_email_log",
                        lambda *a, **k: (state["logs"].append({"args": a, **k}) or ("log-9", True)))
    monkeypatch.setattr(db, "mark_email_log_sent",
                        lambda log_id, t, **k: state["marked"].append((log_id, k)))
    monkeypatch.setattr(sms_webhook.usage, "record_anthropic", lambda *a, **k: None)
    monkeypatch.setattr(sms_webhook.events, "publish",
                        lambda t, e: state["published"].append(e))

    async def _send(tenant_id, to, body, **k):
        state["sent"].append({"to": to, "body": body})
        return "SM_x"
    monkeypatch.setattr(sms_webhook.sms, "send_sms", _send)
    return state


CONN = {"id": "cc1", "tenant_id": TENANT, "channel_type": "sms",
        "phone_number": OUR_NUMBER, "config": {}}


def test_handle_missed_call_drafts_pending_without_auto_send(missed_call_env):
    asyncio.run(sms_webhook._handle_missed_call(CONN, CALLER, "CA1"))
    (c,) = missed_call_env["composed"]
    assert c["channel"] == "sms"
    (row,) = missed_call_env["logs"]
    assert row["kind"] == "missed_call" and row["channel"] == "sms"
    assert row["thread_id"] == f"sms:{CALLER}"
    assert row["source_message_id"] == "CA1"       # duplicate webhook → deduped row
    assert not missed_call_env["sent"]             # draft-only: nothing leaves
    assert any(e["type"] == "draft_ready" for e in missed_call_env["published"])


def test_handle_missed_call_auto_sends_with_disclosure(missed_call_env, monkeypatch):
    monkeypatch.setattr(db, "effective_config",
                        lambda t, m, **k: {"auto_send": True})
    asyncio.run(sms_webhook._handle_missed_call(CONN, CALLER, "CA1"))
    (sent,) = missed_call_env["sent"]
    assert sent["to"] == CALLER
    assert "(Automated reply from our AI assistant)" in sent["body"]
    assert missed_call_env["marked"] == [("log-9", {"via": "auto", "edited": False})]
    assert any(e["type"] == "draft_sent" for e in missed_call_env["published"])


def test_handle_missed_call_duplicate_delivery_is_silent(missed_call_env, monkeypatch):
    monkeypatch.setattr(db, "insert_email_log", lambda *a, **k: ("log-9", False))
    monkeypatch.setattr(db, "effective_config", lambda t, m, **k: {"auto_send": True})
    asyncio.run(sms_webhook._handle_missed_call(CONN, CALLER, "CA1"))
    assert not missed_call_env["sent"] and not missed_call_env["published"]


# ── Preferred-channel resolution (spec §6) ────────────────────────────────────
def _crm_contact(phone=CALLER, pref="SMS"):
    return {"id": "ct1", "email": "sarah@example.com", "phone": phone,
            "custom_data": {"preferred_contact": pref}}


def test_preferred_destination_phone_contact_is_sms(monkeypatch):
    assert sms.preferred_sms_destination(TENANT, CALLER) == CALLER


def test_preferred_destination_email_contact_with_sms_preference(monkeypatch):
    monkeypatch.setattr(db, "crm_contact_by_email", lambda t, e, **k: _crm_contact())
    monkeypatch.setattr(db, "sms_opted_out", lambda t, p, **k: False)
    monkeypatch.setattr(db, "sms_connection", lambda t, **k: {"id": "cc1"})
    assert sms.preferred_sms_destination(TENANT, "sarah@example.com") == CALLER


def test_preferred_destination_defaults_to_email(monkeypatch):
    monkeypatch.setattr(db, "crm_contact_by_email",
                        lambda t, e, **k: _crm_contact(pref="Email"))
    assert sms.preferred_sms_destination(TENANT, "sarah@example.com") is None
    monkeypatch.setattr(db, "crm_contact_by_email",
                        lambda t, e, **k: _crm_contact(phone=None))
    assert sms.preferred_sms_destination(TENANT, "sarah@example.com") is None
    assert sms.preferred_sms_destination(TENANT, None) is None


def test_preferred_destination_stop_falls_back_to_email(monkeypatch):
    monkeypatch.setattr(db, "crm_contact_by_email", lambda t, e, **k: _crm_contact())
    monkeypatch.setattr(db, "sms_opted_out", lambda t, p, **k: True)
    assert sms.preferred_sms_destination(TENANT, "sarah@example.com") is None


def test_preferred_destination_needs_an_active_channel(monkeypatch):
    monkeypatch.setattr(db, "crm_contact_by_email", lambda t, e, **k: _crm_contact())
    monkeypatch.setattr(db, "sms_opted_out", lambda t, p, **k: False)
    monkeypatch.setattr(db, "sms_connection", lambda t, **k: None)
    assert sms.preferred_sms_destination(TENANT, "sarah@example.com") is None


# ── Reminder job routing (spec §6) ────────────────────────────────────────────
BOOKING = {"id": "b1", "contact": "sarah@example.com", "booking_date": "2026-07-06",
           "booking_ref": "BK-1", "window": "14:00", "spaces": None,
           "thread_id": "th-1"}


@pytest.fixture
def reminder_env(monkeypatch):
    import app.jobs.send_reminders as job
    state = {"composed": [], "logs": [], "sms": [], "mail": []}

    async def _compose(cfg, purpose, facts, channel="email", **kwargs):
        state["composed"].append({"facts": facts, "channel": channel})
        return agent.DraftResult(text="See you tomorrow!", usage={}, model="m")
    monkeypatch.setattr(job.agent, "compose_outbound", _compose)
    monkeypatch.setattr(db, "insert_email_log",
                        lambda *a, **k: (state["logs"].append({"args": a, **k}) or ("log-5", True)))
    monkeypatch.setattr(db, "mark_booking_reminded", lambda *a, **k: None)
    monkeypatch.setattr(db, "mark_email_log_sent", lambda *a, **k: None)
    monkeypatch.setattr(db, "list_job_parties", lambda *a, **k: [])
    monkeypatch.setattr(job.usage, "record_anthropic", lambda *a, **k: None)

    async def _send_sms(tenant_id, to, body, **k):
        state["sms"].append(to)
        return "SM_x"
    monkeypatch.setattr(job.sms, "send_sms", _send_sms)

    async def _send_mail(tenant_id, to, subject, body, **k):
        state["mail"].append(to)
    monkeypatch.setattr(job.mailbox, "send_mail", _send_mail)
    return job, state


def test_reminder_routes_to_sms_for_sms_preferring_contact(reminder_env, monkeypatch):
    job, state = reminder_env
    monkeypatch.setattr(job.sms, "preferred_sms_destination", lambda t, c: CALLER)
    outcome = asyncio.run(job._remind_one(TENANT, {"auto_send": True}, True, dict(BOOKING)))
    assert outcome == "sent"
    (c,) = state["composed"]
    assert c["channel"] == "sms" and "reply C" in c["facts"]
    (row,) = state["logs"]
    assert row["channel"] == "sms" and row["thread_id"] == f"sms:{CALLER}"
    assert row["args"][1] == CALLER                  # from_address = the phone
    assert state["sms"] == [CALLER] and not state["mail"]


def test_reminder_stays_on_email_by_default(reminder_env, monkeypatch):
    job, state = reminder_env
    monkeypatch.setattr(job.sms, "preferred_sms_destination", lambda t, c: None)
    asyncio.run(job._remind_one(TENANT, {"auto_send": True}, True, dict(BOOKING)))
    (c,) = state["composed"]
    assert c["channel"] == "email" and "reply C" not in c["facts"]
    (row,) = state["logs"]
    assert row["channel"] == "email" and row["thread_id"] == "th-1"
    assert state["mail"] == ["sarah@example.com"] and not state["sms"]


# ── Confirm-by-reply (spec §6) ────────────────────────────────────────────────
def test_webhook_confirm_stamps_the_upcoming_booking(hook, monkeypatch):
    post, state = hook
    stamped = []
    monkeypatch.setattr(db, "crm_contact_by_phone", lambda t, p, **k: None)
    monkeypatch.setattr(db, "upcoming_booking_for_contacts",
                        lambda t, addrs, today, **k: {"id": "b1", "booking_ref": "BK-1",
                                                      "booking_date": "2026-07-06"})
    monkeypatch.setattr(db, "stamp_client_confirmed",
                        lambda bid, t, **k: stamped.append(bid))
    r = post({"From": CALLER, "To": OUR_NUMBER, "Body": "C", "MessageSid": "SM9"})
    assert stamped == ["b1"]
    assert "confirmed for 2026-07-06" in r.text
    assert not state["drafts"]                       # no LLM for a bare confirm


def test_webhook_bare_yes_without_a_booking_reaches_the_drafter(hook, monkeypatch):
    post, state = hook
    monkeypatch.setattr(db, "crm_contact_by_phone",
                        lambda t, p, **k: {"id": "ct1", "email": "sarah@example.com"})
    monkeypatch.setattr(db, "upcoming_booking_for_contacts",
                        lambda t, addrs, today, **k: None)
    post({"From": CALLER, "To": OUR_NUMBER, "Body": "yes", "MessageSid": "SM10"})
    assert len(state["drafts"]) == 1                 # falls through mid-conversation


# ── S3: module hub + /app/channels settings ───────────────────────────────────
from app.auth import TenantCtx, require_onboarded
from app.security import require_csrf


@pytest.fixture
def app_client(monkeypatch):
    """Authed TestClient (the test_home_modules pattern) with the channel page's
    db collaborators defaulted; individual tests re-patch what they assert on."""
    import app.dashboard as dashboard
    monkeypatch.setattr(db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(db, "get_connection_status", lambda *a, **k: None)
    monkeypatch.setattr(db, "get_tenant", lambda *a, **k: {"mailbox": "a@b.co"})
    monkeypatch.setattr(db, "active_modules", lambda *a, **k: {"sms"})
    monkeypatch.setattr(db, "module_active",
                        lambda t, m, **k: m in db.active_modules(t))
    monkeypatch.setattr(db, "sms_connection", lambda *a, **k: None)
    monkeypatch.setattr(db, "sms_opt_out_list", lambda *a, **k: [])
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id=TENANT, client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_modules_hub_shows_text_messaging(app_client):
    html = app_client.get("/app/modules").text
    assert "Text messaging" in html and "/app/channels" in html
    assert "WhatsApp" in html and "Telegram" in html    # roadmap chips stay honest


def test_channels_page_404_when_module_off(app_client, monkeypatch):
    monkeypatch.setattr(db, "active_modules", lambda *a, **k: set())
    assert app_client.get("/app/channels").status_code == 404


def test_channels_page_offers_activation(app_client, monkeypatch):
    monkeypatch.setattr(settings, "twilio_account_sid", "AC1")
    monkeypatch.setattr(settings, "twilio_auth_token", "tok")
    html = app_client.get("/app/channels").text
    assert "Activate text messaging" in html


def test_channels_page_shows_number_and_divert_code(app_client, monkeypatch):
    monkeypatch.setattr(db, "sms_connection",
                        lambda *a, **k: {"id": "cc1", "phone_number": OUR_NUMBER,
                                         "config": {"text_back_enabled": True}})
    monkeypatch.setattr(db, "sms_opt_out_list",
                        lambda *a, **k: [{"phone": CALLER,
                                          "opted_out_at": "2026-07-05T10:00:00+00:00"}])
    html = app_client.get("/app/channels").text
    assert OUR_NUMBER in html
    assert f"*61*{OUR_NUMBER}#" in html                 # the divert dial string
    assert CALLER in html                               # opt-out list renders


def test_activate_provisions_and_redirects(app_client, monkeypatch):
    import app.channel_routes as cr
    provisioned = []

    async def _prov(tenant_id):
        provisioned.append(tenant_id)
        return {"id": "cc1", "phone_number": OUR_NUMBER}
    monkeypatch.setattr(cr.sms, "provision_number", _prov)
    r = app_client.post("/app/channels/activate", data={"csrf_token": "x"},
                        follow_redirects=False)
    assert r.status_code == 303 and "msg=live" in r.headers["location"]
    assert provisioned == [TENANT]


def test_textback_toggle_updates_config(app_client, monkeypatch):
    saved = []
    monkeypatch.setattr(db, "sms_connection",
                        lambda *a, **k: {"id": "cc1", "phone_number": OUR_NUMBER,
                                         "config": {"text_back_enabled": True}})
    monkeypatch.setattr(db, "update_channel_config",
                        lambda cid, t, config: saved.append(config))
    r = app_client.post("/app/channels/textback",
                        data={"csrf_token": "x", "enabled": "off"}, follow_redirects=False)
    assert r.status_code == 303
    assert saved == [{"text_back_enabled": False}]


def test_manual_opt_out_normalises_the_number(app_client, monkeypatch):
    added = []
    monkeypatch.setattr(db, "sms_opt_out", lambda t, p: added.append(p))
    r = app_client.post("/app/channels/optout",
                        data={"csrf_token": "x", "phone": "07700 900123"},
                        follow_redirects=False)
    assert r.status_code == 303 and added == [CALLER]
    r = app_client.post("/app/channels/optout",
                        data={"csrf_token": "x", "phone": "banana"},
                        follow_redirects=False)
    assert "badnumber" in r.headers["location"] and added == [CALLER]


# ── provision_number (spec §2) ────────────────────────────────────────────────
class _FakeProvisionHttp:
    calls: list = []
    def __init__(self, *a, **k):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    async def get(self, url, params=None, auth=None):
        _FakeProvisionHttp.calls.append({"method": "GET", "url": url, "params": params})
        return _FakeResponse({"available_phone_numbers": [{"phone_number": OUR_NUMBER}]})
    async def post(self, url, data=None, auth=None):
        _FakeProvisionHttp.calls.append({"method": "POST", "url": url, "data": data})
        return _FakeResponse({"phone_number": OUR_NUMBER, "sid": "PN1"})


def test_provision_number_buys_and_wires_webhooks(monkeypatch):
    monkeypatch.setattr(settings, "twilio_account_sid", "AC1")
    monkeypatch.setattr(settings, "twilio_auth_token", "tok")
    monkeypatch.setattr(settings, "app_base_url", "https://blam8.com")
    monkeypatch.setattr(db, "sms_connection", lambda t, **k: None)
    upserts = []
    monkeypatch.setattr(db, "upsert_channel_connection",
                        lambda t, ct, **k: (upserts.append(k)
                                            or {"id": "cc1", **k}))
    monkeypatch.setattr(sms.httpx, "AsyncClient", _FakeProvisionHttp)
    _FakeProvisionHttp.calls = []
    conn = asyncio.run(sms.provision_number(TENANT))
    assert conn["phone_number"] == OUR_NUMBER
    buy = next(c for c in _FakeProvisionHttp.calls if c["method"] == "POST")
    assert buy["data"]["SmsUrl"] == "https://blam8.com/webhook/sms"
    assert buy["data"]["VoiceUrl"] == "https://blam8.com/webhook/voice"
    assert upserts[0]["config"] == {"text_back_enabled": True}


def test_provision_number_is_idempotent(monkeypatch):
    monkeypatch.setattr(settings, "twilio_account_sid", "AC1")
    monkeypatch.setattr(settings, "twilio_auth_token", "tok")
    monkeypatch.setattr(db, "sms_connection",
                        lambda t, **k: {"id": "cc1", "phone_number": OUR_NUMBER})
    monkeypatch.setattr(sms.httpx, "AsyncClient", _FakeProvisionHttp)
    _FakeProvisionHttp.calls = []
    conn = asyncio.run(sms.provision_number(TENANT))
    assert conn["phone_number"] == OUR_NUMBER
    assert not _FakeProvisionHttp.calls            # no second number bought
