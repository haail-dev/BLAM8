"""Per-industry vocabulary seeding for drafts (src/app/industry_lingo.py + the
<industry_context> block in build_system_prompt).

Pins: known industries get a seed; unknown/free-text/None get none; the seed only
enters the prompt for a known industry — so a tenant without a known industry gets
a byte-identical prompt (the frozen-prompt contract).

Run: python -m pytest tests/ -q
"""
from app import industry_lingo
from app.agent import build_system_prompt


def test_seed_for_known_and_unknown():
    assert "callouts" in industry_lingo.seed_for("trades")
    assert industry_lingo.seed_for("LOGISTICS")            # case-insensitive
    assert industry_lingo.seed_for("hospitality") is None  # free-text long tail
    assert industry_lingo.seed_for(None) is None
    assert industry_lingo.seed_for("") is None


def test_known_industry_injects_context():
    p = build_system_prompt({"industry": "trades"})
    assert "<industry_context>" in p
    assert "Trades & field service" in p
    assert "callouts" in p


def test_unknown_industry_adds_nothing():
    # free-text "Something else" sector → no seed, no section
    assert "<industry_context>" not in build_system_prompt({"industry": "hairdressing"})


def test_no_industry_is_byte_identical():
    # the frozen path: no industry key → no industry_context, prompt unchanged
    assert "<industry_context>" not in build_system_prompt({})
    assert "<industry_context>" not in build_system_prompt({"auto_send": True})
