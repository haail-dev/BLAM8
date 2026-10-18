"""On my way (benchmark §6 item 1): the tap sends a two-line ETA message to
whoever is on site, else the booking's contact — email now, SMS when the
channel is live — and logs it like any other outbound."""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import on_my_way
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf


NOW = datetime(2026, 9, 8, 10, 15, tzinfo=on_my_way.DIARY_TZ)


def test_eta_phrase_minutes_and_clock_time():
    assert on_my_way.eta_phrase("30", NOW) == "in about 30 minutes (around 10:45)"
    assert on_my_way.eta_phrase("10:45", NOW) == "at about 10:45"
    assert on_my_way.eta_phrase("9.05", NOW) == "at about 09:05"
    assert on_my_way.eta_phrase("2", NOW) is None          # too soon to mean anything
    assert on_my_way.eta_phrase("500", NOW) is None
    assert on_my_way.eta_phrase("soon", NOW) is None
    assert on_my_way.eta_phrase("", NOW) is None


BOOKING = {"id": "b1", "booking_ref": "BK-1001", "booking_date": "2026-09-08", "window": "am",
           "contact": "kris@x.co", "summary": "Drain, clean and refill — The Byre", "status": "confirmed",
           "thread_id": "t1", "mailbox_id": None}


def test_recipient_is_the_confirmed_on_site_party_else_the_contact():
    jo = {"name": "Jo", "email": "jo@example.com", "label": "housekeeper", "on_site": True, "source": "confirmed"}
    assert on_my_way.recipient(BOOKING, [jo])["email"] == "jo@example.com"
    extracted = {**jo, "source": "extracted"}       # D4: unconfirmed rows are inert
    assert on_my_way.recipient(BOOKING, [extracted])["email"] == "kris@x.co"
    assert on_my_way.recipient(BOOKING, [])["is_fallback"] is True


def test_facts_carry_the_eta_the_person_and_the_visit():
    facts = on_my_way.facts_for(BOOKING, {"name": "Jo", "label": "housekeeper"}, "in about 30 minutes (around 10:45)", "Dan")
    assert "arrive in about 30 minutes (around 10:45)" in facts
    assert "The message is to Jo (housekeeper)" in facts
    assert "Dan is the one coming" in facts and "BK-1001" in facts


def _env(monkeypatch, *, parties, sms_dest=None, booking=None):
    from tests.on_my_way_fakes import claims
    rows = claims(monkeypatch)
    sent, logged = {}, []
    sent["claims"] = rows
    sent["deliveries"] = []
    async def access(*a, **k):
        return "test-token", "microsoft", {}
    monkeypatch.setattr(on_my_way.mailbox, "_access", access)
    monkeypatch.setattr(on_my_way.db, "get_booking", lambda *a, **k: dict(BOOKING if booking is None else booking))
    monkeypatch.setattr(on_my_way.db, "get_scheduling_config", lambda *a, **k: {"intake_fields": TRADE_INTAKE})
    monkeypatch.setattr(on_my_way.db, "list_job_parties", lambda t, b, client=None: parties)
    monkeypatch.setattr(on_my_way.db, "effective_config", lambda t, m, client=None: {"agent_name": "the Dovedale team"})
    monkeypatch.setattr(on_my_way.db, "insert_email_log", lambda *a, **k: ("log-omw", True))
    monkeypatch.setattr(on_my_way.db, "mark_email_log_sent", lambda *a, **k: logged.append(("sent", a[0], k.get("via"))))
    monkeypatch.setattr(on_my_way.db, "log_agent_event", lambda t, kind, summary, **k: logged.append((kind, summary)))
    monkeypatch.setattr(on_my_way.usage, "record_anthropic", lambda *a, **k: None)
    monkeypatch.setattr(on_my_way.sms, "preferred_sms_destination", lambda t, to: sms_dest)
    monkeypatch.setattr(on_my_way.disclosure, "wrap", lambda cfg, text, channel="email": text + " [ai]")

    async def compose(cfg, purpose, facts, channel="email", tenant_id=None, log_operation="outbound"):
        sent["purpose"] = purpose; sent["facts"] = facts; sent["channel"] = channel
        return SimpleNamespace(text="We're on our way — with you in about 30 minutes.", model="m", usage={"input_tokens": 1})
    monkeypatch.setattr(on_my_way.agent, "compose_outbound", compose)

    async def send_mail(tenant_id, to, subject, body, mailbox_id=None, **k):
        sent["mail"] = (to, subject, body)
        sent["deliveries"].append((tenant_id, to, body))
    monkeypatch.setattr(on_my_way.mailbox, "send_mail", send_mail)

    async def send_sms(tenant_id, to, body, email_log_id=None):
        sent["sms"] = (to, body)
        sent["deliveries"].append((tenant_id, to, body))
    monkeypatch.setattr(on_my_way.sms, "send_sms", send_sms)
    return sent, logged


