"""Landing, sign-in and onboarding share the glass launcher and still fallback."""
from pathlib import Path
from types import SimpleNamespace

import jinja2
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("path", ["/", "/login", "/privacy", "/onboarding/interview", "/onboarding/mailbox", "/onboarding/voice", "/onboarding/knowledge", "/onboarding/knowledge/review/example", "/app/concierge"])
def test_glass_logo_on_landing_sign_in_and_onboarding(path):
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(ROOT / "src/app/templates"))
    request = SimpleNamespace(url=SimpleNamespace(path=path), state=SimpleNamespace(tenant_id=""))
    html = env.get_template("_concierge.html").render(request=request, asset_rev="test")
    header = html.split('<header data-bc-resize', 1)[1].split('</header>', 1)[0]
    assert 'src="/static/img/blam8-assistant-glass.png?' in header
    assert 'filter:' not in header
    launcher = html.split('<button id="bc-bubble"', 1)[1].split('</button>', 1)[0]
    landing = path in ("/", "/login") or path.startswith('/onboarding/')
    assert ('src="/static/assistant-glass-logo.js?' in html) == landing
    assert ('src="/static/img/blam8-assistant-glass.png?' in launcher) == landing
    assert ('src="/static/img/blam8-assistant-animated.png?' in html) == (not landing)
    assert 'id="bc-bubble"' in html and 'aria-label="Open BLAM8 assistant"' in html
    assert 'blam8-mark-small.svg' not in header


@pytest.mark.parametrize("path", ["/", "/login", "/privacy"])
def test_actual_public_header_matches_launcher_only_on_requested_routes(path):
    from fastapi.testclient import TestClient
    from app.main import app
    from tests.html_markup import markup
    html = TestClient(app).get(path).text
    page = markup(html)
    header = page.find_all('header')[0]
    copies = header.find_all(**{'data-assistant-glass-logo': None})
    assert len(copies) == (1 if path in ('/', '/login') else 0)
    scripts = [node.attrs.get('src', '') for node in page.find_all('script')]
    assert sum('/static/assistant-glass-logo.js?' in src for src in scripts) == (1 if copies else 0)
    if copies:
        launcher = page.one('button', id='bc-bubble')
        assert copies[0].one('img').attrs['src'] == launcher.one('img').attrs['src']
        assert copies[0].one('img').attrs['width'] == '44'


def test_live_modes_keep_settings_and_center_without_extra_corner_brand():
    from tests.html_markup import markup
    for name in ('voice-live.js', 'voice-gptlive.js'):
        source = (ROOT / 'src/app/static' / name).read_text(encoding='utf-8')
        corner = markup(source).one('div', **{'class': 'vl-corner'})
        assert not corner.find_all('img')
        assert corner.one('button').attrs['aria-label'] == 'Concierge settings'
        assert 'vl-orb' in source
    assert 'vl-side-mark' in (ROOT / 'src/app/static/voice-gptlive.js').read_text(encoding='utf-8')


def test_shared_glass_animation_visibility_and_fallback_replay():
    import subprocess
    result = subprocess.run(['node', 'tests/replays/assistant_glass_copies.mjs'], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
