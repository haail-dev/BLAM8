"""Dedicated install entry and actual browser landing dispatch, without live services."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from app import concierge_routes, dashboard
from app.auth import require_subscription


def test_live_entry_renders_shared_page_without_redirect(monkeypatch):
    app = FastAPI()
    app.include_router(concierge_routes.router)
    ctx = SimpleNamespace(tenant_id='fixture', client=None)
    app.dependency_overrides[require_subscription] = lambda: ctx
    seen = []
    def render(request, context):
        seen.append((request.url.path, context))
        return HTMLResponse('shared Concierge')
    monkeypatch.setattr(dashboard, 'concierge_page', render)
    with TestClient(app) as client:
        response = client.get('/concierge/live', follow_redirects=False)
    assert response.status_code == 200
    assert 'location' not in response.headers
    assert seen == [('/concierge/live', ctx)]


def test_live_entry_keeps_entitlement_gate(monkeypatch):
    app = FastAPI()
    app.include_router(concierge_routes.router)
    app.dependency_overrides[require_subscription] = lambda: SimpleNamespace(tenant_id='fixture', client=None)
    monkeypatch.setattr(dashboard, 'has_concierge', lambda *a, **k: False)
    with TestClient(app) as client:
        response = client.get('/concierge/live', follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['location'] == '/billing/plans?concierge=1'


def test_manifest_and_rendered_install_metadata_are_separate():
    from jinja2 import Environment
    normal = json.loads(Path('src/app/static/manifest.json').read_text())
    live = json.loads(Path('src/app/static/concierge-manifest.json').read_text())
    assert normal['id'] == '/app' and normal['start_url'] == '/app/'
    assert live['id'] != normal['id']
    assert live['name'] == 'BLAM8 Concierge'
    assert live['start_url'] == '/concierge/live'
    assert live['start_url'].startswith(live['scope'])
    source = Path('src/app/templates/app/base.html').read_text(encoding='utf-8')
    lines = '\n'.join(line for line in source.splitlines() if 'rel="manifest"' in line or 'name="apple-mobile-web-app-title"' in line)
    for path, manifest, title in [('/concierge/live', '/static/concierge-manifest.json', 'BLAM8 Concierge'), ('/app/', '/manifest.json', 'BLAM8'), ('/app/concierge', '/manifest.json', 'BLAM8')]:
        html = Environment(autoescape=True).from_string(lines).render(request=SimpleNamespace(url=SimpleNamespace(path=path)))
        assert 'href="'+manifest+'"' in html
        assert 'content="'+title+'"' in html


def test_shortcut_landing_invokes_existing_voice_launcher():
    import shutil
    import subprocess
    node = shutil.which('node')
    if not node:
        pytest.fail('Node is required to verify the live shortcut landing')
    js = Path('src/app/static/concierge_panel.js').read_text(encoding='utf-8')
    start = js.index("    if (location.hash === '#voice') {   //")
    landing = js[start:js.index('\n    return;', start)]
    for path, fragment, expected in [('/app/concierge', '#talk', 'talk'), ('/app/concierge', '#voice', 'refresh'), ('/app/concierge', '', None), ('/concierge/live', '', 'talk'), ('/concierge/live', '#voice', 'refresh')]:
        script = 'let calls=[]; let location={hash:' + json.dumps(fragment) + ',pathname:'+json.dumps(path)+',search:""}; let history={replaceState(a,b,url){if(url!==location.pathname)throw Error("URL changed")}}; let pageApi={afterVoiceRefresh(){calls.push("refresh")},afterTalkLaunch(){calls.push("talk")}};\n' + landing + '\nconsole.log(JSON.stringify(calls));'
        result = subprocess.run([node, '-e', script], capture_output=True, text=True, check=True)
        assert json.loads(result.stdout) == ([] if expected is None else [expected])


def test_dedicated_record_links_open_separately_and_voice_closes_before_settings():
    import shutil
    import subprocess
    node = shutil.which('node')
    if not node:
        pytest.fail('Node is required to verify Concierge settings and record-link interaction')
    js = Path('src/app/static/concierge_panel.js').read_text(encoding='utf-8')
    start = js.index('  function keepPlatformRecordInAnotherTab(')
    end = js.index('\n\n  function csrfToken()', start)
    helpers = js[start:end]
    voice = Path('src/app/static/voice-live.js').read_text(encoding='utf-8')
    gptlive = Path('src/app/static/voice-gptlive.js').read_text(encoding='utf-8')
    assert 'class="cz-voice-settings" aria-label="Concierge settings"' in voice
    assert 'class="cz-voice-settings" aria-label="Concierge settings"' in gptlive
    script = '''
      const assert = require('node:assert/strict');
      const opened = [], navigated = [];
      const location = {pathname: '/concierge/live'};
      const window = {open: (...args) => opened.push(args), location: {set href(v) { navigated.push(v); }}};
    ''' + helpers + '''
      const link = {};
      keepPlatformRecordInAnotherTab(link, '/app/invoices/inv-4');
      assert.equal(link.target, '_blank');
      assert.equal(link.rel, 'noopener noreferrer');
      openPlatformRecord('/app/invoices/inv-4');
      assert.deepEqual(opened, [['/app/invoices/inv-4', '_blank', 'noopener,noreferrer']]);
      assert.deepEqual(navigated, []);
      location.pathname = '/app/concierge';
      const ordinary = {};
      keepPlatformRecordInAnotherTab(ordinary, '/app/invoices/inv-4');
      openPlatformRecord('/app/invoices/inv-4');
      assert.equal(ordinary.target, undefined);
      assert.deepEqual(navigated, ['/app/invoices/inv-4']);
      const order = [];
      closeVoiceBeforeSettings({close: async reason => { order.push('close:' + reason); }}, () => order.push('settings'))
        .then(() => { assert.deepEqual(order, ['close:settings', 'settings']); console.log('ok'); });
    '''
    result = subprocess.run([node, '-e', script], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == 'ok'
