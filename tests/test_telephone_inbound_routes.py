"""HTTP authentication and signed carrier boundaries, entirely offline."""
import json
import uuid
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db, telephone_live_routes as routes
from app.auth import TenantCtx,require_onboarded
from app.config import settings
from app.security import CSRF_COOKIE
from app.telephone_inbound_store import InboundStoreError

ACCOUNT="AC"+"1"*32


@pytest.fixture
def api(monkeypatch):
    ctx=TenantCtx(tenant_id=str(uuid.uuid4()),user_id=str(uuid.uuid4()),client=None)
    member={"user_id":ctx.user_id,"role":"owner","active":True}
    calls=[]
    class Service:
        def command(self,tenant,user,action,payload=None):
            calls.append((tenant,user,action,payload))
            if action == "finish_handover":
                assert payload["parent_call_id"] == "CAparent"
                return {"transfer":{"status":"failed"},"remaining_seconds":40,"message_revision":0}
            return {"calls":[],"enabled":False,"carrier_changed":False}
        def status(self,form,query):
            calls.append(("status",form,query))
            return {"status":"completed"}
        def voice(self,form,query):
            calls.append(("voice",form,query))
            return {}, "<Response><Hangup/></Response>"
        def transfer_event(self,form,query,ended=False):
            calls.append(("transfer",form,query,ended))
            return {"id":"call-1","tenant_id":ctx.tenant_id,"provider_call_id":"CAparent",
                    "transfer":{"id":"transfer-1","target":"+442079460102","status":"failed"}}
        def dispatch(self,*args):
            pytest.fail("disabled transport must never dispatch")
        def carrier_factory(self,*args):
            pytest.fail("disabled transport must never create carrier adapter")
    monkeypatch.setattr(routes,"get_service",lambda: Service())
    monkeypatch.setattr(db,"get_member",lambda *args: member)
    monkeypatch.setattr(settings,"telephone_live_enabled",False)
    monkeypatch.setattr(settings,"telephone_webhook_base_url","https://phone.example.test")
    monkeypatch.setattr(settings,"twilio_account_sid",ACCOUNT)
    monkeypatch.setattr(settings,"twilio_auth_token","offline-twilio-secret")
    monkeypatch.setattr(settings,"telephone_openai_webhook_secret","")
    monkeypatch.setattr(settings,"rate_limit_enabled",False)
    app=FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_onboarded]=lambda:ctx
    with TestClient(app) as client:
        client.cookies.set(CSRF_COOKIE,"offline-csrf")
        yield client,ctx,member,calls


def signed(client,path,form,**kwargs):
    from twilio.request_validator import RequestValidator
    signature=RequestValidator(settings.twilio_auth_token).compute_signature(
        settings.telephone_webhook_base_url+path,form)
    return client.post(path,content=urlencode(form),headers={
        "X-Twilio-Signature":signature,"Content-Type":"application/x-www-form-urlencoded",**kwargs})


def carrier_form():
    return dict(AccountSid=ACCOUNT,CallSid="CA"+"2"*32,To="+447700900010",From="+447700900013")


def test_owner_role_rechecked_and_view_as_denied(api):
    client,ctx,member,calls=api
    assert client.get("/api/telephone/calls").status_code==200
    member["role"]="staff"
    assert client.get("/api/telephone/calls").status_code==403
    member["role"]="owner"
    ctx.viewing_as="real-owner"
    assert client.get("/api/telephone/readiness").status_code==403
    assert len(calls)==1


def test_csrf_and_payload_tenant_cannot_offboard_other_tenant(api):
    client,ctx,member,calls=api
    assert client.post("/api/telephone/offboard",json={"restored":False}).status_code==403
    assert client.post("/api/telephone/offboard",json={"restored":False,"tenant_id":"other"},
                       headers={"X-CSRF-Token":"offline-csrf"}).status_code==422
    assert not calls
    assert client.post("/api/telephone/offboard",json={"restored":False},
                       headers={"X-CSRF-Token":"offline-csrf"}).status_code==200
    assert calls[0][:3]==(ctx.tenant_id,ctx.user_id,"offboard")


def test_live_disabled_blocks_dispatch_but_signed_status_reconciles(api):
    client,ctx,member,calls=api
    assert client.post("/api/telephone/calls/"+str(uuid.uuid4())+"/dispatch",
                       headers={"X-CSRF-Token":"offline-csrf"}).status_code==409
    assert signed(client,"/webhook/telephone/twilio/status",carrier_form()).status_code==200
    assert len(calls)==1 and calls[0][0]=="status"


