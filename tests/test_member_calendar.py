"""People slice 5 — a person's own calendar.

Kris, 2026-09-13, watching his wife accept an invitation and seeing Google name
Calendar on the consent screen: "she can see the business Calendar in BLAM8
while having her own Calendar to have events synced to… mostly used to delegate
work from the main calendar."

The lines this file exists to hold:

  - the grant is CALENDAR ONLY, and Google's incremental consent is off, so an
    owner who connected the business mailbox cannot end up with a mail-capable
    token for their personal calendar;
  - it draws on the SAME 100 unverified-app grants a Gmail mailbox does —
    Google caps users who authorise an unapproved SENSITIVE or restricted scope,
    and calendar.events is sensitive;
  - nothing is written to a calendar that has not been delegated by kind, and
    nothing from the past;
  - one person's dead grant never stops another's, and never reaches the diary.

Run: python -m pytest tests/test_member_calendar.py -q
"""
import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from app import calendar_sync, gmail, graph, member_calendar, people
from app.scheduling import DIARY_TZ

ROOT = Path(__file__).resolve().parents[1]
TENANT = "t1"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


CFG = {"intake_fields": ["pet_name"],
       "service_types": [{"name": "Meet and greet", "minutes": 30}]}
WALK = people.service_kind("Meet and greet")
# push_to_people reads the real clock and drops anything before today (in the
# diary's zone), so the booking the tests push must always sit ahead of it.
SOON = (datetime.now(DIARY_TZ) + timedelta(days=7)).date().isoformat()


def _m(user_id="u1", role="staff", does=(), active=True):
    return people.member({"user_id": user_id, "role": role, "active": active,
                          "display_name": "Sam",
                          "scopes": {"pages": ["today", "diary"], "does": list(does)}})


def _booking(bid="b1", date=SOON, kind="stay"):
    row = {"id": bid, "booking_date": date, "window": "day", "status": "confirmed",
           "summary": "Bella", "contact": "Jo", "source": "email"}
    if kind != "stay":
        row["resource_type"] = "Meet and greet"
        row["window"] = "10:00"
    return row


# ── who the work belongs to ────────────────────────────────────────────────
def test_an_empty_delegation_writes_to_nobodys_calendar():
    """owns_booking lets a staff member with nothing delegated SEE the whole
    diary — narrowing a page must never blank it. A calendar is the other way
    round: it is their own property, and nothing has been handed to them."""
    nothing = _m()
    assert people.owns_booking(nothing, _booking(), CFG) is True
    assert people.does_booking(nothing, _booking(), CFG) is False
    # …and that holds for the roles that can see everything, too.
    for role in ("owner", "manager", "accountant"):
        assert people.does_booking(_m(role=role), _booking(), CFG) is False


def test_only_the_kinds_ticked_travel():
    desk = _m(does=[people.DESK])
    walker = _m(does=[WALK])
    assert people.does_booking(desk, _booking(kind="stay"), CFG) is True
    assert people.does_booking(desk, _booking(kind="appt"), CFG) is False
    assert people.does_booking(walker, _booking(kind="appt"), CFG) is True
    assert people.does_booking(walker, _booking(kind="stay"), CFG) is False
    # Somebody who has left keeps nothing coming.
    assert people.does_booking(_m(does=[people.DESK], active=False), _booking(), CFG) is False
    assert people.does_booking(None, _booking(), CFG) is False


def test_the_page_and_the_calendar_agree_about_the_kind():
    """Both read people.kind_of, so a booking can never be theirs on the diary
    and somebody else's in the calendar."""
    assert people.kind_of(_booking(kind="stay"), CFG) == people.DESK
    assert people.kind_of(_booking(kind="appt"), CFG) == WALK
    for kind in ("stay", "appt"):
        b = _booking(kind=kind)
        m = _m(does=[people.kind_of(b, CFG)])
        assert people.owns_booking(m, b, CFG) is people.does_booking(m, b, CFG) is True


