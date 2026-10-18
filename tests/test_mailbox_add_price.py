"""The price is on screen before the consent screen (Kris 2026-09-01).

"+ Add Outlook" used to link straight to Microsoft. You came back, oauth.py
called billing.sync_mailbox_addon, and a prorated Stripe item appeared — so a
customer's first sight of the £39 was their next invoice. Every route to an
ADDITIONAL mailbox now goes through /app/mailboxes/add, which states the number.

Two things are pinned here, and both are about money:

  * the quote's arithmetic mirrors sync_mailbox_addon EXACTLY — same allowance,
    same grandfathering, same depot growth — so what we promise and what we
    charge can't drift apart;
  * the quote refuses to state a whole-bill total it can't compute exactly. A
    "Core + your OS" tenant on Transport pays £298, not the £248 the catalog
    row heads with, so that tier (and Enterprise, and any tenant paying for
    depots) gets the add-on price alone rather than a confident wrong total.

And one that is about not doing damage: /oauth/connect?mode=add silently
degrades to mode=replace for a tenant who can't add (oauth.py), so a trial user
handed that link would swap their only mailbox instead of gaining one. No
surface a non-subscriber can see is allowed to point at it.

Run: python -m pytest tests/test_mailbox_add_price.py -q
"""
import pytest
from fastapi.testclient import TestClient

import app.billing as billing
import app.dashboard as dashboard
from app.auth import TenantCtx, require_onboarded
from app.main import app


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    yield TestClient(app)
    app.dependency_overrides.clear()


def _boxes(n, grandfathered=False):
    return [{"id": str(i), "provider": "microsoft", "grandfathered": grandfathered}
            for i in range(n)]


def _quote(monkeypatch, plan="starter", status="active", mailboxes=1,
           grandfathered=False, depots=0):
    monkeypatch.setattr(billing.db, "get_tenant", lambda *a, **k: {
        "plan": plan, "subscription_status": status, "stripe_subscription_id": "sub_1"})
    monkeypatch.setattr(billing.db, "list_mailboxes",
                        lambda *a, **k: _boxes(mailboxes, grandfathered))
    monkeypatch.setattr(billing, "billable_depots", lambda *a, **k: depots)
    return billing.mailbox_addon_quote("t1")


# ── The arithmetic, against every plan shape we sell ────────────────────────
def test_the_second_mailbox_on_a_flat_tier_quotes_an_exact_total(monkeypatch):
    q = _quote(monkeypatch, plan="starter", mailboxes=1)
    assert q["charged"] is True
    assert (q["ordinal"], q["extra_gbp"], q["emails_added"]) == (2, 39, 300)
    assert q["total_after"] == 138          # £99 Pro + one £39 mailbox


def test_starter_quotes_its_own_total_not_pros(monkeypatch):
    assert _quote(monkeypatch, plan="email", mailboxes=1)["total_after"] == 78


def test_the_add_on_multiplies_with_each_further_mailbox(monkeypatch):
    q = _quote(monkeypatch, plan="starter", mailboxes=2)
    assert (q["ordinal"], q["mailbox_gbp_after"], q["total_after"]) == (3, 78, 177)


def test_a_mailbox_inside_the_allowance_is_free_and_says_so(monkeypatch):
    """Core + your OS includes two — the second one costs nothing."""
    q = _quote(monkeypatch, plan="professional", mailboxes=1)
    assert q["charged"] is False
    assert q["included"] == 2


def test_a_depot_brings_its_own_mailbox(monkeypatch):
    """The £149 depot price includes a mailbox, so the allowance grows with it —
    exactly as sync_mailbox_addon computes it."""
    q = _quote(monkeypatch, plan="starter", mailboxes=1, depots=1)
    assert q["charged"] is False
    assert q["included"] == 2


def test_grandfathered_mailboxes_never_push_anyone_into_the_add_on(monkeypatch):
    """Launch-era connections are free forever; a third mailbox behind two of
    them is still the tenant's first billable one, and it's inside Pro's
    allowance, so nothing is charged."""
    q = _quote(monkeypatch, plan="starter", mailboxes=2, grandfathered=True)
    assert q["charged"] is False
    assert q["total_after"] == 99           # the plan alone


