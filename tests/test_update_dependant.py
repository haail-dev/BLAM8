"""Changing a dog's (any dependant's) details — by hand and from the Concierge.

Live 2026-09-23, kennels rig, iOS voice: "Bella's on 350g a day now, could you
update the record?" → update_contact aimed at a customer field that doesn't
exist; Bella's food lives on Bella. update_dependant stages the change on the
dependant itself and Confirm runs record_edits.save_dependant — the contact
card's form's own path (the by-hand principle).
"""
import asyncio

import pytest

from app import concierge, concierge_actions as ca, record_edits

DOG = {"id": "g-dog", "label": "Dog", "label_plural": "Dogs", "fields": [
    {"key": "vaccinations", "label": "Vaccinations", "type": "date", "gate": True},
    {"key": "breed", "label": "Breed", "type": "text"},
    {"key": "feeding_instructions", "label": "Feeding instructions", "type": "text"},
    {"key": "stay_level", "label": "Stay level", "type": "dropdown",
     "options": ["Not assessed", "Meet & greet done", "Trial night done"],
     "ladder": {"notify": "approve"}},
]}
BELLA = {"id": "i-bella", "group_id": "g-dog", "contact_id": "c-hannah", "name": "Bella",
         "data": {"feeding_instructions": "300g a day split into two feeds", "breed": "Labrador",
                  "vaccinations": "2027-09-21", "stay_level": "Trial night done"}}
HANNAH = {"id": "c-hannah", "name": "Hannah Price", "email": "hannah@x.com"}


# ── the plan: which detail, what value, checked strictly ────────────────────
def test_the_plan_finds_the_detail_and_checks_the_value():
    f, v = record_edits.plan_dependant_field(DOG, BELLA, "Feeding instructions", "350g a day split into two feeds")
    assert f["key"] == "feeding_instructions" and v == "350g a day split into two feeds"
    # a shortened label still finds its one field
    assert record_edits.plan_dependant_field(DOG, BELLA, "feeding", "350g")[0]["key"] == "feeding_instructions"
    # a date is a real date, stored ISO
    assert record_edits.plan_dependant_field(DOG, BELLA, "Vaccinations", "1 July 2028")[1] == "2028-07-01"
    # a fixed list takes its own word, in its own spelling
    assert record_edits.plan_dependant_field(DOG, BELLA, "Stay level", "meet & greet done")[1] == "Meet & greet done"


@pytest.mark.parametrize("label,value,says", [
    ("Colour", "Yellow", "has no 'Colour'"),
    ("Vaccinations", "next week", "needs a date"),
    ("Stay level", "Expert", "choose one of: Not assessed"),
    ("Breed", "Labrador", "already 'Labrador'"),
])
def test_the_plan_refuses_what_it_cant_store(label, value, says):
    with pytest.raises(record_edits.EditError) as e:
        record_edits.plan_dependant_field(DOG, BELLA, label, value)
    assert says in str(e.value)


# ── the card ─────────────────────────────────────────────────────────────────
def _stub_lookup(monkeypatch, instances):
    monkeypatch.setattr(ca.db, "list_requirement_instances_all", lambda t, client=None, **k: instances)
    monkeypatch.setattr(ca.db, "list_requirement_instances", lambda t, cid, client=None: instances)
    monkeypatch.setattr(ca.db, "list_requirement_groups", lambda t, client=None: [DOG])
    monkeypatch.setattr(ca.db, "crm_get_contact", lambda cid, t, client=None: HANNAH)


def test_the_card_names_the_dog_the_owner_and_the_change(monkeypatch):
    _stub_lookup(monkeypatch, [BELLA])
    summary, payload, details, _ = ca._stage_update_dependant(
        "t1", None, {"name": "bella", "title": "Feeding instructions", "content": "350g a day split into two feeds"})
    assert summary == ("Update bella (Hannah Price) — Feeding instructions: "
                       "300g a day split into two feeds → 350g a day split into two feeds")
    assert payload["instance_id"] == "i-bella" and payload["key"] == "feeding_instructions"
    assert "Dog: bella, on Hannah Price's record" in details[0]


def test_two_dogs_with_one_name_ask_whose(monkeypatch):
    other = dict(BELLA, id="i-bella-2", contact_id="c-other")
    _stub_lookup(monkeypatch, [BELLA, other])
    with pytest.raises(ca.ActionError) as e:
        ca._stage_update_dependant("t1", None, {"name": "Bella", "title": "Breed", "content": "Collie"})
    assert "more than one Bella" in str(e.value)


