import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from app import concierge, concierge_routes as routes, setup_card_model as model, setup_cards as cards, setup_card_store as store
from app.auth import require_tenant, TenantCtx
from app.main import app
from tests.test_setup_card_normalise import SHAPES, booking_labels, dependants_labels, diary_ids, edit, proposal
from tests.test_setup_cards import start, service


def assignment(field, value):
    return {"field": field, "value": value, "source": "owner_said", "reference": "Owner's latest answer"}


def delta(revision=0, op="append", id="", fields=None):
    return [{"page": "diary_settings", "revision": revision, "changes": [{
        "op": op, "id": id, "name": "service" if op == "append" else "", "held": None,
        "fields": fields if fields is not None else [assignment("name", "Review"), assignment("minutes", 45), assignment("timing", "time")]}]}]


def test_delta_replay_keeps_unmentioned_flags_sources_and_line_ids():
    entry = model.unpack(delta())[0]
    one = cards.change(entry["page"], None, 0, "turn-1", entry["changes"])
    id = one["lines"][0]["id"]
    change = model.unpack(delta(1, "set", id, [assignment("minutes", 90)]))[0]
    two = cards.change(change["page"], one, 1, "turn-2", change["changes"])
    assert two["lines"][0]["fields"] == {"name": "Review", "minutes": 90, "timing": "time"}
    assert two["lines"][0]["id"] == id
    remove = model.unpack(delta(2, "remove", id, []))[0]
    three = cards.change(remove["page"], two, 2, "turn-3", remove["changes"])
    assert three["lines"][0]["status"] == "left"
    assert one["lines"][0]["fields"]["minutes"] == 45


@pytest.mark.parametrize("change", [
    lambda d: d[0]["changes"][0]["fields"].append(assignment("minutes", 60)),
    lambda d: d[0]["changes"][0]["fields"][0].pop("source"),
    lambda d: d[0]["changes"][0].update(id="invented"),
    lambda d: d.append(deepcopy(d[0])),
])
def test_malformed_model_change_never_loses_a_field_silently(change):
    raw = delta(); change(raw)
    with pytest.raises(cards.Invalid):
        model.unpack(raw)


def test_schema_and_model_call_use_durable_contract_only_when_requested(monkeypatch):
    from app import settings_guide
    def forbidden_recipe(*args, **kwargs):
        raise AssertionError("Durable setup must not read a trade recipe")
    monkeypatch.setattr(settings_guide, "guide_for", forbidden_recipe)
    captured = []
    async def retrieve(*a, **kw): return []
    async def create(**kw):
        captured.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps({"reply": "Review", "setup_changes": delta()}))], usage=None, model="test")
    monkeypatch.setattr(concierge.kb, "retrieve", retrieve)
    monkeypatch.setattr(concierge, "client", lambda: object())
    monkeypatch.setattr(concierge.llm, "create", create)
    context = model.context({"diary_settings": start(service())}, "/app/diary/settings")
    result = asyncio.run(concierge.answer_help("Change the duration", tenant={"setup_card_context": context}))
    assert result.setup_changes == delta()
    assert captured[0]["system"][-1]["text"] == context
    assert "setup_changes" in captured[0]["output_config"]["format"]["schema"]["required"]
    assert "setup_changes" not in concierge.HELP_SCHEMA["properties"]


@pytest.fixture
def route_client(monkeypatch):
    from app import security
    monkeypatch.setattr(security, '_BUCKETS', {})
    state = {"cards": {}, "changes": delta(), "saves": [], "contexts": []}
    app.dependency_overrides[require_tenant] = lambda: TenantCtx(tenant_id="owner", client=None, user_id="owner")
    monkeypatch.setattr(routes.db, "get_member", lambda *a: None)
    monkeypatch.setattr(routes, "_tenant_context", lambda ctx: {})
    monkeypatch.setattr(routes.usage, "record_anthropic", lambda *a, **kw: None)
    monkeypatch.setattr(routes.setup_handoff, "load_steps", lambda *a: [])
    monkeypatch.setattr(store, "load_all", lambda tid, **kw: deepcopy(state["cards"]))
    monkeypatch.setattr(routes.db, "get_scheduling_config", lambda *a, **kw: state.get("cfg", {}))
    monkeypatch.setattr(routes.db, "mark_dependants_configured", lambda *a, **kw: None)
    def save(tid, page, revision, card, **kw):
        assert tid == "owner"
        if state.get("stale"): raise cards.Stale("changed")
        state["saves"].append(deepcopy(card)); state["cards"][page] = deepcopy(card)
        return card
    monkeypatch.setattr(store, "save", save)
    async def answer(*a, **kw):
        state["contexts"].append(kw["tenant"].get("setup_card_context", ""))
        return concierge.HelpReply("I saved all your settings!", None, None, state.get("recommend"), None, "test", setup_changes=state["changes"])
    monkeypatch.setattr(concierge, "answer_help", answer)
    c = TestClient(app); c.cookies.set("bl_csrf", "token"); c.headers["x-csrf-token"] = "token"
    try: yield c, state
    finally: app.dependency_overrides.clear(); c.close()


