"""The Diary's Cancel button is red-edged, at the right edge, and says what it does.

Kris, 2026-09-20: "In the Diary on a booking we have a Cancel button — turn
the outline red, move the button to the right side and change the text to
Cancel Booking. When clicked it says 'Cancel this booking? It frees up a slot
in the diary' — it doesn't say anything about it writing a draft for you to
send/edit."

The button reads "Cancel booking" in a rose outline at the right edge of the
row's footer; since 2026-09-21 the status chip sits at the footer's LEFT edge
(tests/test_diary_row_layout.py), no longer huddled beside the button. The
confirm prompt names the draft — cancelling a
confirmed booking with a contact composes a note to the customer into the
approval queue (src/app/hand_cancel.py) — and stays as it was for a booking
nobody is waiting to hear about. The transport board's macro says the same.

Run: python -m pytest tests/test_diary_cancel_button.py -q
"""
from tests.test_diary_day_chart import _booking
from tests.test_diary_details_fold import _env, _row, client  # noqa: F401  (the fixture)

GROUP = '<div class="ml-auto flex items-center gap-2 flex-none" data-booking-actions>'
WITH_DRAFT = ("confirm('Cancel this booking? It frees the slot in the diary and drafts a "
              "message to the customer for you to check, edit and send.')")
PLAIN = "confirm('Cancel this booking? It frees the slot in the diary.')"


def _actions(row):
    assert GROUP in row, "the button needs its right-pushed group"
    return row[row.index(GROUP):]


def test_the_button_is_red_edged_at_the_right_edge_and_says_cancel_booking(client, monkeypatch):
    _env(monkeypatch, [_booking("b-9am", "09:00", status="confirmed")])
    row = _row(client.get("/app/diary").text, "b-9am")
    actions = _actions(row)
    button = actions[actions.index("<button"):actions.index("</button>")]
    assert "border-rose-300" in button and "text-rose-700" in button and "hover:bg-rose-50" in button
    assert button.endswith(">Cancel booking")
    assert ">Cancel</button>" not in row and "border-slate-200" not in actions
    assert "Confirmed</span>" not in actions                                  # the chip sits left, before the group
    assert row.index("Confirmed</span>") < row.index(GROUP)


def test_the_prompt_names_the_draft_when_a_customer_is_waiting_to_hear(client, monkeypatch):
    """_booking() carries a contact — the cancel composes them a note."""
    _env(monkeypatch, [_booking("b-9am", "09:00", status="confirmed")])
    row = _row(client.get("/app/diary").text, "b-9am")
    assert WITH_DRAFT in row and "frees up" not in row


def test_the_prompt_stays_plain_for_a_booking_with_nobody_to_tell(client, monkeypatch):
    _env(monkeypatch, [dict(_booking("b-9am", "09:00", status="confirmed"), contact=None)])
    row = _row(client.get("/app/diary").text, "b-9am")
    assert PLAIN in row and "drafts a message" not in row
    assert ">Cancel booking</button>" in row                                  # the button itself is the same


def test_a_hold_and_a_proposal_keep_their_own_buttons_in_the_same_group(client, monkeypatch):
    _env(monkeypatch, [_booking("b-9am", "09:00", status="offered"),
                       _booking("b-11am", "11:00", status="proposed")])
    html = client.get("/app/diary").text
    hold = _actions(_row(html, "b-9am"))
    assert ">Release</button>" in hold and "Cancel booking" not in hold
    assert "confirm('Release this offered slot?" in hold
    proposal = _actions(_row(html, "b-11am"))
    assert ">Confirm</button>" in proposal and ">Decline</button>" in proposal
    assert "Cancel booking" not in proposal and "border-rose-300" not in proposal


def test_the_transport_board_says_the_same():
    src = open("src/app/templates/app/diary_fleet.html", encoding="utf-8").read()
    macro = src[src.index("{% macro status_actions(b) %}"):src.index("{% endmacro %}")]
    assert '<div class="ml-auto flex items-center gap-1.5 flex-none">' in macro
    assert "border-rose-300" in macro and "text-rose-700" in macro
    assert ">Cancel booking</button>" in macro and ">Cancel</button>" not in macro
    assert ("It frees the space on this vehicle{% if b.contact %} and drafts a message to the "
            "customer for you to check, edit and send{% endif %}.") in macro
