"""Email top-up credit (Kris 2026-09-01).

Prepaid emails, bought one-off, spent only once the plan allowance is gone, and
carried over until used. The shape was chosen against a real constraint: a
RECURRING top-up at £39/300 is dominated by the extra-mailbox add-on (same
price, and that one brings a mailbox with it), and priced lower it undercuts Pro
— Starter plus two cheap monthly top-ups would beat £99 for 750. A one-off
competes with nothing, because it doesn't recur.

The rules worth defending in a test are the ones about other people's money:

  * under soft enforcement nothing is refused AND no credit is spent — going
    over the allowance costs a tenant nothing today, so burning prepaid credit
    on those emails would charge them for something they were already getting;
  * the soft path costs no database round-trip, because it sits on the drafting
    path for every inbound email;
  * a Checkout session only ever credits when it is a PAID top-up, and the
    emails come from the order's own metadata rather than being recomputed;
  * the quantity on the form is clamped, not trusted.

The atomicity and the once-only-per-payment guarantees live in Postgres
(grant_email_credit / spend_email_credit in db/schema.sql) and were exercised
against the real database when the migration was applied — a replayed webhook
credits once, and two drafts racing for the last credit cannot both take it.

Run: python -m pytest tests/test_email_topup.py -q
"""
import pytest

import app.billing as billing
import app.usage as usage
from app.config import settings


@pytest.fixture
def hard(monkeypatch):
    monkeypatch.setattr(settings, "usage_enforcement", "hard")


@pytest.fixture
def soft(monkeypatch):
    monkeypatch.setattr(settings, "usage_enforcement", "soft")


def _state(monkeypatch, over, credits, spent=None):
    """Stub summarize + the credit spend, recording whether either was called."""
    calls = {"summarize": 0, "spend": 0}

    def _summarize(tenant_id, client=None):
        calls["summarize"] += 1
        return {"over": over, "credits": credits, "pct": 100.0 if over else 10.0}

    def _spend(tenant_id, email_log_id=None):
        calls["spend"] += 1
        return spent if spent is not None else -1

    monkeypatch.setattr(usage, "summarize", _summarize)
    monkeypatch.setattr(usage.db, "spend_email_credit", _spend)
    return calls


# ── Soft enforcement must cost nothing, in money or in queries ─────────────
def test_soft_never_refuses_and_never_spends_credit(monkeypatch, soft):
    """Nothing is being blocked, so there is nothing for the credit to buy back.
    Spending it here would charge for emails they were getting free."""
    calls = _state(monkeypatch, over=True, credits=500, spent=499)
    claim = usage.claim_draft("t1")
    assert claim["allowed"] is True
    assert claim["used_credit"] is False
    assert calls["spend"] == 0


def test_the_soft_path_does_not_touch_the_database(monkeypatch, soft):
    """This runs before EVERY inbound draft. When the answer cannot be no, it
    must not cost a round-trip to reach it."""
    calls = _state(monkeypatch, over=True, credits=0)
    usage.claim_draft("t1")
    assert calls["summarize"] == 0, "soft enforcement asked the database a question it already knew"


# ── Hard enforcement: the allowance first, then credit, then no ────────────
def test_inside_the_allowance_nothing_is_spent(monkeypatch, hard):
    calls = _state(monkeypatch, over=False, credits=300)
    claim = usage.claim_draft("t1")
    assert claim["allowed"] is True
    assert claim["used_credit"] is False
    assert calls["spend"] == 0, "credit was spent while the plan still had room"


def test_past_the_allowance_one_credit_is_spent_per_email(monkeypatch, hard):
    calls = _state(monkeypatch, over=True, credits=300, spent=299)
    claim = usage.claim_draft("t1")
    assert claim == {"allowed": True, "used_credit": True, "credits_left": 299}
    assert calls["spend"] == 1


def test_out_of_allowance_and_out_of_credit_is_a_refusal(monkeypatch, hard):
    """-1 from the database means the balance was already zero."""
    _state(monkeypatch, over=True, credits=0, spent=-1)
    claim = usage.claim_draft("t1")
    assert claim["allowed"] is False
    assert claim["credits_left"] == 0


def test_quota_status_only_blocks_when_there_is_no_credit_behind_it(monkeypatch, hard):
    monkeypatch.setattr(usage, "summarize", lambda *a, **k: {
        "over": True, "credits": 50, "pct": 100.0})
    assert usage.quota_status("t1")["blocked"] is False
    monkeypatch.setattr(usage, "summarize", lambda *a, **k: {
        "over": True, "credits": 0, "pct": 100.0})
    assert usage.quota_status("t1")["blocked"] is True


# ── Crediting a purchase ───────────────────────────────────────────────────
def _session(**over):
    s = {"metadata": {"kind": "email_topup", "tenant_id": "t1", "emails": "300"},
         "payment_status": "paid", "payment_intent": "pi_1",
         "client_reference_id": "t1"}
    s.update(over)
    return s