def test_send_goes_to_jo_by_email_and_is_logged_as_sent_by_the_human(monkeypatch):
    jo = {"name": "Jo", "email": "jo@example.com", "label": "housekeeper", "on_site": True, "source": "confirmed"}
    sent, logged = _env(monkeypatch, parties=[jo])
    out = asyncio.run(on_my_way.send("t1", dict(BOOKING), "30", engineer="Dan"))
    assert out["ok"] and out["to"] == "jo@example.com" and out["channel"] == "email"
    assert sent["mail"][0] == "jo@example.com" and sent["mail"][1] == "On our way — BK-1001"
    assert sent["mail"][2].endswith("[ai]")                  # the disclosure wrap rides
    assert "Dan is the one coming" in sent["facts"] and "arrive in about 30 minutes" in sent["facts"]
    assert ("sent", "log-omw", "human") in logged
    assert any(e[0] == "on_my_way" and "Told Jo we're on our way to BK-1001" in e[1] for e in logged)


def test_send_takes_sms_when_the_contact_prefers_it(monkeypatch):
    sent, logged = _env(monkeypatch, parties=[], sms_dest="07700900123")
    out = asyncio.run(on_my_way.send("t1", dict(BOOKING), "10:45"))
    assert out["ok"] and out["channel"] == "sms" and out["to"] == "07700900123"
    assert sent["sms"][0] == "07700900123" and sent["channel"] == "sms"
    assert "mail" not in sent


def test_a_bad_eta_or_no_recipient_refuses_before_any_call(monkeypatch):
    sent, logged = _env(monkeypatch, parties=[], booking={**BOOKING, "contact": None})
    assert asyncio.run(on_my_way.send("t1", dict(BOOKING), "soon")) == {"ok": False, "error": "bad_eta"}
    assert asyncio.run(on_my_way.send("t1", {**BOOKING, "contact": None}, "30"))["error"] == "no_recipient"
    assert "purpose" not in sent


# ── the route ─────────────────────────────────────────────────────────────────
@pytest.fixture
def client(monkeypatch):
    from app import security
    monkeypatch.setattr(security, "_BUCKETS", {})
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


TRADE_INTAKE = [{"label": "Job address", "required": True}, {"label": "Job description", "required": True}]
KENNELS_INTAKE = [{"label": "Pet name(s) & breed", "required": True},
                  {"label": "Vaccinations up to date?", "required": True}]


def test_on_my_way_is_for_a_business_that_goes_to_the_customer():
    assert on_my_way.goes_to_customer(TRADE_INTAKE, BOOKING)
    assert not on_my_way.goes_to_customer(KENNELS_INTAKE, BOOKING)      # the dog comes to them
    assert not on_my_way.goes_to_customer(None, BOOKING)
    # an optional where-field defers to the booking: a pickup given is a visit, none is not
    lessons = [{"label": "Level / subject", "required": True}, {"label": "Pickup location", "required": False}]
    assert not on_my_way.goes_to_customer(lessons, BOOKING)
    assert on_my_way.goes_to_customer(lessons, {**BOOKING, "details": {"Pickup location": "12 Mill Lane"}})
    assert not on_my_way.goes_to_customer(lessons, {**BOOKING, "details": {"Pickup location": ""}})
    # a routed job always has somewhere to go
    assert on_my_way.goes_to_customer(KENNELS_INTAKE, {**BOOKING, "delivery_postcode": "LS9 8AS"})


