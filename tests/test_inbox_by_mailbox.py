"""The Inbox a person gets is their own mailbox (People slice 4, the last of it).

Kris, 2026-09-14: "I feel like the inbox thing should only happen if it's a
business email for that person." So the Inbox is not a door with a filter
behind it — it IS a mailbox. Slicing a shared inbox by rules is a filter, and a
filter is something you can be wrong about: one mis-labelled email and a staff
member is reading a complaint about themselves. A whole mailbox that is
genuinely theirs has no edge case.

Two consequences that fall out, and both are held here:

  - a ONE-MAILBOX business can never give staff an Inbox, because that mailbox
    is the owner's own;
  - the owner's inbox is never assignable — it is the oldest connection, and it
    stays the business's (Kris chose "any but the owner's").

Run: python -m pytest tests/test_inbox_by_mailbox.py -q
"""
from pathlib import Path

import pytest

from app import people

ROOT = Path(__file__).resolve().parents[1]
TENANT = "t1"
OWNER_BOX, HERS, HIS = "mb-owner", "mb-em", "mb-sam"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _m(role="staff", pages=("today", "inbox"), boxes=(HERS,), can=()):
    return people.member({"user_id": "u1", "role": role, "active": True,
                          "display_name": "Em",
                          "scopes": {"pages": list(pages), "can": list(can),
                                     "mailboxes": list(boxes)}})


# ── the Inbox IS a mailbox ─────────────────────────────────────────────────
def test_a_tick_with_no_mailbox_behind_it_is_a_shut_door():
    """Not a half-open door — shut. Dropped in clean_scopes, so every read of
    a person agrees rather than each page having to remember."""
    none = _m(boxes=())
    assert "inbox" not in none["scopes"]["pages"]
    assert people.can_open(none, "inbox") is False
    theirs = _m(boxes=(HERS,))
    assert "inbox" in theirs["scopes"]["pages"]
    assert people.can_open(theirs, "inbox") is True


def test_the_abilities_go_with_the_door():
    """Approve-and-send behind a door that isn't open would be a write with no
    page — clean_scopes already drops an ability whose door is shut, and the
    mailbox rule runs first so it applies here too."""
    none = _m(boxes=(), can=("inbox:approve", "inbox:write"))
    assert none["scopes"]["can"] == []
    theirs = _m(boxes=(HERS,), can=("inbox:approve",))
    assert theirs["scopes"]["can"] == ["inbox:approve"]


def test_only_a_tailorable_role_is_ever_given_a_mailbox():
    """An owner is not "given" the inbox — it is theirs. A manager reads the
    business's mail; an accountant has no inbox at all."""
    for role in ("owner", "manager", "accountant"):
        m = people.member({"user_id": "u", "role": role,
                           "scopes": {"mailboxes": [HERS], "pages": list(people.PAGES)}})
        assert m["scopes"]["mailboxes"] == []
        assert people.inbox_is_theirs_only(m) is False
    assert people.inbox_is_theirs_only(_m()) is True
    assert people.inbox_is_theirs_only(None) is False


def test_the_owners_inbox_is_never_on_the_list():
    """list_mailboxes is oldest-first and the oldest is the tenant's default —
    the owner's own. Kris, choosing between the three readings: any but that
    one."""
    boxes = [{"id": OWNER_BOX}, {"id": HERS}, {"id": HIS}]
    assert [b["id"] for b in people.assignable_mailboxes(boxes)] == [HERS, HIS]
    # A ONE-mailbox business has nothing to give, so staff never get an Inbox.
    assert people.assignable_mailboxes([{"id": OWNER_BOX}]) == []
    assert people.assignable_mailboxes([]) == []


# ── whose mail is whose ────────────────────────────────────────────────────
def test_mail_with_no_mailbox_on_it_belongs_to_the_owner():
    """Legacy rows carry a null mailbox_id and render under the DEFAULT one.
    Without this they would be nobody's — and "nobody's" would quietly read as
    "everybody's"."""
    em = _m(boxes=(HERS,))
    assert people.may_read_mailbox(em, HERS) is True
    assert people.may_read_mailbox(em, HIS) is False
    assert people.may_read_mailbox(em, None, OWNER_BOX) is False
    assert people.may_read_mailbox(em, None, HERS) is True
    # …and with no default known at all, it is not theirs.
    assert people.may_read_mailbox(em, None, None) is False
    # Everybody else reads everything.
    assert people.may_read_mailbox(_m(role="manager"), HIS) is True
    assert people.may_read_mailbox(None, HIS) is True