# ── the grant itself ───────────────────────────────────────────────────────
def test_the_grant_asks_for_a_calendar_and_nothing_else():
    for scope in (gmail.CALENDAR_SCOPES, graph.CALENDAR_SCOPES):
        assert "calendar" in scope.lower()
    # No mailbox anywhere in either — "email" is the OpenID claim that names
    # which account they picked, and grants nothing.
    assert "gmail" not in gmail.CALENDAR_SCOPES
    assert not [s for s in graph.CALENDAR_SCOPES.split() if s.startswith("Mail.")]
    assert "Mail.ReadWrite" not in graph.CALENDAR_SCOPES
    # offline_access: without a refresh token the grant dies at the first expiry.
    assert "offline_access" in graph.CALENDAR_SCOPES


def test_google_is_asked_not_to_merge_scopes_already_granted():
    """An owner whose business mailbox holds gmail.readonly must not get that
    back on the token for their personal calendar."""
    url = member_calendar.authorize_url("google", "st8")
    assert "include_granted_scopes" not in url
    assert "calendar.events" in url and "gmail" not in url
    assert "state=st8" in url
    # The mailbox connect keeps incremental consent — unchanged.
    assert "include_granted_scopes=true" in gmail.build_authorize_url("st8")


def test_microsofts_exchange_and_refresh_echo_the_narrow_scope():
    src = _read("src/app/graph.py")
    assert "async def exchange_code_for_tokens(code: str, scope: str | None = None)" in src
    assert "async def refresh_tokens(refresh_token: str, scope: str | None = None)" in src
    assert '"scope": scope or SCOPES' in src


def test_the_account_they_picked_is_read_from_the_id_token():
    def token(claims):
        body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        return {"id_token": "hdr." + body + ".sig"}
    assert member_calendar.account_email(token({"email": "Em@Gmail.com"})) == "em@gmail.com"
    assert member_calendar.account_email(token({"preferred_username": "em@outlook.com"})) \
        == "em@outlook.com"
    # Junk never raises — the label is a nicety, not a credential.
    assert member_calendar.account_email({}) is None
    assert member_calendar.account_email({"id_token": "not-a-jwt"}) is None
    assert member_calendar.account_email({"id_token": "a.!!!!.c"}) is None
    assert member_calendar.account_email(token({"sub": "123"})) is None


# ── the 100 grants ─────────────────────────────────────────────────────────
def test_a_persons_calendar_draws_on_the_same_hundred(monkeypatch):
    """calendar.events is SENSITIVE, and Google's unverified-app cap counts
    users who authorise an unapproved sensitive OR restricted scope. Counting
    only mailboxes would have quietly overrun it."""
    from app import slots
    monkeypatch.setattr(slots.settings, "google_oauth_verified", False)
    monkeypatch.setattr(slots.db, "count_gmail_connections", lambda: 97)
    monkeypatch.setattr(slots.db, "count_google_member_calendars", lambda: 2)
    slots._cache["at"] = -1e9
    s = slots.google_slots()
    assert s["used"] == 99 and s["remaining"] == 1 and s["low"] is True
    slots._cache["at"] = -1e9


def test_reconnecting_your_own_is_the_only_way_past_a_full_cap(monkeypatch):
    from app import oauth
    monkeypatch.setattr(oauth.slots, "google_slots",
                        lambda: {"verified": False, "full": True, "used": 100,
                                 "cap": 100, "remaining": 0, "low": True})
    monkeypatch.setattr(oauth.db, "get_member_calendar", lambda t, u: None)
    assert oauth._member_calendar_allowed(TENANT, "u1") is False
    monkeypatch.setattr(oauth.db, "get_member_calendar", lambda t, u: {"user_id": u})
    assert oauth._member_calendar_allowed(TENANT, "u1") is True
    # A tenant already holding a Google grant is NOT a way in — the mailbox
    # gate's escape hatch is about the same Google user re-consenting, and a
    # staff member is a different one.
    src = _read("src/app/oauth.py")
    assert "_tenant_has_gmail" not in src.split("def _member_calendar_allowed")[1].split("def ")[0]


