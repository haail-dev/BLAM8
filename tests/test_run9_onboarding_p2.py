"""Run #9 (Brightwell Home Cleaning, 2026-09-05) — the onboarding P2s, pinned.

Driven as a real owner, the knowledge interview stopped early because a stray
keyword lit kinds it never asked about; every later card re-listed the saved
notes and each Save re-wrote them; a fact given in one turn was held for the
next; the diary step asked questions where every other step led with its card;
the card's prefill left the collapsed faces on the old receipts; the later
steps knew the trade only through the chat history; and the website reader was
never offered. Run: python -m pytest tests/test_run9_onboarding_p2.py -q
"""
from tests.booking_fixtures import carried_config
from tests.knowledge_review_fixture import review_store
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import concierge
from app import knowledge_coverage as kc

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "src/app" / "static" / "concierge.js").read_text(encoding="utf-8")


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    from app.auth import TenantCtx, require_tenant
    from app.main import app
    app.dependency_overrides[require_tenant] = lambda: TenantCtx(tenant_id="t1", client=None)
    yield TestClient(app)
    app.dependency_overrides.clear()


# ── #6 coverage by keyword ───────────────────────────────────────────────────
def test_a_prices_note_alone_lights_only_prices():
    """Live: the Prices note ("3 hours fortnightly (£58.50 a visit)… 2.5 hours
    weekly") lit Prices + How often work repeats + Hours — 3 of 8 — and the
    interview declared itself done without asking about cadence or opening
    hours. A kind is claimed by the LABEL, or by two distinct keyword hits."""
    cov = kc.coverage([{"source": "Prices", "text":
        "Regular cleans: £19.50/hour, 2-hour minimum. Typical 3-bed house: 3 hours fortnightly "
        "(£58.50 a visit) or 2.5 hours weekly. 1-2 bed flat: approx 2 hours. Oven clean: £55."}])
    assert [k["key"] for k in cov["covered"]] == ["prices"]
    assert cov["score"] == "1 of 8"


def test_a_real_cadence_note_and_a_vetting_note_are_credited():
    cov = kc.coverage([
        {"source": "Cadence", "text": "Weekly or fortnightly, the same weekday and arrival window "
                                       "each time, ongoing until you tell us to stop."},
        {"source": "Staff and insurance", "text": "Cleaners are directly employed; every one holds a "
                                                  "basic DBS certificate; public liability 2m."},
        {"source": "Hours", "text": "Mon-Fri 8am to 5pm"},
    ])
    keys = [k["key"] for k in cov["covered"]]
    assert "cadence" in keys and "safety" in keys and "hours" in keys
    # two distinct hits in the TEXT claim a kind even under a neutral label
    assert "cadence" in kc.classify("Note 3", "Every 6 weeks; we text a reminder the week before.")
    # one stray word does not
    assert "hours" not in kc.classify("Rules", "We need 48 hours of notice to skip a visit.")


# ── #14 the website first ────────────────────────────────────────────────────
def test_the_opening_ask_offers_the_website_first_when_the_page_has_the_reader():
    everything = kc.coverage([])["missing"]
    ask = kc.ask_text(everything, website=True)
    assert ask.startswith("First, in one line, ask whether I have a website")
    assert "Start from your website" in ask and "interview me for the facts" in ask
    # a base with some notes already does not re-open with the website line
    some = kc.coverage([{"source": "Prices", "text": "95 pounds"}])["missing"]
    assert kc.ask_text(some, website=True).startswith("Interview me")
    assert kc.ask_text(everything, website=False).startswith("Interview me")
    src = (ROOT / "src/app" / "onboarding.py").read_text(encoding="utf-8")
    assert "coverage_ask = ''" in src  # The helper remains opt-in; no automatic policy interview.


# ── #8 #9 the note-writing rules ─────────────────────────────────────────────
def test_the_knowledge_guidance_writes_every_fact_that_turn_and_does_not_echo_it():
    g = concierge.ONBOARDING_STEPS["knowledge"]["guidance"]
    assert "EVERY turn in which they give you a fact carries that fact as a note" in g
    assert "do NOT repeat a note's text" in g
    assert "Start from your website" in g


# ── #13 the brief reaches the later steps ────────────────────────────────────
def test_the_later_steps_know_the_business_from_step_one():
    tenant = {"name": "Brightwell Home Cleaning", "industry": "domestic cleaning",
              "brief": "Small domestic cleaning company in Chesterfield; three cleaners."}
    p = concierge.build_onboarding_prompt("knowledge", {}, coverage=kc.coverage([]), tenant=tenant)
    assert "<your_customer>" in p and "three cleaners" in p and "domestic cleaning" in p
    assert "<your_customer>" in concierge.build_onboarding_prompt("voice", {}, tenant=tenant)
    # step 1 WRITES the brief — it is not fed back in
    assert "<your_customer>" not in concierge.build_onboarding_prompt("interview", {}, tenant=tenant)
    assert "<your_customer>" not in concierge.build_onboarding_prompt("knowledge", {}, coverage=kc.coverage([]))


