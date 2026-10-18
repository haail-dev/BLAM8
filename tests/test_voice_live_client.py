"""Live voice — the client half (src/app/static/voice-live.js + concierge_panel.js).

Voice moved from the Assistant widget's "Your business" tab to the Concierge
(2026-09-23) — the matchers now live in concierge_panel.js.

The local spoken-turn matchers live in JS but their vocab lists are JSON
literals, so these tests parse them straight out of the source and run the
matcher semantics over the donor's must-fire / must-NOT-fire lists. The
must-NOT-fire half is the one that matters: a false-positive "yes" executes a
real proposal against real business data, and a false-positive farewell hangs
up on the user mid-thought.

Run: python -m pytest tests/test_voice_live_client.py -q
"""
import json
import pathlib
import re

STATIC = pathlib.Path(__file__).parent.parent / "src/app" / "static"
CONCIERGE_JS = (STATIC / "concierge_panel.js").read_text(encoding="utf-8")
VOICE_JS = (STATIC / "voice-live.js").read_text(encoding="utf-8")
OVERLAY_CSS = (STATIC / "voice-overlay-css.js").read_text(encoding="utf-8")
GPTLIVE_JS = (STATIC / "voice-gptlive.js").read_text(encoding="utf-8")
TURNS_JS = (STATIC / "voice-turns.js").read_text(encoding="utf-8")
TEMPLATE = (pathlib.Path(__file__).parent.parent / "src/app" / "templates" / "app"
            / "_concierge_panel.html").read_text(encoding="utf-8")


def test_voice_choice_saved_reloaded_and_validated_in_actual_javascript():
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.fail("Node is required to verify voice preference persistence")
    helpers = CONCIERGE_JS.split("    var VOICE_CHOICE_KEY =", 1)[1].split("    function syncVoiceChoice", 1)[0]
    reader = VOICE_JS.split("export function savedConciergeVoice()", 1)[1].split("\n}", 1)[0]
    script = """
      const assert = require('node:assert/strict');
      const vm = require('node:vm');
      const values = new Map();
      const localStorage = {getItem: k => values.get(k), setItem: (k,v) => values.set(k,v)};
    """ + "const helpers = " + json.dumps("var VOICE_CHOICE_KEY =" + helpers) + ";" + """
      function panel(voices = ["marin", "cedar", "alloy", "sage", "coral"]) { const root = {querySelectorAll: () => voices.map(value => ({value}))}; const c = vm.createContext({localStorage, root}); vm.runInContext(helpers, c); return c; }
      let first = panel();
      assert.equal(first.savedConciergeVoice(), 'marin');
      for (const voice of ['marin', 'cedar', 'alloy', 'sage', 'coral']) {
        assert.equal(first.saveConciergeVoice(voice), true);
        assert.equal(panel().savedConciergeVoice(), voice);
      }
      assert.equal(first.saveConciergeVoice('unknown'), false);
      assert.equal(panel(['af_heart', 'bf_emma', 'bm_george']).savedConciergeVoice(), 'af_heart');
      assert.equal(panel(['af_heart', 'bf_emma', 'bm_george']).saveConciergeVoice('bf_emma'), true);
      assert.equal(panel(['af_heart', 'bf_emma', 'bm_george']).savedConciergeVoice(), 'bf_emma');
      values.set('blam8.conciergeVoice', 'unknown');
      assert.equal(panel().savedConciergeVoice(), 'marin');
    """ + "function liveVoice()" + reader + "\n}\n" + """
      assert.equal(liveVoice(), 'marin');
      first.saveConciergeVoice('cedar');
      assert.equal(liveVoice(), 'cedar');
      localStorage.getItem = () => { throw Error('blocked'); };
      localStorage.setItem = () => { throw Error('blocked'); };
      assert.equal(liveVoice(), 'marin');
      assert.equal(panel().savedConciergeVoice(), 'marin');
      assert.equal(first.saveConciergeVoice('sage'), false);
    """
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_all_voice_speech_paths_use_choice_and_separate_cached_voices():
    assert VOICE_JS.count('voice: ttsVoice') == 4
    assert 'ttsVoice = savedConciergeVoice();' in VOICE_JS
    assert VOICE_JS.count("const cacheKey = ttsVoice + ':' + text;") == 2
    assert 'data-cz-voice-sample="{{ voice }}"' in TEMPLATE
    assert 'radio.checked = radio.value === savedConciergeVoice()' in CONCIERGE_JS


def js_array(name: str) -> list:
    m = re.search(r"var " + name + r"\s*=\s*(\[[\s\S]*?\]);", CONCIERGE_JS)
    assert m, f"{name} missing from concierge_panel.js"
    return json.loads(m.group(1))


AFFIRM_VOCAB = set(js_array("VOICE_AFFIRM_VOCAB"))
AFFIRM_CORE = set(js_array("VOICE_AFFIRM_CORE"))
DECLINE_VOCAB = set(js_array("VOICE_DECLINE_VOCAB"))
DECLINE_CORE = set(js_array("VOICE_DECLINE_CORE"))
FAREWELLS = set(js_array("VOICE_FAREWELLS"))


# Python mirrors of the JS matcher semantics (the algorithms are four lines
# each; the vocab is the shared single source above).
def _words(text: str) -> list[str]:
    clean = re.sub(r"[^a-z\s]", " ", text.lower().replace("'", "").replace("’", ""))
    return clean.split()


def is_bare_affirmation(text: str) -> bool:
    words = re.sub(r"[^a-z'\s]", " ", text.lower()).split()
    if not words or len(words) > 6:
        return False
    if not all(w in AFFIRM_VOCAB for w in words):
        return False
    return any(w in AFFIRM_CORE for w in words)


def is_bare_decline(text: str) -> bool:
    words = _words(text)
    if not words or len(words) > 6:
        return False
    if not all(w in DECLINE_VOCAB for w in words):
        return False
    return any(w in DECLINE_CORE for w in words)


def is_voice_farewell(text: str) -> bool:
    clean = " ".join(_words(text))
    if not clean or len(clean.split(" ")) > 4:
        return False
    return clean in FAREWELLS


# ── affirmation: a spoken yes executes a REAL proposal ───────────────────────
MUST_AFFIRM = ["yes", "Yes.", "yes please", "yeah go ahead", "ok do it",
               "confirm", "yep that's right", "absolutely", "go ahead",
               "yes do it now", "sure", "okay then",
               # Kris 2026-09-23: these went to the model, which re-drafted
               "Yes, send it.", "Send it.", "send that off", "yes please send it"]
MUST_NOT_AFFIRM = ["yes but change the date", "yes move it to Friday",
                   "no", "what's on tomorrow", "do the invoice for forty pounds",
                   "yes and add another one", "send it to sarah",
                   "that's the wrong amount", "thanks", "ok then what about monday",
                   "great question", "send it to Daniel instead", "send Meg a reminder",
                   "send it on Friday"]


def test_bare_affirmation_fires():
    for phrase in MUST_AFFIRM:
        assert is_bare_affirmation(phrase), f"should affirm: {phrase!r}"


def test_bare_affirmation_never_fires_on_content():
    for phrase in MUST_NOT_AFFIRM:
        assert not is_bare_affirmation(phrase), f"must NOT affirm: {phrase!r}"


# ── decline: dismisses a pending proposal ────────────────────────────────────
MUST_DECLINE = ["no", "No.", "no thanks", "nah", "not now", "not yet",
                "cancel that", "never mind", "leave it", "forget it",
                "don't", "skip it", "no, cancel them"]
MUST_NOT_DECLINE = ["no idea", "not sure what to do", "stop by the shop later",
                    "no that's fine", "cancel the dentist appointment for tomorrow",
                    "do it", "yes", "never been",
                    "leave the chicken out", "I don't know what we should have"]


def test_bare_decline_fires():
    for phrase in MUST_DECLINE:
        assert is_bare_decline(phrase), f"should decline: {phrase!r}"


def test_bare_decline_never_fires_on_content():
    for phrase in MUST_NOT_DECLINE:
        assert not is_bare_decline(phrase), f"must NOT decline: {phrase!r}"


# ── farewell: hangs up the session ───────────────────────────────────────────
MUST_FAREWELL = ["bye", "goodbye", "cheers then", "that's all thanks",
                 "see ya", "night", "ta ra", "all done", "im good", "that's it"]
MUST_NOT_FAREWELL = ["that's enough sugar in the sauce", "what's for dinner",
                     "goodbye then see you later ok", "no", "yes", "morning",
                     "stop", "night shift tomorrow"]


def test_farewell_fires():
    for phrase in MUST_FAREWELL:
        assert is_voice_farewell(phrase), f"should farewell: {phrase!r}"


def test_farewell_never_fires_on_content():
    for phrase in MUST_NOT_FAREWELL:
        assert not is_voice_farewell(phrase), f"must NOT farewell: {phrase!r}"


def test_decline_and_farewell_overlap_is_deliberate():
    """"no thanks" and "leave it" are in BOTH lists: with a card waiting they
    mean "not that" (decline is checked first in voiceUserTurn), otherwise
    goodbye. The ordering in concierge_panel.js is what this rests on."""
    for phrase in ("no thanks", "leave it"):
        assert is_bare_decline(phrase) and is_voice_farewell(phrase)
    decline_pos = CONCIERGE_JS.index("isBareDecline(text)", CONCIERGE_JS.index("async function voiceUserTurn"))
    farewell_pos = CONCIERGE_JS.index("isVoiceFarewell(text)", CONCIERGE_JS.index("async function voiceUserTurn"))
    assert decline_pos < farewell_pos, "decline must be checked before farewell"


# ── structural guarantees on the port ────────────────────────────────────────
def test_voice_module_keeps_the_hard_won_constants():
    """Every one of these cost the donor a live debugging round. If someone
    "tunes" them, this test makes the change a deliberate one."""
    assert "ECHO_HANGOVER_MS = 350" in VOICE_JS          # half-duplex mic gate tail
    assert "BARGE_CONFIRM_MS = 300" in VOICE_JS          # a cough must not barge in
    assert "export const SETTLE_MS = 600;" in TURNS_JS    # the utterance merger
    assert "MIN_SPEECH_PEAK = 0.06" in VOICE_JS          # the ambient floor's minimum
    assert "autoGainControl: false" in VOICE_JS          # AGC eats first words
    assert "input_audio_buffer.clear" in VOICE_JS        # echo-before-gate purge
    assert "[hidden] { display: none !important; }" in OVERLAY_CSS  # invisible-layer trap
    assert ".setPointerCapture(" not in VOICE_JS         # calling it kills touch swipes


