import json
"""Scheduling intake — the per-tenant "what a booking needs" schema + its hard gate.

What this pins:
  - the pure helpers (app/intake): generic suggestions, cleaning (blanks/dupes/caps),
    case-insensitive required-field detection, the prompt block, form parsing
  - the office hold executor REFUSES to book until every required detail is present
    (missing_info) and proceeds once they are; no fields = no gate (today's behaviour)
  - the office <scheduling> prompt carries the injected intake block
  - the routes: the setup page seeds on first run, shows saved fields once configured,
    404s without scheduling, saves+redirects; and the office diary is BLOCKED (redirects
    to setup) until intake is configured

Run: python -m pytest tests/ -q
"""
from tests.booking_fixtures import carried_config
import io
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import intake, scheduling
from app.agent import build_system_prompt
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf


# ── pure helpers ──────────────────────────────────────────────────────────────
def test_suggest_seeds_only_generic_base():
    f = intake.suggest_for("logistics")
    labels = [x["label"] for x in f]
    assert labels == ["Customer name", "Contact number", "Preferred date / time"]
    base = {x["label"]: x for x in f}
    assert base["Customer name"]["required"] and base["Preferred date / time"]["required"]


def test_suggest_unknown_industry_uses_other_bucket():
    labels = [x["label"] for x in intake.suggest_for("bespoke widget fabrication")]
    assert labels == ["Customer name", "Contact number", "Preferred date / time"]
    assert "Collection address" not in labels


def test_clean_fields_drops_blanks_dupes_and_trims():
    raw = [{"label": "Name", "required": True},
           {"label": " name ", "required": False},          # dupe (normalised) — dropped
           {"label": "", "required": True},                 # blank — dropped
           {"label": "Pet", "required": False, "hint": " breed "}]
    out = intake.clean_fields(raw)
    assert [f["label"] for f in out] == ["Name", "Pet"]
    assert out[0]["required"] is True and out[1]["hint"] == "breed"


def test_missing_required_is_case_and_space_insensitive():
    fields = [{"label": "Customer name", "required": True}, {"label": "Pet", "required": True},
              {"label": "Notes", "required": False}]
    assert intake.missing_required(fields, {"customer  NAME": "Sam", "Notes": ""}) == ["Pet"]
    assert intake.missing_required(fields, {"Customer name": "Sam", "pet": "Rex"}) == []
    # an empty/whitespace value counts as missing
    assert intake.missing_required(fields, {"Customer name": " ", "Pet": "Rex"}) == ["Customer name"]


def test_placeholders_do_not_satisfy_the_gate():
    # Live 2026-09-04 (farrier run): after one refusal the model re-called with
    # "<UNKNOWN>" in both required fields and three offers went out on it.
    fields = [{"label": "Contact number", "required": True},
              {"label": "Is the horse currently lame?", "required": True}]
    assert intake.missing_required(fields, {"Contact number": "<UNKNOWN>",
                                            "Is the horse currently lame?": "<UNKNOWN>"}) \
        == ["Contact number", "Is the horse currently lame?"]
    for bad in ("unknown", "TBC", "n/a", "N/A", "?", "-", "[not given]", "(tbc)",
                "Not stated", "to be confirmed", "don't know"):
        assert intake.missing_required(fields, {"Contact number": bad,
                                                "Is the horse currently lame?": "No"}) \
            == ["Contact number"], bad
    # real answers pass, including ones that merely contain a placeholder word
    for good in ("07700 900123", "No — neither horse is lame", "Unknown Road, Wells",
                 "not lame", "Yes, TBC on the exact leg but the near fore"):
        assert intake.missing_required(fields, {"Contact number": good,
                                                "Is the horse currently lame?": good}) == [], good


def test_snake_case_keys_satisfy_the_gate():
    # Live 2026-08-31: the model passed perfect values as company_name /
    # email_provider and the gate refused ALL FOUR required fields — the model
    # then apologised to the customer for a "snag our end". snake_case,
    # punctuation and case differences all match now.
    fields = [{"label": "Company name", "required": True, "hint": ""},
              {"label": "Email provider", "required": True, "hint": ""}]
    assert intake.missing_required(fields, {
        "company_name": "Velcee Consulting Ltd",
        "EMAIL-PROVIDER": "Gmail"}) == []


