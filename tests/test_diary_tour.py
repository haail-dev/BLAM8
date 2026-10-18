"""Page 3 of the walk-through — the Diary (Kris 2026-09-13: "instead of
clicking Done, we click Diary. Then run a tour in the Diary") — and the page
itself cleaned up ("all of this doesn't need to be displayed") with cells
that read smart on a stays diary that also books appointments.

Run: python -m pytest tests/test_diary_tour.py -q
"""
from datetime import datetime
from pathlib import Path

from app import scheduling
from app.intake import STAY_FIELD

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _cfg(**over):
    cfg = {"working_days": "mon,tue,wed,thu,fri,sat", "windows": "day", "slot_minutes": 60,
           "capacity_per_window": 30, "horizon_days": 7, "blackout_dates": "",
           "open_time": "07:00", "close_time": "18:45", "min_notice_hours": 10,
           "intake_fields": [dict(STAY_FIELD), {"label": "Dog name(s)", "required": True}],
           "service_types": [{"name": "Meet & greet", "minutes": 30, "resource": "Meet & greet"}],
           "resource_types": [{"name": "Meet & greet", "count": 2}],
           "nouns": {"booking": "booking", "participant": "dog"}}
    cfg.update(over)
    return cfg


def _rows(day_iso):
    stay = {"id": "s1", "booking_date": day_iso, "start_block": 28, "length_blocks": 96 * 2,
            "status": "confirmed", "summary": "Bella", "window": "day", "details": {}}
    appt = {"id": "a1", "booking_date": day_iso, "start_block": 40, "length_blocks": 2,
            "status": "confirmed", "summary": "Meet & greet — Sam", "window": "10:00",
            "resource_type": "Meet & greet", "details": {"Service": "Meet & greet"}}
    appt2 = {**appt, "id": "a2", "start_block": 44, "summary": "Meet & greet — Jo"}
    return stay, appt, appt2


def test_the_inbox_walk_hands_over_to_the_diary():
    js = _read("src/app/static/tour.js")
    assert 'inbox: { sel: \'[data-tour="day-door"]\', href: "/app/diary", label: "Diary →" }' in js
    assert 'next.textContent = n < total ? "Next →" : ((HANDOFF[page] || {}).label || "Done");' in js
    # Skip never hands over — as a bare listener it read the click event as "handoff".
    assert 'skip.addEventListener("click", function () { finish(false); });' in js
    # The drafting switch walks on either way, no reload to the clean address.
    assert ".then(function () { return finish(true); })" in js
    assert 'skip.addEventListener("click", function () { finish(true); });' in js
    assert "window.location.pathname" not in js
    assert "if (door && door.getClientRects().length)" in js and "} else if (h) {" in js
    shell = _read("src/app/templates/app/base.html")
    assert 'data-tour="day-door"' in shell and 'data-tour="section-settings"' in shell
    assert 'diary: { label: "Diary", href: "/app/diary" }' in shell                   # the way-back chip knows the page
    assert 'a[data-tour="section-settings"].tour-glow { border: 1px solid; border-radius: 9999px; }' in shell


def test_the_diary_page_carries_the_walk():
    dash = _read("src/app/dashboard.py")
    assert '_tour_for(ctx, t, "diary", force=request.query_params.get("tour") == "1")' in dash
    assert "tour_page=tour_page, tour_kind_plural=tour_plural," in dash.split("app/diary.html")[1][:600]
    js = _read("src/app/static/tour.js")
    diary = js.split("diary: [")[1].split("],")[0]
    for sel in ('[data-tour="today"]', '[data-tour="colour"]', '[data-tour="section-settings"]'):
        assert sel in diary
    assert "who's in against your spaces" in diary and "Today is ringed." in diary
    assert "one bar per diary" in diary and "reaches " in diary and "inside your notice period." in diary
    assert 'parseInt(wrap.getAttribute("data-tour-types")' in diary
    assert "live behind" in diary and "this cog." in diary
    page = _read("src/app/templates/app/diary.html")
    assert 'data-tour-diary="{{ \'stays\' if is_stay else mode }}"' in page
    assert '{% if d.today %} data-tour="today"{% elif d.date == tour_colour %} data-tour="colour"{% endif %}' in page
    assert "{% set tour_colour = tn.colour or tn.free %}" in page


