"""People — partners and staff on one BLAM8 (docs/plan-people.md, slice 1).

Kris, 2026-09-13: "we need some way to add employees to BLAM8 and allow the
business to decide what they're able to see/access … seats/staff should be
free … added via invite or QR" — and, on the shape: "a settings block for the
Admin and a page for the Person?" → a Settings row that opens a list, with a
page per person.

Run: python -m pytest tests/test_people.py -q
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import db, people, people_routes
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _row(role="staff", **over):
    row = {"user_id": "u-" + role, "role": role, "display_name": role.title(),
           "email": f"{role}@example.com", "active": True, "scopes": {}}
    row.update(over)
    return row


# ── the model ──────────────────────────────────────────────────────────────
def test_a_legacy_member_keeps_everything_it_had():
    """Every pre-People row is role 'member' and nothing gated on it, so those
    people already saw everything. A migration must never quietly take access
    away from someone who had it yesterday."""
    assert people.normalise_role("member") == "manager"
    assert people.normalise_role("") == "staff" and people.normalise_role("nonsense") == "staff"
    m = people.member(_row(role="member"))
    assert m["role"] == "manager" and people.pages_of(m) == people.PAGES
    # No membership row at all is the account's own owner — every tenant
    # predates People and its single user owns it.
    assert people.can_open(None, "money") is True and people.is_admin(None) is True


def test_a_role_opens_its_own_doors():
    owner, manager = people.member(_row(role="owner")), people.member(_row(role="manager"))
    for m in (owner, manager):
        assert people.pages_of(m) == people.PAGES
        assert people.is_admin(m) is True
    staff = people.member(_row(role="staff", scopes={"pages": ["diary"]}))
    # Today is never a choice — it is where they land (Kris 2026-09-13).
    assert people.pages_of(staff) == ("today", "diary") and people.home_page(staff) == "today"
    assert people.can_open(people.member(_row(role="staff", scopes={"pages": []})), "today") is True
    assert people.can_open(staff, "money") is False and people.can_open(staff, "diary") is True
    assert people.is_admin(staff) is False and people.is_read_only(staff) is False
    acct = people.member(_row(role="accountant"))
    # …except an accountant, who is not staff: their first screen is their one door.
    assert people.pages_of(acct) == ("money",) and people.home_page(acct) == "money"
    assert people.can_open(acct, "today") is False
    assert people.is_read_only(acct) is True and people.is_admin(acct) is False
    # An accountant's doors are not the business's to widen — a stored page
    # list cannot open one, whatever put it there.
    assert people.can_open(people.member(_row(role="accountant", scopes={"pages": list(people.PAGES)})), "inbox") is False
    # Deactivated opens nothing at all.
    assert people.can_open(people.member(_row(role="manager", active=False)), "today") is False


def test_scopes_are_cleaned_against_the_role():
    s = people.clean_scopes({"pages": ["diary", "nonsense", "MONEY"], "does": ["  the desk "],
                             "mailboxes": ["m1"]}, "staff")
    assert s["pages"] == ["today", "diary", "money"]      # Today rides along, always
    assert s["does"] == ["the desk"] and s["mailboxes"] == ["m1"]
    assert people.clean_scopes({"pages": []}, "manager")["pages"] == list(people.PAGES)
    assert people.default_scopes("staff")["pages"] == ["today", "diary"]
    assert people.clean_scopes({"pages": ["diary"]}, "staff")["pages"] == ["today", "diary"]


def test_an_invitation_expires_and_is_used_once():
    now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    live = {"expires_at": (now + timedelta(days=1)).isoformat()}
    assert people.invite_state(live, now) == "pending" and people.usable(live, now) is True
    assert people.invite_state({"expires_at": (now - timedelta(minutes=1)).isoformat()}, now) == "expired"
    assert people.invite_state({**live, "accepted_at": now.isoformat()}, now) == "accepted"
    assert people.invite_state({**live, "revoked_at": now.isoformat()}, now) == "revoked"
    assert people.invite_expiry(now) == now + timedelta(days=7)
    assert len(people.new_token()) >= 30 and people.new_token() != people.new_token()
    assert people.invite_url("https://blam8.com/", "abc") == "https://blam8.com/join/abc"
    # Free seats, soft-capped — an invitation out counts, a spent one does not.
    members = [people.member(_row(role="owner")), people.member(_row(role="staff"))]
    assert people.seats_left(members, [live], now) == people.MAX_PEOPLE - 3
    assert people.seats_left(members, [{**live, "accepted_at": now.isoformat()}], now) == people.MAX_PEOPLE - 2
    # An invitation that has since expired frees its seat again.
    stale = {"expires_at": (now - timedelta(days=1)).isoformat()}
    assert people.seats_left(members, [stale], now) == people.MAX_PEOPLE - 2


def test_the_invitation_carries_its_own_qr():
    svg = people.qr_svg("https://blam8.com/join/abc")
    assert svg.startswith("<svg") and "path" in svg
    src = _read("src/app/people.py")
    assert "must never travel to a third" in src          # drawn here, not fetched from an image service


# ── the guard ──────────────────────────────────────────────────────────────
def test_every_page_key_maps_to_a_door():
    doors = set(dashboard._ACTIVE_DOORS.values())
    assert doors <= set(people.PAGES)
    for key in ("home", "conversations", "diary", "crm", "money", "settings"):
        assert key in dashboard._ACTIVE_DOORS


@pytest.fixture
def as_staff(monkeypatch):
    """Signed in as a staff member with Today and the Diary, nothing else."""
    from tests.test_home_dashboard import Env
    Env(monkeypatch, convos=[], modules={"scheduling", "crm"})
    monkeypatch.setattr(db, "get_member", lambda tid, uid: _row(
        user_id="u-sam", role="staff", display_name="Sam",
        scopes={"pages": ["today", "diary"]}))
    monkeypatch.setattr(db, "list_members", lambda tid: [
        _row(user_id="u-own", role="owner", display_name="Kris"),
        _row(user_id="u-sam", role="staff", display_name="Sam", scopes={"pages": ["today", "diary"]})])
    monkeypatch.setattr(db, "list_member_invites", lambda tid: [])
    monkeypatch.setattr(dashboard.db, "preview_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "visible_modules", lambda *a, **k: {"scheduling", "crm"})
    monkeypatch.setattr(dashboard.db, "list_invoices", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "list_quotes", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "list_pending_invoice_chases", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "crm_list_contacts", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "crm_list_deals", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "ignored_addresses", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "list_deck_notifications", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "list_unhealthy_connections", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "list_requirement_groups", lambda *a, **k: [])
    dashboard.navcache.invalidate("t:t1")
    dashboard.navcache.invalidate("tm:t1")
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None, user_id="u-sam")
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app, follow_redirects=False)
    app.dependency_overrides.clear()
    dashboard.navcache.invalidate("tm:t1")


def test_a_closed_door_sends_them_somewhere_they_can_go(as_staff):
    for path in ("/app/money", "/app/conversations", "/app/settings"):
        r = as_staff.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/app/", path
    assert as_staff.get("/app/").status_code == 200


def test_the_shell_shows_only_the_doors_they_can_open(as_staff):
    html = as_staff.get("/app/").text
    assert 'href="/app/diary"' in html
    assert 'href="/app/money"' not in html and 'href="/app/conversations"' not in html
    shell = _read("src/app/templates/app/base.html")
    for door in ("inbox", "money", "crm", "diary"):
        assert "'%s' in (my_doors or ['%s'])" % (door, door) in shell, door


def test_managing_people_is_the_owners_job(as_staff):
    r = as_staff.get("/app/people")
    assert r.status_code == 303 and r.headers["location"] == "/app/"
    # …but their own profile is theirs to open.
    own = as_staff.get("/app/people/u-sam")
    assert own.status_code == 200 and "Sam" in own.text
    assert 'name="role"' not in own.text                  # they cannot promote themselves
    assert "Diary" in own.text and "closed" in own.text   # what they can and cannot open
    assert as_staff.get("/app/people/u-own").status_code == 303


# ── the admin's page ───────────────────────────────────────────────────────
@pytest.fixture
def as_owner(monkeypatch):
    from tests.test_home_dashboard import Env
    Env(monkeypatch, convos=[], modules={"scheduling", "crm"})
    made = {}
    monkeypatch.setattr(db, "get_member", lambda tid, uid: _row(
        user_id="u-own", role="owner", display_name="Kris") if uid == "u-own" else
        (_row(user_id="u-sam", role="staff", display_name="Sam", scopes={"pages": ["today", "diary"]})
         if uid == "u-sam" else None))
    monkeypatch.setattr(db, "list_members", lambda tid: [
        _row(user_id="u-own", role="owner", display_name="Kris"),
        _row(user_id="u-sam", role="staff", display_name="Sam", scopes={"pages": ["today", "diary"]})])
    monkeypatch.setattr(db, "list_member_invites", lambda tid: [])
    monkeypatch.setattr(dashboard.db, "preview_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "visible_modules", lambda *a, **k: {"scheduling", "crm"})
    monkeypatch.setattr(dashboard.db, "list_invoices", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "list_quotes", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "list_pending_invoice_chases", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "crm_list_contacts", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "crm_list_deals", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "ignored_addresses", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "list_deck_notifications", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "list_unhealthy_connections", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "list_requirement_groups", lambda *a, **k: [])
    dashboard.navcache.invalidate("t:t1")
    dashboard.navcache.invalidate("tm:t1")
    monkeypatch.setattr(db, "create_member_invite", lambda row: made.update(row) or row)
    monkeypatch.setattr(db, "update_member", lambda tid, uid, fields: made.update({"saved": fields}))
    monkeypatch.setattr(db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(db, "list_team_people", lambda tid: [])
    monkeypatch.setattr(db, "sync_team_person_for_member", lambda *a, **k: None)
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None, user_id="u-own")
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app, follow_redirects=False), made
    app.dependency_overrides.clear()
    dashboard.navcache.invalidate("tm:t1")


def test_the_people_page_lists_everyone_and_offers_an_invitation(as_owner):
    client, _ = as_owner
    html = client.get("/app/people").text
    assert "data-people-page" in html
    assert "Kris" in html and "Sam" in html
    assert 'href="/app/people/u-sam"' in html
    # A door reads as its own name — "Diary", never the raw key.
    row = html.split('href="/app/people/u-sam"')[1].split("</a>")[0]
    assert "Today, Diary" in row and "diary," not in row
    assert "Invite someone" in html and 'name="role" value="staff"' in html
    # Staff is the default choice, and only Staff — "checked" on two radios
    # silently made the LAST one (Accountant) the default (caught live).
    import re
    form = html.split("data-invite-form")[1].split("</form>")[0]
    radios = {re.search(r'value="(\w+)"', t).group(1): ("checked" in t)
              for t in re.findall(r"<input[^>]*data-role-radio[^>]*>", form)}
    assert radios == {"manager": False, "staff": True, "accountant": False}
    assert "People are free" in html
    # Settings carries the row that opens it, with its state on the face.
    settings = _read("src/app/templates/app/settings.html")
    assert 'href="/app/people"' in settings and "data-people-row" in settings
    assert "{{ people_face }}" in settings


def test_an_invitation_is_minted_with_the_role_and_its_doors(as_owner):
    client, made = as_owner
    r = client.post("/app/people/invite",
                    data={"display_name": " Sam ", "email": "SAM@Example.com",
                          "role": "staff", "page": ["diary", "money"]})
    assert r.status_code == 303 and r.headers["location"].startswith("/app/people?msg=invited&t=")
    assert made["display_name"] == "Sam" and made["email"] == "sam@example.com"
    assert made["role"] == "staff" and made["scopes"]["pages"] == ["today", "diary", "money"]
    assert made["tenant_id"] == "t1" and len(made["token"]) >= 30
    # An accountant's doors are never the form's to choose.
    client.post("/app/people/invite", data={"role": "accountant", "page": ["inbox"]})
    assert made["role"] == "accountant" and made["scopes"]["pages"] == ["money"]
    # And nobody is invited as the owner — an account has one, and transfer
    # is not an invitation.
    client.post("/app/people/invite", data={"role": "owner"})
    assert made["role"] == "manager"


def test_the_link_and_its_qr_are_shown_once(as_owner, monkeypatch):
    client, _ = as_owner
    monkeypatch.setattr(db, "get_member_invite", lambda t: None)   # the address lookup for the email button
    html = client.get("/app/people?msg=invited&t=tok123").text
    assert "/join/tok123" in html and "data-invite-qr" in html and "<svg" in html
    assert "works once, for the next 7 days" in html
    assert "/join/tok123" not in client.get("/app/people").text     # not again on a plain visit


def test_an_owner_edits_a_person_but_never_the_owner(as_owner, monkeypatch):
    client, made = as_owner
    # This business has a desk to hand out (a stays diary); a kind that isn't
    # one of the business's own is dropped either way.
    from app import people_routes
    monkeypatch.setattr(people_routes, "_kinds", lambda ctx: [{"key": "desk", "label": "Desk"}])
    client.post("/app/people/u-sam", data={"display_name": "Sam Hale", "role": "manager", "active": "1"})
    assert made["saved"]["display_name"] == "Sam Hale"
    assert made["saved"]["role"] == "manager" and made["saved"]["active"] is True
    assert made["saved"]["scopes"]["pages"] == list(people.PAGES)
    made.clear()
    client.post("/app/people/u-own", data={"display_name": "Kris D", "role": "staff",
                                           "active": "", "kind": ["desk"]})
    saved = made["saved"]
    # The owner's ROLE and whether they still work here are nobody's to change
    # — not even their own. (A posted role=staff and a missing "active" would
    # otherwise demote and switch off the account's owner.)
    assert "role" not in saved and "active" not in saved
    assert saved["display_name"] == "Kris D"
    # What they DO is not a permission, though: it is who takes the dogs in,
    # and without it the owner could never be given a kind — so their own
    # calendar could never receive anything (found on the rig, 2026-09-14).
    assert saved["scopes"]["does"] == ["desk"]
    assert saved["scopes"]["pages"] == list(people.PAGES)           # still everything


# ── the invitation, signed out ─────────────────────────────────────────────
def test_the_join_page_says_what_it_is_before_asking_them_to_sign_in(monkeypatch):
    live = {"tenant_id": "t1", "token": "tok", "role": "staff", "display_name": "Sam",
            "scopes": {"pages": ["today", "diary"]},
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()}
    monkeypatch.setattr(db, "get_member_invite", lambda t: live if t == "tok" else None)
    monkeypatch.setattr(db, "get_tenant", lambda tid, **k: {"name": "Little Paws"})
    c = TestClient(app, follow_redirects=False)
    html = c.get("/join/tok").text
    assert 'data-state="pending"' in html
    assert "Little Paws" in html and "Sam" in html
    # Said to THEM, mid-sentence (live 2026-09-30 it read "…, a Staff. Only the doors you give them."):
    assert "joining as <span" in html and ">staff</span>" in html
    assert "a Staff" not in html and "Only the doors you give them" not in html
    assert "opened for you" in html
    assert "Today" in html and "Diary" in html
    assert "Accept and sign in" in html and "nothing happens until you sign in" in html
    # Accepting stashes the token and hands them to the normal sign-in.
    r = c.post("/join/tok")
    assert r.status_code == 303 and r.headers["location"] == "/login?join=1"
    assert people_routes.JOIN_COOKIE in r.headers.get("set-cookie", "")
    # A stale link says so, and offers nothing.
    stale = c.get("/join/nope").text
    assert "We don't recognise this invitation" in stale and "Accept and sign in" not in stale


def test_the_sign_in_callback_binds_an_invited_user_to_the_business():
    src = _read("src/app/auth_routes.py")
    assert "people_routes.JOIN_COOKIE" in src
    assert "db.accept_member_invite(invite_row, user[\"id\"], email," in src
    assert "tenant_id = joined or existing_tenant_id" in src
    assert "tenant_id = db.ensure_tenant_for_user(" in src
    assert "people_routes.clear_join_cookie(resp)" in src
    db_src = _read("src/app/db.py")
    assert 'on_conflict="tenant_id,user_id"' in db_src                # following the link twice is harmless
    assert '"accepted_at": when_iso' in db_src


# ── slice 2: delegation by kind ────────────────────────────────────────────
_STAY_CFG = {
    "intake_fields": [{"label": "Departure / collection date & time"}, {"label": "Dog name(s)"}],
    "service_types": [{"name": "Meet & greet", "minutes": 30, "resource": "Meet & greet"},
                      {"name": "Grooming", "minutes": 60, "resource": "Grooming"}],
    "resource_types": [{"name": "Meet & greet", "count": 1}, {"name": "Grooming", "count": 1}],
    "front_desk": {"enabled": True, "minutes": 15},
}


def test_the_kinds_are_the_ones_the_business_already_has():
    kinds = people.kinds_for(_STAY_CFG)
    assert [k["key"] for k in kinds] == ["desk", "svc:Meet & greet", "svc:Grooming"]
    assert kinds[0]["label"] == "Drop-offs and collections"
    assert kinds[1]["label"] == "Meet & greet" and kinds[2]["label"] == "Grooming"   # as written (F11)
    # No desk on a diary that isn't stays; nothing at all without services.
    assert [k["key"] for k in people.kinds_for({"service_types": _STAY_CFG["service_types"]})] \
        == ["svc:Meet & greet", "svc:Grooming"]
    assert people.kinds_for({}) == []


def test_a_booking_belongs_to_whoever_does_that_kind():
    desk = people.member(_row(role="staff", scopes={"does": ["desk"]}))
    groomer = people.member(_row(role="staff", scopes={"does": ["svc:Grooming"]}))
    stay = {"id": "s1", "status": "confirmed", "summary": "Bella"}
    appt = {"id": "a1", "status": "confirmed", "resource_type": "Grooming"}
    assert people.owns_booking(desk, stay, _STAY_CFG) is True
    assert people.owns_booking(desk, appt, _STAY_CFG) is False
    assert people.owns_booking(groomer, appt, _STAY_CFG) is True
    assert people.owns_booking(groomer, stay, _STAY_CFG) is False
    # A manager sees the whole diary, and so does a staff member with nothing
    # handed to them — delegation narrows a day, it never blanks one.
    assert people.owns_booking(people.member(_row(role="manager")), appt, _STAY_CFG) is True
    assert people.owns_booking(people.member(_row(role="staff")), appt, _STAY_CFG) is True
    assert people.owns_booking(None, appt, _STAY_CFG) is True


def test_the_diary_numbers_follow_the_people():
    """Kris 2026-09-13: "the kennels could have someone that works there, only
    capable of accepting and releasing pets, the higher ups deal with meet &
    greet" — two on the desk means two things at it at once."""
    two_on_desk = [people.member(_row(user_id="a", role="staff", scopes={"does": ["desk"]})),
                   people.member(_row(user_id="b", role="staff", scopes={"does": ["desk", "svc:Grooming"]}))]
    out = people.capacity_from_people(two_on_desk, _STAY_CFG)
    assert out["front_desk"]["hands"] == 2 and out["front_desk"]["enabled"] is True
    assert "resource_types" not in out                    # one groomer, and Grooming is already 1
    three_groomers = [people.member(_row(user_id=x, role="staff", scopes={"does": ["svc:Grooming"]}))
                      for x in "abc"]
    out = people.capacity_from_people(three_groomers, _STAY_CFG)
    assert {p["name"]: p["count"] for p in out["resource_types"]}["Grooming"] == 3
    assert {p["name"]: p["count"] for p in out["resource_types"]}["Meet & greet"] == 1   # nobody assigned, untouched
    assert "front_desk" not in out
    # Somebody who left counts for nothing.
    gone = [people.member(_row(user_id="a", role="staff", active=False, scopes={"does": ["desk"]}))]
    assert people.capacity_from_people(gone, _STAY_CFG) == {}


