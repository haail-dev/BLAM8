import json
"""Dependants (docs/modules/module-requirement-groups.md, src/app/requirement_groups.py).

The booking is about something that belongs to the customer — a horse, a
dog, a vehicle — defined in the tenant's own words, remembered per instance,
gated per instance. Pins: the definition rules, the tool argument, the
per-instance gate (required, placeholder, expiry against the booking date),
durability on merge, the prompt blocks, and the executor wiring.

Run: python -m pytest tests/test_requirement_groups.py -q
"""
from datetime import datetime

import pytest

from app import requirement_groups as rg
from app import scheduling

NOW = datetime(2026, 9, 4, 9, 0)

HORSE = {"id": "g-horse", "label": "Horse", "cardinality": "many", "fields": [
    {"label": "Age", "type": "number", "required": False, "durability": "once"},
    {"label": "Current shoeing", "type": "dropdown", "options": ["Shod all round", "Fronts only", "Barefoot"],
     "required": True, "durability": "confirm"},
    {"label": "Temperament", "type": "text", "required": False, "durability": "confirm"},
    {"label": "Vaccination expiry", "type": "date", "required": False, "durability": "expires", "gate": True},
]}


# ── definitions ──────────────────────────────────────────────────────────────
def test_clean_group_speaks_the_operators_words_and_caps_the_shape():
    g = rg.clean_group({"label": " Horse ", "fields": [
        {"label": "Age", "type": "number"}, {"label": "age", "type": "text"},   # dupe by label
        {"label": "Passport expiry", "type": "text", "durability": "expires", "gate": True},
        {"label": "", "type": "text"}]})
    assert g["label"] == "Horse" and g["label_plural"] == "Horses" and g["cardinality"] == "many"
    assert [f["key"] for f in g["fields"]] == ["age", "passport_expiry"]
    assert g["fields"][1]["type"] == "date"                     # an expiry IS a date
    assert g["fields"][1]["gate"] is True
    assert rg.clean_group({"label": "Dog", "fields": []}) is None
    assert rg.clean_group({"label": "Child", "fields": [{"label": "School", "durability": "weird",
                                                          "gate": True}]})["fields"][0] == {
        "key": "school", "label": "School", "type": "text", "options": [], "required": False,
        "hint": "", "durability": "once", "gate": False, "proof": False,
        "set_by": "customer"}   # gate only means something on expires; proof only alongside a gate
    big = [{"label": f"F{i}"} for i in range(14)]
    assert len(rg.clean_group({"label": "X", "fields": big})["fields"]) == rg.MAX_FIELDS
    assert len(rg.clean_groups([{"label": f"G{i}", "fields": [{"label": "a"}]} for i in range(5)])) == rg.MAX_GROUPS
    assert rg.clean_group({"label": "Pony", "label_plural": "Ponies", "fields": [{"label": "a"}]})["label_plural"] == "Ponies"
    assert rg.clean_group({"label": "Pony", "fields": [{"label": "a"}]})["label_plural"] == "Ponies"


def test_find_group_is_loose_and_the_only_group_needs_no_name():
    dog = {"id": "g-dog", "label": "Dog", "label_plural": "Dogs", "fields": [{"label": "Breed"}]}
    assert rg.find_group([HORSE, dog], "horses")["label"] == "Horse"
    assert rg.find_group([HORSE, dog], "DOG")["label"] == "Dog"
    assert rg.find_group([HORSE, dog], "g-dog")["label"] == "Dog"
    assert rg.find_group([HORSE, dog], "") is None
    assert rg.find_group([HORSE], "")["label"] == "Horse"
    assert rg.find_group([HORSE], "cat")["label"] == "Horse"      # one group: it's that one


# ── values + the gate ────────────────────────────────────────────────────────
def test_normalise_data_matches_labels_loosely_and_drops_placeholders():
    d = rg.normalise_data(HORSE, {"age": "12", "Current Shoeing": "shod all round",
                                  "temperament": "<UNKNOWN>", "Vaccination expiry": "12/10/2026",
                                  "Colour": "bay"})
    assert d == {"age": 12, "current_shoeing": "shod all round", "vaccination_expiry": "2026-10-12"}


