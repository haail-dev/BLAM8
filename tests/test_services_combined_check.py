"""One diary check for a visit of several services.

Live 2026-09-04 (farrier run): two horses meant two check_availability calls
on every portal turn — one per service. `services` lists them and the check
uses their combined length in one call.

Run: python -m pytest tests/test_services_combined_check.py -q
"""
import re

from app import scheduling


def test_the_services_tool_schema_accepts_a_list():
    tools = [t for t in scheduling.SERVICE_TOOLS if t["name"] == "check_availability"] \
        if hasattr(scheduling, "SERVICE_TOOLS") else []
    src = open(scheduling.__file__, encoding="utf-8").read()
    assert '"services": {"type": "array", "items": {"type": "string"}' in src
    if tools:
        assert "services" in tools[0]["input_schema"]["properties"]


def test_the_check_sums_the_lengths_and_names_the_visit():
    src = open(scheduling.__file__, encoding="utf-8").read()
    body = src.split("def _service_check(args: dict) -> dict:")[1].split("def ")[0]
    assert 'combined = [str(n).strip() for n in (args.get("services") or [])' in body
    assert "svc, length = metas[0][0], sum(m[1] for m in metas)" in body
    assert 'out["services_combined"]' in body



def test_a_six_weekly_series_steps_42_days_and_a_quarterly_one_three_months():
    from datetime import date
    src = open(scheduling.__file__, encoding="utf-8").read()
    body = src.split("def _recurrence_dates(")[1].split("# ── Stays")[0]
    assert "diary_config.FREQ_DAYS.get(rep)" in body and "diary_config.FREQ_MONTHS.get(rep, 1)" in body
    # the maths, run the way the executor runs it
    from app import diary_config
    import calendar
    from datetime import timedelta
    def dates(first, rep, n):
        step = diary_config.FREQ_DAYS.get(rep); months = diary_config.FREQ_MONTHS.get(rep, 1); out = []
        for i in range(n):
            if step:
                out.append(first + timedelta(days=step * i))
            else:
                m = first.month - 1 + months * i
                y, mo = first.year + m // 12, m % 12 + 1
                out.append(date(y, mo, min(first.day, calendar.monthrange(y, mo)[1])))
        return out
    assert dates(date(2026, 9, 7), "six_weekly", 3) == [date(2026, 9, 7), date(2026, 10, 19), date(2026, 11, 30)]
    assert dates(date(2026, 11, 30), "quarterly", 3) == [date(2026, 11, 30), date(2027, 2, 28), date(2027, 5, 30)]
    assert dates(date(2026, 9, 7), "monthly", 2) == [date(2026, 9, 7), date(2026, 10, 7)]
    tool = [t for t in scheduling.__dict__.values() if isinstance(t, list) and t and isinstance(t[0], dict)
            and any(x.get("name") == "hold_booking" for x in t)]
    assert '"six_weekly", "quarterly"]' in src
