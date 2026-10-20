"""Real HTTP handlers with a PostgreSQL-backed PostgREST adapter and two browsers."""

import json
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.testclient import TestClient
import pytest

from app import device_sync as sync
from app.auth import TenantCtx, require_tenant
from tests.test_device_sync_database import cluster, sync_db, sync_case

ROOT = Path(__file__).resolve().parents[1]


class Store:
    """Use real migration functions, not an in-memory revision approximation."""

    def __init__(self, case):
        self.case = case

    def rpc(self, name, args):
        self.name, self.args = name, args
        return self

    def table(self, name):
        assert name == "device_sync"
        self.name, self.filters = name, {}
        return self

    def select(self, _columns):
        return self

    def eq(self, name, value):
        self.filters[name] = value
        return self

    def execute(self):
        (driver, dsn), _, _ = self.case
        with driver.connect(dsn, autocommit=True) as c:
            c.execute("set role service_role")
            if self.name == "device_sync":
                rows = c.execute(
                    "select revision,payload from device_sync where tenant_id=%s and user_id=%s and channel=%s",
                    tuple(self.filters[k] for k in ("tenant_id", "user_id", "channel")),
                ).fetchall()
                data = [dict(revision=r[0], payload=r[1]) for r in rows]
            else:
                args = {
                    k: json.dumps(v) if isinstance(v, (dict, list)) else v
                    for k, v in self.args.items()
                }
                parameters = ",".join(k + " => %s" for k in args)
                data = c.execute(
                    f"select {self.name}({parameters})", tuple(args.values())
                ).fetchone()[0]
        return type("Result", (), {"data": data})()


@pytest.fixture
def sync_app(sync_case, monkeypatch):
    _, tenant, user = sync_case
    ctx = TenantCtx(str(tenant), None, str(user))

    async def auth(request: Request, response: Response):
        return ctx

    monkeypatch.setattr(sync.settings, "device_sync_enabled", True)
    monkeypatch.setattr(sync, "require_tenant", auth)
    monkeypatch.setattr(sync.db, "db", lambda: Store(sync_case))
    app = FastAPI()
    app.dependency_overrides[require_tenant] = auth
    app.include_router(sync.router)
    routes = APIRouter(route_class=sync.SyncRoute)
    calls = []
    tuned = []
    from app import setup_handoff

    monkeypatch.setattr(
        setup_handoff,
        "load_steps",
        lambda *a, **k: (
            []
            if tuned
            else [
                {
                    "href": "/app/diary/settings",
                    "done": False,
                    "key": "diary",
                    "label": "Diary",
                }
            ]
        ),
    )

    @routes.get("/onboarding/{stage}")
    @routes.get("/app/{stage}")
    def page(request: Request, stage: str):
        path = request.url.path
        knowledge = stage == "knowledge"
        action = "/onboarding/knowledge/review/review-one" if knowledge else path
        review = ' data-knowledge-review="review-one"' if knowledge else ""
        strip = "<div data-setup-strip></div>" if path == "/app/diary/settings" else ""
        # The same sync and guide scripts used by the application, with a small
        # display adapter. Real auth, model calls and customer data are excluded.
        html = f'''<!doctype html><html><body>{strip}<main><form method="post" action="{action}"{review}>
          <input name="csrf_token" type="hidden" value="test-csrf">
          <input name="version" type="hidden" value="v1">
          <input name="title" value=""><input name="action_one" value="">
          <button type="submit">Continue</button></form></main><div id="messages"></div>
          <script src="/static/device_sync.js"></script>
          <script src="/static/knowledge_guidance.js"></script>
          <script>Blam8DeviceSync.register({{busy:()=>false,set:messages=>{{document.getElementById('messages').textContent=JSON.stringify(messages);}}}});</script>
          </body></html>'''
        return HTMLResponse(html)

    @routes.get("/app/diary/settings")
    def tuning(request: Request):
        return page(request, "settings")

    @routes.get("/app/")
    def app_home(request: Request):
        return page(request, "home")

    @routes.post("/onboarding/{stage}")
    @routes.post("/onboarding/knowledge/review/review-one")
    @routes.post("/app/diary/settings")
    async def advance(request: Request):
        data = await request.form()
        calls.append(dict(data))
        stage = request.url.path.rsplit("/", 1)[-1]
        nxt = {
            "interview": "/onboarding/mailbox",
            "mailbox": "/onboarding/voice",
            "voice": "/onboarding/knowledge",
            "review-one": "/app/diary/settings",
            "settings": "/app/",
        }[stage]
        if stage == "settings":
            tuned.append(True)
        return RedirectResponse(nxt, status_code=303)

    @routes.post("/onboarding/knowledge/review/review-one/guide")
    async def guide(request: Request):
        body = await request.json()
        calls.append(body)
        return {
            "reply": "First decision kept; review the next note.",
            "patches": {"action_one": "keep"},
            "focus_hash": "next-decision",
            "focus": {"kind": "note", "id": "two"},
            "actions": [],
            "save_ready": False,
        }

    @routes.post("/concierge/help")
    @routes.post("/concierge/onboard")
    async def answer(request: Request):
        body = await request.json()
        calls.append(body)
        return {"reply": "A shared answer to " + body["question"]}

    @routes.post("/concierge/validated")
    def validated(value: int):
        return {"value": value}

    @routes.get("/static/{filename}")
    def script(filename: str):
        assert filename in ("device_sync.js", "knowledge_guidance.js")
        return Response(
            (ROOT / "src/app/static" / filename).read_text(),
            media_type="application/javascript",
        )

    app.include_router(routes)
    return app, calls, ctx