def test_farewell_needs_sustained_speech():
    """The 450ms duration guard: transcription hallucinates short phrases from
    coughs, and a hallucinated goodbye that hangs up is the worst false
    positive. 0 (unknown) must not block a genuine goodbye."""
    m = re.search(r"dur === 0 \|\| dur >= 450", CONCIERGE_JS)
    assert m, "farewell duration guard missing from voiceUserTurn"


def test_the_mic_is_always_there_and_voice_is_on_with_the_key():
    """Kris 2026-09-23 ("COPY THE COMPOSER"): the mic hid behind an env flag
    nobody had flipped, so there was no way into a voice conversation. The mic
    is always in the composer; voice is on wherever the OpenAI key is set; a
    server that can't run it says so when the mic is tapped."""
    assert 'data-voice=' in TEMPLATE and "voice_live_on" in TEMPLATE
    assert "root.getAttribute('data-voice') === 'true'" in CONCIERGE_JS
    assert 'id="cz-mic"' in TEMPLATE
    assert "{% if voice_live_on %}" not in TEMPLATE
    assert "if (VOICE_ON || savedVoiceMode() === 'gptlive') { openVoiceLive(); return; }" in CONCIERGE_JS
    from app.config import Settings
    assert Settings.model_fields["voice_live_enabled"].default is True
    # Voice rides the same thread as typing: spoken turns land in it.
    assert "history.push({ role: entry.role" in CONCIERGE_JS
    # ...and the widget no longer carries a second copy of the matchers.
    widget = (STATIC / "concierge.js").read_text(encoding="utf-8")
    assert "VOICE_AFFIRM_VOCAB" not in widget


def test_standard_live_preserves_captions_and_native_has_its_own_allowance_meter():
    assert "function openCaptions()" in VOICE_JS
    assert 'purpose: "captions"' in VOICE_JS
    assert "vl-allowance" not in VOICE_JS
    assert "function startAllowanceSession(id, snapshot)" in GPTLIVE_JS
    assert "vl-allowance" in GPTLIVE_JS
    assert "session.input_transcript.delta" in GPTLIVE_JS
    assert "deps.caption(message)" in GPTLIVE_JS


def test_history_modes_are_contiguous_and_live_visit_survives_reconnect():
    assert "function paintModeBlocks()" in CONCIERGE_JS
    assert "mode !== previousMode" in CONCIERGE_JS
    # Reconnect metadata survives, but only input-mode changes split backdrops.
    assert "visit !== previousVisit" not in CONCIERGE_JS
    assert "onSessionStart: function (sessionId)" in CONCIERGE_JS
    assert "deps.onSessionStart?.(session)" in GPTLIVE_JS
    history_template = (pathlib.Path(__file__).parent.parent / "src/app" / "templates" / "app" / "_concierge_history.html").read_text(encoding="utf-8")
    assert "data-cz-visit-id" in history_template
    assert "mode_visit_id" in (pathlib.Path(__file__).parent.parent / "src/app" / "concierge_routes.py").read_text(encoding="utf-8")
    assert "VC" in (pathlib.Path(__file__).parent.parent / "src/app" / "templates" / "app" / "_concierge_history.html").read_text(encoding="utf-8") or "vc:'VC'" in CONCIERGE_JS
    assert "vl-ghost-mark" in GPTLIVE_JS
    assert "#voice-live-root .vl-transcript, #voice-live-root .vl-reply { display:none; }" in OVERLAY_CSS


def test_waiting_count_uses_native_only_circle_badge():
    assert "var n = waitingItems.length;" in CONCIERGE_JS
    assert "waitBadge.hidden = !n;" in CONCIERGE_JS
    assert "waitBadge.textContent = waitingOpen && n ? '✕' : String(n);" in CONCIERGE_JS
    assert "if (root.id !== 'voice-gptlive-root') return;" in CONCIERGE_JS
    assert "width: 2.75rem; height: 2.75rem; border-radius: 9999px" in TEMPLATE
    assert "#voice-live-root .cz-debrief, #voice-gptlive-root .cz-debrief" in TEMPLATE


def test_spoken_yes_uses_the_single_use_execute_path():
    """A spoken yes claims the SAME single-use proposal row the Confirm button
    claims — no second execute path exists for voice."""
    voice_block = CONCIERGE_JS[CONCIERGE_JS.index("async function voiceExecuteProposal"):]
    assert "return submitProposal(p," in voice_block[:400]
    shared = CONCIERGE_JS[CONCIERGE_JS.index("async function submitProposal"):][:900]
    assert "/concierge/business/execute" in shared and "X-CSRF-Token" in shared


# ── the composer acts as OurTab's (Kris 2026-09-23) ─────────────────────────
def test_composer_mic_and_send_share_one_slot():
    """Empty box = mic, any text = Send: one 44px slot, toggled by .has-text
    on the form, so the swap never reflows."""
    assert "#cz-form.has-text #cz-mic { display: none; }" in TEMPLATE
    assert "#cz-form:not(.has-text) #cz-send { display: none; }" in TEMPLATE
    assert "width: 44px; height: 44px;" in TEMPLATE
    assert "form.classList.toggle('has-text', Boolean(input.value.trim()));" in CONCIERGE_JS
    # A one-line, 16px field (iOS never zooms it); Enter always sends.
    assert 'wrap="off"' in TEMPLATE and "font-size: 16px" in TEMPLATE
    assert "if (e.key === 'Enter' && !e.isComposing)" in CONCIERGE_JS
    assert "e.shiftKey" not in CONCIERGE_JS
    # Everything waits while an answer is on its way.
    busy = CONCIERGE_JS[CONCIERGE_JS.index("function setComposerBusy"):][:300]
    assert "input.disabled = on" in busy and "send.disabled = on" in busy and "mic.disabled = on" in busy


# ── the voice screen: one screen, as OurTab's (donor b662c74 / 2497f80) ──────
def test_voice_screen_is_one_screen_with_a_cards_sheet():
    assert 'class="vl-live"' in VOICE_JS
    for gone in ("vl-dots", "vl-track", "vl-page", "goToPage", "bindSwipe"):
        assert gone not in VOICE_JS, gone
    assert 'root.classList.toggle("has-cards", Boolean(node));' in VOICE_JS
    assert "#voice-live-root.has-cards .vl-orb" in OVERLAY_CSS
    assert "#voice-live-root .vl-stage-scroll:empty { display: none; }" in OVERLAY_CSS
    # Taps on the cards are the cards', never a barge-in.
    assert 'if (event.target.closest(".vl-stage-scroll")) return;' in VOICE_JS


def test_listening_label_returns_after_speech_stops():
    block = VOICE_JS[VOICE_JS.index('if (type.endsWith("speech_stopped"))'):][:800]
    assert 'if (phase === "listening") setPhase("listening");' in block


def test_voice_screen_exports_notify():
    assert "notify: (text) =>" in VOICE_JS


def test_a_spoken_yes_runs_the_card_as_a_tap_would():
    """A spoken yes sends the card's ticked items and edited draft (a bulk yes
    used to run every item, ticked or not), and settles the card on screen."""
    block = CONCIERGE_JS[CONCIERGE_JS.index("async function voiceExecuteProposal"):][:600]
    assert "selected: c ? c.selected() : null" in block
    assert "draft_text: c ? c.draft() : ''" in block
    assert "selected: function () { return p.kind === 'bulk' ? selectedIdx() : null; }" in CONCIERGE_JS
    turn = CONCIERGE_JS[CONCIERGE_JS.index("async function voiceUserTurn"):]
    assert "card._cz.applyOutcome(data)" in turn


def test_a_tapped_card_lets_go_of_voice():
    """Confirm/Cancel by tap while voice holds the card: voice forgets it (a
    later spoken yes re-ran a used proposal) and the voice screen says Done."""
    tap = CONCIERGE_JS[CONCIERGE_JS.index("function settledByTap"):][:500]
    assert "voiceStage.proposal = null;" in tap
    assert "voiceLiveInstance.notify(" in tap
    assert "settledByTap(card, data);" in CONCIERGE_JS    # Confirm
    assert CONCIERGE_JS.count("settledByTap(card, data);") == 2    # Cancel
    assert r"!/^Done\b/.test(said)" in tap    # an executor receipt already says Done


def test_a_tapped_card_says_done_once():
    """The executors' receipts already read "Done — …"; the voice screen must not
    prefix them again (live 2026-10-14: "Done — Done — payment reminder for INV-1001")."""
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.fail("Node is required to verify the tap receipt text")
    start = CONCIERGE_JS.index("function settledByTap")
    fn = CONCIERGE_JS[start:CONCIERGE_JS.index("\n    }\n", start) + 7]
    cases = [({"ok": True, "summary": "Done — payment reminder for INV-1001 sent."},
              "Done — payment reminder for INV-1001 sent."),
             ({"ok": True, "summary": "2 of 2 items completed."}, "Done — 2 of 2 items completed."),
             ({"ok": True, "summary": ""}, "Done — confirmed"),
             ({"ok": False, "summary": "Nothing changed"}, "Nothing changed"),
             ({"ok": False, "summary": ""}, "Nothing changed")]
    script = ("const assert = require('node:assert/strict'); const vm = require('node:vm');"
              "const said = []; const card = {};"
              "const c = vm.createContext({voiceStage: {cardNode: card, proposal: {}},"
              " voiceLiveInstance: {isOpen: () => true, notify: t => said.push(t)}});"
              "vm.runInContext(" + json.dumps(fn) + ", c);"
              "const cases = JSON.parse(" + json.dumps(json.dumps(cases)) + ");"
              "for (const [data, expected] of cases) { said.length = 0; c.settledByTap(card, data);"
              " assert.deepEqual(said, [expected]); }"
              "said.length = 0; c.settledByTap({}, {ok: true, summary: 'x'}); assert.deepEqual(said, []);")
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_the_ended_conversation_hides_resume_audio():
    assert "deps.status('Conversation ended. Hold Mist to start again; results remain in chat.')" in GPTLIVE_JS
    closed = GPTLIVE_JS[GPTLIVE_JS.index("async function closeOwned(reason) {"):][:260]
    assert "owner = null;deps.playback?.('closed');" in closed
    assert "if(owned){owner=null;++generation;deps.playback?.('closed');}" in GPTLIVE_JS


