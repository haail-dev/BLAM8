"""Launch by button: the manifest shortcut, the #talk hash and the opt-in auto-start setting.

String guards, like the greeting tests. Device behaviour (iOS/Android sound without a tap) is not
testable here.  Run: python -m pytest tests/test_launch_shortcut.py -q
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).parent.parent
JS = (ROOT / "src/app/static/concierge_panel.js").read_text(encoding="utf-8")
BASE = (ROOT / "src/app/templates/app/base.html").read_text(encoding="utf-8")
PANEL = (ROOT / "src/app/templates/app/_concierge_panel.html").read_text(encoding="utf-8")


def test_manifest_has_talk_shortcut():
    shortcuts = json.loads((ROOT / "src/app/static/manifest.json").read_text(encoding="utf-8"))["shortcuts"]
    by_name = {s["name"]: s["url"] for s in shortcuts}
    assert by_name["Talk to Concierge"] == "/app/concierge#talk"
    assert by_name["Concierge"] == "/app/concierge"   # the old entry stays


def test_talk_hash_lands_like_voice():
    assert "location.hash === '#voice' || location.hash === '#talk'" in BASE   # voice-landing class
    assert "location.hash === '#talk'" in JS
    assert "a.afterTalkLaunch()" in JS and "pageApi.afterTalkLaunch()" in JS
    # #voice keeps landing paused, never auto-starting
    assert "pageApi.afterVoiceRefresh();" in JS


def test_auto_start_setting_defaults_off_and_is_per_device():
    assert "var TALK_AUTO_KEY = 'blam8.talkAutoStart';" in JS
    assert "localStorage.getItem(TALK_AUTO_KEY) === 'on' ? 'on' : 'off'" in JS
    assert 'name="cz-talk-auto"' in PANEL and 'value="off"' in PANEL
    assert "This works on some phones" in PANEL
    assert "still see the mic button" in PANEL
    assert "iPhone" in PANEL and "Open URLs" in PANEL and "untested" in PANEL.lower()


def test_auto_start_is_fail_safe_and_only_for_talk():
    body = JS[JS.index("afterTalkLaunch: function"):JS.index("afterVoiceRefresh: afterVoiceRefresh")]
    assert "savedTalkAuto() !== 'on'" in body          # setting off: tap prompt
    assert "soundAllowedWithoutTap()" in body          # no sound allowed: tap prompt
    assert "fallback" in body
    assert "setInterval" not in body and "retry" not in body.lower()   # one attempt only
    # the only caller of the auto path is the #talk handling
    assert JS.count("afterTalkLaunch()") == 3   # #talk in the page frame, #talk in the modal frame, and the opt-in open-on-voice landing


# ── Open on the voice screen (installed app lands on voice from its start page) ──
import shutil
import subprocess

import pytest

PUSH = (ROOT / "src/app/static/push.js").read_text(encoding="utf-8")
HEAD_SCRIPT = BASE[BASE.index("(function () {\n    try {\n      var first"):]
HEAD_SCRIPT = HEAD_SCRIPT[:HEAD_SCRIPT.index("})();") + 5]


def _head(path="/app/", search="", hash_="", standalone=True, setting="on", seen=False, nav="navigate"):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    harness = f"""
const store = {{local: {{}}, session: {{}}}};
if ({json.dumps(setting)}) store.local['blam8.openOnVoice'] = {json.dumps(setting)};
if ({json.dumps(seen)}) store.session['blam8.launchSeen'] = '1';
const classes = new Set();
global.window = global; global.localStorage = {{getItem: k => store.local[k] ?? null}};
global.sessionStorage = {{getItem: k => store.session[k] ?? null, setItem: (k, v) => {{ store.session[k] = v; }}}};
global.performance = {{getEntriesByType: () => [{{type: {json.dumps(nav)}}}]}};
global.matchMedia = () => ({{matches: {json.dumps(standalone)}}});
global.navigator = {{}};
global.location = {{pathname: {json.dumps(path)}, search: {json.dumps(search)}, hash: {json.dumps(hash_)}}};
global.document = {{documentElement: {{classList: {{add: c => classes.add(c)}}}}}};
{HEAD_SCRIPT}
console.log(JSON.stringify({{go: window.__blam8OpenVoice === true, veil: classes.has('voice-landing'), seen: store.session['blam8.launchSeen']}}));
"""
    out = subprocess.run([node, "-e", harness], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_open_on_voice_default_off_and_persisted_per_device():
    assert "var OPEN_VOICE_KEY = 'blam8.openOnVoice';" in JS
    assert "localStorage.getItem(OPEN_VOICE_KEY) === 'on' ? 'on' : 'off'" in JS
    assert 'name="cz-open-voice"' in PANEL and "Open on the voice screen" in PANEL
    assert "blam8.openOnVoice" in BASE
    assert _head(setting=None)["go"] is False


@pytest.mark.parametrize("path", ["/app/", "/app"])
def test_open_on_voice_only_on_the_start_page(path):
    assert _head(path=path)["go"] is True
    assert _head(path=path)["veil"] is True
    for kwargs in ({"path": "/app/crm"}, {"path": "/app/concierge"}, {"search": "?contact=x"},
                   {"hash_": "#voice"}, {"hash_": "#talk"}, {"standalone": False}):
        assert _head(**{"path": path, **kwargs})["go"] is False, kwargs


def test_open_on_voice_once_per_launch():
    first = _head()
    assert first["go"] is True and first["seen"] == "1"
    assert _head(seen=True)["go"] is False                      # in-app navigation to Today, or a reload
    assert _head(path="/app/crm")["seen"] == "1"                 # a deep-link first load also uses up the launch
    assert _head(nav="reload")["go"] is False                    # pull-to-refresh / reload
    assert _head(nav="back_forward")["go"] is False


def test_open_on_voice_leaves_notification_landings_alone():
    # push.js says whether a fresh tap owns the landing; the panel then drops the veil instead of opening voice
    assert "window.blam8TapLanding = goWhereTapped;" in PUSH
    assert "return true;   // a tapped notification owns this landing" in PUSH
    assert "if (tapped) document.documentElement.classList.remove('voice-landing');" in JS
    assert "else openVoiceFromLaunch();" in JS
    assert "a.afterTalkLaunch()" in JS[JS.index("function openVoiceFromLaunch"):]


def test_open_on_voice_resume_threshold():
    assert "var VOICE_RESUME_MS = 10 * 60 * 1000" in JS
    body = JS[JS.index("document.addEventListener('visibilitychange', function () {\n    if (document.visibilityState === 'hidden') { hiddenAt"):]
    assert "away < VOICE_RESUME_MS" in body
    assert "!onStartPage()" in body and "readOpenOnVoice() !== 'on'" in body
    assert "isOpen() || voiceOverlayUp()" in body            # never over a conversation already on screen
