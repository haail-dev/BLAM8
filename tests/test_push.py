"""PWA layer — manifest/service-worker/offline routes, the push subscription
API, Web Push delivery (src/app/push.py), and the pipeline's "Draft ready" gating
(pending drafts notify; auto-sent replies and duplicate webhooks don't).

Run: python -m pytest tests/test_push.py -q
"""
import asyncio
import json
import threading
import types

import pytest
from fastapi.testclient import TestClient

from app import agent, db, events, pipeline
from app import push as push_mod
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf_header

# The conftest _no_push_sends fixture stubs notify_draft_ready for every test;
# capture the real function at collection time (fixtures haven't run yet) so
# the unit tests below can exercise it.
REAL_NOTIFY = push_mod.notify_draft_ready


# ── PWA surface routes ───────────────────────────────────────────────────────
def test_manifest_served_from_root_and_valid():
    res = TestClient(app).get("/manifest.json")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/manifest+json")
    m = json.loads(res.text)
    assert m["name"] == "BLAM8" and m["display"] == "standalone"
    assert m["start_url"] == "/app/" and m["scope"] == "/"
    sizes = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert any(i.get("purpose") == "maskable" for i in m["icons"])
    assert [s["name"] for s in m["shortcuts"]] == ["Inbox", "CRM", "Diary", "Talk to Concierge", "Concierge"]


def test_manifest_icons_exist_and_are_served():
    client = TestClient(app)
    m = json.loads(client.get("/manifest.json").text)
    for src in [i["src"] for i in m["icons"]] + [i["icons"][0]["src"] for i in m["shortcuts"]]:
        assert client.get(src).status_code == 200, src


def test_service_worker_served_from_root_scope():
    res = TestClient(app).get("/sw.js")
    assert res.status_code == 200
    assert "javascript" in res.headers["content-type"]
    # Stale-worker guard: browsers must revalidate the SW on every visit.
    assert res.headers["cache-control"] == "no-cache"
    assert "push" in res.text and "notificationclick" in res.text


def test_public_pages_are_installable():
    # Chrome's install criteria must hold on the page the visitor is ON — a
    # logged-out phone at blam8.com needs the manifest link + the SW registrar
    # (push.js) right there, not behind the /app login.
    res = TestClient(app).get("/")
    assert res.status_code == 200
    assert 'rel="manifest" href="/manifest.json"' in res.text
    assert "/static/push.js" in res.text


def test_offline_page_renders_self_contained():
    res = TestClient(app).get("/offline")
    assert res.status_code == 200
    assert "You're offline" in res.text
    # Precached page must not need the network: no CDN/font hosts.
    assert "cdn.tailwindcss.com" not in res.text
    assert "fonts.googleapis.com" not in res.text


# ── subscription API ─────────────────────────────────────────────────────────
@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(
        tenant_id="t1", client=None, user_id="u1")
    app.dependency_overrides[require_csrf_header] = lambda: None
    yield TestClient(app)   # no `with`: startup events must not run
    app.dependency_overrides.clear()


SUB = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc123",
       "keys": {"p256dh": "pkey", "auth": "akey"}}


def test_subscribe_stores_the_device(client, monkeypatch):
    stored = []
    monkeypatch.setattr(db, "upsert_push_subscription",
                        lambda *a: stored.append(a))
    res = client.post("/api/push/subscribe", json=SUB)
    assert res.status_code == 200 and res.json() == {"ok": True}
    assert stored == [("t1", "u1", SUB["endpoint"], "pkey", "akey")]


def test_subscribe_rejects_non_https_endpoints(client, monkeypatch):
    monkeypatch.setattr(db, "upsert_push_subscription",
                        lambda *a: pytest.fail("must not store junk"))
    bad = dict(SUB, endpoint="http://evil.example/collect")
    assert client.post("/api/push/subscribe", json=bad).status_code == 422


def test_subscribe_tolerates_browser_extras(client, monkeypatch):
    # PushSubscription.toJSON() carries expirationTime — must not 422.
    monkeypatch.setattr(db, "upsert_push_subscription", lambda *a: None)
    res = client.post("/api/push/subscribe", json=dict(SUB, expirationTime=None))
    assert res.status_code == 200


def test_unsubscribe_scopes_to_the_user(client, monkeypatch):
    deleted = []
    monkeypatch.setattr(db, "delete_push_subscription",
                        lambda *a: deleted.append(a))
    res = client.post("/api/push/unsubscribe", json={"endpoint": SUB["endpoint"]})
    assert res.status_code == 200
    assert deleted == [("u1", SUB["endpoint"])]


