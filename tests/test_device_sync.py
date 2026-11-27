"""Real HTTP handlers with a PostgreSQL-backed PostgREST adapter and two browsers."""

import json
import re
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
    from app import knowledge_guidance as guidance, llm
    from tests.test_knowledge_review import context, entry
    from app import concierge, usage
    from types import SimpleNamespace

    first = entry(
        label="Services", content="We handle requests and keep owners informed."
    )
    review_ctx, review_row = context(
        [first, entry("two", "Fees", "Management costs £95 each month.")],
        brief=first["content"],
    )
    review_row["id"] = "review-one"
    review_row["payload"]["questions"] = []
    review_version = review_ctx["version"]
    first_form = {"version": review_version, "action_" + first["id"]: "keep"}
    overlap = guidance.present(review_ctx, review_row, first_form)["focus"]
    match_name = "match_" + overlap["id"]

    async def inferred_keep(**kwargs):
        payload = {
            "intent": "keep",
            "reason": "Keep this note after editing it.",
            "answer": "",
            "target": "",
            "explanation": "Recorded as keep.",
        }
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(payload))],
            model="offline",
            usage={},
        )

    monkeypatch.setattr(llm, "create", inferred_keep)
    monkeypatch.setattr(
        concierge, "client", lambda: SimpleNamespace(with_options=lambda **kw: None)
    )
    monkeypatch.setattr(usage, "record_anthropic", lambda *a, **kw: None)
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

    # A test may serve its own page at a setup path in place of this one.
    app.state.pages = {}

    @routes.get("/onboarding/{stage}")
    @routes.get("/app/{stage}")
    def page(request: Request, stage: str):
        path = request.url.path
        if path in app.state.pages:
            return HTMLResponse(app.state.pages[path]())
        knowledge = stage == "knowledge"
        action = "/onboarding/knowledge/review/review-one" if knowledge else path
        review = ' data-knowledge-review="review-one"' if knowledge else ""
        strip = "<div data-setup-strip></div>" if path == "/app/diary/settings" else ""
        # The same sync and guide scripts used by the application, with a small
        # display adapter. Real auth, model calls and customer data are excluded.
        html = f'''<!doctype html><html><body>{strip}<main><form method="post" action="{action}"{review}>
          <input name="csrf_token" type="hidden" value="test-csrf">
          <input name="version" type="hidden" value="{review_version if knowledge else "v1"}">
          <input name="title" value=""><input name="action_{first["id"]}" value=""><input name="action_two" value="">
          <input type="checkbox" name="{match_name}" value="keep_both">
          <section data-draft="{first["id"]}"><textarea name="content_{first["id"]}">{first["content"]}</textarea></section>
          <button type="submit">Continue</button></form></main><div id="messages"></div>
          <script src="/static/device_sync.js"></script>
          <script src="/static/knowledge_guidance.js"></script>
          <script>Blam8DeviceSync.register({{busy:()=>!!window.blam8KnowledgeImportBusy,set:messages=>{{document.getElementById('messages').textContent=JSON.stringify(messages);}}}});</script>
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
        return await guidance.turn(
            str(tenant),
            review_ctx,
            review_row,
            body["form"],
            body["question"],
            body.get("focus_hash", ""),
        )

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
        assert filename in (
            "device_sync.js",
            "knowledge_guidance.js",
            "dirty_save.js",
            "setup_cards.js",
        )
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


def test_automatic_guide_rederives_changed_review_without_replaying_approval(sync_app, monkeypatch):
    from app import llm
    async def no_model(**_kwargs):
        raise AssertionError('Read-only current-note reconciliation must not call a model')
    monkeypatch.setattr(llm,'create',no_model)
    c=client(sync_app)
    version=re.search(r'name="version"[^>]*value="([^"]+)"',c.get('/onboarding/knowledge').text)[1]
    form={'version':version}
    url='/onboarding/knowledge/review/review-one/guide'
    def turn(question='',focus='',automatic=True,revision=None):
        current=c.get('/device-sync/flow').json()['revision']
        response=c.post(url,json={'form':form,'question':question,'focus_hash':focus},headers={
            'X-CSRF-Token':'test-csrf','X-Blam8-Operation':str(uuid4()),
            'X-Blam8-Revision':str(current if revision is None else revision),
            'X-Blam8-Automatic':'1' if automatic else '0'})
        return response
    first=turn();assert first.status_code==200
    shown=first.json();assert shown['focus']['kind']=='note'
    confirmed=turn('Keep this note',shown['focus_hash'],False).json()
    form.update(confirmed['patches']);assert confirmed['focus']['kind']=='overlap'
    overlap=turn('Keep both notes',confirmed['focus_hash'],False).json()
    form.update(overlap['patches']);assert overlap['focus']['id']=='two'
    resumed=turn();assert resumed.status_code==200
    assert resumed.json()['focus']['id']=='two' and resumed.json()['patches']=={}
    count=len(sync_app[1]);form=dict(reversed(list(form.items())))
    assert turn().json()==resumed.json() and len(sync_app[1])==count
    # A new automatic snapshot still needs the current revision; no decision is inferred.
    form['content_two']='Locally revised wording.'
    assert turn(revision=0).status_code==409
    assert len(sync_app[1])==count
    refreshed=turn();assert refreshed.status_code==200
    assert refreshed.json()['focus']['id']=='two' and refreshed.json()['patches']=={}


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


@pytest.mark.parametrize(
    "patch",
    [
        {"form": []},
        {"form": {"bad": None}},
        {
            "form": {
                "bad": {
                    "action": "/onboarding/knowledge",
                    "version": "v1",
                    "values": [],
                }
            }
        },
        {
            "form": {
                "bad": {
                    "action": "/onboarding/knowledge",
                    "version": "v1",
                    "values": {"title": "not-a-list"},
                }
            }
        },
        {
            "form": {
                "bad": {
                    "action": "/onboarding/knowledge",
                    "version": "v1",
                    "values": {"title": [3]},
                }
            }
        },
        {
            "form": {
                "bad": {
                    "action": "/onboarding/knowledge",
                    "page": 3,
                    "version": "v1",
                    "values": {},
                }
            }
        },
        {"guide": []},
        {"guide": {"review": "review-one", "version": "v1", "data": []}},
    ],
)
def test_malformed_checkpoint_rejected_before_claim(sync_app, patch):
    c = client(sync_app)
    body = {"operation": str(uuid4()), "revision": 0, "patch": patch}
    headers = {"X-CSRF-Token": "test-csrf"}
    assert c.post("/device-sync/flow", json=body, headers=headers).status_code == 422
    assert c.get("/device-sync/flow").json()["revision"] == 0
    body["patch"] = {"form": {}, "guide": {}}
    assert c.post("/device-sync/flow", json=body, headers=headers).status_code == 200


def saved_form(action, page=None, **values):
    """A synced form as the client stores it. A draft an older client stored
    has no page, only the action it posts to."""
    saved = {
        "action": action,
        "version": "",
        "values": {name: [value] for name, value in values.items()},
    }
    if page:
        saved["page"] = page
    return saved


@pytest.fixture
def card_app(sync_app):
    """Setup-card routes behind the sync wrapper, a way to seed the flow's
    synced forms, and a way to post to a route as a device would."""
    app = sync_app[0]
    routes = APIRouter(route_class=sync.SyncRoute)

    @routes.post("/concierge/setup-cards/{page}/accept")
    @routes.post("/concierge/setup-cards/{page}/preview")
    async def card(page: str):
        return {"card": {"page": page}}

    app.include_router(routes)
    c = client(sync_app)

    def seed(path, forms):
        revision = c.get("/device-sync/flow").json()["revision"]
        seeded = c.post(
            "/device-sync/flow",
            json={
                "operation": str(uuid4()),
                "revision": revision,
                "patch": {"path": path, "form": forms},
            },
            headers={"X-CSRF-Token": "test-csrf"},
        )
        assert seeded.status_code == 200 and seeded.json()["revision"] == revision + 1

    def post(path, channel="flow"):
        operation = str(uuid4())
        # The assistant channel is separate and keeps its own revision.
        revision = c.get("/device-sync/" + (channel or "assistant")).json()["revision"]
        headers = {
            "X-CSRF-Token": "test-csrf",
            "X-Blam8-Operation": operation,
            "X-Blam8-Revision": str(revision),
        }
        if channel:
            headers["X-Blam8-Channel"] = channel
        return operation, c.post(path, json={}, headers=headers)

    return c, seed, post


@pytest.fixture
def card_routes(card_app):
    """The flow already holding one synced form per tuning page (Getting paid
    and CRM settings)."""
    c, seed, post = card_app
    seed(
        "/app/invoices/settings",
        {
            "/app/invoices/settings:0": saved_form(
                "/app/invoices/settings",
                page="/app/invoices/settings",
                payment_terms="",
                vat_rate="20",
            ),
            "/app/crm/settings:0": saved_form(
                "/app/crm/settings", page="/app/crm/settings", stage_one="Enquiry"
            ),
        },
    )
    return c, post


def test_accepting_a_card_drops_that_pages_synced_form_and_asks_other_devices_to_reload(
    card_routes,
):
    c, post = card_routes
    operation, response = post("/concierge/setup-cards/invoice_settings/accept")
    assert response.status_code == 200
    state = c.get("/device-sync/flow").json()
    assert state["revision"] == 2
    assert list(state["payload"]["form"]) == ["/app/crm/settings:0"]
    assert state["payload"]["refresh"] == operation
    assert state["payload"]["path"] == "/app/invoices/settings"


@pytest.mark.parametrize(
    "path,channel",
    [
        ("/concierge/setup-cards/invoice_settings/preview", "flow"),
        ("/concierge/setup-cards/not_a_page/accept", "flow"),
        ("/concierge/setup-cards/invoice_settings/accept", None),
    ],
    ids=["preview", "unknown-page", "assistant-channel"],
)
def test_only_an_accept_on_the_flow_channel_for_a_known_page_touches_the_form(
    card_routes, path, channel
):
    c, post = card_routes
    _, response = post(path, channel)
    assert response.status_code == 200
    flow = c.get("/device-sync/flow").json()["payload"]
    assert sorted(flow["form"]) == ["/app/crm/settings:0", "/app/invoices/settings:0"]
    assert "refresh" not in flow


def test_an_accept_that_finishes_the_active_step_keeps_the_step_advance(
    card_routes, monkeypatch
):
    from app import setup_handoff

    monkeypatch.setattr(
        setup_handoff,
        "load_steps",
        lambda *a, **k: [
            {
                "href": "/app/invoices/settings",
                "done": True,
                "key": "invoices",
                "label": "Getting paid",
            }
        ],
    )
    c, post = card_routes
    operation, response = post("/concierge/setup-cards/invoice_settings/accept")
    assert response.status_code == 200
    payload = c.get("/device-sync/flow").json()["payload"]
    # The walk moved on and cleared every page's draft; the hint still goes out.
    assert payload["form"] == {} and payload["path"] == "/app/"
    assert payload["refresh"] == operation


# The templates each tuning page renders, so the forms seeded below post to
# the paths the real pages post to. Diary settings includes its scheduling form.
TUNING_PAGE_TEMPLATES = {
    "diary_settings": ("app/diary_settings.html", "app/_scheduling_settings.html"),
    "booking_details": ("app/diary_intake.html",),
    "dependants": ("app/diary_dependants.html",),
    "crm_settings": ("app/crm_settings.html",),
    "invoice_settings": ("app/invoice_settings.html",),
}


def form_actions(page):
    """The paths a tuning page's forms post to, with one stage id filled in."""
    actions = []
    for name in TUNING_PAGE_TEMPLATES[page]:
        source = (ROOT / "src/app/templates" / name).read_text(encoding="utf-8")
        for match in re.finditer(r'<form[^>]*\baction="([^"]+)"', source):
            path = match.group(1).partition("{% if choosing_records %}?")[0]
            actions.append(path.replace("{{ s.id }}", "7"))
    return actions


def test_tuning_page_forms_do_not_all_post_to_their_page():
    """Why the accept drops by page: Diary settings, CRM settings and Getting
    paid post somewhere other than their own path too. Order is not compared:
    an included form is read after the page template, not where it renders."""
    assert {
        page: sorted(form_actions(page)) for page in TUNING_PAGE_TEMPLATES
    } == {
        "diary_settings": [
            "/app/diary/cancel-all",
            "/app/diary/settings/booking-mode",
            "/app/settings/scheduling",
        ],
        "booking_details": ["/app/diary/intake"],
        "dependants": ["/app/diary/dependants", "/app/setup/guide/related-records"],
        "crm_settings": [
            "/app/crm/settings",
            "/app/crm/settings/stages",
            "/app/crm/settings/stages/7",
            "/app/crm/settings/stages/7/delete",
        ],
        "invoice_settings": [
            "/app/invoices/settings",
            "/app/payments/disconnect",
            "/app/payments/methods",
        ],
    }


@pytest.mark.parametrize("page", list(TUNING_PAGE_TEMPLATES))
def test_accepting_a_card_drops_every_form_of_its_page_whatever_they_post_to(
    card_app, page
):
    from app import settings_recommend

    c, seed, post = card_app
    hrefs = {key: settings_recommend.PAGES[key]["href"] for key in TUNING_PAGE_TEMPLATES}
    # Every tuning page's forms as the client captures them: keyed by the path
    # they post to and their index, each recording the page it was captured on.
    forms = {
        f"{action}:{index}": saved_form(action, page=hrefs[key], field="typed")
        for key in TUNING_PAGE_TEMPLATES
        for index, action in enumerate(form_actions(key))
    }
    seed(hrefs[page], forms)
    operation, response = post(f"/concierge/setup-cards/{page}/accept")
    assert response.status_code == 200
    payload = c.get("/device-sync/flow").json()["payload"]
    assert sorted(payload["form"]) == sorted(
        key for key, saved in forms.items() if saved["page"] != hrefs[page]
    )
    assert payload["refresh"] == operation
    assert payload["path"] == hrefs[page]


def test_accepting_a_packages_card_drops_the_getting_paid_forms_it_shares(card_app):
    """The packages card's href is the Getting paid page with an anchor."""
    c, seed, post = card_app
    seed(
        "/app/invoices/settings",
        {
            "/app/payments/methods:0": saved_form(
                "/app/payments/methods", page="/app/invoices/settings", method="bank"
            ),
            "/app/invoices/settings:2": saved_form(
                "/app/invoices/settings", page="/app/invoices/settings", payment_terms="Net 14"
            ),
            "/app/crm/settings:0": saved_form(
                "/app/crm/settings", page="/app/crm/settings", stage_one="Enquiry"
            ),
        },
    )
    operation, response = post("/concierge/setup-cards/packages/accept")
    assert response.status_code == 200
    payload = c.get("/device-sync/flow").json()["payload"]
    assert list(payload["form"]) == ["/app/crm/settings:0"]
    assert payload["refresh"] == operation


def test_a_draft_stored_without_its_page_is_matched_by_its_action(card_app):
    """An older client stored only the action; the form posting to the page's
    own href is still dropped, as before."""
    c, seed, post = card_app
    seed(
        "/app/diary/settings",
        {
            "/app/diary/settings/booking-mode:0": saved_form(
                "/app/diary/settings/booking-mode", booking_mode="stay"
            ),
            "/app/diary/settings:1": saved_form("/app/diary/settings", title="typed"),
        },
    )
    operation, response = post("/concierge/setup-cards/diary_settings/accept")
    assert response.status_code == 200
    payload = c.get("/device-sync/flow").json()["payload"]
    assert list(payload["form"]) == ["/app/diary/settings/booking-mode:0"]
    assert payload["refresh"] == operation


def test_current_note_refresh_updates_guide_without_synthetic_chat_messages(sync_app):
    c=client(sync_app)
    version=re.search(r'name="version"[^>]*value="([^"]+)"',c.get('/onboarding/knowledge').text)[1]
    url='/onboarding/knowledge/review/review-one/guide'
    for _ in range(3):
        state=c.get('/device-sync/flow').json()
        response=c.post(url,json={'form':{'version':version},'question':'Show the current note'},headers={
            'X-CSRF-Token':'test-csrf','X-Blam8-Operation':str(uuid4()),'X-Blam8-Revision':str(state['revision'])})
        assert response.status_code==200
    payload=c.get('/device-sync/flow').json()['payload']
    assert len(payload['messages'])==1
    assert payload['messages'][0]['role']=='assistant' and payload['messages'][0]['knowledge_refresh'] is True
    assert payload['guide']['data']['focus']['kind']=='note'


def test_selected_refresh_card_survives_next_guide_and_another_device(sync_app):
    c=client(sync_app)
    version=re.search(r'name="version"[^>]*value="([^"]+)"',c.get('/onboarding/knowledge').text)[1]
    url='/onboarding/knowledge/review/review-one/guide'
    form={'version':version}
    def ask(question,focus_hash=''):
        state=c.get('/device-sync/flow').json()
        response=c.post(url,json={'form':form,'question':question,'focus_hash':focus_hash},headers={
            'X-CSRF-Token':'test-csrf','X-Blam8-Operation':str(uuid4()),'X-Blam8-Revision':str(state['revision'])})
        assert response.status_code==200,response.text
        answer=response.json();form.update(answer.get('patches',{}));return answer
    first=ask('Show the current note')
    accepted=ask('Keep this note',first['focus_hash'])
    assert accepted['review_decision']['action']=='keep'
    ask('Show the current note')
    payload=client(sync_app).get('/device-sync/flow').json()['payload']
    selected=[m for m in payload['messages'] if (m.get('knowledge_card') or {}).get('selected')]
    assert len(selected)==1
    assert selected[0]['content']==first['reply']
    assert selected[0]['knowledge_card']['state']=='prepared'
    assert selected[0]['knowledge_card']['focus']['id']==first['focus']['id']
    assert len([m for m in payload['messages'] if m.get('knowledge_refresh') and not (m.get('knowledge_card') or {}).get('selected')])==1
