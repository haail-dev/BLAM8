"""Discretion mode — the filming mask (src/app/static/discretion.js).

The mask runs in the browser, so the rules are pinned here by running the same
regexes against the strings that actually appear in the product. What this
guarantees:

  - the things that leaked are masked (a real address reached two published
    videos on 2026-09-22);
  - the things the product is FOR are not — a booking ref, an invoice number, a
    price and a date have to survive, or the mode is useless to film with;
  - masking is idempotent, so the MutationObserver cannot loop.

Run: python -m pytest tests/test_discretion.py -q
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parents[1] / "src/app" / "static" / "discretion.js"


def _js_regex(name: str) -> re.Pattern:
    """Lift a rule out of the JS so the test cannot drift from the shipped file."""
    src = JS.read_text(encoding="utf-8")
    m = re.search(rf"^\s*var {name} = /(.+)/([gimsu]*);\s*$", src, re.M)
    assert m, f"{name} not found in {JS.name}"
    body = m.group(1)
    # JS-only constructs that Python's re spells differently
    body = body.replace("(?:", "(?:")
    return re.compile(body)


def mask(text: str) -> str:
    """The same four substitutions, in the same order, as discretion.js."""
    email = _js_regex("EMAIL")
    postcode = _js_regex("POSTCODE")
    street = _js_regex("STREET")
    phone = _js_regex("PHONE")
    if "@" in text:
        text = email.sub(lambda m: m.group(1) + "***" + m.group(2), text)
    text = postcode.sub(lambda m: m.group(1) + " ***", text)
    text = street.sub(lambda m: "***" + m.group(2), text)
    text = phone.sub(
        lambda m: m.group(1) + re.sub(r"\s", "", m.group(2))[:3] + " ***", text)
    return text


# --- what must be masked -----------------------------------------------------

@pytest.mark.parametrize("raw", [
    "kris.deane93+hannah@gmail.com",
    "business.blam8+meg.ashworth@gmail.com",
    "booked by the agent · kris.deane93+hannah@gmail.com",
    "meg@harringtons.co.uk",
])
def test_an_email_never_survives_intact(raw):
    out = mask(raw)
    assert "kris.deane93" not in out
    assert "business.blam8" not in out
    assert "***" in out
    # still reads as an address on film — that is the point of a demo
    assert "@" in out


def test_the_address_that_actually_leaked():
    raw = "booked by the agent · kris.deane93+hannah@gmail.com"
    assert mask(raw) == "booked by the agent · k***@gmail.com"


@pytest.mark.parametrize("raw,gone", [
    ("The dog is at LS9 6PQ", "6PQ"),
    ("Collection from 14 Harringtons Road", "14 Harringtons"),
    ("Call them on 07700 900123", "900123"),
    ("Ring +44 7700 900123 before six", "900123"),
])
def test_postcodes_streets_and_phones_are_masked(raw, gone):
    assert gone not in mask(raw)
    assert "***" in mask(raw)


def test_a_postcode_keeps_its_district_so_the_line_still_reads():
    assert mask("The dog is at LS9 6PQ") == "The dog is at LS9 ***"


def test_a_street_keeps_its_name_and_loses_the_house_number():
    out = mask("Collection from 14 Harringtons Road")
    assert "Harringtons Road" in out and "14" not in out


# --- what must NOT be masked, or the mode is useless to film with ------------

@pytest.mark.parametrize("raw", [
    "Weekend boarding (BK-1021)",
    "deposit invoice raised (BK-1038) · 7 messages",
    "please use INV-1008 as the payment reference",
    "Cost: £91.50 (3 nights at £30.50)",
    "Mon 12 Oct 08:30 → Mon 26 Oct 17:00 · 14 nights",
    "Quoted £480 from your price list",
    "vaccination noted as current to June 2027",
    "Drop-off: Friday 9 October at 8.30am",
])
def test_the_product_is_left_alone(raw):
    assert mask(raw) == raw, "discretion must not eat refs, money or dates"


# --- the observer must not be able to loop ----------------------------------

@pytest.mark.parametrize("raw", [
    "kris.deane93+hannah@gmail.com",
    "The dog is at LS9 6PQ",
    "Collection from 14 Harringtons Road",
    "Call them on 07700 900123",
])
def test_masking_is_idempotent(raw):
    once = mask(raw)
    assert mask(once) == once, "a second pass changed it — the observer would loop"


# --- the mode is off unless a tenant is explicitly opted in ------------------

def test_discretion_is_off_for_an_unknown_tenant():
    from app import discretion
    discretion.forget()
    assert discretion.enabled(None) is False


def test_the_script_only_touches_text_nodes():
    """Input values and contenteditable must be skipped, or filming would
    corrupt whatever is typed while the mask is on."""
    src = JS.read_text(encoding="utf-8")
    assert "SHOW_TEXT" in src
    assert "isContentEditable" in src
    for tag in ("TEXTAREA", "INPUT", "SCRIPT", "STYLE"):
        assert tag in src, f"{tag} must be skipped"
