import json
"""Remembered booking requirements (app/intake_memory, Kris 2026-09-21).

"Is the Booking requirements info saved on the Contacts CRM for future
bookings?" — it wasn't. What this pins:
  - a row's durability (ask / once / confirm / expires) survives cleaning and
    the form, and never rides on a document row or the stay field;
  - the hold gate reads the customer's record BEFORE it refuses, says what it
    used, leaves a lapsed date for the ask and names it; the customer's own
    word this time wins and is written back — now, or parked for the pipeline
    when the record does not exist yet;
  - a tenant with no remembered row never touches the CRM (byte-identical);
  - a remembered row is promoted to a customer field on save; a refused
    promotion drops back to ask-every-time and the page says so;
  - the drafter prompt and the concierge catalogue teach the concept, and the
    recommendation's remember part is canonicalised where the card reads it.

Run: python -m pytest tests/test_intake_memory.py -q
"""
from tests.booking_fixtures import carried_config
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import intake, intake_memory, scheduling, settings_recommend
from app.auth import TenantCtx, require_onboarded
from app.crm_fields import FieldError
from app.main import app
from app.security import require_csrf

NOW = datetime(2026, 6, 1, 9, 0)

FIELDS = [
    {"label": "Customer name", "required": True, "hint": ""},
    {"label": "Feeding instructions", "required": True, "hint": "", "remember": "confirm"},
    {"label": "Licence number", "required": True, "hint": "", "remember": "once"},
    {"label": "Insurance expiry", "required": False, "hint": "", "remember": "expires"},
    {"label": "Inscription", "required": False, "hint": ""},
]


# ── the schema ────────────────────────────────────────────────────────────────
def test_clean_fields_keeps_durability_only_when_remembered():
    out = intake.clean_fields([
        {"label": "Name", "required": True},
        {"label": "Feeding", "required": True, "remember": "confirm", "crm_key": "feeding"},
        {"label": "Cert", "required": True, "remember": "once", "doc": True},
        {"label": "Odd", "required": True, "remember": "whenever"},
        {"label": intake.STAY_FIELD["label"], "required": True, "remember": "once"},
    ])
    assert "remember" not in out[0] and "crm_key" not in out[0]
    assert out[1]["remember"] == "confirm" and out[1]["crm_key"] == "feeding"
    assert all("remember" not in f for f in out[2:])
    assert [f["label"] for f in intake.remembered(out)] == ["Feeding"]


def test_clean_remember_tolerates_the_models_phrasing():
    assert intake.clean_remember("remember: once") == "once"
    assert intake.clean_remember("Confirmed each visit") == "confirm"
    assert intake.clean_remember("expiry") == "expires"
    assert intake.clean_remember("each visit") == "ask"
    assert intake.clean_remember("") == "ask" and intake.clean_remember(None) == "ask"


def test_parse_form_carries_remember_and_crm_key():
    rows = intake.parse_form(["Name", "Feeding"], ["1", "1"], ["", ""], ["0", "0"], ["", ""],
                             ["ask", "confirm"], ["", "feeding"])
    assert "remember" not in rows[0]
    assert rows[1]["remember"] == "confirm" and rows[1]["crm_key"] == "feeding"
    assert intake.parse_form(["Name"], ["1"], [""])[0]["label"] == "Name"   # older forms


# ── reading the record ────────────────────────────────────────────────────────
def test_prefill_reads_the_record_and_the_customers_word_wins():
    record = {"custom_data": {"feeding_instructions": "two scoops", "licence_number": "AB123"}}
    details, filled, lapsed = intake_memory.prefill_from_record(FIELDS, {"customer_name": "Kris"}, record)
    assert details["Feeding instructions"] == "two scoops" and details["Licence number"] == "AB123"
    assert filled["Feeding instructions"]["remember"] == "confirm" and lapsed == {}
    # what this email says wins over the record — under a snake_case key too
    details, filled, _ = intake_memory.prefill_from_record(FIELDS, {"feeding_instructions": "three"}, record)
    assert "Feeding instructions" not in details and "Feeding instructions" not in filled
    # a placeholder on the record answers nothing; no record answers nothing
    assert intake_memory.prefill_from_record(FIELDS, {}, {"custom_data": {"licence_number": "TBC"}})[1] == {}
    assert intake_memory.prefill_from_record(FIELDS, {"a": 1}, None)[0] == {"a": 1}