def test_the_person_page_hands_out_the_work(as_owner, monkeypatch):
    client, made = as_owner
    import app.people_routes as pr
    client.app.dependency_overrides  # noqa: B018 — the fixture's overrides are already in place
    from app import db as _db
    _db_get = _db.get_scheduling_config
    try:
        _db.get_scheduling_config = lambda *a, **k: dict(_STAY_CFG)
        html = client.get("/app/people/u-sam").text
        assert "What they do" in html and 'value="svc:Meet &amp; greet"' in html
        assert "Drop-offs and collections" in html
        assert "Assign two people to something and the diary lets two of them happen at once." in html
        saved = {}
        monkeypatch.setattr(_db, "upsert_scheduling_config", lambda tid, cfg, **k: saved.update(cfg))
        client.post("/app/people/u-sam",
                    data={"display_name": "Sam", "role": "staff", "page": ["today", "diary"],
                          "kind": ["desk", "svc:Nonsense"], "active": "1"})
        assert made["saved"]["scopes"]["does"] == ["desk"]       # an unknown kind is not a kind
    finally:
        _db.get_scheduling_config = _db_get


def test_a_second_pair_of_hands_unblocks_the_appointment():
    """The whole reason the front desk rule is a count: Kris 2026-09-13, "they
    have an employee/partner that can deal with one thing while you deal with
    the other"."""
    from datetime import datetime as _dt
    from app import scheduling
    cfg = {"working_days": "mon,tue,wed,thu,fri,sat", "windows": "slots", "slot_minutes": 60,
           "capacity_per_window": 30, "horizon_days": 7, "blackout_dates": "", "min_notice_hours": 0,
           "open_time": "07:00", "close_time": "18:45",
           "intake_fields": [{"label": "Departure / collection date & time"}],
           "service_types": [{"name": "Meet & greet", "minutes": 30, "resource": "Meet & greet"}],
           "resource_types": [{"name": "Meet & greet", "count": 2}],
           "front_desk": {"enabled": True, "minutes": 15, "hands": 1}}
    stay = {"id": "s1", "booking_date": "2026-09-14", "start_block": 40, "length_blocks": 96,
            "status": "confirmed", "summary": "Bella", "window": "day", "details": {}}
    day = _dt(2026, 9, 14).date()
    now = _dt(2026, 9, 14, 6, 0, tzinfo=scheduling.DIARY_TZ)

    def starts(c):
        return [s["window"] for s in scheduling.service_availability(
            c, [stay], 2, day, day, now, capacity=2, resource="Meet & greet")]

    assert "10:00" not in starts(cfg)                                  # one pair of hands, taking Bella in
    two = {**cfg, "front_desk": {**cfg["front_desk"], "hands": 2}}
    assert "10:00" in starts(two)                                      # a partner takes the meet & greet
    assert scheduling.front_desk_hands(two) == 2 and scheduling.front_desk_hands(cfg) == 1
    # And the other way: a drop-off is only refused once every hand is busy.
    appt = {"id": "a1", "booking_date": "2026-09-14", "start_block": 40, "length_blocks": 2,
            "status": "confirmed", "summary": "M&G", "window": "10:00",
            "resource_type": "Meet & greet", "details": {"Service": "Meet & greet"}}
    arr = _dt(2026, 9, 14, 10, 15, tzinfo=scheduling.DIARY_TZ)
    dep = _dt(2026, 9, 16, 9, 0, tzinfo=scheduling.DIARY_TZ)
    assert scheduling.front_desk_clash(cfg, [appt], arr, dep) is not None
    assert scheduling.front_desk_clash(two, [appt], arr, dep) is None
    assert scheduling.front_desk_clash(two, [appt, {**appt, "id": "a2"}], arr, dep) is not None