def test_route_sends_and_returns_to_the_job_page(client, monkeypatch):
    calls = {}
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"scheduling"})
    monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: {"intake_fields": TRADE_INTAKE})
    monkeypatch.setattr(dashboard.db, "get_booking", lambda i, t, client=None: dict(BOOKING))

    async def fake_send(tenant_id, booking, eta, engineer=None, cfg=None, client=None, **kwargs):
        calls.update(eta=eta, engineer=engineer); return {"ok": True, "to": "kris@x.co", "channel": "email", "text": "On our way", "log_id": "l"}
    monkeypatch.setattr(on_my_way, "send", fake_send)

    async def resolve(*a, **k):
        calls["resolved"] = True          # must NOT happen: an ETA is not the visit (run #6 nit)
    monkeypatch.setattr(dashboard.crm_intel, "resolve_from_action", resolve)
    r = client.post("/app/diary/b1/on-my-way", data={"eta": "30", "engineer": "Dan"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/app/diary/b1?msg=omw_sent")
    assert calls == {"eta": "30", "engineer": "Dan"}


def test_the_purpose_forbids_a_guessed_pronoun():
    assert "Never guess a pronoun from a name" in on_my_way.PURPOSE


def test_route_refuses_a_booking_the_customer_comes_to(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"scheduling"})
    monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: {"intake_fields": KENNELS_INTAKE})
    monkeypatch.setattr(dashboard.db, "get_booking", lambda i, t, client=None: dict(BOOKING))

    async def fake_send(*a, **k):
        raise AssertionError("a stay is not on its way anywhere")
    monkeypatch.setattr(on_my_way, "send", fake_send)
    r = client.post("/app/diary/b1/on-my-way", data={"eta": "30"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("?msg=omw_nowhere")


def test_route_refuses_a_cancelled_booking_and_a_bad_eta(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"scheduling"})
    monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: {"intake_fields": TRADE_INTAKE})
    monkeypatch.setattr(dashboard.db, "get_booking", lambda i, t, client=None: {**BOOKING, "status": "cancelled"})
    r = client.post("/app/diary/b1/on-my-way", data={"eta": "30"}, follow_redirects=False)
    assert r.headers["location"].endswith("?msg=omw_notlive")
    monkeypatch.setattr(dashboard.db, "get_booking", lambda i, t, client=None: dict(BOOKING))

    async def fake_send(*a, **k):
        return {"ok": False, "error": "bad_eta"}
    monkeypatch.setattr(on_my_way, "send", fake_send)
    r = client.post("/app/diary/b1/on-my-way", data={"eta": "soon"}, follow_redirects=False)
    assert r.headers["location"].endswith("?msg=omw_bad_eta")


def test_the_job_page_carries_the_tap():
    from pathlib import Path
    html = Path("src/app/templates/app/diary_job.html").read_text(encoding="utf-8")
    assert "/on-my-way" in html and 'name="eta"' in html and "Send on my way" in html
    assert "and on_my_way_on %}" in html          # gated on a job the business travels to
    assert "omw_nowhere" in html


def test_retries_and_eta_changes_do_not_create_an_action_but_explicit_correction_does(monkeypatch):
    sent, logged = _env(monkeypatch, parties=[])
    first = asyncio.run(on_my_way.send("t1", BOOKING, "30"))
    again = asyncio.run(on_my_way.send("t1", BOOKING, "45"))
    assert first["ok"] and again["duplicate"] and first["eta"] == again["eta"]
    assert len(sent["deliveries"]) == 1
    key = "after:" + first["action_id"]
    corrected = asyncio.run(on_my_way.send("t1", BOOKING, "45", action_key=key))
    assert corrected["ok"] and corrected["action_id"] != first["action_id"]
    assert "45 minutes" in corrected["eta"]
    assert asyncio.run(on_my_way.send("t1", BOOKING, "60", action_key=key))["duplicate"]
    assert len(sent["deliveries"]) == 2
    assert len([e for e in logged if e[0] == "on_my_way"]) == 2