def ask(c):
    return c.post("/concierge/help", json={"question": "Please correct the duration", "card_mode": True, "path": "/app/diary/settings"})


def test_help_persists_model_changes_and_replaces_false_success_prose(route_client):
    c, state = route_client
    first = ask(c)
    assert first.status_code == 200 and len(state["saves"]) == 1
    assert "I saved" not in first.json()["reply"] and "Nothing" in first.json()["reply"]
    id = state["cards"]["diary_settings"]["lines"][0]["id"]
    state["changes"] = delta(1, "set", id, [assignment("minutes", 90)])
    second = ask(c)
    assert second.json()["setup_cards"][0]["lines"][0]["fields"]["timing"] == "time"
    assert id in state["contexts"][-1]
    assert state["cards"]["diary_settings"]["revision"] == 2


def test_help_rejects_stale_changes_and_unsourced_legacy_rows(route_client):
    c, state = route_client
    state["stale"] = True
    state["recommend"] = [{"page": "diary_settings", "values": [{"name": "service", "value": "Review | 45 | | time"}]}]
    result = ask(c).json()
    assert result["recommend"] is None and result["setup_cards"] == []
    assert len(result["card_errors"]) == 2 and not state["saves"]
    assert "saved all" not in result["reply"]


def test_help_card_mode_requires_csrf_before_model_work(route_client):
    c, state = route_client
    c.headers.pop("x-csrf-token")
    assert ask(c).status_code == 403
    assert not state["contexts"] and not state["saves"]


@pytest.mark.parametrize("page,name,fields", [
    ("booking_details", "field", {"label": "Evidence", "required": False, "kind": "document", "services": ["Review"]}),
    ("dependants", "field", {"kind": "Asset", "label": "Expiry", "type": "date", "required": True,
                              "durability": "expires", "recall_days": 40, "gate": True}),
])
def test_detail_model_corrections_keep_native_choices_and_sources(route_client, page, name, fields):
    c, state = route_client
    state["changes"] = delta(fields=[assignment(k, v) for k, v in fields.items()])
    state["changes"][0]["page"] = page
    state["changes"][0]["changes"][0]["name"] = name
    first = ask(c)
    assert first.status_code == 200 and not first.json()["card_errors"]
    original = deepcopy(state["cards"][page]["lines"][0])
    state["changes"] = delta(1, "set", original["id"], [assignment("hint", "Revised")])
    state["changes"][0]["page"] = page
    second = ask(c)
    assert second.status_code == 200 and not second.json()["card_errors"]
    corrected = state["cards"][page]["lines"][0]
    assert corrected["fields"] == fields | {"hint": "Revised"}
    for key in fields:
        assert corrected["sources"][key] == original["sources"][key]


def test_stay_diary_uses_sourced_deltas_and_verified_mode(route_client):
    legacy = [{"page": "booking_details", "values": [{"name": "field", "value": "Detail | required"}]}]
    assert model.without_legacy_rows(legacy) == (None, True)
    c, state = route_client
    state["cfg"] = {"booking_mode": "stay"}
    state["changes"] = delta(fields=[assignment("name", "Review"), assignment("minutes", 45),
                                    assignment("timing", "time"), assignment("at_once", 2)])
    state["recommend"] = [{"page": "diary_settings", "values": [{"name": "service", "value": "Review | 45 | | time"}]}]
    r = ask(c)
    assert r.status_code == 200 and r.json()["recommend"] is None
    assert json.loads(state["contexts"][0].split(model.RULE, 1)[1])["stay_diary"] is True
    assert state["saves"][0]["lines"][0]["fields"]["at_once"] == 2
    line = state["cards"]["diary_settings"]["lines"][0]
    state["changes"] = delta(1, "set", line["id"], [assignment("minutes", 60)])
    corrected = ask(c).json()["setup_cards"][0]["lines"][0]
    assert corrected["fields"]["at_once"] == 2
    assert corrected["sources"]["at_once"] == line["sources"]["at_once"]