# ── the invitation, emailed ────────────────────────────────────────────────
def test_the_invitation_reads_like_a_person_sent_it():
    subject, body = people.invite_email("Little Paws", "Kris", "Staff",
                                        ["Today", "Diary"], "https://blam8.com/join/tok")
    assert subject == "Kris has invited you to Little Paws on BLAM8"
    assert "Kris has added you to Little Paws on BLAM8 as staff." in body
    assert "You'll be able to open: Today, Diary." in body
    assert "https://blam8.com/join/tok" in body
    assert "works once and lasts 7 days" in body and "costs you nothing" in body
    assert "nothing happens until you sign in" in body
    # The role reads mid-sentence, never "as a Staff".
    assert "as a manager." in people.invite_email("B", "K", "Manager", [], "u")[1]
    assert "as their accountant." in people.invite_email("B", "K", "Accountant", [], "u")[1]
    # No inviter named, no business named: still a sentence.
    assert people.invite_email("", "", "Staff", [], "u")[0] == "You've been invited to a business on BLAM8"


def test_the_ready_card_offers_to_email_it(as_owner, monkeypatch):
    client, _ = as_owner
    monkeypatch.setattr(db, "get_member_invite", lambda t: {
        "id": "i1", "tenant_id": "t1", "token": t, "email": "em@example.com", "role": "staff",
        "scopes": {"pages": ["today", "diary"]},
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()})
    html = client.get("/app/people?msg=invited&t=tok123").text
    assert "data-send-invite" in html and "Email it to em@example.com" in html
    assert "from your own mailbox, so they know who it's from" in html
    assert "/join/tok123" in html and "data-invite-qr" in html      # the link and QR stay
    # No address on the invitation: the button gives way to the plain truth.
    monkeypatch.setattr(db, "get_member_invite", lambda t: {
        "id": "i1", "tenant_id": "t1", "token": t, "email": None, "role": "staff", "scopes": {},
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()})
    html = client.get("/app/people?msg=invited&t=tok123").text
    assert "data-send-invite" not in html and "nothing to email" in html


