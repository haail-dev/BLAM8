"""Post-onboarding guided-settings hand-off (src/app/setup_handoff.py, Julius
2026-08-02): the concierge widget keeps guiding after the wizard — a chip strip
walks a fresh tenant through Diary → CRM → Invoice settings, reusing the
existing "Help me set this up" recommend flows (nothing ever applies from the
strip; saving stays each page's own Save).

What this pins:
  - the pure gates: freshness window, CRM "made it their own" detection
    (default seed ≠ configured), invoice details filled, the step order and
    module gating
  - GET /concierge/setup-status: gathers rows, degrades to show=false, honours
    the dismiss stamp and the freshness window
  - POST /concierge/setup-dismiss: stamps tenant_config so the strip stays
    gone on every device
  - help mode can point at Invoice settings (NAV destination)
  - the widget JS + greeting contract (EU AI Act: the opening message says
    "AI assistant" — it can be a thread's first message)

Run: python -m pytest tests/test_setup_handoff.py -q
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.concierge_routes as concierge_routes
from app import concierge, setup_handoff
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf_header

ROOT = Path(__file__).resolve().parents[1]

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
FRESH = (NOW - timedelta(days=2)).isoformat()
STALE = (NOW - timedelta(days=setup_handoff.FRESH_DAYS + 1)).isoformat()


# ── freshness ────────────────────────────────────────────────────────────────
def test_is_fresh_window_and_garbage():
    assert setup_handoff.is_fresh(FRESH, now=NOW)
    assert setup_handoff.is_fresh(FRESH.replace("+00:00", "Z"), now=NOW)   # Supabase Z-suffix
    assert not setup_handoff.is_fresh(STALE, now=NOW)
    assert not setup_handoff.is_fresh(None, now=NOW)
    assert not setup_handoff.is_fresh("not-a-date", now=NOW)
    # naive timestamps are treated as UTC, not an error
    assert setup_handoff.is_fresh("2026-08-01T12:00:00", now=NOW)


# ── CRM configured detection ─────────────────────────────────────────────────
def test_crm_configured_means_any_stage_exists():
    # Since 2026-09-02 nothing seeds stages, so ANY stage was chosen — either
    # applied from the assistant's recommendation or added by hand.
    assert not setup_handoff.crm_configured([], None)          # never touched
    assert not setup_handoff.crm_configured(None, None)
    # crm_quiet_days is NOT NULL DEFAULT 14 on every tenant_config row — the
    # default value proves nothing (live e2e edge, 2026-08-02)
    assert not setup_handoff.crm_configured([], setup_handoff.DEFAULT_CRM_QUIET_DAYS)
    assert setup_handoff.crm_configured([], 21)                # threshold changed
    assert setup_handoff.crm_configured([{"name": "New enquiry"}], None)
    # Garbage rows without a name still count as untouched.
    assert not setup_handoff.crm_configured([{"name": "  "}, "junk"], None)


def test_invoice_configured_needs_identity_or_bank():
    assert not setup_handoff.invoice_configured(None)
    assert not setup_handoff.invoice_configured({"business_name": "  ", "bank_account_number": ""})
    assert setup_handoff.invoice_configured({"business_name": "Deane & Sons"})
    assert setup_handoff.invoice_configured({"bank_account_number": "12345678"})


# ── steps: order, gating, done flags ─────────────────────────────────────────
def test_steps_order_and_module_gating():
    # Booking requirements = step 1.5 (Julius, 2026-09-02): right after the
    # diary (whose bookings it describes), before the CRM.
    steps = setup_handoff.steps({"scheduling", "crm"}, None, [], None, None)
    # Dependants = step 2.5 (Kris, 2026-09-04): what a booking is about,
    # right after the checklist it belongs beside.
    assert [s["key"] for s in steps] == ["diary", "requirements", "dependants", "crm", "invoicing"]
    assert all(not s["done"] for s in steps)
    # zero-initiative: every step NAVIGATES to its settings page (the on-page
    # "Help me set this up" flow takes over from there)
    assert [s["href"] for s in steps] == \
        ["/app/diary/settings", "/app/diary/intake", "/app/diary/dependants",
         "/app/crm/settings", "/app/invoices/settings"]
    # no diary-backed or crm module → those steps disappear; invoicing is core
    assert [s["key"] for s in setup_handoff.steps(set(), None, [], None, None)] == ["invoicing"]
    # tms counts as a diary module
    assert [s["key"] for s in setup_handoff.steps({"tms"}, None, [], None, None)] == \
        ["diary", "requirements", "dependants", "invoicing"]


def test_steps_done_flags_follow_the_rows():
    steps = setup_handoff.steps(
        {"scheduling", "crm"},
        carried_config({"open_time": "08:00", "intake_configured_at": "2026-09-01T09:00:00+00:00"}),
        [], 30, {"business_name": "Deane & Sons"},
        dependant_groups=[{"label": "Horse"}])
    assert [s["done"] for s in steps] == [True, True, True, True, True]
    # A diary row saved but intake never activated → ONLY requirements undone.
    part = setup_handoff.steps({"scheduling", "crm"}, carried_config({"open_time": "08:00"}),
                               [], 30, {"business_name": "Deane & Sons"})
    assert [(s["key"], s["done"]) for s in part if not s["done"]] == \
        [("requirements", False), ("dependants", False)]
    # Run #8: the page answered with no kind (a driving pupil IS the customer)
    # — the stamp counts the step done without a group.
    answered = setup_handoff.steps({"scheduling", "crm"},
                                   carried_config({"open_time": "08:00", "intake_configured_at": "x",
                                    "dependants_configured_at": "2026-09-05T17:00:00+00:00"}),
                                   [], 30, {"business_name": "Deane & Sons"}, dependant_groups=[])
    assert [(s["key"], s["done"]) for s in answered][:3] == \
        [("diary", True), ("requirements", True), ("dependants", True)]


def test_show_gate():
    todo = [{"key": "diary", "done": False}]
    done = [{"key": "diary", "done": True}]
    assert setup_handoff.show(FRESH, None, todo, now=NOW)
    assert not setup_handoff.show(FRESH, "2026-08-02T09:00:00+00:00", todo, now=NOW)
    assert not setup_handoff.show(STALE, None, todo, now=NOW)
    assert not setup_handoff.show(FRESH, None, done, now=NOW)
    assert not setup_handoff.show(FRESH, None, [], now=NOW)


# ── the routes ───────────────────────────────────────────────────────────────
@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf_header] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


class Env:
    def __init__(self, monkeypatch, *, onboarded_at=None, tcfg=None, modules=frozenset(),
                 cfg=None, stages=(), invoice=None, groups=()):
        self.tcfg_written = {}
        d = concierge_routes.db
        monkeypatch.setattr(d, "list_requirement_groups", lambda *a, **k: [dict(g) for g in groups])
        monkeypatch.setattr(d, "get_tenant", lambda *a, **k: {"onboarded_at": onboarded_at})
        monkeypatch.setattr(d, "get_tenant_config", lambda *a, **k: dict(tcfg or {}))
        monkeypatch.setattr(d, "active_modules", lambda *a, **k: set(modules))
        monkeypatch.setattr(d, "get_scheduling_config", lambda *a, **k: carried_config(cfg) if cfg else None)
        monkeypatch.setattr(d, "crm_list_stages", lambda *a, **k: [dict(s) for s in stages])
        monkeypatch.setattr(d, "get_invoice_settings", lambda *a, **k: invoice)
        monkeypatch.setattr(d, "upsert_tenant_config",
                            lambda tid, patch, **kw: self.tcfg_written.update(patch))


def test_setup_status_fresh_unconfigured_tenant_shows_all_steps(client, monkeypatch):
    fresh = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    Env(monkeypatch, onboarded_at=fresh, modules={"scheduling", "crm"})
    data = client.get("/concierge/setup-status").json()
    assert data["show"] is True
    assert [s["key"] for s in data["steps"]] == \
        ["diary", "requirements", "dependants", "crm", "invoicing"]
    assert all(not s["done"] for s in data["steps"])


def test_setup_status_honours_dismiss_and_staleness(client, monkeypatch):
    fresh = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    Env(monkeypatch, onboarded_at=fresh,
        tcfg={"settings_handoff_dismissed_at": "2026-08-02T09:00:00+00:00"})
    assert client.get("/concierge/setup-status").json() == {"show": False, "steps": []}
    stale = (datetime.now(timezone.utc)
             - timedelta(days=setup_handoff.FRESH_DAYS + 1)).isoformat()
    Env(monkeypatch, onboarded_at=stale, modules={"scheduling"})
    assert client.get("/concierge/setup-status").json()["show"] is False


def test_setup_status_all_configured_stops_showing(client, monkeypatch):
    fresh = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    Env(monkeypatch, onboarded_at=fresh, modules={"scheduling", "crm"},
        tcfg={"crm_quiet_days": 30},
        cfg={"open_time": "08:00",
             "intake_configured_at": "2026-09-01T09:00:00+00:00"},
        invoice={"business_name": "Deane & Sons"}, groups=[{"label": "Horse"}])
    data = client.get("/concierge/setup-status").json()
    assert data["show"] is False
    # steps still reported with their done flags (the widget just won't render)
    assert [s["done"] for s in data["steps"]] == [True, True, True, True, True]


def test_setup_status_degrades_to_hidden_on_db_error(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(concierge_routes.db, "get_tenant", boom)
    assert client.get("/concierge/setup-status").json() == {"show": False, "steps": []}


def test_setup_dismiss_stamps_tenant_config(client, monkeypatch):
    env = Env(monkeypatch)
    assert client.post("/concierge/setup-dismiss").json() == {"ok": True}
    stamp = env.tcfg_written.get("settings_handoff_dismissed_at")
    assert stamp and datetime.fromisoformat(stamp).tzinfo is not None


# ── the help prompt knows an untouched CRM seed isn't "set up" ───────────────
def test_help_prompt_flags_default_crm_seed():
    tenant = {"name": "T", "crm": {"stages": [{"name": "Prospect", "stage_type": "active"}],
                                   "quiet_days": 14, "default_seed": True}}
    p = concierge.build_help_prompt(tenant=tenant)
    assert "UNTOUCHED DEFAULTS" in p
    tenant["crm"]["default_seed"] = False
    assert "UNTOUCHED DEFAULTS" not in concierge.build_help_prompt(tenant=tenant)


# ── help mode can point at Invoice settings ──────────────────────────────────
def test_invoice_settings_nav_destination():
    dest = concierge.NAV_DESTINATIONS["invoice_settings"]
    assert dest["href"] == "/app/invoices/settings"
    assert "invoice_settings" in concierge.HELP_SCHEMA["properties"]["nav"]["enum"]


# ── UI contract wiring (the widget side) ─────────────────────────────────────
def test_widget_js_wires_the_handoff():
    js = (ROOT / "src/app" / "static" / "concierge.js").read_text(encoding="utf-8")
    # 2026-09-13 (one flow of nine): the widget strip has NO dismiss — the
    # five tuning steps are the second half of setup; the endpoint survives
    # for /app/setup/hide-style callers but the widget never posts it.
    assert "/concierge/setup-dismiss" not in js
    for needle in ("/concierge/setup-status",
                   "paintSetupCta", "maybeSetupGreet", "nextSetupStep",
                   "Finish tuning your settings"):
        assert needle in js, needle
    # EU AI Act Art 50(1): the diary message opens the first in-app thread, so
    # it must disclose the AI itself — but ONLY when the thread is genuinely
    # empty (Julius 2026-09-03: the panel carries the onboarding conversation
    # over, and re-introducing mid-thread reads robotic).
    assert "I'm the BLAM8 AI assistant" in js.split("SETUP_MESSAGES")[1].split("};")[0]
    assert 'if (log.length) msg = msg.replace("I\'m the BLAM8 AI assistant. ", "")' in js
    # The copy never instructs a second "Help me set this up" press — arriving
    # on the step page auto-starts the flow (maybeSetupAutoAsk).
    setup_block = js.split("SETUP_MESSAGES")[1].split("};")[0]
    assert "then press" not in setup_block
    assert "maybeSetupAutoAsk" in js


def test_schema_carries_the_dismiss_stamp():
    sql = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    assert "settings_handoff_dismissed_at" in sql


def test_the_auto_ask_stamps_only_after_the_assistant_spoke():
    # Live 2026-09-04: the step key was stamped before the request; the reply came
    # back empty (an empty bubble) and the step could never ask again.
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "src/app" / "static" / "concierge.js").read_text(encoding="utf-8")
    fn = js.split("function maybeSetupAutoAsk()")[1].split("async function askSilently")[0]
    assert "askSilently(btn.getAttribute(\"data-concierge-ask\")).then(function (ok)" in fn
    assert fn.index("askSilently(") < fn.index("sessionStorage.setItem(KEY_SETUP_AUTOASK")
    turn = js.split("async function helpTurn(")[1].split("// ── fix mode")[0]
    assert 'throw new Error("empty reply")' in turn and "return spoke;" in turn


def test_the_wrap_up_hands_over_to_today():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "src/app" / "static" / "concierge.js").read_text(encoding="utf-8")
    wrap = js.split("var doneMsg = ")[1].split("Past the hand-off")[0]
    assert 'appendNavLink({ href: "/app/", label: "Go to Today' in wrap
    assert 'window.location.href = "/app/"' in wrap


def test_the_dependants_step_has_its_message_and_its_page_ask():
    js = (ROOT / "src/app" / "static" / "concierge.js").read_text(encoding="utf-8")
    block = js.split("SETUP_MESSAGES")[1].split("};")[0]
    assert "dependants:" in block and "what a booking is about" in block
    html = (ROOT / "src/app" / "templates" / "app" / "diary_dependants.html").read_text(encoding="utf-8")
    assert 'data-concierge-ask="Set up my dependants' in html


def test_the_assistants_no_kind_answer_closes_the_dependants_step(client, monkeypatch):
    """Run #8 (Kris: "if no kind applies it shouldn't be offered as a step"):
    the help route stamps the step when the assistant's dependants card
    carries no group — a ladder on the customer, or nothing at all — and
    leaves it alone when the card proposes a kind."""
    from types import SimpleNamespace
    import app.concierge_routes as routes
    stamped = []
    monkeypatch.setattr(routes.db, "mark_dependants_configured", lambda t, client=None: stamped.append(t))
    monkeypatch.setattr(routes.usage, "record_anthropic", lambda *a, **k: None)
    monkeypatch.setattr(routes, "_tenant_context", lambda ctx: {})
    from app.auth import require_tenant, TenantCtx
    from app.main import app
    app.dependency_overrides[require_tenant] = lambda: TenantCtx(tenant_id="t1", client=None)

    def fake(recommend):
        async def answer_help(question, history, tenant=None, tenant_id=None):
            return SimpleNamespace(reply="ok", link=None, submit=None, recommend=recommend,
                                   model="m", usage=None)
        return answer_help

    monkeypatch.setattr(routes.concierge, "answer_help", fake([
        {"page": "dependants", "label": "Dependants", "href": "/app/diary/dependants",
         "values": [{"name": "ladder", "value": "customer | DVSA | A, B | approve"}]}]))
    r = client.post("/concierge/help", json={"question": "set up my dependants", "history": []})
    assert r.status_code == 200 and stamped == ["t1"]
    monkeypatch.setattr(routes.concierge, "answer_help", fake([
        {"page": "dependants", "label": "Dependants", "href": "/app/diary/dependants",
         "values": [{"name": "group", "value": "Horse | Horses | several"}]}]))
    client.post("/concierge/help", json={"question": "set up my dependants", "history": []})
    assert stamped == ["t1"]                     # a kind proposed: the owner still saves it
    app.dependency_overrides.pop(require_tenant, None)


def test_the_dependants_save_stamps_the_step_even_with_no_kind(client, monkeypatch):
    import app.dashboard as dashboard
    monkeypatch.setattr(dashboard.db, "list_requirement_groups", lambda t, client=None: [])
    monkeypatch.setattr(dashboard.db, "save_requirement_groups", lambda t, groups, client=None: groups)
    stamped = []
    monkeypatch.setattr(dashboard.setup_forms, "save", lambda ctx,page,expected,ops:
                        stamped.append(ctx.tenant_id) if ops[-1]['fields']['dependants_configured_at'] else None)
    monkeypatch.setattr(dashboard.db, "crm_list_field_definitions", lambda t, entity=None, client=None: [])
    monkeypatch.setattr(dashboard.db, "list_requirement_instances", lambda *a, **k: [])
    from app.auth import require_onboarded, TenantCtx
    from app.main import app
    from app.security import require_csrf
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    try:
        r = client.post("/app/diary/dependants", data={"settings_snapshot":__import__("json").dumps({"page":"dependants","config":{},"definitions":[],"groups":[],"instances":[]})}, follow_redirects=False)
        assert r.status_code == 303 and stamped == ["t1"]
    finally:
        app.dependency_overrides.pop(require_csrf, None)



def test_the_prose_no_dependants_verdict_closes_the_step_and_names_it(client, monkeypatch):
    """Run #9 (2026-09-05, Brightwell): the assistant's verdict arrived as PROSE
    ("this step doesn't apply — skip to step 4") with no card, so nothing was
    stamped; the strip offered "Dependants →" at 4/5 and 5/5, the wrap-up never
    fired and the only exit was ×. The structured `no_dependants` flag closes
    the step, the response names it so the panel refetches the walk-through,
    and a proposed kind still wins over the flag."""
    from types import SimpleNamespace
    import app.concierge_routes as routes
    from app import concierge
    stamped = []
    monkeypatch.setattr(routes.db, "mark_dependants_configured", lambda t, client=None: stamped.append(t))
    monkeypatch.setattr(routes.usage, "record_anthropic", lambda *a, **k: None)
    monkeypatch.setattr(routes, "_tenant_context", lambda ctx: {})
    from app.auth import require_tenant, TenantCtx
    from app.main import app
    app.dependency_overrides[require_tenant] = lambda: TenantCtx(tenant_id="t1", client=None)

    def fake(recommend, flag):
        async def answer_help(question, history, tenant=None, tenant_id=None):
            return SimpleNamespace(reply="Nothing of the sort applies.", link=None, submit=None,
                                   recommend=recommend, model="m", usage=None, no_dependants=flag)
        return answer_help
    try:
        monkeypatch.setattr(routes.concierge, "answer_help", fake(None, True))
        r = client.post("/concierge/help", json={"question": "set up my dependants", "history": []})
        assert r.status_code == 200 and stamped == ["t1"]
        assert r.json()["closed_step"] == "dependants"
        monkeypatch.setattr(routes.concierge, "answer_help", fake(None, False))
        r = client.post("/concierge/help", json={"question": "how do I invoice?", "history": []})
        assert stamped == ["t1"] and r.json()["closed_step"] is None
        monkeypatch.setattr(routes.concierge, "answer_help", fake([
            {"page": "dependants", "label": "Dependants", "href": "/app/diary/dependants",
             "values": [{"name": "group", "value": "Horse | Horses | several"}]}], True))
        r = client.post("/concierge/help", json={"question": "set up my dependants", "history": []})
        assert stamped == ["t1"] and r.json()["closed_step"] is None   # a kind on the table wins
    finally:
        app.dependency_overrides.pop(require_tenant, None)
    # the flag is part of the structured reply, the prompt tells the model when to set it,
    # and only a literal true reads as the verdict
    assert "no_dependants" in concierge.HELP_SCHEMA["required"]
    prompt = concierge.build_help_prompt(facts="", knowledge="")
    assert "Set no_dependants=true only when the owner explicitly says" in prompt and "while proposing a subject kind" in prompt
    assert concierge._help_no_dependants('{"reply": "x", "no_dependants": true}') is True
    assert concierge._help_no_dependants('{"reply": "x", "no_dependants": "true"}') is False
    assert concierge._help_no_dependants("not json") is False
    # the panel refetches the walk-through when a turn names a closed step
    js = (ROOT / "src/app" / "static" / "concierge.js").read_text(encoding="utf-8")
    help_turn = js.split("async function helpTurn")[1].split("async function fixTurn")[0]
    assert "closedStep = data.closed_step || null;" in help_turn
    assert "if (closedStep) {" in help_turn and "fetchSetupStatus();" in help_turn

from tests.booking_fixtures import carried_config
