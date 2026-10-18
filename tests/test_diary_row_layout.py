"""The Diary's day panel and booking row, tidied.

Kris, 2026-09-21: "I want to make the Diary booking chip a little cleaner.
Remove the 1 of 30 capacity chips. Put the buttons in top right. Keep the
cancel button bottom right, move the confirmed chip to the left side. Put the
link text on its own line instead of wrapped."

The day panel's header is the date (with any Closed / offered / waiting chip
beside it) on the left and the + Add / ✕ Close pair in the top-right corner —
one row that never wraps the buttons underneath. The "1 of 30" / "AM · 2 of 5"
capacity chips are gone: the month cell already says how full the day is. A
hairline under that row splits the header from the bookings below, the same
divider the bookings already carry between them.

A booking row reads top to bottom: what, when, who booked it, then the links
(view conversation · job page · details) on a line of their own — they used
to wrap through the customer's address — and a footer with the status chip
at the left edge and its button(s) at the right.

Run: python -m pytest tests/test_diary_row_layout.py -q
"""
import re

import pytest

import app.dashboard as dashboard
from app import scheduling
from tests.test_diary_day_chart import DAY, SLOTS, _booking
from tests.test_diary_details_fold import _env, _row, client  # noqa: F401  (the fixture)

STATUS = '<div class="flex items-center gap-2 flex-wrap min-w-0" data-booking-status>'
ACTIONS = '<div class="ml-auto flex items-center gap-2 flex-none" data-booking-actions>'


def _panel(html, day=DAY.isoformat()):
    start = html.index(f'id="panel-{day}"')
    return html[start:html.index("</section>", start)]


def _header(panel):
    """The panel's first row: everything up to the close of the buttons group."""
    return panel[:panel.index("✕ Close</button>")]


def _windows(monkeypatch, mode, capacity):
    for mod in (dashboard.db, scheduling.db):
        monkeypatch.setattr(mod, "get_scheduling_config",
                            lambda *a, **k: dict(SLOTS, windows=mode, capacity_per_window=capacity))


# ── the day panel's header ────────────────────────────────────────────────────
@pytest.mark.parametrize("mode, window, capacity, chip", [
    ("day", "day", 30, "1 of 30"),
    ("ampm", "am", 5, "AM · 1 of 5"),
])
def test_the_capacity_chip_is_gone_from_the_day_header(client, monkeypatch, mode, window, capacity, chip):
    _env(monkeypatch, [_booking("b-1", window, status="confirmed")])
    _windows(monkeypatch, mode, capacity)
    header = _header(_panel(client.get("/app/diary").text))
    assert "Thursday 10 September" in header
    assert chip not in header and f"of {capacity}" not in header
    assert "bg-emerald-50 text-emerald-700" not in header and "bg-rose-50 text-rose-700" not in header


def test_the_buttons_sit_in_the_top_right_beside_the_date(client, monkeypatch):
    _env(monkeypatch, [_booking("b-9am", "09:00", status="confirmed")])
    header = _header(_panel(client.get("/app/diary").text))
    row = re.search(r'<div class="flex items-start justify-between gap-2 border-b border-slate-100 pb-3">\s*'
                    r'<div class="flex items-center gap-1\.5 flex-wrap min-w-0" data-day-chips>\s*'
                    r'<span class="font-semibold text-navy">Thursday 10 September</span>', header)
    assert row, "the date opens the left group of the one header row, underlined"
    assert header.index("data-day-chips") < header.index("data-day-actions")
    assert '<div class="ml-auto flex items-center gap-1.5 flex-none" data-day-actions>' in header
    assert ">+ Add</button>" in header[header.index("data-day-actions"):]


def test_the_chips_that_stay_sit_beside_the_date(client, monkeypatch):
    """An offered slot still shows on the day — in the date's group, not the buttons'."""
    _env(monkeypatch, [_booking("b-9am", "09:00", status="offered")])
    header = _header(_panel(client.get("/app/diary").text))
    chips = header[header.index("data-day-chips"):header.index("data-day-actions")]
    assert "Thursday 10 September" in chips and ">1 offered</span>" in chips