# ── delivery (src/app/push.py) ───────────────────────────────────────────────────
def _sub_row(sub_id="s1"):
    return {"id": sub_id, "endpoint": "https://push.example/e1",
            "keys_p256dh": "p", "keys_auth": "a"}


def test_notify_noops_without_vapid_keys(monkeypatch):
    monkeypatch.setattr(push_mod.settings, "vapid_private_key", "")
    monkeypatch.setattr(push_mod, "_send_to_tenant",
                        lambda *a: pytest.fail("push disabled — must not send"))
    REAL_NOTIFY("t1", contact_name="Sarah", contact_address="s@x.com",
                subject="Boiler", log_id="L1")


def test_notify_builds_the_draft_ready_payload(monkeypatch):
    monkeypatch.setattr(push_mod.settings, "vapid_private_key", "priv")
    monkeypatch.setattr(push_mod.settings, "vapid_public_key", "pub")
    got, done = [], threading.Event()

    def recorder(tenant_id, payload, kind="generic"):
        got.append((tenant_id, payload, kind))
        done.set()

    monkeypatch.setattr(push_mod, "_send_to_tenant", recorder)
    REAL_NOTIFY("t1", contact_name="Sarah Jones", contact_address="sarah@x.com",
                subject="Boiler service enquiry", log_id="L1")
    assert done.wait(2), "push thread never ran"
    tenant_id, payload, kind = got[0]
    assert tenant_id == "t1"
    assert kind == "draft_ready"   # the agent-trace notification type
    assert payload["title"] == "Draft ready — Sarah Jones"
    assert payload["body"] == "Boiler service enquiry — tap to review and approve"
    assert payload["url"] == "/app/conversations/view?contact=sarah%40x.com"
    assert payload["tag"] == "draft-L1"


def test_notify_falls_back_to_address_and_default_subject(monkeypatch):
    monkeypatch.setattr(push_mod.settings, "vapid_private_key", "priv")
    monkeypatch.setattr(push_mod.settings, "vapid_public_key", "pub")
    got, done = [], threading.Event()
    monkeypatch.setattr(push_mod, "_send_to_tenant",
                        lambda t, p, k="generic": (got.append(p), done.set()))
    REAL_NOTIFY("t1", contact_name=None, contact_address="s@x.com",
                subject="  ", log_id="L2")
    assert done.wait(2)
    assert got[0]["title"] == "Draft ready — s@x.com"
    assert got[0]["body"].startswith("New message — ")


def test_send_fans_out_and_stamps_last_used(monkeypatch):
    import pywebpush
    sent, touched = [], []
    monkeypatch.setattr(pywebpush, "webpush", lambda **kw: sent.append(kw))
    monkeypatch.setattr(db, "list_push_subscriptions",
                        lambda t: [_sub_row("s1"), _sub_row("s2")])
    monkeypatch.setattr(db, "touch_push_subscription", touched.append)
    monkeypatch.setattr(db, "remove_push_subscription_by_id",
                        lambda i: pytest.fail("healthy sub removed"))
    push_mod._send_to_tenant("t1", {"title": "x"})
    assert len(sent) == 2 and touched == ["s1", "s2"]
    assert json.loads(sent[0]["data"]) == {"title": "x"}
    # Fresh claims dict per call — pywebpush stamps per-push-service aud into it.
    assert sent[0]["vapid_claims"] is not sent[1]["vapid_claims"]


def test_send_removes_gone_subscriptions(monkeypatch):
    import pywebpush
    removed = []

    def gone(**kw):
        raise pywebpush.WebPushException(
            "gone", response=types.SimpleNamespace(status_code=410))

    monkeypatch.setattr(pywebpush, "webpush", gone)
    monkeypatch.setattr(db, "list_push_subscriptions", lambda t: [_sub_row("dead")])
    monkeypatch.setattr(db, "touch_push_subscription",
                        lambda i: pytest.fail("dead sub touched"))
    monkeypatch.setattr(db, "remove_push_subscription_by_id", removed.append)
    push_mod._send_to_tenant("t1", {"title": "x"})
    assert removed == ["dead"]