# ── the form ───────────────────────────────────────────────────────────────
def test_the_picker_says_what_it_does_and_what_to_do_when_there_is_nothing():
    html = _read("src/app/templates/app/_people_doors.html")
    block = html.split("{% if p == 'inbox' %}")[1].split("{% endif %}")[0]
    assert 'name="mailbox"' in block and "Which address is theirs" in block
    assert "They read that mailbox and nothing else" in block
    # A one-mailbox business is told why the list is empty, and where to go.
    assert "Your main inbox stays yours" in html and "/app/mailboxes" in html
    # The face warns BEFORE the save, rather than the door quietly vanishing.
    assert "pick their address" in html


def test_the_form_is_never_trusted_for_which_mailbox():
    """A crafted post must not be able to hand somebody the owner's inbox."""
    src = _read("src/app/people_routes.py")
    assert src.count('givable = {b["id"] for b in _mailboxes(ctx)}') == 2   # invite + save
    assert src.count('"mailboxes": [m for m in mailbox if m in givable]') == 2
    # _mailboxes itself is the list MINUS the owner's.
    assert "people.assignable_mailboxes(boxes)" in src


# ── the pages ──────────────────────────────────────────────────────────────
def test_every_inbox_surface_filters_and_every_write_refuses():
    src = _read("src/app/dashboard.py")
    # The list, the thread (page + live fragment share _thread_data), Sent, and
    # "Draft now" on a paused contact.
    assert src.count("_their_mail_only(ctx,") >= 4
    assert "convos = _their_mail_only(ctx, convos)" in src
    # Eight by-hand routes act on one message; all of them check first.
    assert src.count("_require_their_mail(ctx") + src.count("_require_their_log(ctx") >= 8
    # A 403, not a redirect: a form post that lands elsewhere reads as "it worked".
    assert "status_code=403" in src.split("def _require_their_mail")[1].split("def ")[0]


def test_the_owner_never_pays_a_round_trip_to_be_told_they_own_their_inbox():
    src = _read("src/app/dashboard.py")
    lazy = src.split("def _require_their_log")[1].split("\ndef ")[0]
    assert "if not _inbox_is_theirs(ctx):" in lazy and "return" in lazy


def test_their_own_mailbox_is_the_only_tab_and_the_only_thing_scanned():
    src = _read("src/app/dashboard.py")
    block = src.split("# Their own address, and only it")[1].split("box_names =")[0]
    assert 'boxes = [m for m in boxes if m["id"] in _mine]' in block
    scan = src.split("if _inbox_is_theirs(ctx):\n        # Never [None]")[1].split("\n\n")[0]
    assert 'scan = [m["id"] for m in boxes]' in scan


# ── the assistant ──────────────────────────────────────────────────────────
def test_the_inbox_tools_are_handed_the_person():
    """These cannot be filtered after the fact — by then the rows are folded
    into a shape that has lost the mailbox."""
    from app import concierge_data
    assert concierge_data._MEMBER_AWARE == frozenset(
        ("get_inbox_overview", "search_conversations", "get_conversation",
         "list_bookings",   # narrowed like the Diary page (people plan step 4)
         "list_awaiting_approval"))   # the asker's own cards, not a mailbox lens
    src = _read("src/app/concierge_data.py")
    assert "if name in _MEMBER_AWARE else" in src
    for fn in concierge_data._MEMBER_AWARE:
        assert f"def _{fn}(tenant_id, client, args, member=None" in src, fn   # list_awaiting_approval also takes user_id


@pytest.mark.asyncio
async def test_the_assistant_reads_their_mailbox_and_not_the_inbox(monkeypatch):
    from app import concierge_data
    rows = [{"id": "e1", "mailbox_id": HERS, "from_address": "jo@x.com"},
            {"id": "e2", "mailbox_id": OWNER_BOX, "from_address": "jo@x.com"},
            {"id": "e3", "mailbox_id": None, "from_address": "jo@x.com"}]
    monkeypatch.setattr(concierge_data.db, "list_mailboxes",
                        lambda t, client=None: [{"id": OWNER_BOX}, {"id": HERS}])
    kept = concierge_data._their_mailbox_only(TENANT, None, _m(), rows)
    assert [r["id"] for r in kept] == ["e1"]        # not the owner's, not the legacy one
    assert concierge_data._their_mailbox_only(TENANT, None, _m(role="manager"), rows) == rows


@pytest.mark.asyncio
async def test_a_mailbox_lens_that_cannot_be_read_shows_nothing(monkeypatch):
    """Best-effort in ONE direction. Everywhere else a failed read degrades to
    showing more; here that is the leak."""
    from app import concierge_data

    def boom(*a, **k):
        raise RuntimeError("supabase down")
    monkeypatch.setattr(concierge_data.db, "list_mailboxes", boom)
    assert concierge_data._their_mailbox_only(
        TENANT, None, _m(), [{"mailbox_id": HERS}]) == []
