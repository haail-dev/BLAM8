"""Run #9 (Brightwell Home Cleaning, 2026-09-05) — the onboarding P3s, pinned.

Copy and defaults an owner tripped over while the mechanics worked: the voice
step never asked for standing rules; the Gmail button said Workspace; "hit
Continue" on a step whose button says Finish setup; a diary summary that
under-read capacity by three; requirement rows made required that the owner
settles at a look-round; a hint inviting alarm codes by email; a CRM prose
promising a Save the card doesn't have; the invoice walkthrough skipping the
one field the KB answers; a chip posting words in the owner's mouth; a stage
named in the assistant's words; the oven clean lost; credit knobs on a business
with no wallet; "collected once" claimed for a per-booking row; a next step
offered after it was done; a mirrored card changing hours in silence; and a
cleaning company handed a plumber's recipe.
Run: python -m pytest tests/test_run9_onboarding_p3.py -q
"""
from tests.booking_fixtures import carried_config
from pathlib import Path

from app import concierge, scheduling, settings_recommend
from app import settings_guide as sg

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "src/app" / "static" / "concierge.js").read_text(encoding="utf-8")


def _tpl(*parts):
    return (ROOT / "src/app" / "templates").joinpath(*parts).read_text(encoding="utf-8")


def test_15_16_the_voice_step_offers_continue_at_once_and_holds_rules_for_knowledge():
    # 2026-09-13 (kennels run #10, Kris): a rule is not a voice — the voice
    # step no longer asks the standing-rules question; a rule the owner states
    # is held for the Knowledge step (src/app/setup_held.py).
    g = concierge.ONBOARDING_STEPS["voice"]["guidance"]
    assert "set ready_to_advance TRUE in that same reply" in g
    assert "Is there anything it should always say, or never?" not in g
    assert "hold it for the knowledge base" in g


def test_17_the_gmail_button_does_not_read_as_workspace_only():
    # run #10 (Kris): the qualifiers themselves confused — two clean buttons
    # that mirror the sign-in page, the which-is-mine help on one line below
    html = _tpl("onboarding", "mailbox.html")
    assert "Connect Gmail (Google Workspace)" not in html
    assert "personal or Google Workspace" not in html and "(Microsoft 365)" not in html
    assert "Connect with Google" in html and "Connect with Microsoft" in html
    assert "Gmail and Google Workspace addresses are Google" in html


def test_18_the_last_step_says_next_into_the_app_not_finish():
    assert "Guided setup opens after the owner approves the sample reply" in concierge.ONBOARDING_STEPS["knowledge"]["guidance"]
    prompt = concierge.build_onboarding_prompt("knowledge", {})
    assert "Continue, or Next on the last step" in prompt


def test_20_the_diary_summary_reads_capacity_as_concurrency():
    line = scheduling.diary_line(carried_config({"capacity_per_window": 3, "nouns": {"booking": "clean"},
                                  "buffer_minutes": 30}))
    assert line.startswith("Up to 3 cleans at the same time;")
    assert "per day" not in line and "30-minute gap" in line


def test_21_22_29_the_requirements_catalogue_defaults_and_forbids():
    d = settings_recommend.PAGES["booking_details"]["fields"]["field"]
    assert "REQUIRED only for what the agent cannot book without" in d
    assert "preferences or other non-blocking details) is OPTIONAL" in d
    assert "Never a row that asks for an alarm code, key-safe number or password" in d
    # 2026-09-21: a row CAN be remembered now (app/intake_memory) — the rule is
    # to say which rows are, not to deny it.
    assert "say plainly in the reply which rows are remembered and which are asked each visit" in d
    assert "collected once" not in d


def test_23_and_the_silent_change_rule_in_help_rule_9():
    prompt = concierge.build_help_prompt(facts="", knowledge="")
    assert "No durable proposal context" in prompt and "claim anything was saved" in prompt
    assert "Preserve existing choices unless" in prompt


