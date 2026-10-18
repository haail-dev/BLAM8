"""The first walk of Today (src/app/static/tour.js) — Kris, 2026-09-13, at the
kennels re-run's done moment: "once the onboarding has finished and the chat
has auto-closed, the Who-needs-you-next box outline glowing/pulsing with a
connected speech bubble… when clicked, the same for Emails this month and
CRM."

Run: python -m pytest tests/test_today_tour.py -q
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import dashboard, db
from app.auth import TenantCtx, require_onboarded, require_tenant
from app.main import app

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _steps(done: bool):
    return [{"key": k, "href": "/app/x", "done": done} for k in ("diary", "requirements", "dependants", "crm", "invoicing")]


def test_the_walk_is_owed_once_every_step_is_done_and_never_again(monkeypatch):
    ctx = TenantCtx(tenant_id="t1", client=None)
    tcfg = {"tours": {}}
    monkeypatch.setattr(dashboard, "_setup_strip_ctx", lambda c, t: {"steps": _steps(True)})
    monkeypatch.setattr(dashboard.navcache, "get_or_fill", lambda key, ttl, fn: fn())
    monkeypatch.setattr(db, "get_tenant_config", lambda tid, client=None: tcfg)
    monkeypatch.setattr(db, "list_requirement_groups",
                        lambda tid, client=None: [{"label": "Dog", "label_plural": "Dogs"}])
    assert dashboard._tour_for(ctx, {}, "today") == ("today", "dogs")
    # stamped → never again (per account, not per browser)
    tcfg["tours"] = {"today": "2026-09-13T14:00:00+00:00"}
    assert dashboard._tour_for(ctx, {}, "today") == (None, "")
    # forced (Help's "show me around" / ?tour=1) → shown regardless
    assert dashboard._tour_for(ctx, {}, "today", force=True) == ("today", "dogs")
    # a step still undone → not yet
    tcfg["tours"] = {}
    monkeypatch.setattr(dashboard, "_setup_strip_ctx", lambda c, t: {"steps": _steps(False)})
    assert dashboard._tour_for(ctx, {}, "today") == (None, "")
    # no steps at all (a read failure) → not yet, never a 500
    monkeypatch.setattr(dashboard, "_setup_strip_ctx", lambda c, t: None)
    assert dashboard._tour_for(ctx, {}, "today") == (None, "")


def test_done_stamps_the_page_on_the_account(monkeypatch):
    saved = {}
    monkeypatch.setattr(db, "get_tenant_config", lambda tid, client=None: {"tours": {"inbox": "x"}})
    monkeypatch.setattr(db, "upsert_tenant_config", lambda tid, cfg, client=None: saved.update(cfg))
    monkeypatch.setattr(dashboard, "_bust_setup_strip", lambda ctx: None)
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_tenant] = lambda: TenantCtx(tenant_id="t1", client=None)
    from app.security import require_csrf
    app.dependency_overrides[require_csrf] = lambda: None
    try:
        r = TestClient(app).post("/app/tour/done", data={"page": "Today!"})
        assert r.status_code == 200 and r.json() == {"ok": True, "page": "today"}
        assert set(saved["tours"]) == {"inbox", "today"}          # merged, not replaced
    finally:
        app.dependency_overrides.clear()


def test_the_page_and_shell_carry_the_walk():
    home = _read("src/app/templates/app/home.html")
    assert 'class="deck-card glass-card p-8 text-center" data-tour="next"' in home   # the All-clear card itself
    assert '{% if loop.first %} data-tour="next"{% endif %}' in home          # or the top card of the stack
    assert 'aria-label="Who needs you next" data-tour' not in home            # never the whole section
    assert 'data-tour="emails"' in home and 'data-tour="crm"' in home
    shell = _read("src/app/templates/app/base.html")
    assert '{% if tour_page %}<div id="tour-root" data-tour-page="{{ tour_page }}"' in shell
    assert 'src="/static/tour.js?v={{ asset_rev }}"' in shell
    assert "@keyframes tour-breathe" in shell and ".tour-bubble::before" in shell
    js = _read("src/app/static/tour.js")
    assert 'sel: \'[data-tour="next"]\'' in js and 'sel: \'[data-tour="emails"]\'' in js and 'sel: \'[data-tour="crm"]\'' in js
    assert "window.blam8ConciergeClose" in js                       # the chat steps aside first
    assert 'fetch("/app/tour/done"' in js
    assert "if (idx < stops.length - 1) { e.preventDefault(); e.stopPropagation(); show(idx + 1); }" in js
    widget = _read("src/app/static/concierge.js")
    assert "window.blam8ConciergeClose = function () {" in widget
    assert "add column if not exists tours jsonb not null default '{}'::jsonb" in _read("db/schema.sql")


def test_every_stop_after_the_first_has_a_back_arrow_beside_its_count():
    """Kris 2026-09-13: "put a back arrow on the left side of '4 of 4' so
    users can go back a step"."""
    js = _read("src/app/static/tour.js")
    build = js.split("function build(stop, n, total)")[1].split("function place()")[0]
    assert "if (n > 1) {" in build and 'back.setAttribute("data-tour-back", "1");' in build
    assert 'back.addEventListener("click", function () { show(idx - 1); });' in build
    assert build.index("pos.appendChild(back);") < build.index("pos.appendChild(posText);")   # arrow on the LEFT of the count


def test_done_hands_over_to_the_inbox_and_the_header_says_what_the_product_is_for():
    """Kris 2026-09-13: "Clicking Done should close the dialog, make the
    'Inbox →' pulse then load into the inbox" — and the plan line under the
    name gives way to a tagline ("Your business logistics, automated", fades after 4s since 09-30)
    (the trial countdown moves to the usage tile)."""
    js = _read("src/app/static/tour.js")
    assert "var HANDOFF = { today: { sel: " in js and 'href: "/app/conversations" }' in js
    assert "else finish(true); });" in js and 'door.classList.add("tour-glow");' in js
    assert 'skip.addEventListener("click", function () { finish(false); });' in js   # Skip never navigates (the bare listener read the click event as a hand-over)
    home = _read("src/app/templates/app/home.html")
    assert 'Your business logistics, automated</p>' in home and "data-today-tagline" in home
    assert "all modules" not in home and "· trial, {{ trial.days_remaining }}" not in home
    assert "trial emails used · {{ trial.days_remaining }} day" in home
    # Since 2026-09-25 the header row is the morning brief; no door on Today
    # carries data-tour="inbox", so Done opens the inbox by HANDOFF's href.
    assert 'data-tour="inbox"' not in home and ">Inbox →</a>" not in home


def test_the_diary_rail_is_the_fifth_stop():
    """Kris 2026-09-13: "add a step 5 for the Diary bar at the bottom,
    showing what's on today's plate"."""
    js = _read("src/app/static/tour.js")
    today = js.split("today: [")[1].split("],")[0]
    order = [today.index(k) for k in ('[data-tour="next"]', '[data-tour="order"]', '[data-tour="emails"]',
                                     '[data-tour="crm"]', '[data-tour="diary"]')]
    assert order == sorted(order)                                     # next, cog, emails, crm, diary
    assert "What's on the plate today" in today
    assert 'aria-label="Diary rail" data-tour="diary"' in _read("src/app/templates/app/base.html")
