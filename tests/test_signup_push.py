"""Operator sign-up push (Kris, 2026-08-03) — "someone just registered".

Pins: db.ensure_tenant_for_user's CREATE branch fires push.notify_signup (and
the existing-tenant branch never does), the push routes to the tenant that owns
PLATFORM_NOTIFY_MAILBOX with kind operator_signup, it stays silent when the
mailbox/VAPID plumbing is unconfigured, and operator_signup is feed-excluded —
platform plumbing must never land in the BLAM8 tenant's own in-app feed.

Run: python -m pytest tests/test_signup_push.py -q
"""
from types import SimpleNamespace

from app import db, push
from app.config import settings


# ── ensure_tenant_for_user wiring ────────────────────────────────────────────
class _FakeQuery:
    def __init__(self, name):
        self.name = name

    def insert(self, payload):
        self.payload = payload
        return self

    def execute(self):
        return SimpleNamespace(data=[{"id": "t-new", **self.payload}])


class _FakeClient:
    def table(self, name):
        return _FakeQuery(name)


def _wire_create_path(monkeypatch, fired):
    monkeypatch.setattr(db, "db", lambda: _FakeClient())
    monkeypatch.setattr(db, "get_tenant_for_user", lambda uid: None)
    monkeypatch.setattr(db, "apply_plan_modules", lambda *a, **k: None)
    monkeypatch.setattr(db, "upsert_tenant_config", lambda *a, **k: None)
    monkeypatch.setattr(push, "notify_signup", lambda *a: fired.append(a))


def test_create_branch_fires_the_signup_push(monkeypatch):
    fired = []
    _wire_create_path(monkeypatch, fired)
    tid = db.ensure_tenant_for_user("u-1", "Deane & Sons", email="dave@deane.co.uk")
    assert tid == "t-new"
    assert fired == [("t-new", "Deane & Sons", "dave@deane.co.uk")]


def test_existing_tenant_never_fires(monkeypatch):
    fired = []
    _wire_create_path(monkeypatch, fired)
    monkeypatch.setattr(db, "get_tenant_for_user", lambda uid: "t-old")
    assert db.ensure_tenant_for_user("u-1", "Deane & Sons", email="x@y.co") == "t-old"
    assert fired == []


def test_push_failure_never_blocks_the_signup(monkeypatch):
    fired = []
    _wire_create_path(monkeypatch, fired)
    monkeypatch.setattr(push, "notify_signup",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("push down")))
    assert db.ensure_tenant_for_user("u-1", "Biz", email="x@y.co") == "t-new"


# ── notify_signup → the operator tenant ──────────────────────────────────────
def test_notify_signup_routes_to_the_operator_tenant(monkeypatch):
    sent = []
    monkeypatch.setattr(settings, "vapid_public_key", "pk")
    monkeypatch.setattr(settings, "vapid_private_key", "sk")
    monkeypatch.setattr(settings, "platform_notify_mailbox", "admin.blam8@gmail.com")
    monkeypatch.setattr(push.db, "get_connection_by_address",
                        lambda addr: {"tenant_id": "op-1", "id": "c-1"})
    monkeypatch.setattr(push, "notify", lambda tid, **kw: sent.append((tid, kw)))
    push.notify_signup("t-9", "Joe's Plumbing", "joe@plumb.co.uk")
    (tid, kw), = sent
    assert tid == "op-1"
    assert kw["kind"] == "operator_signup" and kw["tag"] == "signup-t-9"
    assert "Joe's Plumbing" in kw["body"] and "joe@plumb.co.uk" in kw["body"]
    assert kw["url"] == "/admin/tenants/t-9"


def test_notify_signup_silent_without_the_platform_mailbox(monkeypatch):
    sent = []
    monkeypatch.setattr(settings, "vapid_public_key", "pk")
    monkeypatch.setattr(settings, "vapid_private_key", "sk")
    monkeypatch.setattr(settings, "platform_notify_mailbox", "")
    monkeypatch.setattr(push, "notify", lambda tid, **kw: sent.append(tid))
    push.notify_signup("t-9", "Biz", "x@y.co")
    assert sent == []


def test_operator_signup_is_feed_excluded(monkeypatch):
    """The BLAM8 tenant's own concierge feed must never show platform plumbing."""
    assert "operator_signup" in push._FEED_EXCLUDED_KINDS
    rows = []
    monkeypatch.setattr(push.db, "add_concierge_notification",
                        lambda *a, **k: rows.append(a))
    push._record_notification("op-1", title="t", body="b", url="/",
                              kind="operator_signup")
    assert rows == []
