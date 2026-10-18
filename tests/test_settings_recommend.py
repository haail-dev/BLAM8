"""Concierge settings recommendations — "Help me set this up" → "Use recommended
settings" (src/app/settings_recommend.py, Julius 2026-07-27).

What this pins:
  - clean(): whitelisted field names only, repeatables kept, scalars deduped,
    values trimmed/capped, unknown page → None (no card)
  - the CRM pipe-format parsers (stages + quiet_days)
  - HELP_SCHEMA carries `recommend` and _parse_help validates it server-side
  - build_help_prompt injects the settings catalogue + the recommend rule
  - POST /concierge/crm-apply: adds only MISSING stages (case-insensitive),
    sets the quiet threshold, 404s without the crm module, 400s on nothing

Run: python -m pytest tests/test_settings_recommend.py -q
"""
import json

import pytest
from fastapi.testclient import TestClient

import app.concierge_routes as concierge_routes
from app import concierge, settings_recommend
from app.auth import TenantCtx, require_tenant
from app.main import app
from app.security import require_csrf_header


# ── clean(): the server-side whitelist ────────────────────────────────────────
def test_clean_keeps_known_fields_and_drops_unknown():
    out = settings_recommend.clean("diary_settings", [
        {"name": "open_time", "value": "08:00"},
        {"name": "made_up_field", "value": "x"},           # not a real form field
        {"name": "service", "value": "Full groom | 90 | 45"},
        {"name": "service", "value": "Puppy trim | 45 | 25"},   # repeatable — both kept
        {"name": "open_time", "value": "09:00"},           # scalar dupe — first wins
        {"name": "dayhours_sat_open", "value": "09:00"},   # per-day override, regex-matched
        {"name": "dayhours_xyz_open", "value": "09:00"},   # bad day code — dropped
    ])
    assert [v["name"] for v in out] == ["open_time", "service", "service", "dayhours_sat_open"]
    assert out[0]["value"] == "08:00"


def test_clean_unknown_page_or_empty_returns_none():
    assert settings_recommend.clean("not_a_page", [{"name": "open_time", "value": "08:00"}]) is None
    assert settings_recommend.clean("diary_settings", []) is None
    assert settings_recommend.clean("", [{"name": "open_time", "value": "08:00"}]) is None
    # only invalid entries → None, so the widget never renders an empty card
    assert settings_recommend.clean("crm_settings", [{"name": "nope", "value": "1"}]) is None


def test_clean_caps_value_length_and_entry_count():
    vals = [{"name": "field", "value": "Label %d | required" % i} for i in range(60)]
    out = settings_recommend.clean("booking_details", vals)
    assert len(out) == settings_recommend.MAX_ENTRIES
    row400 = "L | optional | " + "a" * (400 - 15)
    kept = settings_recommend.clean("booking_details", [{"name": "field", "value": row400}])
    assert kept == [{"name": "field", "value": row400}]
    assert settings_recommend.clean("booking_details",
                                    [{"name": "field", "value": "x" * 500}]) is None
    assert settings_recommend.clean(
        "booking_details",
        [{"name": "stay_mode", "value": "1" * (settings_recommend.MAX_VALUE + 1)}]) is None


# ── the CRM pipe-format parsers ───────────────────────────────────────────────
def test_parse_stages_types_dupes_and_order():
    stages = settings_recommend.parse_stages([
        {"name": "stage", "value": "New enquiry | active"},
        {"name": "stage", "value": "Quoted"},                    # no type → active
        {"name": "stage", "value": "Won | won"},
        {"name": "stage", "value": "won | WON"},                 # dupe (case) — dropped
        {"name": "stage", "value": "Lost | bogus_type"},         # bad type → active
        {"name": "quiet_days", "value": "30"},                   # not a stage
        {"name": "stage", "value": ""},                          # blank — dropped
    ])
    assert [(s["name"], s["stage_type"]) for s in stages] == [
        ("New enquiry", "active"), ("Quoted", "active"), ("Won", "won"), ("Lost", "active")]


def test_parse_quiet_days_clamps_and_rejects_junk():
    assert settings_recommend.parse_quiet_days([{"name": "quiet_days", "value": "30"}]) == 30
    assert settings_recommend.parse_quiet_days([{"name": "quiet_days", "value": "9999"}]) == 365
    assert settings_recommend.parse_quiet_days([{"name": "quiet_days", "value": "soon"}]) is None
    assert settings_recommend.parse_quiet_days([]) is None