# ── tokens ─────────────────────────────────────────────────────────────────
def _cal(expires_in=3600, **extra):
    when = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    return {"tenant_id": TENANT, "user_id": "u1", "provider": "google",
            "access_token": "live", "refresh_token": "r0",
            "token_expires": when.isoformat(), **extra}


@pytest.mark.asyncio
async def test_a_live_token_is_reused_and_a_stale_one_refreshed(monkeypatch):
    assert await member_calendar.live_token(_cal()) == "live"
    saved = {}
    monkeypatch.setattr(member_calendar.gmail, "refresh_tokens",
                        lambda rt: _async({"access_token": "fresh", "expires_in": 3600}))
    monkeypatch.setattr(member_calendar.db, "update_member_calendar_tokens",
                        lambda t, u, a, r, e: saved.update(access=a, refresh=r))
    stale = _cal(expires_in=-60)
    assert await member_calendar.live_token(stale) == "fresh"
    # Google reuses the refresh token and omits it from the response.
    assert saved == {"access": "fresh", "refresh": "r0"}
    assert stale["access_token"] == "fresh"


@pytest.mark.asyncio
async def test_a_grant_with_no_refresh_token_says_so_rather_than_dying_quietly():
    with pytest.raises(RuntimeError):
        await member_calendar.live_token(_cal(expires_in=-60, refresh_token=None))
    msg = member_calendar.why_it_stopped(RuntimeError("no refresh token"))
    assert "connecting again" in msg
    r = httpx.Response(401, request=httpx.Request("POST", "https://example.test"))
    assert "connecting again" in member_calendar.why_it_stopped(
        httpx.HTTPStatusError("x", request=r.request, response=r))


def _async(value):
    async def run():
        return value
    return run()


# ── the push ───────────────────────────────────────────────────────────────
class _Fake:
    """Stands in for src/app/db.py across one push."""

    def __init__(self, bookings, cals, members, events=None):
        self.bookings, self.cals, self.members = bookings, cals, members
        self.events = list(events or [])
        self.errors: list = []

    def list_member_calendars(self, t):
        return list(self.cals)

    def get_member_calendar(self, t, u):
        return next((c for c in self.cals if c["user_id"] == u), None)

    def list_members(self, t):
        return list(self.members)

    def get_scheduling_config(self, t):
        return CFG

    def list_confirmed_bookings(self, t, email_log_id=None, booking_id=None, from_date=None):
        return [b for b in self.bookings if not from_date or b["booking_date"] >= from_date]

    def list_member_booking_events(self, t, booking_id=None, user_id=None):
        return [e for e in self.events
                if (booking_id is None or e["booking_id"] == booking_id)
                and (user_id is None or e["user_id"] == user_id)]

    def set_member_booking_event(self, t, u, b, e):
        self.events.append({"tenant_id": t, "user_id": u, "booking_id": b, "event_id": e})

    def delete_member_booking_event(self, t, u, b):
        self.events = [e for e in self.events
                       if not (e["user_id"] == u and e["booking_id"] == b)]

    def set_member_calendar_error(self, t, u, msg):
        self.errors.append((u, msg))


def _rig(monkeypatch, fake, created=None, deleted=None):
    monkeypatch.setattr(calendar_sync, "db", fake)
    monkeypatch.setattr(member_calendar, "db", fake)

    async def create(token, summary, start, end, all_day=False, body_text=""):
        (created if created is not None else []).append((token, summary, start))
        return "ev-" + summary

    async def delete(token, event_id):
        (deleted if deleted is not None else []).append(event_id)

    class Backend:
        create_calendar_event = staticmethod(create)
        delete_calendar_event = staticmethod(delete)
    monkeypatch.setattr(member_calendar, "backend", lambda p: Backend)
    monkeypatch.setattr(calendar_sync.member_calendar, "backend", lambda p: Backend)
    return Backend


