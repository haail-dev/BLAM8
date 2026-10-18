"""A named item is answered from its own line (Kris 2026-10-16, L8 on the live voice).

The rig held an Expired "Call Sam reminder Monday" (listed first), a Went-through follow-up for Sam and two cancelled tasks;
the spoken answer said "the Sam reminder went through" and never mentioned the expired one. list_awaiting_approval now carries
one plain sentence per item, in the same order as the items, and its guidance says to answer about the item whose title
matches the owner's words first, from that item's own line and state.
Run: python -m pytest tests/test_concierge_item_lines.py -q
"""
from tests.test_concierge_ledger import ago, card, receipt, task, world  # noqa: F401  (the fixtures and builders)


def _sam(world):
    world.tasks = [task("t-late", revision=1, status="waiting", proposal_id="c-late", title="Call Sam reminder Monday",
                        created=ago(minutes=30)),
                   task("t-cancel", status="cancelled", title="Follow-up: call Sam on Monday", outcome="")]
    world.cards = [card("c-late", bound=True, created=ago(minutes=30), summary="Call Sam reminder Monday"),
                   card("c-ran", status="executed", result=receipt("c-ran", via=None), summary="Follow-up for Sam due 16 Oct")]
    return world.run({"status": "all"})


def test_every_item_has_one_line_in_the_same_order(world):
    out = _sam(world)
    assert len(out["item_lines"]) == len(out["items"])
    for item, line in zip(out["items"], out["item_lines"]):
        assert item["title"] in line and item["state"] in line


def test_the_expired_item_reads_as_expired_in_its_own_line_and_the_other_as_went_through(world):
    out = _sam(world)
    lines = {item["title"].strip("«»"): line for item, line in zip(out["items"], out["item_lines"])}
    expired = lines["Call Sam reminder Monday"]
    assert "Expired" in expired and "prepare it again" in expired and "Went through" not in expired
    ran = lines["Follow-up for Sam due 16 Oct"]
    assert "Went through" in ran and "Expired" not in ran
    assert "route not recorded (not a sign-off)" in ran           # an approval with no tap on record is not a sign-off


def test_a_tap_line_says_signed_off_and_a_spoken_line_says_it_is_not(world):
    world.cards = [card("tap", status="executed", result=receipt("tap", via="tap"), summary="Note on Pat's record"),
                   card("words", status="executed", result=receipt("words", via="words"), summary="Follow-up for Sam")]
    out = world.run({"status": "recent"})
    lines = dict(zip([i["title"].strip("«»") for i in out["items"]], out["item_lines"]))
    assert "Approved by you, by tap (signed off)." in lines["Note on Pat's record"]
    assert "Approved by you, by voice (not a sign-off)." in lines["Follow-up for Sam"]


def test_the_guidance_tells_the_agent_to_answer_the_named_item_first_and_never_swap_items(world):
    text = _sam(world)["how_to_read"]
    assert "`item_lines`" in text and "answer about the item whose title matches their words FIRST" in text
    assert "Never describe one item as another" in text
    assert "Never leave out an item that matches their words and is Expired, Needs you or Check this one." in text
    # the sign-off rule from the earlier fix still leads
    assert text.startswith("If they ask what they have SIGNED OFF")
