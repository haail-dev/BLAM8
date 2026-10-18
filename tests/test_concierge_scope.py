"""People slice 4 — the assistant answers within the doors a person has.

A hole opened by slices 1–3 and closed here: the pages hid Money from a staff
member, but the Concierge gated only on the plan tier, so the same person could
ask it who owed the business money and be told. An assistant that answers what
the pages refuse is a hole in the same wall.

Run: python -m pytest tests/test_concierge_scope.py -q
"""
from pathlib import Path

import pytest

from app import concierge_actions, concierge_data, people

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _m(role="staff", pages=("today", "diary"), can=()):
    return people.member({"user_id": "u1", "role": role, "active": True,
                          "display_name": "Sam",
                          "scopes": {"pages": list(pages), "can": list(can)}})


# ── reads ──────────────────────────────────────────────────────────────────
def test_the_catalogue_follows_the_persons_doors():
    names = lambda m: {t["name"] for t in concierge_data.tools_for(True, m)}
    desk = names(_m())
    assert "list_bookings" in desk and "get_availability" in desk
    assert "list_invoices" not in desk and "list_quotes" not in desk
    assert "get_inbox_overview" not in desk and "search_contacts" not in desk
    # The business's own prices and policies are for everyone who works here.
    assert "search_knowledge_base" in desk
    # An owner, a manager, and an account that predates People keep everything.
    everything = {t["name"] for t in concierge_data.tools_for(True)}
    assert names(_m(role="manager")) == everything
    assert names(None) == everything
    # The tier gate still applies underneath, and narrows further.
    assert {t["name"] for t in concierge_data.tools_for(False, _m(role="manager"))} < everything


def test_an_unmapped_tool_is_closed_to_staff_not_open():
    """A tool added later must not be readable by everyone until somebody
    remembers to classify it."""
    assert concierge_data.tool_door("something_invented_later") == "money"
    assert set(concierge_data.TOOL_DOORS) == set(concierge_data.TOOL_NAMES)


@pytest.mark.asyncio
async def test_the_executor_refuses_as_well_as_the_catalogue():
    """Hiding a tool from the model is not refusing it — a long conversation
    can carry the name forward."""
    out = await concierge_data.execute("t1", None, "list_invoices", {},
                                       full_qa=True, member=_m())
    assert out == concierge_data.CLOSED_DOOR
    assert "isn't open to you" in out["message"]
    src = _read("src/app/concierge_data.py")
    assert "door = tool_door(name)" in src and "return CLOSED_DOOR" in src


# ── writes ─────────────────────────────────────────────────────────────────
def test_the_assistant_is_not_a_way_round_the_ticks():
    desk = _m(pages=("today", "diary"), can=("diary:add",))
    assert concierge_actions.may_do(desk, "move_booking") is True
    assert concierge_actions.may_do(desk, "block_date") is True
    assert concierge_actions.may_do(desk, "cancel_booking") is False   # its own tick
    assert concierge_actions.may_do(desk, "invoice_send") is False
    assert concierge_actions.may_do(desk, "update_contact") is False   # door shut
    assert concierge_actions.may_do(desk, "add_kb_entry") is False
    # Owner, manager and a pre-People account may do anything.
    for m in (_m(role="owner"), _m(role="manager"), None):
        assert all(concierge_actions.may_do(m, k) for k in concierge_actions.ACTION_ABILITIES)
    # An accountant may change nothing at all.
    assert not any(concierge_actions.may_do(_m(role="accountant"), k)
                   for k in concierge_actions.ACTION_ABILITIES)


def test_every_action_is_classified_and_the_unknown_one_is_narrow():
    from app import concierge_actions as ca
    assert set(ca.ACTION_ABILITIES) >= set(ca._STAGERS) - {"bulk"}
    assert ca.ability_for("invented_later") == ("settings", "change")
    # Every ability named is one People actually has.
    for page, ability in ca.ACTION_ABILITIES.values():
        assert ability in [a for a, _l, _h in people.abilities_for(page)], (page, ability)


def test_staging_refuses_in_words_a_person_can_act_on(monkeypatch):
    with pytest.raises(concierge_actions.ActionError) as e:
        concierge_actions.stage("t1", None, {"kind": "cancel_booking"},
                                full_qa=True, scheduling_on=True,
                                member=_m(can=("diary:add",)))
    assert "permission" in str(e.value) and "whoever runs it" in str(e.value)


def test_bulk_cannot_smuggle_a_refused_action():
    src = _read("src/app/concierge_actions.py")
    assert "if not may_do(member, k):" in src
    assert "member=member, spoken_ms=spoken_ms," in src   # the bulk stager gets the asker


def test_the_route_asks_who_is_asking():
    src = _read("src/app/concierge_routes.py")
    assert "asker = _asker(ctx)" in src and "def _asker(ctx: TenantCtx)" in src
    assert src.count("full_qa=full_qa, member=asker, user_id=ctx.user_id)") == 2   # typed and voice: the asker AND the signed-in user
    assert "tools_for(full_qa, asker)" in src
    assert "user_id=ctx.user_id, member=member, spoken_ms=spoken_ms)" in src
    # Unknown asker must NOT narrow — an owner losing their own tools is worse.
    assert "we do\n    NOT narrow" in src or "do NOT narrow" in src