@pytest.mark.asyncio
async def test_each_person_gets_their_own_work_and_only_theirs(monkeypatch):
    stay, appt = _booking("b1", kind="stay"), _booking("b2", kind="appt")
    cals = [_cal(), {**_cal(), "user_id": "u2"}]
    members = [{"user_id": "u1", "role": "staff", "active": True,
                "scopes": {"pages": ["today", "diary"], "does": [people.DESK]}},
               {"user_id": "u2", "role": "staff", "active": True,
                "scopes": {"pages": ["today", "diary"], "does": [WALK]}}]
    fake = _Fake([stay, appt], cals, members)
    made: list = []
    _rig(monkeypatch, fake, created=made)
    await calendar_sync.push_to_people(TENANT)
    assert {(e["user_id"], e["booking_id"]) for e in fake.events} == {("u1", "b1"), ("u2", "b2")}
    assert len(made) == 2


@pytest.mark.asyncio
async def test_nothing_is_written_twice_and_nothing_from_the_past(monkeypatch):
    old = _booking("b0", date="2020-01-01")
    soon = _booking("b1")
    members = [{"user_id": "u1", "role": "staff", "active": True,
                "scopes": {"pages": ["today", "diary"], "does": [people.DESK]}}]
    fake = _Fake([old, soon], [_cal()], members,
                 events=[{"user_id": "u1", "booking_id": "b1", "event_id": "ev-old"}])
    made: list = []
    _rig(monkeypatch, fake, created=made)
    await calendar_sync.push_to_people(TENANT)
    assert made == []                      # b1 already there, b0 is history
    fake.events = []
    await calendar_sync.push_to_people(TENANT)
    assert [e["booking_id"] for e in fake.events] == ["b1"]


@pytest.mark.asyncio
async def test_one_dead_grant_records_its_reason_and_never_stops_the_others(monkeypatch):
    members = [{"user_id": "u1", "role": "staff", "active": True,
                "scopes": {"pages": ["today", "diary"], "does": [people.DESK]}},
               {"user_id": "u2", "role": "staff", "active": True,
                "scopes": {"pages": ["today", "diary"], "does": [people.DESK]}}]
    dead = {**_cal(expires_in=-60), "refresh_token": None}
    fake = _Fake([_booking("b1")], [dead, {**_cal(), "user_id": "u2"}], members)
    _rig(monkeypatch, fake)
    await calendar_sync.push_to_people(TENANT)          # must not raise
    assert [e["user_id"] for e in fake.events] == ["u2"]
    assert ("u1", "Your calendar needs connecting again — the permission ran out.") in fake.errors
    assert ("u2", None) in fake.errors                  # a working one is cleared


@pytest.mark.asyncio
async def test_a_cancellation_takes_it_back_out_of_their_calendar(monkeypatch):
    members = [{"user_id": "u1", "role": "staff", "active": True,
                "scopes": {"pages": ["today", "diary"], "does": [people.DESK]}}]
    fake = _Fake([], [_cal()], members,
                 events=[{"user_id": "u1", "booking_id": "b1", "event_id": "ev-1"}])
    gone: list = []
    _rig(monkeypatch, fake, deleted=gone)
    await calendar_sync.remove_from_people(TENANT, booking_id="b1")
    assert gone == ["ev-1"] and fake.events == []


@pytest.mark.asyncio
async def test_disconnecting_clears_everything_that_person_was_given(monkeypatch):
    members = [{"user_id": "u1", "role": "staff", "active": True,
                "scopes": {"pages": ["today", "diary"], "does": [people.DESK]}}]
    fake = _Fake([], [_cal()], members, events=[
        {"user_id": "u1", "booking_id": "b1", "event_id": "ev-1"},
        {"user_id": "u1", "booking_id": "b2", "event_id": "ev-2"},
        {"user_id": "u2", "booking_id": "b3", "event_id": "ev-3"}])
    gone: list = []
    _rig(monkeypatch, fake, deleted=gone)
    await calendar_sync.remove_from_people(TENANT, user_id="u1")
    assert sorted(gone) == ["ev-1", "ev-2"]
    assert [e["user_id"] for e in fake.events] == ["u2"]


