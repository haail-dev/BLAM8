"""OS founding-partner access gate — "Core + your OS" is request-only.

The price stays public but the OS-tier checkout is locked until the operator
approves the tenant (tenants.os_access_approved). This pins the LOCK itself
(server-side in /billing/checkout — hiding the button is not the gate), the
request flow (queued once, duplicates collapse), the plans-page states, and
the operator approve path flipping the tenant flag.

Run: python -m pytest tests/test_os_access_gate.py -q
"""
import pytest
from fastapi.testclient import TestClient

import app.billing_routes as billing_routes
import app.dashboard as dashboard
from app.auth import TenantCtx, require_tenant
from app.config import settings
from app.main import app
from app.security import require_csrf

OS_PRICE = "price_os_248"
PRO_PRICE = "price_pro_298"
CORE_PRICE = "price_core_99"      # plan key 'starter' — sold as Pro since 2026-07-24
STARTER_PRICE = "price_starter_39"  # plan key 'email' — sold as Starter


@pytest.fixture
def client():
    app.dependency_overrides[require_tenant] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)   # no `with`: startup events must not run
    app.dependency_overrides.clear()


class Env:
    """Fake tenant + request store around billing_routes' db/billing calls."""
    def __init__(self, monkeypatch, approved=False, plan=None):
        self.tenant = {"id": "t1", "name": "Koth Transport", "industry": "logistics",
                       "mailbox": "ops@koth.example", "plan": plan,
                       "os_access_approved": approved}
        self.requests = []
        monkeypatch.setattr(settings, "stripe_os_price_id", OS_PRICE)
        monkeypatch.setattr(settings, "stripe_professional_price_id", PRO_PRICE)
        monkeypatch.setattr(settings, "stripe_starter_price_id", CORE_PRICE)
        monkeypatch.setattr(settings, "stripe_email_price_id", STARTER_PRICE)
        monkeypatch.setattr(settings, "platform_notify_mailbox", "")   # no notify emails
        monkeypatch.setattr(billing_routes.billing, "is_configured", lambda: True)
        monkeypatch.setattr(billing_routes.billing, "create_checkout_session",
                            lambda **k: "https://stripe.example/checkout")
        monkeypatch.setattr(billing_routes.db, "get_tenant",
                            lambda tid, client=None: dict(self.tenant))
        # The app-shell context (dashboard._ctx) behind the plans page.
        monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
        monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
        monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
        monkeypatch.setattr(billing_routes.db, "tenant_pending_os_request",
                            lambda tid: next((r for r in self.requests
                                              if not r.get("approved_at") and not r.get("dismissed_at")), None))
        monkeypatch.setattr(billing_routes.db, "create_os_access_request",
                            lambda tid, email, msg: self.requests.append(
                                {"id": f"r{len(self.requests) + 1}", "tenant_id": tid,
                                 "email": email, "message": msg}) or self.requests[-1])