def test_the_header_went_and_the_reset_moved_to_settings():
    page = _read("src/app/templates/app/diary.html")
    for gone in ("Cancel every live booking", "+ Add booking", "{{ summary }}", 'id="addform-toggle"',
                 "at the same time"):
        assert gone not in page, gone
    assert "data-addform-close" in page and "Add by hand" in page
    assert "Every live booking cancelled" in page                                   # the receipt still lands on the diary
    assert 'data-addday="{{ d.date }}"' in page                                     # + Add here stays the way in
    settings = _read("src/app/templates/app/diary_settings.html")
    assert 'action="/app/diary/cancel-all"' in settings and 'value="/app/diary"' in settings
    assert "Cancel every live booking" in settings and "<summary" in settings


def test_a_stays_day_says_who_is_in_and_the_appointments():
    cfg = _cfg()
    stay, appt, appt2 = _rows("2026-09-14")
    now = datetime(2026, 9, 14, 9, 0, tzinfo=scheduling.DIARY_TZ)
    days = {d["date"]: d for d in scheduling.diary_days(cfg, [stay, appt, appt2], now)}
    today = days["2026-09-14"]
    assert today["stay_used"] == 1 and today["appts"] == [{"name": "Meet & greet", "count": 2}]
    assert days["2026-09-15"]["stay_used"] == 1 and days["2026-09-15"]["appts"] == []     # the stay runs on, the visits don't
    weeks = scheduling.calendar_weeks(cfg, [stay, appt, appt2], now)
    cells = {c["date"]: c for w in weeks for c in w["days"] if c.get("in_diary")}
    assert cells["2026-09-14"]["stay_used"] == 1 and cells["2026-09-14"]["free"] == 29
    assert cells["2026-09-14"]["appts"][0]["count"] == 2
    # A windows diary carries nothing of it.
    plain = scheduling.diary_days({"working_days": "mon,tue", "windows": "am,pm", "capacity_per_window": 2,
                                   "horizon_days": 7}, [], now)
    assert plain and plain[0]["stay_used"] == 0 and plain[0]["appts"] == []


def test_the_stays_cell_copy():
    """The macro rendered on its own: 'N in · M free · k meet & greets'."""
    from jinja2 import Environment
    page = _read("src/app/templates/app/diary.html")
    macro = page.split("{% macro stay_cell(d) -%}")[1].split("{%- endmacro %}")[0]
    tpl = Environment().from_string("{% macro stay_cell(d) -%}" + macro + "{%- endmacro %}{{ stay_cell(d) }}")
    base = {"stay_used": 0, "free": 30, "working": True, "notice_blocked": False, "appts": [], "offered_count": 0,
            "notice_hours": 10, "full": False, "types": []}
    assert tpl.render(d=base) == "free"
    assert tpl.render(d={**base, "stay_used": 3, "free": 27}) == "3 in · 27 free"
    assert tpl.render(d={**base, "stay_used": 3, "free": 27,
                         "appts": [{"name": "Meet & greet", "count": 2}]}) == "3 in · 27 free · 2 meet & greets"
    assert tpl.render(d={**base, "appts": [{"name": "Meet & greet", "count": 1}]}) == "1 meet & greet"
    assert tpl.render(d={**base, "notice_blocked": True}) == "needs 10h notice"
    assert tpl.render(d={**base, "working": False, "appts": [{"name": "Grooming", "count": 1}]}) == "closed · 1 grooming"
    assert tpl.render(d={**base, "stay_used": 2, "free": 28, "offered_count": 1}) == "2 in · 28 free · 1 offered"
    # Only one type full says which (Kris: "what if capacity is full but slots aren't?").
    mg = {"key": "Meet & greet", "full": False}
    assert tpl.render(d={**base, "full": True, "stay_used": 30, "free": 0, "types": [{"key": "spaces", "full": True}, mg],
                         "appts": [{"name": "Meet & greet", "count": 2}]}) == "spaces full · 2 meet & greets"
    assert tpl.render(d={**base, "stay_used": 4, "free": 26, "types": [{"key": "spaces", "full": False}, {**mg, "full": True}],
                         "appts": [{"name": "Meet & greet", "count": 6}]}) == "4 in · 26 free · meet & greets full"


def _full_day(day_iso, resource, n):
    """`n` bookings of `resource` each spanning the whole opening day — the pool is full."""
    return [{"id": f"{resource[:2]}{i}", "booking_date": day_iso, "start_block": 28, "length_blocks": 47,
             "status": "confirmed", "summary": f"{resource} {i}", "window": "07:00",
             "resource_type": resource, "details": {"Service": resource}} for i in range(n)]