def test_the_refusal_coaches_a_retry_not_an_apology():
    from app import scheduling
    out = scheduling._missing_info(["Company name"])
    assert out["error"] == "missing_info" and out["missing"] == ["Company name"]
    assert "call this tool again with it filled in" in out["message"]
    assert "placeholder (<UNKNOWN>, TBC" in out["message"]
    assert "never tell the customer something went wrong" in out["message"]


def test_render_for_prompt_lists_fields_and_the_block_rule():
    p = intake.render_for_prompt([{"label": "Pet", "required": True, "hint": "name"},
                                  {"label": "Notes", "required": False, "hint": ""}])
    assert "Pet (REQUIRED) — name" in p and "Notes (optional)" in p
    assert "Do NOT call hold_booking until EVERY required detail" in p
    assert intake.render_for_prompt([]) == ""


def test_intake_harvests_before_it_asks():
    # Live 2026-08-31 (Kris: "surely we already have the info... it should have
    # looked there first"): the gatherer re-asked the same two questions across
    # THREE rounds while the answers sat in the replayed thread. Both intake
    # blocks now say: a detail already given anywhere in the thread, the
    # portal chat, or the customer context is collected — never ask again.
    fields = [{"label": "Pet", "required": True, "hint": ""}]
    for block in (intake.render_for_prompt(fields), intake.render_for_gather(fields)):
        assert "counts as collected" in block
        assert "ask" in block.lower() and "again" in block
    assert "NEVER ask for it again" in intake.render_for_prompt(fields)


def test_parse_form_aligns_positional_arrays():
    fields = intake.parse_form(["Name", "", "Pet"], ["1", "0", "0"], ["who", "", "breed"])
    assert [f["label"] for f in fields] == ["Name", "Pet"]     # blank row dropped
    assert fields[0]["required"] is True and fields[1]["required"] is False
    assert fields[1]["hint"] == "breed"
    # No doc[] array (an old form, a test caller) → every field is an answer.
    assert all(f["doc"] is False for f in fields)


# ── document requirements (Julius, 2026-09-02) ───────────────────────────────
def test_document_fields_round_trip_and_shape_the_ask():
    fields = intake.parse_form(
        ["Vaccination certificate", "Pet"], ["1", "1"], ["yearly booster proof", ""],
        ["1", "0"])
    assert fields[0]["doc"] is True and fields[1]["doc"] is False
    # clean_fields preserves the flag on the db round-trip.
    assert intake.clean_fields(fields)[0]["doc"] is True
    for block in (intake.render_for_prompt(fields), intake.render_for_gather(fields)):
        assert "Vaccination certificate (REQUIRED — a DOCUMENT)" in block
        assert "Pet (REQUIRED)" in block                    # answers unchanged
        assert "attach" in block
        # The reader marks inbound attachments; the drafter is told to read it —
        # and never to pretend it inspected the file.
        assert "[Attachments:" in block
        assert "contents" in block or "checked" in block
    # No document fields → none of the document coaching (prompt unchanged).
    plain = intake.render_for_prompt([{"label": "Pet", "required": True}])
    assert "DOCUMENT" not in plain and "[Attachments:" not in plain


def test_gmail_reader_surfaces_attachment_names():
    from app import gmail
    payload = {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "text/plain", "filename": "", "body": {}},
        {"mimeType": "application/pdf", "filename": "vaccination-cert.pdf",
         "body": {}},
        {"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "image/jpeg", "filename": "bella.jpg", "body": {}}]},
    ]}
    assert gmail._attachment_names(payload) == ["vaccination-cert.pdf", "bella.jpg"]
    body = gmail._body_with_attachments(payload)
    assert body.endswith("[Attachments: vaccination-cert.pdf, bella.jpg]")
    # No attachments → no marker, byte-identical body.
    assert "[Attachments:" not in gmail._body_with_attachments(
        {"mimeType": "text/plain", "filename": "", "body": {}})


