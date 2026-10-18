"""A day panel's Add and Close buttons sit at the right edge, and Add is Add.

Kris, 2026-09-20: "On the Diary when you click a day, it shows Add here &
Close buttons — move them to the right of their container. Change Add here
to Add."

The chips (Closed, offered, waiting) share the date's group; the two buttons
are a group of their own, pushed to the right edge, so a wrapped line of
chips never leaves them stranded mid-row. Since 2026-09-21 the header is one
row — the date left, the buttons in the top-right corner — and the "2 of 3"
capacity chip is gone (tests/test_diary_row_layout.py).

Run: python -m pytest tests/test_diary_day_buttons.py -q
"""
import re

from tests.test_diary_day_chart import DAY, _booking
from tests.test_diary_details_fold import _env, client  # noqa: F401  (the fixture)


def _panel(html, day):
    start = html.index(f'id="panel-{day}"')
    return html[start:html.index("</section>", start)]


def test_the_buttons_are_their_own_group_at_the_right_edge(client, monkeypatch):
    _env(monkeypatch, [_booking("b-9am", "09:00", status="confirmed")])
    panel = _panel(client.get("/app/diary").text, DAY.isoformat())
    actions = re.search(r'<div class="ml-auto flex items-center gap-1\.5 flex-none" data-day-actions>(.*?)</div>', panel, re.S)
    assert actions, "the buttons need their own right-pushed group"
    assert 'data-addday="2026-09-10"' in actions.group(1) and 'data-close="2026-09-10"' in actions.group(1)
    assert ">+ Add</button>" in actions.group(1) and "Add here" not in panel
    assert ">✕ Close</button>" in actions.group(1)
    # …after the date and its chips, never among them.
    assert panel.index('class="flex items-center gap-1.5 flex-wrap min-w-0" data-day-chips>') < panel.index("data-day-actions")


def test_the_add_button_still_honours_the_door(client, monkeypatch):
    """A person without diary:add sees Close alone in the group."""
    src = open("src/app/templates/app/diary.html", encoding="utf-8").read()
    group = src[src.index("data-day-actions"):src.index("✕ Close</button>")]
    assert "my_can.get('diary:add', True)" in group and ">+ Add</button>" in group
