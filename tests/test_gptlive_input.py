"""GPT-Live input controls: who writes the microphone track, hold to talk and the volume line.

The browser half lives in src/app/static/voice-gptlive.js (the transport), voice-talk.js and voice-line.js;
it is run here through the node replays (no real microphone, provider or audio). The server half is only
a place for the close report to land: counts and settings, never speech.
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

from app import gptlive as live
from tests.test_gptlive import session, unit  # noqa: F401  (unit is a fixture)

ROOT = pathlib.Path(__file__).resolve().parents[1]
STATIC = ROOT / "src/app" / "static"
PANEL_JS = (STATIC / "concierge_panel.js").read_text(encoding="utf-8")
GPTLIVE_JS = (STATIC / "voice-gptlive.js").read_text(encoding="utf-8")
TALK_JS = (STATIC / "voice-talk.js").read_text(encoding="utf-8")
TEMPLATE = (ROOT / "src/app" / "templates" / "app" / "_concierge_panel.html").read_text(encoding="utf-8")
# Words that name a kind of business. The product is agnostic: settings, not types.
TRADE_WORDS = ("plumb", "garage", "mechanic", "vehicle", "salon", "barber", "dentist", "clinic", "patient",
               "restaurant", "builder", "electrician", "roofer", "landscap", "gardener", "willowbrook", "hartwell")


def _node(script):
    node = shutil.which("node")
    if not node:
        pytest.fail("Node is required to run the GPT-Live input replays")
    result = subprocess.run([node, script], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_microphone_ownership_replay():
    """One writer for the sent track; a mute always wins; the meter reads a copy, never the sent track."""
    assert "passed" in _node("tests/replays/gptlive_input_replay.mjs")


# ── hold to talk ──────────────────────────────────────────────────────────────────────────────────
def test_talk_controller_gestures_transport_and_idle_clock_replay():
    """The press, tap, tail, hard-stop and watchdog rules; the gestures; the transport; the idle clock."""
    assert "passed" in _node("tests/replays/gptlive_talk_replay.mjs")


def test_control_talk_report_is_allow_listed_bounded_and_never_rejects_a_close():
    body = live.Control(session="s", operation="close",
                        talk={"mode": "hold", "presses": 3, "talk_ms": 12000, "latches": 1, "watchdog_stops": 0, "words": "private"})
    assert body.talk == {"mode": "hold", "presses": 3, "talk_ms": 12000, "latches": 1, "watchdog_stops": 0}
    assert live.Control(session="s", operation="close", talk={"mode": "hands_free"}).talk == {"mode": "hands_free"}
    # a bound or a type that is off drops that one number, not the report
    odd = live.Control(session="s", operation="close",
                       talk={"mode": "hold", "presses": 65536, "talk_ms": -1, "latches": 1.5, "watchdog_stops": True}).talk
    assert odd == {"mode": "hold"}
    top = live.Control(session="s", operation="close", talk={"mode": "hold", "presses": 65535, "talk_ms": 86400000}).talk
    assert top == {"mode": "hold", "presses": 65535, "talk_ms": 86400000}
    # anything that is not a report with a known mode is dropped whole: a diagnostic must not cost a close
    for bad in (["hold"], "hold", 5, {}, {"mode": "toggle"}, {"mode": None, "presses": 1}, {"presses": 1}):
        assert live.Control(session="s", operation="close", talk=bad).talk is None
    assert live.Control(session="s", operation="close").talk is None


@pytest.mark.asyncio
@pytest.mark.parametrize("registered", [False, True])
async def test_client_close_reports_how_the_person_talked(unit, monkeypatch, registered):
    s = session(); traces = []
    if registered:
        live._sessions[s.id] = s
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append((a, kw)))
    body = live.Control(session=s.id if registered else s.voice_session, operation="close", close_reason="end",
                        talk={"mode": "hold", "presses": 4, "talk_ms": 9000, "latches": 1, "watchdog_stops": 0})
    await live.control(body, unit)
    report = next(kw["detail"] for a, kw in traces if a[1] == "gptlive_client_close")
    assert report["talk"] == {"mode": "hold", "presses": 4, "talk_ms": 9000, "latches": 1, "watchdog_stops": 0}
    assert not any(word in json.dumps(traces) for word in ("transcript", "private"))


def test_the_talk_key_is_the_same_in_the_sheet_and_on_the_voice_screen():
    assert "var TALK_KEY = 'blam8.liveTalk';   // = voice-talk.js TALK_KEY" in PANEL_JS
    assert "export const TALK_KEY = 'blam8.liveTalk';" in TALK_JS
    # the voice screen reads the setting each time it opens, and the key is never the Standard one
    assert "inputNow=inputSettings();transport.configureInput(inputNow);" in GPTLIVE_JS
    assert "const talkMode=savedTalkMode()" in GPTLIVE_JS
    assert "blam8.voiceMicLine" not in TALK_JS and "blam8.liveTalk" not in (STATIC / "voice-live.js").read_text(encoding="utf-8")


def test_how_you_talk_card_is_gptlive_only_and_saves_on_this_device():
    card = TEMPLATE.split('<p id="cz-talk-heading" class="cz-set-h">How you talk</p>', 1)[1].split("</section>", 1)[0]
    assert 'name="cz-talk" value="hands_free"' in card and 'name="cz-talk" value="hold"' in card
    assert 'id="cz-talk-status" class="cz-set-hint" role="status"' in card
    assert "Saved on this device." in card
    assert 'data-cz-voices="gptlive" aria-labelledby="cz-talk-heading"' in TEMPLATE
    # the agent is not told things the settings cannot do: the End button ends it, nothing here saves minutes
    assert "End the conversation with the End button" in card and "minutes" not in card


def test_the_sheet_saves_and_reads_the_talk_choice_in_real_javascript():
    node = shutil.which("node")
    if not node:
        pytest.fail("Node is required to verify the talk setting")
    start = PANEL_JS.index("    var TALK_KEY = ")
    helpers = PANEL_JS[start:PANEL_JS.index("    function paintTalk()", start)]
    script = """
      const assert = require('node:assert/strict');
      const vm = require('node:vm');
      const values = new Map();
      const localStorage = {getItem: k => (values.has(k) ? values.get(k) : null), setItem: (k, v) => values.set(k, v), removeItem: k => values.delete(k)};
      const c = vm.createContext({localStorage});
      vm.runInContext(""" + json.dumps(helpers) + """, c);
      assert.equal(c.savedTalk(), 'hands_free');
      assert.equal(c.saveTalk('hold'), true); assert.equal(values.get('blam8.liveTalk'), 'hold'); assert.equal(c.savedTalk(), 'hold');
      assert.equal(c.saveTalk('hands_free'), true); assert.equal(values.has('blam8.liveTalk'), false, 'hands-free is no key at all');
      assert.equal(c.saveTalk('toggle'), false); assert.equal(values.has('blam8.liveTalk'), false);
      values.set('blam8.liveTalk', 'anything else'); assert.equal(c.savedTalk(), 'hands_free');
      localStorage.getItem = () => { throw Error('blocked'); }; localStorage.setItem = () => { throw Error('blocked'); };
      assert.equal(c.savedTalk(), 'hands_free'); assert.equal(c.saveTalk('hold'), false);
    """
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_a_long_press_is_disarmed_in_hold_mode_but_the_two_argument_call_still_works():
    """Hold mode disarms an unarmed long press and keeps the optional argument default."""
    assert "export function installHoldAction(button, action, {armed} = {}) {" in GPTLIVE_JS
    assert "if (armed && !armed()) { consumed = false; return; }" in GPTLIVE_JS


def test_the_new_copy_names_no_kind_of_business():
    cards = TEMPLATE.split('data-cz-voices="gptlive" aria-labelledby="cz-talk-heading"', 1)[1].split("</section>", 1)[0]
    copy = cards + TALK_JS + "".join(re.findall(r"'[^'\n]*(?:Mist|mic|line|talk)[^'\n]*'", GPTLIVE_JS))
    for word in TRADE_WORDS:
        assert word not in copy.lower(), word


# ── the volume line ───────────────────────────────────────────────────────────────────────────────
LINE_JS = (STATIC / "voice-line.js").read_text(encoding="utf-8")


def test_line_gate_fail_open_counters_transport_and_report_replay():
    """Attack, hold and hysteresis; every fail-open reason; counters; the transport; the close report."""
    assert "passed" in _node("tests/replays/gptlive_line_replay.mjs")


def test_control_line_report_is_allow_listed_bounded_and_never_rejects_a_close():
    full = {"line": 0.12, "closed_ms": 4000, "opens": 3, "near_misses": 1, "agc_requested_off": True,
            "fail_open": {"warmup": 1, "press": 2, "context": 1}, "words": "private", "label": "Private microphone"}
    body = live.Control(session="s", operation="close", line=full)
    assert body.line == {"line": 0.12, "closed_ms": 4000, "opens": 3, "near_misses": 1, "agc_requested_off": True,
                         "fail_open": {"warmup": 1, "press": 2, "context": 1}}
    # reasons the server does not know are left out, and a bad count drops that one count, not the report
    odd = live.Control(session="s", operation="close", line={
        "line": 0.5, "closed_ms": -1, "opens": 65536, "near_misses": 1.5, "agc_requested_off": "yes",
        "fail_open": {"made_up": 3, "gap": 65536, "zero": True, "hidden": 1}}).line
    assert odd == {"line": 0.5, "fail_open": {"hidden": 1}}
    # not a line the browser could have set (or no line at all): nothing to report
    for bad in ([0.1], "0.1", 5, {}, {"line": None}, {"line": 0}, {"line": 0.0}, {"line": 1}, {"line": 1.5}, {"line": -0.1},
                {"line": "0.1"}, {"line": True}, {"closed_ms": 1}):
        assert live.Control(session="s", operation="close", line=bad).line is None, bad
    assert live.Control(session="s", operation="close", line={"line": 0.2, "fail_open": ["gap"]}).line == {"line": 0.2}
    assert live.Control(session="s", operation="close").line is None
    # one body can carry both reports
    both = live.Control(session="s", operation="close", talk={"mode": "hold", "presses": 1}, line={"line": 0.2})
    assert both.talk == {"mode": "hold", "presses": 1} and both.line == {"line": 0.2}


@pytest.mark.asyncio
@pytest.mark.parametrize("registered", [False, True])
async def test_client_close_reports_the_volume_line(unit, monkeypatch, registered):
    s = session(); traces = []
    if registered:
        live._sessions[s.id] = s
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append((a, kw)))
    line = {"line": 0.2, "closed_ms": 1500, "opens": 2, "near_misses": 1, "agc_requested_off": True, "fail_open": {"warmup": 1}}
    await live.control(live.Control(session=s.id if registered else s.voice_session, operation="close", close_reason="end", line=line), unit)
    report = next(kw["detail"] for a, kw in traces if a[1] == "gptlive_client_close")
    assert report["line"] == line
    assert not any(word in json.dumps(traces) for word in ("transcript", "private"))


def test_the_line_keys_are_the_same_in_the_sheet_and_on_the_voice_screen_and_never_the_standard_one():
    assert "var LIVE_LINE_KEY = 'blam8.liveMicLine';   // = voice-line.js LINE_KEY" in PANEL_JS
    assert "export const LINE_KEY = 'blam8.liveMicLine';" in LINE_JS
    assert "blam8.voiceMicLine" not in LINE_JS and "blam8.liveMicLine" not in (STATIC / "voice-live.js").read_text(encoding="utf-8")
    # the Standard literals the existing pins rely on are untouched
    assert "var MIC_LINE_KEY = 'blam8.voiceMicLine';" in PANEL_JS


def test_the_mic_opened_ahead_of_the_screen_asks_for_what_the_screen_would():
    """startPreMic (concierge_panel.js) hands its stream to voice-gptlive.js, so both must ask for the same thing."""
    node = shutil.which("node")
    if not node:
        pytest.fail("Node is required to verify the capture constraints")
    start = PANEL_JS.index("    function liveAgcOff() {")
    pre = PANEL_JS[start:PANEL_JS.index("    async function openVoiceLive(opts) {", start)]
    script = """
      import {pathToFileURL} from 'node:url';
      import assert from 'node:assert/strict';
      import vm from 'node:vm';
      const {captureConstraints} = await import(pathToFileURL(process.cwd() + '/src/app/static/voice-gptlive.js'));
      const {savedLiveLine} = await import(pathToFileURL(process.cwd() + '/src/app/static/voice-line.js'));
      const {savedTalkMode} = await import(pathToFileURL(process.cwd() + '/src/app/static/voice-talk.js'));
      const asked = [];
      let n = 0;
      for (const line of [null, '', 'abc', '0', '1', '1.5', '-0.2', '0.05', '0.3', '0.999'])
        for (const talk of [null, 'hold', 'hands_free', 'HOLD']) {
          const values = new Map();
          if (line !== null) values.set('blam8.liveMicLine', line);
          if (talk !== null) values.set('blam8.liveTalk', talk);
          const storage = {getItem: k => (values.has(k) ? values.get(k) : null)};
          const c = vm.createContext({localStorage: storage, LIVE_LINE_KEY: 'blam8.liveMicLine', TALK_KEY: 'blam8.liveTalk', performance: {now: () => 1},
            window: {}, setTimeout: () => 0, releasePreMic() {}, navigator: {mediaDevices: {getUserMedia: constraints => { asked.push(constraints); return Promise.resolve({}); }}}});
          vm.runInContext(""" + json.dumps(pre) + """.split("    async function openVoiceLive")[0] + "\\nstartPreMic();", c);
          const agcOff = Boolean(savedTalkMode(storage) !== 'hold' && savedLiveLine(storage));
          assert.equal(JSON.stringify(asked.at(-1)), JSON.stringify(captureConstraints(agcOff)), JSON.stringify([line, talk]));
          n += 1;
        }
      assert.equal(asked.length, n);
      assert.equal(captureConstraints(true).audio.autoGainControl, false);
      assert.equal(captureConstraints().audio.autoGainControl, true);
    """
    result = subprocess.run([node, "--input-type=module", "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert PANEL_JS.count("autoGainControl: false") >= 1      # the settings sheet's own meter, as before


def test_each_voice_mode_keeps_its_own_line_and_gptlive_has_no_automatic_one():
    node = shutil.which("node")
    if not node:
        pytest.fail("Node is required to verify the line card")
    start = PANEL_JS.index("    var MIC_LINE_KEY = ")
    helpers = PANEL_JS[start:PANEL_JS.index("    function startMeter() {", start)]
    script = """
      const assert = require('node:assert/strict');
      const vm = require('node:vm');
      const values = new Map();
      const localStorage = {getItem: k => (values.has(k) ? values.get(k) : null), setItem: (k, v) => values.set(k, v), removeItem: k => values.delete(k)};
      const slider = {value: '5'}, mark = {style: {left: '0%'}};
      const sheet = {querySelector: s => (s === '#cz-mic-line' ? slider : s === '.cz-meter-line' ? mark : null)};
      let mode = 'standard';
      const c = vm.createContext({localStorage, sheet, meter: null, savedVoiceMode: () => mode});
      vm.runInContext(""" + json.dumps(helpers) + """, c);
      // Standard keeps the key it always had, GPT-Live its own: a line in one never reaches the other
      c.writeLine(0.2); assert.equal(values.get('blam8.voiceMicLine'), '0.2'); assert.equal(values.has('blam8.liveMicLine'), false);
      assert.equal(c.readLine(), 0.2);
      mode = 'gptlive'; assert.equal(c.readLine(), null, 'a Standard line does not gate GPT-Live');
      c.writeLine(0.1); assert.equal(values.get('blam8.liveMicLine'), '0.1'); assert.equal(values.get('blam8.voiceMicLine'), '0.2');
      assert.equal(c.readLine(), 0.1);
      mode = 'standard'; assert.equal(c.readLine(), 0.2);
      // anything but a number between 0 and 1 is no line, and a store that throws is none either
      mode = 'gptlive';
      for (const bad of ['', 'abc', '0', '1', '2', '-0.3']) { values.set('blam8.liveMicLine', bad); assert.equal(c.readLine(), null, bad); }
      localStorage.getItem = () => { throw Error('blocked'); }; localStorage.setItem = () => { throw Error('blocked'); };
      assert.equal(c.readLine(), null); c.writeLine(0.1);
      // the readout: Standard says what it always said, GPT-Live says "Line off" until the owner sets one
      const m = {room: 0.02, you: 0.1, line: 0.2, live: false, mine: true};
      assert.equal(c.lineReadout(m, 0.2), 'Room 20 · You 45 · Line 63 (yours)');
      assert.equal(c.lineReadout({...m, mine: false}, 0.06), 'Room 20 · You 45 · Line 35 — drag to set');
      assert.equal(c.lineReadout({...m, live: true, mine: false}, 0.5), 'Room 20 · You 45 · Line off');
      assert.equal(c.lineReadout({...m, live: true}, 0.2), 'Room 20 · You 45 · Line 63 (yours)');
      // "Turn the line off" removes the GPT-Live key only, and puts the marker back at the right-hand edge
      const g = vm.createContext({localStorage: {getItem: k => (values.has(k) ? values.get(k) : null), setItem: (k, v) => values.set(k, v), removeItem: k => values.delete(k)},
        sheet, savedVoiceMode: () => 'gptlive'});
      vm.runInContext(""" + json.dumps(helpers) + """, g);
      g.meter = {mine: true, line: 0.1};      // the helpers declare `var meter = null`, so it is set after they run
      values.set('blam8.liveMicLine', '0.1'); values.set('blam8.voiceMicLine', '0.2');
      g.turnLineOff();
      assert.equal(values.has('blam8.liveMicLine'), false); assert.equal(values.get('blam8.voiceMicLine'), '0.2');
      assert.equal(g.meter.mine, false); assert.equal(g.meter.line, 0.5); assert.equal(slider.value, '100'); assert.equal(mark.style.left, '100%');
    """
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    meter = PANEL_JS[PANEL_JS.index("function startMeter()"):][:4500]
    assert "placedAt: saved || live ? 1 : 0" in meter          # GPT-Live is never placed automatically
    assert "if (m.live && !m.mine) msg = '';" in meter         # and says nothing about a line that is off
    assert "if (offBtn) offBtn.hidden = !(m.live && m.mine);" in meter
    assert "placeLine(Math.max(m.room * AUTO_OVER_ROOM, AUTO_FLOOR));   // once" in meter   # Standard keeps its once-only placement


def test_the_line_card_serves_both_modes_with_their_own_words():
    card = TEMPLATE.split('<p class="cz-set-h">How loud counts as you</p>', 1)[1].split("</section>", 1)[0]
    assert 'data-cz-voices="standard"' in card and 'data-cz-voices="gptlive"' in card
    standard, live_copy = (card.split('data-cz-voices="standard">', 1)[1].split("</p>", 1)[0],
                           card.split('data-cz-voices="gptlive">', 1)[1].split("</p>", 1)[0])
    assert "held" in standard and "repeat" in standard
    # the honest words for GPT-Live: it drops what is under the line, defaults to everything, and says when it cannot measure
    assert "not sent to the agent" in live_copy and "Off, everything is sent" in live_copy
    assert "cannot measure the room, everything is sent and the voice screen says so" in live_copy
    assert "ignored" not in live_copy and "held" not in live_copy and "minutes" not in live_copy
    assert 'id="cz-mic-off"' in card and ">Turn the line off</button>" in card and 'id="cz-mic-off" class="cz-set-btn" hidden' in card
    assert 'id="cz-mic-holdnote" class="cz-set-hint" hidden>The line is not used while Hold to talk is on.' in card
    assert "var note = sheet.querySelector('#cz-mic-holdnote');" in PANEL_JS and "note.hidden = current !== 'hold';" in PANEL_JS


def test_the_line_names_no_kind_of_business_and_the_voice_screen_says_what_is_going_on():
    card = TEMPLATE.split('<p class="cz-set-h">How loud counts as you</p>', 1)[1].split("</section>", 1)[0]
    copy = card + LINE_JS + "".join(re.findall(r"'[^'\n]*(?:line|Line)[^'\n]*'", GPTLIVE_JS))
    for word in TRADE_WORDS:
        assert word not in copy.lower(), word
    # the four lines the owner is told, word for word
    for said in ("Volume line on — sound under your line is not sent.",
                 "The volume line cannot work on this phone right now, so everything is being sent. Tap the screen to try again.",
                 "Something under your line was not sent. If that was you, lower the line in Settings.",
                 "Line off on this phone — everything is sent"):
        assert said in GPTLIVE_JS, said
    # near misses: the first is said, then at most once a minute
    assert "if(!nearNotedAt || Date.now()-nearNotedAt>=60000)" in GPTLIVE_JS
