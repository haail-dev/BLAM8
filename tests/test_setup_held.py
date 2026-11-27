"""Held for later (src/app/setup_held.py) — facts an owner gives at one setup
step that belong to another page, kept and prefilled there.

Kris, kennels run #10 (2026-09-13): "info for a future step has been
presented & picked up on, it should be held until we reach that step, then
it's the first thing in there waiting… could just be prefilled." Pins:

- clean_many: only real pages and real field names survive (the recommend
  catalogue's own cleaner for its pages; a whitelist for Voice / Invoice
  details);
- merge: a single-value field keeps the owner's LATEST statement, repeatable
  rows accumulate without duplicates, used items are left alone;
- add: records against the tenant and returns the page LABELS touched — the
  only source of the widget's "Kept for later" line;
- for_page / mark_used: a page reads its unused items; saving stamps them;
- prompt_block: the facts, page by page, told to the model verbatim;
- the schemas: both assistant replies carry `held`; the onboarding prompt
  carries the rule, the catalogue and what is already held; the voice step
  no longer files rules in the voice box.

Run: python -m pytest tests/test_setup_held.py -q
"""
import pytest
from fastapi.testclient import TestClient

from app import concierge, db, setup_held
from app.auth import TenantCtx, require_tenant
from app.main import app


# ── validation ───────────────────────────────────────────────────────────────
def test_clean_many_keeps_only_real_pages_and_fields():
    out = setup_held.clean_many([
        {"page": "diary_settings", "values": [{"name": "capacity_per_window", "value": "30"},
                                              {"name": "min_notice_hours", "value": "10"},
                                              {"name": "made_up", "value": "x"}]},
        {"page": "invoice_settings", "values": [{"name": "default_deposit_percent", "value": " 25 "},
                                                {"name": "bank_account_number", "value": "1234"}]},
        {"page": "voice", "values": [{"name": "sign_off", "value": "Kristie at Little Paws"}]},
        {"page": "nonsense", "values": [{"name": "x", "value": "y"}]},
        {"page": "knowledge", "values": []},
        "junk",
    ])
    assert [e["page"] for e in out] == ["diary_settings", "invoice_settings", "voice"]
    assert out[0]["values"] == [{"name": "capacity_per_window", "value": "30"},
                                {"name": "min_notice_hours", "value": "10"}]
    assert out[1]["values"] == [{"name": "default_deposit_percent", "value": "25"}]   # bank details never held
    assert out[2]["values"] == [{"name": "sign_off", "value": "Kristie at Little Paws"}]


def test_schema_is_plain_and_names_every_page():
    sch = setup_held.schema()
    assert sch["items"]["properties"]["page"]["enum"] == setup_held.page_keys()
    assert "voice" in setup_held.page_keys() and "invoice_settings" in setup_held.page_keys()
    assert "diary_settings" in setup_held.page_keys() and "knowledge" in setup_held.page_keys()
    flat = str(sch)
    assert "minItems" not in flat and "maxItems" not in flat      # structured output rejects these


# ── merge semantics ──────────────────────────────────────────────────────────
def test_merge_latest_statement_wins_for_a_single_value_and_rows_accumulate():
    items = setup_held.merge([], "diary_settings",
                             [{"name": "capacity_per_window", "value": "15"}], "interview")
    items = setup_held.merge(items, "diary_settings",
                             [{"name": "capacity_per_window", "value": "30"},
                              {"name": "service", "value": "Trial night | 60 | 30.50 | time"}], "voice")
    items = setup_held.merge(items, "diary_settings",
                             [{"name": "service", "value": "Trial night | 60 | 30.50 | time"},
                              {"name": "service", "value": "Meet & greet | 30 | 0 | time"}], "knowledge")
    caps = [i for i in items if i["name"] == "capacity_per_window"]
    assert len(caps) == 1 and caps[0]["value"] == "30" and caps[0]["step"] == "voice"
    rows = [i["value"] for i in items if i["name"] == "service"]
    assert rows == ["Trial night | 60 | 30.50 | time", "Meet & greet | 30 | 0 | time"]