# ── executor enforcement (the hard gate) ──────────────────────────────────────
NOW = datetime(2026, 6, 16, 10, 0, tzinfo=scheduling.DIARY_TZ)


def test_office_hold_blocked_until_required_details_present():
    fields = [{"label": "Customer name", "required": True}, {"label": "Pet", "required": True}]
    execute, state = scheduling.make_executor("t1", now=NOW, intake_fields=fields)
    out = execute("hold_booking", {"date": "2026-06-18", "window": "day", "summary": "x",
                                   "details": {"Customer name": "Sam"}})
    assert out["error"] == "missing_info" and out["missing"] == ["Pet"]
    assert state["bookings"] == []          # nothing was placed


def test_office_hold_proceeds_once_required_details_are_there(monkeypatch):
    monkeypatch.setattr(scheduling, "place_hold", lambda *a, **k: (
        {"id": "b-1", "booking_ref": "BK-1", "booking_date": "2026-06-18", "window": "day"}, None))
    fields = [{"label": "Customer name", "required": True}]
    execute, state = scheduling.make_executor("t1", now=NOW, intake_fields=fields)
    out = execute("hold_booking", {"date": "2026-06-18", "window": "day", "summary": "x",
                                   "details": {"Customer name": "Sam"}})
    assert out["held"] is True and out["booking_ref"] == "BK-1"
    assert len(state["bookings"]) == 1


def test_no_intake_fields_means_no_gate(monkeypatch):
    monkeypatch.setattr(scheduling, "place_hold", lambda *a, **k: (
        {"id": "b-1", "booking_ref": "BK", "booking_date": "2026-06-18", "window": "day"}, None))
    execute, _ = scheduling.make_executor("t1", now=NOW)     # no intake_fields → today's behaviour
    out = execute("hold_booking", {"date": "2026-06-18", "window": "day", "summary": "x"})
    assert out["held"] is True


# ── prompt injection ──────────────────────────────────────────────────────────
def test_office_prompt_carries_the_intake_block():
    block = "Booking intake — REQUIRED: Pet"
    p = build_system_prompt({}, scheduling=True, today=date(2026, 6, 16), intake=block)
    assert block in p
    assert "Booking intake" not in build_system_prompt({}, scheduling=True, today=date(2026, 6, 16))


# ── routes ────────────────────────────────────────────────────────────────────
@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def _env(monkeypatch, modules=("scheduling",), cfg=None, industry="other"):
    monkeypatch.setattr(dashboard.setup_forms, 'load', lambda ctx, page: {
        'page': page, 'config': carried_config(cfg), 'definitions': []})
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set(modules))
    monkeypatch.setattr(dashboard.db, "module_active", lambda tid, m, **k: m in modules)
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: carried_config(cfg))
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"industry": industry})
    monkeypatch.setattr(dashboard.db, "get_tenant_config", lambda *a, **k: {"business_brief": ""})


def test_intake_page_uses_generic_base_when_fresh(client, monkeypatch):
    _env(monkeypatch, cfg=None, industry="logistics")
    r = client.get("/app/diary/intake")
    assert r.status_code == 200
    assert "Customer name" in r.text and "Collection address" not in r.text
    assert "Finish setting up scheduling" in r.text          # the not-configured banner


def test_fresh_intake_page_keeps_its_save_bar_in_view(client, monkeypatch):
    """First-sign-up film, 30 Sep 2026: the checklist is pre-filled and meant to
    be accepted, but dirty_save.js hides the save bar until the form changes,
    so an owner happy with the defaults had nothing to press and the step never
    got its intake_configured_at. A first run marks the bar data-save-always."""
    from pathlib import Path
    _env(monkeypatch, cfg=None, industry="logistics")
    r = client.get("/app/diary/intake")
    assert "data-save-row data-save-always" in r.text
    assert "Looks right" in r.text
    js = Path("src/app/static/dirty_save.js").read_text(encoding="utf-8")
    assert "if (row.hasAttribute('data-save-always')) { row.dataset.dirtyWatched = '1'; row.hidden = false; return; }" in js
    # once saved, the bar earns its place again
    cfg = {"intake_configured_at": "2026-01-01T00:00:00+00:00",
           "intake_fields": [{"label": "Pet name", "required": True, "hint": ""}]}
    _env(monkeypatch, cfg=cfg)
    r = client.get("/app/diary/intake")
    assert "data-save-always" not in r.text and "Save changes" in r.text


