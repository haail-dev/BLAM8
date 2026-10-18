"""The Debrief button, client half: the logic (tests/replays/debrief_button_replay.mjs runs
the real functions), what the two sides agree on, who is shown it, and where it sits.

Run: python tests/run_offline.py tests/test_debrief_button.py -q
"""
import pathlib
import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from app import debrief_walk as dw
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf_header

ROOT = pathlib.Path(__file__).resolve().parents[1]
STATIC = ROOT / "src/app" / "static"
PANEL_HTML = (ROOT / "src/app" / "templates" / "app" / "_concierge_panel.html").read_text(encoding="utf-8")


def test_the_button_logic_against_the_real_functions():
    node = shutil.which("node")
    if not node:
        pytest.fail("Node is required to replay the Debrief button")
    result = subprocess.run([node, "tests/replays/debrief_button_replay.mjs"], cwd=ROOT,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Debrief button regressions passed." in result.stdout


def test_the_two_sides_say_the_same_words():
    """The server never checks the question against the flag, but the chat shows
    it and the model reads it: one wording, kept in step across the two files."""
    live = (STATIC / "voice-live.js").read_text(encoding="utf-8")
    assert re.search(r'^const DEBRIEF_START = "%s";$' % re.escape(dw.START_LINE), live, re.M)
    assert re.search(r'^const DEBRIEF_NEXT = "%s";$' % re.escape(dw.NEXT_LINE), live, re.M)


def test_both_voice_screens_can_start_it():
    live = (STATIC / "voice-live.js").read_text(encoding="utf-8")
    native = (STATIC / "voice-gptlive.js").read_text(encoding="utf-8")
    assert "    startDebrief,\n" in live
    # native mode asks the server — it has no channel to the provider — and never silences
    start = native.split("async startDebrief(step){", 1)[1].split("\n    notify(text)", 1)[0]
    assert "operation:'debrief'" in start and "transport.silence" not in start
    assert "boundedRequest('control'" in start


def test_the_button_sits_top_right_and_clear_of_the_top_left_row():
    """Codex's live-allowance work owns the top-left row (the allowance bar, the
    badges, the context chip). The corner on the right is the Debrief's."""
    css = PANEL_HTML.split("The Debrief button (Kris 2026-10-14)", 1)[1].split("</style>", 1)[0]
    base = css.split("{", 1)[1].split("}", 1)[0]
    assert "right: 0.8rem" in base and "left:" not in base
    assert "top: max(0.9rem, env(safe-area-inset-top))" in base
    # both overlays, so neither mode is left without it
    assert "#voice-live-root .cz-debrief" in css and "#voice-gptlive-root .cz-debrief" in css
    # the native activity strip makes room rather than running under it, and the same room on
    # the left keeps its content centred (live 2026-10-14: ~20 px off with the right side alone)
    assert "#voice-gptlive-root.has-debrief .vl-top-strip { padding-left: 3.2rem; padding-right: 3.2rem; }" in css
    # a phone-sized target: padded out from the 2rem circle
    assert "inset: -6px" in css


@pytest.fixture
def panel(monkeypatch):
    """Render /app/concierge/panel as a given person."""
    import app.dashboard as dashboard
    from app import db
    seen = {"member": None, "viewing_as": None}
    monkeypatch.setattr(db, "get_member", lambda *a, **k: seen["member"])
    monkeypatch.setattr(dashboard.db, "get_member", lambda *a, **k: seen["member"])
    monkeypatch.setattr(dashboard.db, "visible_modules", lambda *a, **k: {"crm"})
    monkeypatch.setattr(dashboard.db, "list_concierge_messages", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "get_tenant_config", lambda *a, **k: {})
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(
        tenant_id="t1", client=None, user_id="u1", viewing_as=seen["viewing_as"])
    app.dependency_overrides[require_csrf_header] = lambda: None
    client = TestClient(app)

    def render(member=None, viewing_as=None):
        seen["member"], seen["viewing_as"] = member, viewing_as
        app.dependency_overrides[require_onboarded] = lambda: TenantCtx(
            tenant_id="t1", client=None, user_id="u1", viewing_as=viewing_as)
        res = client.get("/app/concierge/panel")
        assert res.status_code == 200
        return res.text
    yield render
    app.dependency_overrides.clear()


@pytest.mark.parametrize("member,viewing_as,flag", [
    (None, None, "on"),                                               # the account's own owner
    ({"role": "owner", "active": True, "scopes": {}}, None, "on"),
    ({"role": "manager", "active": True, "scopes": {}}, None, "on"),
    ({"role": "staff", "active": True, "scopes": {}}, None, "off"),
    (None, "owner-id", "off"),                                        # viewing as someone else
])
def test_only_the_owner_and_managers_are_shown_the_button(panel, member, viewing_as, flag):
    html = panel(member, viewing_as)
    assert f'data-debrief="{flag}"' in html
    # and the stylesheet that places it ships in every case (it is inert without the button)
    assert "The Debrief button (Kris 2026-10-14)" in html
