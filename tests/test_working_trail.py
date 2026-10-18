"""The agent's working, in English — the live ghost and the ⓘ read the same lines.

Kris, 2026-09-21, from a recording of a live draft: the ghost "doesn't seem to
actually show the actions of the Agent", and the ⓘ "doesn't show the plain
English prose reasoning". The pinned defect is the first test below: a hold the
diary REFUSED went up on screen as "Holding a diary slot…", because the label
came from a static dict keyed on the tool's name and the result was discarded.

Run: python -m pytest tests/test_working_trail.py -q
"""
from app import working

STAY = {"arrival": "2026-10-12T08:30", "departure": "2026-10-26T17:00"}
VACC_REFUSAL = {"error": "missing_info",
                "missing": ["Bella: Vaccinations (expired 2026-10-03, before the booking)"],
                "message": "Still needed for Bella: Vaccinations …"}


# ── the defect ───────────────────────────────────────────────────────────────
def test_a_refused_hold_says_it_was_refused_and_why():
    """The whole complaint in one line. This is the most informative thing a
    draft does all day and the old trail hid it behind the word "Holding"."""
    line = working.outcome("hold_booking", STAY, VACC_REFUSAL)
    assert "more customer details needed" in line
    assert "Vaccinations (expired 2026-10-03, before the booking)" in line
    assert "Held" not in line          # never the success verb on a refusal


def test_the_intent_line_still_goes_up_first():
    """The work should look live, so the doing-it line leads and the outcome
    replaces it — the ghost is not just a log printed at the end."""
    assert working.intent("hold_booking", STAY).endswith("…")
    assert "12th Oct 26 to 26th Oct 26" in working.intent("hold_booking", STAY)


def test_a_held_booking_reads_as_held_with_its_reference():
    line = working.outcome("hold_booking", STAY, {"held": True, "booking_ref": "BK-1036"})
    assert line == "Held 12th Oct 26 to 26th Oct 26 — BK-1036."
    assert "Tried" not in line         # the success case is the one read most


def test_an_already_booked_refusal_names_the_booking_that_blocked_it():
    line = working.outcome("hold_booking", STAY,
                           {"error": "already_booked", "booking_ref": "BK-1036"})
    assert "Tried to hold" in line and "already booked" in line and "BK-1036" in line


# ── dates, the way the operator reads them ───────────────────────────────────
def test_dates_are_the_operator_convention_never_a_raw_iso_string():
    line = working.outcome("check_availability", STAY, {"available": True, "nights": 14})
    assert "12th Oct 26 to 26th Oct 26" in line
    assert "2026-10-12" not in line
    assert "14 nights" in line


def test_a_single_dated_call_carries_its_window():
    assert "(morning)" in working.outcome("check_availability",
                                     {"date": "2026-10-12", "window": "am"},
                                     {"available": False})


def test_a_day_window_is_not_worth_saying():
    line = working.outcome("check_availability", {"date": "2026-10-12", "window": "day"},
                           {"available": True})
    assert "(day)" not in line


def test_no_space_is_said_plainly():
    assert working.outcome("check_availability", STAY, {"available": False}).endswith("no available times returned.")


# ── total: this renders on a live drafting path ──────────────────────────────
def test_an_unknown_tool_still_reads_as_english():
    """A tool added later must degrade to something, not vanish — silence on
    the ghost is what made the trail look broken in the first place."""
    assert working.intent("walk_the_dog", {}) == "Checking this request…"
    assert "could not be confirmed" in working.outcome("walk_the_dog", {}, {"ok": True})


def test_odd_shapes_never_raise():
    for args in (None, "", [], {"arrival": "not-a-date"}):
        for result in (None, "", [], "boom", {}, {"error": "tool_failed"}):
            assert isinstance(working.outcome("hold_booking", args, result), str)
            assert isinstance(working.intent("hold_booking", args), str)


def test_an_unparseable_date_passes_through_rather_than_being_dropped():
    assert "not-a-date" in working.intent("check_availability", {"date": "not-a-date"})