def test_intake_page_shows_saved_fields_when_configured(client, monkeypatch):
    cfg = {"intake_configured_at": "2026-01-01T00:00:00+00:00",
           "intake_fields": [{"label": "Pet name", "required": True, "hint": ""}]}
    _env(monkeypatch, cfg=cfg)
    r = client.get("/app/diary/intake")
    assert "Pet name" in r.text and "Finish setting up scheduling" not in r.text


def test_intake_page_serves_every_tier(client, monkeypatch):
    # 2026-07-04 (Kris): booking requirements are an EMAIL-side concern — the
    # inbox pill links here for email-only, scheduling and vertical tenants
    # alike, so the page serves every tier (it used to 404 without scheduling)
    _env(monkeypatch, modules=("tms",))
    assert client.get("/app/diary/intake").status_code == 200


def test_office_diary_blocked_until_intake_configured(client, monkeypatch):
    _env(monkeypatch, modules=("scheduling",), cfg=None)    # scheduling on, NOT configured
    r = client.get("/app/diary", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/diary/intake"


def test_save_intake_persists_and_redirects(client, monkeypatch):
    _env(monkeypatch)
    saved = {}
    monkeypatch.setattr(dashboard.setup_forms, "save",
                        lambda ctx, page, expected, ops: saved.update(fields=ops[-1]['fields']['intake_fields']))
    r = client.post("/app/diary/intake", follow_redirects=False, data={
        "settings_snapshot": json.dumps(dashboard.setup_forms.load(None,"booking_details")),
        "label": ["Customer name", "Pet"], "required": ["1", "0"], "hint": ["who", ""],
    })
    assert r.status_code == 303 and r.headers["location"] == "/app/diary?intake=saved"
    assert [f["label"] for f in saved["fields"]] == ["Customer name", "Pet"]
    assert saved["fields"][0]["required"] is True and saved["fields"][1]["required"] is False


# ── Overnight-stays toggle (departure/collection) ─────────────────────────────
def test_stay_toggle_adds_and_removes_departure_field():
    base = intake.suggest_for("other")
    assert intake.is_stay(base) is False
    on = intake.apply_stay(base, True)
    assert intake.is_stay(on) is True
    assert any("departure" in f["label"].lower() for f in on)
    # the reserved field is hidden from the editable grid (it's the toggle instead)
    assert all("departure" not in f["label"].lower() for f in intake.editable_fields(on))
    # toggling off removes it again, leaving the rest intact
    off = intake.apply_stay(on, False)
    assert intake.is_stay(off) is False
    assert [f["label"] for f in off] == [f["label"] for f in base]


def test_stay_prompt_tells_agent_to_capture_both_times():
    on = intake.apply_stay(intake.suggest_for("other"), True)
    prompt = intake.render_for_prompt(on)
    assert "STAYS" in prompt and "DEPARTURE" in prompt


# ── The page explains itself (Kris 2026-09-01) ─────────────────────────────
# "It really isn't easy to understand how to use it." Two concrete causes: the
# column headers are `hidden md:grid`, so on a phone the three inputs had NO
# labels at all — three anonymous boxes a row, seven rows deep, with the key in
# a paragraph further up; and the page answered to five different names.

def _intake_html():
    """The template as served, without needing the whole route stack."""
    import re
    src = io.open(r"src/app/templates/app/diary_intake.html", encoding="utf-8").read()
    return src


def test_every_input_is_labelled_on_mobile():
    """The header row is desktop-only, so each field carries its own label
    underneath it. Without this a phone shows three unlabelled boxes."""
    src = _intake_html()
    for label in ("Detail to collect", "Must the agent have it?",
                  "Private hint for the agent"):
        # once in the server-rendered row, once in the JS <template>
        assert src.count(label) >= 2, f"{label} missing from one of the two rows"


def test_added_rows_are_labelled_too():
    """The row exists twice — rendered by Jinja and cloned by JS for "Add a
    detail". Labelling only the first would make added rows the confusing ones,
    which is worse than labelling neither."""
    src = _intake_html()
    tpl = src.split('<template id="row-template">', 1)[1]
    for label in ("Detail to collect", "Must the agent have it?",
                  "Private hint for the agent"):
        assert label in tpl, f"{label} missing from the JS row template"


def test_the_page_answers_to_one_name():
    """It was called Booking requirements, Scheduling setup, Booking details,
    Your booking checklist and Requirements — and the links that reach it used
    a different one from the heading it lands on."""
    src = _intake_html()
    assert "{% block heading %}Booking requirements{% endblock %}" in src
    # (the phrase survives in the comment explaining the rename — what matters
    # is that it is no longer a heading anyone reads)
    assert ">Your booking checklist<" not in src
    settings = io.open(r"src/app/templates/app/settings.html", encoding="utf-8").read()
    row = settings.split('href="/app/diary/intake"', 1)[1][:400]
    assert "Booking requirements" in row, "the Settings link still says something else"


def test_sender_address_satisfies_email_requirements():
    """Live e2e 2026-09-03: the draft asked Kris for an email address IN AN
    EMAIL FROM KRIS. The sender's own address fills any email-shaped required
    field — but never overwrites one the customer explicitly gave."""
    from app import intake
    fields = [{"label": "Email address", "required": True, "hint": ""},
              {"label": "Site address", "required": True, "hint": ""}]
    out = intake.satisfy_from_sender(fields, {"Site address": "St Mary's"},
                                     "kris@x.co")
    assert out["Email address"] == "kris@x.co"
    assert out["Site address"] == "St Mary's"
    # explicit value wins; junk senders fill nothing
    kept = intake.satisfy_from_sender(fields, {"Email address": "other@x.co"},
                                      "kris@x.co")
    assert kept["Email address"] == "other@x.co"
    assert intake.satisfy_from_sender(fields, {}, "front desk") == {}
    # the gate no longer lists it as missing
    assert "Email address" not in intake.missing_required(
        fields, intake.satisfy_from_sender(fields, {}, "kris@x.co"))
    # and the prompt coaching says so
    assert "SENDER'S OWN ADDRESS" in intake.render_for_prompt(
        [{"label": "Email address", "required": True, "hint": ""}])


def test_sender_fill_is_announced_in_the_hold_result(monkeypatch):
    """Live e2e 2026-09-03 (Stone Mason acceptance): the gate filled the email
    SILENTLY, the agent never learned it had, and the reply still asked for it.
    A hold that used the sender's address now says so in its result — and the
    prompt line for the field says never to ask."""
    from app import intake
    fields = [{"label": "Email address", "required": True, "hint": ""},
              {"label": "Full name", "required": True, "hint": ""}]
    assert intake.sender_filled_labels(fields, {"Full name": "Kris"}, "kris@x.co") == ["Email address"]
    assert intake.sender_filled_labels(fields, {"Email address": "a@b.co"}, "kris@x.co") == []
    assert intake.sender_filled_labels(fields, {}, None) == []
    # Live 2026-09-03 (second redraft): the agent keyed the literal words
    # "sender's email address" — a placeholder, not an address — and it stuck.
    # Anything without an "@" in an email field is treated as unfilled.
    ph = intake.satisfy_from_sender(fields, {"Email address": "sender's email address"}, "kris@x.co")
    assert ph["Email address"] == "kris@x.co"
    assert intake.sender_filled_labels(
        fields, {"Email address": "sender's email address"}, "kris@x.co") == ["Email address"]
    monkeypatch.setattr(scheduling, "place_hold", lambda *a, **k: (
        {"id": "b-1", "booking_ref": "BK-1", "booking_date": "2026-06-18", "window": "day"}, None))
    monkeypatch.setattr(scheduling.db, "release_offered_for_contact", lambda *a, **k: 0)
    monkeypatch.setattr(scheduling.db, "offered_bookings_for_contact", lambda *a, **k: [])
    execute, _ = scheduling.make_executor("t1", contact="kris@x.co", now=NOW, intake_fields=fields)
    out = execute("hold_booking", {"date": "2026-06-18", "window": "day", "summary": "x",
                                   "details": {"Full name": "Kris"}})
    assert out["held"] is True
    assert out["prefilled_from_sender"] == ["Email address"]
    assert "do NOT ask" in out["message"]
    prompt = intake.render_for_prompt(fields)
    line = [l for l in prompt.splitlines() if l.startswith("- Email address")][0]
    assert "NEVER ask for it" in line
    assert "leave it out of `details`" in line


# ── per-service scope + documents need a file (Kris 2026-09-04) ─────────────
MASON = [
    {"label": "Type of visit", "required": True, "hint": ""},
    {"label": "Inscription wording", "required": True, "hint": ""},
    {"label": "Cemetery permit or church faculty", "required": True, "hint": "", "doc": True,
     "services": ["Installation day"]},
    {"label": "Photos of the site", "required": True, "hint": "", "doc": True,
     "services": ["Site survey", "Installation day"]},
]


def test_scoped_fields_bind_only_their_services():
    """Live 2026-09-03: the permit gated a design CONSULTATION, and the agent keyed
    'not yet obtained' into the document field to get past it. A field scoped to
    some services binds only a booking for one of them; unscoped fields bind all."""
    from app import intake
    clean = intake.clean_fields(MASON)
    assert clean[2]["services"] == ["Installation day"] and "services" not in clean[0]
    assert intake.applies(clean[2], "installation day") is True        # case-insensitive
    assert intake.applies(clean[2], "Design consultation") is False
    assert intake.applies(clean[2], None) is False                     # unknown service → stands down
    assert intake.applies(clean[0], None) is True
    assert intake.required_labels(MASON, "Design consultation") == ["Type of visit", "Inscription wording"]
    assert intake.required_labels(MASON, "Installation day") == [
        "Type of visit", "Inscription wording", "Cemetery permit or church faculty", "Photos of the site"]
    base = {"Type of visit": "Design consultation", "Inscription wording": "IN LOVING MEMORY"}
    assert intake.missing_required(MASON, base, "Design consultation") == []
    assert intake.missing_required(MASON, base, "Installation day") == [
        "Cemetery permit or church faculty", "Photos of the site"]
    # a DOCUMENT is satisfied by a file, never by a promise
    inst = {**base, "Photos of the site": "plot-photos.png",
            "Cemetery permit or church faculty": "Not yet obtained — to apply after consultation"}
    assert intake.missing_required(MASON, inst, "Installation day") == ["Cemetery permit or church faculty"]
    inst["Cemetery permit or church faculty"] = "faculty-st-marys.pdf"
    assert intake.missing_required(MASON, inst, "Installation day") == []
    assert intake.looks_like_document("[Attachments: scan.jpeg]") is True
    assert intake.looks_like_document("yes - files came") is True
    assert intake.looks_like_document("to be supplied before installation") is False
    # scope survives the form round trip (one '|'-joined hidden per row) and the prompt says it
    fields = intake.parse_form(["A", "Permit"], ["1", "1"], ["", ""], ["0", "1"],
                               ["", "Installation day|Site survey"])
    assert "services" not in fields[0] and fields[1]["services"] == ["Installation day", "Site survey"]
    for block in (intake.render_for_prompt(MASON), intake.render_for_gather(MASON)):
        assert "ONLY FOR INSTALLATION DAY" in block
        assert "not before a design consultation" in block or "never ask for it" in block
    assert "never key 'not yet'" in intake.render_for_prompt(MASON)
    # legacy rows (no scope) render exactly as before
    assert "ONLY FOR" not in intake.render_for_prompt([{"label": "Pet", "required": True, "hint": ""}])


def test_booking_tools_carry_the_exact_detail_keys():
    """Kris 2026-09-04: a first-call key mismatch ('inscription_wording', 'phone',
    'notes') is a whole extra model turn — 26 of 31 gate refusals in a week. The
    tenant's exact labels ride every `details` argument description; the shared
    tool constants are never mutated."""
    from app import intake, scheduling
    fields = [{"label": "Full name", "required": True, "hint": ""},
              {"label": "Inscription wording & lettering style", "required": True, "hint": ""},
              {"label": intake.STAY_FIELD["label"], "required": True, "hint": ""}]
    line = intake.detail_keys_line(fields)
    assert "'Full name', 'Inscription wording & lettering style'" in line
    assert intake.STAY_FIELD["label"] not in line          # the stay flag is not a key
    assert intake.detail_keys_line([]) == ""
    before = scheduling.SERVICE_TOOLS[1]["input_schema"]["properties"]["details"]["description"]
    tools = intake.with_detail_keys(scheduling.SERVICE_TOOLS, fields)
    hold = next(t for t in tools if t["name"] == "hold_booking")
    assert hold["input_schema"]["properties"]["details"]["description"].endswith(
        "A key not in this list is ignored and will get the hold refused.")
    assert "'Inscription wording & lettering style'" in hold["input_schema"]["properties"]["details"]["description"]
    # constants untouched; tools without a details argument pass through as the same object
    assert scheduling.SERVICE_TOOLS[1]["input_schema"]["properties"]["details"]["description"] == before
    check = next(t for t in tools if t["name"] == "check_availability")
    assert check is next(t for t in scheduling.SERVICE_TOOLS if t["name"] == "check_availability")
    assert intake.with_detail_keys(scheduling.SERVICE_TOOLS, []) == list(scheduling.SERVICE_TOOLS)


def test_the_prompt_invites_an_optional_document_alongside_the_required_asks():
    # Live 2026-09-04 (farrier run): the passport was optional and scoped to the very
    # service being booked, and the first draft asked the optional access notes instead.
    p = intake.render_for_prompt([{"label": "Horse name", "required": True},
                                  {"label": "Passport", "required": False, "doc": True,
                                   "services": ["New-client assessment"]}])
    assert "An OPTIONAL document that applies to the service being booked still gets one line" in p
    assert "attach it if you have it" in p


def test_details_scoped_to_another_service_are_dropped_from_the_bag():
    fields = [{"label": "Horse name", "required": True},
              {"label": "Vet name and practice", "required": True,
               "services": ["Remedial/therapeutic shoeing"]}]
    bag = {"Horse name": "Murphy", "Vet name and practice": "Mendip Equine"}
    assert intake.drop_out_of_scope(fields, bag, "Full set of shoes") == {"Horse name": "Murphy"}
    assert intake.drop_out_of_scope(fields, bag, "Remedial/therapeutic shoeing") == bag
    assert intake.drop_out_of_scope(fields, bag, None) == bag          # scope unknown: keep
    assert intake.drop_out_of_scope(fields, {"vet_name_and_practice": "x", "Notes": "y"},
                                    "Trim") == {"Notes": "y"}          # loose key matching


def test_the_drafter_skips_the_diary_when_a_rule_forbids_the_booking():
    # Live 2026-09-04: two availability checks on a remedial request the vet-first
    # rule had already ruled out.
    from app import agent
    src = agent.build_system_prompt({}, scheduling=True)
    assert "EXCEPT when a business rule in your knowledge" in src
    assert "Then do NOT check the diary" in src


def test_a_service_name_with_a_comma_stays_one_scope_entry():
    """Live 2026-09-04 (hot tub run): "Drain, clean and refill" was split into
    two scope names and never matched its own bookings. The form joins with
    '|'; a comma only separates when nothing else does."""
    from app.intake import _clean_services
    assert _clean_services("Routine service|Drain, clean and refill|HSG282 water test") == [
        "Routine service", "Drain, clean and refill", "HSG282 water test"]
    assert _clean_services("Routine service, HSG282 water test") == [
        "Routine service", "HSG282 water test"]
    assert _clean_services("Repair call-out") == ["Repair call-out"]


@pytest.fixture(autouse=True)
def _established_booking_policy(monkeypatch):
    from app import db
    monkeypatch.setattr(db, "get_scheduling_config", lambda *a, **k: carried_config())