# ── the model contract: schema + parse ────────────────────────────────────────
def test_help_schema_carries_recommend():
    # A LIST since 2026-09-02 — the single-object slot silently dropped the
    # second page of a two-page recommendation ("press each card", one card).
    props = concierge.HELP_SCHEMA["properties"]
    assert "recommend" in props and "recommend" in concierge.HELP_SCHEMA["required"]
    assert props["recommend"]["type"] == "array"
    assert props["recommend"]["items"]["properties"]["page"]["enum"] == \
        list(settings_recommend.PAGE_KEYS)


def test_parse_help_extracts_valid_recommendations():
    text = json.dumps({"reply": "Here you go.", "nav": "", "submit": {"kind": "", "summary": ""},
                       "recommend": [
                           {"page": "booking_details", "values": [
                               {"name": "field", "value": "Pet name | required | e.g. Bella"},
                               {"name": "stay_mode", "value": "1"},
                               {"name": "invented", "value": "x"}]},
                           {"page": "diary_settings", "values": [
                               {"name": "open_time", "value": "08:00"}]},
                       ]})
    reply, nav, submit, recs = concierge._parse_help(text)
    assert [r["page"] for r in recs] == ["booking_details", "diary_settings"]
    rec = recs[0]
    assert rec["href"] == "/app/diary/intake" and rec["label"] == "Booking details"
    assert [v["name"] for v in rec["values"]] == ["field", "stay_mode"]


def test_parse_help_tolerates_the_old_single_object_shape():
    text = json.dumps({"reply": "r", "nav": "", "submit": {"kind": "", "summary": ""},
                       "recommend": {"page": "diary_settings", "values": [
                           {"name": "open_time", "value": "08:00"}]}})
    *_, recs = concierge._parse_help(text)
    assert [r["page"] for r in recs] == ["diary_settings"]


def test_parse_help_keeps_the_first_card_per_page():
    text = json.dumps({"reply": "r", "nav": "", "submit": {"kind": "", "summary": ""},
                       "recommend": [
                           {"page": "diary_settings", "values": [
                               {"name": "open_time", "value": "08:00"}]},
                           {"page": "diary_settings", "values": [
                               {"name": "open_time", "value": "09:00"}]}]})
    *_, recs = concierge._parse_help(text)
    assert len(recs) == 1 and recs[0]["values"][0]["value"] == "08:00"


def test_repeatable_fields_dedupe_on_their_label():
    # Live e2e 2026-09-02: the model listed "Departure / collection date &
    # time" twice and the checklist gained a duplicate row.
    values = settings_recommend.clean("booking_details", [
        {"name": "field", "value": "Departure / collection date & time | required | when"},
        {"name": "field", "value": "departure / collection date & time | required | again"},
        {"name": "field", "value": "Pet name | required | e.g. Bella"}])
    assert [v["value"].split("|")[0].strip() for v in values] == \
        ["Departure / collection date & time", "Pet name"]


def test_parse_help_rejects_bad_page_and_empty_values():
    base = {"reply": "r", "nav": "", "submit": {"kind": "", "summary": ""}}
    for rec in ({"page": "evil_page", "values": [{"name": "open_time", "value": "08:00"}]},
                {"page": "", "values": []},
                {"page": "diary_settings", "values": [{"name": "nope", "value": "1"}]}):
        *_, got = concierge._parse_help(json.dumps({**base, "recommend": rec}))
        assert got is None


def test_help_prompt_carries_catalogue_and_rule():
    p = concierge.build_help_prompt(tenant={"name": "T", "industry": "kennels"})
    assert "Settings catalogue" in p
    assert "Propose settings only from explicit owner answers" in p
    assert "booking_details" in p and "crm_settings" in p and "diary_settings" in p
    # Run #8: the credit knobs are the diary card's own, never deferred to the packages page.
    assert "diary_settings" in p and "packages" in p
    assert "never fill a default" in p
    # grounded in what's already configured
    p2 = concierge.build_help_prompt(tenant={
        "name": "T", "cfg": {"intake_fields": [{"label": "Pet name", "required": True}]},
        "crm": {"stages": [{"name": "Won", "stage_type": "won"}], "quiet_days": 21}})
    # 2026-09-21: each row says how it is kept (app/intake_memory) — the default
    # row is asked each visit; a remembered one reads "remembered once".
    assert "Pet name (required, asked each visit)" in p2
    assert "Won (won)" in p2 and "21 days" in p2