def test_send_survives_one_bad_subscription(monkeypatch):
    import pywebpush
    sent = []

    def flaky(**kw):
        if not sent:
            sent.append("fail")
            raise pywebpush.WebPushException(
                "server error", response=types.SimpleNamespace(status_code=500))
        sent.append("ok")

    monkeypatch.setattr(pywebpush, "webpush", flaky)
    monkeypatch.setattr(db, "list_push_subscriptions",
                        lambda t: [_sub_row("s1"), _sub_row("s2")])
    monkeypatch.setattr(db, "touch_push_subscription", lambda i: None)
    monkeypatch.setattr(db, "remove_push_subscription_by_id",
                        lambda i: pytest.fail("500 is not gone — keep the sub"))
    push_mod._send_to_tenant("t1", {"title": "x"})
    assert sent == ["fail", "ok"]        # second device still got its push


# ── pipeline gating ──────────────────────────────────────────────────────────
class Env:
    """The no-modules pipeline harness (test_pipeline_events.Env) + a push
    recorder: patches draft_for_email's collaborators so the gating decision —
    push iff the draft lands pending — runs for real."""

    def __init__(self, monkeypatch, *, auto_send=False, created=True):
        self.pushes: list = []
        self.sent = []

        async def fake_draft(cfg, email, **kw):
            return agent.DraftResult(text="Reply.", usage={"input_tokens": 1}, model="m")

        async def fake_send(*a, **k):
            self.sent.append(a)

        async def fake_retrieve(*a, **k):
            return []

        monkeypatch.setattr(pipeline.agent, "draft_reply", fake_draft)
        monkeypatch.setattr(pipeline.kb, "retrieve", fake_retrieve)
        monkeypatch.setattr(pipeline.kb, "render_for_prompt", lambda *a: None)
        monkeypatch.setattr(pipeline.usage, "record_anthropic", lambda *a, **k: None)
        monkeypatch.setattr(pipeline.mailbox, "send_reply", fake_send)
        monkeypatch.setattr(pipeline.db, "is_ignored", lambda *a, **k: False)
        monkeypatch.setattr(pipeline.db, "get_tenant", lambda *a, **k: {"mailbox": "me@biz.co"})
        monkeypatch.setattr(pipeline.db, "get_tenant_config", lambda *a, **k: {"auto_send": auto_send})
        monkeypatch.setattr(pipeline.db, "get_mailbox_config", lambda *a, **k: None)
        monkeypatch.setattr(pipeline.db, "own_mailbox_addresses", lambda *a, **k: {"me@biz.co"})
        monkeypatch.setattr(pipeline.db, "mailbox_self_addresses", lambda *a, **k: set())
        monkeypatch.setattr(pipeline.db, "bookings_for_contact", lambda *a, **k: [])
        monkeypatch.setattr(pipeline.db, "crm_contact_by_email", lambda *a, **k: None)
        monkeypatch.setattr(pipeline.crm, "ingest_email_log_row",
                            lambda *a, **k: {"activities": 0, "contact_id": None, "company_id": None})
        monkeypatch.setattr(pipeline.db, "get_conversation", lambda *a, **k: [])
        monkeypatch.setattr(pipeline.db, "get_scheduling_config", lambda *a, **k: None)
        monkeypatch.setattr(pipeline.db, "active_modules", lambda *a, **k: set())
        monkeypatch.setattr(pipeline.db, "insert_email_log", lambda *a, **k: ("log-1", created))
        monkeypatch.setattr(pipeline.db, "mark_email_log_sent", lambda *a, **k: None)
        # The gating recorder — replaces the conftest no-op stub.
        monkeypatch.setattr(pipeline.push, "notify_draft_ready",
                            lambda tenant_id, **kw: self.pushes.append((tenant_id, kw)))


EMAIL = {"from_address": "ops@acme.co.uk", "from_name": "Ops", "subject": "Quote",
         "body": "Friday?", "thread_id": "th-9"}


def test_pending_draft_pushes_draft_ready(monkeypatch):
    env = Env(monkeypatch)
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1"))
    assert len(env.pushes) == 1
    tenant_id, kw = env.pushes[0]
    assert tenant_id == "t1"
    assert kw == {"contact_name": "Ops", "contact_address": "ops@acme.co.uk",
                  "subject": "Quote", "log_id": "log-1", "reason": ""}


def test_auto_sent_reply_does_not_push(monkeypatch):
    env = Env(monkeypatch, auto_send=True)
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1",
                                         auto_send_ok=True))
    assert env.sent                      # the reply went out…
    assert env.pushes == []              # …so there is nothing to review