def test_signature_canonical_origin_query_and_provider_account_bound(api):
    client,ctx,member,calls=api
    path="/webhook/telephone/twilio/status?call_id=one&attempt_id=two"
    assert signed(client,path,carrier_form(),Host="attacker.example").status_code==200
    wrong=dict(carrier_form(),AccountSid="AC"+"3"*32)
    assert signed(client,path,wrong).status_code==403
    assert client.post(path,data=carrier_form(),headers={"X-Twilio-Signature":"forged"}).status_code==403
    assert len(calls)==1


def test_duplicate_form_keys_and_oversized_payload_rejected_before_state(api):
    client,ctx,member,calls=api
    path="/webhook/telephone/twilio/status"
    assert client.post(path,content="AccountSid=a&AccountSid=b").status_code==422
    assert client.post(path,content=b"x"*32769).status_code==413
    assert not calls


def test_disabled_voice_hangs_up_without_model_or_state_admission(api):
    client,ctx,member,calls=api
    response=signed(client,"/webhook/telephone/twilio/voice",carrier_form())
    assert response.status_code==200 and "<Hangup" in response.text
    assert not calls


def test_failed_transfer_fallback_cannot_claim_connected(api):
    client,ctx,member,calls=api
    response=signed(client,"/webhook/telephone/twilio/transfer-ended",carrier_form())
    assert response.status_code==200
    assert "<Gather" in response.text and "<Hangup" in response.text
    assert "expected_revision=0" in response.text and "choosing whether to save" in response.text
    assert "<Dial" not in response.text
    assert any(len(item)==4 and item[2]=="finish_handover" for item in calls)


def test_unavailable_store_returns_retryable_failure(api,monkeypatch):
    class Failed:
        def command(self,*args):
            raise InboundStoreError("store_unavailable")
    monkeypatch.setattr(routes,"get_service",lambda:Failed())
    assert api[0].get("/api/telephone/calls").status_code==503


def test_absent_telephone_rpc_fails_closed_without_affecting_disabled_voice(api,monkeypatch):
    from app.telephone_inbound_store import TelephoneInboundStore
    from app.telephone_live import TelephoneLiveService
    class MissingSchema:
        def rpc(self,*args):
            raise RuntimeError("PGRST202: missing telephone_inbound_command")
    store=TelephoneInboundStore(client=MissingSchema())
    monkeypatch.setattr(routes,"get_service",lambda:TelephoneLiveService(store,objectives=object()))
    response=api[0].get("/api/telephone/calls")
    assert response.status_code==503 and response.json()=={"detail":"store_unavailable"}
    response=signed(api[0],"/webhook/telephone/twilio/voice",carrier_form())
    assert response.status_code==200 and "<Hangup" in response.text


def test_actual_inbound_store_not_found_is_not_generic_success_or_server_error(api,monkeypatch):
    class Missing:
        def command(self,*args):
            raise InboundStoreError("not_found")
    monkeypatch.setattr(routes,"get_service",lambda:Missing())
    assert api[0].get("/api/telephone/calls").status_code==404


def openai_signed(raw):
    import base64,hashlib,hmac,time
    secret=b"offline-webhook-secret-at-least32bytes"
    settings.telephone_openai_webhook_secret="whsec_"+base64.b64encode(secret).decode()
    timestamp=str(int(time.time()))
    signature=base64.b64encode(hmac.new(secret,b"wh_test."+timestamp.encode()+b"."+raw,hashlib.sha256).digest()).decode()
    return {"webhook-id":"wh_test","webhook-timestamp":timestamp,"webhook-signature":"v1,"+signature}


def incoming_body(headers):
    import time
    return json.dumps(dict(id="evt_incoming",object="event",created_at=int(time.time()),
        type="realtime.call.incoming",data=dict(call_id="rtc_incoming",sip_headers=headers))).encode()


def test_openai_signature_missing_or_duplicate_correlation_cannot_admit(api):
    client,ctx,member,calls=api
    raw=incoming_body([])
    assert client.post("/webhook/telephone/openai",content=raw).status_code==403
    assert client.post("/webhook/telephone/openai",content=raw,headers=openai_signed(raw)).status_code==403
    raw=incoming_body([{"name":"x-blam8-correlation","value":"a"*64}]*2)
    assert client.post("/webhook/telephone/openai",content=raw,headers=openai_signed(raw)).status_code==403
    assert not calls


def test_openai_already_consumed_correlation_never_accepts_again(api,monkeypatch):
    class Claimed:
        def command(self,*args):
            raise InboundStoreError("correlation_consumed")
        def realtime_factory(self):
            pytest.fail("replayed correlation must not perform model control")
    monkeypatch.setattr(routes,"get_service",lambda:Claimed())
    raw=incoming_body([{"name":"x-blam8-correlation","value":"a"*64}])
    response=api[0].post("/webhook/telephone/openai",content=raw,headers=openai_signed(raw))
    assert response.status_code==200 and response.json()["accepted"] is False
