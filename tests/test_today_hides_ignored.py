"""Ignored senders have no card on Today (the CRM's rule, Kris 2026-09-01).

Live 2026-09-30 (rig tidy for the second plumber film): ignoring a sender cleared their
thread and their queued drafts, but their open promises, their "angry" flag and their
held bookings stayed on Today, so an ignored test customer kept a card. Today now
filters at read time against the ignore list, exactly like the CRM and search: nothing
is stamped, and un-ignoring brings the card straight back.

Run: python -m pytest tests/test_today_hides_ignored.py -q
"""
import app.dashboard as dashboard
from tests.test_home_dashboard import client  # noqa: F401  (the fixture)
from tests.test_today_promise_popup import PROMISE, _crm


def test_a_promise_card_shows_for_a_customer(client, monkeypatch):
    _crm(monkeypatch, promises=[PROMISE])
    monkeypatch.setattr(dashboard.db, "ignored_addresses", lambda *a, **k: set())
    assert "Send the slab measurements" in client.get("/app/").text


def test_an_ignored_sender_has_no_card(client, monkeypatch):
    _crm(monkeypatch, promises=[PROMISE])
    monkeypatch.setattr(dashboard.db, "ignored_addresses", lambda *a, **k: {"SARAH@acme.co.uk"})
    html = client.get("/app/").text
    assert "Send the slab measurements" not in html        # case-insensitive, like the CRM
    assert "Sarah Vance" not in html


def test_an_unreadable_ignore_list_hides_nothing(client, monkeypatch):
    _crm(monkeypatch, promises=[PROMISE])

    def boom(*a, **k):
        raise RuntimeError("supabase down")
    monkeypatch.setattr(dashboard.db, "ignored_addresses", boom)
    assert "Send the slab measurements" in client.get("/app/").text