def test_service_price_correction_survives_reload_and_keeps_other_services(store):
    def hold(*offers):
        setup_held.add("t1", [{"page": "diary_settings", "values": [
            {"name": "service", "value": offer} for offer in offers
        ]}], "interview")

    full = "Full-day coverage | 480 | 1200 | time"
    original = "Half-day coverage | 240 | 700 | time"
    corrected = "Half-day coverage | 240 | 750 | time"
    hold(full, original)
    previous = setup_held.load("t1")[-1]
    hold(corrected)
    items = setup_held.load("t1")
    assert [item["value"] for item in items] == [full, corrected]
    legacy = [previous, *items]
    assert original not in setup_held.prompt_block(legacy)
    assert [v["value"] for v in setup_held.for_page(legacy, "diary_settings")] == [full, corrected]


def test_merge_leaves_used_items_alone_and_a_restatement_is_new():
    items = [{"page": "diary_settings", "name": "min_notice_hours", "value": "10",
              "step": "interview", "at": "t", "used_at": "t2"}]
    items = setup_held.merge(items, "diary_settings",
                             [{"name": "min_notice_hours", "value": "12"}], "help")
    assert [(i["value"], bool(i.get("used_at"))) for i in items] == [("10", True), ("12", False)]
    assert setup_held.for_page(items, "diary_settings") == [{"name": "min_notice_hours", "value": "12"}]


# ── the record ───────────────────────────────────────────────────────────────
@pytest.fixture
def store(monkeypatch):
    rows = {"t1": {"setup_held": []}}
    monkeypatch.setattr(db, "get_tenant_config", lambda tid, client=None: dict(rows.get(tid) or {}))
    monkeypatch.setattr(db, "upsert_tenant_config",
                        lambda tid, cfg, client=None: rows.setdefault(tid, {}).update(cfg))
    return rows


def test_add_records_and_returns_page_labels(store):
    labels = setup_held.add("t1", [
        {"page": "diary_settings", "values": [{"name": "capacity_per_window", "value": "30"}]},
        {"page": "invoice_settings", "values": [{"name": "default_deposit_percent", "value": "25"}]},
        {"page": "bogus", "values": [{"name": "x", "value": "y"}]},
    ], step="interview")
    assert labels == ["Diary settings: capacity per window 30",
                      "Getting paid: default deposit percent 25"]   # names what was kept
    held = store["t1"]["setup_held"]
    assert {(i["page"], i["name"], i["value"], i["step"]) for i in held} == {
        ("diary_settings", "capacity_per_window", "30", "interview"),
        ("invoice_settings", "default_deposit_percent", "25", "interview")}
    assert setup_held.add("t1", [], step="voice") == []          # nothing held → no claim


def test_mark_used_stamps_the_pages_items_only(store):
    setup_held.add("t1", [
        {"page": "diary_settings", "values": [{"name": "capacity_per_window", "value": "30"}]},
        {"page": "knowledge", "values": [{"name": "note", "value": "Trial day | Every new dog has a trial day first."}]},
    ], step="voice")
    assert setup_held.mark_used("t1", "diary_settings") == 1
    held = setup_held.load("t1")
    assert setup_held.for_page(held, "diary_settings") == []
    assert setup_held.for_page(held, "knowledge") == [
        {"name": "note", "value": "Trial day | Every new dog has a trial day first."}]


def test_load_and_add_never_raise(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "get_tenant_config", boom)
    monkeypatch.setattr(db, "upsert_tenant_config", boom)
    assert setup_held.load("t1") == []
    assert setup_held.add("t1", [{"page": "voice", "values": [{"name": "tone", "value": "warm"}]}],
                          step="interview") == []


