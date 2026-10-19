"""The ripples (src/app/static/water-core.js, the engine, and src/app/static/voice-pond.js, the GPT-Live screen's use of it): the
wiring and the guarantees that can be read from the source. The behaviour itself (taps, pointer, voice, sleeping, reduced
motion, hidden tab, a slow device) is proven in a real browser by tests/pond_browser.py."""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src/app" / "static"
CORE = (STATIC / "water-core.js").read_text(encoding="utf-8")
POND = (STATIC / "voice-pond.js").read_text(encoding="utf-8")
SCREEN = (STATIC / "voice-gptlive.js").read_text(encoding="utf-8")


def test_the_engine_is_a_plain_module_with_no_imports_and_no_page_knowledge():
    assert not re.search(r"^\s*import\b", CORE, re.M) and "await import" not in CORE
    assert "export function createWater(" in CORE
    # what belongs to one screen stays out of it: no class names of the voice screen, no microphone
    code = re.sub(r"//.*$", "", CORE, flags=re.M)   # the comments may name the places that use it; the code may not
    assert "vl-" not in code and "orb" not in code.lower()
    assert "AudioContext" not in CORE and "getUserMedia" not in CORE and "setInterval" not in CORE


def test_the_voice_screens_pond_uses_the_engine_with_the_deployment_revision():
    assert "await import('./water-core.js' + revision)" in POND
    assert "export function createPond(" in POND
    assert not re.search(r"^\s*import\b", POND, re.M)
    # it owns no loop, no listeners and no canvas drawing of its own
    assert "requestAnimationFrame" not in POND and "addEventListener" not in POND and "getContext" not in POND


def test_the_screen_loads_it_with_the_deployment_revision_like_its_siblings():
    assert "import('./voice-pond.js' + revision)" in SCREEN
    assert "{ createPond }" in SCREEN


def test_a_failure_in_the_pond_cannot_touch_the_screen():
    assert "try{pond=createPond(root.querySelector('.vl-stage'),root.querySelector('.vl-orb'));}catch(_){pond=null;}" in SCREEN
    assert "try{pond?.destroy();}catch(_){}pond=null;" in SCREEN


def test_the_voice_reaches_the_pond_through_the_signals_the_mist_already_reads():
    # one reading per Mist frame, handed on: the pond opens no second meter and starts no timer of its own
    assert "pond?.hear(seen);return seen;" in SCREEN
    assert "AudioContext" not in POND and "getUserMedia" not in POND and "setInterval" not in POND


def test_right_pager_page_uses_perimeter_sources_while_the_voice_page_keeps_the_orb_source():
    assert "classList.contains('cz-pager-body')" in POND
    assert "getPropertyValue('--cz-pan')" in POND and "> 0.5" in POND
    assert "const cx = mic.x + host.clientWidth, radius = host.clientWidth * 0.43" in POND
    assert "CFG.perimeterPoints" in POND and "water.addStone(cx + Math.cos(angle) * radius" in POND
    assert "water.addStone(mic.x, mic.y," in POND


def test_the_right_page_has_the_centered_stateful_blam8_mark():
    assert "BLAM8_MOTION_MARK" in SCREEN and "blam8-mark-motion.js" in SCREEN
    assert "replace('vl-ghost-mark','vl-side-mark')" in SCREEN
    assert "replaceAll('vl-motion-gear-mask','vl-motion-gear-mask-side')" in SCREEN
    assert "left:150%; top:50%" in SCREEN and ".vl-side-mark .vl-gear" in SCREEN
    for mode in ("user", "thinking", "agent"):
        assert f'data-mist-mode="{mode}"' in SCREEN


def test_the_engine_sleeps_stops_when_hidden_and_respects_reduced_motion():
    assert "prefers-reduced-motion: reduce" in CORE
    assert "doc.hidden" in CORE and "visibilitychange" in CORE
    assert "calmFramesToSleep" in CORE and "function sleep(" in CORE
    assert CORE.count("requestAnimationFrame(") == 2   # frame() reschedules itself, wake() starts it: one chain


def test_every_listener_the_engine_adds_is_removed():
    added = re.findall(r"(?:host|surface|win|doc)\.addEventListener\('([a-z]+)'", CORE)
    removed = re.findall(r"(?:host|surface|win|doc)\.removeEventListener\('([a-z]+)'", CORE)
    assert sorted(added) == sorted(removed) and added