def test_voice_cards_land_in_the_thread():
    # Closing parks the card in the chat AND lets go of it (clearVoiceCard
    # parks first), so reopening starts clean (Kris 2026-09-23).
    assert "onClose: function (closing)" in CONCIERGE_JS
    assert "waitingOpen = false; doneOpen = false; contextOpen = false; clearVoiceCard(); scrollDown();" in CONCIERGE_JS
    clear = CONCIERGE_JS[CONCIERGE_JS.index("function clearVoiceCard()"):][:600]
    assert "parkVoiceCard();" in clear and "voiceStage.cardNode = null;" in clear
    assert "parkVoiceCard();\n        voiceStage.proposal = payload.proposal;" in CONCIERGE_JS


def test_a_draft_skip_locks_the_card_at_once():
    # Kris 2026-09-23: three taps on ✕ before the server answered = three skips.
    skip = CONCIERGE_JS[CONCIERGE_JS.index("skip: function () {"):][:700]
    assert skip.index("paint('skipping');") < skip.index("postForm(")
    assert ".catch(function () { paint('pending'); return false; });" in skip
    assert "skipping: 'Skipping…'," in CONCIERGE_JS


# ── mute (Kris 2026-09-23: time to read and tap without ending the talk) ─────
def test_mute_silences_the_mic_and_keeps_the_conversation():
    # The mic lives in the orb (Kris 2026-09-23): green pulse live, struck-
    # through with a red pulse muted; its tap never counts as an interrupt.
    assert '<button type="button" class="vl-mic" aria-pressed="false">' in VOICE_JS
    assert "vl-mute" not in VOICE_JS
    assert "@keyframes vl-mic-live" in OVERLAY_CSS and "@keyframes vl-mic-muted" in OVERLAY_CSS
    assert "event.stopPropagation();   // the mic is not a tap-to-interrupt" in VOICE_JS
    mute = VOICE_JS[VOICE_JS.index("function setMuted(next)"):][:600]
    # Mute ONLY switches the mic off (Kris 2026-09-23: "it shouldn't interfere
    # with the working"): the disabled track feeds silence through the normal
    # pipeline — nothing is cleared, dropped, or specially sent.
    assert "t.enabled = !muted" in mute
    assert 'pendingUtterance = "";' not in mute
    assert '"input_audio_buffer.clear"' not in mute
    assert "input_audio_buffer.append" not in mute
    assert "if (muted) { batch = []" not in VOICE_JS
    assert "if (muted && type" not in VOICE_JS
    # The session is NOT torn down: mute never calls stop() or bargeIn().
    assert "stop()" not in mute and "bargeIn()" not in mute
    # The label says so; a tap on the controls is never a barge-in.
    assert "Mic muted — tap the mic when you're ready" in VOICE_JS
    assert 'if (event.target.closest(".vl-controls")) return;' in VOICE_JS
    # A new conversation always starts unmuted.
    assert "muted = false;\n      paintMute();" in VOICE_JS


def test_lookups_are_said_out_loud_while_it_thinks():
    # Kris 2026-09-23: "when it's thinking/searching stuff it is quiet". The
    # chip's hint is spoken; the detail never is (it can be a customer's name).
    tool = VOICE_JS[VOICE_JS.index('if (msg.t === "tool") {'):][:1500]
    assert 'const line = base.replace(/…$/, ".");' in tool
    assert "sayHint(line)" in tool
    assert "speakPhrase(msg.detail" not in VOICE_JS
    # Every chip is said, queued in order behind whatever is playing (Kris
    # 2026-09-23: "we miss some due to the last not being finished"). Only an
    # exact back-to-back repeat is skipped. No cap, no newest-only drop.
    say = VOICE_JS[VOICE_JS.index("const sayHint = (line) => {"):][:400]
    assert "if (!current() || turn.silent || line === lastHint) return;" in say
    assert "ttsChain = ttsChain" in say and "speakPhrase(line, speechSignal)" in say
    assert "saidHints" not in VOICE_JS and "nextHint" not in VOICE_JS
    # The first reply sentence ends the thinking window.
    speak = VOICE_JS[VOICE_JS.index('if (msg.t === "speak"'):][:200]
    assert "turnThinking = false;" in speak
    # When the commentary finishes the orb goes back to thinking, not listening.
    assert 'setPhase(stillThinking ? "thinking" : "listening"' in VOICE_JS
    # Each phrase costs one TTS call a session; repeats play from memory.
    assert "phraseCache.set(cacheKey, chunks)" in VOICE_JS


def test_the_mic_is_shut_until_the_greeting_has_played():
    # Kris 2026-09-23: "the mic is instantly listening, even during the Agent
    # greeting" -> "we just need the mic to not be active until after the
    # greeting". The hold is its own clock (playback's hangover must not be
    # able to shorten it), nothing heard inside it is kept for later, it lifts
    # the moment the greeting ends, and the greeting is one word.
    assert "LAUNCH_HOLD_MS" not in VOICE_JS
    gated = VOICE_JS[VOICE_JS.index("function micGated() {"):][:200]
    assert gated.index("launchHoldUntil) return true;") < gated.index("if (userSpeaking) return false;")
    assert "!liveSources.length && Date.now() >= launchHoldUntil" in VOICE_JS
    onopen = VOICE_JS[VOICE_JS.index("ws.onopen = () => {"):][:2600]   # the greeting-Off ready beat sits above the hold
    assert "launchHoldUntil = Date.now() + GREETING_HOLD_MAX_MS;" in onopen
    assert "const holdLifted = () => { if (current()) launchHoldUntil = 0; };" in onopen
    assert onopen.index("waitForPlaybackEnd(GREETING_HOLD_MAX_MS)") < onopen.index(".then(holdLifted)")
    assert 'return part + ".";' in CONCIERGE_JS


def test_voice_cards_sit_low_with_no_white_sheet():
    # Kris 2026-09-23: the action buttons were "surrounded by a big white box"
    # and the reply was "small and has to be scrolled". The cards sit outside
    # the orb column, just above End; the reply is no longer capped at 6em.
    markup = VOICE_JS[VOICE_JS.index('<div class="vl-live"'):][:1500]
    assert markup.index("</div>\n        <div class=\"vl-stage-scroll\"") < markup.index('<div class="vl-controls">')
    sheet = OVERLAY_CSS[OVERLAY_CSS.index("#voice-live-root .vl-stage-scroll {"):][:300]
    assert "background: #f8fafc" not in sheet
    assert "#voice-live-root.has-cards .vl-reply { max-height: 6em; }" not in OVERLAY_CSS


def test_the_last_reply_stays_on_screen_while_you_answer_it():
    # Kris 2026-09-23: "When i start talking, the Agent's previous message is
    # removed from screen ... it should only leave once the transcript has
    # stopped causing the agent to start." A finished turn was never aborted,
    # so it still counted as busy and speech "barged in" on it and wiped it.
    assert "function turnBusy()" in VOICE_JS
    started = VOICE_JS[VOICE_JS.index('if (type.endsWith("speech_started"))'):][:2000]
    assert "const busy = liveSources.length > 0 || turnBusy();" in started
    assert "turnAbort && !turnAbort.signal.aborted);" not in started
    # runTurn marks a turn active for exactly its lifetime, however it ends
    run = VOICE_JS[VOICE_JS.index("async function runTurn("):][:300]
    assert "activeTurn = mine;" in run and "finally" in run
    assert "if (activeTurn === mine) activeTurn = null;" in run
    assert "runTurn(u.text, meta);" in VOICE_JS
    # an interruption stops the voice, never the words on screen
    assert "if (bargeIn()) els.reply.textContent" not in VOICE_JS
    # the reply clears at answer start/open or accepted discard (ACK or SSE)
    assert VOICE_JS.count('els.reply.textContent = "";') == 4


def test_an_ellipsis_with_no_words_never_sticks():
    # Kris 2026-09-23: the transcript turned into "…" and stayed — no words
    # came back and nothing reached the server. Every dead end is an
    # utterance now (the assembler reports empty ones too), and it clears.
    heard = VOICE_JS[VOICE_JS.index("function heardUtterance(u) {"):][:700]
    assert 'if (els.transcript.textContent === "…") els.transcript.textContent = "";' in heard
    assert "notify(\"Didn't catch that — say it again\");" in heard
    assert 'logHeard("nothing", u, u.why);' in heard
    # a segment the server never closes is let go, not waited on for ever
    assert "export const ORPHAN_MS = 8000;" in TURNS_JS


BASE = (pathlib.Path(__file__).parent.parent / "src/app" / "templates" / "app"
        / "base.html").read_text(encoding="utf-8")