# ── the prompts ──────────────────────────────────────────────────────────────
def test_prompt_block_tells_the_model_to_use_held_facts_verbatim():
    items = [{"page": "diary_settings", "name": "capacity_per_window", "value": "30", "step": "interview", "at": "t"},
             {"page": "diary_settings", "name": "min_notice_hours", "value": "10", "step": "interview", "at": "t"},
             {"page": "invoice_settings", "name": "default_deposit_percent", "value": "25", "step": "voice", "at": "t"},
             {"page": "voice", "name": "sign_off", "value": "K", "step": "interview", "at": "t", "used_at": "t2"}]
    block = setup_held.prompt_block(items)
    assert "VERBATIM" in block and "never ask for them again" in block
    assert 'page "diary_settings" (Diary settings): capacity_per_window = 30; min_notice_hours = 10' in block
    assert 'page "invoice_settings" (Getting paid): default_deposit_percent = 25' in block
    assert "sign_off" not in block                                  # used items are done
    assert setup_held.prompt_block([]) == ""


def test_onboarding_reply_schema_and_prompt_carry_held():
    for step in ("interview", "voice", "knowledge", "mailbox"):
        sch = concierge.onboarding_schema(step)
        assert "held" in sch["properties"] and "held" in sch["required"]
    prompt = concierge.build_onboarding_prompt(
        "interview", {}, held=[{"page": "diary_settings", "name": "capacity_per_window",
                               "value": "30", "step": "interview", "at": "t"}])
    assert "HELD FOR LATER" in prompt
    assert 'page "diary_settings"' in prompt and "capacity_per_window" in prompt   # the catalogue
    assert 'page "invoice_settings"' in prompt                                       # the extras
    assert "capacity_per_window = 30" in prompt                                      # what is already held


def test_voice_step_holds_rules_for_the_knowledge_base_instead_of_filing_them_as_voice():
    g = concierge.ONBOARDING_STEPS["voice"]["guidance"]
    assert "hold it for the knowledge base" in g and "page knowledge" in g
    assert "voice_notes is only for how the writing sounds" in g
    assert "Don't ask for standing rules" in g
    assert "Is there anything it should always say" not in g


def test_help_schema_and_reply_carry_held():
    assert "held" in concierge.HELP_SCHEMA["properties"]
    assert "held" in concierge.HELP_SCHEMA["required"]
    assert concierge._help_held('{"held": [{"page": "invoice_settings", "values": '
                                '[{"name": "default_deposit_percent", "value": "25"}]}]}') == [
        {"page": "invoice_settings", "values": [{"name": "default_deposit_percent", "value": "25"}]}]
    assert concierge._help_held("not json") == []
    prompt = concierge.build_help_prompt(facts="", knowledge="")
    assert "No durable proposal context" in prompt and "Do not emit recommend or held settings" in prompt


# ── the routes record what the model held ────────────────────────────────────
@pytest.fixture
def client():
    app.dependency_overrides[require_tenant] = lambda: TenantCtx(tenant_id="t1", client=None)
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_onboard_route_records_held_and_names_the_pages(client, store, monkeypatch):
    from app import concierge_routes as routes
    seen = {}

    async def fake_answer(step, message, history=None, current=None, tenant_id=None,
                          coverage=None, tenant=None, held=None):
        seen["held_before"] = held
        return concierge.OnboardingReply(
            reply="Kept the capacity for your diary.", form_fill={}, ready_to_advance=False,
            usage=None, model="m", notes=[],
            held=[{"page": "diary_settings", "values": [{"name": "capacity_per_window", "value": "30"}]}])
    monkeypatch.setattr(routes.concierge, "answer_onboarding", fake_answer)
    monkeypatch.setattr(routes.fairuse, "onboarding_allowed", lambda *a, **k: True)
    monkeypatch.setattr(routes.usage, "record_anthropic", lambda *a, **k: None)
    r = client.post("/concierge/onboard", json={"step": "interview", "question": "30 dogs at once"})
    assert r.status_code == 200
    assert r.json()["held_for"] == ["Diary settings: capacity per window 30"]
    assert seen["held_before"] == []
    assert store["t1"]["setup_held"][0]["value"] == "30"
    # the next turn sees it
    client.post("/concierge/onboard", json={"step": "voice", "question": "hi"})
    assert seen["held_before"][0]["name"] == "capacity_per_window"