def test_an_expired_date_on_record_is_not_reused_and_is_named():
    record = {"custom_data": {"insurance_expiry": "2026-03-01"}}
    details, _, lapsed = intake_memory.prefill_from_record(FIELDS, {}, record, on="2026-06-18")
    assert "Insurance expiry" not in details and lapsed["Insurance expiry"].startswith("lapsed")
    details, _, lapsed = intake_memory.prefill_from_record(
        FIELDS, {}, {"custom_data": {"insurance_expiry": "2027-03-01"}}, on="2026-06-18")
    assert details["Insurance expiry"] == "2027-03-01" and lapsed == {}
    # a validity on the definition (run #8) extends the date on record
    defs = [{"key": "insurance_expiry", "field_type": "date", "durability": "expires", "valid_months": 12}]
    details, _, lapsed = intake_memory.prefill_from_record(FIELDS, {}, record, defs, on="2026-06-18")
    assert details["Insurance expiry"] == "2026-03-01" and lapsed == {}
    assert "ask for the new date" in intake_memory.lapsed_note({"Insurance expiry": "lapsed 1st Mar 26"})


def test_record_note_says_what_was_used_and_asks_to_confirm():
    note = intake_memory.record_note({
        "Feeding instructions": {"value": "two scoops", "remember": "confirm"},
        "Licence number": {"value": "AB123", "remember": "once"}})
    assert "Feeding instructions: two scoops" in note and "do NOT ask" in note
    assert "Confirm Feeding instructions in ONE line" in note and "Licence number in ONE" not in note
    assert intake_memory.record_note({}) == ""


# ── writing it back ───────────────────────────────────────────────────────────
def test_values_to_remember_takes_only_real_values_on_remembered_rows():
    given = {"Customer name": "Kris", "feeding_instructions": "two scoops", "Licence number": "<UNKNOWN>",
             "Insurance expiry": "1 March 2027", "Inscription": "Happy 40th"}
    assert intake_memory.values_to_remember(FIELDS, given) == {
        "feeding_instructions": "two scoops", "insurance_expiry": "2027-03-01"}
    assert intake_memory.values_to_remember(FIELDS, {"Insurance expiry": "soon"}) == {}
    scoped = [{"label": "Vet name", "required": False, "remember": "once", "services": ["Remedial"]}]
    assert intake_memory.values_to_remember(scoped, {"Vet name": "Dr X"}, service="Full set") == {}
    assert intake_memory.values_to_remember(scoped, {"Vet name": "Dr X"}, service="Remedial") == {"vet_name": "Dr X"}
    assert intake_memory.labels_for(FIELDS, ["feeding_instructions"]) == ["Feeding instructions"]


def test_remember_by_email_finds_the_contact(monkeypatch):
    calls = []
    monkeypatch.setattr(intake_memory.db, "crm_contact_by_email",
                        lambda tid, email, **k: {"id": "c-9"} if email == "kris@x.co" else None)
    monkeypatch.setattr(intake_memory.db, "crm_merge_custom_data",
                        lambda entity, rid, tid, values, **k: calls.append((rid, values, k.get("overwrite"))) or {})
    assert intake_memory.remember_by_email("t1", "kris@x.co", {"feeding_instructions": "x"}) == {"feeding_instructions": "x"}
    assert intake_memory.remember_by_email("t1", "nobody@x.co", {"feeding_instructions": "x"}) == {}
    assert calls == [("c-9", {"feeding_instructions": "x"}, True)]


