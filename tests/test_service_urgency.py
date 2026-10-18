"""Urgent services keep their own clock (docs/plan-service-urgency.md, D1 stage 1).

Hartwell's knowledge said "same day if reported before 2pm" but the diary's
4 hours' notice applied to every service, so a no-heating email at 11:00 on a
Saturday was offered Monday. A service can now carry its own notice and a
same-day cut-off; the check, the hold's re-derivation, the too-soon reason and
the prompt all read them. A service without them is byte-identical.

Run: python -m pytest tests/test_service_urgency.py -q
"""
from datetime import date, datetime

from app import scheduling, services
from app.scheduling import DIARY_TZ

CFG = {"windows": "ampm", "open_time": "08:00", "close_time": "18:00", "capacity_per_window": 2,
       "min_notice_hours": 4, "working_days": "mon,tue,wed,thu,fri,sat"}
CALLOUT = {"name": "Emergency callout", "minutes": 120, "price": 85, "timing": "half",
           "same_day_until": "14:00", "notice_hours": 1}
SERVICE = {"name": "Boiler repair", "minutes": 120, "price": 85, "timing": "half"}
SAT = date(2026, 9, 26)


def at(h, m=0):
    return datetime(2026, 9, 26, h, m, tzinfo=DIARY_TZ)


def windows(svc, now, day=SAT):
    length = scheduling.slotgrid.blocks_for_minutes(svc["minutes"])
    return [(s["date"], s["window"]) for s in scheduling.service_availability(
        CFG, [], length, day, day, now, timing="half", svc=svc)]


def test_the_settings_are_cleaned_and_said_in_words():
    got = services.clean_service_types([{**CALLOUT, "same_day_until": "2pm", "notice_hours": "1"},
                                        {**SERVICE, "notice_hours": "", "same_day_until": ""}])
    assert "same_day_until" not in got[0]                     # '2pm' isn't HH:MM: dropped, not guessed
    got = services.clean_service_types([CALLOUT, SERVICE])
    assert got[0]["same_day_until"] == "14:00" and got[0]["notice_hours"] == 1
    assert "same_day_until" not in got[1] and "notice_hours" not in got[1]   # byte-identical
    assert services.is_urgent(got[0]) and not services.is_urgent(got[1])
    assert services.rules_label(got[0]) == "same day if asked before 14:00, 1 h notice"
    rows = services.parse_form(["Emergency callout"], ["120"], ["85"], timing_list=["half"],
                               same_day_list=["14:00"], notice_list=["1"])
    assert rows[0]["same_day_until"] == "14:00" and rows[0]["notice_hours"] == 1


def test_before_the_cut_off_an_urgent_service_can_go_today():
    # 12:30 Sat: 1 h notice lets a 2-hour callout start in the afternoon window.
    assert (SAT.isoformat(), "pm") in windows(CALLOUT, at(12, 30))
    # The same diary, a normal 2-hour job on the diary's 4 h notice: nothing
    # today (cut-off 16:30, the last start that fits by 18:00 is 16:00).
    assert windows(SERVICE, at(12, 30)) == []


def test_after_the_cut_off_today_is_off_and_said_as_too_late():
    assert windows(CALLOUT, at(14, 30)) == []
    why = scheduling.why_none(CFG, SAT, SAT, at(14, 30), timing="half",
                              length_blocks=8, svc=CALLOUT)
    assert why["why_none"] == "notice" and "never quote the hours of notice" in why["why_none_note"]


def test_the_hold_places_today_on_the_services_own_notice():
    # first_fit_start on the service's notice places it at the first grid
    # start after 13:30 (14:00, block 56); on the diary's 4 h it would refuse.
    length = scheduling.slotgrid.blocks_for_minutes(120)
    assert scheduling.first_fit_start(CFG, [], length, SAT, "pm", at(12, 30), notice_hours=1) == 56
    assert scheduling.first_fit_start(CFG, [], length, SAT, "pm", at(12, 30)) is None


def test_a_service_without_rules_is_unchanged():
    assert scheduling.service_notice(scheduling._norm(CFG), SERVICE) == 4
    assert not scheduling.past_same_day_cutoff(SERVICE, at(17))


def test_the_prompt_says_hold_today_as_a_window_and_safety_first():
    text = services.render_for_prompt([CALLOUT, SERVICE])
    assert "booked same day if asked before 14:00, 1 h notice" in text
    assert "HOLD it" in text and "never a clock time" in text and "safety instruction" in text
    # Agnostic by rule (2026-09-26): the line reaches every business with an
    # urgent service, so it names no trade, regulator or phone line.
    for word in ("gas", "boiler", "engineer", "0800"):
        assert word not in services.URGENT_PROMPT_LINE.lower()
    assert services.URGENT_PROMPT_LINE not in services.render_for_prompt([SERVICE])


def test_a_vulnerable_household_on_an_urgent_job_is_flagged_on_today(monkeypatch):
    from app import push, urgency
    sent = []
    monkeypatch.setattr(push, "notify", lambda tid, **k: sent.append(k))
    body = "Hi, no heating since this morning and my mum is 84, she lives with us. Can you come?"
    assert urgency.flag(body) == ("my mum is 84", "no heating since")
    assert urgency.maybe_flag("t1", contact="a@x.co", name="Ann", body=body,
                              service_types=[CALLOUT, SERVICE])
    assert sent[0]["kind"] == "vulnerable_household" and "urgent, and my mum is 84" in sent[0]["title"]
    # Any business's customer, in their own words (a vet's, a locksmith's).
    assert urgency.flag("Our dog is 14 and elderly, she stopped eating, can you see her today?")
    assert urgency.flag("Locked out with a newborn, it's urgent")
    # No urgent service on this diary, or no one at risk named: nothing.
    assert not urgency.maybe_flag("t1", contact="a@x.co", name="Ann", body=body, service_types=[SERVICE])
    assert not urgency.maybe_flag("t1", contact="a@x.co", name="Ann",
                                  body="Leaking tap, whenever suits.", service_types=[CALLOUT])
    assert urgency.flag("Two small kids in the house, no hot water since last night")
    assert len(sent) == 1


def test_an_older_relative_is_found_the_way_people_write_it():
    """Live 2026-09-30 (Hartwell rig): "I've got my mum staying with us, she's 84 … could someone come
    today?" raised no flag, because only "my mum is 84" written together matched."""
    from app import urgency
    t = ("Our boiler has stopped completely, no heating and no hot water. I've got my mum staying with us, "
         "she's 84 and really feels the cold. Is there any chance someone could come today?")
    assert urgency.flag(t)
    assert urgency.flag("My dad lives with us and he's 91. The heating is broken down, please come asap")
    # No false alarms: an age under 70, a relative with no age, or the age in another sentence.
    assert not urgency.flag("My mum's visiting and she's 45, can someone come today?")
    assert not urgency.flag("My mum recommended you. Can someone come today?")
    assert not urgency.flag("My dad is fine. She's 84 years into the lease. Come today?")
