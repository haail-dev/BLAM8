from copy import deepcopy
import pytest
from fastapi.testclient import TestClient
from app import db, setup_card_store as store, setup_cards as cards
from app.auth import require_onboarded, TenantCtx
from app.main import app
from tests.test_setup_cards import start, service


@pytest.fixture
def client(monkeypatch):
    from app import security
    monkeypatch.setattr(security, '_BUCKETS', {})
    state = {"card": start(service()), "cfg": {"service_types": [], "resource_types": []}, "saves": []}
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="owner", client=None)
    def load(tid, page, **kw):
        assert tid == "owner"
        return deepcopy(state["card"])
    def save(tid, page, revision, card, **kw):
        assert tid == "owner"
        state["saves"].append(kw)
        state["card"] = deepcopy(card)
        return card
    monkeypatch.setattr(store, "load", load)
    monkeypatch.setattr(store, "load_all", lambda tid, **kw: {"diary_settings": deepcopy(state["card"])})
    monkeypatch.setattr(store, "save", save)
    monkeypatch.setattr(db, "get_scheduling_config", lambda *a, **kw: deepcopy(state["cfg"]))
    monkeypatch.setattr(db, "visible_modules", lambda *a, **kw: {"scheduling"})
    monkeypatch.setattr(db, "list_requirement_groups", lambda *a, **kw: [])
    c = TestClient(app)
    try:
        c.cookies.set("bl_csrf", "token")
        c.headers["x-csrf-token"] = "token"
        yield c, state
    finally:
        c.close()
        app.dependency_overrides.clear()


BASE = "/concierge/setup-cards/diary_settings"


def test_customer_progress_rechecks_crm_permission_before_writing(client, monkeypatch):
    from app import dashboard
    from fastapi import HTTPException
    from tests.test_setup_card_ladders import row
    c, state = client
    state['card'] = start(row(), page='dependants')
    monkeypatch.setattr(db, 'crm_list_field_definitions', lambda *a, **kw: [])
    base = '/concierge/setup-cards/dependants'
    body = selection(state)
    assert c.post(base + '/preview', json=body).status_code == 404
    monkeypatch.setattr(db, 'visible_modules', lambda *a, **kw: {'crm'})
    preview = c.post(base + '/preview', json=body).json()
    body['settings_version'] = preview['settings_version']
    def denied(*a): raise HTTPException(403, 'Read only')
    monkeypatch.setattr(dashboard, 'require_crm_write', denied)
    assert c.post(base + '/accept', json=body).status_code == 403
    assert not state['saves']
    monkeypatch.setattr(dashboard, 'require_crm_write', lambda *a: None)
    assert c.post(base + '/accept', json=body).status_code == 200
    assert state['saves'][0]['settings_patch']['definitions'][0]['ladder']['notify'] == 'approve'


def test_knowledge_embedding_failure_keeps_card_pending_and_success_uses_atomic_patch(client, monkeypatch):
    from app import setup_card_knowledge as notes, usage
    from tests.test_setup_card_knowledge import proposal
    c, state = client
    state['card'] = proposal()
    monkeypatch.setattr(notes, 'snapshot', lambda *a: {'sources': [{'label': 'Hours', 'chunks': []}]})
    monkeypatch.setattr(notes.kb, 'is_configured', lambda: True)
    monkeypatch.setattr(usage, 'record_voyage', lambda *a, **kw: None)
    async def fail(texts): raise RuntimeError('Unavailable')
    monkeypatch.setattr(notes.embeddings, 'embed_documents', fail)
    base = '/concierge/setup-cards/knowledge'
    body = selection(state)
    preview = c.post(base + '/preview', json=body).json()
    body['settings_version'] = preview['settings_version']
    assert c.post(base + '/accept', json=body).status_code == 503
    assert not state['saves'] and state['card']['lines'][0]['status'] == 'proposed'
    async def embed(texts): return [[0.1]*1024 for _ in texts], 1
    monkeypatch.setattr(notes.embeddings, 'embed_documents', embed)
    assert c.post(base + '/accept', json=body).status_code == 200
    patch = state['saves'][0]['settings_patch']
    assert patch['notes'][0]['chunks'][0]['content'] == 'We open at 9.'
    assert 'embedding' not in str(state['card'])


