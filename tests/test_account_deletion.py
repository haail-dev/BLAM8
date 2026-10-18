"""Account closure route — POST /app/settings/close-account.

Pins the destructive contract:
  * deletes nothing unless the body is exactly confirm="DELETE"
  * captures the tenant's members BEFORE the cascade delete, then deletes the
    tenant (one delete → ON DELETE CASCADE wipes all child tables)
  * removes the Supabase login only for users with NO other tenant
  * best-effort revokes each mailbox's Graph webhook first
  * signs the session out and lands on the public home

Route-level via TestClient with auth + CSRF deps overridden and db/oauth
monkeypatched — in-process only, no network. TestClient without a context
manager on purpose (startup events must not run).

Run: python -m pytest tests/test_account_deletion.py -q
"""
import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf


class Env:
    """Records the deletion calls so tests can assert what happened + the order."""

    def __init__(self, monkeypatch, members=("u1",), other_tenants=(), mailboxes=()):
        self.calls = []          # ordered log of (name, arg)
        self.deleted_users = []
        self.graph_torn_down = []
        _other = set(other_tenants)
        _conns = {m: {"id": m, "subscription_id": f"sub-{m}"} for m in mailboxes}

        def list_mailboxes(tenant_id, **k):
            return [{"id": m} for m in mailboxes]

        def get_connection_by_id(cid, tenant_id=None):
            return _conns.get(cid)

        async def del_graph_sub(conn, tenant_id):
            self.graph_torn_down.append(conn["id"])

        def list_members(tenant_id):
            self.calls.append(("list_members", tenant_id))
            return list(members)

        def delete_tenant(tenant_id):
            self.calls.append(("delete_tenant", tenant_id))

        def has_other(user_id):
            return user_id in _other

        def delete_auth_user(user_id):
            self.calls.append(("delete_auth_user", user_id))
            self.deleted_users.append(user_id)

        monkeypatch.setattr(dashboard.db, "list_mailboxes", list_mailboxes)
        monkeypatch.setattr(dashboard.db, "get_connection_by_id", get_connection_by_id)
        monkeypatch.setattr(dashboard.oauth, "_delete_graph_subscription", del_graph_sub)
        monkeypatch.setattr(dashboard.db, "list_tenant_member_user_ids", list_members)
        monkeypatch.setattr(dashboard.db, "delete_tenant", delete_tenant)
        monkeypatch.setattr(dashboard.db, "user_has_other_tenants", has_other)
        monkeypatch.setattr(dashboard.db, "delete_auth_user", delete_auth_user)


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)   # no `with`: startup events must not run
    app.dependency_overrides.clear()


def close(client, **form):
    return client.post("/app/settings/close-account", data=form, follow_redirects=False)


def test_wrong_confirmation_deletes_nothing(client, monkeypatch):
    env = Env(monkeypatch)
    r = close(client)                       # no confirm field at all
    assert r.status_code == 303 and "err=confirm" in r.headers["location"]
    assert ("delete_tenant", "t1") not in env.calls and env.deleted_users == []

    r = close(client, confirm="delete")     # wrong case must not pass
    assert "err=confirm" in r.headers["location"]
    assert ("delete_tenant", "t1") not in env.calls


def test_confirm_deletes_tenant_then_lone_user(client, monkeypatch):
    env = Env(monkeypatch, members=("u1",), other_tenants=())
    r = close(client, confirm="DELETE")
    assert r.status_code == 303 and "account=closed" in r.headers["location"]
    names = [c[0] for c in env.calls]
    assert names.index("list_members") < names.index("delete_tenant")  # captured before cascade
    assert ("delete_tenant", "t1") in env.calls
    assert env.deleted_users == ["u1"]                                 # lone user's login removed
    assert r.headers.get("set-cookie")                                 # session cleared


def test_user_shared_across_tenants_is_kept(client, monkeypatch):
    env = Env(monkeypatch, members=("u1", "u2"), other_tenants=("u2",))
    r = close(client, confirm="DELETE")
    assert "account=closed" in r.headers["location"]
    assert ("delete_tenant", "t1") in env.calls
    assert env.deleted_users == ["u1"]      # u2 still belongs to another tenant → kept


def test_graph_subscription_revoked_before_delete(client, monkeypatch):
    env = Env(monkeypatch, members=("u1",), mailboxes=("m1",))
    r = close(client, confirm="DELETE")
    assert "account=closed" in r.headers["location"]
    assert env.graph_torn_down == ["m1"]    # provider webhook revoked
    assert ("delete_tenant", "t1") in env.calls