# ── the hold gate ─────────────────────────────────────────────────────────────
def _hold_env(monkeypatch, contact_row):
    held = {}
    monkeypatch.setattr(scheduling, "place_hold", lambda *a, **k: (held.update(k) or
        {"id": "b-1", "booking_ref": "BK-1", "booking_date": "2026-06-18", "window": "day"}, None))
    monkeypatch.setattr(scheduling.db, "release_offered_for_contact", lambda *a, **k: 0)
    monkeypatch.setattr(scheduling.db, "offered_bookings_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "crm_contact_by_email", lambda tid, email, **k: contact_row)
    monkeypatch.setattr(scheduling.db, "crm_list_field_definitions", lambda *a, **k: [])
    writes = []
    monkeypatch.setattr(scheduling.db, "crm_merge_custom_data",
                        lambda entity, rid, tid, values, **k: writes.append((entity, rid, dict(values), k.get("overwrite"))) or {})
    return writes, held


def test_the_hold_gate_reads_the_record_before_it_refuses(monkeypatch):
    writes, held = _hold_env(monkeypatch, {"id": "c-1", "custom_data": {
        "feeding_instructions": "two scoops", "licence_number": "AB123"}})
    execute, state = scheduling.make_executor("t1", contact="kris@x.co", now=NOW, intake_fields=FIELDS)
    out = execute("hold_booking", {"date": "2026-06-18", "window": "day", "summary": "x",
                                   "details": {"Customer name": "Kris"}})
    assert out["held"] is True
    assert sorted(out["prefilled_from_record"]) == ["Feeding instructions", "Licence number"]
    assert "two scoops" in out["message"] and "Confirm Feeding instructions" in out["message"]
    # the record's values ride on the booking like any detail
    assert held["details"]["Feeding instructions"] == "two scoops"
    # nothing the customer said this time → nothing written, nothing parked
    assert writes == [] and "remember_pending" not in state and "remembered_labels" not in state


def test_the_customers_own_word_is_written_back_to_the_record(monkeypatch):
    writes, _ = _hold_env(monkeypatch, {"id": "c-1", "custom_data": {"feeding_instructions": "two scoops"}})
    execute, state = scheduling.make_executor("t1", contact="kris@x.co", now=NOW, intake_fields=FIELDS)
    out = execute("hold_booking", {"date": "2026-06-18", "window": "day", "summary": "x",
                                   "details": {"Customer name": "Kris", "Feeding instructions": "three scoops now",
                                               "Licence number": "AB123"}})
    assert out["held"] is True and "prefilled_from_record" not in out
    assert writes == [("contact", "c-1", {"feeding_instructions": "three scoops now", "licence_number": "AB123"}, True)]
    assert state["remembered_labels"] == ["Feeding instructions", "Licence number"]


def test_a_first_time_customer_parks_the_values_for_the_pipeline(monkeypatch):
    writes, _ = _hold_env(monkeypatch, None)
    execute, state = scheduling.make_executor("t1", contact="new@x.co", now=NOW, intake_fields=FIELDS)
    out = execute("hold_booking", {"date": "2026-06-18", "window": "day", "summary": "x",
                                   "details": {"Customer name": "Sam", "Feeding instructions": "one scoop",
                                               "Licence number": "ZZ9"}})
    assert out["held"] is True and writes == []
    assert state["remember_pending"] == {"feeding_instructions": "one scoop", "licence_number": "ZZ9"}
    # the pipeline flushes after CRM ingest onto the record it just made, and logs once
    events = []
    monkeypatch.setattr(intake_memory.db, "crm_merge_custom_data",
                        lambda entity, rid, tid, values, **k: writes.append((rid, dict(values))) or {})
    monkeypatch.setattr(intake_memory.db, "log_agent_event",
                        lambda tid, kind, summary, **k: events.append((kind, summary, k.get("detail"))))
    labels = intake_memory.flush_pending("t1", "c-new", state, fields=FIELDS, email_log_id="L1", contact="new@x.co")
    assert writes == [("c-new", {"feeding_instructions": "one scoop", "licence_number": "ZZ9"})]
    assert labels == ["Feeding instructions", "Licence number"] and "remember_pending" not in state
    assert events[0][0] == "crm_autofill" and "Kept 2 booking details" in events[0][1]
    assert events[0][2] == {"remembered": labels}
    assert intake_memory.flush_pending("t1", "c-new", {}, fields=FIELDS) == [] and len(events) == 1


def test_a_lapsed_date_on_record_is_named_in_the_refusal(monkeypatch):
    fields = FIELDS[:1] + [{"label": "Insurance expiry", "required": True, "hint": "", "remember": "expires"}]
    _hold_env(monkeypatch, {"id": "c-1", "custom_data": {"insurance_expiry": "2026-03-01"}})
    execute, _ = scheduling.make_executor("t1", contact="kris@x.co", now=NOW, intake_fields=fields)
    out = execute("hold_booking", {"date": "2026-06-18", "window": "day", "summary": "x",
                                   "details": {"Customer name": "Kris"}})
    assert out["error"] == "missing_info" and out["missing"] == ["Insurance expiry"]
    assert "lapsed" in out["message"] and "ask for the new date" in out["message"]
    assert out["lapsed_on_record"]["Insurance expiry"].startswith("lapsed")


def test_no_remembered_rows_means_no_record_read(monkeypatch):
    """Every tenant that has not switched a row on is byte-identical: the gate
    never touches the CRM (the older gate tests stub no contact lookup)."""
    fields = [{"label": "Customer name", "required": True, "hint": ""}]
    _hold_env(monkeypatch, None)

    def boom(*a, **k):
        raise AssertionError("record read without a remembered row")
    monkeypatch.setattr(scheduling.db, "crm_contact_by_email", boom)
    execute, state = scheduling.make_executor("t1", contact="kris@x.co", now=NOW, intake_fields=fields)
    out = execute("hold_booking", {"date": "2026-06-18", "window": "day", "summary": "x",
                                   "details": {"Customer name": "Kris"}})
    assert out["held"] is True and "remember_pending" not in state


# ── promotion to a customer field ─────────────────────────────────────────────
def test_sync_definitions_adopts_creates_and_reports(monkeypatch):
    existing = [{"key": "licence_number", "label": "Licence number", "entity": "contact", "field_type": "text"}]
    added = []
    monkeypatch.setattr(intake_memory.db, "crm_list_field_definitions", lambda tid, entity=None, **k: list(existing))
    monkeypatch.setattr(intake_memory.crm_fields, "add_field", lambda tid, d, **k: added.append(d) or d)
    fields, problems = intake_memory.sync_definitions("t1", FIELDS)
    by = {f["label"]: f for f in fields}
    assert by["Licence number"]["crm_key"] == "licence_number"                 # adopted by slug
    assert by["Feeding instructions"]["crm_key"] == "feeding_instructions"     # created
    assert by["Insurance expiry"]["crm_key"] == "insurance_expiry"
    assert "crm_key" not in by["Customer name"] and problems == []
    assert {d["label"]: (d["field_type"], d["durability"]) for d in added} == {
        "Feeding instructions": ("text", "once"), "Insurance expiry": ("date", "expires")}
    assert added[0]["extraction_hint"] == "Given when booking: Feeding instructions"
    # the cap: the row drops back to ask-every-time and the reason comes back

    def full(tid, d, **k):
        raise FieldError("That's the lot — max 10 fields per entity")
    monkeypatch.setattr(intake_memory.crm_fields, "add_field", full)
    fields, problems = intake_memory.sync_definitions("t1", FIELDS)
    by = {f["label"]: f for f in fields}
    assert "remember" not in by["Feeding instructions"] and by["Licence number"]["remember"] == "once"
    assert problems[0].startswith("Feeding instructions: That's the lot")
    # a read blip keeps the rows as they are

    def blip(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(intake_memory.db, "crm_list_field_definitions", blip)
    fields, problems = intake_memory.sync_definitions("t1", FIELDS)
    assert {f["label"]: f.get("remember") for f in fields}["Feeding instructions"] == "confirm" and problems == []
    # no remembered row → no read at all
    assert intake_memory.sync_definitions("t1", FIELDS[:1]) == (intake.clean_fields(FIELDS[:1]), [])


# ── the drafter and the concierge ─────────────────────────────────────────────
def test_the_prompt_tells_the_drafter_how_a_remembered_row_is_treated():
    p = intake.render_for_prompt(FIELDS)
    assert "Feeding instructions (REQUIRED — ON FILE from their last booking" in p
    assert "Licence number (REQUIRED — REMEMBERED on the customer's record" in p
    assert "Insurance expiry (optional — a DATE kept on the customer's record" in p
    assert "`prefilled_from_record`" in p
    g = intake.render_for_gather(FIELDS)
    assert "kept on the customer's record" in g and "confirm it in one line" in g
    assert "prefilled_from_record" not in intake.render_for_prompt(FIELDS[:1])


def test_the_recommendation_carries_the_remember_part_fifth():
    vals = settings_recommend.clean("booking_details", [
        {"name": "field", "value": "Feeding instructions | required | how much and when | remember: confirm"},
        {"name": "field", "value": "Licence number | required | | only for: | remembered once"},
        {"name": "field", "value": "Inscription | optional | the wording | remember: each visit"},
        {"name": "field", "value": "Vet | optional | | only for: Remedial"},
    ])
    got = {v["value"].split("|")[0].strip(): [p.strip() for p in v["value"].split("|")] for v in vals}
    assert got["Feeding instructions"] == ["Feeding instructions", "required", "how much and when", "", "remember: confirm"]
    assert got["Licence number"] == ["Licence number", "required", "", "only for:", "remember: once"]
    assert got["Inscription"] == ["Inscription", "optional", "the wording"]
    assert got["Vet"] == ["Vet", "optional", "", "only for: Remedial"]
    p = settings_recommend.catalogue_block()
    assert "remember: once, confirm or expires" in p and "A document row" in p
    assert "write it as a booking_details row with a remember part instead" in p


def test_the_card_script_fills_the_remember_select():
    src = Path(__file__).resolve().parents[1].joinpath("src", "app", "static", "concierge.js").read_text(encoding="utf-8")
    assert 'services: "services", remember: "remember", kind: "doc"' in src
    from app import setup_rows
    row = setup_rows.parse("booking_details", "field", "Address | required | remember: confirm")
    assert row["fields"]["remember"] == "confirm"


# ── the page ──────────────────────────────────────────────────────────────────
@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def _env(monkeypatch, cfg):
    monkeypatch.setattr(dashboard.setup_forms, 'load', lambda ctx, page: {
        'page': page, 'config': carried_config(cfg), 'definitions': []})
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"scheduling"})
    monkeypatch.setattr(dashboard.db, "module_active", lambda tid, m, **k: m == "scheduling")
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: carried_config(cfg))
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"industry": "other"})
    monkeypatch.setattr(dashboard.db, "get_tenant_config", lambda *a, **k: {"business_brief": ""})