# ── POST /concierge/crm-apply ─────────────────────────────────────────────────
@pytest.fixture
def client():
    app.dependency_overrides[require_tenant] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf_header] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


class CrmEnv:
    def __init__(self, monkeypatch, modules={"crm"}, existing=()):
        self.inserted, self.deleted, self.tcfg = [], [], {}
        monkeypatch.setattr(concierge_routes.db, "active_modules", lambda *a, **k: set(modules))
        monkeypatch.setattr(concierge_routes.db, "crm_list_stages",
                            lambda *a, **k: [dict(s) for s in existing])
        monkeypatch.setattr(concierge_routes.db, "crm_insert_stage",
                            lambda tid, name, **kw: self.inserted.append({"name": name, **kw}))
        monkeypatch.setattr(concierge_routes.db, "crm_delete_stage",
                            lambda sid, tid, **kw: self.deleted.append(sid))
        monkeypatch.setattr(concierge_routes.db, "upsert_tenant_config",
                            lambda tid, patch, **kw: self.tcfg.update(patch))


def test_crm_apply_replaces_the_pipeline(client, monkeypatch):
    """Julius 2026-09-02: 'only apply what the AI suggests' — the recommended
    stages BECOME the pipeline. A matching stage is kept (never duplicated),
    anything not in the recommendation is removed, and the summary says where
    its deals went."""
    env = CrmEnv(monkeypatch, existing=[
        {"id": "s-won", "name": "Won", "display_order": 40},
        {"id": "s-prospect", "name": "Prospect", "display_order": 10}])
    r = client.post("/concierge/crm-apply", json={"values": [
        {"name": "stage", "value": "New enquiry | active"},
        {"name": "stage", "value": "won | won"},               # exists (case) — kept
        {"name": "quiet_days", "value": "21"},
    ]})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] and data["link"]["href"].startswith("/app/crm/settings")
    assert [s["name"] for s in env.inserted] == ["New enquiry"]
    assert env.inserted[0]["display_order"] == 50          # continues past the max
    assert env.deleted == ["s-prospect"]                   # not recommended → gone
    assert env.tcfg == {"crm_quiet_days": 21}
    assert "New enquiry" in data["summary"] and "21 days" in data["summary"]
    assert "Removed: Prospect" in data["summary"] and "Unstaged" in data["summary"]


def test_crm_apply_quiet_days_only_removes_nothing(client, monkeypatch):
    env = CrmEnv(monkeypatch, existing=[{"id": "s1", "name": "Won"}])
    r = client.post("/concierge/crm-apply",
                    json={"values": [{"name": "quiet_days", "value": "10"}]})
    assert r.status_code == 200
    assert env.deleted == [] and env.inserted == []
    assert env.tcfg == {"crm_quiet_days": 10}


def test_crm_apply_gates_on_module_and_rejects_empty(client, monkeypatch):
    CrmEnv(monkeypatch, modules=set())
    assert client.post("/concierge/crm-apply",
                       json={"values": [{"name": "stage", "value": "X"}]}).status_code == 404
    CrmEnv(monkeypatch)
    assert client.post("/concierge/crm-apply",
                       json={"values": [{"name": "bogus", "value": "1"}]}).status_code == 400


# ── run #7 (2026-09-05): a blackout date must never sit in the past ───────────
def test_catalogue_opens_with_today_and_the_next_date_rule():
    from datetime import date
    block = settings_recommend.catalogue_block(today=date(2026, 9, 5))
    assert block.startswith("Today is 2026-09-05 (Saturday).")
    assert "NEXT such date" in block


def test_past_blackout_dates_roll_forward_to_the_next_occurrence():
    from datetime import date
    today = date(2026, 9, 5)
    # The swim school's half-term, dated to last year by the model.
    assert settings_recommend.next_dates("2025-10-27\n2025-10-28", today=today) == "2026-10-27\n2026-10-28"
    # Already future: untouched. Earlier this year: next year. Junk: dropped.
    assert settings_recommend.next_dates("2026-12-24\n2026-02-14\nnot a date", today=today) == "2026-12-24\n2027-02-14"
    out = settings_recommend.clean("diary_settings", [
        {"name": "blackout_dates", "value": "2025-10-27\n2025-10-30"},
        {"name": "open_time", "value": "08:00"},
    ], today=today)
    assert out[0] == {"name": "blackout_dates", "value": "2026-10-27\n2026-10-30"}