def test_settings_cog_holds_the_voice_line_and_the_tour():
    # Kris 2026-09-23: a cog in the Concierge bar with the tour and a threshold
    # slider over a live room meter.
    assert "data-cz-settings" in BASE
    assert 'id="cz-settings"' in TEMPLATE and 'id="cz-mic-line"' in TEMPLATE
    assert 'id="cz-tour-start"' in TEMPLATE and "cz-meter-room" in TEMPLATE
    # One key, both sides: the sheet writes it, voice reads it each start.
    assert "var MIC_LINE_KEY = 'blam8.voiceMicLine';" in CONCIERGE_JS
    assert 'export const MIC_LINE_KEY = "blam8.voiceMicLine";' in VOICE_JS
    assert "micLine = savedMicLine();" in VOICE_JS
    # The sheet's automatic line mirrors voice's automatic gate.
    assert "AUTO_FLOOR = 0.06, AUTO_OVER_ROOM = 4" in CONCIERGE_JS
    assert "MIN_SPEECH_PEAK = 0.06" in VOICE_JS and "SPEECH_OVER_AMBIENT = 4" in VOICE_JS
    # The meter hears what voice hears (AGC off, same constraints).
    assert CONCIERGE_JS.count("autoGainControl: false") >= 1
    # A set line IS the gate; unset keeps the rolling-room rule.
    gate = VOICE_JS[VOICE_JS.index("function speechGate("):][:300]
    assert "if (micLine) return micLine * (margin / SPEECH_OVER_AMBIENT);" in gate
    assert "Math.max(ambientLevel * margin, MIN_SPEECH_PEAK)" in gate
    # The mic is off whenever the sheet is: closing the Concierge closes it.
    assert "if (api) api.hideSettings();" in CONCIERGE_JS
    assert "m.stream.getTracks().forEach(function (t) { t.stop(); });" in CONCIERGE_JS


def test_the_tour_moved_to_the_cog():
    assert 'class="vl-tour-chip"' not in VOICE_JS
    assert "startTour: () => { if (root && open) openTour(); }," in VOICE_JS
    assert "voiceLiveInstance.startTour()" in CONCIERGE_JS


def test_settings_cards_follow_the_voice_mode():
    """15 Oct: the cog showed Voice, Voice volume, Greeting and the line card in GPT-Live mode, though
    GPT-Live reads none of them. Each card now says which voice mode uses it (data-cz-voices) and the
    sheet paints only those, whenever it opens and whenever the mode radio changes."""
    import shutil
    import subprocess
    import pytest
    cards = re.findall(r'<section class="cz-set-sec"([^>]*)>\s*<p[^>]*class="cz-set-h"[^>]*>([^<]+)</p>', TEMPLATE)
    assert len(cards) == TEMPLATE.count('<section class="cz-set-sec"') == 9, cards
    wanted = {}
    for attributes, heading in cards:
        found = re.search(r'data-cz-voices="(standard|gptlive|both)"', attributes)
        assert found, "every card says which voice mode uses it: " + heading
        wanted[heading.strip()] = found.group(1)
    assert wanted == {"How loud counts as you": "both", "How you talk": "gptlive", "Conversation mode": "both",
                      "Voice": "standard", "Voice volume": "standard", "Greeting": "standard", "Voice tour": "both", "Launch by button": "both", "Open on the voice screen": "both"}
    # the line card's old copy said quiet sound "is ignored"; Standard holds the sentence and asks for it again
    assert "anything quieter than the line is ignored" not in TEMPLATE
    assert "quieter than the line is held, and the screen asks you to repeat it" in TEMPLATE
    # painted in both places that can change what is wanted: opening the sheet and switching the mode
    assert CONCIERGE_JS.count("paintModeSections();") == 2
    handler = CONCIERGE_JS.split("radio.addEventListener('change', function () {\n        if (!radio.checked || ['standard', 'gptlive']", 1)[1].split("closing.then(", 1)[0]
    assert "paintModeSections();" in handler and "stopMeter();" in handler
    show = CONCIERGE_JS.split("    function showSettings(on) {", 1)[1].split("\n    }\n", 1)[0]
    assert "paintModeSections();" in show and "startMeter();" in show
    # run the real function: a card is hidden unless it is for the mode in use or for both
    node = shutil.which("node")
    if not node:
        pytest.fail("Node is required to verify the settings cards")
    body = CONCIERGE_JS.split("    function paintModeSections() {", 1)[1].split("\n    }\n", 1)[0]
    functions = "function paintModeSections() {" + body + "\n}\n"
    script = """
      const assert = require('node:assert/strict');
      const vm = require('node:vm');
      const card = wants => ({hidden: null, getAttribute: name => (name === 'data-cz-voices' ? wants : null)});
      const nodes = ['standard', 'both', 'gptlive'].map(card);
      const sheet = {querySelectorAll: selector => { assert.equal(selector, '[data-cz-voices]'); return nodes; }};
      let mode = 'standard';
      const c = vm.createContext({sheet, savedVoiceMode: () => mode});
      vm.runInContext(""" + json.dumps(functions) + """, c);
      c.paintModeSections();
      assert.deepEqual(nodes.map(n => n.hidden), [false, false, true]);
      mode = 'gptlive'; c.paintModeSections();
      assert.deepEqual(nodes.map(n => n.hidden), [true, false, false]);
      mode = 'standard'; c.paintModeSections();
      assert.deepEqual(nodes.map(n => n.hidden), [false, false, true]);
      c.sheet = null; nodes.forEach(n => { n.hidden = null; }); c.paintModeSections();
      assert.deepEqual(nodes.map(n => n.hidden), [null, null, null]);
    """
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_muting_while_getting_ready_still_mutes_the_mic():
    # Kris 2026-09-23: muted on opening, walked away, and the room became a
    # turn — setMuted had no tracks yet, so the mic came up live.
    start = VOICE_JS[VOICE_JS.index("const acquisition = navigator.mediaDevices.getUserMedia("):][:2200]
    apply = start.index("micStream.getAudioTracks().forEach((t) => { t.enabled = !muted; });")
    assert apply < start.index("source.connect(capture);")


def test_the_line_only_moves_under_your_finger():
    # Kris 2026-09-23 (iOS): the "automatic" line followed the room and pulled
    # the thumb away mid-drag — "automatic is pointless". No Automatic button;
    # an unset line is placed ONCE, then only a drag moves it.
    assert 'id="cz-mic-auto"' not in TEMPLATE
    assert "autoBtn" not in CONCIERGE_JS
    meter = CONCIERGE_JS[CONCIERGE_JS.index("function startMeter()"):][:4500]
    assert meter.count("placeLine(") == 3          # the def, the start, the one-off
    assert "placeLine(Math.max(m.room * AUTO_OVER_ROOM, AUTO_FLOOR));   // once" in meter
    assert "m.placedAt = 1;" in meter
    assert "slider.value = " in meter.split("function placeLine(")[1][:300]


def test_a_sound_while_thinking_never_cancels_the_question():
    # Kris 2026-09-23: "heard a little sound in the background n cancelled the
    # transcript" — a clatter while the answer was being worked on barged in
    # and threw the question away. Sound alone only stops the VOICE.
    timer = VOICE_JS[VOICE_JS.index("bargeTimer = setTimeout(() => {"):][:1400]
    assert "if (!liveSources.length) return;" in timer
    assert timer.index("if (!liveSources.length) return;") < timer.index("bargeIn();")
    words = VOICE_JS[VOICE_JS.index("// Real words while we were counting down a barge"):][:400]
    assert "if (liveSources.length) bargeIn();" in words
    # Words decide: a real sentence (or a plain stop/cancel/wait/no) becomes
    # the next turn, which cancels the one still running; a stray word is
    # HELD on screen (tap to send), never thrown away (2026-09-24).
    heard = VOICE_JS[VOICE_JS.index("function heardUtterance(u) {"):][:1600]
    assert 'if (turnBusy() && phase === "thinking" && !meansToInterrupt(u.text)) { holdWords(u, "mid_thought"); return; }' in heard
    run = VOICE_JS[VOICE_JS.index("async function runAssistantTurn("):][:1600]
    assert "if (turnAbort && !turnAbort.signal.aborted) turnAbort.abort();" in run
    assert run.index("turnAbort.abort();") < run.index("const controller = new AbortController();")


def test_what_counts_as_meaning_to_interrupt():
    src = VOICE_JS[VOICE_JS.index("const INTERRUPT_WORDS"):][:400]
    for w in ("stop", "cancel", "wait", "no", "actually"):
        assert f'"{w}"' in src
    assert "words.length >= 2" in src


def test_a_spoken_yes_is_answered_on_the_spot():
    # Kris 2026-09-23 18:15: the yes confirmed Bella's card, nothing was said
    # (the follow-up model turn never came), and it read as a yes that failed.
    # The result the change returned is spoken straight back; no second turn.
    assert "return { confirmed: !!data.ok, answered: true, ack: said };" in CONCIERGE_JS
    assert "return { confirmed: sent, answered: true, ack: line };" in CONCIERGE_JS
    block = VOICE_JS[VOICE_JS.index("if (outcome.answered) {"):][:800]
    assert "await (LOCAL_LINES.includes(outcome.ack) ? speakPhrase(outcome.ack, signal)" in block
    assert ": streamTtsSentence(outcome.ack, signal)).catch(() => {});" in block
    assert block.index("return;") < len(block)
    # ...and it comes before the model turn is started
    assert VOICE_JS.index("if (outcome.answered) {") < VOICE_JS.index("turnThinking = true;\n    thinkingNote")


def test_her_voice_has_a_volume_with_a_limiter():
    # Kris 2026-09-23: "no volume control for the Agent voice. She's very quiet
    # and my phone is full volume".
    assert 'export const VOICE_VOLUME_KEY = "blam8.voiceVolume";' in VOICE_JS
    assert "export const VOICE_VOLUME_DEFAULT = 1.6;" in VOICE_JS
    assert "var VOICE_VOLUME_KEY = 'blam8.voiceVolume', VOICE_VOLUME_DEFAULT = 1.6;" in CONCIERGE_JS
    play = VOICE_JS[VOICE_JS.index("function ensurePlayCtx()"):][:900]
    assert "createGain()" in play and "createDynamicsCompressor()" in play
    assert "playGain.connect(limiter);" in play and "limiter.connect(playCtx.destination);" in play
    # every sentence goes through it; each conversation reads the latest setting
    assert "src.connect(playGain || ctx.destination);" in VOICE_JS
    assert "voiceVolume = savedVoiceVolume();" in VOICE_JS
    assert 'id="cz-voice-vol"' in TEMPLATE and 'min="50" max="300"' in TEMPLATE