def test_mode_acceptance_rechecks_booking_history_and_allows_email_only(client, monkeypatch):
    from tests.test_setup_card_mode import mode
    c, state = client
    history = {'exists': False}
    state['card'] = start(mode(True), page='booking_details')
    monkeypatch.setattr(db, 'visible_modules', lambda *a, **kw: {'email'})
    monkeypatch.setattr(db, 'crm_list_field_definitions', lambda *a, **kw: [])
    monkeypatch.setattr(db, 'setup_has_booking_history', lambda *a, **kw: history['exists'])
    base = '/concierge/setup-cards/booking_details'
    body = selection(state)
    preview = c.post(base + '/preview', json=body).json()
    history['exists'] = True
    assert c.post(base + '/accept', json=body | {'settings_version': preview['settings_version']}).status_code == 409
    assert c.post(base + '/preview', json=body).status_code == 422
    assert not state['saves']
    history['exists'] = False
    assert c.post(base + '/accept', json=body | {'settings_version': preview['settings_version']}).status_code == 200
    assert state['saves'][0]['settings_patch']['booking_mode'] == 'stay'


def test_customer_field_acceptance_rechecks_definitions_and_edit_permission(client, monkeypatch):
    from fastapi import HTTPException
    from app import dashboard
    from tests.test_setup_card_fields import proposal, field
    c, state = client
    state['card'] = proposal(field())
    definitions = []
    monkeypatch.setattr(db, 'visible_modules', lambda *a, **kw: {'crm'})
    monkeypatch.setattr(db, 'crm_list_field_definitions', lambda *a, **kw: deepcopy(definitions))
    body = selection(state)
    base = '/concierge/setup-cards/crm_fields'
    preview = c.post(base + '/preview', json=body).json()
    def denied(*a, **kw): raise HTTPException(403, 'Read only')
    with monkeypatch.context() as scoped:
        scoped.setattr(dashboard, 'require_crm_write', denied)
        assert c.post(base + '/accept', json=body | {'settings_version': preview['settings_version']}).status_code == 403
    assert not state['saves']
    definitions.append({'id': 'other', 'key': 'other', 'label': 'Other'})
    assert c.post(base + '/accept', json=body | {'settings_version': preview['settings_version']}).status_code == 409
    definitions.clear()
    assert c.post(base + '/accept', json=body | {'settings_version': preview['settings_version']}).status_code == 200
    assert state['saves'][0]['settings_patch']['definitions'][0]['key'] == 'reference'


def test_crm_acceptance_requires_module_and_rechecks_threshold_and_stages(client, monkeypatch):
    from tests.test_setup_card_crm import stage
    c, state = client
    state['card'] = start(stage(), page='crm_settings')
    config = {'crm_quiet_days': 14}
    stages = []
    monkeypatch.setattr(db, 'get_tenant_config', lambda *a, **kw: deepcopy(config))
    monkeypatch.setattr(db, 'crm_list_stages', lambda *a, **kw: deepcopy(stages))
    monkeypatch.setattr(store, 'load_all', lambda *a, **kw: {'crm_settings': deepcopy(state['card'])})
    base = '/concierge/setup-cards/crm_settings'
    body = selection(state)
    assert c.post(base + '/preview', json=body).status_code == 404
    monkeypatch.setattr(db, 'visible_modules', lambda *a, **kw: {'crm'})
    assert c.get('/concierge/setup-cards').status_code == 200
    preview = c.post(base + '/preview', json=body).json()
    config['crm_quiet_days'] = 7
    assert c.post(base + '/accept', json=body | {'settings_version': preview['settings_version']}).status_code == 409
    preview = c.post(base + '/preview', json=body).json()
    stages.append(dict(id='existing', name='Waiting', stage_type='active', display_order=0))
    assert c.post(base + '/accept', json=body | {'settings_version': preview['settings_version']}).status_code == 409
    preview = c.post(base + '/preview', json=body).json()
    assert c.post(base + '/accept', json=body | {'settings_version': preview['settings_version']}).status_code == 200
    assert state['saves'][0]['settings_patch']['stages'][0]['id'] == 'existing'


