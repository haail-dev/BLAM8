"""Dependants Phases 2+3 (docs/modules/module-requirement-groups.md).

Read, not typed: extraction from any inbound email plans writes and chips by
confidence and durability; a new dependant is a chip that creates the row;
expiry dates that lapse before a booking earn a word; the concierge stages a
kind from prose; the portal lets the customer keep their own current.

Run: python -m pytest tests/test_dependants_phase23.py -q
"""
import json
from datetime import date

import pytest

from app import dependants_extract as dx
from app import dependants_sweep as sweep
from app import requirement_groups as rg

HORSE = rg.clean_group({"id": "g-horse", "label": "Horse", "fields": [
    {"label": "Age", "type": "number", "durability": "once"},
    {"label": "Current shoeing", "type": "dropdown", "options": ["Shod all round", "Fronts only", "Barefoot"],
     "required": True, "durability": "confirm"},
    {"label": "Vaccination expiry", "type": "date", "durability": "expires", "gate": True},
]})
MURPHY = {"id": "i-murphy", "group_id": "g-horse", "contact_id": "c-1", "name": "Murphy",
          "data": {"age": 12, "current_shoeing": "Shod all round"}, "field_meta": {}}


# ── extraction: the plan ─────────────────────────────────────────────────────
def test_sure_reads_write_middling_reads_become_chips_and_once_is_never_overwritten():
    steps = dx.plan([HORSE], [MURPHY], [
        {"group": "Horse", "name": "murphy", "confidence": 0.95, "fields": [
            {"key": "current_shoeing", "value": "Fronts only", "confidence": 0.9},   # confirm-field, sure → write
            {"key": "vaccination_expiry", "value": "2027-03-01", "confidence": 0.6},  # middling → chip
            {"key": "Age", "value": "13", "confidence": 0.95},                        # once, on record → chip
            {"key": "Colour", "value": "bay", "confidence": 0.9}]}])                  # not a field → dropped
    kinds = [(s["action"], s["data"]) for s in steps]
    assert kinds == [("write", {"current_shoeing": "Fronts only"}),
                     ("suggest", {"vaccination_expiry": "2027-03-01", "age": 13})]


def test_a_new_dependant_is_a_chip_not_a_row_and_a_bare_mention_is_nothing():
    steps = dx.plan([HORSE], [MURPHY], [
        {"group": "horses", "name": "Biscuit", "confidence": 0.9,
         "fields": [{"key": "Age", "value": "2", "confidence": 0.9}]},
        {"group": "horse", "name": "Somebody's horse", "confidence": 0.3, "fields": []},
        {"group": "horse", "name": "Tilly", "confidence": 0.5, "fields": []}])
    assert [(s["action"], s["name"]) for s in steps] == [("suggest", "Biscuit"), ("skip", "Somebody's horse"),
                                                         ("skip", "Tilly")]
    assert steps[0]["existing"] is None and steps[0]["data"] == {"age": 2}


def test_apply_writes_by_durability_and_queues_the_chips(monkeypatch):
    written, chips = [], []
    monkeypatch.setattr(dx.db, "upsert_requirement_instance",
                        lambda t, gid, cid, name, data, meta=None, client=None: written.append((name, data)) or {"id": "x"})
    monkeypatch.setattr(dx.db, "crm_insert_field_suggestion", lambda t, **kw: chips.append(kw) or True)
    tally = dx.apply("t1", "c-1", [HORSE], [MURPHY], [
        {"group": "Horse", "name": "Murphy", "confidence": 0.95, "fields": [
            {"key": "current_shoeing", "value": "Barefoot", "confidence": 0.9},
            {"key": "Age", "value": "13", "confidence": 0.95}]},
        {"group": "Horse", "name": "Biscuit", "confidence": 0.9,
         "fields": [{"key": "Age", "value": "2", "confidence": 0.9}]}], "e1")
    assert written == [("Murphy", {"age": 12, "current_shoeing": "Barefoot"})]
    assert tally == {"written": 1, "suggested": 2, "skipped": 0}
    assert chips[0]["entity"] == "dependant" and chips[0]["record_id"] == "i-murphy" and chips[0]["field_key"] == "age"
    assert chips[1]["record_id"] == "c-1" and chips[1]["field_key"] == "__new__:g-horse"
    assert json.loads(chips[1]["value"]) == {"name": "Biscuit", "fields": {"age": 2}}


