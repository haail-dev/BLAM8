"""The free trial's email cap is disclosed wherever the trial is offered.

The trial ends on 14 days OR TRIAL_EMAIL_CAP handled emails, whichever comes first
(src/app/trial.py). The time limit was stated everywhere; the email cap was stated nowhere
public — so a busy signup could hit an invisible wall. terms.html §6 always covered it in
principle ("until your usage allowance is reached"), but never with a number.

Every disclosure reads the number from settings rather than hardcoding it, so the site
cannot quote a cap the deployment isn't enforcing. These tests pin that.

Run: python -m pytest tests/test_trial_disclosure.py -q
"""
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def _cap() -> str:
    return str(settings.trial_email_cap)


def test_the_cap_exists_and_is_what_trial_py_enforces():
    from app import trial
    assert trial.TRIAL_EMAIL_CAP == settings.trial_email_cap


def test_landing_pricing_states_the_cap_next_to_the_price(client):
    body = client.get("/").text
    assert _cap() in body
    assert "whichever comes first" in body


def test_landing_faq_answer_states_the_cap(client):
    # The FAQ is also JSON-LD, so this is the answer an AI reads back when asked what
    # the trial includes — the launch post invites exactly that.
    body = client.get("/").text
    assert f"14 days or {_cap()} handled emails" in body


def test_login_page_states_the_cap_where_the_trial_actually_starts(client):
    assert _cap() in client.get("/login").text


def test_llms_txt_states_the_cap(client):
    body = client.get("/llms.txt").text
    assert f"14 days or {_cap()} handled emails" in body


def test_no_public_surface_promises_a_bare_14_day_trial_without_the_cap(client):
    """A bare "14-day free trial" in prose reads as "14 days, that's the only limit".
    Buttons are exempt — a CTA saying "Start free 14-day trial" is fine while the copy
    beside it carries the qualifier."""
    body = client.get("/").text
    # The pricing subtitle and the how-much answer are the two prose spots that used to
    # overpromise; both must now carry the qualifier.
    assert "14-day free trial. No card to start." not in body
    assert "14-day free trial, no card to start.</p>" not in body


def test_the_disclosure_tracks_the_setting_rather_than_a_hardcoded_number(client, monkeypatch):
    # If someone raises the cap on Render, the site must say the new number, not 100.
    monkeypatch.setattr(settings, "trial_email_cap", 250)
    from app.templating import templates
    monkeypatch.setitem(templates.env.globals, "trial_email_cap", 250)
    body = client.get("/").text
    assert "14 days or 250 handled emails" in body