@pytest.mark.parametrize("failure", ["compose", "parties", "config", "log", "wrap", "usage"])
def test_pre_send_failures_are_retryable_without_sending(monkeypatch, failure):
    sent, _ = _env(monkeypatch, parties=[])
    def fail(*a, **k):
        raise RuntimeError("injected preparation failure")
    async def afail(*a, **k):
        fail()
    targets = {
        "compose": (on_my_way.agent, "compose_outbound", afail),
        "parties": (on_my_way.db, "list_job_parties", fail),
        "config": (on_my_way.db, "effective_config", fail),
        "log": (on_my_way.db, "insert_email_log", fail),
        "wrap": (on_my_way.disclosure, "wrap", fail),
        "usage": (on_my_way.usage, "record_anthropic", fail),
    }
    obj, name, replacement = targets[failure]
    original = getattr(obj, name)
    monkeypatch.setattr(obj, name, replacement)
    out = asyncio.run(on_my_way.send("t1", BOOKING, "30"))
    assert out["state"] == "pre_send_failure" and not sent["deliveries"]
    monkeypatch.setattr(obj, name, original)
    assert asyncio.run(on_my_way.send("t1", BOOKING, "30"))["ok"]
    assert len(sent["deliveries"]) == 1


@pytest.mark.parametrize("accepted", [False, True])
def test_provider_timeout_never_becomes_retryable_even_if_it_did_accept(monkeypatch, accepted):
    sent, _ = _env(monkeypatch, parties=[])
    async def uncertain(*a, **k):
        if accepted:
            sent["deliveries"].append("provider accepted then connection dropped")
        raise TimeoutError("no response")
    monkeypatch.setattr(on_my_way.mailbox, "send_mail", uncertain)
    out = asyncio.run(on_my_way.send("t1", BOOKING, "30"))
    assert out["state"] == "unknown"
    for eta in ("30", "45", "60"):
        retry = asyncio.run(on_my_way.send("t1", BOOKING, eta))
        assert not retry["ok"] and retry["state"] == "unknown"
    assert len(sent["deliveries"]) == int(accepted)


@pytest.mark.parametrize("operation", ["dispatch", "sent"])
@pytest.mark.parametrize("committed", [False, True])
def test_lost_database_response_never_causes_an_extra_send(monkeypatch, operation, committed):
    sent, _ = _env(monkeypatch, parties=[])
    original = on_my_way.store.action
    def lost(*args, **kwargs):
        if args[4] == operation:
            if committed:
                original(*args, **kwargs)
            raise TimeoutError("database response lost")
        return original(*args, **kwargs)
    monkeypatch.setattr(on_my_way.store, "action", lost)
    out = asyncio.run(on_my_way.send("t1", BOOKING, "30"))
    assert out["state"] == "unknown"
    monkeypatch.setattr(on_my_way.store, "action", original)
    retry = asyncio.run(on_my_way.send("t1", BOOKING, "30"))
    assert retry["duplicate"]
    assert len(sent["deliveries"]) == int(operation == "sent")
    assert retry["ok"] == (operation == "sent" and committed)


def test_process_crash_after_send_before_receipt_is_held(monkeypatch):
    sent, _ = _env(monkeypatch, parties=[])
    async def crash(*a, **k):
        sent["deliveries"].append("accepted")
        raise asyncio.CancelledError()
    monkeypatch.setattr(on_my_way.mailbox, "send_mail", crash)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(on_my_way.send("t1", BOOKING, "30"))
    assert asyncio.run(on_my_way.send("t1", BOOKING, "30"))["state"] == "unknown"
    assert sent["deliveries"] == ["accepted"]


def test_history_failure_does_not_discard_proven_send(monkeypatch):
    sent, _ = _env(monkeypatch, parties=[])
    def fail(*a, **k):
        raise RuntimeError("history unavailable")
    monkeypatch.setattr(on_my_way.db, "mark_email_log_sent", fail)
    assert asyncio.run(on_my_way.send("t1", BOOKING, "30"))["ok"]
    assert asyncio.run(on_my_way.send("t1", BOOKING, "30"))["ok"]
    assert len(sent["deliveries"]) == 1