def test_hours_unit_follows_the_service_lengths_not_the_wording():
    """Run #8: a card for 60- and 120-minute lessons wrote hours_unit = hour twice,
    under which a 2-hour lesson spends one hour. The cleaner sets minutes whenever
    the services are not all one hour; a one-hour-only menu keeps the choice."""
    mixed = settings_recommend.clean("diary_settings", [
        {"name": "service", "value": "1-hour lesson | 60 | 34 | time"},
        {"name": "service", "value": "2-hour lesson | 120 | 66 | time"},
        {"name": "hours_unit", "value": "hour"}])
    assert {v["name"]: v["value"] for v in mixed}["hours_unit"] == "minutes"
    whole = settings_recommend.clean("diary_settings", [
        {"name": "service", "value": "Cut | 60 | 25 | time"},
        {"name": "hours_unit", "value": "hour"}])
    assert {v["name"]: v["value"] for v in whole}["hours_unit"] == "hour"
    other = settings_recommend.clean("packages", [{"name": "package", "value": "Block | 10 | hour | 320 | 180"}])
    assert other and other[0]["name"] == "package"


def test_a_recall_past_a_year_is_read_as_the_validity():
    """Run #8: the card wrote "Theory test pass date | date | expires 730" for a
    two-year certificate with a 60-day reminder — a recall cannot exceed a year,
    so the cleaner turns it into the default recall plus the validity in months."""
    out = settings_recommend.clean("crm_fields", [
        {"name": "contact_field", "value": "Theory test pass date | date | expires 730 | the pass certificate"},
        {"name": "contact_field", "value": "First aid | date | expires 90 | the certificate"},
        {"name": "contact_field", "value": "DBS | date | expires 60 valid 36 | the check"}])
    vals = [v["value"] for v in out]
    assert vals[0] == "Theory test pass date | date | expires 60 valid 24 | the pass certificate"
    assert vals[1].startswith("First aid | date | expires 90 |")           # a real recall stays
    assert vals[2] == "DBS | date | expires 60 valid 36 | the check"        # already explicit
    assert "dropdown: option, option" in settings_recommend.catalogue_block()
    assert "never a field for a progress level" in settings_recommend.catalogue_block()



# ── words(): stored codes said in the owner's words ──────────────────────────
# First-sign-up film, 30 Sep 2026: the card read "Windows: ampm", "Working
# days: mon,tue,wed,thu,fri,sat", "Recurring enabled: 1", "Service out of
# hours: Name | evenings … | | yes | 140 | 65" on camera.
@pytest.mark.parametrize("name, value, said", [
    ("working_days", "mon,tue,wed,thu,fri,sat", "Working days: Monday to Saturday"),
    ("working_days", "mon,wed,fri", "Working days: Monday, Wednesday and Friday"),
    ("working_days", "mon,tue,wed,thu,fri,sat,sun", "Working days: every day"),
    ("windows", "ampm", "Bookings: morning and afternoon windows"),
    ("recurring_enabled", "1", "Repeat bookings: yes"),
    ("recurring_freq", "monthly,six_weekly,quarterly", "Repeat bookings: monthly, every six weeks and quarterly"),
    ("noun_booking", "job", "Your word for a booking: \u201cjob\u201d"),
    ("bank_holiday_nation", "england-and-wales", "Bank holidays: closed on England and Wales bank holidays"),
    ("service_out_of_hours", "Emergency visit | evenings and Sundays and bank holidays | | yes | 140 | 65",
     "Out of hours: Emergency visit \u2014 evenings and Sundays and bank holidays, \u00a3140 first hour, then \u00a365 an hour"),
    ("service_out_of_hours", "Call-out | evenings until 21:00 | Saturdays | yes | 95 |",
     "Out of hours: Call-out \u2014 evenings until 21:00 and Saturdays and bank holidays, \u00a395 first hour"),
    ("service", "Emergency | 60 | 85 | half", "Service: Emergency \u2014 60 min, \u00a385, a morning or afternoon window"),
])
def test_display_says_a_diary_code_in_words(name, value, said):
    assert settings_recommend.display("diary_settings", name, value) == said


def test_display_is_none_for_a_value_it_cannot_word():
    # The widget then prints the plain "Name: value" line, as before.
    assert settings_recommend.display("diary_settings", "open_time", "08:00") is None
    assert settings_recommend.display("diary_settings", "working_days", "weekdays") is None
    assert settings_recommend.display("diary_settings", "recurring_freq", "monthly,yearly") is None
    assert settings_recommend.display("invoice_settings", "payment_terms", "On completion") is None