def test_missing_for_names_required_gaps_and_lapsed_gated_dates():
    assert rg.missing_for(HORSE, {}) == ["Current shoeing"]
    assert rg.missing_for(HORSE, {"current_shoeing": "TBC"}) == ["Current shoeing"]
    ok = {"current_shoeing": "Barefoot", "vaccination_expiry": "2026-10-12"}
    assert rg.missing_for(HORSE, ok, booking_date="2026-10-01") == []
    assert rg.missing_for(HORSE, ok, booking_date="2026-10-19") == \
        ["Vaccination expiry (expired 2026-10-12, before the booking)"]
    assert rg.missing_for(HORSE, {"current_shoeing": "Barefoot", "vaccination_expiry": "soon"},
                          booking_date="2026-10-01") == ["Vaccination expiry (not a date: soon)"]
    # an optional expiry with no value doesn't gate — nothing to check yet
    assert rg.missing_for(HORSE, {"current_shoeing": "Barefoot"}, booking_date="2026-10-19") == []


def test_merge_honours_durability():
    data, meta, changed = rg.merge({}, {}, {"age": 12, "current_shoeing": "Barefoot"}, HORSE,
                                   source="extracted", email_log_id="e1", now_iso="t0")
    assert data == {"age": 12, "current_shoeing": "Barefoot"} and changed == ["Age", "Current shoeing"]
    assert meta["age"] == {"source": "extracted", "at": "t0", "email_log_id": "e1"}
    # a read never overwrites a once-field already on record; confirm-fields take the newest
    data2, _m, changed2 = rg.merge(data, meta, {"age": 13, "current_shoeing": "Fronts only"}, HORSE,
                                   source="extracted", now_iso="t1")
    assert data2["age"] == 12 and data2["current_shoeing"] == "Fronts only" and changed2 == ["Current shoeing"]
    # the operator can
    data3, _m, _c = rg.merge(data2, {}, {"age": 13}, HORSE, source="owner")
    assert data3["age"] == 13
    # same value: no change recorded
    assert rg.merge(data3, {}, {"age": 13}, HORSE, source="owner")[2] == []


def test_subjects_from_args_reads_the_tool_argument():
    subs, err = rg.subjects_from_args({"subjects": [
        {"name": "Murphy", "group": "horse", "fields": {"Age": "12", "Current shoeing": "Shod all round"}},
        {"name": "Tilly", "fields": {"Current shoeing": "Barefoot"}}]}, [HORSE])
    assert err is None and [s["name"] for s in subs] == ["Murphy", "Tilly"]
    assert subs[0]["data"] == {"age": 12, "current_shoeing": "Shod all round"}
    assert subs[1]["group"]["label"] == "Horse"
    assert rg.subjects_from_args({}, [HORSE]) == ([], None)
    assert rg.subjects_from_args({"subjects": [{"fields": {}}]}, [HORSE])[1].startswith("Every subject needs a `name`")
    dog = {"label": "Dog", "fields": [{"label": "Breed"}]}
    _s, err = rg.subjects_from_args({"subjects": [{"name": "Rex"}]}, [HORSE, dog])
    assert "Set `group` to one of: Horse, Dog" in err
    assert rg.subjects_from_args({"subjects": "Murphy"}, [HORSE])[1] is None   # a bare string is a name
    assert rg.subjects_from_args({"subjects": ["Murphy", "Tilly"]}, [HORSE])[0][1]["name"] == "Tilly"


# ── prompts + tools ──────────────────────────────────────────────────────────
def test_render_for_prompt_is_empty_without_groups_and_speaks_the_tenants_words():
    assert rg.render_for_prompt([]) == ""
    p = rg.render_for_prompt([HORSE])
    assert "Horse (Horses; a customer may have several):" in p
    assert "- Current shoeing (REQUIRED; on record: confirm it still stands" in p
    assert "one of Shod all round, Fronts only, Barefoot" in p
    assert "- Vaccination expiry (optional; an expiry date — must cover the whole booking (right through to its last day" in p
    assert "Never squeeze two into one answer ('Murphy; Tilly')" in p
    assert "dog" not in p.lower()                                   # no industry text