def test_scalar_deltas_replace_only_migrated_legacy_fields_and_keep_zero(route_client):
    c, state = route_client
    state["changes"] = delta(fields=[assignment("key", "buffer_minutes"), assignment("value", 0)])
    state["changes"][0]["changes"][0]["name"] = "setting"
    state["recommend"] = [{"page": "diary_settings", "values": [
        {"name": "buffer_minutes", "value": "30"}, {"name": "waitlist_stage", "value": "Waiting"},
        {"name": "service_urgent", "value": "Review | 30"}]}]
    reply = ask(c).json()
    assert reply["setup_cards"][0]["lines"][0]["fields"] == {"key": "buffer_minutes", "value": 0}
    assert reply["recommend"] is None  # Same-day rules now use sourced deltas too.
    assert '"catalogue"' in state["contexts"][0]


@pytest.mark.parametrize('page,key,value', [('invoice_settings', 'payment_terms', 'Due on Receipt.'), ('packages', 'package_low_at', 0)])
def test_invoice_scalar_model_deltas_preserve_values_and_suppress_legacy_packages(route_client, page, key, value):
    c, state = route_client
    state['changes'] = delta(fields=[assignment('key', key), assignment('value', value)])
    state['changes'][0]['page'] = page
    state['changes'][0]['changes'][0]['name'] = 'setting'
    state['recommend'] = [{'page': page, 'values': [{'name': key, 'value': 'old'}]},
                          {'page': 'packages', 'values': [{'name': 'package', 'value': 'Pass | 5 | session | 50 | 30 |'}]}]
    response = ask(c)
    assert response.status_code == 200
    reply = response.json()
    assert reply['setup_cards'][0]['lines'][0]['fields'] == {'key': key, 'value': value}
    assert reply['recommend'] is None


def test_package_model_correction_preserves_unmentioned_choices(route_client):
    from tests.test_setup_card_packages import package
    c, state = route_client
    state['changes'] = delta(fields=[assignment(k, v) for k, v in package().items()])
    state['changes'][0]['page'] = 'packages'
    state['changes'][0]['changes'][0]['name'] = 'package'
    reply = ask(c).json()
    line = reply['setup_cards'][0]['lines'][0]
    state['changes'] = delta(1, 'set', line['id'], [assignment('price', 50)])
    state['changes'][0]['page'] = 'packages'
    corrected = ask(c).json()['setup_cards'][0]['lines'][0]
    assert corrected['fields'] == package(price=50)
    assert corrected['sources']['services'] == line['sources']['services']


def test_crm_stage_deltas_preserve_type_and_suppress_legacy_recommendations(route_client):
    c, state = route_client
    fields = dict(name='Waiting', stage_type='active', display_order=1)
    state['changes'] = delta(fields=[assignment(k, v) for k, v in fields.items()])
    state['changes'][0]['page'] = 'crm_settings'
    state['changes'][0]['changes'][0]['name'] = 'stage'
    state['recommend'] = [{'page': 'crm_settings', 'values': [{'name': 'stage', 'value': 'Waiting | active'}, {'name': 'quiet_days', 'value': '14'}]}]
    reply = ask(c).json()
    assert reply['recommend'] is None
    line = reply['setup_cards'][0]['lines'][0]
    state['changes'] = delta(1, 'set', line['id'], [assignment('display_order', 2)])
    state['changes'][0]['page'] = 'crm_settings'
    revised = ask(c).json()['setup_cards'][0]['lines'][0]
    assert revised['fields'] == fields | {'display_order': 2}
    assert revised['sources']['stage_type'] == line['sources']['stage_type']


def test_service_exception_deltas_keep_zero_notice_and_suppress_legacy_rows(route_client):
    c, state = route_client
    fields = {'name': 'Review', 'enabled': True, 'same_day_until': '14:00', 'notice_hours': 0}
    state['changes'] = delta(fields=[assignment(k, v) for k, v in fields.items()])
    state['changes'][0]['changes'][0]['name'] = 'service_urgent'
    state['recommend'] = [{'page': 'diary_settings', 'values': [{'name': 'service_urgent', 'value': 'Review | 12:00 | 2'}]}]
    reply = ask(c).json()
    assert reply['recommend'] is None
    assert reply['setup_cards'][0]['lines'][0]['fields'] == fields


