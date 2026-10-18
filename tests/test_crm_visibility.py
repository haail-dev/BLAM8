"""Staff CRM — whose customers, and which fields (People slice 3).

"The Diary" is every booking and "the CRM" is every contact: a door is a coarse
thing. A person who does the drop-offs needs the people whose dogs they take
in, not the whole book — and Kris, when the roles were settled: default *Staff
can see* off "for anything onboarding marked as access information (codes,
keys), on for name, phone, the dependants' names and notes."

What this file exists to hold:

  - the default is read from the tenant's own words, and the word list is TIGHT
    — "Key facts" must not silently hide itself;
  - a field marked private is still open to somebody given "See private
    details", the tick People already offers;
  - a staff member's CRM is the customers behind their own work, on the list,
    on the card, on the writes and in the assistant — all four, because any one
    of them left open is the whole book;
  - a field they cannot see is a field they cannot write.

Run: python -m pytest tests/test_crm_visibility.py -q
"""
from pathlib import Path

import pytest

from app import crm_visibility as vis, people

ROOT = Path(__file__).resolve().parents[1]
TENANT = "t1"

CFG = {"intake_fields": ["pet_name"],
       "service_types": [{"name": "Meet and greet", "minutes": 30}]}
WALK = people.service_kind("Meet and greet")


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _m(role="staff", does=("desk",), can=()):
    return people.member({"user_id": "u1", "role": role, "active": True,
                          "display_name": "Sam",
                          "scopes": {"pages": ["today", "diary", "crm"],
                                     "does": list(does), "can": list(can)}})


def _b(contact, kind="stay"):
    row = {"id": "b", "contact": contact, "booking_date": "2026-09-20",
           "window": "day", "status": "confirmed", "summary": "Bella"}
    if kind != "stay":
        row["resource_type"] = "Meet and greet"
    return row


# ── the default, from the tenant's own words ───────────────────────────────
def test_access_information_starts_shut():
    for label in ("Alarm code", "Key safe", "key-safe number", "Gate code", "Door codes",
                  "Access code", "Entry code", "Combination", "Passcode", "Password",
                  "PIN", "Sort code", "Account number", "Card number", "CVV",
                  "Access notes", "Access details", "Key box"):
        assert vis.default_staff_can_see({"label": label}) is False, label
    # …and so does a harmless label whose HINT gives it away.
    assert vis.default_staff_can_see(
        {"label": "Getting in", "extraction_hint": "the gate code and key safe"}) is False


def test_the_word_list_is_tight_on_purpose():
    """A field that silently hides itself is a worse failure than one the owner
    has to tick shut: the first is invisible, the second is on a page in front
    of them."""
    for label in ("Key facts", "Coding preference", "Postcode", "Pinned note", "Role",
                  "Phone", "Fleet size", "Budget", "Discount code", "Encoded reference",
                  "Breed", "Vehicle reg", "Allergies", "Preferred day"):
        assert vis.default_staff_can_see({"label": label}) is True, label


def test_nobody_but_staff_is_narrowed_at_all():
    shut = {"key": "alarm_code", "label": "Alarm code", "staff_can_see": False}
    for role in ("owner", "manager"):
        assert vis.can_see_field(_m(role=role), shut) is True
    assert vis.can_see_field(None, shut) is True          # predates People
    assert vis.can_see_field(_m(), shut) is False


def test_private_details_is_the_tick_that_opens_it():
    """People already offers "See private details — access codes, keys, medical
    notes". This is not a wall between staff and the truth; it is the business
    deciding, per person, who holds the keys."""
    shut = {"key": "alarm_code", "label": "Alarm code", "staff_can_see": False}
    assert vis.can_see_field(_m(), shut) is False
    assert vis.can_see_field(_m(can=["crm:private"]), shut) is True


def test_a_value_is_dropped_not_blanked():
    """A row reading "Alarm code: hidden" tells somebody there is one to ask
    for."""
    defs = [{"key": "alarm_code", "label": "Alarm code", "staff_can_see": False},
            {"key": "breed", "label": "Breed", "staff_can_see": True}]
    data = {"alarm_code": "1984", "breed": "Collie"}
    assert vis.redact(data, defs, _m()) == {"breed": "Collie"}
    assert vis.redact(data, defs, _m(role="owner")) == data
    assert vis.visible_defs(defs, _m()) == [defs[1]]


def test_a_dependants_own_fields_obey_the_same_rule():
    """The "Property" group is exactly where a key-safe number lives."""
    group = {"label": "Property",
             "fields": [{"key": "key_safe", "label": "Key safe"},
                        {"key": "floors", "label": "Floors"}]}
    assert [f["key"] for f in vis.visible_group_fields(group, _m())] == ["floors"]
    assert vis.redact_dependant({"key_safe": "C1234", "floors": "2"}, group, _m()) == {"floors": "2"}
    assert len(vis.visible_group_fields(group, _m(role="manager"))) == 2