@pytest.fixture
def granted(monkeypatch):
    seen = []
    monkeypatch.setattr(billing.db, "grant_email_credit",
                        lambda t, e, p: seen.append((t, e, p)) or 300)
    return seen


def test_a_paid_topup_credits_the_emails_from_its_own_metadata(granted):
    """Read back off the order, not recomputed from today's price — a later
    change to what £39 buys must not restate an order already paid for."""
    assert billing.credit_topup_from_session(_session()) == 300
    assert granted == [("t1", 300, "pi_1")]


@pytest.mark.parametrize("session, why", [
    (_session(payment_status="unpaid"), "not paid"),
    (_session(metadata={"kind": "something_else"}), "not a top-up"),
    (_session(metadata={"kind": "email_topup", "emails": "0"}), "no emails on it"),
    (_session(metadata={"kind": "email_topup", "emails": "300"},
              client_reference_id=None), "no tenant on it"),
])
def test_nothing_else_credits_anything(granted, session, why):
    assert billing.credit_topup_from_session(session) is None, why
    assert granted == [], why


def test_a_subscription_checkout_is_left_alone(granted):
    """The plan-upgrade path shares this webhook; it must fall straight past."""
    assert billing.credit_topup_from_session({"metadata": {}, "payment_status": "paid"}) is None
    assert granted == []


# ── The purchase itself ────────────────────────────────────────────────────
@pytest.fixture
def checkout(monkeypatch):
    captured = {}

    class _Session:
        @staticmethod
        def create(**kw):
            captured.update(kw)
            return type("S", (), {"url": "https://checkout.stripe.test/x"})()

    monkeypatch.setattr(billing.stripe, "checkout", type("C", (), {"Session": _Session}))
    monkeypatch.setattr(billing, "ensure_stripe_customer", lambda *a, **k: "cus_1")
    monkeypatch.setattr(billing.plans, "topup_price_id", lambda: "price_topup")
    return captured


def test_the_topup_is_a_one_off_payment_not_a_subscription(checkout):
    """The whole reason the shape works. mode='subscription' here would recreate
    exactly the cannibalisation the one-off was chosen to avoid."""
    billing.create_topup_checkout_session("t1", 1, None, "https://s", "https://c")
    assert checkout["mode"] == "payment"
    assert "subscription_data" not in checkout


def test_the_emails_bought_are_stamped_on_the_order(checkout):
    billing.create_topup_checkout_session("t1", 3, None, "https://s", "https://c")
    assert checkout["line_items"] == [{"price": "price_topup", "quantity": 3}]
    assert checkout["metadata"]["emails"] == "900"
    assert checkout["payment_intent_data"]["metadata"]["kind"] == "email_topup"


@pytest.mark.parametrize("asked, got", [(0, 1), (-5, 1), (999, billing.TOPUP_MAX_PACKS)])
def test_the_quantity_is_clamped_not_trusted(checkout, asked, got):
    """A tampered or fat-fingered quantity must not turn £39 into a four-figure
    order."""
    billing.create_topup_checkout_session("t1", asked, None, "https://s", "https://c")
    assert checkout["line_items"][0]["quantity"] == got


def test_no_price_id_means_nothing_is_for_sale(checkout, monkeypatch):
    """Same rule the other add-ons use: unconfigured is simply off."""
    monkeypatch.setattr(billing.plans, "topup_price_id", lambda: None)
    with pytest.raises(RuntimeError):
        billing.create_topup_checkout_session("t1", 1, None, "https://s", "https://c")


# ── Nothing is sold while the limit isn't enforced ─────────────────────────
def test_the_topup_route_refuses_while_enforcement_is_soft(monkeypatch):
    """The whole point of credit is getting past a limit. Under soft
    enforcement claim_draft never spends it, so a top-up bought now could never
    be used — taking £39 for emails the tenant already gets free. The page hides
    the button; this is the lock behind it."""
    import app.billing_routes as br
    from fastapi.testclient import TestClient
    from app.auth import TenantCtx, require_tenant
    from app.main import app as fastapi_app
    from app.security import require_csrf

    monkeypatch.setattr(settings, "usage_enforcement", "soft")
    monkeypatch.setattr(br.billing, "is_configured", lambda: True)
    monkeypatch.setattr(br.plans, "topup_price_id", lambda: "price_topup")
    fastapi_app.dependency_overrides[require_tenant] = lambda: TenantCtx(tenant_id="t1", client=None)
    fastapi_app.dependency_overrides[require_csrf] = lambda: None
    try:
        r = TestClient(fastapi_app).post("/billing/topup", data={"packs": "1"})
        assert r.status_code == 503
    finally:
        fastapi_app.dependency_overrides.clear()