def test_enterprise_is_unlimited_and_quotes_nothing(monkeypatch):
    q = _quote(monkeypatch, plan="enterprise", mailboxes=4)
    assert q["unlimited"] is True
    assert q["charged"] is False


# ── It refuses to state a total it cannot compute exactly ───────────────────
@pytest.mark.parametrize("plan, why", [
    ("professional", "£248 assumes a £149 OS module; Transport's is £199"),
    ("enterprise", "negotiated"),
    ("scale", "retired — not in the catalog at all"),
    (None, "no plan on the tenant"),
])
def test_no_total_is_offered_for_a_plan_whose_price_is_not_one_flat_number(monkeypatch, plan, why):
    assert _quote(monkeypatch, plan=plan, mailboxes=3)["total_after"] is None, why


def test_no_total_is_offered_beside_a_depot_add_on(monkeypatch):
    """The plan price is not this tenant's bill — they're paying £149s too."""
    q = _quote(monkeypatch, plan="starter", mailboxes=3, depots=1)
    assert q["charged"] is True
    assert q["total_after"] is None


def test_a_tenant_without_a_subscription_cannot_add(monkeypatch):
    assert _quote(monkeypatch, plan="starter", status=None)["can_add"] is False


# ── Nothing reaches the provider without passing the price ─────────────────
def _env(monkeypatch, connected=True, subscribed=True, mailboxes=1):
    monkeypatch.setattr(dashboard.db, "get_tenant_config", lambda *a, **k: {})
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {
        "name": "T", "provider": "microsoft", "mailbox": "hello@acme.co.uk",
        "portal_slug": "acme", "plan": "starter",
        "subscription_status": "active" if subscribed else None,
        "stripe_subscription_id": "sub_1" if subscribed else None})
    monkeypatch.setattr(dashboard.db, "get_connection_status",
                        lambda *a, **k: {"sub_expires": None} if connected else None)
    monkeypatch.setattr(dashboard.db, "visible_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "list_mailboxes", lambda *a, **k: _boxes(mailboxes))
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: {})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.billing, "billable_depots", lambda *a, **k: 0)
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: subscribed)
    monkeypatch.setattr(dashboard.portal, "ensure_portal_slug", lambda *a, **k: "acme")
    monkeypatch.setattr(dashboard.storage, "banner_public_url", lambda *a, **k: None)


def test_the_confirm_step_states_the_price_and_the_total(client, monkeypatch):
    _env(monkeypatch)
    html = client.get("/app/mailboxes/add").text
    assert "£39/month" in html
    assert "£138/month" in html
    assert "+300" in html
    # and only THEN the buttons that spend it
    assert html.index("£39/month") < html.index("/oauth/connect?provider=microsoft")


def test_the_mailboxes_page_no_longer_links_straight_to_the_provider(client, monkeypatch):
    _env(monkeypatch, mailboxes=2)
    html = client.get("/app/mailboxes").text
    assert "/app/mailboxes/add" in html
    assert "oauth/connect" not in html, "the add buttons must go via the price first"


def test_settings_offers_the_add_route_to_a_subscriber(client, monkeypatch):
    _env(monkeypatch, subscribed=True)
    html = client.get("/app/settings").text
    assert "/app/mailboxes/add" in html
    assert "£39/month" in html


def test_settings_never_hands_a_non_subscriber_the_add_link(client, monkeypatch):
    """That link degrades to mode=replace for them — it would disconnect the one
    mailbox they have. They get the plans page instead."""
    _env(monkeypatch, subscribed=False)
    html = client.get("/app/settings").text
    assert "/app/mailboxes/add" not in html
    assert "mode=add" not in html
    assert "/billing/plans" in html
    assert "£39/month each" in html          # still told what it would cost


def test_the_confirm_step_is_shut_to_a_non_subscriber(client, monkeypatch):
    _env(monkeypatch, subscribed=False)
    assert client.get("/app/mailboxes/add").status_code == 404


def test_add_is_not_read_as_a_mailbox_id(client, monkeypatch):
    """/mailboxes/add must be declared above /mailboxes/{mailbox_id}/... —
    otherwise 'add' routes into the per-mailbox pages."""
    _env(monkeypatch)
    assert client.get("/app/mailboxes/add").status_code == 200