def test_failed_auto_send_pushes_the_pending_draft(monkeypatch):
    env = Env(monkeypatch, auto_send=True)

    async def broken_send(*a, **k):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(pipeline.mailbox, "send_reply", broken_send)
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1",
                                         auto_send_ok=True))
    assert len(env.pushes) == 1          # row stayed pending → owner must know


def test_duplicate_webhook_delivery_does_not_push(monkeypatch):
    env = Env(monkeypatch, created=False)
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1"))
    assert env.pushes == []


def test_a_tapped_notification_lands_on_its_page_even_when_navigate_fails():
    # Kris 2026-09-24: the draft notification brought BLAM8 forward still on
    # the voice screen — navigate() failed quietly on iOS.
    sw = open("src/app/static/sw.js", encoding="utf-8").read()
    assert "w.navigate(url).catch(() => null)" in sw
    assert "w.postMessage({ type: 'blam8:open', url })" in sw
    page = open("src/app/static/push.js", encoding="utf-8").read()
    assert "d.type !== 'blam8:open'" in page and "location.assign(d.url)" in page
    assert "d.url.charAt(0) !== '/' || d.url.charAt(1) === '/'" in page   # our own paths only


def test_a_tap_is_remembered_and_every_page_goes_there_when_it_comes_forward():
    # Kris 2026-09-24, twice: on iPhone the app reopened on the last thread,
    # not the notification's — navigate() and the message both missed.
    sw = open("src/app/static/sw.js", encoding="utf-8").read()
    assert "const OPEN_CACHE = 'blam8-open';" in sw
    assert "k !== CACHE && k !== OPEN_CACHE" in sw                 # a version bump never wipes a tap
    # The window opens FIRST: iOS only honours openWindow() while it still counts
    # as the tap — saving before it gave a blank in-app browser (tap-9).
    assert sw.index("clients.matchAll({ type: 'window'") < sw.index("caches.open(OPEN_CACHE).then((c) => c.put(OPEN_KEY")
    assert "Promise.all([open, remember])" in sw
    page = open("src/app/static/push.js", encoding="utf-8").read()
    assert "function goWhereTapped()" in page
    assert "Date.now() - (d.at || 0) < 60000" in page               # a minute, then stale
    assert "c.delete('/__blam8_open')" in page                      # read once
    assert "visibilitychange" in page and "pageshow" in page



def test_settings_says_plainly_what_an_iphone_tap_does():
    # Ten live taps (2026-09-24): an already-open iPhone home-screen app is only
    # brought forward; iOS never says which notification. Said, not hidden.
    t = open("src/app/templates/app/settings.html", encoding="utf-8").read()
    assert "data-iphone-tap-note" in t and "opens BLAM8 where you left it" in t
    sw = open("src/app/static/sw.js", encoding="utf-8").read()
    assert "sendNote" not in sw and "BUILD" not in sw          # the test instrumentation is gone


def test_full_auto_send_logs_the_missing_sign_off_and_still_sends(monkeypatch):
    """The announcement never blocks: in full-auto with no sign-off set the reply
    goes out, and the agent log's drafted event says why the ending is the agent's."""
    env = Env(monkeypatch, auto_send=True)
    events = []
    monkeypatch.setattr(pipeline.db, "log_agent_event",
                        lambda tenant_id, kind, summary, **kw: events.append((kind, kw)))
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1",
                                         auto_send_ok=True))
    assert env.sent                                  # the reply went out
    drafted = [kw for kind, kw in events if kind == "drafted"]
    assert len(drafted) == 1
    assert drafted[0]["detail"]["sign_off_missing"] is True


def test_a_draft_with_the_sign_off_applied_logs_no_badge(monkeypatch):
    env = Env(monkeypatch)

    async def signed_draft(cfg, email, **kw):
        return agent.DraftResult(text="Reply.\n\nThanks", usage={"input_tokens": 1}, model="m",
                                 sign_off="Thanks")

    monkeypatch.setattr(pipeline.agent, "draft_reply", signed_draft)
    events = []
    monkeypatch.setattr(pipeline.db, "log_agent_event",
                        lambda tenant_id, kind, summary, **kw: events.append((kind, kw)))
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1"))
    assert len(env.pushes) == 1                      # still a pending draft to review
    drafted = [kw for kind, kw in events if kind == "drafted"]
    assert "sign_off_missing" not in drafted[0]["detail"]