def test_accepting_a_new_chip_creates_the_row_and_a_field_chip_merges(monkeypatch):
    monkeypatch.setattr(dx.db, "list_requirement_groups", lambda t, client=None: [HORSE])
    made = []
    monkeypatch.setattr(dx.db, "upsert_requirement_instance",
                        lambda t, gid, cid, name, data, meta=None, client=None: made.append((cid, name, data, {k: v["source"] for k, v in meta.items()})) or {"id": "n"})
    assert dx.accept("t1", {"record_id": "c-1", "field_key": "__new__:g-horse",
                            "value": json.dumps({"name": "Biscuit", "fields": {"age": 2}}), "email_log_id": "e1"})
    assert made == [("c-1", "Biscuit", {"age": 2}, {"age": "confirmed"})]
    monkeypatch.setattr(dx.db, "get_requirement_instance", lambda i, t, client=None: dict(MURPHY))
    assert dx.accept("t1", {"record_id": "i-murphy", "field_key": "age", "value": "13"})
    assert made[-1][1] == "Murphy" and made[-1][2]["age"] == 13         # a confirmed chip overrides a once-field
    assert not dx.accept("t1", {"record_id": "c-1", "field_key": "__new__:g-nope", "value": "{}"})


def test_annotate_writes_the_chip_in_the_operators_words():
    out = dx.annotate([
        {"id": "s1", "entity": "dependant", "record_id": "i-murphy", "field_key": "age", "value": "13"},
        {"id": "s2", "entity": "dependant", "record_id": "c-1", "field_key": "__new__:g-horse",
         "value": json.dumps({"name": "Biscuit", "fields": {"age": 2}})},
        {"id": "s3", "entity": "contact", "record_id": "c-1", "field_key": "phone", "value": "x"}],
        [HORSE], [MURPHY])
    assert [o["id"] for o in out] == ["s1", "s2"]
    assert out[0]["who"] == "Murphy" and out[0]["label"] == "Age" and out[0]["replaces"] == "12"
    assert out[1]["kind"] == "new" and out[1]["label"] == "a new horse" and out[1]["value_text"] == "Age: 2"


BISCUIT = {"id": "i-biscuit", "group_id": "g-horse", "contact_id": "c-1", "name": "Biscuit",
           "data": {"current_shoeing": "Shod all round"}, "field_meta": {}}


def test_a_dependant_that_arrived_by_another_door_absorbs_its_new_chip(monkeypatch):
    # kennels film 2026-09-20: the enquiry queued "a new dog: Biscuit — add?", the
    # booking then created Biscuit, and the chip sat above the row it offered to make
    chip = {"id": "s-new", "entity": "dependant", "record_id": "c-1", "field_key": "__new__:g-horse",
            "value": json.dumps({"name": "biscuit", "fields": {"age": 2, "current_shoeing": "Barefoot"}}),
            "email_log_id": "e1", "status": "pending"}
    monkeypatch.setattr(dx.db, "crm_pending_suggestions", lambda t, record_ids=None, client=None: [chip])
    saved, resolved = [], []
    monkeypatch.setattr(dx.db, "upsert_requirement_instance",
                        lambda t, gid, cid, name, data, meta=None, client=None:
                        saved.append((name, data, {k: v["source"] for k, v in meta.items()}))
                        or {**BISCUIT, "name": name, "data": data, "field_meta": meta})
    monkeypatch.setattr(dx.db, "crm_resolve_suggestion",
                        lambda sid, t, status, client=None: resolved.append((sid, status)))
    assert dx.absorb_new_chips("t1", "c-1", HORSE, dict(BISCUIT)) == 1
    # the row is the newer read: its shoeing stands; the chip's age fills the gap
    assert saved == [("Biscuit", {"current_shoeing": "Shod all round", "age": 2}, {"age": "extracted"})]
    assert resolved == [("s-new", "accepted")]
    # a chip for someone else is left alone
    saved.clear(); resolved.clear()
    assert dx.absorb_new_chips("t1", "c-1", HORSE, dict(MURPHY)) == 0
    assert saved == [] and resolved == []