def test_sending_goes_out_of_the_businesss_own_mailbox(as_owner, monkeypatch):
    client, _ = as_owner
    sent = {}
    live = {"id": "i1", "tenant_id": "t1", "token": "tok", "email": "em@example.com",
            "role": "staff", "scopes": {"pages": ["today", "diary"]},
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()}
    monkeypatch.setattr(db, "get_member_invite", lambda t: live if t == "tok" else None)

    async def _send(tenant_id, to, subject, body, **kw):
        sent.update({"tenant_id": tenant_id, "to": to, "subject": subject, "body": body})
    monkeypatch.setattr(people_routes.mailbox, "send_mail", _send)
    r = client.post("/app/people/invite/send", data={"token": "tok"})
    assert r.status_code == 303 and r.headers["location"] == "/app/people?msg=sent&to=em@example.com"
    assert sent["tenant_id"] == "t1" and sent["to"] == "em@example.com"
    assert "/join/tok" in sent["body"] and "BLAM8" in sent["subject"]
    # A send that fails leaves the link exactly where it was.
    async def _boom(*a, **k):
        raise RuntimeError("no mailbox")
    monkeypatch.setattr(people_routes.mailbox, "send_mail", _boom)
    r = client.post("/app/people/invite/send", data={"token": "tok"})
    assert r.headers["location"] == "/app/people?msg=sendfailed&t=tok"
    assert "wouldn't send just now" in client.get("/app/people?msg=sendfailed&t=tok").text
    # A spent or foreign invitation sends nothing at all.
    monkeypatch.setattr(people_routes.mailbox, "send_mail", _send)
    assert client.post("/app/people/invite/send", data={"token": "nope"}).headers["location"] \
        == "/app/people?msg=stale"


