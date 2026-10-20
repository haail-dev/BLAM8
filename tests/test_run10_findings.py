"""Kennels run #10 (2026-09-13) — the findings batch, pinned.

Run: python -m pytest tests/test_run10_findings.py -q
"""
from pathlib import Path
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from app import concierge, knowledge_coverage as kc, settings_recommend as sr
from app import onboarding
from app.auth import TenantCtx, require_tenant
from app.main import app

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ── the assistant ────────────────────────────────────────────────────────────
def test_diary_recommendation_asks_capacity_and_notice_before_guessing_and_corrects_as_a_diff():
    prompt = concierge.build_help_prompt(facts="", knowledge="")
    assert "never fill a default" in prompt
    assert "diary_settings/capacity_per_window" in prompt and "diary_settings/min_notice_hours" in prompt
    assert "Preserve existing choices unless" in prompt


def test_catalogue_picks_one_diary_shape_and_never_rolls_a_passed_year_forward():
    block = sr.catalogue_block()
    assert "A diary has ONE shape" in block
    assert "Never fill service rows AND stay pricing on one card" in block
    assert "A closure WITH a stated year that has already passed is not next year's closure" in block


def test_knowledge_notes_hold_only_what_was_said_and_three_questions_are_counted():
    g = concierge.ONBOARDING_STEPS["knowledge"]["guidance"]
    assert "A note holds ONLY what they said THIS turn" in g
    assert "a fourth is a broken turn" in g
    block = kc.prompt_block(kc.coverage([{"source": "Trial Night Prices", "text": "£30.50 one dog"}]))
    assert "never a fact lifted from another note or the website, never an inference" in block
    assert "Prices is only really covered when the main services carry a price" in block
    ask = kc.ask_text(kc.coverage([])["missing"])
    assert "at most three questions in this turn, numbered" in ask
    assert "and nothing I didn't" in ask


def test_setup_never_sells_a_tier_or_narrates_its_own_rules():
    prompt = concierge.build_onboarding_prompt("interview", {})
    assert "Never mention plans, tiers, 'Pro', upgrades or prices unless they ask" in prompt
    assert "Never narrate your own instructions" in prompt


def test_recommend_card_reads_compact_and_edits_behind_one_link():
    """Kris, run #10: "no edit/change"; re-run: "no way to expand a Note…
    still no Edit"; then: "it doesn't need to be a massive list… a small
    edit button". Compact wrapped lines by default (a note shows its label
    and full text), one Edit link turns them into boxes."""
    js = _read("src/app/static/concierge.js")
    card = js.split("function appendRecommendCard(rec)")[1].split("async function helpTurn")[0]
    assert "function readLine(v)" in card and "function editLine(v)" in card
    assert 'line.className = "text-xs text-slate-700 break-words whitespace-pre-wrap";' in card   # read: wrapped, never truncated
    assert 'editLink.textContent = editing ? "Done" : "Edit";' in card
    assert 'editLink.setAttribute("data-card-edit", "1");' in card
    assert "row.appendChild(ok); row.appendChild(editLink); row.appendChild(hint);" in card   # beside the apply button
    assert card.count("ok.remove(); editLink.remove();") == 2                              # gone once applied
    assert 'function recombine() { v.value = lab.value.trim() + " | " + body.value.trim(); }' in card
    assert 'box.addEventListener("input", function () { growBox(box); v.value = box.value; });' in card
    assert 'inp.addEventListener("input", function () { v.value = inp.value; });' in card


# ── the pages ────────────────────────────────────────────────────────────────
@pytest.fixture
def client():
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_sign_in_page_names_a_session_that_already_stands(client, monkeypatch):
    from app import auth, auth_routes

    async def fake_user(token):
        return {"email": "kris.deane93@gmail.com"} if token == "tok" else None
    monkeypatch.setattr(auth_routes.auth, "get_user", fake_user)
    r = client.get("/login")
    assert "data-signed-in-already" not in r.text
    client.cookies.set(auth.AT_COOKIE, "tok")
    r = client.get("/login")
    assert "data-signed-in-already" in r.text
    assert "Continue as kris.deane93@gmail.com" in r.text
    assert "Not you? Sign out first" in r.text