def test_pull_down_on_the_voice_screen_refreshes_and_comes_back():
    # Kris 2026-09-23: "add the pull down to refresh to the VC" — the overlay
    # sat above the app's pull, so every fresh deploy meant leaving voice.
    # Only the orb column moves (Kris 2026-09-24): End conversation and the
    # top-left badges stay locked in place.
    assert '<div class="vl-live" data-pull-bar data-pull-moves="#voice-live-root .vl-live" data-pull-reopen="voice">' in VOICE_JS
    assert '<div class="vl-stage-scroll" data-pull-refresh="off"></div>' in VOICE_JS   # the cards scroll
    assert '<p class="vl-reply" data-pull-refresh="off"></p>' in VOICE_JS             # so does a long reply
    assert "#voice-live-root #pull-refresh-label { z-index: 95;" in OVERLAY_CSS           # the label, inside the overlay
    # the reload lands on #voice: the Concierge reopens and says to tap the mic
    # (a fresh page has no tap behind it — iOS opens no mic and plays no sound)
    assert "if (location.hash === '#concierge' || location.hash === '#voice' || location.hash === '#talk') {" in CONCIERGE_JS
    assert "if (wantVoice) load().then(function (a) {\n      if (a) { if (wantTalk) a.afterTalkLaunch(); else a.afterVoiceRefresh(); }" in CONCIERGE_JS
    assert "bubble('assistant', 'Refreshed — tap the mic to carry on talking.');" in CONCIERGE_JS
    assert CONCIERGE_JS.count("location.hash === '#voice'") == 3   # modal frame (test + wantVoice) + page frame


def test_the_refresh_lands_back_on_a_paused_voice_screen_with_its_label():
    # Kris 2026-09-23: "no text for the refresh being shown … then it loads the
    # Concierge text chat again. It's not smooth at all."
    # The label rides inside the overlay while voice is open (z-index alone
    # didn't put it above the overlay on iOS), and goes back on close.
    op = VOICE_JS[VOICE_JS.index("    open(opts) {"):][:1400]
    assert 'const pullLabel = document.getElementById("pull-refresh-label");' in op
    assert "if (pullLabel) root.appendChild(pullLabel);" in op
    assert 'if (opts && opts.paused) { setPhase("paused"); return; }' in op
    st = VOICE_JS[VOICE_JS.index("function stop() {"):][:700]
    assert "if (pullLabel) document.body.appendChild(pullLabel);" in st
    # paused: the orb waits for the tap that iOS needs before mic and sound
    assert 'paused: "Refreshed — tap the orb to carry on",' in VOICE_JS
    assert VOICE_JS.count('if (phase === "paused") { start(); return; }') == 2
    assert "if (VOICE_ON) { resurfaceNewestOnLoad = true; openVoiceLive({ paused: true }); return; }" in CONCIERGE_JS
    assert "voiceLiveInstance.open(opts);" in CONCIERGE_JS


def test_straight_into_voice_with_no_flash_of_the_chat():
    # Kris 2026-09-23: "load straight into VC without seeing a second of
    # Concierge text? Same for press n hold to VC".
    base = open("src/app/templates/app/base.html", encoding="utf-8").read()
    assert "if (location.hash === '#voice' || location.hash === '#talk') document.documentElement.classList.add('voice-landing');" in base
    assert "html.voice-landing body > *:not(#voice-live-root) { visibility: hidden; }" in base
    assert '<link rel="modulepreload" href="/static/voice-live.js?v={{ asset_rev }}">' in base
    # the veil is on while voice is on its way and off once it's up — or can't be
    ov = CONCIERGE_JS.split("async function openVoiceLive(opts) {", 1)[1].split("async function openVoiceLiveInner", 1)[0]
    assert "veil(true);" in ov and "} finally {\n        veil(false);" in ov
    assert "savedVoiceMode() === 'standard' && !VOICE_ON" in ov
    # press-and-hold veils the instant it fires, in both frames
    assert CONCIERGE_JS.count("document.documentElement.classList.add('voice-landing');") == 2
    assert "else document.documentElement.classList.remove('voice-landing');   // no panel: show the page" in CONCIERGE_JS


def test_after_listening_stopped_the_mic_reconnects_in_place():
    # Kris 2026-09-23: "Came back n it said listening stopped, would be good if
    # I could click the mic button to continue."
    assert 'setPhase("error", "Listening stopped — tap the mic to carry on.");' in VOICE_JS
    assert VOICE_JS.count('if (phase === "error") { reconnect(); return; }') == 2   # orb + mic
    r0 = VOICE_JS.index("async function reconnect() {")
    rc = VOICE_JS[r0:VOICE_JS.index("  function stop() {", r0)]
    # the mic and the neural ear keep running; only the connection is replaced
    # (rebuilding the audio left iOS "listening" with a mic that sent nothing)
    assert 'micStream.getAudioTracks().some((t) => t.readyState === "live" && !t.muted)' in rc
    assert "openRealtime(token);" in rc
    assert "if (ws) { try { ws.close(1000); }" in rc
    # a mic that has actually died gets the full start; never the screen's close
    assert "teardownAudio();" in rc and "start();" in rc
    assert "is-open" not in rc and "onClose" not in rc
    # start() and reconnect() open the connection the same way
    assert VOICE_JS.count("openRealtime(token);") == 2
    assert "function attachRealtimeHandlers() {" in VOICE_JS


def test_a_turn_whose_server_goes_quiet_is_given_up_not_held_forever():
    # Kris 2026-09-23: "it got stuck thinking for ages" — a deploy restarted
    # the server under the request and nothing ever came back.
    assert "const TURN_STALL_MS = 45000;" in VOICE_JS
    guard = VOICE_JS[VOICE_JS.index("function stallGuard(promise, ms) {"):][:400]
    assert 'reject(new Error("stalled"))' in guard and "Promise.race([promise, stall])" in guard
    assert 'await stallGuard(deps.apiFetch("chat", body, signal), TURN_STALL_MS);' in VOICE_JS
    assert "await stallGuard(reader.read(), TURN_STALL_MS);" in VOICE_JS
    assert 'stalled ? "That took too long — check the thread before trying again." : "Lost the connection — check the thread and waiting list."' in VOICE_JS


def test_back_from_the_background_the_audio_engine_is_checked_not_assumed():
    # Kris 2026-09-23: "If I leave the app, come back then talk when it tells
    # me, it doesn't listen? Even after tapping to continue". iOS parks the
    # audio engine and mutes the track; the track still reads "live".
    r0 = VOICE_JS.index("async function reconnect() {")
    rc = VOICE_JS[r0:VOICE_JS.index("  function stop() {", r0)]
    assert 'micCtx.state === "running"' in rc and 't.readyState === "live" && !t.muted' in rc
    # a rebuild starts with NO await between the tap and start() (iOS won't
    # run an engine made after the tap has lapsed)
    rebuild = rc[rc.index("if (!micAlive) {"):][:200]
    assert "await" not in rebuild and "start();" in rebuild
    # coming back to the foreground with anything parked says to tap
    assert 'document.addEventListener("visibilitychange", () => {' in VOICE_JS
    assert 'if (parked) setPhase("error", "Back — tap the mic to carry on.");' in VOICE_JS
    # the on-phone detector that could gate a woken mic into silence is gone
    assert "neural" not in VOICE_JS.lower().replace("neural ear", "")


def test_after_the_background_or_a_dead_track_the_tap_rebuilds_everything():
    # Kris 2026-09-23 23:4x: unlocked the phone, tapped the mic, "nothing gets
    # transcribed" — the track read live and unmuted and delivered silence.
    assert "const DEAD_MIC_MS = 2000;" in VOICE_JS
    assert 'if (document.visibilityState === "hidden") { wasHidden = true; if (ripples) ripples.pause(); return; }' in VOICE_JS
    r0 = VOICE_JS.index("async function reconnect() {")
    rc = VOICE_JS[r0:VOICE_JS.index("  function stop() {", r0)]
    assert "const micAlive = !wasHidden && !zeroSince && micStream" in rc
    handler = VOICE_JS[VOICE_JS.index("capture.port.onmessage = (event) => {"):][:900]
    assert 'setPhase("error", "The mic went quiet — tap the mic to carry on.");' in handler
    assert "if (!allZero) zeroSince = 0;" in handler


def test_a_reconnect_shows_the_diagnostic_line():
    # Kris 2026-09-24: still deaf after an unlock — the line says which part is silent.
    assert "function showDebug(forMs) {" in VOICE_JS and "if (els.debug.hidden) showDebug(0); else hideDebug();" in VOICE_JS
    paint = VOICE_JS[VOICE_JS.index("function paintDebug() {"):][:1200]
    for word in ("mic ${fps} f/s", "engine ${micCtx ? micCtx.state", "track ${track ? track.readyState", "link ${wsState}", "sent ${(dbgSent / 1024)"):
        assert word in paint, word


def test_a_sentence_held_by_room_noise_is_sent_after_a_pause():
    # Kris 2026-09-24: "it won't even keep the first sentence I say, it looks
    # like it's trying to hear more" — the service heard the room as speech
    # and never said "stopped". Replayed in tests/replays/voice_replay.mjs.
    assert "export const NOISE_HOLD_MAX_MS = 2500;" in TURNS_JS
    assert 'emit(doneIds, "noise_hold")' in TURNS_JS


def test_the_mic_engine_is_made_inside_the_tap_before_any_await():
    # Kris 2026-09-24 00:19, the diagnostic line after an unlock + tap:
    # "mic 0 f/s · engine running · track live · link open" — an engine made
    # after the token fetch and getUserMedia had waited never ran.
    st = VOICE_JS[VOICE_JS.index("  async function start() {"):]
    first_await = st.index("await ")
    made = st.index('if (!micCtx || micCtx.state === "closed") micCtx = new AudioContext({ sampleRate: SAMPLE_RATE });')
    assert made < first_await
    assert 'if (micCtx.state !== "running") micCtx.resume().catch(() => {});' in st[:first_await]
    assert "if (!current() || micCtx !== engine) {" in st
    assert st.count("new AudioContext({ sampleRate: SAMPLE_RATE })") == 1   # not a second one after getUserMedia