def test_package_acceptance_checks_saved_service_snapshot(client, monkeypatch):
    from tests.test_setup_card_packages import package, PREFS
    from tests.test_setup_cards import append
    c, state = client
    invoice = {'extras': {'packages': [], 'package_prefs': PREFS}}
    monkeypatch.setattr(db, 'get_invoice_settings', lambda *a, **kw: deepcopy(invoice))
    state['cfg'] = {'service_types': [{'name': 'Review'}]}
    state['card'] = start(append('package', **package(services=['Review'])), page='packages')
    base = '/concierge/setup-cards/packages'
    body = selection(state)
    preview = c.post(base + '/preview', json=body)
    assert preview.status_code == 200
    state['cfg']['service_types'][0]['name'] = 'Changed'
    assert c.post(base + '/accept', json=body | {'settings_version': preview.json()['settings_version']}).status_code == 409
    assert c.post(base + '/preview', json=body).status_code == 422
    assert not state['saves']
    state['cfg']['service_types'][0]['name'] = 'Review'
    assert c.post(base + '/accept', json=body | {'settings_version': preview.json()['settings_version']}).status_code == 200
    assert state['saves'][0]['expected_settings']['_setup_scheduling'] == state['cfg']
    assert state['saves'][0]['settings_patch']['extras']['packages'][0]['key'] == 'pass'


@pytest.mark.parametrize('page,key,value', [('invoice_settings', 'footer_note', 'Owner text'), ('packages', 'package_low_at', 0)])
def test_invoice_snapshot_guards_other_page_changes_without_scheduling(client, monkeypatch, page, key, value):
    from tests.test_setup_card_scalars import setting
    c, state = client
    invoice = {'extras': {'package_prefs': {'paid_only': False, 'portal': True, 'low_at': 2, 'expiry_days': 7}}}
    monkeypatch.setattr(db, 'get_invoice_settings', lambda *a, **kw: deepcopy(invoice))
    monkeypatch.setattr(db, 'visible_modules', lambda *a, **kw: {'email'})
    state['card'] = start(setting(key, value), page=page)
    body = selection(state)
    base = '/concierge/setup-cards/' + page
    preview = c.post(base + '/preview', json=body)
    assert preview.status_code == 200
    invoice['extras']['other'] = 'changed'
    assert c.post(base + '/accept', json=body | {'settings_version': preview.json()['settings_version']}).status_code == 409
    assert not state['saves']
    preview = c.post(base + '/preview', json=body).json()
    assert c.post(base + '/accept', json=body | {'settings_version': preview['settings_version']}).status_code == 200
    assert state['saves'][0]['expected_settings'] == invoice


def test_stage_link_preview_options_and_renamed_stage_are_checked(client, monkeypatch):
    from tests.test_setup_card_scalars import setting
    c, state = client
    stages = [{"id": "a", "name": "Waiting"}]
    monkeypatch.setattr(db, "crm_list_stages", lambda *a, **kw: deepcopy(stages))
    state["card"] = start(setting("waitlist_stage", "waiting"))
    body = selection(state)
    read = c.get("/concierge/setup-cards").json()
    assert read["options"]["settings"]["waitlist_stage"]["choices"] == ["Waiting"]
    preview = c.post(BASE + "/preview", json=body).json()
    stages[0]["name"] = "Queued"
    assert c.post(BASE + "/accept", json=body | {"settings_version": preview["settings_version"]}).status_code == 409
    assert not state["saves"]
    stages[0]["name"] = "Waiting"
    assert c.post(BASE + "/accept", json=body | {"settings_version": preview["settings_version"]}).status_code == 200
    saved = state["saves"][0]
    assert saved["settings_patch"] == {"courses": {"waitlist_stage": "Waiting"}}
    assert saved["expected_settings"]["_setup_stages"] == stages


def selection(state):
    return {"revision": state["card"]["revision"], "ids": [state["card"]["lines"][0]["id"]]}