@pytest.mark.parametrize("change", ["cancel", "recipient", "travel", "config"])
def test_changes_during_composition_refuse_stale_send(monkeypatch, change):
    sent, _ = _env(monkeypatch, parties=[])
    compose = on_my_way.agent.compose_outbound
    async def mutate(*a, **k):
        result = await compose(*a, **k)
        if change == "cancel":
            monkeypatch.setattr(on_my_way.db, "get_booking", lambda *a, **k: {**BOOKING, "status": "cancelled"})
        elif change == "recipient":
            monkeypatch.setattr(on_my_way.db, "list_job_parties", lambda *a, **k: [{"email": "someone@example.test"}])
        elif change == "travel":
            monkeypatch.setattr(on_my_way.db, "get_scheduling_config", lambda *a, **k: {"intake_fields": []})
        else:
            monkeypatch.setattr(on_my_way.db, "effective_config", lambda *a, **k: {"agent_name": "Changed"})
        return result
    monkeypatch.setattr(on_my_way.agent, "compose_outbound", mutate)
    assert asyncio.run(on_my_way.send("t1", BOOKING, "30"))["state"] == "pre_send_failure"
    assert not sent["deliveries"]


def test_store_failure_is_closed_and_other_tenant_is_unaffected(monkeypatch):
    sent, _ = _env(monkeypatch, parties=[])
    original = on_my_way.store.action
    def failure(tenant, *a, **k):
        if tenant == "t1":
            raise RuntimeError("unavailable")
        return original(tenant, *a, **k)
    monkeypatch.setattr(on_my_way.store, "action", failure)
    assert asyncio.run(on_my_way.send("t1", BOOKING, "30"))["error"] == "unavailable"
    assert asyncio.run(on_my_way.send("t2", BOOKING, "30"))["ok"]
    assert len(sent["deliveries"]) == 1 and sent["deliveries"][0][0] == "t2"


def test_owner_permission_removed_after_render_refuses_post(client, monkeypatch):
    monkeypatch.setattr(dashboard.people, "can_do", lambda *a, **k: False)
    sent, _ = _env(monkeypatch, parties=[])
    response = client.post("/app/diary/b1/on-my-way", data={"eta":"30"}, follow_redirects=False)
    assert response.status_code == 403 and not sent["claims"] and not sent["deliveries"]


def test_outbound_log_is_never_an_approvable_second_send(monkeypatch):
    sent, _ = _env(monkeypatch, parties=[])
    logs=[]
    def insert(*a, **k):
        logs.append(k)
        return "log-omw",True
    monkeypatch.setattr(on_my_way.db,"insert_email_log",insert)
    assert asyncio.run(on_my_way.send("t1",BOOKING,"30"))["ok"]
    assert logs[0]["status"] == "skipped" and logs[0]["kind"] == "on_my_way"


@pytest.mark.parametrize("status", ["pending", "skipped", "sent"])
def test_generic_approval_cannot_bypass_the_on_my_way_claim(monkeypatch, status):
    from fastapi import HTTPException
    sent,_=_env(monkeypatch,parties=[])
    monkeypatch.setattr(dashboard.db,"get_email_log_entry",lambda *a,**k: {
        "id":"l1", "kind":"on_my_way", "status":status, "draft_reply":"On our way"})
    def unexpected(*a,**k):
        raise AssertionError("must refuse before editing or sending")
    monkeypatch.setattr(dashboard.db,"update_email_log_draft",unexpected)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(dashboard.send_approved(TenantCtx(tenant_id="t1",client=None),"l1","On our way"))
    assert exc.value.status_code==409 and not sent["deliveries"]


@pytest.mark.parametrize("error", [on_my_way.sms.SmsNotConfigured, on_my_way.sms.SmsOptedOut])
def test_known_sms_pre_request_rejection_is_retryable(monkeypatch, error):
    sent,_=_env(monkeypatch,parties=[],sms_dest="07700900123")
    original=on_my_way.sms.send_sms
    async def rejected(*a,**k):
        raise error("rejected before HTTP")
    monkeypatch.setattr(on_my_way.sms,"send_sms",rejected)
    assert asyncio.run(on_my_way.send("t1",BOOKING,"30"))["state"]=="pre_send_failure"
    assert not sent["deliveries"]
    monkeypatch.setattr(on_my_way.sms,"send_sms",original)
    assert asyncio.run(on_my_way.send("t1",BOOKING,"30"))["ok"]
    assert len(sent["deliveries"])==1