def test_a_waiting_invitation_can_be_emailed_from_its_row(as_owner, monkeypatch):
    client, _ = as_owner
    monkeypatch.setattr(db, "list_member_invites", lambda tid: [{
        "id": "i1", "token": "tok", "email": "em@example.com", "display_name": "Em",
        "role": "staff", "scopes": {},
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()}])
    html = client.get("/app/people").text
    row = html.split("data-invite-row")[1].split("</li>")[0]
    assert "data-resend-invite" in row and 'value="tok"' in row and "Cancel" in row


# ── more control: what they may CHANGE behind a door they can open ─────────
def test_a_door_on_its_own_is_only_reading():
    """Kris 2026-09-13: "each of these tick boxes should have more options to
    each of them once ticked. More control basically"."""
    staff = people.member(_row(role="staff", scopes={
        "pages": ["diary", "money"], "can": ["diary:add", "money:chase", "crm:edit"]}))
    # An ability behind a shut door is dropped on the way in — closing a door
    # can never leave a write behind it.
    assert staff["scopes"]["can"] == ["diary:add", "money:chase"]
    assert people.can_do(staff, "diary", "add") is True
    assert people.can_do(staff, "diary", "cancel") is False       # not ticked
    assert people.can_do(staff, "money", "chase") is True
    assert people.can_do(staff, "money", "send") is False
    assert people.can_do(staff, "crm", "edit") is False           # door shut
    # Owner and Manager may do anything behind a door they can open.
    for role in ("owner", "manager"):
        m = people.member(_row(role=role))
        assert all(people.can_do(m, p, a) for p in people.PAGES for a, _l, _h in people.abilities_for(p))
    # An Accountant may change nothing — that is what read-only means.
    acct = people.member(_row(role="accountant", scopes={"can": ["money:send", "money:chase"]}))
    assert acct["scopes"]["can"] == []
    assert people.can_do(acct, "money", "send") is False and people.can_open(acct, "money") is True
    # An account that predates People is its own owner.
    assert people.can_do(None, "diary", "cancel") is True
    # Every ability belongs to a real door.
    assert set(people.ABILITIES) == set(people.PAGES)