def test_describe_names_a_held_value_in_a_few_words():
    assert setup_held.describe("dependants", "field",
                               "Dog | Vaccinations | required | expires 90 | gate | must be current") == "Vaccinations (Dog)"
    assert setup_held.describe("dependants", "group", "Dog | Dogs | several") == "Dog"
    assert setup_held.describe("knowledge", "note", "Trial day | Every new dog has one first.") == "Trial day"
    assert setup_held.describe("diary_settings", "capacity_per_window", "30") == "capacity per window 30"
    assert setup_held.describe("invoice_settings", "payment_terms",
                               "a 25% deposit, the balance by bank transfer on collection") == "payment terms"


def test_describe_says_a_stored_code_in_words():
    """First-sign-up film, 30 Sep 2026: the "Kept for later" line read "working
    days mon,tue,wed,thu,fri,sat", "Emergency" (a bare word) and "show payment
    terms 1". One vocabulary with the recommendation card."""
    assert setup_held.describe("diary_settings", "working_days", "mon,tue,wed,thu,fri,sat") == "working days Monday to Saturday"
    assert setup_held.describe("diary_settings", "windows", "ampm") == "morning and afternoon windows"
    assert setup_held.describe("diary_settings", "recurring_enabled", "1") == "repeat bookings yes"
    assert setup_held.describe("diary_settings", "service", "Emergency | 60 | 85 | half") == "service Emergency"
    assert setup_held.describe("diary_settings", "service_out_of_hours", "Emergency visit | evenings | | yes | 140 | 65") == "out of hours Emergency visit"
    assert setup_held.describe("invoice_settings", "show_payment_terms", "1") == "payment terms on invoices yes"
    assert setup_held.describe("diary_settings", "open_time", "08:00") == "open time 08:00"   # no code: plain form


def test_widget_writes_the_kept_line_only_from_the_servers_held_for():
    from pathlib import Path
    js = Path("src/app/static/concierge.js").read_text(encoding="utf-8")
    assert "function appendHeldLine(receipts)" in js
    assert 'p.textContent = "✓ Kept for later — " + receipts.join(" · ")' in js
    assert "heldExplained = true;" in js                                # the explainer rides once
    assert js.count("appendHeldLine(heldFor)") == 2                 # onboarding + help turns
    assert 'heldFor = Array.isArray(data.held_for) ? data.held_for : [];' in js


def test_a_fact_for_a_step_behind_the_customer_is_never_held(store):
    """Re-run 2026-09-13: on Knowledge the panel said "Kept for later — Voice"
    for a step already done."""
    voice = [{"page": "voice", "values": [{"name": "sign_off", "value": "Kristie at Little Paws"}]}]
    diary = [{"page": "diary_settings", "values": [{"name": "capacity_per_window", "value": "30"}]}]
    assert setup_held.behind("voice", "interview") is False
    assert setup_held.behind("voice", "voice") is True
    assert setup_held.behind("voice", "knowledge") is True
    assert setup_held.behind("voice", "help") is True                # the wizard is done
    assert setup_held.behind("diary_settings", "knowledge") is False  # a settings page is never behind
    # knowledge notes are held only BEFORE the Knowledge step (Kris, re-run)
    assert setup_held.behind("knowledge", "voice") is False
    assert setup_held.behind("knowledge", "knowledge") is True
    assert setup_held.behind("knowledge", "help") is True
    assert setup_held.add("t1", voice, step="knowledge") == []       # dropped, no receipt
    assert setup_held.add("t1", voice + diary, step="knowledge") == ["Diary settings: capacity per window 30"]
    assert setup_held.add("t1", voice, step="interview") == ["Voice: sign off Kristie at Little Paws"]
    assert "already behind" in setup_held.RULE