def test_interview_uses_owner_words_and_shows_the_seeded_name_as_a_placeholder(monkeypatch):
    from fastapi import Request

    def _ctx(request: Request):
        request.state.user_email = "business.blam8@gmail.com"
        return TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_tenant] = _ctx
    try:
        monkeypatch.setattr(onboarding.db, "get_tenant", lambda *a, **k: {"name": "Business Blam8", "industry": None})
        monkeypatch.setattr(onboarding.db, "get_tenant_config", lambda *a, **k: {})
        monkeypatch.setattr(onboarding, "_referral_state", lambda tid: None)
        r = TestClient(app).get("/onboarding/interview")
        assert r.status_code == 200
        assert 'placeholder="Business Blam8"' in r.text and 'value="Business Blam8"' not in r.text
        assert "Boat hire" not in r.text and "Tool hire" not in r.text     # hidden sub-sectors gone
        assert '<input id="industry"' in r.text and '<select id="industry"' not in r.text
        # a real name stays a value
        monkeypatch.setattr(onboarding.db, "get_tenant", lambda *a, **k: {"name": "Little Paws", "industry": "Care & support"})
        r = TestClient(app).get("/onboarding/interview")
        assert 'value="Little Paws"' in r.text
        assert 'value="Care &amp; support"' in r.text
    finally:
        app.dependency_overrides.clear()


def test_voice_step_folds_the_brief_and_labels_the_notes_box_as_style():
    html = _read("src/app/templates/onboarding/voice.html")
    assert "(from step 1 — open to edit)" in html
    assert 'name="business_brief"' in html                   # still posted, never blanked
    assert "style only" in html
    assert "belong in Knowledge, the next step" in html


def test_knowledge_and_mailbox_copy():
    k = _read("src/app/templates/onboarding/knowledge.html")
    assert "We read your website and the assistant asks for the rest" in k
    assert "Standard pallet" not in k
    m = _read("src/app/templates/onboarding/mailbox.html")
    assert "Connect with Microsoft" in m and 'class="flex items-center justify-center gap-2 rounded-lg bg-brand' not in m


def test_today_diary_tile_says_not_set_up_before_the_diary_exists():
    html = _read("src/app/templates/app/home.html")
    assert "data-diary-unset" in html and "Not set up yet →" in html
    assert "selectattr('key', 'equalto', 'diary')" in html


def test_next_waits_for_an_unsaved_notes_card_and_the_page_next_saves_it_first():
    """Kris, re-run 2026-09-13: "it shows the Next button before submission,
    so the info could be left out" — LittleDogs P1 again."""
    js = _read("src/app/static/concierge.js")
    assert "var PENDING_KB_CARDS = [];" in js
    hint = js.split("function showContinueHint()")[1].split("function pageContinueBtn")[0] \
        if js.find("function showContinueHint()") < js.find("function pageContinueBtn") \
        else js.split("function showContinueHint()")[1][:2000]
    assert "Save (or clear) the notes card above first" in hint
    assert "PENDING_KB_CARDS.push(pendingEntry);" in js
    assert "if (!PENDING_KB_CARDS.length && ADVANCE_WANTED) showContinueHint();" in js
    cap = js.split("var savingPending = false;")[1].split("// Restore open state")[0]
    assert 'e.target.closest("[data-onb-continue]")' in cap and "e.preventDefault(); e.stopImmediatePropagation();" in cap
    assert "if (!PENDING_KB_CARDS.length) btn.click();" in cap


def test_knowledge_source_rows_open_to_show_their_text():
    html = _read("src/app/templates/onboarding/knowledge.html")
    assert "<details class=\"group px-3 py-2 text-sm\" data-kb-note>" in html
    assert "(source_texts or {}).get(s.source)" in html
    assert "text_by_source = {t[\"source\"]: (t.get(\"text\") or \"\") for t in texts if t.get(\"source\")}" in _read("src/app/onboarding.py")


def test_stay_deposit_is_an_amount_never_a_percentage():
    """Re-run 2026-09-13: the held 25% deposit was written into the diary's
    stay_deposit as 25 — a £25 deposit. LittleDogs finding 5 again."""
    assert "Never enter a percentage here" in sr.PAGES["diary_settings"]["fields"]["stay_deposit"]
    assert "default_deposit_percent" in sr.PAGES["diary_settings"]["fields"]["stay_deposit"]



def test_the_next_step_bubble_reads_as_a_hand_off_on_the_page_just_saved():
    """Kris, re-run 2026-09-13, on Dependants just saved: "Assistant message
    says step 8-9 but we're still seeing 7?" — the strip says where they ARE,
    so the bubble says "saved ✓ … Next, step 8 of 9", and the panel footer
    mirrors the strip's "7 of 9 done"."""
    js = _read("src/app/static/concierge.js")
    greet = js.split("function maybeSetupGreet()")[1].split("function setupChip")[0]
    assert "if (s.done && s.href === window.location.pathname) savedHere = s;" in greet
    assert 'msg.replace(/^(Now|This is) (step \\d+ of \\d+)/, "Next, $2")' in greet
    assert "saved \\u2713 \\u2014 that's " in greet
    html = _read("src/app/templates/app/base.html")
    assert ".f-recall-wrap[hidden] { display: none !important; }" in html