def test_the_form_nests_the_abilities_under_their_door(as_owner):
    client, _ = as_owner
    html = client.get("/app/people").text
    assert 'data-door-block="diary"' in html and 'value="diary:add"' in html
    # Today is shown as where they land, never as a box to tick.
    assert 'data-door-always="today"' in html and 'data-door-block="today"' not in html
    assert 'name="page" value="today"' not in html
    assert "their first screen" in html
    assert "Cancel bookings" in html and "reaches the customer" in html
    assert "A door on its own lets them look. Tick what they may change." in html
    partial = _read("src/app/templates/app/_people_doors.html")
    assert "data-door-more" in partial and "more.hidden = !check.checked;" in partial
    assert 'face.textContent = on ? on + " thing"' in partial      # the door's own face says how much


def test_the_ticks_are_enforced_where_they_are_spent(as_staff, monkeypatch):
    """A tick nobody checks is decoration."""
    monkeypatch.setattr(db, "get_member", lambda tid, uid: _row(
        user_id="u-sam", role="staff", display_name="Sam",
        scopes={"pages": ["today", "diary"], "can": ["diary:add"]}))
    dashboard.navcache.invalidate("tm:t1")
    monkeypatch.setattr(dashboard.db, "list_live_bookings", lambda *a, **k: [])
    r = as_staff.post("/app/diary/cancel-all", data={"return_to": "/app/diary"})
    assert r.status_code == 403 and "permission" in r.text
    r = as_staff.post("/app/diary/b-1/status", data={"status": "cancelled"})
    assert r.status_code == 403
    src = _read("src/app/dashboard.py")
    assert 'require_can(ctx, "diary", "add")' in src and 'require_can(ctx, "diary", "cancel")' in src
    assert 'require_can(ctx, "money", "chase")' in src and 'require_can(ctx, "money", "send")' in src
    assert 'require_can(ctx, "inbox", "approve")' in src
    assert src.count('require_can(ctx, "money", "send")') == 2     # invoices and quotes
    # And the pages stop offering what they cannot use.
    money = _read("src/app/templates/app/money.html")
    diary = _read("src/app/templates/app/diary.html")
    assert "my_can.get('money:chase', True)" in money
    assert "my_can.get('diary:add', True)" in diary