def test_24_the_invoice_ask_covers_quote_defaults_from_the_kb():
    html = _tpl("app", "invoice_settings.html")
    # A card since run log F6: the same defaults, from what the owner said.
    assert "deposit (and whether it is asked when a booking is confirmed), quote validity" in html
    assert "only what I have said, nothing guessed" in html


def test_25_a_page_chip_asks_silently_never_as_the_owner():
    handler = JS.split('e.target.closest("[data-concierge-ask]")')[1].split("// ── Recommended-settings prefill")[0]
    assert "askSilently(text); return;" in handler
    assert "!ONB_MODE" in handler          # the wizard's own panel keeps its send path


def test_26_27_28_the_catalogue_speaks_the_owners_words_and_lists_every_service():
    diary = settings_recommend.PAGES["diary_settings"]["fields"]
    assert "EVERY service the owner names or prices" in diary["service"]
    assert "omit for pay-per-booking" in diary["no_show_spends"]
    assert "omit for pay-per-booking" in diary["excused_refunds"]
    assert "CUSTOMER'S OWN WORDS" in settings_recommend.PAGES["crm_settings"]["fields"]["stage"]


def test_the_customer_block_carries_the_walk_through_position():
    steps = [{"key": "diary", "label": "Diary & scheduling settings", "done": True},
             {"key": "dependants", "label": "Dependants", "done": False},
             {"key": "crm", "label": "CRM settings", "done": True},
             {"key": "invoicing", "label": "Getting paid", "done": False}]
    block = concierge._customer_block({"name": "B", "handoff": steps, "setup": {}})
    assert "Settings walk-through: done — Diary & scheduling settings, CRM settings; NEXT — Dependants (then Getting paid)" in block
    assert "never offer one already listed as done" in block
    for s in steps:
        s["done"] = True
    block = concierge._customer_block({"name": "B", "handoff": steps, "setup": {}})
    assert "every step is done" in block and "never announce a next step" in block
    src = (ROOT / "src/app" / "concierge_routes.py").read_text(encoding="utf-8")
    assert '"handoff": handoff,' in src and "setup_handoff.steps(mods, cfg, stage_rows" in src


def test_30_a_cleaning_company_gets_the_domestic_rounds_recipe_not_a_plumbers():
    hits = sg.match_profiles("domestic cleaning", "Small domestic cleaning company in Chesterfield")
    assert hits and hits[0]["id"] == "domestic_rounds"
    assert sg.match_profiles("something else", "domestic cleaning of houses on a fortnightly round")[0]["id"] == "domestic_rounds"
    assert sg.match_profiles("gardener")[0]["id"] == "domestic_rounds"
    assert sg.match_profiles("trades")[0]["id"] == "field_service"          # plumbers keep theirs
    assert sg.match_profiles("plumber")[0]["id"] == "field_service"
    recipe = hits[0]["recipe"]
    assert "Recurring bookings: ON with weekly and fortnightly" in recipe
    assert "Never a row for an alarm code" in recipe and "Delivery & collection: off" in recipe


def test_24b_the_help_agent_reads_the_owners_notes_not_just_their_titles():
    """Live after the P3 batch: asked for payment terms, the assistant said "your
    notes mention payment but I can't see the exact terms" — it had only the
    source names. The customer block now carries each note's opening text."""
    block = concierge._customer_block({"name": "B", "setup": {
        "knowledge_texts": [{"source": "Payment", "text": "Monthly invoice on the 1st.\nPay within 7 days. "
                                                          "Deep cleans over 150 take a 50% deposit."}]}})
    assert "Their knowledge notes, in their words" in block
    assert "· Payment: Monthly invoice on the 1st. Pay within 7 days. Deep cleans over 150 take a 50% deposit." in block
    assert "quote the number they wrote" in block
    src = (ROOT / "src/app" / "concierge_routes.py").read_text(encoding="utf-8")
    assert '"knowledge_texts": knowledge_texts,' in src and '[:400]' in src
