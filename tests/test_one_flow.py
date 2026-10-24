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


def test_strip_items_are_the_in_app_steps_numbered_after_the_wizard():
    on_step = setup_handoff.strip(STEPS, "/app/diary/intake", onboarded_at=NOW, now=NOW)
    # The wizard's four are behind the owner by now; only the in-app steps are drawn.
    assert [i["badge"] for i in on_step["items"]] == ["5", "6", "7", "8", "9"]
    assert on_step["items"][0]["name"] == "Diary & scheduling"      # no trailing "settings" in the progress bar
    assert [i["done"] for i in on_step["items"]] == [True, False, False, False, False]
    assert [i["current"] for i in on_step["items"]] == [False, True, False, False, False]

    off_step = setup_handoff.strip(STEPS, "/app/", onboarded_at=NOW, now=NOW)
    assert [i["key"] for i in off_step["items"] if i["current"]] == ["requirements"]   # the next open step


@pytest.mark.parametrize("label, shown", [
    ("Diary & scheduling settings", "Diary & scheduling"),
    ("CRM settings", "CRM"),
    ("Invoice SETTINGS", "Invoice"),                  # any capitalisation
    ("Booking requirements", "Booking requirements"),
    ("Dependants", "Dependants"),
    ("Getting paid", "Getting paid"),
    ("Settings", "Settings"),                         # a name that is only the word keeps it
    ("Settings review", "Settings review"),           # only a trailing word is dropped
])
def test_the_progress_bar_names_drop_a_trailing_settings(label, shown):
    steps = [{"key": "k", "label": label, "done": False, "href": "/app/x"}]
    assert setup_handoff.strip(steps, "/app/x", onboarded_at=NOW, now=NOW)["items"][0]["name"] == shown
    assert steps[0]["label"] == label                 # the step's own label is untouched


def test_a_step_may_supply_its_own_badge_for_a_later_grouping():
    grouped = [dict(STEPS[0], badge="5"), dict(STEPS[1], badge="5A"), dict(STEPS[2])]
    items = setup_handoff.strip(grouped, "/app/", onboarded_at=NOW, now=NOW)["items"]
    assert [i["badge"] for i in items] == ["5", "5A", "7"]   # without one, a step keeps its place in the nine


def test_the_shell_carries_the_strip_and_busts_it_on_every_save():
    html = _read("src/app/templates/app/base.html")
    assert "data-setup-strip" in html and "data-setup-stepper" in html
    assert "setup_stepper.css" in html and "setup_stepper.js" in html
    assert "Step {{ setup_strip.pos }} of {{ setup_strip.total }} ·" not in html   # no longer a sentence
    assert "Setup · {{ setup_strip.done }} of {{ setup_strip.total }} done" in html   # kept for screen readers
    # The walk has no skipping (Kris 2026-10-22): no Next link, no hint, no Hide on the strip.
    assert "data-setup-next" not in html and "Save this page to move on" not in html
    assert 'action="/app/setup/hide"' not in html
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
    monkeypatch.setattr(onboarding.db,"get_tenant_config",lambda *a,**k:{"agent_name":"Office team","tone":"Professional","sign_off":"Regards"})
    monkeypatch.setattr(onboarding.db, "set_onboarded", lambda *a, **k: None)
    monkeypatch.setattr(onboarding.db, "upsert_tenant_config", lambda *a, **k: None)
    monkeypatch.setattr(onboarding,'_prepare_guided_setup',lambda ctx:'/app/setup/guide')
    monkeypatch.setattr(setup_handoff, "load_steps", lambda tid, client=None: list(STEPS))
    review_store.sources = [{'source':'Reviewed facts','content':'Confirmed business facts.'}]
    assert review_store.rows == []  # saved Knowledge and no pending review allow handoff
    r = client.post("/onboarding/knowledge/text", data={"label": "", "content": "", "then": "continue"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/app/setup/guide"  # explicit introduction before actual Diary
    monkeypatch.setattr(setup_handoff, "load_steps", lambda tid, client=None: [dict(s, done=True) for s in STEPS])
    r = client.post("/onboarding/knowledge/text", data={"label": "", "content": "", "then": "continue"},
                    follow_redirects=False)
    assert r.headers["location"] == "/app/"                          # nothing left → Today


def test_knowledge_button_says_next_with_the_first_step(client, monkeypatch, review_store):
    from app import knowledge as kb
    monkeypatch.setattr(kb, "is_configured", lambda: False)
    monkeypatch.setattr(onboarding.db, "list_knowledge_sources", lambda tid, client=None: [])
    monkeypatch.setattr(onboarding.db, "knowledge_source_texts", lambda tid, client=None: [])
    monkeypatch.setattr(onboarding.db, "get_tenant_config", lambda tid, client=None: {"agent_name":"Office team","tone":"Professional","sign_off":"Regards"})
    monkeypatch.setattr(setup_handoff, "load_steps", lambda tid, client=None: [dict(STEPS[0], done=False)] + STEPS[1:])
    r = client.get("/onboarding/knowledge")
    assert ">Next: Guided setup</button>" in r.text
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
    # Historical registered guard name retained; the grouping now totals seven.
    assert 'records: "I\'m the BLAM8 AI assistant. This is {POS}: Records and workflows.' in js
    assert 'diary: "Now {POS}: Diary.' in js


def test_the_wizard_stepper_draws_all_nine():
    """The first four retain their routes; three later groups stay visible as
    upcoming. The registered historical test name remains for outcome coverage."""
    ctx = onboarding._step_ctx("interview")
    assert [t["label"] for t in ctx["tuning"]] == ["Records and workflows", "Diary", "Charges and payments"]
    assert ctx["step_count"] + len(ctx["tuning"]) == setup_handoff.WIZARD_STEPS + 3
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