# ── the wiring ─────────────────────────────────────────────────────────────
def test_the_business_calendar_and_the_peoples_are_independent():
    """A business that never connected a calendar still fills its people's, and
    the tenant push failing must not skip them."""
    src = _read("src/app/calendar_sync.py")
    body = src.split("async def push_bookings")[1].split("async def _push_business")[0]
    assert "await _push_business(" in body and "await push_to_people(" in body
    # The person's event id lives in its own table — deleting one calendar's
    # event can never blank the other's.
    assert "set_member_booking_event" in src and "set_booking_calendar_event" in src


def test_the_callback_branches_before_it_asks_for_mail_scopes():
    src = _read("src/app/oauth.py")
    for half in src.split("tenant_id, mode, state_user =")[1:]:
        before = half.split("exchange_code_for_tokens")[0]
        assert "if mode == member_calendar.MODE:" in before
    assert src.count("_finish_member_calendar(request, tenant_id, state_user") == 2
    # Whose calendar it is comes from the bound state, never the URL.
    connect = src.split("def my_calendar")[1].split("async def _finish_member_calendar")[0]
    assert "user_id=ctx.user_id" in connect
    finish = src.split("async def _finish_member_calendar")[1] \
        .split("def _spawn_member_backfill")[0]
    code = finish.split('"""')[2]          # past the docstring, which names it
    assert "email_connections" not in code and "upsert_email_connection" not in code
    assert "db.save_member_calendar(" in code


def test_the_disconnect_route_sits_above_the_wildcard():
    """FastAPI matches in declaration order, and "/{user_id}" swallows a word —
    the same trap slice 3 hit with /say."""
    src = _read("src/app/people_routes.py")
    assert src.index('@router.post("/calendar/off"') < src.index('@router.get("/{user_id}"')
    assert src.index('@router.post("/calendar/off"') < src.index('@router.post("/{user_id}"')


def test_their_page_offers_it_to_them_and_nobody_else():
    html = _read("src/app/templates/app/person.html")
    card = html.split("data-cal>")[1]
    assert "/oauth/my-calendar?provider=google" in card
    assert "/oauth/my-calendar?provider=microsoft" in card
    # Only they may connect it; an admin may disconnect it (someone who left).
    assert "Only they can connect it." in card
    assert 'action="/app/people/calendar/off"' in card
    assert "{% if own or can_edit %}" in card
    # And it says what will actually happen.
    assert "Only what you do" in card and "from today on" in card


# ── found live on the rig, 2026-09-14 ──────────────────────────────────────
def test_the_owner_can_say_what_they_do():
    """The owner's page offered "Connect Google Calendar" and then "nothing is
    handed to you by kind yet — ask whoever runs BLAM8", which the owner IS.
    Their role, doors and whether they still work here are nobody's to change;
    what they DO is not a permission, it is who takes the dogs in."""
    src = _read("src/app/people_routes.py")
    branch = src.split('elif people.is_admin(mine) and people.normalise_role(row.get("role")) == "owner":')[1]
    branch = branch.split("    try:")[0]
    assert '"does": [k for k in kind if k in known]' in branch
    # …and nothing else about the owner's row is touched by it.
    for forbidden in ('"role"', '"active"', '"pages"'):
        assert forbidden not in branch, forbidden
    html = _read("src/app/templates/app/person.html")
    assert "{% elif can_edit and person.role == 'owner' %}" in html
    assert "data-kinds-owner" in html
    assert "tick what you do above" in html          # not "ask whoever runs BLAM8"


def test_a_manager_works_the_floor_too():
    """Kinds were kept only for a tailorable role, which quietly meant a
    manager could never be given any either. The one role that does none of
    the work is the accountant."""
    src = _read("src/app/people_routes.py")
    assert 'if new_role not in people.READ_ONLY else []' in src
    assert people.READ_ONLY == ("accountant",)