def test_render_known_lists_the_record_and_what_is_still_needed():
    inst = [{"group_id": "g-horse", "name": "Murphy", "data": {"age": 12, "current_shoeing": "Shod all round",
                                                                 "vaccination_expiry": "2026-10-12"}},
            {"group_id": "g-horse", "name": "Tilly", "data": {"age": 6}}]
    k = rg.render_known([HORSE], inst, booking_date="2026-10-19")
    assert "- Horse: Murphy — Age: 12; Current shoeing: Shod all round; Vaccination expiry: 2026-10-12. Still needed: Vaccination expiry (expired 2026-10-12, before the booking)" in k
    assert "- Horse: Tilly — Age: 6. Still needed: Current shoeing" in k
    assert "never re-ask a value shown" in k
    assert rg.render_known([HORSE], []) == ""


def test_with_subjects_gives_the_booking_tools_the_argument_in_the_tenants_words():
    tools = rg.with_subjects(scheduling.SERVICE_TOOLS, [HORSE])
    hold = next(t for t in tools if t["name"] == "hold_booking")
    sub = hold["input_schema"]["properties"]["subjects"]
    assert sub["type"] == "array" and sub["items"]["required"] == ["name"]
    assert "'Age', 'Current shoeing', 'Temperament', 'Vaccination expiry'" in sub["description"]
    assert "NEVER several in one string" in sub["description"]
    assert "subjects" not in next(t for t in scheduling.SERVICE_TOOLS if t["name"] == "hold_booking")["input_schema"]["properties"]
    assert rg.with_subjects(scheduling.SERVICE_TOOLS, []) == list(scheduling.SERVICE_TOOLS)
    assert "subjects" in next(t for t in tools if t["name"] == "offer_slots")["input_schema"]["properties"]


def test_summary_and_instance_line():
    assert rg.summary_of([{"name": "Murphy"}, {"name": "Tilly"}]) == "Murphy, Tilly"
    assert rg.instance_line(HORSE, {"name": "Tilly", "data": {}}) == "Tilly — nothing on record yet"


def test_editor_form_parses_positional_rows_into_groups():
    groups = rg.parse_editor_form(
        g_label=["Horse", ""], g_plural=["", ""], g_cardinality=["many", "one"],
        f_group=["0", "0", "1"], f_label=["Age", "Passport expiry", "Orphan"],
        f_type=["number", "text", "text"], f_required=["0", "1", "0"],
        f_durability=["once", "expires", "once"], f_gate=["0", "1", "0"], f_hint=["", "the date on the card", ""])
    assert len(groups) == 1 and groups[0]["label"] == "Horse"
    assert [f["key"] for f in groups[0]["fields"]] == ["age", "passport_expiry"]
    assert groups[0]["fields"][1] == {"key": "passport_expiry", "label": "Passport expiry", "type": "date",
                                      "options": [], "required": True, "hint": "the date on the card",
                                      "durability": "expires", "gate": True, "proof": False,
                                      "set_by": "customer", "recall_days": 30}


