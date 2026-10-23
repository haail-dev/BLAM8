"""Held for later, slice 2 — the pages render their held facts into their
forms on arrival (src/app/setup_held.py; kennels run #10, 2026-09-13).

Pins:
- the settings pages hand the shell {page, values, fresh}; the shell prints
  it as #setup-prefill; nothing when nothing is held;
- the applier fills from it only when no recommend-card stash is pending,
  only on pages whose prefill just types (never crm_fields), and on a saved
  page EMPTY fields only — Kris: "future form fills without being
  overwritten by new info";
- the Voice step's empty boxes take held values, a saved value stays;
- the Knowledge step carries held notes for the panel's ready card;
- every Save stamps its page's facts used.

Run: python -m pytest tests/test_setup_held_prefill.py -q
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import dashboard, db, onboarding
from app import knowledge as kb
from app.auth import TenantCtx, require_tenant
from app.main import app
from tests.knowledge_review_fixture import review_store

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


HELD = [
    {"page": "diary_settings", "name": "capacity_per_window", "value": "30", "step": "interview", "at": "t"},
    {"page": "diary_settings", "name": "min_notice_hours", "value": "10", "step": "interview", "at": "t"},
    {"page": "voice", "name": "sign_off", "value": "Kristie at Little Paws", "step": "interview", "at": "t"},
    {"page": "voice", "name": "tone", "value": "warm", "step": "interview", "at": "t", "used_at": "t2"},
    {"page": "knowledge", "name": "note", "value": "Trial day | Every new dog has a trial day before their first stay.",
     "step": "voice", "at": "t"},
]


# ── the page → shell hand-off ────────────────────────────────────────────────
def test_setup_prefill_hands_the_page_its_unused_facts(monkeypatch):
    monkeypatch.setattr(db, "get_tenant_config", lambda tid, client=None: {"setup_held": HELD})
    ctx = TenantCtx(tenant_id="t1", client=None)
    out = dashboard._setup_prefill(ctx, "diary_settings", fresh=True)
    assert out == {"page": "diary_settings", "fresh": True,
                   "values": [{"name": "capacity_per_window", "value": "30"},
                              {"name": "min_notice_hours", "value": "10"}]}
    assert dashboard._setup_prefill(ctx, "booking_details", fresh=True) is None   # nothing held → no tag
    assert dashboard._setup_prefill(ctx, "voice", fresh=False)["values"] == [
        {"name": "sign_off", "value": "Kristie at Little Paws"}]                    # used tone is done


def test_the_shell_prints_the_prefill_and_the_pages_pass_it():
    html = _read("src/app/templates/app/base.html")
    assert '<script type="application/json" id="setup-prefill">{{ setup_prefill | tojson }}</script>' in html
    src = _read("src/app/dashboard.py")
    for page, fresh in (("diary_settings", "fresh=not row"), ("booking_details", "fresh=not configured"),
                        ("dependants", "fresh=not groups"), ("invoice_settings", "fresh=not row")):
        assert f'_setup_prefill(ctx, "{page}", {fresh})' in src
    # every Save stamps its page's held facts used
    for page in ("diary_settings", "booking_details", "dependants", "invoice_settings", "crm_settings"):
        assert f'_held_used(ctx, "{page}")' in src
    onb = _read("src/app/onboarding.py")
    assert 'setup_held.mark_used(ctx.tenant_id, "voice", client=ctx.client)' in onb
    assert onb.count('setup_held.mark_used(ctx.tenant_id, "knowledge", client=ctx.client)') == 1
    assert 'setup_held.mark_used(ctx.tenant_id, "knowledge", client=ctx.client)' in _read("src/app/concierge_routes.py")


# ── the applier ──────────────────────────────────────────────────────────────
def test_applier_fills_from_the_page_only_when_no_card_stash_and_empty_fields_only():
    js = _read("src/app/static/concierge.js")
    assert 'var HELD_PAGES = ["diary_settings", "booking_details", "dependants", "invoice_settings", "packages"];' in js
    assert "crm_fields" not in js.split("var HELD_PAGES = ")[1].split(";")[0]   # never the posting page
    fn = js.split("function applyHeldPrefill()")[1].split("function applyPrefillData(data)")[0]
    assert 'document.getElementById("setup-prefill")' in fn
    assert "PF_ONLY_EMPTY = !data.fresh;" in fn                       # saved page → empty fields only
    assert "held: true" in fn
    # the session stash (a pressed card) wins; the held facts are the fallback
    head = js.split("function applySettingsPrefill()")[1].split("function applyHeldPrefill()")[0]
    assert head.count("return applyHeldPrefill()") >= 3
    # empty-only mode never touches a filled box, a select, a checkbox or saved rows
    setf = js.split("function pfSetField(form, name, value)")[1].split("function pfFillRows")[0]
    assert 'if (PF_ONLY_EMPTY && (el.type === "checkbox" || el.tagName === "SELECT" || String(el.value || "").trim())) return false;' in setf
    rows = js.split("function pfFillRows(")[1].split("function pfBanner")[0]
    assert "if (PF_ONLY_EMPTY && rows.children.length) return;" in rows
    # invoice details are typed in, and the banner says where the values came from
    assert 'data.page === "invoice_settings"' in js
    assert "Filled in from what you told the assistant." in js
    # a held fill never clears the card stash it did not consume
    tail = js.split("function applyPrefillData(data)")[1].split("applySettingsPrefill();")[0]
    assert "if (!data.held) { try { sessionStorage.removeItem(KEY_PREFILL); } catch (e) {} }" in tail
    # the wizard's knowledge step renders held notes as a ready card, after setOpen
    boot = js.split("if (openFlag === \"1\" || (ONB_MODE && openFlag !== \"0\")) setOpen(true);")[1]
    assert "function maybeHeldNotesCard()" in boot
    assert 'label: "knowledge notes — from what you told me earlier"' in boot


# ── the wizard steps ─────────────────────────────────────────────────────────
@pytest.fixture
def client():
    app.dependency_overrides[require_tenant] = lambda: TenantCtx(tenant_id="t1", client=None)
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_voice_step_prefills_empty_boxes_from_held_and_keeps_saved_values(client, monkeypatch):
    monkeypatch.setattr(db, "get_tenant_config",
                        lambda tid, client=None: {"setup_held": HELD, "agent_name": "the office team"})
    monkeypatch.setattr(db, "get_connection_status", lambda tid, client=None: None)
    r = client.get("/onboarding/voice")
    assert r.status_code == 200
    assert 'value="Kristie at Little Paws"' not in r.text          # sign_off is a textarea
    assert ">Kristie at Little Paws</textarea>" in r.text            # held sign-off, filled
    assert 'value="the office team"' in r.text                      # saved agent name untouched
    assert "data-held-prefill" in r.text and "sign-off" in r.text.split("data-held-prefill")[1][:200]
    assert "tone of voice" not in r.text.split("data-held-prefill")[1][:200]   # used item not claimed


def test_voice_step_says_nothing_when_nothing_is_held(client, monkeypatch):
    monkeypatch.setattr(db, "get_tenant_config", lambda tid, client=None: {})
    monkeypatch.setattr(db, "get_connection_status", lambda tid, client=None: None)
    r = client.get("/onboarding/voice")
    assert "data-held-prefill" not in r.text


def test_knowledge_step_carries_held_notes_for_the_panels_card(client, monkeypatch, review_store):
    monkeypatch.setattr(kb, "is_configured", lambda: True)
    monkeypatch.setattr(onboarding.db, "list_knowledge_sources", lambda tid, client=None: [])
    monkeypatch.setattr(onboarding.db, "knowledge_source_texts", lambda tid, client=None: [])
    monkeypatch.setattr(onboarding.db, "get_tenant_config", lambda tid, client=None: {"setup_held": HELD,"agent_name":"Office team","tone":"Professional","sign_off":"Regards"})
    r = client.get("/onboarding/knowledge")
    assert 'id="held-notes"' in r.text
    carrier = r.text.split('data-onb-held-notes=')[1].split("></div>")[0]
    assert "Trial day | Every new dog has a trial day" in carrier
    assert carrier.startswith("'")                                   # single-quoted: tojson escapes '
    # nothing held → no carrier
    monkeypatch.setattr(onboarding.db, "get_tenant_config", lambda tid, client=None: {"agent_name":"Office team","tone":"Professional","sign_off":"Regards"})
    assert 'id="held-notes"' not in client.get("/onboarding/knowledge").text