def test_an_unknown_dog_is_said_plainly(monkeypatch):
    _stub_lookup(monkeypatch, [BELLA])
    with pytest.raises(ca.ActionError) as e:
        ca._stage_update_dependant("t1", None, {"name": "Max", "title": "Breed", "content": "Collie"})
    assert "can't find Max" in str(e.value)


def test_a_level_says_it_sends_the_level_message(monkeypatch):
    _stub_lookup(monkeypatch, [BELLA])
    _, _, details, _ = ca._stage_update_dependant(
        "t1", None, {"name": "Bella", "title": "Stay level", "content": "Meet & greet done"})
    assert any("sends the level message" in d for d in details)


def test_confirm_runs_the_forms_own_path(monkeypatch):
    seen = {}

    async def save(tid, contact, group, name, raw, via, client=None):
        seen.update(contact=contact["id"], group=group["id"], name=name, raw=raw, via=via)
        return {"id": "i-bella"}
    monkeypatch.setattr(ca.db, "get_requirement_instance", lambda iid, t, client=None: BELLA)
    monkeypatch.setattr(ca.db, "crm_get_contact", lambda cid, t, client=None: HANNAH)
    monkeypatch.setattr(ca.db, "list_requirement_groups", lambda t, client=None: [DOG])
    monkeypatch.setattr(record_edits, "save_dependant", save)
    out = asyncio.run(ca._exec_update_dependant("t1", None, {
        "instance_id": "i-bella", "contact_id": "c-hannah", "group_id": "g-dog", "name": "Bella",
        "key": "feeding_instructions", "label": "Feeding instructions",
        "value": "350g a day", "shown": "350g a day"}, None))
    assert seen == {"contact": "c-hannah", "group": "g-dog", "name": "Bella",
                    "raw": {"feeding_instructions": "350g a day"}, "via": "the Concierge"}
    assert out["summary"] == "Done — Bella's Feeding instructions is now '350g a day'."
    assert out["link"]["href"] == "/app/crm/contacts/c-hannah"


# ── one path for both doors ──────────────────────────────────────────────────
def test_save_dependant_writes_and_leaves_a_timeline_line(monkeypatch):
    stamps, events, upserts = [], [], []
    monkeypatch.setattr(record_edits.db, "list_requirement_instances", lambda t, cid, client=None: [BELLA])
    monkeypatch.setattr(record_edits.db, "upsert_requirement_instance",
                        lambda t, gid, cid, name, data, meta, client=None: upserts.append(data) or {"id": "i-bella", **BELLA, "data": data})
    monkeypatch.setattr(record_edits, "_stamp", lambda t, cid, subj, line: stamps.append(line))
    monkeypatch.setattr(record_edits.db, "log_agent_event", lambda *a, **k: events.append(a))
    from app import dependants_extract, ladders
    monkeypatch.setattr(dependants_extract, "absorb_new_chips", lambda *a, **k: None)
    monkeypatch.setattr(ladders, "changed_ladder", lambda g, old, new: [])
    asyncio.run(record_edits.save_dependant("t1", HANNAH, DOG, "Bella",
                                            {"feeding_instructions": "350g a day"}, via="the Concierge"))
    assert upserts[0]["feeding_instructions"] == "350g a day"
    assert upserts[0]["breed"] == "Labrador"          # the rest of Bella is untouched
    assert stamps == ["Bella: Feeding instructions set to '350g a day' "
                      "(was '300g a day split into two feeds') — by you, from the Concierge"]


def test_the_contact_card_form_goes_through_the_same_function():
    src = open("src/app/dashboard.py", encoding="utf-8").read()
    route = src[src.index("async def crm_contact_dependant_save"):][:1600]
    assert "record_edits.save_dependant(" in route
    assert "upsert_requirement_instance" not in route


def test_the_model_is_offered_update_dependant():
    assert "update_dependant" in concierge.BUSINESS_ACTION_KINDS
    assert "update_dependant" in ca.allowed_kinds(full_qa=False, scheduling_on=False)
    assert ca.ability_for("update_dependant") == ("crm", "edit")
    for full in (True, False):
        p = concierge.build_business_prompt(business_name="Little Paws", today="x", full_qa=full)
        assert "update_dependant (change ONE detail of a dependant" in p