def test_ladder_deltas_keep_order_and_explicit_empty_gates(route_client):
    c, state = route_client
    fields = {'who': 'contact', 'label': 'Progress', 'levels': ['Stage 1', 'Stage 2'],
              'notify': 'off', 'gates': [], 'staff_can_see': False}
    state['changes'] = delta(fields=[assignment(k, v) for k, v in fields.items()])
    state['changes'][0]['page'] = 'dependants'
    state['changes'][0]['changes'][0]['name'] = 'ladder'
    state['recommend'] = [{'page': 'dependants', 'values': [{'name': 'ladder', 'value': 'customer | Progress | A, B | auto |'}]}]
    reply = ask(c).json()
    assert reply['recommend'] is None
    assert reply['setup_cards'][0]['lines'][0]['fields'] == fields


def test_knowledge_deltas_preserve_prose_and_suppress_legacy_note(route_client):
    c, state = route_client
    fields = {'label': 'Hours', 'content': 'We open at 9.\n\nA | B.'}
    state['changes'] = delta(fields=[assignment(k, v) for k, v in fields.items()])
    state['changes'][0]['page'] = 'knowledge'
    state['changes'][0]['changes'][0]['name'] = 'note'
    state['recommend'] = [{'page': 'knowledge', 'values': [{'name': 'note', 'value': 'Hours | Old'}]}]
    reply = ask(c).json()
    assert reply['recommend'] is None
    assert reply['setup_cards'][0]['lines'][0]['fields'] == fields


def test_booking_mode_native_false_suppresses_legacy_choice(route_client):
    c, state = route_client
    state['changes'] = delta(fields=[assignment('key', 'stay_mode'), assignment('value', False)])
    state['changes'][0]['page'] = 'diary_settings'
    state['changes'][0]['changes'][0]['name'] = 'setting'
    state['recommend'] = [{'page': 'diary_settings', 'values': [{'name': 'stay_mode', 'value': '1'}]}]
    reply = ask(c).json()
    assert reply['recommend'] is None
    assert reply['setup_cards'][0]['lines'][0]['fields'] == {'key': 'stay_mode', 'value': False}


def test_customer_field_deltas_keep_expiry_and_visibility_choices(route_client):
    from tests.test_setup_card_fields import field
    c, state = route_client
    values = field(field_type='date', durability='expires', recall_days=60, valid_months=24)
    state['changes'] = delta(fields=[assignment(k, v) for k, v in values.items()])
    state['changes'][0]['page'] = 'crm_fields'
    state['changes'][0]['changes'][0]['name'] = 'contact_field'
    state['recommend'] = [{'page': 'crm_fields', 'values': [{'name': 'contact_field', 'value': 'Reference | date | expires 30 |'}]}]
    reply = ask(c).json()
    assert reply['recommend'] is None
    line = reply['setup_cards'][0]['lines'][0]
    state['changes'] = delta(1, 'set', line['id'], [assignment('valid_months', 0)])
    state['changes'][0]['page'] = 'crm_fields'
    revised = ask(c).json()['setup_cards'][0]['lines'][0]
    assert revised['fields'] == values | {'valid_months': 0}
    assert revised['sources']['staff_can_see'] == line['sources']['staff_can_see']


def test_help_persists_considered_only_turn_without_claiming_settings_saved(route_client):
    c, state = route_client
    state["changes"] = [{"page": "diary_settings", "revision": 0, "changes": [],
        "considered": [{"key": "diary_settings/buffer_minutes", "state": "needs_answer"}]}]
    response = ask(c)
    assert response.status_code == 200 and not response.json()["card_errors"]
    assert len(state["saves"]) == 1
    assert state["cards"]["diary_settings"]["lines"] == []
    assert state["cards"]["diary_settings"]["considered"][0]["source_turn"]
    assert "Nothing from this reply has been applied" in response.json()["reply"]
    assert response.json()["closed_step"] is None
    state["changes"][0]["revision"] = 0
    assert ask(c).json()["card_errors"]
    assert len(state["saves"]) == 1