def test_back_from_the_background_the_tap_reloads_into_voice():
    # Kris 2026-09-24: in-place rebuilds after an unlock stayed deaf three
    # times over; a fresh page has never failed. Reliable beats clever.
    r0 = VOICE_JS.index("async function reconnect() {")
    rc = VOICE_JS[r0:VOICE_JS.index("  function stop() {", r0)]
    assert "if (wasHidden || zeroSince) { reloadIntoVoice(); return; }" in rc
    rl = VOICE_JS[VOICE_JS.index("function reloadIntoVoice() {"):][:300]
    assert '+ "#voice"' in rl and "window.location.reload();" in rl


def test_a_reload_keeps_the_conversations_memory_and_the_card_it_left():
    # Kris 2026-09-24: "the only issue would be losing context on refresh if
    # user is in the middle of a task when the phone locks".
    src = open("src/app/concierge_routes.py", encoding="utf-8").read()
    assert "BusinessHistoryResponse(messages=messages" in src
    assert 'id,role,content,links,trail,created_at,user_id' in open("src/app/db.py", encoding="utf-8").read()
    seed = CONCIERGE_JS[CONCIERGE_JS.index("function seedHistory() {"):][:1200]
    assert "fetch('/concierge/business/history?context_only=true'" in seed
    assert "if (history.length) return;   // a turn already happened here: it wins" in seed
    assert ".slice(-HISTORY_SEED_TURNS)" in seed and "lookups: lookupsOf(m.trail)" in seed
    assert "var CONTEXT_ROUNDS = 8, CONTEXT_TURNS = CONTEXT_ROUNDS * 2;" in CONCIERGE_JS
    assert "var HISTORY_RECENT_MS = 3 * 60 * 60 * 1000, HISTORY_SEED_TURNS = CONTEXT_TURNS;" in CONCIERGE_JS
    assert "loadWaiting();\n    seedHistory();\n" in CONCIERGE_JS
    # the reload landing brings back the card set up in the last ten minutes
    assert "if (VOICE_ON) { resurfaceNewestOnLoad = true; openVoiceLive({ paused: true }); return; }" in CONCIERGE_JS
    assert "if (fresh.length) resurface(fresh[0]);" in CONCIERGE_JS


def test_a_one_word_yes_is_measured_from_before_speech_started():
    # Kris 2026-09-24: "When I say Yes to send the email, it shows the word in
    # transcript then it removes it n does nothing." The segment's loudness
    # reaches back 1.5 s (replayed in tests/replays/voice_replay.mjs); every frame's
    # loudness goes to the assembler, and no gate sits in front of it.
    assert "export const RECENT_PEAK_MS = 1500;" in TURNS_JS
    assert "s.peak = Math.max(s.peak, recentPeak(now));" in TURNS_JS
    assert "turns.notePeak(peak, Date.now());" in VOICE_JS


def test_nothing_heard_is_ever_thrown_away_in_silence():
    # reports/Voice agent turn taking practices.md, option 1 (Kris 2026-09-24).
    heard = VOICE_JS[VOICE_JS.index("function heardUtterance(u) {"):][:1600]
    # an answer to the card on screen goes first — no loudness test
    ans = heard.index("if (deps.expectsAnswer && deps.expectsAnswer(u.text)) {")
    assert ans < heard.index("if (u.peak < speechGate())")
    # too quiet is HELD, visibly, and a tap sends it
    assert 'if (u.peak < speechGate()) { holdWords(u, "quiet"); return; }' in heard
    assert "if (heldWords) { event.stopPropagation(); sendHeld(); return; }" in VOICE_JS
    assert '#voice-live-root .vl-transcript.is-held::after {' in OVERLAY_CSS and 'content: "Tap to send";' in OVERLAY_CSS
    # every outcome is logged
    assert 'deps.apiFetch("heard", {' in VOICE_JS
    for outcome in ('logHeard("sent", u, "answer");', 'logHeard("held", u, reason);',
                    'logHeard("nothing", u, u.why);', 'logHeard("struck",'):
        assert outcome in VOICE_JS, outcome
    # the panel knows what an answer is
    assert "expectsAnswer: function (text) {" in CONCIERGE_JS
    assert "return waiting && (isBareAffirmation(text) || isBareDecline(text));" in CONCIERGE_JS
    # no gate in front of the speech service
    assert "neuralActiveUntil" not in VOICE_JS and "startNeuralVad" not in VOICE_JS
    # one owner for "finished", keyed by item id
    assert "const itemId = msg.item_id || \"\";" in VOICE_JS
    assert "turns.completed({ itemId, transcript: msg.transcript, now });" in VOICE_JS
    assert "pendingUtterance" not in VOICE_JS and "scheduleUtteranceCommit" not in VOICE_JS