def test_sms_response_loss_is_unknown(monkeypatch):
    sent,_=_env(monkeypatch,parties=[],sms_dest="07700900123")
    async def uncertain(*a,**k):
        sent["deliveries"].append("accepted")
        raise ValueError("response body was unreadable after HTTP")
    monkeypatch.setattr(on_my_way.sms,"send_sms",uncertain)
    assert asyncio.run(on_my_way.send("t1",BOOKING,"30"))["state"]=="unknown"
    assert asyncio.run(on_my_way.send("t1",BOOKING,"30"))["state"]=="unknown"
    assert sent["deliveries"]==["accepted"]


def test_durable_receipt_keeps_the_initiating_actor_and_exact_disclosed_body(monkeypatch):
    from app import actor
    sent,_=_env(monkeypatch,parties=[])
    monkeypatch.setattr(actor,"name_for",lambda *a,**k: "Jo")
    actor.remember("t1","u1","jo@example.test")
    try:
        assert asyncio.run(on_my_way.send("t1",BOOKING,"30"))["ok"]
        row=sent["claims"][("t1","b1","initial")]
        assert row["initiator"]=={"actor_user_id":"u1","actor_name":"Jo"}
        assert row["receipt"]["body"]==sent["mail"][2]
        # An inherited context cannot attribute another tenant's action to Jo.
        assert asyncio.run(on_my_way.send("t2",BOOKING,"30"))["ok"]
        assert sent["claims"][("t2","b1","initial")]["initiator"]=={}
    finally:
        actor.forget()


@pytest.mark.parametrize("failure", ["no_connection", "refresh"])
def test_dead_mailbox_preparation_retries_once(monkeypatch, failure):
    real_access = on_my_way.mailbox._access
    sent, _ = _env(monkeypatch, parties=[])
    monkeypatch.setattr(on_my_way.mailbox, "_access", real_access)
    mailbox = on_my_way.mailbox
    connection = {} if failure == "no_connection" else {"provider": "microsoft"}
    monkeypatch.setattr(mailbox.db, "get_email_connection", lambda *a: connection)
    monkeypatch.setattr(mailbox, "_provider", lambda *a: "microsoft")
    async def broken(*a):
        raise RuntimeError("refresh token revoked")
    async def healthy(*a):
        return "test-token"
    monkeypatch.setattr(mailbox, "valid_access_token", broken)
    failed = asyncio.run(on_my_way.send("t1", BOOKING, "30"))
    assert failed["state"] == "pre_send_failure" and not sent["deliveries"]
    assert "facts" not in sent  # Not even composed while access is unavailable.
    connection["provider"] = "microsoft"
    monkeypatch.setattr(mailbox, "valid_access_token", healthy)
    assert asyncio.run(on_my_way.send("t1", BOOKING, "30"))["ok"]
    assert asyncio.run(on_my_way.send("t1", BOOKING, "30"))["duplicate"]
    assert len(sent["deliveries"]) == 1


@pytest.mark.parametrize("failure", ["no_connection", "refresh"])
def test_mailbox_send_mail_raises_typed_pre_request_error(monkeypatch, failure):
    mailbox = on_my_way.mailbox
    async def unavailable(*a):
        if failure == "refresh":
            raise ValueError("refresh failed")
        return None, "microsoft", None
    monkeypatch.setattr(mailbox, "_access", unavailable)
    async def forbidden(*a, **k):
        raise AssertionError("no message request may be made")
    monkeypatch.setattr(mailbox.graph, "graph_post", forbidden)
    with pytest.raises(mailbox.MailNotSentBeforeRequest) as exc:
        asyncio.run(mailbox.send_mail("t1", "c@example.test", "Subject", "Body"))
    assert isinstance(exc.value, RuntimeError)


