"""The cold per-page reads overlap instead of queuing (each ~45-110 ms from Render to Supabase),
with identical results and unchanged failure behaviour."""
import time

import pytest

from app import dashboard, db, navcache, setup_handoff
from app.auth import TenantCtx


def _slow(value, delay=0.2, calls=None, name=None):
    def fn(*args, **kwargs):
        if calls is not None:
            calls.append(name)
        time.sleep(delay)
        return value() if callable(value) else value
    return fn


def _stub_steps(monkeypatch, modules, calls):
    monkeypatch.setattr(db, "active_modules", _slow(set(modules), 0.2, calls, "modules"))
    monkeypatch.setattr(db, "get_scheduling_config", _slow({}, 0.2, calls, "sched"))
    monkeypatch.setattr(db, "get_invoice_settings", _slow({}, 0.2, calls, "invoice"))
    monkeypatch.setattr(db, "get_tenant_config", _slow({"crm_quiet_days": 7}, 0.2, calls, "tcfg"))
    monkeypatch.setattr(db, "crm_list_stages", _slow([], 0.2, calls, "stages"))
    monkeypatch.setattr(db, "list_requirement_groups", _slow([], 0.2, calls, "groups"))


def test_load_steps_reads_overlap_and_the_answer_is_unchanged(monkeypatch):
    calls = []
    _stub_steps(monkeypatch, {"crm", "scheduling"}, calls)
    seen = {}
    monkeypatch.setattr(setup_handoff, "steps", lambda mods, cfg, stages, quiet, inv, dependant_groups=None:
                        seen.update(mods=mods, cfg=cfg, stages=stages, quiet=quiet, inv=inv, groups=dependant_groups) or ["ok"])
    started = time.monotonic()
    assert setup_handoff.load_steps("t1") == ["ok"]
    assert time.monotonic() - started < 0.7          # modules, then one wave: ~0.4 s, not 1.4 s in a row
    assert seen["mods"] == {"crm", "scheduling"} and seen["quiet"] == 7
    assert sorted(calls) == sorted(["modules", "sched", "invoice", "tcfg", "stages", "groups"])


def test_load_steps_skips_the_reads_a_tenant_does_not_have(monkeypatch):
    calls = []
    _stub_steps(monkeypatch, set(), calls)
    monkeypatch.setattr(setup_handoff, "steps", lambda *a, **k: ["ok"])
    assert setup_handoff.load_steps("t1") == ["ok"]
    assert "stages" not in calls and "groups" not in calls


def test_load_steps_failure_is_still_an_empty_list(monkeypatch):
    calls = []
    _stub_steps(monkeypatch, {"crm"}, calls)
    def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(db, "get_invoice_settings", boom)
    assert setup_handoff.load_steps("t1") == []


@pytest.fixture
def warm_ctx(monkeypatch):
    monkeypatch.setattr(navcache, "_disabled", False)
    navcache._store.clear()
    yield TenantCtx(tenant_id="t1", client=object(), user_id="u1")
    navcache._store.clear()


def test_prewarm_fills_exactly_the_keys_ctx_reads_and_overlaps(monkeypatch, warm_ctx):
    monkeypatch.setattr(db, "list_email_log", _slow(lambda: [1, 2, 3]))
    monkeypatch.setattr(db, "get_tenant_config", _slow({"a": 1}))
    monkeypatch.setattr(setup_handoff, "load_steps", _slow(["step"]))
    monkeypatch.setattr(db, "is_platform_admin", _slow(True))
    monkeypatch.setattr(db, "list_members", _slow(lambda: [1, 2]))
    started = time.monotonic()
    futures = dashboard._prewarm_ctx(warm_ctx, None)
    for f in futures:
        f.result()
    assert time.monotonic() - started < 0.6           # five 0.2 s reads side by side
    assert navcache.peek("t:t1:drafts") == (True, 3)
    assert navcache.peek("t:t1:tcfg-strip") == (True, {"a": 1})
    assert navcache.peek("t:t1:setupsteps") == (True, ["step"])
    assert navcache.peek("pa:u1") == (True, True)
    assert navcache.peek("pc:t1") == (True, 2)