def test_the_page_offers_the_durability_on_both_rows_and_shows_the_face(client, monkeypatch):
    _env(monkeypatch, {"intake_configured_at": "2026-06-01", "intake_fields": FIELDS})
    html = client.get("/app/diary/intake").text
    assert html.count("Remember it for next time?") == len(FIELDS) + 1   # every rendered row + the JS template
    assert "Feeding instructions · on file, confirmed each time" in html
    assert "Licence number · remembered" in html
    assert 'name="crm_key"' in html and "MEM_FACE" in html


def test_saving_promotes_remembered_rows_and_reports_a_refused_one(client, monkeypatch):
    _env(monkeypatch, {"intake_configured_at": "2026-06-01", "intake_fields": []})
    saved = {}
    monkeypatch.setattr(dashboard.setup_forms, "save", lambda ctx, page, expected, ops:
                        (saved.update(fields=ops[-1]['fields']['intake_fields']), added.extend(o['fields'] for o in ops[:-1])))
    monkeypatch.setattr(intake_memory.db, "crm_list_field_definitions", lambda *a, **k: [])
    added = []
    monkeypatch.setattr(intake_memory.crm_fields, "add_field", lambda tid, d, **k: added.append(d) or d)
    form = {"settings_snapshot": json.dumps(dashboard.setup_forms.load(None,"booking_details")), "label": ["Customer name", "Feeding instructions"], "required": ["1", "1"],
            "hint": ["", "how much"], "doc": ["0", "0"], "services": ["", ""],
            "remember": ["ask", "confirm"], "crm_key": ["", ""]}
    r = client.post("/app/diary/intake", follow_redirects=False, data=form)
    assert r.status_code == 303 and r.headers["location"] == "/app/diary?intake=saved"
    assert saved["fields"][1]["remember"] == "confirm" and saved["fields"][1]["crm_key"] == "feeding_instructions"
    assert "remember" not in saved["fields"][0]
    assert added[0]["label"] == "Feeding instructions" and added[0]["extraction_hint"] == "how much"

    def full(d, existing):
        raise FieldError("That's the lot")
    monkeypatch.setattr(intake_memory.crm_fields, "prepare_field", full)
    r = client.post("/app/diary/intake", follow_redirects=False, data=form)
    assert r.status_code == 303 and r.headers["location"].startswith("/app/diary/intake?intake=saved&memerr=")
    assert "remember" not in saved["fields"][1]
    html = client.get(r.headers["location"]).text
    assert "Not remembered" in html and "Feeding instructions:" in html


@pytest.fixture(autouse=True)
def _established_booking_policy(monkeypatch):
    from app import db
    monkeypatch.setattr(db, "get_scheduling_config", lambda *a, **k: carried_config())
