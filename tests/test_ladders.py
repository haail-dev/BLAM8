"""Progress ladders — slice 1 (src/app/ladders.py, docs/plan-progress-packages.md).

Run #7 (the swim school, 2026-09-05): the level was a free-text confirm fact
("Stage 4") the operator typed; the portal read it back ("According to the
record, Finn is in Stage 4"). This slice makes the levels a SETTING — an
ordered dropdown on the kind (or a contact field when the customer is the one
who progresses) — with a prerequisite the booking gate honours, a prompt
block, and a level change that records, drafts for approval, or sends.

Run: python -m pytest tests/test_ladders.py -q
"""
from tests.booking_fixtures import carried_config
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from app import ladders, requirement_groups as rg, scheduling, settings_recommend
from app.scheduling import DIARY_TZ

STAGES = ["Stage 1", "Stage 2", "Stage 3", "Stage 4"]
CHILD = {"id": "g-child", "label": "Child", "label_plural": "Children", "cardinality": "many", "fields": [
    {"key": "date_of_birth", "label": "Date of birth", "type": "text", "required": True, "durability": "once"},
    {"key": "swim_england_stage", "label": "Swim England stage", "type": "dropdown", "options": STAGES,
     "required": False, "durability": "once", "gate": True,
     "ladder": {"notify": "approve", "gates": [{"service": "Stage 3 class", "min": "Stage 3"}]}},
]}


# ── the definition ───────────────────────────────────────────────────────────
def test_clean_field_keeps_a_ladder_and_makes_it_a_confirm_fact():
    f = rg.clean_field(CHILD["fields"][1])
    assert ladders.is_ladder(f)
    assert f["durability"] == "confirm" and f["gate"] is False
    assert f["ladder"] == {"notify": "approve", "gates": [{"service": "Stage 3 class", "min": "Stage 3"}]}
    # not a dropdown / one level: the block is dropped, the field survives
    assert "ladder" not in rg.clean_field({"label": "Age", "type": "number", "ladder": {"notify": "auto"}})
    assert "ladder" not in rg.clean_field({"label": "Belt", "type": "dropdown", "options": ["White"], "ladder": {}})


def test_editor_rows_land_on_the_kind_and_the_contact():
    lads = ladders.parse_editor(
        ["Child", "contact"], ["Swim England stage", "Driving level"],
        ["Stage 1, Stage 2, Stage 3", "learner\ntest-ready"], ["auto", "off"],
        ["Stage 3 class = Stage 3\nNonsense = Stage 9", ""])
    assert [l["who"] for l in lads] == ["Child", "contact"]
    assert lads[0]["levels"] == ["Stage 1", "Stage 2", "Stage 3"] and lads[0]["notify"] == "auto"
    assert lads[0]["gates"] == [{"service": "Stage 3 class", "min": "Stage 3"}]     # the unknown level dropped
    assert lads[1]["levels"] == ["learner", "test-ready"] and lads[1]["gates"] == []
    groups = ladders.apply_to_groups([{"label": "Child", "fields": [{"label": "Date of birth", "type": "text"}]}], lads)
    f = groups[0]["fields"][-1]
    assert ladders.is_ladder(f) and f["key"] == "swim_england_stage" and f["durability"] == "confirm"
    assert ladders.from_groups(groups)[0]["levels"] == ["Stage 1", "Stage 2", "Stage 3"]
    # a ladder no longer listed keeps its values as a plain dropdown
    plain = ladders.apply_to_groups(groups, [])
    assert plain[0]["fields"][-1]["type"] == "dropdown" and "ladder" not in plain[0]["fields"][-1]


def test_a_row_with_one_level_or_no_label_is_dropped():
    assert ladders.parse_editor(["Child"], ["Belt"], ["White"], ["approve"], [""]) == []
    assert ladders.parse_editor(["Child"], [""], ["a, b"], ["approve"], [""]) == []