# ── the executor gate ────────────────────────────────────────────────────────
def _wire(monkeypatch, instances=None):
    placed = {"holds": [], "instances": [], "links": []}

    def fake_place_hold(*a, **k):
        placed["holds"].append((a, k))
        return {"id": "b-1", "booking_ref": "BK-1", "booking_date": "2026-09-18", "window": "day"}, None
    monkeypatch.setattr(scheduling, "place_hold", fake_place_hold)
    monkeypatch.setattr(scheduling.db, "crm_contact_by_email", lambda t, e, client=None: {"id": "c-1"})
    monkeypatch.setattr(scheduling.db, "offered_bookings_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "release_offered_for_contact", lambda *a, **k: None)
    monkeypatch.setattr(scheduling.db, "list_requirement_instances",
                        lambda t, cid, client=None: list(instances or []))
    monkeypatch.setattr(scheduling.db, "upsert_requirement_instance",
                        lambda t, gid, cid, name, data, field_meta=None, client=None:
                        placed["instances"].append((name, data)) or {"id": "i-" + name.lower(), "name": name, "data": data})
    monkeypatch.setattr(scheduling.db, "link_booking_subjects",
                        lambda t, bid, ids, client=None: placed["links"].append((bid, list(ids))))
    # A refusal for missing DETAIL reserves the slot while it is gathered
    # (test_reserve_while_gathering.py); the reservation is recorded here.
    placed["offers"] = []
    monkeypatch.setattr(scheduling.db, "hold_booking_if_capacity",
                        lambda tid, d, w, s, **kw: placed["offers"].append((d, kw.get("status")))
                        or {"id": "o-1", "booking_ref": "BK-O1", "booking_date": d, "window": w,
                            "status": kw.get("status")})
    return placed


def test_a_hold_needs_a_subject_when_the_tenant_has_a_group(monkeypatch):
    placed = _wire(monkeypatch)
    execute, state = scheduling.make_executor("t1", now=NOW, contact="kris@example.com", groups=[HORSE])
    out = execute("hold_booking", {"date": "2026-09-18", "window": "day", "summary": "x", "details": {}})
    assert out["error"] == "missing_info" and "Which horse" in out["message"]
    # nothing BOOKED — at most the day is reserved while they say which horse
    # (the reservation itself is pinned in test_reserve_while_gathering.py)
    assert placed["holds"] == [] and all(s == "offered" for _d, s in placed["offers"])
    assert all(b.get("status") == "offered" for b in state["bookings"])


def test_the_gate_names_the_subject_and_the_field(monkeypatch):
    _wire(monkeypatch)
    execute, state = scheduling.make_executor("t1", now=NOW, contact="kris@example.com", groups=[HORSE])
    out = execute("hold_booking", {"date": "2026-09-18", "window": "day", "summary": "x", "details": {},
                                   "subjects": [{"name": "Murphy", "fields": {"Current shoeing": "Shod all round"}},
                                                {"name": "Tilly", "fields": {"Age": "6"}}]})
    assert out["error"] == "missing_info"
    assert out["missing"] == ["Tilly: Current shoeing"]
    assert "Still needed for Tilly: Current shoeing" in out["message"]
    # nothing booked: at most a reservation while the detail is gathered
    assert all(b.get("status") == "offered" for b in state["bookings"])


def test_a_complete_hold_writes_the_instances_and_links_them(monkeypatch):
    placed = _wire(monkeypatch)
    execute, state = scheduling.make_executor("t1", now=NOW, contact="kris@example.com", groups=[HORSE])
    out = execute("hold_booking", {"date": "2026-09-18", "window": "day", "summary": "Shoeing", "details": {},
                                   "subjects": [{"name": "Murphy", "fields": {"Current shoeing": "Shod all round", "Age": 12}},
                                                {"name": "Tilly", "fields": {"Current shoeing": "Barefoot"}}]})
    assert out["held"] is True and out["subjects"] == ["Murphy", "Tilly"]
    assert [n for n, _d in placed["instances"]] == ["Murphy", "Tilly"]
    assert placed["instances"][0][1] == {"current_shoeing": "Shod all round", "age": 12}
    assert placed["links"] == [("b-1", ["i-murphy", "i-tilly"])]
    a, k = placed["holds"][0]
    details = k.get("details") if "details" in k else next(x for x in a if isinstance(x, dict) and "Horses" in x)
    assert details["Horses"] == "Murphy, Tilly"                          # legible on every existing surface


def test_the_record_prefills_so_a_returning_customer_is_not_re_asked(monkeypatch):
    on_record = [{"id": "i-murphy", "group_id": "g-horse", "contact_id": "c-1", "name": "murphy",
                  "data": {"current_shoeing": "Shod all round", "age": 12}, "field_meta": {}}]
    placed = _wire(monkeypatch, instances=on_record)
    execute, state = scheduling.make_executor("t1", now=NOW, contact="kris@example.com", groups=[HORSE])
    out = execute("hold_booking", {"date": "2026-09-18", "window": "day", "summary": "x", "details": {},
                                   "subjects": [{"name": "Murphy"}]})          # nothing new stated
    assert out["held"] is True
    assert placed["instances"] == [("Murphy", {"current_shoeing": "Shod all round", "age": 12})]


def test_a_lapsed_gated_expiry_blocks_only_that_subject(monkeypatch):
    on_record = [{"id": "i-murphy", "group_id": "g-horse", "contact_id": "c-1", "name": "Murphy",
                  "data": {"current_shoeing": "Shod all round", "vaccination_expiry": "2026-09-01"}, "field_meta": {}},
                 {"id": "i-tilly", "group_id": "g-horse", "contact_id": "c-1", "name": "Tilly",
                  "data": {"current_shoeing": "Barefoot"}, "field_meta": {}}]
    _wire(monkeypatch, instances=on_record)
    execute, _ = scheduling.make_executor("t1", now=NOW, contact="kris@example.com", groups=[HORSE])
    out = execute("hold_booking", {"date": "2026-09-18", "window": "day", "summary": "x", "details": {},
                                   "subjects": ["Murphy", "Tilly"]})
    assert out["error"] == "missing_info"
    assert out["missing"] == ["Murphy: Vaccination expiry (expired 2026-09-01, before the booking)"]



def test_words_never_overwrite_a_real_date_on_record(monkeypatch):
    """K1 take 2026-09-22: the card set the jabs to a real date, the model then
    passed "Updated — card received" for that field, and the gate refused the
    booking as "not a date" — asking for a date already on file."""
    on_record = [{"id": "i-murphy", "group_id": "g-horse", "contact_id": "c-1", "name": "Murphy",
                  "data": {"current_shoeing": "Shod all round", "vaccination_expiry": "2027-09-22"},
                  "field_meta": {}}]
    _wire(monkeypatch, instances=on_record)
    execute, state = scheduling.make_executor("t1", now=NOW, contact="kris@example.com", groups=[HORSE])
    out = execute("hold_booking", {"date": "2026-09-18", "window": "day", "summary": "x", "details": {},
                                   "subjects": [{"name": "Murphy", "fields": {
                                       "Vaccination expiry": "Updated — murphy-card.pdf received"}}]})
    assert out.get("held") is True, out


def test_a_new_date_still_wins_over_the_record(monkeypatch):
    on_record = [{"id": "i-murphy", "group_id": "g-horse", "contact_id": "c-1", "name": "Murphy",
                  "data": {"current_shoeing": "Shod all round", "vaccination_expiry": "2027-09-22"},
                  "field_meta": {}}]
    _wire(monkeypatch, instances=on_record)
    execute, _ = scheduling.make_executor("t1", now=NOW, contact="kris@example.com", groups=[HORSE])
    out = execute("hold_booking", {"date": "2026-09-18", "window": "day", "summary": "x", "details": {},
                                   "subjects": [{"name": "Murphy", "fields": {
                                       "Vaccination expiry": "2026-09-01"}}]})
    assert out["error"] == "missing_info" and "expired 2026-09-01" in out["missing"][0]


def test_a_stay_is_refused_when_the_expiry_lapses_part_way_through(monkeypatch):
    """Kris 2026-09-22: the gate read only a stay's arrival, so jabs lapsing on
    the Sunday of a Friday-to-Monday stay went through. The whole stay counts."""
    on_record = [{"id": "i-murphy", "group_id": "g-horse", "contact_id": "c-1", "name": "Murphy",
                  "data": {"current_shoeing": "Shod all round", "vaccination_expiry": "2026-10-25"},
                  "field_meta": {}}]
    placed = _wire(monkeypatch, instances=on_record)
    execute, state = scheduling.make_executor("t1", now=NOW, contact="kris@example.com",
                                              groups=[HORSE], stay=True)
    out = execute("hold_booking", {"arrival": "2026-10-23T08:00", "departure": "2026-10-26T17:00",
                                   "summary": "Stay", "details": {}, "subjects": ["Murphy"]})
    assert out["error"] == "missing_info"
    assert out["missing"] == ["Murphy: Vaccination expiry (lapses 2026-10-25, part-way through "
                              "the booking — it has to last until 2026-10-26)"]
    assert placed["holds"] == [] and state["bookings"] == []

def test_no_groups_means_no_change(monkeypatch):
    monkeypatch.setattr(scheduling, "place_hold", lambda *a, **k: (
        {"id": "b-1", "booking_ref": "BK-1", "booking_date": "2026-09-18", "window": "day"}, None))
    execute, state = scheduling.make_executor("t1", now=NOW)
    out = execute("hold_booking", {"date": "2026-09-18", "window": "day", "summary": "x",
                                   "subjects": [{"name": "stray"}]})
    assert out["held"] is True and "subjects" not in out


# ── the pages ────────────────────────────────────────────────────────────────
@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    import app.dashboard as dashboard
    from app.auth import TenantCtx, require_onboarded
    from app.main import app
    from app.security import require_csrf
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"scheduling"})
    monkeypatch.setattr(dashboard.db, "visible_modules", lambda *a, **k: {"scheduling"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app, follow_redirects=False)
    app.dependency_overrides.clear()


def test_the_editor_renders_the_tenants_kinds_and_saves_them(client, monkeypatch):
    import app.dashboard as dashboard
    stored = {"rows": [dict(HORSE, label_plural="Horses")]}
    monkeypatch.setattr(dashboard.db, "list_requirement_groups", lambda t, client=None: list(stored["rows"]))
    snapshot = {'page':'dependants','groups':stored['rows'],'instances':[], 'definitions':[], 'config': carried_config()}
    monkeypatch.setattr(dashboard.setup_forms,'load',lambda *a: snapshot)
    saved = {}
    monkeypatch.setattr(dashboard.setup_forms, "save",
                        lambda ctx, page, expected, ops: saved.update(groups=[o['fields'] for o in ops if o['table']=='requirement_groups' and o['op']!='delete']))
    r = client.get("/app/diary/dependants")
    assert r.status_code == 200
    assert 'value="Horse"' in r.text and 'value="Current shoeing"' in r.text
    assert "Expires (a date)" in r.text and "must cover the booking" in r.text
    r = client.post("/app/diary/dependants", data={
        "settings_snapshot":json.dumps(snapshot),
        "g_label": ["Dog"], "g_plural": [""], "g_cardinality": ["many"], "g_id": [""],
        "f_group": ["0", "0"], "f_label": ["Breed", "Vaccination expiry"], "f_type": ["text", "date"],
        "f_required": ["1", "0"], "f_durability": ["once", "expires"], "f_gate": ["0", "1"],
        "f_hint": ["", "the date on the card"]})
    assert r.status_code == 303 and r.headers["location"].endswith("dependants=saved")
    assert [g["label"] for g in saved["groups"]] == ["Dog"] and saved["groups"][0]["label_plural"] == "Dogs"
    assert saved["groups"][0]["fields"][1]["gate"] is True


def test_the_job_page_says_what_the_booking_is_about(client, monkeypatch):
    import app.dashboard as dashboard
    booking = {"id": "7b1e0000-0000-4000-8000-000000000001", "contact": "kris@example.com", "summary": "Shoeing",
               "booking_date": "2026-09-18", "window": "am", "status": "confirmed", "booking_ref": "BK-9"}
    monkeypatch.setattr(dashboard.db, "get_booking", lambda *a, **k: dict(booking))
    monkeypatch.setattr(dashboard.db, "list_job_parties", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "list_requirement_groups", lambda t, client=None: [dict(HORSE)])
    monkeypatch.setattr(dashboard.db, "subjects_for_bookings", lambda t, ids, client=None: {
        booking["id"]: [{"id": "i-murphy", "group_id": "g-horse", "name": "Murphy",
                         "data": {"current_shoeing": "Shod all round"}}]})
    monkeypatch.setattr(dashboard.db, "crm_contact_by_email", lambda *a, **k: {"id": "c-1"})
    monkeypatch.setattr(dashboard.db, "list_requirement_instances", lambda *a, **k: [
        {"id": "i-murphy", "group_id": "g-horse", "name": "Murphy", "data": {}},
        {"id": "i-tilly", "group_id": "g-horse", "name": "Tilly", "data": {}}])
    r = client.get(f"/app/diary/{booking['id']}")
    assert r.status_code == 200
    # written out as a record since 2026-09-23 (label then value), not "label: value"
    assert "Murphy" in r.text and ">Current shoeing</dt>" in r.text and ">Shod all round" in r.text
    assert 'option value="i-tilly"' in r.text and 'option value="i-murphy"' not in r.text   # only the unlinked one
    assert "Link to this booking" in r.text

from tests.booking_fixtures import carried_config


@pytest.fixture(autouse=True)
def _established_booking_policy(monkeypatch):
    from app import db
    monkeypatch.setattr(db, "get_scheduling_config", lambda *a, **k: carried_config())
