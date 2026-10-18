"""Phone page tests use the real Jinja template and browser script, offline."""
from pathlib import Path
import json
import uuid

import pytest
from tests.test_telephone_inbound_routes import api, signed, carrier_form

ROOT=Path(__file__).resolve().parents[1]


def render_phone():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app import telephone_live_routes as routes
    from app.auth import TenantCtx
    from unittest.mock import patch
    app=FastAPI()
    app.include_router(routes.router)
    ctx=TenantCtx(tenant_id=str(uuid.uuid4()),user_id=str(uuid.uuid4()),client=None)
    app.dependency_overrides[routes._owner]=lambda:ctx
    def context(active,ctx,**extra):
        return dict(active=active,tenant={"name":"Offline business"},
            me={"role":"owner","active":True},nav_items=[],modules=[],
            asset_rev="offline-phone-test",app_revision="offline-phone-test",concierge_on=False,
            csrf_token=lambda request:"offline-csrf",**extra)
    with patch.object(routes,"_ctx",context),TestClient(app) as client:
        response=client.get("/app/phone")
    assert response.status_code==200
    return response.text


def test_real_phone_page_separates_inbound_outbound_and_preserves_truthful_limits():
    html=render_phone()
    assert 'id="phone-inbound"' in html and 'id="phone-outbound"' in html
    assert "Configuration checks do not prove a working call" in html
    assert "Called person presses 1 to accept handover" in html
    assert "BLAM8 has not changed or independently tested the carrier" in html
    assert "Required information controls task completion" in html


def test_disabled_phone_renders_without_telephone_schema_or_service(monkeypatch):
    from app import telephone_live_routes as routes
    monkeypatch.setattr(routes.settings,"telephone_live_enabled",False)
    monkeypatch.setattr(routes,"get_service",lambda:pytest.fail("disabled page must not query telephone schema"))
    assert "Telephone service is disabled" in render_phone()




def test_fallback_requires_failed_handover_and_exact_message_revision(api,monkeypatch):
    from app import telephone_live_routes as routes
    calls=[]
    row={"id":str(uuid.uuid4()),"tenant_id":api[1].tenant_id,"transfer":{"status":"answered"},
         "message_revision":2,"message_text":"Caller message.","remaining_seconds":40}
    class Service:
        def resolve_carrier(self,*args):
            return row
        def command(self,*args):
            calls.append(args)
            return row
    monkeypatch.setattr(routes,"get_service",lambda:Service())
    path="/webhook/telephone/twilio/fallback-message"
    assert signed(api[0],path,dict(carrier_form(),SpeechResult="Caller message.")).status_code==409
    row["transfer"]["status"]="failed"
    assert signed(api[0],"/webhook/telephone/twilio/fallback-confirm?revision=1",
                  dict(carrier_form(),Digits="1")).status_code==409
    assert not calls


def test_fallback_invalid_signature_has_no_effect_and_crm_failure_is_truthful(api,monkeypatch):
    from app import telephone_live_routes as routes
    from app.telephone_inbound_store import InboundStoreError
    calls=[]
    row={"id":str(uuid.uuid4()),"tenant_id":api[1].tenant_id,"transfer":{"status":"failed"},
         "message_revision":1,"message_text":"Caller message.","remaining_seconds":40}
    class Service:
        def resolve_carrier(self,*args):
            calls.append("resolve")
            return row
        def command(self,tenant,user,action,payload):
            calls.append(action)
            if action=="outbox_deliver":
                raise InboundStoreError("store_unavailable")
            return row
    monkeypatch.setattr(routes,"get_service",lambda:Service())
    path="/webhook/telephone/twilio/fallback-confirm?revision=1"
    assert api[0].post(path,data=dict(carrier_form(),Digits="1")).status_code==403
    assert not calls
    response=signed(api[0],path,dict(carrier_form(),Digits="1"))
    assert response.status_code==200
    assert "delivery needs the business to check it" in response.text
    assert "has been saved for the business" not in response.text
    assert calls==["resolve","message","outbox_deliver"]


def test_handover_callee_binding_rejects_forged_parent_or_target(api,monkeypatch):
    from app import telephone_live_routes as routes
    calls=[]
    transfer_id=str(uuid.uuid4())
    row={"id":str(uuid.uuid4()),"tenant_id":api[1].tenant_id,"provider_call_id":"CA"+"a"*32,
        "transfer":{"id":transfer_id,"target":"+447700900020","provider_leg_id":"CA"+"b"*32,"status":"accepted"},
        "remaining_seconds":40}
    class Service:
        def resolve_carrier(self,*args):return row
        def command(self,*args):calls.append(args);return row
    monkeypatch.setattr(routes,"get_service",lambda:Service())
    path="/webhook/telephone/twilio/handover-accept?transfer_id="+transfer_id
    form=dict(carrier_form(),ParentCallSid="wrong-parent",CallSid="CA"+"b"*32,To="+447700900020",Digits="1")
    assert signed(api[0],path,form).status_code==409
    form.update(ParentCallSid=row["provider_call_id"],To="+447700900099")
    assert signed(api[0],path,form).status_code==409
    assert not calls