def test_mailbox_lost_between_preparation_and_send_is_still_retryable(monkeypatch):
    real_send = on_my_way.mailbox.send_mail
    sent, _ = _env(monkeypatch, parties=[])
    calls = []
    async def access(*a):
        calls.append(True)
        if len(calls) == 2:
            raise RuntimeError("refresh revoked after preparation")
        return "test-token", "microsoft", {}
    async def post(*a, **k):
        sent["deliveries"].append(k["json"])
    monkeypatch.setattr(on_my_way.mailbox, "_access", access)
    monkeypatch.setattr(on_my_way.mailbox, "send_mail", real_send)
    monkeypatch.setattr(on_my_way.mailbox.graph, "graph_post", post)
    assert asyncio.run(on_my_way.send("t1", BOOKING, "30"))["state"] == "pre_send_failure"
    assert not sent["deliveries"]
    assert asyncio.run(on_my_way.send("t1", BOOKING, "30"))["ok"]
    assert len(sent["deliveries"]) == 1


def test_owner_resend_route_preserves_history_and_actor_and_uses_selected_eta(client, monkeypatch):
    import json
    from app import actor
    sent, _ = _env(monkeypatch, parties=[])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"scheduling"})
    monkeypatch.setattr(actor, "stamp", lambda t: {"actor_user_id": "u1", "actor_name": "Jo"})
    original = on_my_way.mailbox.send_mail
    async def uncertain(*a, **k):
        await original(*a, **k)
        raise TimeoutError()
    monkeypatch.setattr(on_my_way.mailbox, "send_mail", uncertain)
    assert "omw_unknown" in client.post("/app/diary/b1/on-my-way", data={"eta":"30"}, follow_redirects=False).headers["location"]
    old = sent["claims"][("t1","b1","initial")]["attempt"]
    monkeypatch.setattr(on_my_way.mailbox, "send_mail", original)
    form = {"eta":"45", "action_key":"initial", "resend_attempt":old}
    assert "omw_sent" in client.post("/app/diary/b1/on-my-way", data=form, follow_redirects=False).headers["location"]
    client.post("/app/diary/b1/on-my-way", data=form, follow_redirects=False)
    assert len(sent["deliveries"]) == 2
    row = sent["claims"][("t1","b1","initial")]
    assert row["eta"] == "45" and "45 minutes" in row["receipt"]["eta"]
    evidence = json.loads(row["history"][0]["receipt"]["reconciliation_evidence"])
    assert evidence["reason"] == "owner chose to resend" and evidence["initiator"]["actor_name"] == "Jo"
    assert row["history"][0]["receipt"]["body"] == sent["deliveries"][0][2]


def test_owner_resend_requires_same_permission(client, monkeypatch):
    sent, _ = _env(monkeypatch, parties=[])
    monkeypatch.setattr(dashboard.people, "can_do", lambda *a, **k: False)
    response = client.post("/app/diary/b1/on-my-way", data={"eta":"30", "resend_attempt":"old"}, follow_redirects=False)
    assert response.status_code == 403 and not sent["claims"]


def test_old_historical_banner_hidden_without_removing_claim(monkeypatch):
    from datetime import timedelta
    today = datetime.now(on_my_way.DIARY_TZ).date()
    row = {"id":"historic", "state":"unknown", "receipt":{"reason":"historical_evidence_requires_review"}}
    monkeypatch.setattr(on_my_way.store, "latest", lambda *a: row)
    assert on_my_way.page_state("t1","b1",{"booking_date":str(today-timedelta(days=1))})["hide_historical"]
    assert not on_my_way.page_state("t1","b1",{"booking_date":str(today)}).get("hide_historical")
    assert not on_my_way.page_state("t1","b1",{"booking_date":str(today+timedelta(days=1))}).get("hide_historical")
    assert row["state"] == "unknown"


def test_promise_form_has_no_on_my_way_key():
    from pathlib import Path
    import re
    html = Path("src/app/templates/app/diary_job.html").read_text(encoding="utf-8")
    forms = re.findall(r'<form\b.*?</form>', html, re.S)
    promise = [f for f in forms if "/app/crm/promises/" in f and "/resolve" in f]
    assert promise and all('name="action_key"' not in f for f in promise)


