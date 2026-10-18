"""View as (src/app/viewas.py; Kris 2026-09-24): the owner sees BLAM8 exactly as one
of their people does — read-only, nothing saved under the person's name."""
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import auth, concierge_actions, concierge_routes, navcache, viewas

T, OWNER, EM, OTHER_T = "t1", "u-owner", "u-em", "t2"
MEMBERS = {
    (T, OWNER): {"user_id": OWNER, "role": "owner", "active": True},
    (T, EM): {"user_id": EM, "role": "staff", "display_name": "Em", "active": True},
    (T, "u-mgr"): {"user_id": "u-mgr", "role": "manager", "active": True},
    (T, "u-gone"): {"user_id": "u-gone", "role": "staff", "active": False},
}


@pytest.fixture(autouse=True)
def _members(monkeypatch):
    monkeypatch.setattr(auth.db, "get_member", lambda t, u: MEMBERS.get((t, u)))
    monkeypatch.setattr(navcache, "get_or_fill", lambda key, ttl, fill, **k: fill())


def _req(cookie, method="GET", path="/app/"):
    return SimpleNamespace(cookies={viewas.COOKIE: cookie} if cookie else {}, method=method,
                           url=SimpleNamespace(path=path), state=SimpleNamespace())


def test_the_owner_views_as_em_and_every_page_answers_as_her():
    req = _req(viewas.sign(OWNER, EM, T))
    assert auth._viewing_as(req, T, OWNER) == EM
    assert req.state.viewing_as == {"name": "Em", "role": "staff"}


def test_nothing_can_be_changed_while_viewing():
    cookie = viewas.sign(OWNER, EM, T)
    with pytest.raises(HTTPException) as e:
        auth._viewing_as(_req(cookie, "POST", "/app/diary/123/edit"), T, OWNER)
    assert e.value.status_code == 403 and "viewing BLAM8 as Em" in e.value.detail
    # …except leaving, and asking the Concierge a question
    assert auth._viewing_as(_req(cookie, "POST", "/app/view-as/exit"), T, OWNER) == EM
    assert auth._viewing_as(_req(cookie, "POST", "/concierge/business"), T, OWNER) == EM
    assert auth._viewing_as(_req(cookie, "POST", "/concierge/voice/chat"), T, OWNER) == EM
    for path in ("/concierge/business/execute", "/concierge/voice/local", "/app/people/say"):
        with pytest.raises(HTTPException):
            auth._viewing_as(_req(cookie, "POST", path), T, OWNER)


def test_only_the_owner_and_only_someone_active_on_the_same_account():
    assert auth._viewing_as(_req(viewas.sign("u-mgr", EM, T)), T, "u-mgr") is None    # a manager can't
    assert auth._viewing_as(_req(viewas.sign(OWNER, "u-gone", T)), T, OWNER) is None  # deactivated
    assert auth._viewing_as(_req(viewas.sign(OWNER, EM, OTHER_T)), T, OWNER) is None  # another tenant
    assert auth._viewing_as(_req(viewas.sign(OWNER, EM, T)), T, "u-mgr") is None      # someone else's cookie
    assert auth._viewing_as(_req(viewas.sign(OWNER, OWNER, T)), T, OWNER) is None     # yourself


def test_a_forged_or_old_cookie_does_nothing(monkeypatch):
    real = viewas.sign(OWNER, EM, T)
    forged = real.replace(EM, "u-mgr")
    assert auth._viewing_as(_req(forged), T, OWNER) is None
    later = time.time() + viewas.MAX_AGE_S + 5
    monkeypatch.setattr(viewas.time, "time", lambda: later)
    assert auth._viewing_as(_req(real), T, OWNER) is None


def test_a_concierge_turn_while_viewing_saves_nothing_and_sets_up_no_card(monkeypatch):
    saved, staged = [], []
    monkeypatch.setattr(concierge_routes.db, "add_concierge_message", lambda *a, **k: saved.append(a))
    monkeypatch.setattr(concierge_actions, "stage", lambda *a, **k: staged.append(a))
    monkeypatch.setattr(concierge_actions, "stage_check", lambda *a, **k: "Block the diary on 11th Oct 26")
    monkeypatch.setattr(concierge_routes.trace, "emit", lambda *a, **k: None)
    monkeypatch.setattr(concierge_routes.usage, "record_anthropic", lambda *a, **k: None)
    ctx = SimpleNamespace(tenant_id=T, client=None, user_id=EM, viewing_as=OWNER)
    result = SimpleNamespace(reply="Shall I?", links=[], action={"kind": "block_date", "new_date": "2026-10-11"},
                             usage={}, model="m")
    out = concierge_routes.finish_business_turn(ctx, result, question="Block the 11th", trail=[],
                                                tools_called=[], t0=time.monotonic(),
                                                full_qa=True, scheduling_on=True, member=MEMBERS[(T, EM)])
    assert saved == [] and staged == [] and out["proposal"] is None
    assert "You'd get a card here" in out["reply"] and "Block the diary on 11th Oct 26" in out["reply"]