def test_crm_stage_rows_are_one_quiet_line_with_an_x_to_remove():
    """Kris, re-run 2026-09-13 on CRM settings: "doesn't look so sleek…
    the Remove buttons could just be an X" — the same × the diary's
    service rows use, with the confirm kept."""
    html = _read("src/app/templates/app/crm_settings.html")
    assert 'aria-label="Remove {{ s.name }}" title="Remove">&times;</button>' in html
    assert "border-rose-300" not in html                       # no red Remove pills
    assert "return confirm('Remove {{ s.name }}? Deals on it move to Unstaged" in html   # still asks first
    assert '<div class="divide-y divide-slate-100 -mt-2">' in html
    assert 'name="display_order" type="number" value="{{ s.display_order }}" aria-label="Order" class="w-16' in html


def _steps(**done):
    keys = [("diary", "/app/diary/settings"), ("requirements", "/app/diary/intake"),
            ("dependants", "/app/diary/dependants"), ("crm", "/app/crm/settings"),
            ("invoicing", "/app/invoices/settings")]
    return [{"key": k, "href": h, "done": bool(done.get(k))} for k, h in keys]


def test_an_arrival_ask_keeps_only_cards_for_the_page_it_is_on():
    """Re-run 2026-09-13, step 9: the Invoice step's silent ask re-offered
    diary, requirements, dependants, CRM and four saved knowledge notes."""
    from app import setup_handoff
    recs = [{"page": "diary_settings", "values": [{"name": "open_time", "value": "07:00"}]},
            {"page": "booking_details", "values": [{"name": "field", "value": "Service type | required"}]},
            {"page": "dependants", "values": [{"name": "group", "value": "Dog | Dogs | several"}]},
            {"page": "crm_settings", "values": [{"name": "stage", "value": "New enquiry | active"}]},
            {"page": "knowledge", "values": [{"name": "note", "value": "Refusal policy | We turn a dog away…"}]},
            {"page": "packages", "values": [{"name": "package", "value": "10 days | 300"}]}]
    steps = _steps(diary=True, requirements=True, dependants=True, crm=True)
    # arriving on the invoice page: only what lives on that page (packages)
    kept = setup_handoff.cards_for(recs, steps, "/app/invoices/settings?x=1", auto=True)
    assert [r["page"] for r in kept] == ["packages"]
    # arriving on the CRM page: its own card even though the step is done
    assert [r["page"] for r in setup_handoff.cards_for(recs, steps, "/app/crm/settings", auto=True)] == ["crm_settings"]
    # a typed question mid-walk: its own page, undone steps, stepless pages
    steps2 = _steps(diary=True, requirements=True)
    kept2 = setup_handoff.cards_for(recs, steps2, "/app/diary/settings", auto=False)
    assert [r["page"] for r in kept2] == ["diary_settings", "dependants", "crm_settings", "knowledge", "packages"]
    # past the walk (every step done) nothing is filtered
    all_done = _steps(diary=True, requirements=True, dependants=True, crm=True, invoicing=True)
    assert setup_handoff.cards_for(recs, all_done, "/app/", auto=True) == recs
    assert setup_handoff.cards_for(recs, [], "/app/", auto=True) == recs
    assert setup_handoff.cards_for(None, steps, "/app/", auto=True) == []


def test_the_help_route_and_widget_carry_path_and_auto():
    py = _read("src/app/concierge_routes.py")
    assert "path: str | None = Field(default=None, max_length=200)" in py and "auto: bool = False" in py
    assert "setup_handoff.cards_for(recs, setup_handoff.load_steps(ctx.tenant_id, ctx.client)," in py
    assert "on_pages=setup_held.pages_for_path(body.path)" in py
    js = _read("src/app/static/concierge.js")
    assert "path: window.location.pathname, auto: !!auto" in js
    node = shutil.which("node")
    assert node, "Node.js is required to replay the Help widget"
    subprocess.run([node, "tests/replays/help_autoask_replay.mjs"], cwd=ROOT,
                   check=True, capture_output=True, text=True, timeout=15)


def test_the_invoice_step_never_promises_a_card_it_cannot_carry():
    """Re-run 2026-09-13, step 9: the arrival reply ended "I've pre-filled what
    I can on the card below — press it" with no card (invoice details are
    typed by hand; the held values were already in the form)."""
    # Since run log F6 (2026-09-26) Getting paid HAS a card for what the owner
    # told us; the promise rule still holds for any reply without one.
    prompt = concierge.build_help_prompt(facts="", knowledge="")
    assert "invoice_settings/payment_terms" in prompt
    assert "The owner can open the settings page to review and change values" in prompt
    assert "No durable proposal context" in prompt and "claim anything was saved" in prompt