# ── whose customers ────────────────────────────────────────────────────────
def test_delegation_narrows_and_an_empty_one_does_not_blank():
    """The same rule the diary uses: a person is never shown an empty page
    because a box was left unticked."""
    assert vis.narrows_contacts(_m(does=["desk"])) is True
    assert vis.narrows_contacts(_m(does=[])) is False
    assert vis.narrows_contacts(_m(role="manager", does=["desk"])) is False
    assert vis.narrows_contacts(None) is False


def test_their_customers_are_the_ones_behind_their_own_work():
    books = [_b("jo@x.com", "stay"), _b("SAM@x.com", "stay"), _b("pat@x.com", "appt")]
    assert vis.theirs(_m(does=["desk"]), books, CFG) == {"jo@x.com", "sam@x.com"}
    assert vis.theirs(_m(does=[WALK]), books, CFG) == {"pat@x.com"}
    assert vis.theirs(_m(does=[]), books, CFG) == set()


def test_the_list_and_the_card_agree():
    allowed = {"jo@x.com"}
    rows = [{"email": "Jo@X.com"}, {"email": "pat@x.com"}]
    assert vis.keep_contacts(rows, _m(), allowed) == [rows[0]]
    assert vis.may_open(rows[0], _m(), allowed) is True
    assert vis.may_open(rows[1], _m(), allowed) is False
    # An owner is untouched by either.
    assert vis.keep_contacts(rows, _m(role="owner"), set()) == rows
    assert vis.may_open(rows[1], _m(role="owner"), set()) is True


# ── the wiring ─────────────────────────────────────────────────────────────
def test_the_whole_book_pages_are_not_offered_or_reachable():
    """A "pipeline" of a subset is not a pipeline. The menu must agree with the
    routes rather than offering links that bounce."""
    src = _read("src/app/dashboard.py")
    for active in ("crm_companies", "crm_pipeline", "crm_sequences", "crm_tasks",
                   "crm_reports", "crm_fields", "crm_settings"):
        assert f'"{active}"' in src.split("_CRM_WHOLE_BOOK = (")[1].split(")")[0], active
    assert 'if active in _CRM_WHOLE_BOOK and crm_visibility.narrows_contacts(m):' in src
    nav = _read("src/app/templates/app/_crm_nav.html")
    assert "{% if not crm_mine %}" in nav
    assert '"crm_mine": crm_visibility.narrows_contacts(me),' in src


def test_a_field_they_cannot_see_is_a_field_they_cannot_write():
    """Without this the form could post f_alarm_code and set what the page
    refused to show."""
    src = _read("src/app/dashboard.py")
    body = src.split("async def crm_contact_fields")[1].split("@router.post")[0]
    assert "crm_visibility.visible_defs(defs, _me(ctx))" in body


def test_every_by_hand_write_checks_the_tick_and_the_scope():
    src = _read("src/app/dashboard.py")
    guard = src.split("def require_crm_write")[1].split("def _crm_lens")[0]
    assert 'require_can(ctx, "crm", "edit")' in guard
    assert "crm_visibility.may_open(contact, mine, allowed)" in guard
    # basics, field values, a note, a ladder move, and the three schema routes
    assert src.count("require_crm_write(ctx") >= 8


def test_somebody_elses_customer_is_not_a_404():
    """They exist — they are just not this person's work. "Not found" would
    send somebody looking for a bug."""
    src = _read("src/app/dashboard.py")
    assert 'RedirectResponse("/app/crm?msg=nottheirs", status_code=303)' in src
    html = _read("src/app/templates/app/crm_contacts.html")
    assert "isn't one of yours" in html
    # …and it does not fade: a warning is "this still needs you", not "what
    # happened" (tests/test_flash.py).
    block = html.split("nottheirs")[1].split("{% endif %}")[0]
    assert "data-flash" not in block[block.index("<div"):]


def test_the_owner_can_flip_any_field_on_the_page():
    html = _read("src/app/templates/app/crm_fields.html")
    assert 'name="staff_can_see"' in html and "data-staff-sees" in html
    src = _read("src/app/dashboard.py")
    assert '"staff_can_see_set": True' in src      # a human decided; leave it alone
    fields_src = _read("src/app/crm_fields.py")
    assert '"staff_can_see": crm_visibility.default_staff_can_see(definition)' in fields_src