def test_a_day_has_one_bar_per_diary_type():
    """Kris 2026-09-13: "split the box in 2/3/4 depending how many diary types
    get used, each with a bar growing upwards until capacity is full"."""
    cfg = _cfg()
    stay, appt, appt2 = _rows("2026-09-14")
    now = datetime(2026, 9, 14, 9, 0, tzinfo=scheduling.DIARY_TZ)
    day = next(d for d in scheduling.diary_days(cfg, [stay, appt, appt2], now) if d["date"] == "2026-09-14")
    spaces, mg = day["types"]
    assert spaces["key"] == "spaces" and spaces["label"] == "dogs" and spaces["used"] == 1 and spaces["capacity"] == 30
    assert spaces["pct"] == 3 and spaces["full"] is False and spaces["bar"] == "bg-emerald-500"
    assert mg["key"] == "Meet & greet" and mg["label"] == "meet & greets" and mg["used"] == 2 and mg["capacity"] == 2
    assert 0 < mg["pct"] < 10 and mg["full"] is False and mg["bar"] == "bg-blue-500"
    assert day["all_full"] is False and day["full"] is False
    # The spaces full, the meet & greet pools open: not "Full".
    dogs = [{**stay, "id": f"s{i}"} for i in range(30)]
    day = next(d for d in scheduling.diary_days(cfg, dogs + [appt], now) if d["date"] == "2026-09-14")
    assert day["full"] is True and day["types"][0]["full"] is True and day["types"][0]["pct"] == 100
    assert day["types"][1]["full"] is False and day["all_full"] is False
    # Both meet & greet pools booked all day: that bar reaches the top; the spaces don't.
    day = next(d for d in scheduling.diary_days(cfg, [stay] + _full_day("2026-09-14", "Meet & greet", 2), now)
               if d["date"] == "2026-09-14")
    assert day["types"][1]["full"] is True and day["types"][1]["pct"] == 100
    assert day["types"][0]["full"] is False and day["all_full"] is False
    # Everything full: Full.
    day = next(d for d in scheduling.diary_days(cfg, dogs + _full_day("2026-09-14", "Meet & greet", 2), now)
               if d["date"] == "2026-09-14")
    assert day["all_full"] is True
    # The offered share of a bar.
    day = next(d for d in scheduling.diary_days(cfg, [{**stay, "status": "offered", "expires_at": "2026-09-15T09:00:00+00:00"}], now)
               if d["date"] == "2026-09-14")
    assert day["types"][0]["pct"] == 3 and day["types"][0]["soft_pct"] == 3
    # A windows diary: one bar, the windows summed.
    plain = scheduling.diary_days({"working_days": "mon,tue", "windows": "ampm", "capacity_per_window": 2,
                                   "horizon_days": 7}, [], now)
    assert [t["key"] for t in plain[0]["types"]] == ["spaces"] and plain[0]["types"][0]["capacity"] == 4


def test_the_day_chart_gives_each_type_its_own_lanes():
    cfg = _cfg(windows="slots")          # the chart is the slots diary's day view
    stay, appt, appt2 = _rows("2026-09-14")
    now = datetime(2026, 9, 14, 9, 0, tzinfo=scheduling.DIARY_TZ)
    day = next(d for d in scheduling.diary_days(cfg, [stay, appt, appt2], now) if d["date"] == "2026-09-14")
    chart = day["chart"]
    assert chart["lanes"] == 32 and chart["used"] == 1                    # 30 spaces + 2 meet & greet lanes
    assert [g["label"] for g in chart["groups"]] == ["spaces", "meet & greets"]
    assert chart["groups"][1]["start"] == 30 and chart["groups"][1]["used"] == 1   # the two visits don't overlap
    by_id = {b["booking_id"]: b for b in chart["bars"]}
    assert by_id["s1"]["lane"] == 0 and by_id["s1"]["cls"].startswith("bg-emerald-500")
    assert by_id["a1"]["lane"] == 30 and by_id["a2"]["lane"] == 30 and by_id["a1"]["cls"].startswith("bg-blue-500")
    rows = {b["id"]: b for b in day["bookings"]}
    assert rows["s1"]["dot"] == "bg-emerald-500" and rows["a1"]["dot"] == "bg-blue-500"
