"""One flow of nine (Kris, kennels run #10, 2026-09-13): "we've shown/done
four steps. Now we're about to move into the application and in theory the
user can just ignore the next 5 steps… the next 5 steps should really be
incorporated into the onboarding we've just gone through."

Pins:
- setup_handoff.strip: positions count the wizard's four, "here" is read
  from the page path, done pages offer Next, a fresh tenant cannot hide it;
- the wizard's last button says "Next: <first tuning step>" and Finish lands
  on that step's page, not Today;
- the shell strip rides every app page; the widget strip has no ×; the first
  in-app message no longer says "Setup's done".

Run: python -m pytest tests/test_one_flow.py -q
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import onboarding, setup_handoff
from app.auth import TenantCtx, require_tenant
from app.main import app
from app.security import require_csrf
from tests.knowledge_review_fixture import review_store

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


STEPS = [
    {"key": "diary", "label": "Diary & scheduling settings", "done": True, "href": "/app/diary/settings"},
    {"key": "requirements", "label": "Booking requirements", "done": False, "href": "/app/diary/intake"},
    {"key": "dependants", "label": "Dependants", "done": False, "href": "/app/diary/dependants"},
    {"key": "crm", "label": "CRM settings", "done": False, "href": "/app/crm/settings"},
    {"key": "invoicing", "label": "Getting paid", "done": False, "href": "/app/invoices/settings"},
]
NOW = datetime(2026, 9, 13, 12, tzinfo=timezone.utc)


def test_strip_counts_the_wizard_and_reads_here_from_the_path():
    s = setup_handoff.strip(STEPS, "/app/", onboarded_at=NOW, now=NOW)
    assert (s["total"], s["done"]) == (9, 5)
    assert s["next"]["key"] == "requirements" and s["here"] is None and s["pos"] is None
    assert s["can_hide"] is False                                    # fresh: no dismiss

    on_step = setup_handoff.strip(STEPS, "/app/diary/intake", onboarded_at=NOW, now=NOW)
    assert on_step["here"]["key"] == "requirements" and on_step["pos"] == 6
    assert on_step["after"]["key"] == "dependants" and on_step["here_done"] is False

    on_done = setup_handoff.strip(STEPS, "/app/diary/settings", onboarded_at=NOW, now=NOW)
    assert on_done["pos"] == 5 and on_done["here_done"] is True
    assert on_done["next"]["key"] == "requirements"                  # Next offered from a done page


def test_strip_is_none_when_all_done_or_hidden_and_older_tenants_may_hide():
    all_done = [dict(s, done=True) for s in STEPS]
    assert setup_handoff.strip(all_done, "/app/", onboarded_at=NOW, now=NOW) is None
    assert setup_handoff.strip(STEPS, "/app/", onboarded_at=NOW, dismissed_at="t", now=NOW) is None
    old = setup_handoff.strip(STEPS, "/app/", onboarded_at=NOW - timedelta(days=30), now=NOW)
    assert old and old["can_hide"] is True
    assert setup_handoff.strip([], "/app/") is None


def test_the_shell_carries_the_strip_and_busts_it_on_every_save():
    html = _read("src/app/templates/app/base.html")
    assert "data-setup-strip" in html and "data-setup-next" in html
    assert "Setup · {{ setup_strip.done }} of {{ setup_strip.total }} done" in html
    assert "Save this page to move on" in html
    assert 'action="/app/setup/hide"' in html and "setup_strip.can_hide" in html
    src = _read("src/app/dashboard.py")
    assert '"setup_strip_raw": _setup_strip_ctx(ctx, tenant),' in src
    assert "_bust_setup_strip(ctx)" in src.split("def _held_used")[1].split("def _ctx")[0]
    assert '@router.post("/setup/hide"' in src


# ── the wizard's last button ─────────────────────────────────────────────────
@pytest.fixture
def client():
    app.dependency_overrides[require_tenant] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_finish_lands_on_the_first_open_tuning_step(client, monkeypatch, review_store):
    monkeypatch.setattr(onboarding.db, "set_onboarded", lambda *a, **k: None)
    monkeypatch.setattr(onboarding.db, "upsert_tenant_config", lambda *a, **k: None)
    monkeypatch.setattr(setup_handoff, "load_steps", lambda tid, client=None: list(STEPS))
    assert review_store.rows == []  # completion is permitted only after pending review is checked
    r = client.post("/onboarding/knowledge/text", data={"label": "", "content": "", "then": "continue"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/app/diary/intake"              # not /app/
    monkeypatch.setattr(setup_handoff, "load_steps", lambda tid, client=None: [dict(s, done=True) for s in STEPS])
    r = client.post("/onboarding/knowledge/text", data={"label": "", "content": "", "then": "continue"},
                    follow_redirects=False)
    assert r.headers["location"] == "/app/"                          # nothing left → Today


def test_knowledge_button_says_next_with_the_first_step(client, monkeypatch, review_store):
    from app import knowledge as kb
    monkeypatch.setattr(kb, "is_configured", lambda: False)
    monkeypatch.setattr(onboarding.db, "list_knowledge_sources", lambda tid, client=None: [])
    monkeypatch.setattr(onboarding.db, "knowledge_source_texts", lambda tid, client=None: [])
    monkeypatch.setattr(onboarding.db, "get_tenant_config", lambda tid, client=None: {})
    monkeypatch.setattr(setup_handoff, "load_steps", lambda tid, client=None: [dict(STEPS[0], done=False)] + STEPS[1:])
    r = client.get("/onboarding/knowledge")
    line = next(l for l in r.text.splitlines() if "data-onb-continue" in l)
    assert ">Next: Diary &amp; scheduling settings</button>" in line or "Next: Diary" in r.text
    assert "Finish setup</button>" not in r.text


def test_load_steps_never_raises(monkeypatch):
    from app import db
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "active_modules", boom)
    assert setup_handoff.load_steps("t1") == []
    assert setup_handoff.first_open([]) is None


def test_explicitly_disabled_bookings_complete_diary_setup_but_remain_unbookable():
    from app import booking_policy
    review = {key: {"state": "off", "review_status": "confirmed"}
              for key in booking_policy.FIELDS}
    cfg = {"booking_setting_review": review}

    policy = booking_policy.compile_policy(cfg)
    assert policy["ready"] is False
    assert policy["unresolved_settings"] == list(booking_policy.FIELDS)

    steps = setup_handoff.steps({"scheduling"}, cfg, [], 14, {})
    assert steps[0]["done"] is True
    assert steps[0]["status"] == "Bookings explicitly disabled"


def test_one_confirmed_off_requirement_resolves_setup_without_enabling_booking():
    from app import booking_policy
    cfg = {"booking_setting_review": {
        "capacity": {"state": "off", "review_status": "confirmed"},
    }}

    policy = booking_policy.compile_policy(cfg)
    assert policy["ready"] is False

    steps = setup_handoff.steps({"scheduling"}, cfg, [], 14, {})
    assert steps[0]["done"] is True
    assert steps[0]["status"] == "Bookings explicitly disabled"


def test_the_widget_counts_nine_and_has_no_dismiss():
    js = _read("src/app/static/concierge.js")
    assert "var WIZARD_STEPS = 4;" in js
    assert setup_handoff.WIZARD_STEPS == 4
    assert '"step " + (WIZARD_STEPS + doneCount + 1) + " of " + (WIZARD_STEPS + all.length)' in js
    assert '"Setup · " + (WIZARD_STEPS + done) + " of " + (WIZARD_STEPS + steps.length) +' in js
    assert '" done · next: step " + (WIZARD_STEPS + done + 1) + " of " + (WIZARD_STEPS + steps.length)' in js
    strip = js.split("function paintSetupCta()")[1].split("async function fetchSetupStatus")[0]
    assert "setup-dismiss" not in strip and 'x.textContent = "×"' not in strip
    assert "Setup's done" not in js
    assert 'diary: "Four steps done — {TOTAL} to go' in js


def test_the_wizard_stepper_draws_all_nine():
    """Kris (re-run): "it's still looking like 4 steps and we're done." The
    wizard's stepper carries the five tuning steps greyed as upcoming, the
    bar and the phone line count out of nine."""
    ctx = onboarding._step_ctx("interview")
    assert [t["label"] for t in ctx["tuning"]] == ["Diary", "Requirements", "Dependants", "CRM", "Getting paid"]
    assert ctx["step_count"] + len(ctx["tuning"]) == setup_handoff.WIZARD_STEPS + 5
    html = _read("src/app/templates/onboarding/base.html")
    assert 'data-stepper-total="{{ total }}"' in html
    assert 'data-tuning-step="{{ key }}"' in html and 'key=t.key' in html
    assert "step_circle(step_count + loop.index, t.label" in html                  # numbered 5..9
    assert 'class="onboarding-progress"' in html
    assert "--label-start" in html and "--label-end" in html
    assert "label.offsetWidth" in html and "getBoundingClientRect()" in html
    assert "data-step-line" not in html and "aria-controls=\"onboarding-step-label-{{ number }}\"" in html


def test_settings_pages_drop_cross_step_links_during_the_walk():
    """Kris (re-run, 2026-09-13): the diary page's "lives in Booking
    requirements" link "is just allowing the user to skip… the top of the
    page tells you the next step"."""
    assert '"setup_walk_active": _setup_walk_active(ctx, tenant),' in _read("src/app/dashboard.py")
    assert "{% if not fleet_mode and not setup_walk_active %}" in _read("src/app/templates/app/_scheduling_settings.html")
    assert "{% if setup_walk_active %}<span class=\"font-medium\">Dependants</span> — the next step" in _read("src/app/templates/app/diary_intake.html")
    assert "{% if setup_walk_active %}<span class=\"font-medium\">Booking requirements</span>" in _read("src/app/templates/app/diary_dependants.html")