def test_handover_without_explicit_digit_one_cannot_confirm_human(api,monkeypatch):
    from app import telephone_live_routes as routes
    calls=[]
    transfer_id=str(uuid.uuid4())
    row={"id":str(uuid.uuid4()),"tenant_id":api[1].tenant_id,"provider_call_id":"CA"+"a"*32,
        "transfer":{"id":transfer_id,"target":"+447700900020","provider_leg_id":"CA"+"b"*32,"status":"accepted"},
        "remaining_seconds":40}
    class Service:
        def resolve_carrier(self,*args):return row
        def command(self,*args):calls.append(args);return row
    monkeypatch.setattr(routes,"get_service",lambda:Service())
    path="/webhook/telephone/twilio/handover-accept?transfer_id="+transfer_id
    form=dict(carrier_form(),ParentCallSid=row["provider_call_id"],CallSid="CA"+"b"*32,To="+447700900020",Digits="2")
    response=signed(api[0],path,form)
    assert response.status_code==200 and "Hangup" in response.text and not calls
    form["Digits"]="1"
    assert signed(api[0],path,form).status_code==200
    assert len(calls)==1 and calls[0][2]=="human_accept"


def test_fallback_callbacks_replay_without_unconfirming_or_duplicate_message_write(api,monkeypatch):
    from app import telephone_live_routes as routes
    from app.telephone_inbound_store import InboundStoreError
    writes=[]
    row={"id":str(uuid.uuid4()),"tenant_id":api[1].tenant_id,"transfer":{"status":"failed"},
         "message_revision":0,"message_text":None,"message_confirmed":False,"remaining_seconds":40}
    class Service:
        def resolve_carrier(self,*args):return dict(row)
        def command(self,tenant,user,action,payload):
            if action=="message":
                if payload["expected_revision"]!=row["message_revision"]:
                    raise InboundStoreError("revision_conflict")
                writes.append(payload)
                row.update(message_revision=row["message_revision"]+1,message_text=payload["text"],message_confirmed=payload["confirmed"])
            return dict(row)
    monkeypatch.setattr(routes,"get_service",lambda:Service())
    speech="/webhook/telephone/twilio/fallback-message?expected_revision=0"
    form=dict(carrier_form(),SpeechResult="Please call Tuesday.")
    assert signed(api[0],speech,form).status_code==200
    assert signed(api[0],speech,form).status_code==200
    assert len(writes)==1 and row["message_revision"]==1
    confirm="/webhook/telephone/twilio/fallback-confirm?revision=1"
    assert signed(api[0],confirm,dict(carrier_form(),Digits="1")).status_code==200
    assert signed(api[0],confirm,dict(carrier_form(),Digits="1")).status_code==200
    assert signed(api[0],speech,form).status_code==200
    assert len(writes)==2 and row["message_confirmed"] is True and row["message_revision"]==2
    assert signed(api[0],speech,dict(form,SpeechResult="Different stale text.")).status_code==409
    assert len(writes)==2


def test_fallback_correction_capture_binds_current_revision(api,monkeypatch):
    from app import telephone_live_routes as routes
    row={"id":str(uuid.uuid4()),"tenant_id":api[1].tenant_id,"transfer":{"status":"failed"},
         "message_revision":1,"message_text":"Correct me.","message_confirmed":False,"remaining_seconds":40}
    class Service:
        def resolve_carrier(self,*args):return row
        def command(self,*args):pytest.fail("requesting correction must not confirm or change text")
    monkeypatch.setattr(routes,"get_service",lambda:Service())
    response=signed(api[0],"/webhook/telephone/twilio/fallback-confirm?revision=1",dict(carrier_form(),Digits="2"))
    assert response.status_code==200 and "expected_revision=1" in response.text


def test_policy_reload_reads_only_authenticated_owner_tenant(api,monkeypatch):
    from app import telephone_live_routes as routes
    calls=[]
    class Store:
        def command(self,*args):
            calls.append(args)
            return {"daily_budget_minor":350,"enabled":False}
    monkeypatch.setattr(routes,"TelephoneStore",Store)
    response=api[0].get("/api/telephone/policy")
    assert response.status_code==200 and response.json()["daily_budget_minor"]==350
    assert calls==[(api[1].tenant_id,api[1].user_id,"config")]
    api[2]["role"]="staff"
    assert api[0].get("/api/telephone/policy").status_code==403
    assert len(calls)==1
