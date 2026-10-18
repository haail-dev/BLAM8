"""First-walk finding F19, 1 Oct 2026: the Dependants editor offered "One of"
but had no box for the options, and a stored dropdown lost its options on the
next page save."""
import pathlib

import pytest

from app import requirement_groups as rg

TEMPLATE = pathlib.Path(__file__).resolve().parent.parent / "src/app/templates/app/diary_dependants.html"


def _parse(**kw):
    return rg.parse_editor_form(["Item"], ["Items"], ["many"], ["0"], ["Choice"], ["dropdown"],
                                ["0"], ["once"], ["0"], [""], **kw)


def test_posted_options_are_split_trimmed_and_stored():
    """First-walk finding F19, 1 Oct 2026."""
    g = _parse(f_options=["Red, Green , Blue"])
    assert g[0]["fields"][0]["options"] == ["Red", "Green", "Blue"]


def test_old_shape_post_keeps_stored_options():
    """First-walk finding F19, 1 Oct 2026: a post without f_options must not wipe them."""
    existing = [{"label": "Item", "label_plural": "Items", "cardinality": "many", "fields": [
        {"label": "Choice", "type": "dropdown", "options": ["Option one", "Option two"]}]}]
    g = _parse(existing=existing)
    assert g[0]["fields"][0]["options"] == ["Option one", "Option two"]


def test_template_has_options_input_in_row_loop_and_row_template():
    """First-walk finding F19, 1 Oct 2026."""
    html = TEMPLATE.read_text(encoding="utf-8")
    loop, tpl = html.split('<template id="field-template">')
    assert 'name="f_options"' in loop and 'name="f_options"' in tpl


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


def test_save_then_reload_keeps_the_options(client, monkeypatch):
    """First-walk finding F19, 1 Oct 2026: save -> reload keeps the options,
    including through an old-shape save that omits f_options."""
    import app.dashboard as dashboard
    stored = {"rows": []}
    monkeypatch.setattr(dashboard.db, "list_requirement_groups", lambda t, client=None: list(stored["rows"]))
    import json
    def snapshot():
        return {'page':'dependants','config':{},'groups':stored['rows'],'instances':[],'definitions':[]}
    monkeypatch.setattr(dashboard.setup_forms,'load',lambda *a: snapshot())
    monkeypatch.setattr(dashboard.setup_forms,'save',lambda ctx,page,expected,ops:
        stored.update(rows=[{**o['fields'],'id':o['id']} for o in ops if o['table']=='requirement_groups' and o['op']!='delete']))
    base = {"g_label": ["Item"], "g_plural": [""], "g_cardinality": ["many"], "g_id": [""],
            "f_group": ["0"], "f_label": ["Choice"], "f_type": ["dropdown"],
            "f_required": ["0"], "f_durability": ["once"], "f_gate": ["0"], "f_hint": [""]}
    r = client.post("/app/diary/dependants", data=dict(base, settings_snapshot=json.dumps(snapshot()), f_options=["Option one, Option two"]))
    assert r.status_code == 303
    page = client.get("/app/diary/dependants").text
    assert 'value="Option one, Option two"' in page
    r = client.post("/app/diary/dependants", data=dict(base, settings_snapshot=json.dumps(snapshot())))      # old shape, no f_options
    assert r.status_code == 303
    assert 'value="Option one, Option two"' in client.get("/app/diary/dependants").text
