"""Who's on the job (src/app/job_people.py; docs/plan-people-team-and-jobs.md
step 2; Kris 2026-09-24: "delegate the next drop-off to Em")."""
import asyncio

import pytest

from app import concierge_actions, concierge_data, db, job_people, people

T = "t1"
TEAM = [
    {"id": "p-jul", "name": "Julius", "role": "owner", "user_id": "u-jul", "does": ["desk"], "active": True,
     "created_at": "1"},
    {"id": "p-em", "name": "Em Hopkins", "role": "staff", "user_id": "u-em", "does": ["svc:Meet & greet"],
     "active": True, "created_at": "2"},
    {"id": "p-kr", "name": "Kristie", "role": "staff", "user_id": None, "does": [], "active": True,
     "created_at": "3"},
]
STAY = {"id": "b-stay", "booking_ref": "BK-1040", "summary": "Bella — boarding", "status": "confirmed",
        "booking_date": "2099-10-02", "resource_type": ""}
MG = {"id": "b-mg", "booking_ref": "BK-1041", "summary": "Meet & greet — Otis", "status": "confirmed",
      "booking_date": "2099-10-03", "resource_type": "Meet & greet"}


@pytest.fixture
def rig(monkeypatch):
    on: list[dict] = []
    cfg = {"people_per_job": "one",
           "service_types": [{"name": "Meet & greet", "minutes": 30, "timing": "time"}]}
    monkeypatch.setattr(db, "list_team_people", lambda tid: list(TEAM))
    monkeypatch.setattr(db, "list_booking_people",
                        lambda tid, ids: [r for r in on if r["booking_id"] in ids])

    def add(row):
        on[:] = [r for r in on if not (r["booking_id"] == row["booking_id"]
                                       and r["person_id"] == row["person_id"])]
        on.append({**row, "created_at": str(len(on))})

    def remove(tid, bid, pids=None):
        on[:] = [r for r in on if not (r["booking_id"] == bid and (pids is None or r["person_id"] in pids))]
    monkeypatch.setattr(db, "add_booking_person", add)
    monkeypatch.setattr(db, "remove_booking_people", remove)
    monkeypatch.setattr(db, "get_scheduling_config", lambda tid, client=None: cfg)
    books = {"b-stay": dict(STAY), "b-mg": dict(MG)}
    monkeypatch.setattr(concierge_actions, "_resolve_booking",
                        lambda tid, c, raw: books.get(raw) or next(
                            (b for b in books.values() if b["booking_ref"] == raw), None))
    monkeypatch.setattr(db, "get_booking", lambda bid, tid, client=None: books.get(bid))
    return on, cfg


def test_a_name_finds_the_person_as_people_say_it():
    roster = [people.team_person(r) for r in TEAM]
    assert job_people.find_person(roster, "em")["id"] == "p-em"
    assert job_people.find_person(roster, "Em Hopkins")["id"] == "p-em"
    assert job_people.find_person(roster, "kris")["id"] == "p-kr"
    assert job_people.find_person(roster, "Bella") is None


def test_one_person_per_job_replaces_several_adds(rig):
    on, cfg = rig
    assert job_people.put_on(T, "b-stay", "p-jul", cfg=cfg) == []
    assert job_people.put_on(T, "b-stay", "p-em", cfg=cfg) == ["Julius"]
    assert [r["person_id"] for r in on] == ["p-em"]
    cfg["people_per_job"] = "several"
    assert job_people.put_on(T, "b-stay", "p-kr", cfg=cfg, part="drop-off") == []
    names = [p["name"] for p in job_people.by_booking(T, ["b-stay"])["b-stay"]]
    assert names == ["Em Hopkins", "Kristie"]


def test_a_job_she_is_on_is_hers_whatever_its_kind(rig):
    on, cfg = rig
    em = people.member({"user_id": "u-em", "role": "staff",
                        "scopes": {"pages": ["today", "diary"], "does": ["svc:Meet & greet"]}})
    stay = dict(STAY)
    job_people.annotate(T, [stay])
    assert not people.owns_booking(em, stay, cfg)          # a stay is the desk's work, not hers
    job_people.put_on(T, "b-stay", "p-em", cfg=cfg)
    stay = dict(STAY)
    job_people.annotate(T, [stay])
    assert stay["on_it"] == ["Em Hopkins"] and stay["on_it_users"] == ["u-em"]
    assert people.owns_booking(em, stay, cfg) and people.does_booking(em, stay, cfg)


