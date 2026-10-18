"""OS preview — the founding-partner look-inside (subscriptions.status='preview').

The invariant this file pins: preview lights the UI ONLY. module_ui_visible /
visible_modules say yes, while module_active (the pipeline/jobs/calendar gate)
keeps saying no — the agent never acts on a previewed OS. Plus the flow: the
preview route seeds + redirects to the OS home, the hub and plans page grow the
affordance, the app shell shows the banner, and a preview row survives buying a
non-OS tier (apply_plan_modules) but upgrades to active on the OS tier.

Run: python -m pytest tests/test_os_preview.py -q
"""
import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
import app.db as db_mod
from app.auth import TenantCtx, require_onboarded, require_tenant
from app.main import app
from app.security import require_csrf


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "get_connection_status", lambda *a, **k: None)
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_tenant] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


# ── the invariant: UI on, agent off ──────────────────────────────────────────
def test_preview_lights_ui_but_never_the_agent(monkeypatch):
    monkeypatch.setattr(db_mod, "active_modules", lambda *a, **k: set())
    monkeypatch.setattr(db_mod, "preview_modules", lambda *a, **k: {"tms"})
    assert db_mod.module_ui_visible("t1", "tms") is True      # dashboard surfaces open
    assert db_mod.visible_modules("t1") == {"tms"}
    assert db_mod.module_active("t1", "tms") is False         # pipeline/jobs stay off
    assert db_mod.active_modules("t1") == set()


# ── starting a preview ────────────────────────────────────────────────────────
def test_preview_route_seeds_and_opens_the_os(client, monkeypatch):
    calls = []
    monkeypatch.setattr(dashboard.db, "get_tenant",
                        lambda *a, **k: {"industry": "logistics", "mailbox": "a@b.co"})
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "set_module_preview",
                        lambda tid, m: calls.append(("preview", tid, m)))
    monkeypatch.setattr(dashboard.db, "seed_module_defaults",
                        lambda tid, m: calls.append(("seed", tid, m)))
    r = client.post("/app/os/preview", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/app/tms"                # straight into the OS
    assert calls == [("preview", "t1", "tms"), ("seed", "t1", "tms")]


def test_preview_route_noop_when_module_already_active(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_tenant",
                        lambda *a, **k: {"industry": "trades", "mailbox": "a@b.co"})
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"fsm"})
    monkeypatch.setattr(dashboard.db, "set_module_preview",
                        lambda *a: pytest.fail("must not downgrade an active module"))
    r = client.post("/app/os/preview", follow_redirects=False)
    assert r.headers["location"] == "/app/fsm"


def test_preview_route_without_a_built_module_bounces_to_hub(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_tenant",
                        lambda *a, **k: {"industry": "recruitment", "mailbox": "a@b.co"})
    r = client.post("/app/os/preview", follow_redirects=False)
    assert r.headers["location"] == "/app/modules"


# ── the surfaces ──────────────────────────────────────────────────────────────
def test_modules_hub_offers_preview_for_the_tenants_own_os(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_tenant",
                        lambda *a, **k: {"industry": "logistics", "mailbox": "a@b.co"})
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
    html = client.get("/app/modules").text
    assert "Preview it first — look inside" in html
    # Only the tenant's OWN vertical gets the affordance — one preview form.
    assert html.count('action="/app/os/preview"') == 1


def test_modules_hub_shows_previewing_state_and_banner(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_tenant",
                        lambda *a, **k: {"industry": "logistics", "mailbox": "a@b.co"})
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "preview_modules", lambda *a, **k: {"tms"})
    html = client.get("/app/modules").text
    assert "Previewing" in html                               # the card state
    assert "You're previewing Field OS · Transport" in html   # the app-shell banner
    assert "Request founding access" in html                  # banner CTA
    assert "Preview it first" not in html                     # affordance consumed


def test_plans_page_no_longer_offers_preview_entry(client, monkeypatch):
    # Two-tier collapse (2026-07-24): the OS card — and with it the preview
    # entry point — is parked off the plans page. The preview MACHINERY
    # (POST /app/os/preview, the hub overlay) stays live and is pinned by the
    # rest of this file; only the public doorway went away.
    import app.billing_routes as billing_routes
    monkeypatch.setattr(billing_routes.billing, "is_configured", lambda: True)
    monkeypatch.setattr(dashboard.db, "get_tenant",
                        lambda *a, **k: {"industry": "trades", "mailbox": "a@b.co",
                                         "plan": None, "os_access_approved": False})
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "tenant_pending_os_request", lambda tid: None)
    html = client.get("/billing/plans").text
    assert "Preview your OS first" not in html
    monkeypatch.setattr(dashboard.db, "preview_modules", lambda *a, **k: {"fsm"})
    html = client.get("/billing/plans").text
    assert "keep looking around" not in html


# ── plan churn ────────────────────────────────────────────────────────────────
def _capture_plan_reconcile(monkeypatch):
    calls = []
    monkeypatch.setattr(db_mod, "get_tenant", lambda tid: {"industry": "logistics"})
    monkeypatch.setattr(db_mod, "set_module_active",
                        lambda tid, m, on: calls.append((m, on)))
    monkeypatch.setattr(db_mod, "seed_module_defaults", lambda tid, m: None)
    return calls


def test_buying_core_leaves_a_preview_alone(monkeypatch):
    calls = _capture_plan_reconcile(monkeypatch)
    monkeypatch.setattr(db_mod, "preview_modules", lambda *a, **k: {"tms"})
    db_mod.apply_plan_modules("t1", "starter")
    assert ("scheduling", True) in calls                      # Core still activates
    assert not any(m == "tms" for m, _ in calls)              # the preview survives


def test_buying_the_os_tier_upgrades_the_preview(monkeypatch):
    calls = _capture_plan_reconcile(monkeypatch)
    monkeypatch.setattr(db_mod, "preview_modules", lambda *a, **k: {"tms"})
    db_mod.apply_plan_modules("t1", "professional")
    assert ("tms", True) in calls                             # preview → active