# ── the lock ──────────────────────────────────────────────────────────────────
def test_os_checkout_blocked_until_approved(client, monkeypatch):
    Env(monkeypatch, approved=False)
    for price in (OS_PRICE, PRO_PRICE):
        r = client.post("/billing/checkout", data={"price_id": price}, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/billing/plans?os_gated=1"


def test_os_checkout_allowed_once_approved(client, monkeypatch):
    Env(monkeypatch, approved=True)
    r = client.post("/billing/checkout", data={"price_id": OS_PRICE}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "https://stripe.example/checkout"


def test_existing_os_subscriber_is_not_locked_out(client, monkeypatch):
    # A legacy/operator-set professional tenant must keep its checkout path
    # (portal re-checkout, plan changes) even with the flag unset.
    Env(monkeypatch, approved=False, plan="professional")
    r = client.post("/billing/checkout", data={"price_id": PRO_PRICE}, follow_redirects=False)
    assert r.headers["location"] == "https://stripe.example/checkout"


def test_core_tier_stays_self_serve(client, monkeypatch):
    Env(monkeypatch, approved=False)
    r = client.post("/billing/checkout", data={"price_id": CORE_PRICE}, follow_redirects=False)
    assert r.headers["location"] == "https://stripe.example/checkout"


# ── the request flow ─────────────────────────────────────────────────────────
def test_request_access_queues_once(client, monkeypatch):
    env = Env(monkeypatch, approved=False)
    r = client.post("/billing/request-os-access", data={"message": "3 vans, 2 depots"},
                    follow_redirects=False)
    assert r.headers["location"] == "/billing/plans?os_requested=1"
    assert len(env.requests) == 1
    assert env.requests[0]["message"] == "3 vans, 2 depots"
    assert env.requests[0]["email"] == "ops@koth.example"
    # A second submit while one is pending must not create a duplicate row.
    client.post("/billing/request-os-access", data={"message": "again"},
                follow_redirects=False)
    assert len(env.requests) == 1


def test_request_access_noop_when_already_approved(client, monkeypatch):
    env = Env(monkeypatch, approved=True)
    r = client.post("/billing/request-os-access", data={"message": "hi"},
                    follow_redirects=False)
    assert r.headers["location"] == "/billing/plans"
    assert env.requests == []


# ── the plans page states ─────────────────────────────────────────────────────
# Since the 2026-07-24 two-tier collapse the OS tier is PARKED off the page
# entirely (no card, no price, no request button) — the page sells Starter and
# Pro only. The checkout LOCK above still guards the parked price ids for any
# path that can still reach them (a hand-crafted POST, a stale tab).

def test_plans_page_sells_only_the_three_tiers(client, monkeypatch):
    Env(monkeypatch, approved=False)
    html = client.get("/billing/plans").text
    assert "Starter" in html and "Pro" in html
    assert f'value="{STARTER_PRICE}"' in html      # both loop tiers buyable
    assert f'value="{CORE_PRICE}"' in html
    assert f'value="{PRO_PRICE}"' not in html      # parked tiers: no buy button…
    assert f'value="{OS_PRICE}"' not in html
    assert "£248" not in html and "£298" not in html   # …and no price anywhere
    assert "Request founding access" not in html   # the OS card is gone, not gated
    assert "Talk to us" in html                    # bigger-than-Pro routes to hello@


def test_plans_page_parked_even_when_os_approved(client, monkeypatch):
    # Approval still unlocks the CHECKOUT (test above) for legacy flows, but
    # the page itself no longer renders the parked tier for anyone.
    Env(monkeypatch, approved=True)
    html = client.get("/billing/plans").text
    assert "Request founding access" not in html
    assert f'value="{PRO_PRICE}"' not in html
    assert f'value="{OS_PRICE}"' not in html


# ── operator console queue ───────────────────────────────────────────────────
def test_admin_queue_renders_pending_and_handled(monkeypatch):
    import app.admin as admin
    from app.admin_auth import AdminCtx, require_admin
    app.dependency_overrides[require_admin] = lambda: AdminCtx(
        user_id="u1", email="ops@blam8.com", role="owner")
    try:
        rows = [
            {"id": "r1", "tenant_id": "t1", "email": "a@x.com", "message": "3 vans",
             "created_at": "2026-07-11T10:00:00Z", "approved_at": None, "dismissed_at": None},
            {"id": "r2", "tenant_id": "t2", "email": None, "message": None,
             "created_at": "2026-07-10T10:00:00Z",
             "approved_at": "2026-07-10T12:00:00Z", "dismissed_at": None},
        ]
        monkeypatch.setattr(admin.db, "list_os_access_requests", lambda: rows)
        monkeypatch.setattr(admin.db, "get_tenant",
                            lambda tid: {"name": f"Biz {tid}", "industry": "logistics",
                                         "mailbox": f"{tid}@x.com"})
        monkeypatch.setattr(admin.db, "count_unseen_feature_requests", lambda: 0)
        monkeypatch.setattr(admin.db, "count_pending_os_requests", lambda: 1)
        html = TestClient(app).get("/admin/os-access").text
        assert "Biz t1" in html and "3 vans" in html          # pending row
        assert "Approved 2026-07-10" in html                  # handled row
        assert "Send the good news" in html                   # one-click mailto
    finally:
        app.dependency_overrides.clear()


# ── operator approve flips the tenant flag ───────────────────────────────────
def test_approve_request_unlocks_tenant(monkeypatch):
    import app.db as db_mod
    calls = {}
    req = {"id": "r1", "tenant_id": "t1", "dismissed_at": None}
    monkeypatch.setattr(db_mod, "get_os_access_request", lambda rid: dict(req))
    monkeypatch.setattr(db_mod, "set_os_access_approved",
                        lambda tid, ok: calls.update(tenant=tid, ok=ok))

    class _Q:
        def update(self, *a, **k): return self
        def eq(self, *a, **k): return self
        def is_(self, *a, **k): return self
        def execute(self): return type("R", (), {"data": []})()
    monkeypatch.setattr(db_mod, "db", lambda: type("C", (), {"table": lambda self, n: _Q()})())

    out = db_mod.approve_os_access_request("r1")
    assert out["tenant_id"] == "t1"
    assert calls == {"tenant": "t1", "ok": True}


def test_approve_skips_dismissed_request(monkeypatch):
    import app.db as db_mod
    monkeypatch.setattr(db_mod, "get_os_access_request",
                        lambda rid: {"id": "r1", "tenant_id": "t1", "dismissed_at": "2026-07-11"})
    monkeypatch.setattr(db_mod, "set_os_access_approved",
                        lambda tid, ok: pytest.fail("must not unlock a dismissed request"))
    assert db_mod.approve_os_access_request("r1") is None