def test_contact_ladders_sync_to_field_definitions(monkeypatch):
    calls = {"insert": [], "update": []}
    monkeypatch.setattr(ladders, "rg", rg)
    from app import db
    monkeypatch.setattr(db, "crm_list_field_definitions", lambda *a, **k: [
        {"id": "d-old", "key": "old_ladder", "entity": "contact", "ladder": {"notify": "auto"}},
        {"id": "d-plain", "key": "role", "entity": "contact", "ladder": None}])
    monkeypatch.setattr(db, "crm_insert_field_definition", lambda t, d, client=None: calls["insert"].append(d) or d)
    monkeypatch.setattr(db, "crm_update_field_definition", lambda i, t, f, client=None: calls["update"].append((i, f)))
    ladders.sync_contact_ladders("t1", [{"who": "contact", "label": "Driving level", "levels": ["learner", "test-ready"],
                                        "notify": "approve", "gates": []}])
    assert calls["insert"][0]["key"] == "driving_level" and calls["insert"][0]["field_type"] == "dropdown"
    assert calls["insert"][0]["options"] == ["learner", "test-ready"]
    assert ("d-old", {"ladder": None}) in calls["update"]           # no longer a ladder, values kept


# ── the prerequisite ─────────────────────────────────────────────────────────
def test_prerequisite_refuses_below_the_level_and_clears_at_it():
    g = rg.clean_group(CHILD)
    below = ladders.prerequisite_refusal(g, {"swim_england_stage": "Stage 2"}, "Finn", "Stage 3 class")
    assert below["error"] == "prerequisite" and "Finn is at Stage 2" in below["message"]
    assert "needs at least Stage 3" in below["message"] and "never book" in below["message"]
    none = ladders.prerequisite_refusal(g, {}, "Finn", "Stage 3 class")
    assert none["error"] == "prerequisite" and "no level on record yet" in none["message"]
    assert ladders.prerequisite_refusal(g, {"swim_england_stage": "Stage 3"}, "Finn", "Stage 3 class") is None
    assert ladders.prerequisite_refusal(g, {"swim_england_stage": "Stage 4"}, "Finn", "stage 3 class") is None
    assert ladders.prerequisite_refusal(g, {"swim_england_stage": "Stage 1"}, "Finn", "Assessment swim") is None


