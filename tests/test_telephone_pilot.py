"""Offline telephone boundary tests; no real carrier or model calls."""
import pytest
from pydantic import ValidationError

from app.config import Settings


def test_pilot_defaults_disabled_and_no_live_provider_is_selectable():
    config = Settings(_env_file=None)
    assert config.telephone_pilot_enabled is False
    assert config.telephone_pilot_provider == "simulated"
    assert config.telephone_pilot_webhook_secret == ""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, telephone_pilot_provider="twilio")


def test_callback_secret_is_not_in_settings_repr():
    config = Settings(_env_file=None, telephone_pilot_webhook_secret="offline-secret-marker")
    assert "offline-secret-marker" not in repr(config)


@pytest.fixture
def api(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app import db, telephone_routes as routes
    from app.auth import TenantCtx, require_onboarded
    from app.config import settings
    from app.security import CSRF_COOKIE
    import uuid

    ctx = TenantCtx(tenant_id=str(uuid.uuid4()), user_id=str(uuid.uuid4()), client=None)
    member = {"user_id": ctx.user_id, "role": "owner", "active": True}
    calls = []
    class Store:
        def command(self, tenant, user, action, payload=None):
            calls.append((tenant,user,action,payload))
            return {"status": "pending_approval", "calls": []}
    monkeypatch.setattr(routes,"get_store",lambda: Store())
    monkeypatch.setattr(db,"get_member",lambda *args: member)
    monkeypatch.setattr(settings,"telephone_pilot_enabled",True)
    monkeypatch.setattr(settings,"telephone_pilot_webhook_secret","s"*32)
    monkeypatch.setattr(settings,"rate_limit_enabled",False)
    app=FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_onboarded]=lambda: ctx
    with TestClient(app) as client:
        client.cookies.set(CSRF_COOKIE,"offline-csrf")
        yield client,ctx,member,calls,settings


def callback_body():
    import uuid
    return dict(idempotency_key=str(uuid.uuid4()),to_number="+447700900001",
                from_number="+447700900002",objective="Record callback result",
                script="Ask whether the requested callback is still needed.",max_duration_seconds=60)


def test_http_requires_csrf_and_ignores_no_body_identity(api):
    client,ctx,member,calls,settings=api
    path="/api/telephone-pilot/calls"
    assert client.post(path,json=callback_body()).status_code==403
    assert not calls
    body=callback_body()
    body["tenant_id"]="another-tenant"
    assert client.post(path,json=body,headers={"X-CSRF-Token":"offline-csrf"}).status_code==422
    assert not calls
    assert client.post(path,json=callback_body(),headers={"X-CSRF-Token":"offline-csrf"}).status_code==200
    assert calls[-1][:3]==(ctx.tenant_id,ctx.user_id,"create")


def test_http_fresh_role_and_view_as_block_reads_and_writes(api):
    client,ctx,member,calls,settings=api
    assert client.get("/api/telephone-pilot/calls").status_code==200
    member["role"]="staff"
    assert client.get("/api/telephone-pilot/calls").status_code==403
    member["role"]="owner"
    ctx.viewing_as="real-owner"
    assert client.get("/api/telephone-pilot/calls").status_code==403
    assert len(calls)==1


def test_disabled_blocks_new_calls_but_existing_records_and_cancel_available(api):
    import uuid
    client,ctx,member,calls,settings=api
    settings.telephone_pilot_enabled=False
    assert client.post("/api/telephone-pilot/calls",json=callback_body(),
                       headers={"X-CSRF-Token":"offline-csrf"}).status_code==404
    assert client.get("/api/telephone-pilot/calls").status_code==200
    assert client.post("/api/telephone-pilot/calls/"+str(uuid.uuid4())+"/cancel",
                       headers={"X-CSRF-Token":"offline-csrf"}).status_code==200
    assert [c[2] for c in calls]==["list","cancel"]


def test_webhook_missing_secret_tamper_and_oversize_do_not_mutate(api):
    client,ctx,member,calls,settings=api
    assert client.post("/webhook/telephone-pilot",content=b"{}").status_code==403
    settings.telephone_pilot_webhook_secret=""
    assert client.post("/webhook/telephone-pilot",content=b"{}",
                       headers={"X-Telephone-Signature":"a"*64}).status_code==403
    assert client.post("/webhook/telephone-pilot",content=b"x"*8193).status_code==413
    assert not calls


def test_signed_terminal_event_still_reconciles_when_disabled(api):
    import json,uuid
    from app.telephone_provider import sign_simulation_event
    client,ctx,member,calls,settings=api
    settings.telephone_pilot_enabled=False
    raw=json.dumps(dict(tenant_id=ctx.tenant_id,call_id=str(uuid.uuid4()),provider_call_id="sim_x",
                        event_id="event-1",sequence=1,status="completed")).encode()
    signature=sign_simulation_event(raw,settings.telephone_pilot_webhook_secret)
    assert client.post("/webhook/telephone-pilot",content=raw+b" ",
                       headers={"X-Telephone-Signature":signature}).status_code==403
    assert client.post("/webhook/telephone-pilot",content=raw,
                       headers={"X-Telephone-Signature":signature}).status_code==200
    assert len(calls)==1 and calls[0][:3]==(ctx.tenant_id,None,"event")


@pytest.mark.parametrize("message,expected",[("invalid_state","invalid_state"),
    ("SQL context with telephone number and token","store_unavailable"),
    ({"untrusted":"error"},"store_unavailable")])
def test_store_error_boundary_only_exposes_exact_safe_codes(message,expected):
    from app.telephone_store import TelephoneStore,StoreError
    class ProviderError(Exception):
        pass
    class Client:
        def rpc(self,*args):
            error=ProviderError("unsafe detail")
            error.message=message
            raise error
    with pytest.raises(StoreError) as caught:
        TelephoneStore(Client()).command("tenant","user","list")
    assert caught.value.code==expected


def test_store_malformed_response_fails_closed():
    from types import SimpleNamespace
    from app.telephone_store import TelephoneStore,StoreError
    class Client:
        def rpc(self,*args):
            return self
        def execute(self):
            return SimpleNamespace(data=[])
    with pytest.raises(StoreError,match="store_unavailable"):
        TelephoneStore(Client()).command("tenant","user","list")


def test_http_unavailable_store_is_service_failure_not_success_or_conflict(api, monkeypatch):
    from app import telephone_routes as routes
    from app.telephone_store import StoreError
    class Unavailable:
        def command(self, *args):
            raise StoreError("store_unavailable")
    monkeypatch.setattr(routes, "get_store", lambda: Unavailable())
    response = api[0].get("/api/telephone-pilot/calls")
    assert response.status_code == 503
    assert response.json()["detail"] == "store_unavailable"