def test_a_failed_prewarm_leaves_the_key_cold_for_the_original_read(monkeypatch, warm_ctx):
    monkeypatch.setattr(db, "list_email_log", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(db, "get_tenant_config", lambda *a, **k: {})
    monkeypatch.setattr(setup_handoff, "load_steps", lambda *a, **k: [])
    monkeypatch.setattr(db, "is_platform_admin", lambda *a, **k: False)
    monkeypatch.setattr(db, "list_members", lambda *a, **k: [])
    for f in dashboard._prewarm_ctx(warm_ctx, None):
        f.result()                                    # never raises
    assert navcache.peek("t:t1:drafts") == (False, None)


def test_prewarm_only_counts_members_for_an_owner_and_never_when_viewing_as(monkeypatch, warm_ctx):
    monkeypatch.setattr(db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(db, "get_tenant_config", lambda *a, **k: {})
    monkeypatch.setattr(setup_handoff, "load_steps", lambda *a, **k: [])
    monkeypatch.setattr(db, "is_platform_admin", lambda *a, **k: False)
    asked = []
    monkeypatch.setattr(db, "list_members", lambda *a, **k: asked.append(1) or [])
    for f in dashboard._prewarm_ctx(warm_ctx, {"role": "staff"}):
        f.result()
    assert asked == []                                # a staff member never reads the count
    for f in dashboard._prewarm_ctx(warm_ctx, {"role": "owner"}):
        f.result()
    assert asked == [1]


def test_prewarm_is_off_when_the_cache_is_off(monkeypatch):
    monkeypatch.setattr(navcache, "_disabled", True)
    assert dashboard._prewarm_ctx(TenantCtx(tenant_id="t1", client=object(), user_id="u1"), None) == []


def test_prewarm_also_fills_the_nav_chrome(monkeypatch, warm_ctx):
    monkeypatch.setattr(db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(db, "get_tenant_config", lambda *a, **k: {})
    monkeypatch.setattr(setup_handoff, "load_steps", lambda *a, **k: [])
    monkeypatch.setattr(db, "is_platform_admin", lambda *a, **k: False)
    monkeypatch.setattr(db, "list_members", lambda *a, **k: [])
    monkeypatch.setattr(dashboard, "_chrome", _slow({"mods": {"crm"}}, 0.1))
    for f in dashboard._prewarm_ctx(warm_ctx, None):
        f.result()
    assert navcache.peek("t:t1:chrome") == (True, {"mods": {"crm"}})


def test_member_count_resolves_the_person_itself_when_not_told(monkeypatch, warm_ctx):
    monkeypatch.setattr(db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(db, "get_tenant_config", lambda *a, **k: {})
    monkeypatch.setattr(setup_handoff, "load_steps", lambda *a, **k: [])
    monkeypatch.setattr(db, "is_platform_admin", lambda *a, **k: False)
    monkeypatch.setattr(dashboard, "_chrome", lambda *a, **k: {})
    asked = []
    monkeypatch.setattr(db, "list_members", lambda *a, **k: asked.append(1) or [])
    monkeypatch.setattr(dashboard, "_me", lambda ctx: {"role": "staff"})
    for f in dashboard._prewarm_ctx(warm_ctx):
        f.result()
    assert asked == []
    monkeypatch.setattr(dashboard, "_me", lambda ctx: {"role": "owner"})
    for f in dashboard._prewarm_ctx(warm_ctx):
        f.result()
    assert asked == [1]


def _req(method, accept):
    from starlette.requests import Request
    return Request({"type": "http", "method": method, "path": "/app/",
                    "headers": [(b"accept", accept.encode())] if accept else []})


def test_the_router_dependency_warms_only_for_a_page_navigation(monkeypatch):
    started = []
    monkeypatch.setattr(dashboard, "_prewarm_ctx", lambda ctx, *a: started.append(ctx.tenant_id) or [])
    ctx = TenantCtx(tenant_id="t1", client=object(), user_id="u1")
    dashboard._page_prewarm(_req("GET", "text/html,application/xhtml+xml"), ctx)
    assert started == ["t1"]
    dashboard._page_prewarm(_req("GET", "*/*"), ctx)               # a fetch
    dashboard._page_prewarm(_req("GET", "application/json"), ctx)  # an API call
    dashboard._page_prewarm(_req("GET", ""), ctx)
    dashboard._page_prewarm(_req("POST", "text/html"), ctx)        # a form post
    assert started == ["t1"]


def test_the_dashboard_router_carries_the_prewarm_dependency():
    deps = [d.dependency for d in dashboard.router.dependencies]
    assert dashboard._page_prewarm in deps