@pytest.mark.parametrize("state,hidden,may_send", [
    ("unknown",False,True), ("unknown",True,True), ("unknown",False,False),
    ("sent",False,True), ("preparing",False,True), ("unavailable",False,True)])
def test_owner_page_uses_plain_words_and_limits_resend(state, hidden, may_send):
    from pathlib import Path
    from jinja2 import Environment, DictLoader, ChainableUndefined
    from html.parser import HTMLParser
    env = Environment(loader=DictLoader({"app/base.html":"{% block content %}{% endblock %}",
                                        "app/_say_to.html":""}), undefined=ChainableUndefined)
    env.filters["pretty_date"] = str
    env.filters["window_label"] = str
    env.globals["csrf_token"] = lambda r: "csrf"
    source = Path("src/app/templates/app/diary_job.html").read_text(encoding="utf-8")
    html = env.from_string(source).render(
        b=BOOKING, request=SimpleNamespace(query_params={}), on_my_way_on=True, may_assign=may_send,
        duty_labels={},
        omw_action={"id":"a1", "attempt":"try1", "action_key":"initial", "state":state,
                    "hide_historical":hidden, "receipt":{"recipient_name":"Jo", "eta":"at about 14:30"}},
        msg="omw_in_progress" if state=="preparing" else "")
    class Visible(HTMLParser):
        def __init__(self):
            super().__init__(); self.words=[]
        def handle_data(self, data):
            self.words.append(data)
    visible=Visible(); visible.feed(html)
    words=" ".join(visible.words).lower()
    assert all(word not in words for word in ("receipt", "verified", "reconcile", "original action"))
    if state=="unknown" and not hidden:
        assert "reached Jo" in html and "worst case they get it twice" in html
    else:
        assert "worst case they get it twice" not in html
    assert ('name="resend_attempt"' in html)==(state=="unknown" and not hidden and may_send)
    assert (">Send again</button>" in html)==(state=="unknown" and not hidden and may_send)


@pytest.mark.parametrize("first_route", ["owner", "sheet"])
def test_concurrent_owner_and_sheet_taps_share_customer_and_office_receipts(client, monkeypatch, first_route):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from app import job_sheet_public, push
    record = {**BOOKING, "booking_date": "2099-10-01"}
    sent, _ = _env(monkeypatch, parties=[], booking=record)
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"scheduling"})
    sheet = {"id": "s1", "tenant_id": "t1", "person_id": "p1"}
    monkeypatch.setattr(job_sheet_public, "_load", lambda _: (sheet, record, {"name": "Jo"}))
    monkeypatch.setattr(on_my_way.db, "claim_job_sheet", lambda *a, **k: True)
    notices = []
    monkeypatch.setattr(push, "notify", lambda *a, **k: notices.append(k))
    monkeypatch.setattr(push, "_prefs", lambda *a: {})
    monkeypatch.setattr(push, "_deliver", lambda t, payload, kind, cfg: notices.append({**payload, "kind": kind}))
    started, release = threading.Event(), threading.Event()
    compose = on_my_way.agent.compose_outbound
    async def delayed(*a, **k):
        started.set()
        assert await asyncio.to_thread(release.wait, 10)
        return await compose(*a, **k)
    monkeypatch.setattr(on_my_way.agent, "compose_outbound", delayed)
    paths = {"owner": "/app/diary/b1/on-my-way", "sheet": "/j/token/on-my-way"}
    def tap(route, eta="30"):
        return client.post(paths[route], data={"eta": eta}, follow_redirects=False)
    with ThreadPoolExecutor(2) as pool:
        pending = pool.submit(tap, first_route)
        assert started.wait(10)
        second = tap("sheet" if first_route == "owner" else "owner", "45")
        assert "in_progress" in second.headers["location"]
        release.set()
        assert pending.result(10).status_code == 303
    assert "omw_already" in tap("sheet").headers["location"]
    tap("owner")
    tap("sheet")
    assert len(sent["deliveries"]) == 1
    assert len(notices) == 1 and notices[0]["kind"] == "team_on_way"