def test_the_onboard_route_passes_the_tenant_to_the_voice_and_knowledge_steps(client, monkeypatch, review_store):
    import app.concierge_routes as routes
    seen = []

    async def fake(step, message, history, current, tenant_id=None, coverage=None, tenant=None,
                   held=None):
        seen.append((step, tenant))
        return SimpleNamespace(reply="ok", form_fill={}, ready_to_advance=False, notes=[],
                               usage=None, model="m")
    monkeypatch.setattr(routes.concierge, "answer_onboarding", fake)
    monkeypatch.setattr(routes.usage, "record_anthropic", lambda *a, **k: None)
    monkeypatch.setattr(routes.fairuse, "onboarding_allowed", lambda *a, **k: True)
    monkeypatch.setattr(routes, "_tenant_context", lambda ctx: {"name": "Brightwell", "brief": "cleaning"})
    monkeypatch.setattr(routes.db, "knowledge_source_texts", lambda *a, **k: [])
    monkeypatch.setattr(routes.db, "get_tenant", lambda *a, **k: {"industry": "Example"})
    monkeypatch.setattr(routes.db, "get_tenant_config", lambda *a, **k: {})
    monkeypatch.setattr(routes.db, "get_connection_status", lambda *a, **k: {"id": "mailbox"})
    from app import onboarding, setup_held
    monkeypatch.setattr(setup_held, "load", lambda *a, **k: [])
    next_target = {"label": "Getting paid", "href": "/app/invoices/settings"}
    monkeypatch.setattr(onboarding, "_next_step_target", lambda ctx: next_target)
    for step in ("knowledge", "voice", "interview"):
        r = client.post("/concierge/onboard", json={"step": step, "question": "hi", "history": [], "current": {}})
        assert r.status_code == 200, r.text
    for (step, tenant), expected_next in zip(seen, (next_target, {"label": "Knowledge", "href": "/onboarding/knowledge"},
                                                    {"label": "Customer inbox", "href": "/onboarding/mailbox"})):
        assert tenant["onboarding_journey"]["current"] == step
        assert tenant["onboarding_journey"]["next"] == expected_next
        assert tenant["onboarding_journey"]["saved_completion"]["mailbox"] is True
        business = {key: value for key, value in tenant.items() if key != "onboarding_journey"}
        assert business == ({"name": "Brightwell", "brief": "cleaning"} if step in ("knowledge", "voice") else {})


# ── #7 the card carries only new notes ───────────────────────────────────────
def test_knowledge_card_staging_does_not_silently_filter_duplicate_labels():
    card = JS.split("function appendRecommendCard(rec) {")[1].split("var wrap = document.createElement")[0]
    assert "kbNewValues(" not in card
    assert "Knowledge notes — pending review" in JS
    assert 'new CustomEvent("blam8:knowledge-review")' in JS
    assert "kbRememberSaved(rec.values);" not in JS


# ── #10 card first on the diary step · #11 the prefill opens the folds ───────
def test_the_diary_step_leads_with_the_card_and_the_prefill_opens_the_folds():
    """Kris, live: the diary step asked two questions before any card while every
    later step opened with its card; and after the card filled the page the
    collapsed faces still read the OLD receipts ("No buffer", "Recurring Off")
    over inputs that held the new values."""
    prompt = concierge.build_help_prompt(facts="", knowledge="")
    assert "Propose settings only from explicit owner answers" in prompt
    assert "list every number you ASSUMED in `reply`" not in prompt
    assert "ask up to three short questions first and recommend on a later" not in prompt
    pf = JS.split("function pfEvents(el) {")[1].split("function pfNumber")[0]
    assert 'el.closest("details")' in pf and "fold.open = true;" in pf


def test_the_diary_page_ask_demands_the_card_and_the_snapshot_carries_the_diary_facts():
    """Live re-check of #10 (aed6a5b): rule 9 alone did not move the model — it
    asked three questions and declared recurring bookings and the travel gap
    'not set yet' on a diary showing both. The page's own ask now demands the
    card in the first reply, and the customer block states what IS set."""
    html = (ROOT / "src/app" / "templates" / "app" / "_scheduling_settings.html").read_text(encoding="utf-8")
    assert "using only my explicit answers and supplied source material" in html
    assert "Ask about missing choices before proposing a change" in html
    cfg = carried_config({"working_days": "mon,tue,wed,thu,fri", "capacity_per_window": 3, "buffer_minutes": 30,
           "open_time": "08:00", "close_time": "17:00", "windows": "day", "slot_minutes": 60,
           "min_notice_hours": 72, "horizon_days": 60,
           "service_types": [{"name": "Regular clean", "minutes": 120, "timing": "half"}],
           "recurring": {"enabled": True, "frequencies": ["weekly", "fortnightly"], "max_occurrences": 52},
           "nouns": {"booking": "clean", "participant": "customer"}})
    line = concierge._diary_today(cfg)
    assert "30-minute gap" in line and "72 hours' notice, books 60 days ahead" in line
    assert "Regular clean (120 min, half)" in line
    assert "recurring bookings ON (weekly, fortnightly, up to 52 times)" in line
    assert "never describe them as missing" in line
    assert concierge._diary_today(None) == "configured"
    block = concierge._customer_block({"name": "B", "cfg": cfg, "setup": {"diary_configured": True}})
    assert "Diary: configured — " in block and "recurring bookings ON" in block
    prompt = concierge.build_help_prompt(facts="", knowledge="")
    assert "Preserve existing choices unless" in prompt
