"""Exercise the shortcut route without starting application services."""
import ast
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient


def test_live_shortcut_uses_existing_launch_path():
    source = Path('src/app/concierge_routes.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    route = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'concierge_live_link')
    app = FastAPI()
    scope = {'router': app, 'RedirectResponse': RedirectResponse}
    exec(compile(ast.Module(body=[route], type_ignores=[]), '<shortcut>', 'exec'), scope)
    with TestClient(app) as client:
        response = client.get('/live', follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['location'] == '/app/concierge#talk'
    tree = ast.parse(Path('src/app/dashboard.py').read_text(encoding='utf-8'))
    page = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'concierge_page')
    assert 'Depends(require_onboarded)' in ast.unparse(page)
    assert 'has_concierge' in ast.unparse(page)


def test_shortcut_landing_invokes_existing_voice_launcher():
    import json
    import shutil
    import subprocess
    import pytest
    node = shutil.which('node')
    if not node:
        pytest.fail('Node is required to verify the live shortcut landing')
    js = Path('src/app/static/concierge_panel.js').read_text(encoding='utf-8')
    start = js.index("    if (location.hash === '#voice') {   //")
    landing = js[start:js.index('\n    return;', start)]
    for fragment, expected in [('#talk', 'talk'), ('#voice', 'refresh'), ('', None)]:
        script = 'let calls=[]; let location={hash:' + json.dumps(fragment) + ',pathname:"/concierge",search:""}; let history={replaceState(){}}; let pageApi={afterVoiceRefresh(){calls.push("refresh")},afterTalkLaunch(){calls.push("talk")}};\n' + landing + '\nconsole.log(JSON.stringify(calls));'
        result = subprocess.run([node, '-e', script], capture_output=True, text=True, check=True)
        assert json.loads(result.stdout) == ([] if expected is None else [expected])