def test_display_says_the_invoice_switches_in_words():
    assert settings_recommend.display("invoice_settings", "show_payment_terms", "1") == "Payment terms on invoices: yes"
    assert settings_recommend.display("invoice_settings", "default_deposit_percent", "25") is None   # a number reads as it is


def test_with_display_keeps_name_and_value_and_adds_the_line_only_when_known():
    out = settings_recommend.with_display("diary_settings", [
        {"name": "working_days", "value": "mon,tue"}, {"name": "open_time", "value": "08:00"}])
    assert out == [{"name": "working_days", "value": "mon,tue", "display": "Working days: Monday and Tuesday"},
                   {"name": "open_time", "value": "08:00"}]


def test_the_card_widget_prints_the_servers_words():
    from pathlib import Path
    js = Path("src/app/static/concierge.js").read_text(encoding="utf-8")
    assert 'line.textContent = v.display || (prettyName(v.name) + ": " + v.value);' in js


def test_the_card_opens_the_booking_gates_it_fills():
    """First-sign-up film, 30 Sep 2026: "Use recommended settings" then "Save
    diary settings" left capacity, working days and opening hours NULL — the
    form posts each gate's state select, still on "Not set yet", so
    booking_policy.apply_settings dropped the typed values. The card now sets
    the gate to "set" and ticks its confirm for every gated field it wrote,
    and fills the only timezone on offer with them."""
    from pathlib import Path
    js = Path("src/app/static/concierge.js").read_text(encoding="utf-8")
    assert 'var PF_GATES = { capacity_per_window: "capacity", working_days: "working_days",' in js
    assert 'open_time: "opening_hours", close_time: "opening_hours" };' in js
    assert "if (wrote && PF_GATES[v.name]) gates[PF_GATES[v.name]] = true;" in js
    assert "pfOpenGates(form, gates);" in js
    assert """var st = form.querySelector('select[name="' + k + '_state"]');""" in js
    assert 'if (st && st.value !== "set") { st.value = "set"; pfEvents(st); }' in js
    assert """var c = form.querySelector('input[name="confirm_booking_setting"][value="' + k + '"]');""" in js
    assert 'if (hasTz) { tz.value = "Europe/London"; pfEvents(tz); keys.push("timezone"); }' in js
    # pfSetField reports whether it wrote, so a gate never opens for a value
    # that did not land (held-prefill mode leaves a saved field alone).
    assert "if (!els.length) return false;" in js


# Real take, 30 Sep 2026: the Dependants card printed "Field: Property | Address
# | text | required | once | no gate | …" and "Group: Property | Properties |
# several"; the Diary card "Same-day: Emergency — 14:00, 0"; the CRM card
# "Stage: New enquiry | active".
@pytest.mark.parametrize("page, name, value, said", [
    ("dependants", "group", "Property | Properties | several", "Kind: Property (plural Properties) any number each"),
    ("dependants", "group", "Dog | Dogs | one", "Kind: Dog (plural Dogs) one each"),
    ("dependants", "field", "Property | Address | text | required | once | no gate | the address",
     "Detail: Address (Property) \u2014 required, asked once and kept"),
    ("dependants", "field", "Dog | Vaccinations | date | required | expires | gate | must be current",
     "Detail: Vaccinations (Dog) \u2014 required, a date, with an expiry date, booking waits for it"),
    ("dependants", "ladder", "customer | Level | Bronze, Silver, Gold | approve | Advanced = Silver",
     "Progression: Level (customer) \u2014 Bronze, Silver, Gold; moves drafted for your approval; Advanced = Silver"),
    ("diary_settings", "service_urgent", "Emergency | 14:00 | 0", "Same-day: Emergency \u2014 same day if asked before 14:00"),
    ("diary_settings", "service_urgent", "Call-out | 12:00 | 2", "Same-day: Call-out \u2014 same day if asked before 12:00, 2 hours notice"),
    ("crm_settings", "stage", "New enquiry | active", "Stage: New enquiry"),
    ("crm_settings", "stage", "Job done | won", "Stage: Job done (won)"),
    ("crm_settings", "stage", "Went quiet | dormant", "Stage: Went quiet (gone quiet)"),
])
def test_display_says_a_pipe_row_as_a_sentence(page, name, value, said):
    assert settings_recommend.display(page, name, value) == said