@pytest.mark.parametrize("page", ["booking_details", "dependants"])
def test_details_acceptance_without_scheduling_and_related_read_failure(client, monkeypatch, page):
    from tests.test_setup_cards import append
    c, state = client
    monkeypatch.setattr(db, "visible_modules", lambda *a, **kw: {"email"})
    monkeypatch.setattr(db, "crm_list_field_definitions", lambda *a, **kw: [])
    if page == "booking_details":
        state["card"] = start(append("field", label="Reference", required=False, remember="once"), page=page)
    else:
        state["card"] = start(append("group", label="Asset", plural="Assets", cardinality="many"),
            append("field", kind="Asset", label="Code", type="text", required=False, durability="once", gate=False), page=page)
    body = {"revision": 1, "ids": [r["id"] for r in state["card"]["lines"]]}
    base = "/concierge/setup-cards/" + page
    preview = c.post(base + "/preview", json=body)
    assert preview.status_code == 200
    def unavailable(*a, **kw):
        raise RuntimeError("offline")
    reader = "crm_list_field_definitions" if page == "booking_details" else "list_requirement_groups"
    with monkeypatch.context() as scoped:
        scoped.setattr(db, reader, unavailable)
        assert c.post(base + "/accept", json=body | {"settings_version": preview.json()["settings_version"]}).status_code == 503
        assert not state["saves"]
    accepted = c.post(base + "/accept", json=body | {"settings_version": preview.json()["settings_version"]})
    assert accepted.status_code == 200
    assert state["saves"][0]["expected_settings"]["config"] == state["cfg"]
    assert accepted.json()["card"]["status"] == "applied"


def test_preview_is_read_only_accept_has_server_computed_receipt(client):
    c, state = client
    body = selection(state)
    preview = c.post(BASE + "/preview", json=body)
    assert preview.status_code == 200 and not state["saves"]
    result = c.post(BASE + "/accept", json=body | {"settings_version": preview.json()["settings_version"]})
    assert result.status_code == 200
    assert result.json()["card"]["status"] == "applied"
    assert state["saves"][0]["expected_settings"] == state["cfg"]
    assert state["saves"][0]["settings_patch"]["service_types"][0]["minutes"] == 45
    assert c.post(BASE + "/accept", json=body | {"settings_version": preview.json()["settings_version"]}).status_code == 409


def test_saved_settings_changed_after_preview_requires_new_review(client):
    c, state = client
    body = selection(state)
    preview = c.post(BASE + "/preview", json=body).json()
    state["cfg"]["buffer_minutes"] = 15
    r = c.post(BASE + "/accept", json=body | {"settings_version": preview["settings_version"]})
    assert r.status_code == 409 and not state["saves"]


def test_stay_appointment_preview_exposes_capacity_and_saves_both_catalogues(client):
    c, state = client
    state["cfg"] = {"booking_mode": "stay", "capacity_per_window": 20}
    state["card"] = start(service(at_once=3))
    body = selection(state)
    read = c.get("/concierge/setup-cards").json()
    assert read["views"]["diary_settings"]["stay"] is True
    assert "Stays" in read["options"]["services"]
    preview = c.post(BASE + "/preview", json=body).json()
    assert preview["changes"][0]["fields"]["at_once"] == 3
    assert "3 at once" in preview["changes"][0]["display"]
    accepted = c.post(BASE + "/accept", json=body | {"settings_version": preview["settings_version"]})
    assert accepted.status_code == 200
    assert set(state["saves"][0]["settings_patch"]) == {"service_types", "resource_types"}
    assert state["saves"][0]["settings_patch"]["resource_types"] == [{"name": "Review", "count": 3}]


def test_mode_change_after_preview_invalidates_stay_appointment(client):
    c, state = client
    state["cfg"] = {"booking_mode": "stay"}
    state["card"] = start(service(at_once=3))
    body = selection(state)
    preview = c.post(BASE + "/preview", json=body).json()
    state["cfg"]["booking_mode"] = "appointment"
    assert c.post(BASE + "/accept", json=body | {"settings_version": preview["settings_version"]}).status_code == 409
    assert not state["saves"]