def client(sync_app):
    return TestClient(sync_app[0], cookies={"bl_csrf": "test-csrf"})


def test_duplicate_continue_runs_once_and_stale_phone_cannot_advance(sync_app):
    c = client(sync_app)
    form = {
        "csrf_token": "test-csrf",
        "_sync_operation": str(uuid4()),
        "_sync_revision": "0",
        "title": "Reviewed",
    }
    first = c.post("/onboarding/interview", data=form, follow_redirects=False)
    assert first.status_code == 303
    assert (
        c.post("/onboarding/interview", data=form, follow_redirects=False).status_code
        == 303
    )
    assert len(sync_app[1]) == 1

    form["_sync_operation"] = str(uuid4())
    assert c.post("/onboarding/interview", data=form).status_code == 409
    assert len(sync_app[1]) == 1
    assert c.get("/device-sync/flow").json()["payload"]["path"] == "/onboarding/mailbox"


def test_assistant_response_survives_sender_closing_before_browser_save(sync_app):
    c = client(sync_app)
    headers = {
        "X-CSRF-Token": "test-csrf",
        "X-Blam8-Operation": str(uuid4()),
        "X-Blam8-Revision": "0",
    }
    body = {"question": "Help on my phone"}
    assert c.post("/concierge/help", json=body, headers=headers).status_code == 200
    assert c.post("/concierge/help", json=body, headers=headers).status_code == 200
    assert len(sync_app[1]) == 1
    state = c.get("/device-sync/assistant")
    assert state.headers["cache-control"] == "no-store"
    assert [m["role"] for m in state.json()["payload"]["messages"]] == [
        "user",
        "assistant",
    ]


def test_two_devices_share_one_automatic_greeting(sync_app):
    c = client(sync_app)
    headers = {
        "X-CSRF-Token": "test-csrf",
        "X-Blam8-Operation": str(uuid4()),
        "X-Blam8-Revision": "0",
        "X-Blam8-Automatic": "1",
    }
    body = {"question": "Start this step", "step": "interview"}
    assert c.post("/concierge/onboard", json=body, headers=headers).status_code == 200
    headers["X-Blam8-Operation"] = str(uuid4())
    assert c.post("/concierge/onboard", json=body, headers=headers).status_code == 200
    assert len(sync_app[1]) == 1
    messages = c.get("/device-sync/flow").json()["payload"]["messages"]
    assert [m["role"] for m in messages] == ["assistant"]


def test_sync_requires_csrf_and_never_accepts_external_navigation(sync_app):
    c = client(sync_app)
    payload = {
        "operation": str(uuid4()),
        "revision": 0,
        "patch": {"path": "https://elsewhere.invalid/"},
    }
    assert c.post("/device-sync/flow", json=payload).status_code == 403
    assert (
        c.post(
            "/device-sync/flow", json=payload, headers={"X-CSRF-Token": "test-csrf"}
        ).status_code
        == 422
    )
    payload["patch"] = {"complete": True}
    assert (
        c.post(
            "/device-sync/flow", json=payload, headers={"X-CSRF-Token": "test-csrf"}
        ).status_code
        == 422
    )


def test_view_as_cannot_read_or_write_device_sessions(sync_app):
    sync_app[2].viewing_as = str(uuid4())
    assert client(sync_app).get("/device-sync/assistant").status_code == 403


def test_validation_failure_releases_claim_and_query_changes_are_not_replayed(sync_app):
    c = client(sync_app)
    headers = {
        "X-CSRF-Token": "test-csrf",
        "X-Blam8-Operation": str(uuid4()),
        "X-Blam8-Revision": "0",
    }
    assert c.post("/concierge/validated", json={}, headers=headers).status_code == 422
    headers.update({"X-Blam8-Operation": str(uuid4()), "X-Blam8-Revision": "1"})
    assert c.post("/concierge/validated?value=1", json={}, headers=headers).json() == {
        "value": 1
    }
    assert (
        c.post("/concierge/validated?value=2", json={}, headers=headers).status_code
        == 409
    )


@pytest.mark.parametrize(
    "path",
    [
        "//elsewhere.invalid/path",
        "/onboarding/../login",
        "/app/settings",
        "/onboarding/knowledge\\evil",
    ],
)
def test_navigation_is_limited_to_setup_routes(path):
    assert not sync.path_allowed(path)