def test_the_card_names_who_comes_off_and_confirm_puts_them_on(rig, monkeypatch):
    on, cfg = rig
    job_people.put_on(T, "b-stay", "p-jul", cfg=cfg)
    summary, payload, details, _ = concierge_actions._stage_assign_person(
        T, None, {"booking_id": "BK-1040", "person": "Em"})
    assert summary.startswith("Put Em Hopkins on Bella — boarding (BK-1040)")
    assert "Em Hopkins sees it on their Today and Diary" in details
    assert "Takes Julius off it (one person per job)" in details
    monkeypatch.setattr(db, "log_agent_event", lambda *a, **k: None)
    out = asyncio.run(concierge_actions._exec_assign_person(T, None, payload, None))
    assert "Em Hopkins is on Bella — boarding, instead of Julius" in out["summary"]
    assert [r["person_id"] for r in on] == ["p-em"]
    with pytest.raises(concierge_actions.ActionError, match="already on that job"):
        concierge_actions._stage_assign_person(T, None, {"booking_id": "BK-1040", "person": "Em"})
    with pytest.raises(concierge_actions.ActionError, match="nobody called 'Bella' on the team"):
        concierge_actions._stage_assign_person(T, None, {"booking_id": "BK-1040", "person": "Bella"})
    _s, _p, d2, _ = concierge_actions._stage_assign_person(T, None, {"booking_id": "b-stay", "person": "Kristie"})
    assert "Kristie has no mobile or email yet — add one in People to send the job sheet" in d2


def test_the_diary_lookup_answers_whats_em_doing_and_narrows_for_staff(rig, monkeypatch):
    on, cfg = rig
    monkeypatch.setattr(db, "search_bookings", lambda *a, **k: [dict(STAY), dict(MG)])
    monkeypatch.setattr(db, "subjects_for_bookings", lambda *a, **k: {})
    run = lambda args, member=None: concierge_data._list_bookings(T, None, args, member)
    everything = run({})
    assert {b["reference"] for b in everything["bookings"]} == {"BK-1040", "BK-1041"}
    assert next(b for b in everything["bookings"] if b["reference"] == "BK-1041")["work"] == "Meet & greet"
    # Em does meet & greets: that's hers; the stay isn't, until she's put on it.
    assert [b["reference"] for b in run({"person": "Em"})["bookings"]] == ["BK-1041"]
    job_people.put_on(T, "b-stay", "p-em", cfg=cfg)
    mine = run({"person": "em"})["bookings"]
    assert {b["reference"] for b in mine} == {"BK-1040", "BK-1041"}
    assert next(b for b in mine if b["reference"] == "BK-1040")["on_it"] == ["Em Hopkins"]
    assert "Nobody called 'Bella'" in run({"person": "Bella"})["note"]
    # A staff member asking is narrowed exactly as their Diary page is.
    kr = people.member({"user_id": "u-kr2", "role": "staff",
                        "scopes": {"pages": ["today", "diary"], "does": ["svc:Meet & greet"]}})
    assert [b["reference"] for b in run({}, kr)["bookings"]] == ["BK-1041"]


def test_the_team_is_told_to_the_concierge(rig):
    note = concierge_actions.team_note(T)
    assert note.startswith("[The team — people who work here, NOT customers:")
    assert "Em Hopkins (Staff; does meet & greet)" in note
    assert "Kristie (Staff, freelancer or helper, gets job sheets)" in note


def test_the_job_page_and_the_setting_are_wired():
    job = open("src/app/templates/app/diary_job.html", encoding="utf-8").read()
    assert "data-whos-on-it" in job and 'action="/app/diary/{{ b.id }}/people"' in job
    diary = open("src/app/templates/app/diary_settings.html", encoding="utf-8").read()
    people_settings = open("src/app/templates/app/people_settings.html", encoding="utf-8").read()
    assert 'action="/app/diary/people-per-job"' not in diary
    assert 'action="/app/people/settings"' in people_settings and 'value="several"' in people_settings


def test_a_name_heard_as_a_letter_or_under_another_key_still_finds_em(rig):
    # Live 2026-09-24: "Em" came back as "M", and the card said "nobody called ''".
    roster = [people.team_person(r) for r in TEAM]
    assert job_people.find_person(roster, "M")["id"] == "p-em"
    assert job_people.find_person(roster, "M.")["id"] == "p-em"
    summary, *_ = concierge_actions._stage_assign_person(T, None, {"booking_id": "BK-1040", "name": "M"})
    assert summary.startswith("Put Em Hopkins on")
    with pytest.raises(concierge_actions.ActionError, match="I didn't catch who to put on it"):
        concierge_actions._stage_assign_person(T, None, {"booking_id": "BK-1040"})


def test_the_transcriber_is_told_the_team_names():
    src = open("src/app/concierge_routes.py", encoding="utf-8").read()
    assert '"The people who work there: "' in src


def test_the_parser_keeps_who_and_their_part():
    # Live 2026-09-24: the model wrote {"booking_id": "BK-1010", "person": "Em"}
    # and the parser's field list dropped "person" — so test through the parser.
    import json
    from app import concierge
    text = json.dumps({"reply": "Shall I?", "links": [], "action": {
        "kind": "assign_person",
        "fields": json.dumps({"booking_id": "BK-1010", "person": "Em", "part": "collection"})}})
    _reply, _links, action = concierge._parse_business(text)
    assert action["kind"] == "assign_person"
    assert action["person"] == "Em" and action["part"] == "collection" and action["booking_id"] == "BK-1010"
