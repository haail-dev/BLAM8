"""Kris, 16 Oct 2026: a link labelled "Invoices" took him to Plans & billing (the BLAM8 subscription page). The agent had no
invoices kind to name, so it used 'billing'. Inbox, invoices, quotes and the money overview are now kinds of their own.
Run: python -m pytest tests/test_concierge_link_kinds.py -q
"""
import pathlib

from app import concierge
from app.concierge_routes import resolve_business_links

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = (ROOT / "src/app/concierge.py").read_text(encoding="utf-8")


def test_inbox_invoices_quotes_and_money_are_kinds_the_agent_can_name():
    for kind in ("inbox", "invoices", "quotes", "money"):
        assert kind in concierge.BUSINESS_LINK_KINDS
    assert "billing" in concierge.BUSINESS_LINK_KINDS and "invoice_settings" in concierge.BUSINESS_LINK_KINDS   # the others are unchanged


def test_each_resolves_to_its_own_page_and_invoices_is_not_the_subscription_page():
    out = resolve_business_links([
        {"kind": "invoices", "id": "", "label": "Invoices"}, {"kind": "quotes", "id": "", "label": "Quotes"},
        {"kind": "inbox", "id": "", "label": "Inbox overview"}, {"kind": "money", "id": "", "label": "Money"},
        {"kind": "billing", "id": "", "label": "Plans"}])
    assert [(l["label"], l["href"]) for l in out] == [
        ("Invoices", "/app/invoices"), ("Quotes", "/app/quotes"), ("Inbox overview", "/app/conversations"),
        ("Money", "/app/money"), ("Plans", "/billing/plans")]


def test_the_agent_is_told_which_kind_is_which_and_never_to_use_billing_for_them():
    assert "kind 'pipeline', 'knowledge', 'settings', 'billing', 'inbox', 'invoices', 'quotes', 'money' with id ''." in SRC
    assert "for any of those NEVER use 'billing'" in SRC
