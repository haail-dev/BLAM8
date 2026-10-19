"""The kennels run's open batch (docs/e2e-run-kennels-2026-09-13.md), built
2026-09-13 after the walk-through:

  (yy)  the Booking-requirements Dependants line names the kind when it knows it
  (aaa) stay_mode owns the collection time — never a second row asking for it
  (ccc) a help reply never links away from the step you are standing on
  (hhh) a dependant field carries its TYPE, so a date is a date
  (iii) a step whose page has no Save says what DOES move it on
  (ooo) a reworded duplicate knowledge note is still a duplicate
  (qqq) "All clear" never sits over an unskimmed before-BLAM8 backlog

Run: python -m pytest tests/test_run10_batch.py -q
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import knowledge_coverage, settings_recommend, setup_handoff
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ── (iii) a step with no Save says what moves it on ─────────────────────────
def test_the_crm_step_says_set_a_pipeline_not_save_this_page():
    steps = [{"key": "crm", "label": "CRM settings", "done": False,
              "move_on": "Set a pipeline to move on", "href": "/app/crm/settings"},
             {"key": "invoicing", "label": "Getting paid", "done": False,
              "href": "/app/invoices/settings"}]
    strip = setup_handoff.strip(steps, "/app/crm/settings", onboarded_at=None)
    assert strip["here"]["move_on"] == "Set a pipeline to move on"
    assert setup_handoff.strip(steps, "/app/invoices/settings", onboarded_at=None)["here"].get("move_on") is None
    shell = _read("src/app/templates/app/base.html")
    assert "{{ setup_strip.here.move_on or 'Save this page to move on' }}" in shell
    src = _read("src/app/setup_handoff.py")
    assert '"move_on": "Set a pipeline to move on",' in src
    # The one card that writes on press says so — the others fill a form.
    js = _read("src/app/static/concierge.js")
    assert '"applies at once — this page has no Save; anything not listed is removed"' in js


# ── (hhh) a dependant field carries its type ───────────────────────────────
def test_a_dependant_field_carries_its_type():
    spec = settings_recommend.PAGES["dependants"]["fields"]["field"]
    assert "Kind | Label | type | required or optional" in spec
    assert "TYPE is text, number, date, yes/no" in spec
    js = _read("src/app/static/concierge.js")
    assert 'pfSetField(row, "f_type", f.type)' in js
    from app import setup_rows
    assert setup_rows.parse("dependants", "field",
                            "Item | Date of birth | date | optional | once | no gate")["fields"]["type"] == "date"
    assert setup_rows.parse("dependants", "field",
                            "Item | Choice | bool | optional | once | no gate")["fields"]["type"] == "bool"
    from app import requirement_groups as rg
    assert rg.clean_field({"label": "Date of birth", "type": "date"})["type"] == "date"
    assert rg.clean_field({"label": "Neutered", "type": "bool"})["type"] == "bool"


# ── (aaa) the stay toggle owns the collection time ─────────────────────────
def test_stay_mode_drops_a_second_collection_row():
    vals = [{"name": "stay_mode", "value": "1"},
            {"name": "field", "value": "Drop-off date and time | required"},
            {"name": "field", "value": "Collection date and time | required"},
            {"name": "field", "value": "Dog name(s) | required"}]
    out = settings_recommend.clean("booking_details", vals)
    labels = [v["value"].split("|")[0].strip() for v in out if v["name"] == "field"]
    assert labels == ["Drop-off date and time", "Dog name(s)"]
    # Off, nothing is dropped — a day-visit diary really does need both rows.
    out = settings_recommend.clean("booking_details", [{"name": "stay_mode", "value": "0"}] + vals[1:])
    assert len([v for v in out if v["name"] == "field"]) == 3
    assert "never ALSO write a booking_details field row" in settings_recommend.PAGES["diary_settings"]["fields"]["stay_mode"]


# ── (ccc) no door out of the step you are standing on ──────────────────────
def test_a_help_reply_never_links_to_another_setup_step():
    steps = [{"key": "requirements", "label": "Booking requirements", "done": True,
              "href": "/app/diary/intake"},
             {"key": "dependants", "label": "Dependants", "done": False,
              "href": "/app/diary/dependants"}]
    back = {"label": "Open Booking details", "href": "/app/diary/intake"}
    assert setup_handoff.nav_allowed(back, steps, "/app/diary/dependants") is False
    assert setup_handoff.nav_allowed(back, steps, "/app/diary/intake") is True     # the page you are on
    assert setup_handoff.nav_allowed({"href": "/app/knowledge"}, steps, "/app/diary/dependants") is True
    assert setup_handoff.nav_allowed(None, steps, "/app/diary/dependants") is True
    done = [{**s, "done": True} for s in steps]
    assert setup_handoff.nav_allowed(back, done, "/app/diary/dependants") is True  # walk over, nothing filtered
    routes = _read("src/app/concierge_routes.py")
    assert "setup_handoff.nav_allowed(link," in routes and "return HelpResponse(reply=reply, link=link," in routes


# ── (ooo) a reworded duplicate is still a duplicate ────────────────────────
def test_a_reworded_duplicate_note_is_dropped():
    notes = [
        {"label": "Deposit", "content": "We take a 25% deposit to hold the dates, payable on booking."},
        {"label": "Deposits", "content": "A deposit of 25% is payable on booking to hold the dates."},
        {"label": "Vaccinations", "content": "Every dog must be up to date with kennel cough."},
    ]
    out = knowledge_coverage.clean_notes(notes)
    assert [n["label"] for n in out] == ["Deposit", "Vaccinations"]
    # The old label dedupe still holds, and genuinely different notes both live.
    assert len(knowledge_coverage.clean_notes(notes + [{"label": "Deposit", "content": "Anything"}])) == 2
    assert knowledge_coverage._same_note("open 7am to 6pm", "Open 7am to 6pm.") is True
    assert knowledge_coverage._same_note("We take a 25% deposit.", "Dogs need kennel cough cover.") is False


# ── (qqq) "All clear" over an unskimmed backlog ────────────────────────────
@pytest.fixture
def home(monkeypatch):
    from tests.test_home_dashboard import Env
    Env(monkeypatch, convos=[], modules={"scheduling"})
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {
        "name": "Little Paws", "industry": "kennels",
        "onboarded_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()})
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app, follow_redirects=False)
    app.dependency_overrides.clear()


def test_today_names_the_backlog_instead_of_all_clear(home, monkeypatch):
    from app import backlog
    monkeypatch.setattr(backlog, "count_rows", lambda *a, **k: 32)
    html = home.get("/app/").text
    assert "All clear — nothing waiting for you." not in html
    assert "Nothing new needs you — but your inbox came with history." in html
    assert "32 message" in html and "ignore any sender the agent should never answer" in html
    assert 'href="/app/conversations"' in html
    monkeypatch.setattr(backlog, "count_rows", lambda *a, **k: 0)
    dashboard.navcache.invalidate("t:t1")
    assert "All clear — nothing waiting for you." in home.get("/app/").text


def test_an_established_account_is_never_told_about_its_backlog(monkeypatch):
    from tests.test_home_dashboard import Env
    from app import backlog
    Env(monkeypatch, convos=[], modules={"scheduling"})
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {
        "name": "Little Paws",
        "onboarded_at": (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()})
    monkeypatch.setattr(backlog, "count_rows", lambda *a, **k: 32)
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t2", client=None)
    try:
        html = TestClient(app, follow_redirects=False).get("/app/").text
        assert "All clear — nothing waiting for you." in html and "came with history" not in html
    finally:
        app.dependency_overrides.clear()


# ── (yy) the requirements line names the kind ──────────────────────────────
def test_the_requirements_line_names_the_kind_when_it_knows_it():
    page = _read("src/app/templates/app/diary_intake.html")
    assert "{% if dep_kind %}Things to know about each {{ dep_kind }}" in page
    assert "the rows here are what changes each visit." in page
    assert "a person, item or other named subject?" in page          # still generic when unknown
    dash = _read("src/app/dashboard.py")
    assert "service_names=service_names, dep_kind=dep_kind," in dash
    assert 'setup_held.for_page(setup_held.load(ctx.tenant_id, client=ctx.client), "dependants")' in dash
