"""Security review 2026-09: M10 (no third-party Tailwind script), M26 (deep
health), M29 (a laptop never starts the production background loops)."""
import pathlib

import pytest
from fastapi.testclient import TestClient

from app import config

TEMPLATES = pathlib.Path(__file__).resolve().parents[1] / "src/app" / "templates"


def test_no_template_loads_the_tailwind_cdn():
    hits = [p.relative_to(TEMPLATES).as_posix() for p in TEMPLATES.rglob("*.html")
            if "cdn.tailwindcss" in p.read_text(encoding="utf-8") or "tailwind.config =" in p.read_text(encoding="utf-8")]
    assert hits == []


def test_csp_no_longer_allows_the_cdn():
    from app import security
    assert "cdn.tailwindcss" not in security.CSP


@pytest.mark.parametrize("env,flag,expected", [
    ({}, None, False),                                   # a laptop
    ({"RENDER": "true"}, None, True),                    # Render
    ({"RENDER_GIT_COMMIT": "abc1234"}, None, True),
    ({"RENDER": "true"}, False, False),                  # explicit off wins
    ({}, True, True),                                    # explicit on wins
])
def test_background_jobs_only_on_render(monkeypatch, env, flag, expected):
    for k in ("RENDER", "RENDER_GIT_COMMIT"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(config.settings, "run_daily_jobs", flag)
    assert config.background_jobs_on() is expected


def test_deep_health_reports_a_dead_database(monkeypatch):
    from app import db
    from app.main import app

    class _Dead:
        def table(self, *_a, **_k):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(db, "db", lambda: _Dead())
    c = TestClient(app)
    shallow = c.get("/health")
    assert shallow.status_code == 200 and "db" not in shallow.json()
    deep = c.get("/health?deep=1")
    assert deep.status_code == 503 and deep.json()["db"] == "down"


# ── M16/M17: operator audit; M5: operator sessions last hours ────────────────
class _Rec:
    def __init__(self):
        self.rows = []

    def table(self, name):
        rec = self

        class _T:
            def insert(self, row):
                rec.rows.append((name, row)); return self

            def execute(self):
                return None
        return _T()


def _req(method, path, query="", path_params=None):
    from starlette.requests import Request
    scope = {"type": "http", "method": method, "path": path, "query_string": query.encode(),
             "headers": [], "path_params": path_params or {}}
    return Request(scope)


def test_admin_changes_and_body_views_are_audited(monkeypatch):
    from app import admin_auth, db
    rec = _Rec()
    monkeypatch.setattr(db, "db", lambda: rec)
    ctx = admin_auth.AdminCtx(user_id="u1", email="op@example.com", role="admin")
    admin_auth._audit(_req("GET", "/admin/tenants"), ctx)                       # plain view: no row
    admin_auth._audit(_req("GET", "/admin/llm/abc", "body=1", {"call_id": "abc"}), ctx)
    admin_auth._audit(_req("POST", "/admin/tenants/t-1/comp", "", {"tenant_id": "t-1"}), ctx)
    assert [r["method"] for _, r in rec.rows] == ["GET", "POST"]
    assert rec.rows[0][1]["detail"]["body_view"] is True
    assert rec.rows[1][1]["tenant_id"] == "t-1" and rec.rows[1][1]["admin_email"] == "op@example.com"


def test_admin_audit_failure_never_blocks(monkeypatch):
    from app import admin_auth, db

    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "db", boom)
    admin_auth._audit(_req("POST", "/admin/x"), admin_auth.AdminCtx("u", "e", "admin"))


def test_admin_cookies_last_hours():
    from starlette.responses import Response
    from app import admin_auth
    r = Response()
    admin_auth.set_admin_cookies(r, {"access_token": "a", "refresh_token": "b"})
    ages = [h for k, h in r.raw_headers if k == b"set-cookie"]
    assert ages and all(b"Max-Age=43200" in h for h in ages)