def test_annotate_drops_a_new_chip_for_a_dependant_already_on_record():
    out = dx.annotate([
        {"id": "s2", "entity": "dependant", "record_id": "c-1", "field_key": "__new__:g-horse",
         "value": json.dumps({"name": "biscuit", "fields": {"age": 2}})}],
        [HORSE], [MURPHY, BISCUIT])
    assert out == []


def test_the_prompt_carries_the_kinds_and_the_record_and_the_parser_is_forgiving():
    p = dx.build_user_prompt([HORSE], [MURPHY], "Re: Murphy", "He's 12 and shod all round", "Kris")
    assert "kind 'Horse' (plural Horses)" in p and "key=vaccination_expiry" in p
    assert "Horse 'Murphy': {\"age\": 12" in p
    assert dx.parse_response("nope") == [] and dx.parse_response('{"instances":[{"name":"x"}]}') == [{"name": "x"}]


# ── expiry chases ────────────────────────────────────────────────────────────
def test_due_names_a_lapse_before_the_booking_and_a_lapse_within_the_window():
    inst = [dict(MURPHY, data={"vaccination_expiry": "2026-10-10"}),
            {"id": "i-tilly", "group_id": "g-horse", "contact_id": "c-1", "name": "Tilly",
             "data": {"vaccination_expiry": "2027-06-01"}}]
    links = {"i-murphy": [{"id": "b1", "booking_ref": "BK-7", "booking_date": "2026-10-15"}],
             "i-tilly": [{"id": "b2", "booking_ref": "BK-8", "booking_date": "2026-10-15"}]}
    out = sweep.due([HORSE], inst, links, date(2026, 9, 20))
    assert [(o["instance"]["name"], o["before_booking"], o["lapsed"]) for o in out] == [("Murphy", True, False)]
    title, body = sweep.phrase(out[0])
    assert title == "Murphy's vaccination expiry lapses before BK-7"
    assert "10th Oct 26" in body and "15th Oct 26" in body
    # already lapsed
    out2 = sweep.due([HORSE], [dict(MURPHY, data={"vaccination_expiry": "2026-09-01"})], links, date(2026, 9, 20))
    assert out2 and out2[0]["lapsed"] and sweep.phrase(out2[0])[0] == "Murphy's vaccination expiry has lapsed"
    # within the window but after the booking: still a word, the softer one
    out3 = sweep.due([HORSE], [dict(MURPHY, data={"vaccination_expiry": "2026-09-30"})],
                     {"i-murphy": [{"id": "b1", "booking_ref": "BK-7", "booking_date": "2026-09-25"}]}, date(2026, 9, 20))
    assert out3 and not out3[0]["before_booking"] and "worth asking for the renewal now" in sweep.phrase(out3[0])[1]
    # no upcoming booking, or no date: nothing to say
    assert sweep.due([HORSE], inst, {}, date(2026, 9, 20)) == []
    assert sweep.due([HORSE], [dict(MURPHY, data={})], links, date(2026, 9, 20)) == []


def test_the_feed_and_the_push_families_know_the_kind():
    from app import deck, notify_prefs
    assert deck.topic_of("dependant_expiry") == "bookings"
    assert notify_prefs._BY_KIND["dependant_expiry"] == "notify_bookings"
    from app import usage
    assert usage.SOURCE_BY_OPERATION["dependant_extract"] == "inbound_email"