def test_their_today_is_their_own(as_staff, monkeypatch):
    """Kris 2026-09-13: "Today shouldn't be a tickbox, it should just be the
    main screen for the employee … they should have their own Today page for
    what they've got on"."""
    desk = people.member(_row(role="staff", scopes={"pages": ["today", "diary"]}))
    draft = {"name": "Sarah", "facts": [{"kind": "draft", "topic": "drafts"}]}
    booking = {"name": "Bella", "facts": [{"kind": "booking", "topic": "bookings"}]}
    mixed = {"name": "Jo", "facts": [{"topic": "drafts"}, {"topic": "bookings"}]}
    assert people.card_is_theirs(desk, dict(draft)) is False        # no Inbox door
    assert people.card_is_theirs(desk, dict(booking)) is True
    # A card with one fact they can act on keeps that fact and loses the rest.
    m = dict(mixed, facts=list(mixed["facts"]))
    assert people.card_is_theirs(desk, m) is True and [f["topic"] for f in m["facts"]] == ["bookings"]
    # A manager's Today is untouched, and an unknown topic is never dropped —
    # Today must not go quiet because a new card wasn't listed here.
    assert people.card_is_theirs(people.member(_row(role="manager")), dict(draft)) is True
    assert people.card_is_theirs(desk, {"facts": [{"topic": "something-new"}]}) is True
    assert people.card_is_theirs(desk, {"facts": []}) is True
    # On the live page: no drafts, and no sight of the business's allowance.
    html = as_staff.get("/app/").text
    assert "Trial:" not in html and "data-usage-more" not in html
    assert 'data-tour="crm"' not in html
    home = _read("src/app/templates/app/home.html")
    assert "{% if people_admin is not defined or people_admin %}" in home
    dash = _read("src/app/dashboard.py")
    assert "people.card_is_theirs(_mine, c, cfg_row)" in dash
    assert "people.owns_booking(_mine, b, cfg_row)" in dash
