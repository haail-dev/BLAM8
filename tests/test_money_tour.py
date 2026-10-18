"""Page 4 of the walk-through — Money (Kris 2026-09-13: "Done, now the Money
page?" … "still allow the tour to be done at the Diary point; just when the
Money page is first opened, it triggers its own tour").

Run: python -m pytest tests/test_money_tour.py -q
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import db
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_the_money_walk_is_owed_on_its_own_and_the_diary_keeps_its_done(monkeypatch):
    ctx = TenantCtx(tenant_id="t1", client=None)
    tcfg = {"tours": {"today": "x", "inbox": "x", "diary": "x"}}
    steps = [{"key": k, "href": "/app/x", "done": True} for k in ("diary", "crm", "invoicing")]
    monkeypatch.setattr(dashboard, "_setup_strip_ctx", lambda c, t: {"steps": steps})
    monkeypatch.setattr(dashboard.navcache, "get_or_fill", lambda key, ttl, fn: fn())
    monkeypatch.setattr(db, "get_tenant_config", lambda tid, client=None: tcfg)
    monkeypatch.setattr(db, "list_requirement_groups", lambda tid, client=None: [])
    assert dashboard._tour_for(ctx, {}, "money") == ("money", "")
    tcfg["tours"]["money"] = "2026-09-13T20:00:00+00:00"
    assert dashboard._tour_for(ctx, {}, "money") == (None, "")
    js = _read("src/app/static/tour.js")
    assert "diary: {" not in js.split("var HANDOFF")[1].split("};")[0]      # the Diary's Done stays Done — no hand-over
    money = js.split("money: [")[1].split("],")[0]
    for sel in ('[data-tour="sheet"]', '[data-tour="chase"]', '[data-tour="accountant"]', '[data-tour="section-settings"]'):
        assert sel in money
    assert "Your money, as a funnel" in money and "At 7, 14 and 30 days late" in money
    assert "Hand to your accountant" in money and "Money settings" in money
    assert 'money: { label: "Money", href: "/app/money" }' in _read("src/app/templates/app/base.html")
    dash = _read("src/app/dashboard.py")
    assert '_tour_for(ctx, {}, "money", force=request.query_params.get("tour") == "1")' in dash
    page = _read("src/app/templates/app/money.html")
    for a in ('data-tour="sheet"', 'data-tour="chase"', 'data-tour="accountant"'):
        assert a in page


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"scheduling"})
    monkeypatch.setattr(dashboard.db, "visible_modules", lambda *a, **k: {"scheduling"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "list_quotes", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "list_invoices", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "list_pending_invoice_chases", lambda *a, **k: [])
    monkeypatch.setattr(dashboard, "_tour_for", lambda ctx, t, page, force=False, after=None: ("money", "") if force else (None, ""))
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app, follow_redirects=False)
    app.dependency_overrides.clear()


def test_an_empty_money_page_still_carries_every_stop(client):
    html = client.get("/app/money?tour=1").text
    assert '<div id="tour-root" data-tour-page="money"' in html
    for a in ('data-tour="sheet"', 'data-tour="chase"', 'data-tour="accountant"', 'data-tour="section-settings"'):
        assert a in html, a                                        # nothing owed, nothing overdue: the anchors are still there
    assert "Nothing overdue." in html
    # The funnel shows every stage, at zero too (Kris: "it says Accepted … but it's not on the list?").
    sheet = html.split('data-tour="sheet"')[1].split("</div>")[0]
    for row in ("Quoted, awaiting an answer", "0 quotes out", "Accepted, not yet invoiced", "Owed to you",
                "Overdue", "Collected this month", "Collected all time"):
        assert row in sheet, row
    assert sheet.index("Quoted") < sheet.index("Accepted") < sheet.index("Owed to you") < sheet.index("Overdue")
    plain = client.get("/app/money").text
    assert 'id="tour-root"' not in plain