# ── the ⓘ panel ──────────────────────────────────────────────────────────────
def test_the_panel_is_every_call_in_order_one_line_each():
    panel = working.panel([
        ("check_availability", STAY, {"available": True, "nights": 14}),
        ("hold_booking", STAY, VACC_REFUSAL),
        ("check_availability", STAY, {"available": True, "nights": 14}),
    ])
    lines = panel.split("\n")
    assert len(lines) == 3
    assert lines[0].startswith("Checked the diary")
    assert "more customer details needed" in lines[1]
    assert "{" not in panel and "→" not in panel     # prose, not the machine view


def test_a_draft_that_called_nothing_has_an_empty_panel():
    """A reply needing no tools has no working; a heading over nothing is worse
    than no panel at all."""
    assert working.panel([]) == ""
    assert working.panel(None) == ""


def test_a_malformed_call_is_skipped_not_fatal():
    assert working.panel([("hold_booking", STAY, {"held": True}), ("oops",)]) != ""


# ── what the ⓘ actually stores ───────────────────────────────────────────────
class _Result:
    def __init__(self, reasoning=""):
        self.reasoning = reasoning


RAW = '→ hold_booking({"arrival": "2026-10-12T08:30"})\n\n← {"held": true}'


def test_the_stored_panel_leads_with_prose_and_keeps_the_machine_view():
    """Kris asked for the plain English; the raw calls stay underneath because
    the machine view is the evidence the prose is rendered from."""
    from app.pipeline import _working_panel
    out = _working_panel({"calls": [("hold_booking", STAY, VACC_REFUSAL)]}, _Result(RAW))
    saved = working.unpack(out)
    assert saved["owner"].startswith("Looked at\n")
    assert "more customer details needed" in saved["owner"]
    assert "hold_booking" not in saved["owner"]
    assert saved["technical"]["trail"] == RAW


def test_a_draft_with_no_calls_keeps_raw_evidence_out_of_the_owner_panel():
    from app.pipeline import _working_panel
    for state in ({"calls": []}, None):
        out = _working_panel(state, _Result(RAW))
        assert working.unpack(out)["technical"]["trail"] == RAW
        assert RAW not in working.owner_panel(out)


def test_nothing_captured_stores_nothing():
    """None, not an empty string — the template hides the panel on falsy."""
    from app.pipeline import _working_panel
    assert _working_panel({"calls": []}, _Result("")) is None


def test_an_empty_answer_says_why_not_no_space():
    """Kris 2026-09-25, plumber film: Sarah's same-day emergency at 17:58 read
    "Checked the diary — no space" — it was the notice period."""
    from datetime import date, datetime
    from app import scheduling
    cfg = {"working_days": "mon,tue,wed,thu,fri,sat", "open_time": "08:00:00",
           "close_time": "18:00:00", "min_notice_hours": 4}
    fri = date(2026, 9, 25)
    at = datetime(2026, 9, 25, 17, 58, tzinfo=scheduling.DIARY_TZ)
    assert scheduling.empty_reasons(cfg, fri, fri, at) == {"2026-09-25": "notice"}
    late = datetime(2026, 9, 25, 18, 30, tzinfo=scheduling.DIARY_TZ)
    assert scheduling.empty_reasons(cfg, fri, fri, late) == {"2026-09-25": "over"}
    morning = datetime(2026, 9, 25, 9, 0, tzinfo=scheduling.DIARY_TZ)
    slots = {**cfg, "windows": "slots"}
    assert scheduling.empty_reasons(slots, fri, fri, morning) == {"2026-09-25": "full"}
    # Morning/afternoon windows: the last start is noon, not closing time
    # (Kris 2026-09-26: a gas smell at 12:08 on a Saturday read "fully booked").
    ampm = {**cfg, "windows": "ampm"}
    sat = date(2026, 9, 26)
    at_1208 = datetime(2026, 9, 26, 12, 8, tzinfo=scheduling.DIARY_TZ)
    assert scheduling.empty_reasons(ampm, sat, sat, at_1208) == {"2026-09-26": "notice"}
    assert scheduling.empty_reasons(slots, sat, sat, datetime(2026, 9, 26, 8, 0, tzinfo=scheduling.DIARY_TZ))         == {"2026-09-26": "full"}
    sun = date(2026, 9, 27)
    assert scheduling.empty_reasons(cfg, sun, sun, at) == {"2026-09-27": "closed"}

    got = scheduling.why_none(cfg, fri, fri, at)
    assert got["why_none"] == "notice" and "Never say the diary is full" in got["why_none_note"]
    assert "never quote the hours of notice" in got["why_none_note"]           # run log F13
    assert "why_none_note" not in scheduling.why_none(slots, fri, fri, morning)  # full is full

    line = working.outcome("check_availability", {"date_from": "2026-09-25", "date_to": "2026-09-25"},
                           {"available": [], **got})
    assert "too soon — inside the notice period" in line and "no space" not in line
    # a genuinely full day still says so
    assert working.outcome("check_availability", {}, {"available": [], "why_none": "full"}).endswith("no space.")


