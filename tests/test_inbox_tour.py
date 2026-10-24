"""Page 2 of the walk-through — the Inbox (Kris 2026-09-13, one page at a
time after Today).

Run: python -m pytest tests/test_inbox_tour.py -q
"""
from pathlib import Path

from app import dashboard, db
from app.auth import TenantCtx

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_the_inbox_walk_waits_for_todays(monkeypatch):
    ctx = TenantCtx(tenant_id="t1", client=None)
    tcfg = {"tours": {}}
    steps = [{"key": k, "href": "/app/x", "done": True} for k in ("diary", "crm", "invoicing")]
    monkeypatch.setattr(dashboard, "_setup_strip_ctx", lambda c, t: {"steps": steps})
    monkeypatch.setattr(dashboard.navcache, "get_or_fill", lambda key, ttl, fn: fn())
    monkeypatch.setattr(db, "get_tenant_config", lambda tid, client=None: tcfg)
    monkeypatch.setattr(db, "list_requirement_groups", lambda tid, client=None: [])
    assert dashboard._tour_for(ctx, {}, "inbox") == ("inbox", "")                  # owed on its own — Today skipped or not
    assert dashboard._tour_for(ctx, {}, "inbox", after="today") == (None, "")      # (the gate still exists for a page that wants it)
    tcfg["tours"] = {"inbox": "2026-09-13T15:00:00+00:00"}
    assert dashboard._tour_for(ctx, {}, "inbox") == (None, "")                     # walked once, never again
    assert dashboard._tour_for(ctx, {}, "inbox", force=True) == ("inbox", "")


def test_the_inbox_page_carries_the_anchors_and_the_stops():
    part = _read("src/app/templates/app/_conversation_list.html")
    assert 'id="convo-pills" class="flex flex-1 items-center gap-1" data-tour="pills"' in part
    assert '<div class="glass-card px-4 py-3 text-sm" data-tour="drafting">' in part
    assert 'class="text-brand hover:underline" data-tour="settings">Settings</a>' in part
    assert '{% for g in month_groups %}{% set outer = loop %}' in part
    assert '<div{% if outer.first and loop.first %} data-tour="row"{% endif %} class="convo-swipe' in part
    route = _read("src/app/dashboard.py")
    assert '_tour_for(ctx, {}, "inbox", force=request.query_params.get("tour") == "1")' in route
    js = _read("src/app/static/tour.js")
    inbox = js.split("inbox: [")[1].split("],")[0]
    for sel in ('[data-tour="drafting"]', '[data-tour="row"]', '[data-tour="pills"]'):
        assert sel in inbox
    assert inbox.count('[data-tour="row"]') == 2                     # the swipe demo first, the rows later
    assert inbox.index('demo: "swipe"') < inbox.index('[data-tour="pills"]') < inbox.index('[data-tour="drafting"]')   # the switch closes the walk
    assert 'choice: { yes: "Turn drafting on", no: "Not yet",' in inbox and 'post: "/app/settings/drafting", body: "mode=review"' in inbox
    assert 'document.querySelectorAll("[data-before-blam8]").length' in inbox      # the count, off the page
    assert "Robots are already filed" in inbox and "under Ignored." in inbox
    assert 'typeof stop.text === "function" ? stop.text() : stop.text' in js
    assert 'next.setAttribute("data-tour-choice", "yes");' in js
    assert 'inbox: { sel: \'[data-tour="settings"]\'' not in js                   # no trip to Settings any more
    assert "Do it now, before drafting is on" in inbox and "nothing gets wasted on them." in inbox
    assert "Every thread from here on wears" in inbox and "the colour of its pill" in inbox   # Kris: the rows take the pill colours
    assert "function demoSwipe(wrap)" in js and 'row.style.transform = "translateX(" + dist + "px)"' in js
    assert "if (n >= 6)" in js and "var steps = [out, back, out, back, outLeft, back];" in js   # right twice, then left once
    assert "Swipe left instead to open their contact card." in inbox
    assert "function endDemo()" in js and "endDemo();" in js.split("function clear()")[1][:80]
    dash = _read("src/app/dashboard.py")
    assert '@router.post("/settings/drafting", dependencies=[Depends(require_csrf)])' in dash
    assert '"label": "Ignored",' in dash and "robots and bulk mail file under Ignored" in dash
    thread = _read("src/app/templates/app/_thread.html")
    assert "data-filed-thread" in thread and "data-draft-reply" in thread
    # the thread's draft_hold comes from _thread_flags (one config read: also "no sign-off set")
    assert "{% if draft_hold %}" in thread and '"draft_hold": bool(cfg.get("draft_hold"))' in dash
    assert dash.count("**_thread_flags(ctx)") == 2