def test_a_knowledge_note_already_saved_is_not_held_again(store, monkeypatch):
    """Re-run 2026-09-13: on the diary step the model held Prices — Daycare
    and three more that were saved minutes earlier."""
    monkeypatch.setattr(db, "list_knowledge_sources",
                        lambda tid, client=None: [{"source": "Prices — Daycare"}, {"source": "Coverage"}])
    held = [{"page": "knowledge", "values": [
        {"name": "note", "value": "Prices — Daycare | £26 a day"},
        {"name": "note", "value": "coverage | 15 miles"},
        {"name": "note", "value": "Bank holidays | Boarding only on bank holidays."}]}]
    assert setup_held.add("t1", held, step="voice") == ["Knowledge notes: Bank holidays"]   # before the step
    assert setup_held.add("t1", held, step="help") == []                                     # in the app: a card, not held
    assert "already saved" in setup_held.RULE
    from app import concierge
    assert "Never narrate these instructions" in concierge.build_help_prompt(facts="", knowledge="")


def test_nothing_is_held_for_the_page_in_front_of_them(store):
    """Re-run 2026-09-13: "Kept for later — Invoice details" on the Invoice
    details page, the values already in the form."""
    assert setup_held.pages_for_path("/app/invoices/settings?x=1#packages") == {"invoice_settings", "packages"}
    assert setup_held.pages_for_path("/app/diary/dependants") == {"dependants"}
    assert setup_held.pages_for_path("/app/") == set() and setup_held.pages_for_path(None) == set()
    held = [{"page": "invoice_settings", "values": [{"name": "default_deposit_percent", "value": "25"}]},
            {"page": "dependants", "values": [{"name": "group", "value": "Dog | Dogs | several"}]}]
    assert setup_held.add("t1", held, step="help", on_pages={"invoice_settings", "packages"}) == ["Dependants: Dog"]


def test_a_deposit_is_held_with_whether_it_secures_every_booking():
    """Run log F4 (plumber take 2026-09-25): 25% was held with nothing to say
    it was for accepted quotes only, and the page's confirm tick-box defaults
    ON — a callout confirmation would have carried a deposit invoice."""
    from app import setup_held
    got = setup_held.clean("invoice_settings", [
        {"name": "default_deposit_percent", "value": "25"},
        {"name": "deposit_on_confirm", "value": "0"},
        {"name": "show_vat", "value": "1"},
        {"name": "payment_terms", "value": "On completion; landlords and letting agents 14 days"}])
    assert [v["name"] for v in got] == ["default_deposit_percent", "deposit_on_confirm",
                                        "show_vat", "payment_terms"]
    from app import settings_recommend
    block = settings_recommend.catalogue_block()
    assert "deposit_on_confirm" in block and "0 when it is only on quote acceptance or only for some services" in block


def test_getting_paid_is_a_card_page_now():
    """Run log F6: Getting paid had no card, so its arrival ask wrote an 18.9 s
    essay and called the default terms the owner's own."""
    from pathlib import Path
    from app import settings_recommend, setup_handoff
    assert "invoice_settings" in settings_recommend.PAGES
    vals = settings_recommend.clean("invoice_settings", [
        {"name": "payment_terms", "value": "On completion; landlords and agents 14 days"},
        {"name": "deposit_on_confirm", "value": "0"},
        {"name": "bank_account_number", "value": "12345678"}])      # never on the card
    assert [v["name"] for v in vals] == ["payment_terms", "deposit_on_confirm"]
    steps = [{"key": "invoicing", "href": "/app/invoices/settings", "done": False}]
    recs = [{"page": "invoice_settings", "values": vals}]
    assert setup_handoff.cards_for(recs, steps, "/app/invoices/settings", auto=True) == recs
    page = Path("src/app/templates/app/invoice_settings.html").read_text(encoding="utf-8")
    assert "put a recommended card in your FIRST reply" in page and "Explain this Getting paid" not in page


def test_a_knowledge_answer_that_is_also_a_setting_is_held_as_both():
    """Run log F3: the plumber's "how a first booking works" answer was saved
    as a note only — the checklist, the survey service and the terms were each
    asked for again later."""
    from app import setup_held
    r = setup_held.RULE
    assert "save the note AND hold the setting it fixes" in r
    assert "booking_details field rows" in r and "diary_settings service row" in r
    assert "only its stated duration, price and timing" in r
    assert "whether it applies on confirmation (deposit_on_confirm) or only on quote acceptance" in r and "never a setting they didn't state" in r