# ── the concierge configures a kind from prose ───────────────────────────────
def test_define_dependants_stages_a_kind_from_prose_lines(monkeypatch):
    from app import concierge_actions as ca
    monkeypatch.setattr(ca.db, "list_requirement_groups", lambda t, client=None: [])
    summary, payload, details, draft = ca._stage_define_dependants("t1", None, {
        "title": "Horse", "name": "Horses",
        "content": "Age | optional | once | no gate | in years\n"
                   "Current shoeing | required | confirm | no gate | shod all round, fronts only or barefoot\n"
                   "Vaccination expiry | optional | expires | gate | the date on the record"})
    g = payload["group"]
    assert summary.startswith("Add the kind “Horse” (Horses) — 3 things")
    assert [f["key"] for f in g["fields"]] == ["age", "current_shoeing", "vaccination_expiry"]
    assert g["fields"][2]["type"] == "date" and g["fields"][2]["gate"] is True
    assert details[1].startswith("Current shoeing — required, confirm each time")
    with pytest.raises(ca.ActionError):
        ca._stage_define_dependants("t1", None, {"title": "Horse", "content": ""})
    assert "define_dependants" in ca.allowed_kinds(False, False)
    from app import concierge
    assert "define_dependants" in concierge.BUSINESS_ACTION_KINDS


@pytest.mark.asyncio
async def test_define_dependants_executes_into_the_tenants_kinds(monkeypatch):
    from app import concierge_actions as ca
    monkeypatch.setattr(ca.db, "list_requirement_groups", lambda t, client=None: [HORSE])
    saved = {}
    monkeypatch.setattr(ca.db, "save_requirement_groups", lambda t, groups, client=None: saved.update(g=groups) or groups)
    monkeypatch.setattr(ca.db, "log_agent_event", lambda *a, **k: None)
    dog = rg.clean_group({"label": "Dog", "fields": [{"label": "Breed"}]})
    out = await ca._exec_define_dependants("t1", None, {"group": dog}, False)
    assert [g["label"] for g in saved["g"]] == ["Horse", "Dog"] and "dogs now" in out["summary"]


def test_the_recommend_catalogue_and_the_interview_carry_the_question():
    from app import settings_recommend, concierge
    assert "dependants" in settings_recommend.PAGES
    vals = settings_recommend.clean("dependants", [{"name": "group", "value": "Horse | Horses | several"},
                                                   {"name": "field", "value": "Horse | Age | optional | once | no gate | in years"}])
    assert vals and [v["name"] for v in vals] == ["group", "field"]
    src = concierge.ONBOARDING_STEPS["interview"]["guidance"] + concierge.build_business_prompt()
    assert "is a booking about something that belongs to the" in src and "a person, item or other named subject" in src
    assert "DEPENDANTS (define_dependants)" in src


# ── portal self-service ──────────────────────────────────────────────────────
def test_the_customer_updates_their_own_dependant_on_the_portal(monkeypatch):
    from app import portal_data as pd
    from types import SimpleNamespace
    session = SimpleNamespace(tenant_id="t1", contact={"id": "c-1", "email": "k@x"}, profile=("k@x", [], []),
                              _dep_groups=[HORSE])
    monkeypatch.setattr(pd, "_trace", lambda *a, **k: None)
    monkeypatch.setattr(pd.db, "list_requirement_instances", lambda t, cid, client=None: [dict(MURPHY)])
    saved = []
    monkeypatch.setattr(pd.db, "upsert_requirement_instance",
                        lambda t, gid, cid, name, data, meta=None, client=None: saved.append((name, data)) or {"id": "i-murphy", "name": name})
    out = pd._update_dependant(session, {"name": "murphy", "fields": {"Vaccination expiry": "12/03/2027", "Age": "13"}})
    assert out["saved"] and out["changed"] == ["Vaccination expiry"]       # the customer can't rewrite a once-field
    assert saved == [("Murphy", {"age": 12, "current_shoeing": "Shod all round", "vaccination_expiry": "2027-03-12"})]
    assert session.profile is None                                        # re-read next turn
    out2 = pd._update_dependant(session, {"name": "Biscuit", "fields": {"Current shoeing": "Barefoot"}})
    assert out2["saved"] and out2["new"] and saved[-1] == ("Biscuit", {"current_shoeing": "Barefoot"})
    assert pd._update_dependant(SimpleNamespace(tenant_id="t1", contact=None), {"name": "x", "fields": {}}) is pd.NOT_IDENTIFIED
    assert any(t["name"] == "update_dependant" for t in pd.TOOLS)