def test_a_hold_deadline_is_said_like_a_person_would():
    """Run log F14: "held until Friday 25 September at 20:00", written on the Friday."""
    from datetime import datetime
    from app import dates
    from app.scheduling import DIARY_TZ
    fri = datetime(2026, 9, 25, 17, 58, tzinfo=DIARY_TZ)
    at = lambda *a: datetime(*a, tzinfo=DIARY_TZ)  # noqa: E731
    assert dates.deadline_phrase(at(2026, 9, 25, 20, 0), fri) == "8pm tonight"
    assert dates.deadline_phrase(at(2026, 9, 25, 15, 30), at(2026, 9, 25, 9, 0)) == "3:30pm today"
    assert dates.deadline_phrase(at(2026, 9, 26, 8, 0), fri) == "tomorrow at 8am"
    assert dates.deadline_phrase(at(2026, 9, 27, 12, 8), fri) == "Sunday at 12:08pm"
    assert dates.deadline_phrase(at(2026, 10, 5, 12, 0), fri) == "Monday 5th October at noon"


def test_a_held_morning_window_reads_as_the_window_on_the_thread():
    """Run log F19: the thread said '08:00–10:00' for a morning offered as 08:00–12:00."""
    from app import dashboard
    cfg = {"windows": "ampm", "open_time": "08:00:00", "close_time": "18:00:00",
           "working_days": "mon,tue,wed,thu,fri,sat"}
    b = {"booking_date": "2026-09-28", "window": "am", "start_block": 32, "length_blocks": 8,
         "details": {"Service": "Emergency callout"}, "status": "offered"}
    assert dashboard._held_what(b, "booking", cfg) == ("Emergency callout", "Morning (08:00–12:00)")
    assert dashboard._held_what({**b, "window": "pm", "start_block": 48}, "booking", cfg) \
        == ("Emergency callout", "Afternoon (12:00–18:00)")
    assert dashboard._held_what(b)[1] == "Morning"            # no diary to hand: the word alone


def test_a_day_beyond_the_horizon_is_too_far_ahead_not_full():
    """Live 2026-09-26 (Clare, Christmas): with a 90-day horizon Boxing Day was
    past it, and the reply said "Boxing Day is already fully booked"."""
    from datetime import date, datetime
    from app import scheduling
    cfg = {"working_days": "mon,tue,wed,thu,fri,sat", "open_time": "08:00:00",
           "close_time": "18:00:00", "min_notice_hours": 4, "horizon_days": 90}
    now = datetime(2026, 9, 26, 19, 0, tzinfo=scheduling.DIARY_TZ)
    got = scheduling.why_none(cfg, date(2026, 12, 26), date(2026, 12, 26), now)
    assert got["why_none"] == "ahead" and "NOT full" in got["why_none_ahead"]
    line = working.outcome("check_availability", {}, {"available": [], **got})
    assert "too far ahead to book yet" in line and "no space" not in line