def test_scalar_accept_confirms_with_authenticated_actor_and_stale_values_fail(client):
    from tests.test_setup_card_scalars import setting
    c, state = client
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="owner", client=None, user_id="signed-in-owner")
    state["cfg"] = {"capacity_per_window": 4, "booking_revision": 5}
    state["card"] = start(setting("capacity_per_window", 4))
    body = selection(state)
    preview = c.post(BASE + "/preview", json=body).json()
    assert not state["saves"]
    state["cfg"]["capacity_per_window"] = 3
    assert c.post(BASE + "/accept", json=body | {"settings_version": preview["settings_version"]}).status_code == 409
    preview = c.post(BASE + "/preview", json=body).json()
    accepted = c.post(BASE + "/accept", json=body | {"settings_version": preview["settings_version"]})
    assert accepted.status_code == 200
    review = state["saves"][0]["settings_patch"]["booking_setting_review"]["capacity"]
    assert review["confirmed_by"] == "signed-in-owner" and review["revision"] == 6


def test_failed_read_never_becomes_empty_success(client, monkeypatch):
    c, state = client
    def broken(*a, **kw):
        raise RuntimeError("database unavailable")
    monkeypatch.setattr(store, "load", broken)
    assert c.get(BASE).status_code == 503
    assert not state["saves"]


def test_csrf_and_forged_accept_payload(client):
    c, state = client
    c.headers.pop("x-csrf-token")
    assert c.post(BASE + "/changes", json={"revision": 1, "changes": [{"op": "remove", "id": state["card"]["lines"][0]["id"]}]}).status_code == 403
    c.headers["x-csrf-token"] = "token"
    body = selection(state) | {"settings_version": "0" * 64, "settings_patch": {"capacity_per_window": 9}}
    assert c.post(BASE + "/accept", json=body).status_code == 422
    assert not state["saves"]


def test_database_race_maps_to_conflict_without_success(client, monkeypatch):
    c, state = client
    body = selection(state)
    preview = c.post(BASE + "/preview", json=body).json()
    def raced(*a, **kw):
        raise cards.Stale("reload")
    monkeypatch.setattr(store, "save", raced)
    assert c.post(BASE + "/accept", json=body | {"settings_version": preview["settings_version"]}).status_code == 409
    assert state["card"]["status"] == "proposed"


def test_edit_endpoint_preserves_other_fields_and_rejects_missing_sources(client):
    c, state = client
    line = state["card"]["lines"][0]
    change = {"op": "set", "id": line["id"], "fields": {"minutes": 90}, "sources": {}}
    assert c.post(BASE + "/changes", json={"revision": 1, "changes": [change]}).status_code == 422
    assert not state["saves"]
    change["sources"] = {"minutes": {"kind": "owner_said", "reference": "Owner edit"}}
    r = c.post(BASE + "/changes", json={"revision": 1, "changes": [change]})
    assert r.status_code == 200
    assert r.json()["card"]["lines"][0]["fields"] == {"name": "Review", "minutes": 90, "timing": "time"}


def test_history_uses_authenticated_tenant_and_passes_cursor(client, monkeypatch):
    c, _ = client
    def history(tid, page, *, before, client):
        assert (tid, page, before) == ("owner", "diary_settings", 70)
        return [{"revision": 69, "card": {"status": "applied"}}]
    monkeypatch.setattr(store, "history", history)
    r = c.get(BASE + "/history?before=70")
    assert r.status_code == 200 and r.json()["revisions"][0]["revision"] == 69
    assert c.get(BASE + "/history?before=0").status_code == 422


def test_panel_receives_effective_values_and_per_line_acceptability(client):
    c, state = client
    r = c.get("/concierge/setup-cards")
    assert r.status_code == 200
    view = r.json()["views"]["diary_settings"]
    assert view["all"]["changes"][0]["fields"]["minutes"] == 45
    assert view["rows"][state["card"]["lines"][0]["id"]]["settings_version"] == view["all"]["settings_version"]


def test_first_card_can_create_a_diary_without_guessing_core_settings(client):
    c, state = client
    state["cfg"] = None
    body = selection(state)
    preview = c.post(BASE + "/preview", json=body)
    assert preview.status_code == 200
    response = c.post(BASE + "/accept", json=body | {"settings_version": preview.json()["settings_version"]})
    assert response.status_code == 200
    assert state["saves"][0]["expected_settings"] == {}
    assert set(state["saves"][0]["settings_patch"]) == {"service_types"}
