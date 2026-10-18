"""Concierge opens as: Pop-up | Full screen (per device, computers only). Run: python -m pytest tests/test_concierge_opens_as.py -q"""
import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
JS = (ROOT / "src/app/static/concierge_panel.js").read_text(encoding="utf-8")
SETTINGS = (ROOT / "src/app/templates/app/settings.html").read_text(encoding="utf-8")
FN = JS[JS.index("  function fullScreenWanted() {"):]
FN = FN[:FN.index("  window.blam8ConciergeOpen")]


def _open(saved, fine_pointer):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    code = f"""
var opened = 0, went = null;
global.localStorage = {{getItem: () => {json.dumps(saved)}}};
global.window = global; global.matchMedia = (q) => ({{matches: {json.dumps(fine_pointer)} && q === '(hover: hover) and (pointer: fine)'}});
global.location = {{assign: (u) => {{ went = u; }}}};
function open() {{ opened++; }}
{FN}
openConcierge();
console.log(JSON.stringify({{opened, went}}));
"""
    return json.loads(subprocess.run([node, "-e", code], capture_output=True, text=True, check=True).stdout)


def test_default_is_popup():
    assert _open(None, True) == {"opened": 1, "went": None}
    assert _open("popup", True) == {"opened": 1, "went": None}


def test_full_screen_on_a_computer_goes_to_the_page():
    assert _open("full", True) == {"opened": 0, "went": "/app/concierge"}


def test_full_screen_does_nothing_without_a_fine_hovering_pointer():
    assert _open("full", False) == {"opened": 1, "went": None}    # phone/tablet: saved value ignored


def test_the_button_and_menu_links_use_it_and_voice_launches_do_not():
    assert "'(hover: hover) and (pointer: fine)'" in JS and "max-width" not in FN   # not the screen width
    assert "if (isOpen()) close(); else openConcierge();" in JS
    assert "if (fullScreenWanted()) return;   // the link's own href" in JS         # menu / Today row: plain navigation
    assert "window.blam8ConciergeOpen = openConcierge;" in JS
    # #concierge/#voice/#talk landings, hold-for-voice and the voice screen still call open() directly
    assert "open(wantVoice);" in JS and "if (!isOpen()) open(true);" in JS
    for untouched in ("wantVoice", "afterTalkLaunch", "afterVoiceRefresh"):
        assert untouched not in FN


def test_settings_row_is_per_device_greyed_on_phones_and_guarded():
    assert "var KEY = 'blam8.conciergeOpensAs';" in SETTINGS
    assert "localStorage.getItem(KEY) === 'full' ? 'full' : 'popup'" in SETTINGS     # default Pop-up
    assert "(hover: hover) and (pointer: fine)" in SETTINGS
    assert "Full screen is for computers" in SETTINGS and "b.disabled = !computer;" in SETTINGS
    assert SETTINGS.index('id="water-switch"') < SETTINGS.index('id="cz-open-as"')   # beside Ripples
    assert "<form" not in SETTINGS[SETTINGS.index('id="cz-open-as"') - 600:SETTINGS.index("var KEY = 'blam8.conciergeOpensAs'")]   # instant, no save button owed