def ask_on(c, path):
    return c.post("/concierge/help", json={"question": "Here are my answers", "card_mode": True, "path": path})


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_help_saves_each_known_first_proposal_shape(route_client, shape):
    c, state = route_client
    state["changes"] = SHAPES[shape]()
    reply = ask_on(c, "/app/diary/settings").json()
    assert reply["card_errors"] == []
    assert [card["page"] for card in reply["setup_cards"]] == [entry["page"] for entry in state["changes"]]
    assert all(line["status"] == "proposed" and line["problems"] == [] for card in reply["setup_cards"] for line in card["lines"])
    assert [card["page"] for card in state["saves"]] == [entry["page"] for entry in state["changes"]]


def test_help_saves_the_valid_page_and_names_the_rejected_one(route_client, monkeypatch):
    c, state = route_client
    monkeypatch.setattr(routes.setup_handoff, "load_steps", lambda *a: [
        {"key": "diary", "done": True, "href": "/app/diary/settings"},
        {"key": "dependants", "done": False, "href": "/app/diary/dependants"}])
    state["changes"] = dependants_labels() + diary_ids()
    reply = ask_on(c, "/app/diary/dependants").json()
    assert reply["card_errors"] == ["Diary settings is already set up; that proposal was not kept."]
    assert [card["page"] for card in reply["setup_cards"]] == ["dependants"]
    assert list(state["cards"]) == ["dependants"] and len(state["saves"]) == 1
    assert reply["reply"].startswith("Updated setup records: Dependants.")
    assert reply["reply"].endswith("Diary settings is already set up; that proposal was not kept.")


def test_help_names_the_page_and_reason_for_an_invalid_change(route_client):
    c, state = route_client
    state["changes"] = dependants_labels() + [proposal("diary_settings",
        edit("set", "diary_settings/timezone", "", {"value": "Europe/London"}),
        edit("set", "mystery", "", {"value": 1}))]
    reply = ask_on(c, "/app/diary/dependants").json()
    assert reply["card_errors"] == ["Diary settings: Choose a pending line from this card. (set mystery)"]
    assert [card["page"] for card in reply["setup_cards"]] == ["dependants"]
    assert list(state["cards"]) == ["dependants"]
    assert "incomplete or invalid" not in json.dumps(reply)
    state["changes"] = [proposal("booking_details", edit("append", "", "Photos", {"colour": "blue"}))]
    reply = ask_on(c, "/app/diary/intake").json()
    assert reply["card_errors"] == ["Booking details: Append requires a supported row name and no existing ID. (append Photos)"]
    assert "booking_details" not in state["cards"]


def test_help_says_so_when_the_whole_list_is_unreadable(route_client):
    c, state = route_client
    state["changes"] = [proposal("diary_settings", edit("set", "line-1", "stage", {"value": 1}))]
    reply = ask_on(c, "/app/diary/settings").json()
    assert reply["card_errors"] == ["The proposed changes were incomplete or invalid. Existing cards were kept; please ask again."]
    assert not state["saves"]


def test_help_reply_mentions_normalised_settings(route_client):
    c, state = route_client
    state["changes"] = diary_ids()
    reply = ask_on(c, "/app/diary/settings").json()
    assert reply["card_errors"] == []
    assert reply["reply"] == ("Updated setup records: Diary settings. Review proposals in Suggested or Held and considered notes "
                              "in the page record. Diary settings: 5 values proposed as new settings. "
                              "Nothing from this reply has been applied to your settings.")
    assert "I saved" not in reply["reply"]


def test_help_mentions_a_rewrite_only_for_a_page_that_saved(route_client):
    c, state = route_client
    state["changes"] = diary_ids() + booking_labels()
    state["stale"] = True
    reply = ask_on(c, "/app/diary/settings").json()
    assert reply["setup_cards"] == [] and len(reply["card_errors"]) == 2
    assert "proposed as new settings" not in reply["reply"] and "sorted into" not in reply["reply"]


def test_unpack_keeps_evidence_only_for_inferred_values():
    fields = [assignment("name", "Review"), assignment("minutes", 45),
              {"field": "timing", "value": "time", "source": "inferred", "reference": "Usual setting", "evidence": []},
              ]
    fields[0] = dict(fields[0], evidence=[])
    entry = model.unpack(delta(fields=fields))[0]
    sources = entry["changes"][0]["sources"]
    assert sources["name"] == {"kind": "owner_said", "reference": "Owner's latest answer"}
    assert sources["timing"] == {"kind": "inferred", "reference": "Usual setting", "evidence": []}
    schema_source = model.ASSIGNMENT["properties"]["source"]["enum"]
    assert "inferred" in schema_source and "evidence" in model.ASSIGNMENT["required"]
