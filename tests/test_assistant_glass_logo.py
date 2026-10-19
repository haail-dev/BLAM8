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