def test_the_executor_refuses_a_hold_below_the_prerequisite(monkeypatch):
    cfg = carried_config({"windows": "slots", "open_time": "08:00", "close_time": "18:30", "slot_minutes": 30,
           "capacity_per_window": 6, "min_notice_hours": 0, "working_days": "mon,tue,wed,thu,fri,sat,sun"})
    services = [{"name": "Stage 3 class", "minutes": 30}, {"name": "Assessment swim", "minutes": 15}]
    inserted = []
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(cfg))
    monkeypatch.setattr(scheduling.db, "list_active_bookings", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "offered_bookings_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "release_offered_for_contact", lambda *a, **k: 0)
    monkeypatch.setattr(scheduling.db, "set_booking_quote", lambda *a, **k: None)
    monkeypatch.setattr(scheduling.db, "crm_contact_by_email", lambda *a, **k: {"id": "c-kris"})
    monkeypatch.setattr(scheduling.db, "list_requirement_instances", lambda *a, **k: [
        {"id": "i-finn", "group_id": "g-child", "name": "Finn",
         "data": {"date_of_birth": "2 June 2018", "swim_england_stage": "Stage 2"}, "field_meta": {}}])
    monkeypatch.setattr(scheduling.db, "upsert_requirement_instance",
                        lambda t, g, c, name, data, meta: {"id": "i-" + name.lower()})
    monkeypatch.setattr(scheduling.db, "link_booking_subjects", lambda *a, **k: None)

    def fake_insert(tenant_id, date_iso, sb, length, summary, window="day", **kw):
        row = {"id": f"b{len(inserted) + 1}", "booking_ref": f"BK-{len(inserted) + 1}", "booking_date": date_iso,
               "start_block": sb, "length_blocks": length, "window": window, "summary": summary, **kw}
        inserted.append(row)
        return row
    monkeypatch.setattr(scheduling.db, "insert_stay_booking", fake_insert)
    execute, _ = scheduling.make_executor("t1", contact="kris@x.co", service_types=services,
                                          groups=[CHILD], now=datetime(2026, 9, 5, 9, 0, tzinfo=DIARY_TZ))
    args = {"date": "2026-09-19", "window": "10:00", "service": "Stage 3 class", "summary": "Stage 3 — Finn",
            "subjects": [{"name": "Finn", "group": "Child", "fields": {}}]}
    out = execute("hold_booking", args)
    assert out["error"] == "prerequisite" and out["have"] == "Stage 2" and not inserted
    ok = execute("hold_booking", {**args, "service": "Assessment swim", "window": "08:30"})
    assert ok.get("held") is True


def test_the_prompt_block_lists_the_levels_and_the_gates():
    text = rg.render_for_prompt([CHILD])
    assert "Swim England stage, levels in order: Stage 1 → Stage 2 → Stage 3 → Stage 4" in text
    assert "Stage 3 class needs at least Stage 3" in text
    assert "never from the customer's say-so" in text


# ── the level change ─────────────────────────────────────────────────────────
def _fake_env(monkeypatch, notify):
    from app import db, agent, mailbox, push, usage
    sent, logged, pushed, events = [], [], [], []
    monkeypatch.setattr(db, "log_agent_event", lambda t, kind, summary, **k: events.append((kind, summary, k)))
    monkeypatch.setattr(db, "effective_config", lambda *a, **k: {"agent_name": "Hannah"})
    monkeypatch.setattr(db, "insert_email_log", lambda *a, **k: (logged.append((a, k)) or ("log-1", None)))
    monkeypatch.setattr(db, "mark_email_log_sent", lambda *a, **k: None)
    monkeypatch.setattr(usage, "record_anthropic", lambda *a, **k: None)
    monkeypatch.setattr(push, "notify", lambda t, **k: pushed.append(k))

    async def compose(cfg, purpose, facts, **k):
        assert "Isla" in facts and "Stage 2" in facts and "level 2 of 4" in facts
        return SimpleNamespace(text="Isla has moved up to Stage 2 today.", model="m", usage={})
    monkeypatch.setattr(agent, "compose_outbound", compose)

    async def send_mail(t, to, subject, body, mailbox_id=None):
        sent.append((to, subject, body))
    monkeypatch.setattr(mailbox, "send_mail", send_mail)
    field = {**rg.clean_field(CHILD["fields"][1]), "ladder": {"notify": notify, "gates": []}}
    return field, sent, logged, pushed, events


def _run(coro):
    return asyncio.run(coro)


def test_a_move_records_and_drafts_for_approval(monkeypatch):
    field, sent, logged, pushed, events = _fake_env(monkeypatch, "approve")
    out = _run(ladders.on_level_change("t1", {"email": "kris@x.co", "name": "Kris"}, {"id": "i-isla", "name": "Isla"},
                                       CHILD, field, "Stage 1", "Stage 2"))
    assert out == {"ok": True, "notify": "approve", "sent": False, "log_id": "log-1",
                   "text": "Isla has moved up to Stage 2 today."}
    assert events[0][0] == "level_change" and "Isla moved to Stage 2" in events[0][1]
    assert logged[0][1]["status"] == "pending" and logged[0][1]["kind"] == "level_change"
    assert not sent and pushed[0]["kind"] == "level_change" and "approval" in pushed[0]["body"]


def test_a_move_on_auto_sends_and_records(monkeypatch):
    field, sent, logged, pushed, events = _fake_env(monkeypatch, "auto")
    out = _run(ladders.on_level_change("t1", {"email": "kris@x.co", "name": "Kris"}, {"id": "i-isla", "name": "Isla"},
                                       CHILD, field, "Stage 1", "Stage 2"))
    assert out["sent"] is True and sent[0][0] == "kris@x.co" and "Stage 2" in sent[0][1]
    assert pushed[0]["body"] == "Told Kris."


def test_a_move_on_off_only_records(monkeypatch):
    field, sent, logged, pushed, events = _fake_env(monkeypatch, "off")
    out = _run(ladders.on_level_change("t1", {"email": "kris@x.co"}, {"id": "i-isla", "name": "Isla"},
                                       CHILD, field, None, "Stage 2"))
    assert out == {"ok": True, "notify": "off", "sent": False}
    assert events and not logged and not sent and not pushed


def test_changed_ladder_sees_only_real_level_moves():
    g = rg.clean_group(CHILD)
    assert ladders.changed_ladder(g, {"swim_england_stage": "Stage 1"}, {"swim_england_stage": "Stage 2"}) == \
        [(g["fields"][1], "Stage 1", "Stage 2")]
    assert ladders.changed_ladder(g, {"swim_england_stage": "Stage 1"}, {"swim_england_stage": "stage 1"}) == []
    assert ladders.changed_ladder(g, {}, {"swim_england_stage": "Stage 9"}) == []      # not a level


# ── the card ─────────────────────────────────────────────────────────────────
def test_the_dependants_card_carries_a_ladder_row():
    out = settings_recommend.clean("dependants", [
        {"name": "group", "value": "Child | Children | several"},
        {"name": "ladder", "value": "Child | Swim England stage | Stage 1, Stage 2, Stage 3 | approve | Stage 3 class = Stage 3"},
        {"name": "ladder", "value": "child | swim england stage | Stage 1, Stage 2 | auto"},   # a dupe by who+label
        {"name": "ladder", "value": "customer | Driving level | learner, test-ready | off"},
    ])
    assert [v["value"].split("|")[1].strip() for v in out if v["name"] == "ladder"] == \
        ["Swim England stage", "Driving level"]
    assert "ladder" in settings_recommend.catalogue_block()


def test_the_contact_ladder_gates_the_customer_themselves(monkeypatch):
    """Run #8 (driving school): the pupil IS the customer — the ladder lives on
    the contact, and a service gated on it refuses below the level, refuses with
    "no level on record yet" when unset, and clears at the level."""
    from app import db as _db
    defs = [{"key": "dvsa", "label": "DVSA driver's record", "entity": "contact", "field_type": "dropdown",
             "options": ["Introduced", "Helped", "Prompted", "Test ready"],
             "ladder": {"notify": "approve", "gates": [{"service": "Test-day car hire", "min": "Test ready"}]}}]
    record = {"name": "Kris", "custom_data": {}}
    monkeypatch.setattr(_db, "crm_list_field_definitions", lambda tid, entity=None, client=None: defs)
    monkeypatch.setattr(_db, "crm_contact_by_email", lambda tid, email, client=None: record)
    none = ladders.contact_prerequisite_refusal("t1", "kris@x.co", "Test-day car hire")
    assert none["error"] == "prerequisite" and "no level on record yet" in none["message"]
    record["custom_data"] = {"dvsa": "Prompted"}
    below = ladders.contact_prerequisite_refusal("t1", "kris@x.co", "Test-day car hire")
    assert "Kris is at Prompted" in below["message"] and "needs at least Test ready" in below["message"]
    record["custom_data"] = {"dvsa": "Test ready"}
    assert ladders.contact_prerequisite_refusal("t1", "kris@x.co", "Test-day car hire") is None
    # an ungated service, an unknown contact, no contact at all: never a refusal
    record["custom_data"] = {}
    assert ladders.contact_prerequisite_refusal("t1", "kris@x.co", "1-hour lesson") is None
    monkeypatch.setattr(_db, "crm_contact_by_email", lambda tid, email, client=None: None)
    assert ladders.contact_prerequisite_refusal("t1", "kris@x.co", "Test-day car hire") is None
    assert ladders.contact_prerequisite_refusal("t1", None, "Test-day car hire") is None


def test_the_customers_own_level_change_is_written_to_them():
    """Run #8: the ladder on the CONTACT — the mover is the reader. The facts
    say so and demand the second person; a dependant's move still names the
    dependant to the parent."""
    field = {"key": "dvsa", "label": "DVSA driver's record", "type": "dropdown",
             "options": ["Introduced", "Helped", "Prompted", "Test ready"], "ladder": {"notify": "approve", "gates": []}}
    own = ladders.facts_for("Kris", "Customer", field, "Helped", "Prompted", "Kris")
    assert "the customer themselves" in own and "second person" in own and "you've moved up to Prompted" in own
    assert '"Kris has moved"' in own and "level 3 of 4" in own and "next level is Test ready" in own
    assert "about their own progress" in own
    child = ladders.facts_for("Finn", "Child", field, "Helped", "Prompted", "Kris")
    assert child.startswith("Finn (the customer's child) has moved to Prompted") and "The message is to Kris." in child
    assert "second person" not in child
    assert "write to them as 'you'" in ladders.PURPOSE