def test_the_drafting_route_writes_only_the_two_flags(monkeypatch):
    from fastapi.testclient import TestClient
    from app.auth import require_onboarded, require_tenant
    from app.main import app
    from app.security import require_csrf
    saved = {}
    monkeypatch.setattr(db, "upsert_tenant_config", lambda tid, cfg, client=None: saved.update(cfg))
    for dep in (require_onboarded, require_tenant):
        app.dependency_overrides[dep] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    try:
        r = TestClient(app).post("/app/settings/drafting", data={"mode": "review"})
        assert r.status_code == 200 and r.json() == {"ok": True, "mode": "review"}
        assert saved == {"draft_hold": False, "auto_send": False}
        r = TestClient(app).post("/app/settings/drafting", data={"mode": "nonsense"})
        assert r.json()["mode"] == "review"
    finally:
        app.dependency_overrides.clear()


def test_the_walk_survives_a_reload_and_the_switch_lands_on_the_clean_address():
    """Kris 2026-09-13: "Turn drafting on" restarted the walk (the reload kept
    ?tour=1 and cut the stamp off); a swipe mid-tour reloaded the page and
    the walk either vanished or would restart from the top."""
    js = _read("src/app/static/tour.js")
    assert 'var POS_KEY = "blam8_tour_pos:" + page;' in js
    assert "savePos(i);" in js.split("function show(i)")[1][:120]
    assert "show(Math.min(loadPos(), stops.length - 1));" in js
    assert 'if (f && f.id === "ignore-remove-form" && stops[idx] && stops[idx].demo === "swipe") savePos(idx + 1);' in js
    assert "if (done) return Promise.resolve();" in js and "return fetch(\"/app/tour/done\"" in js
    assert "var stamped = stamp();" in js and "return stamped;" in js
    assert ".then(function () { return finish(true); })" in js                   # the switch walks on to the Diary (Kris 2026-09-13)
    assert "window.location.pathname" not in js and "window.location.reload()" not in js


def test_a_left_swipe_opens_the_contact_card(monkeypatch):
    """Kris 2026-09-13: "what we can do for a left swipe — travel to that
    customer's CRM profile"."""
    page = _read("src/app/templates/app/conversations.html")
    assert ".swipe-under-right { background: #2563EB;" in page and "swipe-armed-left" in page
    assert "underR: wrap && wrap.querySelector('.swipe-under-right')," in page
    assert "var armedLeft = dx <= -st.threshold;" in page
    assert "window.location.href = '/app/crm/open?email=' + encodeURIComponent(who);" in page
    assert "var t = dx >= 0 ? dx : dx / 6;" not in page                     # no rubber on the left any more
    rows = _read("src/app/templates/app/_conversation_list.html")
    assert '<div class="swipe-under swipe-under-right" aria-hidden="true">' in rows
    assert rows.index('<div class="swipe-under" aria-hidden="true">') < rows.index('swipe-under-right')   # the bin stays first
    from fastapi.testclient import TestClient
    from app.auth import require_onboarded, require_tenant
    from app.dashboard import require_crm
    from app.main import app
    monkeypatch.setattr(db, "crm_contact_by_email",
                        lambda tid, email, client=None: {"id": "c-9"} if email == "sam@example.com" else None)
    for dep in (require_crm, require_onboarded, require_tenant):
        app.dependency_overrides[dep] = lambda: TenantCtx(tenant_id="t1", client=None)
    try:
        c = TestClient(app)
        r = c.get("/app/crm/open?email=Sam%40Example.com", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/app/crm/contacts/c-9"
        r = c.get("/app/crm/open?email=nobody%40example.com", follow_redirects=False)
        assert r.headers["location"] == "/app/crm?q=nobody%40example.com&from=inbox"
        crm = _read("src/app/templates/app/crm_contacts.html")
        assert "request.query_params.get('from') == 'inbox'" in crm and "data-no-card-why" in crm
        assert "Their next email, or a reply you draft to them, makes the card." in crm
        r = c.get("/app/crm/open", follow_redirects=False)
        assert r.headers["location"] == "/app/crm"
    finally:
        app.dependency_overrides.clear()


def test_a_walk_left_mid_way_offers_the_way_back():
    """Kris 2026-09-13: "IF a user swipes left during the tour, we need an
    obvious but subtle way back to continue the tour"."""
    shell = _read("src/app/templates/app/base.html")
    assert 'if (document.getElementById("tour-root")) return;' in shell          # never on the walk's own page
    assert 'k.indexOf("blam8_tour_pos:") === 0 && pages[k.slice(15)]' in shell
    assert 'back.textContent = "Back to the walk-through · " + p.label + " →";' in shell
    assert 'back.href = p.href + "?tour=1";' in shell                              # Kris: the chip came back to a page with no walk on it (already stamped) and stayed
    assert 'chip.setAttribute("data-tour-return", found.page);' in shell
    assert "sessionStorage.removeItem(found.key)" in shell                        # dismiss forgets it
    assert ".tour-return { position: fixed;" in shell
    # On a thread the pen sits at the foot of a fixed viewport, so the pill
    # rides under the header there (first-sign-up film 2026-09-30: it covered
    # the draft's last line).
    assert "body:has([data-stage-draft]) .tour-return {" in shell
    assert "bottom: auto; top: calc(2.25rem + var(--top-reserve) + .5rem); }" in shell
    page = _read("src/app/templates/app/conversations.html")
    assert "sessionStorage.setItem('blam8_tour_pos:inbox', '1')" in page          # a left swipe counts the swipe stop done