def test_the_concierge_talks_to_the_person_and_knows_what_they_may_do():
    # Kris 2026-09-24: "talk to them, not about them", and it tried a block
    # before finding out Em can't.
    from app import people
    em = people.member({"user_id": EM, "role": "staff", "display_name": "Em Hopkins",
                        "scopes": {"pages": ["today", "diary"], "can": []}})
    note = concierge_actions.asker_note(em, full_qa=True, scheduling_on=True)
    assert "talking to Em (Staff)" in note and 'as "you"' in note
    assert "NOT theirs to do" in note and "add and move bookings" in note
    assert "don't look anything up and don't propose it" in note
    assert "Doors they can open: Today, Diary" in note
    em2 = people.member({"user_id": EM, "role": "staff", "display_name": "Em",
                         "scopes": {"pages": ["today", "diary"], "can": ["diary:add"]}})
    note2 = concierge_actions.asker_note(em2, full_qa=True, scheduling_on=True)
    assert "What they may have you prepare: add and move bookings" in note2
    # The owner, and an account from before People, get no note at all.
    assert concierge_actions.asker_note(None, full_qa=True, scheduling_on=True) is None
    owner = people.member({"user_id": OWNER, "role": "owner"})
    assert concierge_actions.asker_note(owner, full_qa=True, scheduling_on=True) is None
    # Both routes pass it to the model.
    src = open("src/app/concierge_routes.py", encoding="utf-8").read()
    # One route calls it directly, the streaming one off the event loop in a thread.
    assert "concierge_actions.asker_note(" in src
    assert src.count("_people_note(ctx, asker, full_qa, scheduling_on)") == 1
    assert src.count("asyncio.to_thread(_people_note, ctx, asker, full_qa, scheduling_on)") == 1


def test_the_menu_item_is_the_owners():
    base = open("src/app/templates/app/base.html", encoding="utf-8").read()
    assert '{% if view_as_ok %}' in base and 'href="/app/view-as"' in base
    assert "data-viewing-as" in base and 'action="/app/view-as/exit"' in base
    # The way out can't hide under the iPhone's clock (Kris 2026-09-24: "I have
    # no way to leave"): the banner sits under the top bar, and the menu has it.
    assert base.index('id="app-bar"') < base.index("data-viewing-as")
    assert base.index("{% set _va =") < base.index('id="app-bar"')
    assert "data-menu-leave-view-as" in base and "Leave View as ({{ _va.name }})" in base


def test_a_page_renders_with_the_banner_while_viewing(monkeypatch):
    # The banner lives in base.html, so a template error here breaks every page.
    from fastapi.testclient import TestClient
    from app.main import app
    from app.auth import TenantCtx, require_onboarded, require_tenant
    from app import people_routes

    def fake_ctx(request: __import__("fastapi").Request):
        request.state.viewing_as = {"name": "Em", "role": "staff"}
        return TenantCtx(tenant_id=T, client=None, user_id=EM, viewing_as=OWNER)
    app.dependency_overrides[require_onboarded] = fake_ctx
    app.dependency_overrides[require_tenant] = fake_ctx
    monkeypatch.setattr(people_routes.db, "list_members", lambda t, client=None: list(
        m for (tt, _), m in MEMBERS.items() if tt == T))
    import app.dashboard as dash
    monkeypatch.setattr(dash.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dash, "_chrome", lambda ctx: {"preview_mods": [], "mods": set(), "tenant": {}, "enterprise": False})
    monkeypatch.setattr(dash.db, "is_platform_admin", lambda u: False)
    try:
        res = TestClient(app).get("/app/view-as", follow_redirects=False)
    finally:
        app.dependency_overrides.clear()
    assert res.status_code == 200, res.text[:400]
    assert "data-viewing-as" in res.text and "Viewing as Em" in res.text
    assert "Leave View as" in res.text
    assert 'href="/app/view-as"' not in res.text      # no menu item while already viewing