# ── the assistant narrows exactly as the pages do ──────────────────────────
def test_the_whole_book_tools_close_with_the_whole_book_pages():
    from app import concierge_data
    names = {t["name"] for t in concierge_data.tools_for(True, _m(does=["desk"]))}
    for tool in concierge_data.WHOLE_BOOK_TOOLS:
        assert tool not in names, tool
    # One customer at a time is still theirs to ask about.
    assert "search_contacts" in names and "get_contact_record" in names
    # A person with nothing delegated is not narrowed, and keeps them.
    wide = {t["name"] for t in concierge_data.tools_for(True, _m(does=[]))}
    assert concierge_data.WHOLE_BOOK_TOOLS <= wide


@pytest.mark.asyncio
async def test_the_assistant_refuses_the_whole_book_as_well_as_hiding_it(monkeypatch):
    from app import concierge_data
    out = await concierge_data.execute(TENANT, None, "list_dormant_contacts", {},
                                       full_qa=True, member=_m(does=["desk"]))
    assert out == concierge_data.NOT_THEIRS
    assert "isn't one of the ones you work with" in out["message"]


@pytest.mark.asyncio
async def test_a_search_comes_back_narrowed(monkeypatch):
    from app import concierge_data
    monkeypatch.setattr(concierge_data, "_EXECUTORS", {
        **concierge_data._EXECUTORS,
        "search_contacts": lambda t, c, a: {"contacts": [{"email": "jo@x.com"},
                                                         {"email": "pat@x.com"}], "count": 2}})
    monkeypatch.setattr(concierge_data.db, "list_booking_contacts",
                        lambda t, s, client=None: [_b("jo@x.com")])
    monkeypatch.setattr(concierge_data.db, "get_scheduling_config",
                        lambda t, client=None: CFG)
    out = await concierge_data.execute(TENANT, None, "search_contacts", {},
                                       full_qa=True, member=_m(does=["desk"]))
    assert out == {"contacts": [{"email": "jo@x.com"}], "count": 1}


@pytest.mark.asyncio
async def test_one_customer_is_refused_and_their_private_fields_dropped(monkeypatch):
    from app import concierge_data
    record = {"found": True, "email": "jo@x.com", "name": "Jo",
              "fields": [{"label": "Alarm code", "value": "1984"},
                         {"label": "Breed", "value": "Collie"}],
              "dependants": [{"kind": "Dog", "name": "Bella",
                              "facts": {"key_safe": "C1", "breed": "Collie"}}]}
    monkeypatch.setattr(concierge_data, "_EXECUTORS", {
        **concierge_data._EXECUTORS,
        "get_contact_record": lambda t, c, a: dict(record, email=a.get("contact"))})
    monkeypatch.setattr(concierge_data.db, "list_booking_contacts",
                        lambda t, s, client=None: [_b("jo@x.com")])
    monkeypatch.setattr(concierge_data.db, "get_scheduling_config", lambda t, client=None: CFG)
    monkeypatch.setattr(concierge_data.db, "crm_list_field_definitions",
                        lambda t, client=None: [{"key": "alarm_code", "label": "Alarm code"},
                                                {"key": "breed", "label": "Breed"}])
    monkeypatch.setattr(concierge_data.db, "list_requirement_groups",
                        lambda t, client=None: [{"label": "Dog", "label_plural": "Dogs",
                                                 "fields": [{"key": "key_safe", "label": "Key safe"},
                                                            {"key": "breed", "label": "Breed"}]}])
    me = _m(does=["desk"])
    out = await concierge_data.execute(TENANT, None, "get_contact_record",
                                       {"contact": "jo@x.com"}, full_qa=True, member=me)
    assert [f["label"] for f in out["fields"]] == ["Breed"]
    assert out["dependants"][0]["facts"] == {"breed": "Collie"}
    # Somebody else's customer is refused outright.
    other = await concierge_data.execute(TENANT, None, "get_contact_record",
                                         {"contact": "pat@x.com"}, full_qa=True, member=me)
    assert other == concierge_data.NOT_THEIRS


@pytest.mark.asyncio
async def test_a_lens_that_cannot_be_read_refuses_rather_than_widens(monkeypatch):
    """Best-effort in ONE direction only. Everywhere else in this codebase a
    failed read degrades to showing more; here that would be the leak."""
    from app import concierge_data

    def boom(*a, **k):
        raise RuntimeError("supabase down")
    monkeypatch.setattr(concierge_data, "_EXECUTORS", {
        **concierge_data._EXECUTORS,
        "search_contacts": lambda t, c, a: {"contacts": [{"email": "jo@x.com"}], "count": 1}})
    monkeypatch.setattr(concierge_data.db, "list_booking_contacts", boom)
    out = await concierge_data.execute(TENANT, None, "search_contacts", {},
                                       full_qa=True, member=_m(does=["desk"]))
    assert out == concierge_data.NOT_THEIRS
