"""Stripe webhook — signature verification, event dedup, and the dispatch table.

The webhook is the source of truth for subscription state. Pin the surface that
matters most:
  * a valid signature + a handled event type runs the right billing helper
  * a tampered signature is rejected 400 (Stripe sees the failure in the
    Dashboard, we never write anything)
  * a duplicate event id (retry) is acknowledged 200 but is a silent no-op

Run: python -m pytest tests/test_stripe_webhook.py -q
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
import stripe
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    # Pretend billing is configured so the route doesn't 503.
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_xxx")
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_test_xxx")
    return TestClient(app, follow_redirects=False)


def _construct(event: dict):
    """Bypass real signature math by patching construct_event to return
    whatever event dict we want (mirrors Stripe's own docs for unit tests)."""
    return patch("app.stripe_webhook.stripe.Webhook.construct_event",
                 return_value=event)


def test_unsigned_payload_is_rejected_400(client):
    """Tampered / missing-signature requests must 400, not 5xx — Stripe surfaces
    the rejection in its Dashboard delivery log, which is how we'd notice a
    forged or stale-secret event."""
    with patch("app.stripe_webhook.stripe.Webhook.construct_event",
               side_effect=stripe.error.SignatureVerificationError("bad sig", "sig")):
        r = client.post("/stripe/webhook", content=b'{"id":"evt_1"}',
                        headers={"stripe-signature": "t=0,v1=deadbeef"})
    assert r.status_code == 400


def test_duplicate_event_is_acknowledged_but_skipped(client):
    """First arrival of evt_1 inserts into stripe_events; second arrival of the
    SAME id must short-circuit (Stripe retries up to 3 days)."""
    evt = {"id": "evt_dup", "type": "customer.subscription.updated", "data": {"object": {}}}

    # First call: record_event returns True (new) → dispatch runs.
    with _construct(evt), \
         patch("app.stripe_webhook.db.stripe_record_event", return_value=True) as rec1, \
         patch("app.stripe_webhook.billing.sync_subscription") as sync1:
        r1 = client.post("/stripe/webhook", content=b"{}",
                         headers={"stripe-signature": "t=0,v1=ok"})
    assert r1.status_code == 200
    rec1.assert_called_once_with("evt_dup", "customer.subscription.updated")
    sync1.assert_called_once()

    # Second call: record_event returns False (duplicate) → sync NOT called.
    with _construct(evt), \
         patch("app.stripe_webhook.db.stripe_record_event", return_value=False), \
         patch("app.stripe_webhook.billing.sync_subscription") as sync2:
        r2 = client.post("/stripe/webhook", content=b"{}",
                         headers={"stripe-signature": "t=0,v1=ok"})
    assert r2.status_code == 200
    sync2.assert_not_called()


def test_checkout_completed_links_customer_to_tenant(client):
    """checkout.session.completed must call stripe_set_customer with the
    client_reference_id (the tenant id we set at session creation)."""
    evt = {
        "id": "evt_co", "type": "checkout.session.completed",
        "data": {"object": {"client_reference_id": "tenant-uuid",
                            "customer": "cus_123"}},
    }
    with _construct(evt), \
         patch("app.stripe_webhook.db.stripe_record_event", return_value=True), \
         patch("app.stripe_webhook.db.stripe_get_customer_id", return_value=None), \
         patch("app.stripe_webhook.db.stripe_set_customer") as set_cust:
        r = client.post("/stripe/webhook", content=b"{}",
                        headers={"stripe-signature": "t=0,v1=ok"})
    assert r.status_code == 200
    set_cust.assert_called_once_with("tenant-uuid", "cus_123",
                                     billing_provider="self_serve")


def test_subscription_deleted_marks_canceled(client):
    """customer.subscription.deleted → billing.mark_canceled (which writes
    status='canceled' for the matching tenant)."""
    evt = {
        "id": "evt_del", "type": "customer.subscription.deleted",
        "data": {"object": {"id": "sub_1", "customer": "cus_1"}},
    }
    with _construct(evt), \
         patch("app.stripe_webhook.db.stripe_record_event", return_value=True), \
         patch("app.stripe_webhook.billing.mark_canceled") as mark:
        r = client.post("/stripe/webhook", content=b"{}",
                        headers={"stripe-signature": "t=0,v1=ok"})
    assert r.status_code == 200
    mark.assert_called_once()


def test_unknown_event_type_is_ignored(client):
    """An event Stripe sends that we don't subscribe to must 200 (so Stripe
    stops retrying) but call NO billing helpers."""
    evt = {"id": "evt_unk", "type": "charge.refunded", "data": {"object": {}}}
    with _construct(evt), \
         patch("app.stripe_webhook.db.stripe_record_event", return_value=True), \
         patch("app.stripe_webhook.billing.sync_subscription") as sync:
        r = client.post("/stripe/webhook", content=b"{}",
                        headers={"stripe-signature": "t=0,v1=ok"})
    assert r.status_code == 200
    sync.assert_not_called()


def test_webhook_503s_when_billing_not_configured(client, monkeypatch):
    """If keys are blank, refuse fast so Stripe's Dashboard delivery log lights
    up — better than silently dropping events when an env var is missing."""
    monkeypatch.setattr(settings, "stripe_secret_key", "")
    r = client.post("/stripe/webhook", content=b"{}",
                    headers={"stripe-signature": "t=0,v1=ok"})
    assert r.status_code == 503