def test_the_turn_replays_pass():
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not installed")
    r = subprocess.run([node, "tests/replays/voice_replay.mjs"], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr


def test_end_conversation_sits_low():
    # Kris 2026-09-24: "lower the End conversation button so that it's only
    # got a little padding below. Gives more space for the bottom third."
    assert "margin-bottom: max(0.5rem, calc(env(safe-area-inset-bottom) - 1.1rem));" in OVERLAY_CSS
    assert "margin-bottom: max(1.1rem, env(safe-area-inset-bottom));" not in OVERLAY_CSS



def test_the_words_appear_with_the_voice():
    # Kris 2026-09-24: "the text reply from the Agent appears a couple seconds
    # before the TTS fires". Each sentence is requested the moment it's ready
    # (not after the last one downloaded) and goes on screen as its audio starts.
    speak = VOICE_JS[VOICE_JS.index("const speak = (sentence) => {"):][:900]
    assert "const buf = fetchTts(sentence, speechSignal);          // requested NOW" in speak
    assert ".then(() => { if (current() && !turn.silent) return playTts(buf, speechSignal, reveal); })" in speak
    assert "// Not painted here: each sentence appears as its voice starts." in VOICE_JS
    assert "els.reply.textContent = spokenSoFar;\n            const { sentences, rest }" not in VOICE_JS
    play = VOICE_JS[VOICE_JS.index("async function playTts(buf, signal, onStart) {"):][:500]
    assert "if (!started) { started = true; if (onStart) onStart(); }" in play
    # the phone's fixed answers are fetched once at the start and replayed
    assert "const LOCAL_LINES = [\"Sent.\", \"You're welcome.\"," in VOICE_JS
    assert "setTimeout(() => { if (current()) LOCAL_LINES.forEach(warmPhrase); }, hello ? 2500 : 0);" in VOICE_JS
    assert "await speakPhrase(line, audioAbort.signal)" in VOICE_JS


# ── The ripples and the Greeting setting (docs/spec-voice-ripples.md, 2026-09-30) ──
RIPPLES_JS = (STATIC / "voice-ripples.js").read_text(encoding="utf-8")


def test_greeting_is_a_per_device_setting_that_defaults_to_off():
    # Decision 1: Off for everyone, existing devices included (no stored value = Off).
    assert "var VOICE_GREETING_KEY = 'blam8.voiceGreeting';" in CONCIERGE_JS
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.fail("Node is required to verify the greeting setting")
    helpers = CONCIERGE_JS.split("    var VOICE_GREETING_KEY =", 1)[1].split("    function veil(", 1)[0]
    script = """
      const assert = require('node:assert/strict');
      const vm = require('node:vm');
      const values = new Map();
      const localStorage = {getItem: k => values.get(k), setItem: (k,v) => values.set(k,v)};
    """ + "const helpers = " + json.dumps("var VOICE_GREETING_KEY =" + helpers) + ";" + """
      function panel() { const c = vm.createContext({localStorage, Date}); vm.runInContext(helpers, c); return c; }
      assert.equal(panel().savedVoiceGreeting(), 'off');       // nothing stored = Off
      assert.equal(panel().voiceGreeting(), '');                 // Off = no TTS at all
      assert.equal(panel().saveVoiceGreeting('on'), true);
      assert.equal(panel().savedVoiceGreeting(), 'on');
      assert.match(panel().voiceGreeting(), /^(Morning|Afternoon|Evening)\.$/);
      values.set('blam8.voiceGreeting', 'yes');                  // anything but 'on' is Off
      assert.equal(panel().savedVoiceGreeting(), 'off');
      localStorage.getItem = () => { throw Error('blocked'); };
      localStorage.setItem = () => { throw Error('blocked'); };
      assert.equal(panel().savedVoiceGreeting(), 'off');
      assert.equal(panel().voiceGreeting(), '');
      assert.equal(panel().saveVoiceGreeting('on'), false);
    """
    subprocess.run([node, "-e", script], check=True)
    assert 'name="cz-greeting" value="off"' in TEMPLATE and 'name="cz-greeting" value="on"' in TEMPLATE
    # the morning BRIEF's regex is a different thing and stays as it was
    assert "if (!/^\s*(Morning|Afternoon)\./.test(last.content || '')) return;" in CONCIERGE_JS


def test_greeting_off_opens_the_mic_after_the_ready_ring_with_no_tts():
    block = VOICE_JS[VOICE_JS.index("ws.onopen = () => {"):][:2600]
    off = block[block.index("if (!hello) {"):block.index("launchHoldUntil = Date.now() + GREETING_HOLD_MAX_MS;")]
    assert "streamTtsSentence" not in off and "speakPhrase" not in off   # no TTS at all
    assert "if (ripples) ripples.ready(); playReadySound();" in off
    # the sound plays BEFORE the mic opens: a fixed hold, then "listening"
    assert "const holdMs = READY_STILL_MS + READY_SOUND_MS + READY_QUIET_MS;" in off
    assert "launchHoldUntil = Date.now() + holdMs;" in off
    assert 'if (open && phase === "connecting") setPhase("listening");' in off
    assert "const READY_QUIET_MS = 300;" in VOICE_JS
    # greeting On: the ready ring waits for the spoken word, and no water sound
    on = block[block.index("launchHoldUntil = Date.now() + GREETING_HOLD_MAX_MS;"):]
    assert "if (open && ripples) ripples.ready();" in on and "playReadySound" not in on
    # the sound: synthesised, through the playback gain (the saved volume) and never while muted
    sound = VOICE_JS[VOICE_JS.index("function playReadySound()"):][:1600]
    assert "if (muted || !open) return;" in sound
    assert "const out = playGain || ctx.destination;" in sound
    assert "osc.frequency.setValueAtTime(220, t0);" in sound and "exponentialRampToValueAtTime(140, t0 + 0.18);" in sound
    assert 'band.type = "bandpass"' in sound


def test_ripples_are_a_canvas_behind_the_orb_with_a_reduced_motion_fallback():
    assert 'const { createRipples } = await import("./voice-ripples.js"' in VOICE_JS
    assert 'canvas.setAttribute("aria-hidden", "true");' in RIPPLES_JS
    assert "#voice-live-root .vl-ripples { position: absolute; inset: 0; z-index: 0; pointer-events: none; }" in OVERLAY_CSS
    # reduced motion: no particles, the orb glow pulses instead
    assert 'window.matchMedia("(prefers-reduced-motion: reduce)")' in RIPPLES_JS
    assert "if (reducedMotion()) return createPulseFallback(root);" in RIPPLES_JS
    assert "#voice-live-root.vl-pulse .vl-orb { animation: vl-glow-pulse 700ms ease-out; }" in OVERLAY_CSS
    # the phases drive it: drop on connecting, one red ring on error, still when paused
    assert 'if (next === "connecting" && before !== "connecting") ripples.drop();' in VOICE_JS
    assert 'else if (next === "error") ripples.error();' in VOICE_JS
    # backgrounded: the loop stops and no backlog plays on return
    assert 'if (document.visibilityState === "hidden") { wasHidden = true; if (ripples) ripples.pause(); return; }' in VOICE_JS
    assert "for (const r of rings) r.alive = false;    // rings born before the sleep would all arrive at once" in RIPPLES_JS
    # the loop runs only while a ring is alive, and everything goes on session end
    assert "if (any || drive) raf = requestAnimationFrame(frame);" in RIPPLES_JS
    assert "if (ripples) { ripples.destroy(); ripples = null; }" in VOICE_JS
    # the timeline that keeps it from feeling rushed (spec §3)
    assert "const RING_MS = 2600;" in RIPPLES_JS and "const CADENCE_MS = 1150;" in RIPPLES_JS
    assert "const PEAKS = [1, 0.8, 0.65, 0.55];" in RIPPLES_JS
    assert "const READY_STILL_MS = 250;" in RIPPLES_JS
    assert 'emit({ radius: 1.35, alpha: 1, hue: "green", speed: 1 / 1.25, priority: true })' in RIPPLES_JS
    # a slow-device fallback thins the loading cadence only; ready and error always go out
    assert "if (lastFrame && now - lastFrame < 100) {" in RIPPLES_JS
    assert "if (!opts.priority) {" in RIPPLES_JS
    # in time with the orb (Kris 30 Sep): every ring is born as the green ring's CSS pulse peaks;
    # the error is a double-beat that keeps going until the phase moves on; a wave has depth
    assert "function pulsePeakIn(minMs = 0) {" in RIPPLES_JS
    assert 'anims.find((x) => /^vl-ring-/.test(x.animationName || ""))' in RIPPLES_JS
    assert "cadence = setTimeout(nextLoadingRing, pulsePeakIn(CADENCE_MS));" in RIPPLES_JS
    assert "cadence = setTimeout(() => errorBeat(!second), second ? pulsePeakIn(ERROR_REST_MS) : ERROR_BEAT_MS);" in RIPPLES_JS
    assert "const rad = r - d * band + ring.radJ[i] * Math.sin(drift + ring.phase[i]);" in RIPPLES_JS
    # born at the green ring, read at each birth, not at the button
    assert "r0 = (rr && rr.width ? rr.width : or.width) / 2;" in RIPPLES_JS
    assert "relayout();                       // the ring's edge as it is now" in RIPPLES_JS
    assert 'ctx.clip("evenodd");' in RIPPLES_JS   # nothing painted inside the ring
    # the ring slides into the wave's colour at each birth, then eases back (Kris 30 Sep)
    assert "border: 2px solid var(--vl-ring);" in OVERLAY_CSS
    assert '@property --vl-glow { syntax: "<color>"; inherits: true; initial-value: rgb(34 197 94); }' in OVERLAY_CSS
    assert "#voice-live-root.vl-ring-tint .vl-orb span { transition: transform 0.09s linear, border-color 0.3s ease, --vl-glow 0.3s ease; }" in OVERLAY_CSS
    assert "tint(slot.sprite);" in RIPPLES_JS and "const TINT_HOLD_MS = 700;" in RIPPLES_JS
    assert 'root.classList.remove("vl-ring-tint");' in RIPPLES_JS
    # the emitter is ready for the waveform driver (spec §4), which is not built yet
    assert "function setDrive(fn)" in RIPPLES_JS and "function emit(opts = {})" in RIPPLES_JS


def test_the_top_strip_overlays_and_never_moves_the_stage():
    """Kris 14 Oct: the activity and notice chips pushed the mist, the status line
    and End down when they appeared. The strip now floats over the stage under the
    allowance bar, so showing or hiding it moves nothing."""
    strip = ("#voice-gptlive-root .vl-stage > .vl-top-strip { position:absolute; z-index:4; left:0; right:0; "
             "top:calc(max(.9rem, env(safe-area-inset-top)) + 1.6rem); display:none; flex-wrap:wrap; "
             "align-items:flex-start; justify-content:center; gap:.35rem; margin:0; padding-top:0; "
             "padding-bottom:0; pointer-events:none; }")
    assert strip in GPTLIVE_JS
    # the old in-flow rule is gone: it reserved 2.75rem above and 2.7rem of height
    assert "#voice-gptlive-root .vl-top-strip { position:relative" not in GPTLIVE_JS
    assert "margin-top:2.75rem" not in GPTLIVE_JS and "min-height:2.7rem" not in GPTLIVE_JS
    # the same selector shape as the allowance bar: `.vl-stage > :not(.vl-ambient)` forces
    # position:relative at equal specificity, so only a later rule of that shape wins
    generic = "#voice-gptlive-root .vl-stage > :not(.vl-ambient) { position:relative; z-index:1; }"
    allowance = "#voice-gptlive-root .vl-stage > .vl-allowance { position:absolute; z-index:6; }"
    assert GPTLIVE_JS.index(generic) < GPTLIVE_JS.index(allowance) < GPTLIVE_JS.index(strip)
    # it sits just under the allowance bar (its top, 1.25rem tall, and a .35rem gap)
    assert "top:max(.9rem, env(safe-area-inset-top)); left:50%;" in GPTLIVE_JS and "width:4.5rem; height:1.25rem;" in GPTLIVE_JS
    # the chips stay tappable while the strip itself never catches a tap on the mist
    assert "#voice-gptlive-root .vl-top-strip > * { pointer-events:auto; }" in GPTLIVE_JS
    # the activity chip is a status line, not a control: it never eats a tap meant for the mist
    # (review, 14 Oct: on short screens the strip covered most of the mist's tap target)
    quiet = "#voice-gptlive-root .vl-stage > .vl-top-strip > .vl-activity { pointer-events:none; }"
    assert quiet in GPTLIVE_JS and GPTLIVE_JS.index("#voice-gptlive-root .vl-top-strip > * { pointer-events:auto; }") < GPTLIVE_JS.index(quiet)
    # on short viewports a lone chip sits to one side so the centre stays clear of the mist
    assert "@media(max-height:500px) { #voice-gptlive-root .vl-stage > .vl-top-strip { justify-content:space-between; } }" in GPTLIVE_JS
    # the expanded allowance bar (it opens on a tap) is about 2rem tall, so the strip steps below it
    # rather than being covered by it; nothing else on the stage moves
    assert ("#voice-gptlive-root .vl-stage > .vl-allowance.is-expanded ~ .vl-top-strip "
            "{ top:calc(max(.9rem, env(safe-area-inset-top)) + 2.5rem); }") in GPTLIVE_JS
    # showing is still the has-live-strip class, declared after the hidden default
    shown = "#voice-gptlive-root.has-live-strip .vl-top-strip { display:flex; }"
    assert GPTLIVE_JS.index(strip) < GPTLIVE_JS.index(shown)
    # the Debrief button's side room composes: the overlay rule never sets left/right
    # padding (a tie with the has-debrief rule would be settled by stylesheet order)
    assert "padding-left" not in strip and "padding-right" not in strip
    assert "#voice-gptlive-root .vl-top-strip { padding-left:.65rem; padding-right:.65rem; }" in GPTLIVE_JS
    assert "#voice-gptlive-root.has-debrief .vl-top-strip { padding-left: 3.2rem; padding-right: 3.2rem; }" in TEMPLATE


def test_live_screen_buttons_hug_their_text_and_keep_a_44px_hit_area():
    """Kris 14 Oct: the live screen's text buttons were tall pills with room above and
    below the words. Padding only: the faces hug the text, a ::before keeps the tap
    target, and no font-size anywhere changes."""
    def rules(selector):
        found = [line.strip() for line in GPTLIVE_JS.splitlines() if line.strip().startswith(selector + " {")]
        assert found, selector
        return found

    def rule(selector):
        return rules(selector)[0]

    tokens = "#voice-gptlive-root { --vl-pad-y:.22rem; --vl-pad-x:.55rem; }"
    face = ("#voice-gptlive-root .vl-notice, #voice-gptlive-root .vl-controls button, #voice-gptlive-root .vl-end, "
            "#voice-gptlive-root .vl-task-heading, #voice-gptlive-root .vl-stage-scroll a, "
            "#voice-gptlive-root .cz-draft-acts button:not(.cz-draft-btn) "
            "{ position:relative; padding:var(--vl-pad-y) var(--vl-pad-x); min-height:0; height:auto; line-height:1.2; }")
    hit = ("#voice-gptlive-root .vl-notice::before, #voice-gptlive-root .vl-controls button::before, "
           "#voice-gptlive-root .vl-end::before, #voice-gptlive-root .vl-task-heading::before, "
           "#voice-gptlive-root .vl-stage-scroll a::before, "
           "#voice-gptlive-root .cz-draft-acts button:not(.cz-draft-btn)::before "
           '{ content:""; position:absolute; top:min(0px, calc(50% - 22px)); bottom:min(0px, calc(50% - 22px)); '
           "left:min(0px, calc(50% - 22px)); right:min(0px, calc(50% - 22px)); }")
    assert tokens in GPTLIVE_JS and face in GPTLIVE_JS and hit in GPTLIVE_JS
    # appended after every existing rule for these selectors, so it wins at equal specificity
    for earlier in ("#voice-gptlive-root .vl-controls button", "#voice-gptlive-root .vl-end",
                    "#voice-gptlive-root .vl-notice", "#voice-gptlive-root .vl-task-heading"):
        assert GPTLIVE_JS.index(rule(earlier)) < GPTLIVE_JS.index(face) < GPTLIVE_JS.index(hit)
    # the old fixed heights are gone from every face the shared rule now sizes
    for selector in ("#voice-gptlive-root .vl-activity", "#voice-gptlive-root .vl-notice",
                     "#voice-gptlive-root .vl-controls button", "#voice-gptlive-root .vl-task-heading",
                     "#voice-gptlive-root .vl-end"):
        lines = " ".join(rules(selector))
        assert "min-height:44px" not in lines and "height:44px" not in lines and "min-height:32px" not in lines, lines
    assert "min-height:44px" in rule("#voice-gptlive-root .vl-task")        # whole-row buttons keep their height
    # positioned, so a row's own taps sit above the heading's hit area (a static row would lose to it)
    assert "position:relative" in rule("#voice-gptlive-root .vl-task")
    assert "padding:var(--vl-pad-y) var(--vl-pad-x)" in rule("#voice-gptlive-root .vl-activity")
    # the same extent in both directions: a face shorter or narrower than 44px grows to 44px
    assert hit.count("calc(50% - 22px)") == 4
    # padding only: every text size is still the one it was
    assert "font-size:.75rem" in rule("#voice-gptlive-root .vl-activity")
    assert "font-size:.73rem" in rule("#voice-gptlive-root .vl-notice")
    assert "@media(max-width:400px) { #voice-gptlive-root .vl-notice { font-size:.65rem; } }" in GPTLIVE_JS
    assert "font-size:.85rem" in rule("#voice-gptlive-root .vl-controls button")
    assert "font-size:.8rem" in rule("#voice-gptlive-root .vl-task-heading")
    assert "font-size: 0.85rem" in OVERLAY_CSS.split("#voice-live-root .vl-end {", 1)[1].split("}", 1)[0]
    assert "font-size: 0.85rem" in OVERLAY_CSS.split("#voice-live-root .vl-stage-scroll a {", 1)[1].split("}", 1)[0]
    assert ".cz-wait-go { font-size: 0.78rem;" in TEMPLATE and ".cz-wait-x { font-size: 0.8rem;" in TEMPLATE
    block = GPTLIVE_JS[GPTLIVE_JS.index(tokens):].split("`;document.head.appendChild(style);", 1)[0]
    assert "font-size" not in block
    assert all(line.strip().startswith(("#voice-gptlive-root", "/*")) for line in block.splitlines() if line.strip())
    # unchanged on purpose: the waiting chips (their wrapper clips a bigger hit area), the Confirm/Cancel
    # circles, the count badges, Debrief and the context bar, and the Standard overlay's own buttons
    assert "padding: 0.35rem 0.4rem 0.35rem 0.75rem;" in TEMPLATE and "padding: 0.35rem 0.6rem 0.35rem 0.3rem;" in TEMPLATE
    assert ".cz-draft-btn { width: 2.25rem; height: 2.25rem;" in TEMPLATE
    assert "#voice-gptlive-root .cz-context-chip { min-height: 44px; }" in TEMPLATE
    # on short viewports End's hit area would reach up over Retry audio's face, so End steps clear
    # of it (after the base End margin so the later rule wins), and the scroll box leaves End its band
    end_base = "#voice-gptlive-root .vl-end { margin:0 auto max(.5rem, env(safe-area-inset-bottom)); }"
    end_short = "@media(max-height:500px) { #voice-gptlive-root .vl-end { margin-top:1rem; } }"
    assert GPTLIVE_JS.index(end_base) < GPTLIVE_JS.index(end_short)
    assert "width:calc(100% - 1rem); margin-bottom:.65rem; }" in GPTLIVE_JS
    assert "#voice-live-root .vl-controls button { min-height:44px; padding:.55rem .85rem;" in OVERLAY_CSS
    assert "height: 2.4rem;" in OVERLAY_CSS.split("#voice-live-root .vl-end {", 1)[1].split("}", 1)[0]


PAINT_ALLOWANCE = "function paintAllowance() {" + GPTLIVE_JS.split("  function paintAllowance() {", 1)[1].split(
    "  async function refreshAllowance() {", 1)[0]


def test_the_allowance_bar_warns_before_the_cut():
    """The day's live-voice time ended mid-sentence with no warning (14 Oct). A minute
    before the cut the bar opens by itself; in the last 20 seconds the status line says so."""
    body = PAINT_ALLOWANCE
    assert "if (remaining <= 60 && !allowanceMinuteShown) {" in body
    assert "els.allowance.classList.add('is-expanded');" in body
    assert "els.allowance.setAttribute('aria-expanded', 'true');" in body
    assert "els.allowance.removeAttribute('aria-label');" in body      # as the tap handler does when it opens
    assert "if (!els.allowance.classList.contains('is-expanded')) {" in body
    assert "if (remaining <= 20 && allowanceSession && !allowanceEndingShown) {" in body
    assert "status('20 seconds of live voice left — it will end soon.');" in body
    # one-shot: the flag is taken before the bar opens, so a later tap to close it is respected
    assert body.index("allowanceMinuteShown = true;") < body.index("els.allowance.classList.add('is-expanded');")
    assert body.index("allowanceEndingShown = true;") < body.index("status('20 seconds")
    # an unlimited allowance returns before either warning, and the numbers are painted first
    unlimited = body.index("allowanceSnapshot.unlimited")
    assert unlimited < body.index("return;", unlimited) < body.index("remaining <= 60")
    assert body.index("root.classList.add('has-allowance');") < body.index("remaining <= 60")
    assert body.index("root.classList.add('has-allowance');") > body.index("els.allowanceFill.style.width")
    # the flags live with the other allowance state and are reset when the screen opens
    declared = GPTLIVE_JS.split("  let allowanceSnapshot = null,", 1)[1].split(";", 1)[0]
    assert "allowanceMinuteShown = false" in declared and "allowanceEndingShown = false" in declared
    opened = GPTLIVE_JS.split("    open(opts) {", 1)[1].split("    close(reason", 1)[0]
    assert "allowanceSnapshot=null;allowanceSampledAt=Date.now();allowanceMinuteShown=false;allowanceEndingShown=false;" in opened


def test_the_allowance_warnings_run_once_in_real_javascript():
    import shutil
    import subprocess
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.fail("Node is required to run the allowance warnings")
    script = """
      const assert = require('node:assert/strict');
      const vm = require('node:vm');
    """ + "const code = " + json.dumps(PAINT_ALLOWANCE) + ";" + "const ENDING = " + json.dumps(
        "20 seconds of live voice left — it will end soon.") + ";" + """
      const fake = () => { const classes = new Set(), attrs = {}; return {
        classList: {add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c)},
        setAttribute: (k, v) => { attrs[k] = String(v); }, removeAttribute: k => { delete attrs[k]; },
        attrs, style: {}, textContent: '', hidden: true}; };
      function screen(remaining, session, extra) {
        const els = {allowance: fake(), allowanceText: fake(), allowanceProgress: fake(), allowanceFill: fake()};
        els.allowance.setAttribute('aria-label', 'Live voice allowance today. Tap to expand remaining time');
        const statuses = [];
        const context = vm.createContext({els, root: fake(), status: text => statuses.push(text),
          allowanceSnapshot: Object.assign({enabled: true, unlimited: false, limit_seconds: 600, remaining_seconds: remaining}, extra || {}),
          allowanceSession: session, allowanceSampledAt: Date.now(), allowanceMinuteShown: false, allowanceEndingShown: false});
        vm.runInContext(code, context);
        return {els, statuses, paint: () => vm.runInContext('paintAllowance()', context), context};
      }
      const open = s => s.els.allowance.classList.contains('is-expanded');
      // plenty left: nothing opens, nothing is said
      let s = screen(120, 'live-1'); s.paint();
      assert.equal(open(s), false); assert.deepEqual(s.statuses, []);
      // exactly a minute left: the bar opens by itself, with the tap handler's attributes, and says nothing yet
      s = screen(60, 'live-1'); s.paint();
      assert.equal(open(s), true); assert.equal(s.els.allowance.attrs['aria-expanded'], 'true');
      assert.equal('aria-label' in s.els.allowance.attrs, false); assert.deepEqual(s.statuses, []);
      // a tap that closes it is respected on every later paint
      s.els.allowance.classList.remove('is-expanded'); s.paint(); s.paint();
      assert.equal(open(s), false);
      // the last 20 seconds of a running conversation: one status line, once
      s = screen(19, 'live-1'); s.paint(); s.paint(); s.paint();
      assert.deepEqual(s.statuses, [ENDING]);
      assert.equal(open(s), true);
      // no conversation running: the bar still opens, but the status line is left alone
      s = screen(19, null); s.paint();
      assert.equal(open(s), true); assert.deepEqual(s.statuses, []);
      // opened by hand earlier: still counted, and never toggled shut by the warning
      s = screen(50, 'live-1'); s.els.allowance.classList.add('is-expanded'); s.paint();
      assert.equal(open(s), true);
      // unlimited: neither warning, whatever the numbers say
      s = screen(5, 'live-1', {unlimited: true}); s.paint();
      assert.equal(open(s), false); assert.deepEqual(s.statuses, []);
      // a time that has already run past the snapshot counts too (the paint beat runs every second)
      s = screen(90, 'live-1'); vm.runInContext('allowanceSampledAt = Date.now() - 35000', s.context); s.paint();
      assert.equal(open(s), true); assert.equal(s.statuses.length, 0);
      vm.runInContext('allowanceSampledAt = Date.now() - 75000', s.context); s.paint();
      assert.deepEqual(s.statuses, [ENDING]);
    """
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