# ── the booking row ───────────────────────────────────────────────────────────
def _paragraphs(row):
    return re.findall(r"<p [^>]*>.*?</p>", row, re.S)


def test_the_links_have_a_line_of_their_own(client, monkeypatch):
    _env(monkeypatch, [dict(_booking("b-9am", "09:00", status="confirmed"),
                            details={"Dogs": "Biscuit"}, source="email", email_log_id="e1",
                            contact="business.blam8+daniel.okafor@gmail.com")])
    row = _row(client.get("/app/diary").text, "b-9am")
    source = next(p for p in _paragraphs(row) if "booked by the agent" in p)
    links = next(p for p in _paragraphs(row) if "data-booking-links" in p)
    # Who booked it and the address on one line; no link shares it.
    assert "business.blam8+daniel.okafor@gmail.com" in source and "break-words" in source
    assert "<a " not in source and "<button" not in source
    # The links on the next line, in the order they always had.
    assert row.index(source) < row.index(links)
    assert links.index("view conversation</a>") < links.index("job page</a>") < links.index("details</button>")
    assert "booked by the agent" not in links and "@" not in links.split("job page")[1]
    # …and the folded details still follow the link that opens them.
    assert row.index(links) < row.index('id="details-b-9am" data-booking-details hidden>')


def test_a_hand_added_booking_keeps_the_job_page_link_on_its_own_line(client, monkeypatch):
    _env(monkeypatch, [dict(_booking("b-9am", "09:00", status="confirmed"), source="manual", contact=None)])
    row = _row(client.get("/app/diary").text, "b-9am")
    source = next(p for p in _paragraphs(row) if "added manually" in p)
    links = next(p for p in _paragraphs(row) if "data-booking-links" in p)
    assert "·" not in source and "<a " not in source
    assert "job page</a>" in links and "view conversation" not in links and "details" not in links


def test_the_chip_is_at_the_left_and_the_button_at_the_right(client, monkeypatch):
    _env(monkeypatch, [_booking("b-9am", "09:00", status="confirmed")])
    row = _row(client.get("/app/diary").text, "b-9am")
    footer = row[row.index('<div class="mt-2 flex items-center justify-between gap-2 flex-wrap">'):]
    assert footer.index(STATUS) < footer.index(ACTIONS)
    status = footer[footer.index(STATUS):footer.index(ACTIONS)]
    actions = footer[footer.index(ACTIONS):]
    assert "Confirmed</span>" in status and "<button" not in status
    assert ">Cancel booking</button>" in actions and "Confirmed</span>" not in actions
    # The words sit above the footer, never beside it.
    assert row.index("Catch-up call 09:00") < row.index(STATUS)


def test_every_status_keeps_its_chip_left_and_its_buttons_right(client, monkeypatch):
    _env(monkeypatch, [_booking("b-9am", "09:00", status="offered"),
                       _booking("b-11am", "11:00", status="proposed"),
                       dict(_booking("b-2pm", "14:00", status="confirmed"),
                            details={"calendar_event_missing_at": "2026-09-05T10:00:00+00:00"})])
    html = client.get("/app/diary").text
    for bid, chips, buttons in [
        ("b-9am", ["Offered"], [">Release</button>"]),
        ("b-11am", ["Proposed</span>"], [">Confirm</button>", ">Decline</button>"]),
        ("b-2pm", ["Calendar event missing</span>", "Confirmed</span>"], [">Cancel booking</button>"]),
    ]:
        row = _row(html, bid)
        status = row[row.index(STATUS):row.index(ACTIONS)]
        actions = row[row.index(ACTIONS):]
        for c in chips:
            assert c in status and c not in actions, (bid, c)
        for b in buttons:
            assert b in actions and b not in status, (bid, b)
        assert "<form" not in status
