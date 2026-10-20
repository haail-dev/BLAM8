/* Live voice conversation with the business concierge (docs/voice-live-seed.md).
 *
 * Ported from OurTab's src/voice-live.js — the donor is deliberately
 * self-contained and dependency-injected precisely so this lift could happen.
 * Every constant and gate below encodes a field lesson that cost the donor a
 * debugging round; change them only with a phone in your hand.
 *
 * Pipeline: mic capture → OpenAI realtime STT (ephemeral token, WebSocket) →
 * answer_business streamed as SSE (/concierge/voice/chat) → OpenAI TTS
 * streamed as raw PCM (/concierge/voice/tts) → speakers, with barge-in.
 *
 * deps: {
 *   apiFetch(action, body, signal) -> fetch Response ("token" | "chat" | "tts")
 *   buildChatBody()             -> {question, history} for /voice/chat
 *   onUserTurn(text, meta)      -> Promise<{confirmed?, declined?, farewell?, ack?}>
 *                                  append+persist the user turn and resolve any
 *                                  local outcome (confirm/decline/goodbye)
 *   onAssistantTurn(payload)    -> void: append+persist ({reply, links, proposal, trail})
 *   onDiscardTurn()             -> optional: tapping the heard words struck the turn just sent —
 *                                  drop its user turn from history + the chat
 *   greeting()                  -> optional: short text spoken when the mic goes live
 *   renderStage()               -> optional: {node, fresh} for the swipeable second
 *                                  page — a DOM node (BLAM8 builds cards as nodes
 *                                  with listeners, not HTML strings); fresh means
 *                                  the newest reply brought its own content
 *   onMount(rootEl)             -> optional: called once when the overlay DOM exists
 *   onClose()                   -> optional
 *   canStart()                  -> {ok, reason?}
 * }
 *
 * The audio contract, both directions, is PCM16 mono 24kHz — what the realtime
 * transcription session takes and what TTS emits, so nothing is resampled twice.
 *
 * WHEN YOU'VE FINISHED is decided in ONE place: voice-turns.js, keyed by the
 * speech service's item ids (reports/Voice agent turn taking practices.md,
 * 2026-09-24). Nothing below throws away words that were transcribed: what
 * isn't sent is shown, held, and can be sent with a tap — and every outcome
 * is logged (/concierge/voice/heard). Don't add a gate that drops speech.
 */

// Loaded with this file's own ?v= so a deploy never pairs a new engine with a
// cached assembler.
const [{ createTurnAssembler }, { OVERLAY_CSS }] = await Promise.all([
  import("./voice-turns.js" + new URL(import.meta.url).search),
  import('./voice-overlay-css.js' + new URL(import.meta.url).search),
]);
// The ripples behind the orb (docs/spec-voice-ripples.md): loading, ready, error.
const { createRipples } = await import("./voice-ripples.js" + new URL(import.meta.url).search);
const { createTaskDelivery } = await import("./task-delivery.js" + new URL(import.meta.url).search);

const SAMPLE_RATE = 24000;
// What the Debrief button says on the owner's behalf (= src/app/debrief_walk.py
// START_LINE / NEXT_LINE; tests/test_debrief_button.py keeps the two in step).
const DEBRIEF_START = "Run me through my debrief.";
const DEBRIEF_NEXT = "Next.";
export function savedConciergeVoice() {
  try {
    const voice = localStorage.getItem("blam8.conciergeVoice");
    return ["marin", "cedar", "alloy", "sage", "coral", "af_heart", "bf_emma", "bm_george"].includes(voice) ? voice : "marin";
  } catch (_e) { return "marin"; }
}
const MIC_BATCH_SAMPLES = 1200; // ~50ms per append — small enough for live VAD
const CLIP_WINDOW_MS = 700;     // speech_started this soon after the gate opens = cut off

// THE SELF-INTERRUPTION GUARD. Mobile browsers' echo cancellation only covers
// audio played through media elements or WebRTC — Web Audio API playback (our
// PCM player) is often NOT in the canceller's reference signal, so the phone's
// mic hears the TTS voice, the server VAD fires speech_started, and the agent
// barges in on itself (seen live on the donor, 2026-08-23). So the mic feed is
// GATED while we are audibly speaking, plus a short tail for the room's echo
// to die down. Interruption survives two ways: tapping the orb, and real voice
// barge-in in the silent gaps between sentences.
const ECHO_HANGOVER_MS = 350;

// THE GREETING HOLD (Kris 2026-09-23: "the mic is instantly listening, even
// during the Agent greeting"). The playback gate only closes once audio is
// actually PLAYING, so while the greeting was still being fetched the mic was
// live and hearing the room. Now the mic is shut outright (nothing kept,
// nothing sent) from the moment the session connects until the greeting has
// finished coming out of the speaker, and not a moment longer: "Listening"
// appears the instant it lifts. The cap only matters if the greeting never
// arrives, so a hung TTS call can't leave the mic shut for good.
const GREETING_HOLD_MAX_MS = 8000;

// THE READY MOMENT with the greeting Off (docs/spec-voice-ripples.md, Kris
// 2026-09-30, decisions 1 and 2): no spoken word. The socket opens, a beat of
// stillness, one green ring with a soft water sound, 300 ms of quiet, and only
// then does the mic open and "Listening" appear, so the sound can't bleed into
// your first words. Fixed timers, never the animation, decide this: the
// ripple is drawn by the UI thread and must not be able to hold the mic shut.
const READY_STILL_MS = 250;
const READY_SOUND_MS = 400;
const READY_QUIET_MS = 300;

// A speech_started alone is not an interruption: a cough fires the VAD too
// (it hung up on the donor mid-reply, transcribed as "go away"). Barge-in
// commits only when the speech SUSTAINS this long, or produces actual
// transcript text; a blip that ends sooner cancels nothing.
const BARGE_CONFIRM_MS = 300;

// How far ahead a fresh burst of TTS audio is scheduled. The first chunk used
// to start 20ms out, so any jitter on chunk two was an audible stutter; 150ms
// of priming is inaudible as latency and absorbs it.
const PLAYBACK_PRIME_S = 0.15;

// THE AMBIENT FLOOR. The mic level keeps a rolling estimate of the ROOM. An
// utterance whose loudest moment never stood above it is probably the telly —
// but since 2026-09-24 it is never thrown away for that: it's HELD on screen,
// "tap to send", and an answer to the card that's waiting skips the test
// entirely. Noise itself is filtered before the speech decision, by the
// speech service (noise_reduction), not by gates after it.
const AMBIENT_EMA = 0.005;          // slow: the floor tracks the room, not the word
const SPEECH_OVER_AMBIENT = 4;      // music transients beat average*3 — wide margin
const BARGE_OVER_AMBIENT = 5;       // cancelling a reply needs the strongest proof
const MIN_SPEECH_PEAK = 0.06;       // real talking-into-a-phone level
// The owner's own line (Kris 2026-09-23): the Concierge's settings cog shows
// the room and your voice on a meter and lets you set the line between them.
// Kept per device (it's this phone's mic in this room). Unset = automatic.
export const MIC_LINE_KEY = "blam8.voiceMicLine";
// Her voice's loudness (Kris 2026-09-23: "no volume control for the Agent
// voice. She's very quiet and my phone is full volume"). A gain on the
// playback, per device, set from the settings cog; a limiter after it keeps a
// loud setting from crackling. 1 = the TTS as it comes; the default lifts it.
export const VOICE_VOLUME_KEY = "blam8.voiceVolume";
export const VOICE_VOLUME_DEFAULT = 1.6;
export function savedVoiceVolume() {
  try {
    const v = parseFloat(localStorage.getItem(VOICE_VOLUME_KEY) || "");
    return v >= 0.5 && v <= 3 ? v : VOICE_VOLUME_DEFAULT;
  } catch (_e) { return VOICE_VOLUME_DEFAULT; }
}
export function savedMicLine() {
  try {
    const v = parseFloat(localStorage.getItem(MIC_LINE_KEY) || "");
    return v > 0 && v < 1 ? v : null;
  } catch (_e) { return null; }
}

// The on-phone speech detector (Silero) that used to decide which audio the
// speech service ever heard is GONE (2026-09-24): a soft "yes" scoring under
// its line never reached the transcriber at all, and nothing said so. All
// audio goes; the service's own filter and detector decide.
const DEAD_MIC_MS = 2000;
// How often the turn assembler is asked "have they finished?".
const TURN_BEAT_MS = 100;

// A turn's stream must keep moving: no frame for this long and the turn
// is given up (a deploy restarting under the request left the orb
// "thinking" indefinitely, live 2026-09-23).
const TURN_STALL_MS = 45000;
function stallGuard(promise, ms) {
  let timer;
  const stall = new Promise((_resolve, reject) => {
    timer = setTimeout(() => reject(new Error("stalled")), ms);
  });
  return Promise.race([promise, stall]).finally(() => clearTimeout(timer));
}

const WORKLET_SOURCE = `
class PcmCapture extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch) this.port.postMessage(ch.slice(0));
    return true;
  }
}
registerProcessor("pcm-capture", PcmCapture);`;

// Overlay styles, injected once. Self-contained on purpose: the voice overlay
// has its own fixed dark palette (it reads the same in light and dark app
// themes), so nothing here rides the app's theme layer.
// [hidden]{display:none} is load-bearing: a styled overlay with display:flex
// defeats the hidden attribute and an invisible layer eats every tap.
export { OVERLAY_CSS };

// Complete sentences peel off the front as they form; the remainder waits for
// more deltas. TTS is requested per sentence so the first one is speaking
// while the rest of the reply is still being generated.
export function splitReadySentences(buffer) {
  const out = [];
  let rest = buffer;
  for (;;) {
    const m = rest.match(/^[\s\S]{10,}?[.!?…](?=\s|$)/);
    if (!m) break;
    out.push(m[0].trim());
    rest = rest.slice(m[0].length).replace(/^\s+/, "");
  }
  return { sentences: out, rest };
}

export function createVoiceLive(deps) {
  let ttsVoice = "marin";
  let root = null;
  let open = false;
  let voiceSession = null;
  let taskBeat = 0;
  const delivery = createTaskDelivery({
    now: () => Date.now(), id: () => crypto.randomUUID(),
    interrupted: () => !open || document.visibilityState !== 'visible' || userSpeaking || turnBusy() ||
      !!(deps.confirmationBusy && deps.confirmationBusy()),
    gap: () => open && document.visibilityState === 'visible' && phase === "listening" && !turnBusy() && !turnInFlight && !userSpeaking &&
      !turns.hasPending() && !heldWords && !liveSources.length && Date.now() >= micMutedUntil &&
      !(deps.approvalWaiting && deps.approvalWaiting()) &&
      !(deps.confirmationBusy && deps.confirmationBusy()),
    read: async signal => deps.taskRead ? deps.taskRead(voiceSession, signal) : [],
    transition: (job, operation, outcome, signal) => deps.taskTransition ? deps.taskTransition({
      task_id: job.task.id, revision: job.task.revision, event_id: job.task.event_id,
      voice_session: job.task.voice_session, delivery_id: job.id, operation, outcome: outcome || 'interrupted',
    }, signal) : Promise.resolve({ ok: false }),
    tts: async (text, signal) => {
      const response = await deps.apiFetch('tts', { text, voice: ttsVoice }, signal);
      if (!response.ok) return null;
      const bytes = new Uint8Array(await response.arrayBuffer());
      return bytes.length ? bytes : null;
    },
    play: async (bytes, signal, text) => {
      if (signal.aborted || !playCtx || playCtx.state !== 'running') return false;
      els.reply.textContent = text;
      const stopOwnedAudio = () => stopPlayback();
      signal.addEventListener('abort', stopOwnedAudio, { once: true });
      try {
        playPcm(bytes);
        while (liveSources.length && !signal.aborted && open) {
          await new Promise(resolve => setTimeout(resolve, 25));
        }
        return !signal.aborted && open && playCtx?.state === 'running';
      } finally {
        signal.removeEventListener('abort', stopOwnedAudio);
      }
    },
  });

  // Audio out
  let playCtx = null;
  let nextPlayTime = 0;
  let pcmCarry = new Uint8Array(0);
  let liveSources = [];

  // Audio in
  let micStream = null;
  let micCtx = null;
  let ws = null;

  // Turn state
  let phase = "idle"; // connecting | listening | thinking | speaking | error
  // True from a question until its first reply sentence arrives. Spoken
  // commentary plays inside that window, and when it ends the orb goes back
  // to "thinking", not "listening": the answer is still coming.
  let turnThinking = false;
  let thinkingNote = "";
  let micMutedUntil = 0;
  let launchHoldUntil = 0;
  let micGateAnnounced = false;
  let gateTailFrames = [];   // mic audio captured during the hangover, flushed on ungate
  let bargeTimer = 0;        // pending sustained-speech barge-in
  let speechStartedAt = 0;
  let userSpeaking = false;  // between speech_started and stopped (ungated)
  // Time saved (Kris 2026-09-24): the owner's speaking time counts against
  // what a card or draft saves. Everything said since the last one — the ask
  // and any back-and-forth that led to it — rides each turn as spoken_ms.
  let spokenSinceCard = 0;
  // Mute (Kris 2026-09-23: "no way to pause or mute the mic and leave the
  // conversation going, giving me time to read or click on screen buttons").
  // Muted: the mic track is off, nothing is sent, and any speech events for
  // audio already in flight are ignored — the conversation stays open and a
  // reply that's playing plays on.
  let muted = false;
  let levelRaf = 0;
  let ripples = null;         // the canvas behind the orb (voice-ripples.js), while the screen is open
  let ambientLevel = 0.004;   // rolling room level, music and all
  // What you said, assembled by item id (voice-turns.js); the beat asks it
  // whether you've finished.
  let localSpeech = false;
  const turns = createTurnAssembler({
    transcriptionWaitMs: () => localSpeech ? 15000 : 4000,
    onUtterance: (u) => heardUtterance(u),
    onDisplay: (text) => paintHeard(text),
    onEvent: (name, detail) => logHeard(name, { text: detail.text || "" }),
  });
  let turnBeat = 0;
  // Heard, not sent — too quiet to be sure it was you, or a single word while
  // the last question is still being worked on. Shown with "tap to send";
  // the next thing you say replaces it.
  let heldWords = null;
  let micLine = savedMicLine(); // the owner's line from settings, or null (auto)
  let voiceVolume = savedVoiceVolume();
  let playGain = null;
  // POSSIBLY CLIPPED (Kris 2026-09-23: "psychosis on my mind", "I need no
  // forgiveness"). While a reply plays the mic is gated, so speech that began
  // during playback reaches the server only from the gate's opening — the STT
  // guesses a sentence from a fragment. A segment that starts within
  // CLIP_WINDOW_MS of the gate opening was very likely already under way; the
  // turn goes up flagged so the concierge asks rather than acts on a guess.
  let gateOpenedAt = 0;
  // Tap-to-cancel on the heard words. turnInFlight = the sent turn it can still
  // strike ({id}); speech still transcribing when it's struck is ignored by
  // its item id (voice-turns.js discard), so it can't swallow the next words.
  let turnInFlight = null;
  // Is an answer actually under way (thinking, fetching or speaking)? Kris
  // 2026-09-23: "when i start talking, the Agent's previous message is removed
  // from screen". Barge-in asked `turnAbort && !aborted`, but a turn that
  // finishes normally is never aborted, so a finished reply still counted as
  // busy and ~300ms into the next question it was cleared as an interruption.
  // This is set for exactly as long as runAssistantTurn runs.
  let activeTurn = null;
  function turnBusy() {
    return Boolean(activeTurn && turnAbort && !turnAbort.signal.aborted);
  }
  let wasHidden = false;      // the app went to the background since the mic came up
  // The diagnostic line (triple-tap the status, or shown by itself for 90 s
  // after a reconnect): which part of the chain is silent.
  let dbgFrames = 0, dbgFramesLast = 0, dbgSent = 0, dbgTimer = 0, dbgHideAt = 0, dbgLevel = 0;
  let zeroSince = 0;          // the mic has delivered only exact zeros since (a dead track)
  let turnAbort = null;


  // Display-only captions session (gpt-live-transcribe): word-by-word text
  // while the user is still talking. The turn engine never reads from it.
  let captionsWs = null;
  let captionBuffer = "";
  let captionsLive = false;
  let localCaptionItem = null;

  const els = {};

  function ensureDom() {
    if (root) return;
    const style = document.createElement("style");
    style.textContent = OVERLAY_CSS;
    document.head.appendChild(style);
    root = document.createElement("div");
    root.id = "voice-live-root";
    root.innerHTML = `
      <div class="vl-corner"><img class="vl-brand" src="/static/img/blam8-assistant-glass.png${new URL(import.meta.url).search}" width="44" height="44" alt="BLAM8"><button type="button" class="cz-voice-settings" aria-label="Concierge settings" title="Settings"><svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M19.14 12.94c.04-.31.06-.63.06-.94s-.02-.63-.06-.94l2.03-1.58a.5.5 0 0 0 .12-.64l-1.92-3.32a.5.5 0 0 0-.61-.22l-2.39.96a7.28 7.28 0 0 0-1.63-.95l-.36-2.54A.49.49 0 0 0 13.88 2h-3.76a.49.49 0 0 0-.49.42L9.27 4.96c-.6.23-1.15.55-1.63.95l-2.39-.96a.5.5 0 0 0-.61.22L2.72 8.49a.5.5 0 0 0 .12.64l2.03 1.58a7.28 7.28 0 0 0-.06 1.88L2.78 14.17a.5.5 0 0 0-.12.64l1.92 3.32a.5.5 0 0 0 .61.22l2.39-.96c.48.4 1.03.72 1.63.95l.36 2.54a.49.49 0 0 0 .49.42h3.76a.49.49 0 0 0 .49-.42l.36-2.54c.6-.23 1.15-.55 1.63-.95l2.39.96a.5.5 0 0 0 .61-.22l1.92-3.32a.5.5 0 0 0-.12-.64l-2.03-1.58ZM12 15.5a3.5 3.5 0 1 1 0-7 3.5 3.5 0 0 1 0 7Z"/></svg></button></div>
      <div class="vl-stage">
        <div class="vl-toasts" aria-live="polite"></div>
        <!-- The Tour starts from the Concierge's settings cog (Kris 2026-09-23). -->
        <div class="vl-tour" role="dialog" aria-live="polite" hidden></div>
        <!-- The pull moves the orb column alone (Kris 2026-09-24): End
             conversation, the badges and the cards stay where they are. -->
        <div class="vl-live" data-pull-bar data-pull-moves="#voice-live-root .vl-live" data-pull-reopen="voice">
          <div class="vl-orb"><span aria-hidden="true"></span><button type="button" class="vl-mic" aria-pressed="false"></button></div>
          <p class="vl-status" role="status"></p>
          <p class="vl-debug" hidden></p>
          <p class="vl-transcript"></p>
          <p class="vl-reply" data-pull-refresh="off"></p>
        </div>
        <div class="vl-stage-scroll" data-pull-refresh="off"></div>
        <div class="vl-controls">
          <button type="button" class="vl-stop">Stop speech</button>
          <button type="button" class="vl-discard">Discard request</button>
          <button type="button" class="vl-end">End conversation</button>
        </div>
      </div>
    `;
    document.body.appendChild(root);
    root.querySelector('.cz-voice-settings').addEventListener('click', (event) => {
      event.stopPropagation(); deps.onSettings?.();
    });
    root.querySelector('.vl-stop').addEventListener('click',()=>{if(phase==='speaking' || phase==='thinking'){bargeIn();setPhase('listening');}});
    root.querySelector('.vl-discard').addEventListener('click',()=>discardHeard());
    els.orb = root.querySelector(".vl-orb");
    els.toasts = root.querySelector(".vl-toasts");
    els.status = root.querySelector(".vl-status");
    els.debug = root.querySelector(".vl-debug");
    // Triple-tap the status line: live floor/peak numbers, so "it's still
    // picking up the noise" can come back as data we can tune from.
    let statusTaps = [];
    els.status.addEventListener("click", () => {
      const now = Date.now();
      statusTaps = statusTaps.filter((t) => now - t < 1500);
      statusTaps.push(now);
      if (statusTaps.length >= 3) {
        statusTaps = [];
        if (els.debug.hidden) showDebug(0); else hideDebug();
      }
    });
    els.transcript = root.querySelector(".vl-transcript");
    els.transcript.addEventListener("click", (event) => {
      // Held words: the tap sends them. Words on their way: the tap strikes them.
      if (heldWords) { event.stopPropagation(); sendHeld(); return; }
      if (!els.transcript.classList.contains("is-strikable")) return;
      event.stopPropagation();   // a strike is not a tap-to-interrupt
      discardHeard();
    });
    els.transcript.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      if (heldWords) { event.preventDefault(); sendHeld(); return; }
      if (els.transcript.classList.contains("is-strikable")) {
        event.preventDefault();
        discardHeard();
      }
    });
    els.tour = root.querySelector(".vl-tour");
    els.tour.addEventListener("click", (event) => event.stopPropagation());
    // The transcript is painted from many places; its strikable state follows.
    new MutationObserver(paintDiscard).observe(els.transcript,
      { childList: true, characterData: true, subtree: true });
    els.reply = root.querySelector(".vl-reply");
    els.stageScroll = root.querySelector(".vl-stage-scroll");
    root.querySelector(".vl-end").addEventListener("click", () => stop());
    els.mute = root.querySelector(".vl-mic");
    els.mute.addEventListener("click", (event) => {
      event.stopPropagation();   // the mic is not a tap-to-interrupt
      if (phase === "paused") { start(); return; }
      if (phase === "error") { reconnect(); return; }
      setMuted(!muted);
    });
    paintMute();
    if (deps.onMount) deps.onMount(root);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") { wasHidden = true; if (ripples) ripples.pause(); return; }
      if (document.visibilityState !== "visible" || !open) return;
      if (ripples) ripples.resume();
      const parked = !micCtx || micCtx.state !== "running" || !ws || ws.readyState !== 1
        || !(micStream && micStream.getAudioTracks().some((t) => t.readyState === "live" && !t.muted));
      if (parked) setPhase("error", "Back — tap the mic to carry on.");
    });
    // While it's talking (or still thinking), a tap is the guaranteed
    // interrupt — voice barge-in is gated during playback (ECHO_HANGOVER_MS).
    root.querySelector(".vl-stage").addEventListener("click", (event) => {
      if (event.target.closest(".vl-controls")) return;
      // Taps on the cards sheet belong to its cards, not to barge-in.
      if (event.target.closest(".vl-stage-scroll")) return;
      if (event.target.closest(".vl-tour")) return;
      if (phase === "paused") { start(); return; }
      if (phase === "error") { reconnect(); return; }
      if (phase === "speaking" || phase === "thinking") {
        bargeIn();
        setPhase("listening");
      }
    });
  }

  // The app calls this on every state change so cards track live state; the
  // turn loop calls it with autoPresent after a reply lands. Cards share the
  // screen with the orb, so presenting is just making sure the sheet reads
  // from the top.
  function refreshStage(autoPresent = false) {
    if (!root || !deps.renderStage) return;
    const result = deps.renderStage() || {};
    const node = result.node || null;
    const fresh = Boolean(result.fresh);
    els.stageScroll.innerHTML = "";
    if (node) els.stageScroll.appendChild(node);
    const hadCards = root.classList.contains("has-cards");
    root.classList.toggle("has-cards", Boolean(node));
    if (autoPresent && fresh) els.stageScroll.scrollTop = 0;
    // The orb moves and shrinks (0.3 s): the ripples' origin follows, now and once it has settled.
    if (ripples && hadCards !== Boolean(node)) {
      requestAnimationFrame(() => ripples && ripples.relayout());
      setTimeout(() => ripples && ripples.relayout(), 350);
    }
  }

  // The orb's ring follows YOUR voice, straight off the mic — the instant
  // "it can hear me" signal the delayed transcript can't give.
  function paintDebug() {
    if (!els.debug || els.debug.hidden) return;
    const fps = dbgFrames - dbgFramesLast;
    dbgFramesLast = dbgFrames;
    const track = micStream && micStream.getAudioTracks()[0];
    const wsState = ws ? ["connecting", "open", "closing", "closed"][ws.readyState] : "none";
    els.debug.textContent =
      `mic ${fps} f/s · level ${dbgLevel.toFixed(3)} · room ${ambientLevel.toFixed(3)} · gate ${speechGate().toFixed(3)}`
      + ` · engine ${micCtx ? micCtx.state : "none"} · track ${track ? track.readyState + (track.muted ? "/muted" : "") : "none"}`
      + ` · link ${wsState} · sent ${(dbgSent / 1024).toFixed(0)} KB`
      + ` · ${phase}${document.visibilityState === "visible" ? "" : " · hidden"}`;
    if (dbgHideAt && Date.now() > dbgHideAt) hideDebug();
  }
  function showDebug(forMs) {
    if (!els.debug) return;
    els.debug.hidden = false;
    dbgHideAt = forMs ? Date.now() + forMs : 0;
    if (!dbgTimer) dbgTimer = setInterval(paintDebug, 1000);
    paintDebug();
  }
  function hideDebug() {
    if (els.debug) els.debug.hidden = true;
    if (dbgTimer) { clearInterval(dbgTimer); dbgTimer = 0; }
    dbgHideAt = 0;
  }

  function setOrbLevel(level) {
    if (!root || levelRaf) return;
    levelRaf = requestAnimationFrame(() => {
      levelRaf = 0;
      if (!root) return;
      root.style.setProperty("--vl-level", String(level));
      dbgLevel = level;
    });
  }

  // ---------- what was heard, and what became of it ----------
  // The words as they build, straight from the assembler. Live captions own
  // the line while they're painting (faster, same words).
  function paintHeard(text) {
    if (!els.transcript) return;
    if (text) { clearHeld(); if (!captionsLive || text === "…") els.transcript.textContent = text; }
    paintDiscard();
  }

  // Every outcome goes on record (Kris 2026-09-24: a transcribed question sat
  // on "Listening" and nothing said which path had eaten it).
  function logHeard(outcome, u = {}, reason = "") {
    if (!deps.apiFetch) return;
    deps.apiFetch("heard", {
      outcome, reason,
      text: String(u.text || "").slice(0, 300),
      peak: Number(u.peak) || 0, gate: speechGate(),
      segments: Number(u.segments) || 0, salvaged: Boolean(u.salvaged), clipped: Boolean(u.clipped),
      duration_ms: Number(u.durationMs) || 0,
    }).catch(() => {});
  }

  // Heard but not sent: on screen with "tap to send", never silently gone.
  // Words the transcriber made up (Kris 2026-09-24: sat silent, and "Yes,
  // she's coming. Yes, I will confirm the booking for Mabel with Jess" came
  // through at a peak of 0.065 against a line of 0.051 — names from the
  // hint it's given, heard in a blip). Real speech from the owner peaks at
  // 0.5-1.0 and runs at 2-4 words a second. A long sentence from a sound
  // barely over the line, or more words than the sound had time to hold,
  // is held, not sent.
  function soundsInvented(u) {
    const words = (String(u.text || "").match(/[A-Za-z']+/g) || []).length;
    const secs = (Number(u.durationMs) || 0) / 1000;
    if (words >= 6 && u.peak < speechGate() * 1.6) return true;
    if (secs > 0.2 && words >= 5 && words / secs > 6) return true;
    return false;
  }

  function holdWords(u, reason) {
    heldWords = { text: u.text, meta: { durationMs: u.durationMs, clipped: u.clipped } };
    els.transcript.textContent = u.text;
    els.transcript.classList.add("is-held");
    els.transcript.setAttribute("role", "button");
    els.transcript.setAttribute("tabindex", "0");
    els.transcript.setAttribute("aria-label", "Send: " + u.text);
    notify(reason === "quiet" ? "Too quiet to be sure — please repeat your request"
      : reason === "unlikely" ? "That didn't sound like you — please repeat your request"
      : "Still on the last request — please repeat when ready");
    logHeard("held", u, reason);
  }
  function clearHeld() {
    if (!heldWords) return;
    heldWords = null;
    if (!els.transcript) return;
    els.transcript.classList.remove("is-held");
    els.transcript.removeAttribute("aria-label");
  }
  function sendHeld() {
    const p = heldWords;
    clearHeld();
    if (!p) return;
    logHeard("sent", { text: p.text }, "tapped");
    runTurn(p.text, p.meta);
  }

  // A finished utterance, from the assembler. One decision, in this order:
  // nothing heard → say so; an answer to the card on screen → it goes, no
  // loudness test; too quiet or a stray word mid-thought → held, visible;
  // otherwise → it's your turn.
  function heardUtterance(u) {
    if (!open) return;
    if (!u.text) {
      if (els.transcript.textContent === "…") els.transcript.textContent = "";
      if (!u.echo) {
        notify("Didn't catch that — say it again");
        logHeard("nothing", u, u.why);
      }
      paintDiscard();
      return;
    }
    els.transcript.textContent = u.text;
    const meta = { durationMs: u.durationMs, clipped: u.clipped };
    if (deps.expectsAnswer && deps.expectsAnswer(u.text)) {
      logHeard("sent", u, "answer");
      runTurn(u.text, meta);
      return;
    }
    if (!turnBusy() && isCourtesy(u.text)) {
      logHeard("sent", u, "courtesy");
      deps.apiFetch("local", { question: u.text, answer: "You're welcome." }).catch(() => {});
      sayLocally("You're welcome.");
      return;
    }
    if (u.peak < speechGate()) { holdWords(u, "quiet"); return; }
    if (soundsInvented(u)) { holdWords(u, "unlikely"); return; }
    // A single word while the last question is still being thought about is
    // more likely a cough heard as "uh" than a new question — held, not lost.
    if (turnBusy() && phase === "thinking" && !meansToInterrupt(u.text)) { holdWords(u, "mid_thought"); return; }
    logHeard("sent", u, u.why);
    runTurn(u.text, meta);
  }

  function notify(text) {
    if (!root || !text) return;
    // No trailing "…" on the chips (Kris 2026-09-24).
    text = String(text).replace(/\s*…\s*$/, "");
    while (els.toasts.children.length >= 3) els.toasts.removeChild(els.toasts.firstChild);
    const pill = document.createElement("div");
    pill.className = "vl-toast";
    pill.textContent = text;
    pill.addEventListener("animationend", () => pill.remove());
    els.toasts.appendChild(pill);
  }

  function setPhase(next, note = "") {
    const before = phase;
    phase = next;
    if (!root) return;
    root.dataset.phase = next;
    // The water follows the phase (spec §3): the stone drops as we connect,
    // a fault is one slow red ring, a pull-to-refresh pause goes still.
    if (ripples) {
      if (next === "connecting" && before !== "connecting") ripples.drop();
      else if (next === "error") ripples.error();
      else if (next === "paused" || next === "idle") ripples.still();
    }
    const texts = {
      connecting: "Getting ready…",
      paused: "Refreshed — tap the orb to carry on",
      listening: "Listening — just talk",
      thinking: note || "Thinking…",
      speaking: "Speaking — tap to interrupt",
      error: note || "Something went wrong"
    };
    els.status.textContent = (muted && next === "listening")
      ? "Mic muted — tap the mic when you're ready"
      : (texts[next] || "");
  }

  const MIC_ON_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10v1a7 7 0 0 0 14 0v-1"/><path d="M12 18v4"/></svg>';
  const MIC_OFF_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3l18 18"/><path d="M9 9v2a3 3 0 0 0 5.1 2.1M15 10V5a3 3 0 0 0-5.7-1.3"/><path d="M5 10v1a7 7 0 0 0 11.5 5.4M19 10v1a7 7 0 0 1-.4 2.3"/><path d="M12 18v4"/></svg>';
  function paintMute() {
    if (!els.mute) return;
    els.mute.setAttribute("aria-pressed", muted ? "true" : "false");
    els.mute.setAttribute("aria-label", muted ? "Unmute the mic" : "Mute the mic");
    els.mute.innerHTML = muted ? MIC_OFF_ICON : MIC_ON_ICON;
    root.classList.toggle("is-muted", muted);
  }

  // Mute ONLY switches the mic off (Kris 2026-09-23: "it shouldn't interfere
  // with the working"). A disabled track still delivers frames — silence — so
  // the pipeline runs exactly as if you'd stopped talking: a sentence in
  // progress closes and is answered, a reply plays on, nothing is cleared.
  function setMuted(next) {
    muted = Boolean(next);
    if (micStream) micStream.getAudioTracks().forEach((t) => { t.enabled = !muted; });
    setOrbLevel(0);
    paintMute();
    if (phase === "listening") setPhase("listening");
  }

  // ---------- audio out: schedule raw PCM chunks back-to-back ----------
  function ensurePlayCtx() {
    if (!playCtx) {
      playCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: SAMPLE_RATE });
      // Every sentence plays through the volume, then a limiter.
      playGain = playCtx.createGain();
      playGain.gain.value = voiceVolume;
      const limiter = playCtx.createDynamicsCompressor();
      limiter.threshold.value = -3;
      limiter.knee.value = 4;
      limiter.ratio.value = 20;
      limiter.attack.value = 0.002;
      limiter.release.value = 0.2;
      playGain.connect(limiter);
      limiter.connect(playCtx.destination);
    }
    if (playCtx.state === "suspended") playCtx.resume();
    return playCtx;
  }

  function playPcm(bytes) {
    const ctx = ensurePlayCtx();
    const merged = new Uint8Array(pcmCarry.length + bytes.length);
    merged.set(pcmCarry);
    merged.set(bytes, pcmCarry.length);
    const usable = merged.length - (merged.length % 2);
    pcmCarry = merged.slice(usable);
    if (!usable) return;
    const i16 = new Int16Array(merged.buffer.slice(0, usable));
    const f32 = new Float32Array(i16.length);
    for (let i = 0; i < i16.length; i += 1) f32[i] = i16[i] / 32768;
    const buf = ctx.createBuffer(1, f32.length, SAMPLE_RATE);
    buf.getChannelData(0).set(f32);
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(playGain || ctx.destination);
    const lead = liveSources.length ? 0.02 : PLAYBACK_PRIME_S;
    const startAt = Math.max(ctx.currentTime + lead, nextPlayTime);
    src.start(startAt);
    nextPlayTime = startAt + buf.duration;
    liveSources.push(src);
    // "listening" too: between sentences the queue can drain for a moment,
    // and the next sentence must pull the phase back or tap-to-interrupt dies.
    // "connecting" covers the greeting, which plays before we ever listen.
    if (phase === "thinking" || phase === "listening" || phase === "connecting") setPhase("speaking");
    const playbackGeneration = audioGeneration;
    src.onended = () => {
      if (playbackGeneration !== audioGeneration || !liveSources.includes(src)) return;
      liveSources = liveSources.filter((s) => s !== src);
      micMutedUntil = Date.now() + ECHO_HANGOVER_MS;
      if (!liveSources.length && open && phase === "speaking") {
        const stillThinking = turnThinking && turnAbort && !turnAbort.signal.aborted;
        // Mid-greeting the label stays as it is; the greeting's own ending
        // lifts the hold and says "Listening".
        if (Date.now() < launchHoldUntil) return;
        setPhase(stillThinking ? "thinking" : "listening", stillThinking ? thinkingNote : "");
      }
    };
  }

  // The water sound on ready (spec decision 2): a short low plink (sine, 220 Hz
  // falling to 140 Hz over 180 ms, fast attack, exponential release) and a very
  // quiet filtered-noise tail, under 400 ms in all, synthesised here — no
  // asset. Through the playback gain and limiter, so the device's saved voice
  // volume applies. Never while muted; never with the greeting On (the spoken
  // word is the cue there); reduced-motion users still hear it.
  function playReadySound() {
    if (muted || !open) return;
    let ctx;
    try { ctx = ensurePlayCtx(); } catch (_e) { return; }
    const out = playGain || ctx.destination;
    const t0 = ctx.currentTime + 0.02;
    const osc = ctx.createOscillator();
    osc.type = "sine";
    osc.frequency.setValueAtTime(220, t0);
    osc.frequency.exponentialRampToValueAtTime(140, t0 + 0.18);
    const env = ctx.createGain();
    env.gain.setValueAtTime(0.0001, t0);
    env.gain.exponentialRampToValueAtTime(0.32, t0 + 0.008);
    env.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.3);
    osc.connect(env); env.connect(out);
    osc.start(t0); osc.stop(t0 + 0.32);
    const len = Math.floor(ctx.sampleRate * 0.25);
    const buf = ctx.createBuffer(1, len, ctx.sampleRate);
    const d = buf.getChannelData(0);
    for (let i = 0; i < len; i += 1) d[i] = Math.random() * 2 - 1;
    const noise = ctx.createBufferSource();
    noise.buffer = buf;
    const band = ctx.createBiquadFilter();
    band.type = "bandpass"; band.frequency.value = 900; band.Q.value = 0.9;
    const tail = ctx.createGain();
    tail.gain.setValueAtTime(0.0001, t0 + 0.02);
    tail.gain.exponentialRampToValueAtTime(0.045, t0 + 0.05);
    tail.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.27);
    noise.connect(band); band.connect(tail); tail.connect(out);
    noise.start(t0 + 0.02); noise.stop(t0 + 0.28);
  }

  function stopPlayback() {
    for (const src of liveSources) {
      try { src.stop(); } catch (_e) { /* already ended */ }
    }
    liveSources = [];
    nextPlayTime = 0;
    pcmCarry = new Uint8Array(0);
    micMutedUntil = Date.now() + ECHO_HANGOVER_MS;
  }

  // True while our own voice could be what the mic is hearing. NEVER true
  // while the USER is mid-utterance: if playback starts while they're already
  // talking, gating would drop their words and the buffer-clear would wipe
  // the ones the server had — the donor's reproduced "first words lost" bug.
  function micGated() {
    if (Date.now() < launchHoldUntil) return true;
    if (userSpeaking) return false;
    return liveSources.length > 0 || Date.now() < micMutedUntil;
  }

  // Did the segment actually stand out above the room? Music, the telly and
  // the dishwasher live at the floor; someone talking to their phone doesn't.
  // The level a segment has to reach to count as you. Automatic: well above
  // the rolling room level, never under MIN_SPEECH_PEAK. With a line set in
  // the settings, the line IS the gate (a barge-in needs a quarter more).
  function speechGate(margin = SPEECH_OVER_AMBIENT) {
    if (micLine) return micLine * (margin / SPEECH_OVER_AMBIENT);
    return Math.max(ambientLevel * margin, MIN_SPEECH_PEAK);
  }

  function segmentStoodOut(margin = SPEECH_OVER_AMBIENT) {
    return turns.activePeak() >= speechGate(margin);
  }

  // A plain thank-you needs no lookup and no model (Kris 2026-09-24: "Thank
  // you." ran a whole turn — the draft looked up again, 182 words written —
  // 8 s to say "you're welcome"). Only when nothing else is in the words;
  // "cheers" stays a goodbye (the panel's farewell list).
  const COURTESY_WORDS = new Set(["thank", "thanks", "you", "very", "much", "a", "lot", "ta",
    "lovely", "great", "brilliant", "perfect", "nice", "one", "cool", "fab", "fantastic", "that's", "thats", "ok", "okay"]);
  function isCourtesy(text) {
    const words = String(text || "").toLowerCase().match(/[a-z']+/g) || [];
    return words.length > 0 && words.length <= 5 && words.every((w) => COURTESY_WORDS.has(w))
      && words.some((w) => w === "thank" || w === "thanks" || w === "ta");
  }
  async function sayLocally(line) {
    const generation = audioGeneration;
    els.reply.textContent = line;
    await speakPhrase(line, audioAbort.signal).catch(() => {});
    await waitForPlaybackEnd(6000);
    if (open && generation === audioGeneration && phase === "speaking") setPhase("listening");
  }

  const INTERRUPT_WORDS = new Set(["stop", "cancel", "wait", "no", "hang", "hold", "actually"]);
  function meansToInterrupt(text) {
    const words = String(text || "").toLowerCase().match(/[a-z']+/g) || [];
    return words.length >= 2 || words.some((w) => INTERRUPT_WORDS.has(w));
  }

  // TTS in two halves (Kris 2026-09-24: "the text reply appears a couple of
  // seconds before the TTS fires"). FETCH starts the moment a sentence is
  // ready, buffering as it arrives; PLAY takes its turn in the queue. Before,
  // sentence two was only requested once sentence one had fully downloaded,
  // so every sentence paid the whole round trip in the open.
  function fetchTts(sentence, signal) {
    const parentSignal = signal;
    const controller = new AbortController();
    const abortRequest = () => controller.abort();
    if (parentSignal) parentSignal.addEventListener("abort", abortRequest, { once: true });
    if (parentSignal && parentSignal.aborted) controller.abort();
    signal = controller.signal;
    const buf = { chunks: [], done: false, wake: null };
    const poke = () => { const w = buf.wake; buf.wake = null; if (w) w(); };
    let reader = null;
    const aborted = () => {
      if (reader) reader.cancel().catch(() => {});
      buf.done = true; buf.chunks = []; poke();
    };
    if (signal) signal.addEventListener("abort", aborted, { once: true });
    (async () => {
      try {
        const r = await stallGuard(deps.apiFetch("tts", { text: sentence, voice: ttsVoice }, signal), TURN_STALL_MS);
        if (r.ok && r.body) {
          reader = r.body.getReader();
          for (;;) {
            const { done, value } = await stallGuard(reader.read(), TURN_STALL_MS);
            if (done || (signal && signal.aborted)) {
              if (signal && signal.aborted) reader.cancel().catch(() => {});
              break;
            }
            buf.chunks.push(value);
            poke();
          }
        }
      } catch (_e) {
        controller.abort();
        if (reader) reader.cancel().catch(() => {});
      }
      if (parentSignal) parentSignal.removeEventListener("abort", abortRequest);
      signal.removeEventListener("abort", aborted);
      buf.done = true;
      poke();
    })();
    return buf;
  }
  // onStart fires as the first audio is scheduled — the words go on screen
  // WITH the voice, not seconds ahead of it.
  async function playTts(buf, signal, onStart) {
    let i = 0;
    let started = false;
    for (;;) {
      while (i < buf.chunks.length) {
        if (signal && signal.aborted) return;
        if (!started) { started = true; if (onStart) onStart(); }
        playPcm(buf.chunks[i]);
        i += 1;
      }
      if (buf.done) return;
      await new Promise((resolve) => { buf.wake = resolve; });
    }
  }
  // One utterance, fetched and played: the greeting and one-off lines.
  async function streamTtsSentence(sentence, signal, onStart) {
    if (!sentence.trim() || (signal && signal.aborted)) return;
    await playTts(fetchTts(sentence, signal), signal, onStart);
  }

  // Spoken commentary (Kris 2026-09-23: "when it's thinking/searching stuff it
  // is quiet"). The chip's own words are a small fixed set ("Checking your
  // inbox…"), so each is fetched from TTS once per session and replayed from
  // memory after that: no second round trip, no second charge.
  const phraseCache = new Map();
  // The phone's own answers are a handful of fixed lines: fetched once when
  // the conversation starts, so "Sent." and "You're welcome." play at once.
  const LOCAL_LINES = ["Sent.", "You're welcome.", "Okay — it's waiting in the thread.", "Okay — I'll leave it.",
    "Sorry — was that a yes? Say yes or no."];
  async function warmPhrase(text) {
    const generation = audioGeneration;
    const signal = audioAbort.signal;
    const cacheKey = ttsVoice + ':' + text;
    if (phraseCache.has(cacheKey)) return;
    try {
      const r = await deps.apiFetch("tts", { text, voice: ttsVoice }, signal);
      if (!r.ok || !r.body || signal.aborted || generation !== audioGeneration) return;
      const reader = r.body.getReader();
      const chunks = [];
      for (;;) {
        const { done, value } = await reader.read();
        if (signal.aborted || generation !== audioGeneration) { reader.cancel().catch(() => {}); return; }
        if (done) break;
        chunks.push(value);
      }
      phraseCache.set(cacheKey, chunks);
    } catch (_e) { /* played live instead */ }
  }
  async function speakPhrase(text, signal) {
    if (signal && signal.aborted) return;
    const cacheKey = ttsVoice + ':' + text;
    const cached = phraseCache.get(cacheKey);
    if (cached) {
      if (!(signal && signal.aborted)) cached.forEach(playPcm);
      return;
    }
    const controller = new AbortController();
    const abortRequest = () => controller.abort();
    if (signal) signal.addEventListener("abort", abortRequest, { once: true });
    let reader = null;
    const cancelReader = () => { if (reader) reader.cancel().catch(() => {}); };
    controller.signal.addEventListener("abort", cancelReader, { once: true });
    try {
      const r = await stallGuard(deps.apiFetch("tts", { text, voice: ttsVoice }, controller.signal), TURN_STALL_MS);
      if (!r.ok || !r.body || controller.signal.aborted) return;
      reader = r.body.getReader();
      const chunks = [];
      for (;;) {
        const { done, value } = await stallGuard(reader.read(), TURN_STALL_MS);
        if (controller.signal.aborted) break;
        if (done) { phraseCache.set(cacheKey, chunks); break; }
        chunks.push(value);
        playPcm(value);
      }
    } finally {
      if (signal) signal.removeEventListener("abort", abortRequest);
      controller.abort();
      cancelReader();
    }
  }

  // Resolves when the speaker has actually finished, so a goodbye can be
  // heard in full before the session tears down.
  function waitForPlaybackEnd(maxMs) {
    return new Promise((resolve) => {
      const startedAt = Date.now();
      const tick = () => {
        if (!liveSources.length || Date.now() - startedAt > maxMs) return resolve();
        setTimeout(tick, 120);
      };
      tick();
    });
  }

  function paintDiscard() {
    if (!els.transcript) return;
    const heard = (els.transcript.textContent || "").trim();
    const live = Boolean(turnInFlight || turns.hasPending());
    const on = Boolean(heard && heard !== "…" && live && !heldWords);
    els.transcript.classList.toggle("is-strikable", on);
    if (on) {
      els.transcript.setAttribute("role", "button");
      els.transcript.setAttribute("tabindex", "0");
      els.transcript.setAttribute("aria-label", "Cancel: " + heard);
    } else {
      els.transcript.removeAttribute("role");
      els.transcript.removeAttribute("tabindex");
      els.transcript.removeAttribute("aria-label");
    }
  }

  // ---------- the Tour ----------
  // First-use explanation of this screen. Add a step here when the voice
  // Concierge gains something worth explaining; `spot` names the control that
  // glows while the step is showing.
  const TOUR_STEPS = [
    { title: "Just talk", spot: null,
      body: "It's listening the whole time. Ask anything about your business — pause when you're done and it answers out loud." },
    { title: "Tap the mic to mute", spot: ".vl-mic",
      body: "Green means it's listening; red with a line through it means muted. Anything you'd already said still gets answered." },
    { title: "Tap the orb to interrupt", spot: ".vl-orb",
      body: "Tap anywhere on the orb while it's talking and it stops. You can also just start speaking." },
    { title: "Discard an unintended request", spot: ".vl-discard",
      body: "Tap Discard request if it picked up something you did not mean. Cancellation is confirmed only before work starts; check the saved result if it is too late." },
  ];
  const TOUR_SEEN_KEY = "blam8.voiceTourSeen";
  let tourStep = 0;

  function openTour() {
    tourStep = 0;
    try { localStorage.setItem(TOUR_SEEN_KEY, String(TOUR_STEPS.length)); } catch (_e) { /* private mode */ }
    renderTour();
  }

  function closeTour() {
    els.tour.hidden = true;
    root.querySelectorAll(".vl-tour-spot").forEach((n) => n.classList.remove("vl-tour-spot"));
  }

  function renderTour() {
    const step = TOUR_STEPS[tourStep];
    root.querySelectorAll(".vl-tour-spot").forEach((n) => n.classList.remove("vl-tour-spot"));
    if (step.spot) {
      const target = root.querySelector(step.spot);
      if (target) target.classList.add("vl-tour-spot");
    }
    els.tour.textContent = "";
    const h = document.createElement("h3");
    h.textContent = step.title;
    const p = document.createElement("p");
    p.textContent = step.body;
    const foot = document.createElement("div");
    foot.className = "vl-tour-foot";
    const count = document.createElement("span");
    count.className = "vl-tour-step";
    count.textContent = `${tourStep + 1} of ${TOUR_STEPS.length}`;
    const btns = document.createElement("div");
    if (tourStep > 0) {
      const back = document.createElement("button");
      back.type = "button";
      back.textContent = "Back";
      back.addEventListener("click", () => { tourStep -= 1; renderTour(); });
      btns.appendChild(back);
    }
    const last = tourStep === TOUR_STEPS.length - 1;
    const next = document.createElement("button");
    next.type = "button";
    next.className = "is-primary";
    next.textContent = last ? "Got it" : "Next";
    next.addEventListener("click", () => {
      if (last) closeTour();
      else { tourStep += 1; renderTour(); }
    });
    btns.appendChild(next);
    foot.append(count, btns);
    els.tour.append(h, p, foot);
    els.tour.hidden = false;
  }

  // Submitted speech is removed only after the server accepts cancellation.
  // A write helper already entered may finish; keep consuming its result silently.
  async function discardHeard() {
    const turn = turnInFlight;
    if (turn) {
      turn.silent = true;
      turn.playback.abort();
      stopPlayback();
      notify("Cancellation requested…");
      let outcome = { status: "unknown" };
      try {
        const response = await deps.apiFetch("discard", { turn_id: turn.id });
        if (response.ok) outcome = await response.json();
      } catch (_e) { /* lost response is not acceptance */ }
      if (turnInFlight !== turn || !open) return;
      if (outcome.status !== "cancelled") {
        notify(outcome.status === "too_late"
          ? "Work already started — check the thread and waiting list."
          : "Cancellation unconfirmed — check the thread and waiting list.");
        return;
      }
      turnInFlight = null;
      turn.controller.abort();
      if (deps.onDiscardTurn) deps.onDiscardTurn(turn.id);
      els.reply.textContent = "";
    }
    if (bargeTimer) { clearTimeout(bargeTimer); bargeTimer = 0; }
    logHeard("struck", { text: els.transcript.textContent || "" }, "tapped");
    turns.discard();
    clearHeld();
    captionBuffer = "";
    userSpeaking = false;
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "input_audio_buffer.clear" }));
    els.transcript.textContent = "";
    notify("Cancelled before work started");
    paintDiscard();
    if (open) setPhase("listening");
  }

  // ---------- one assistant turn: SSE speak-stream -> sentence TTS ----------
  // activeTurn marks the turn as running for exactly its lifetime, however it
  // ends; a newer turn that has already started keeps its own mark.
  async function runTurn(text, meta = {}) {
    const mine = {};
    activeTurn = mine;
    try {
      await runAssistantTurn(text, meta);
    } finally {
      if (activeTurn === mine) activeTurn = null;
    }
  }

  async function runAssistantTurn(text, meta = {}) {
    delivery.interrupt();
    const turnId = Math.random().toString(36).slice(2, 12) + Date.now().toString(36);
    const turn = { id: turnId, silent: false, controller: null, playback: new AbortController() };
    const settle = () => {
      if (turnInFlight && turnInFlight.id === turnId) turnInFlight = null;
      paintDiscard();
    };
    // The next question takes over: the one still running is cancelled here,
    // not by whatever sound came first.
    if (turnAbort && !turnAbort.signal.aborted) turnAbort.abort();
    turnInFlight = null;
    clearHeld();
    setPhase("thinking");
    captionBuffer = "";
    // Deliberately NOT navigating: whichever page the user is browsing, a new
    // turn talks over it without stealing the screen.
    els.reply.textContent = "";
    const controller = new AbortController();
    turn.controller = controller;
    turnAbort = controller;
    const signal = controller.signal;
    signal.addEventListener("abort", () => turn.playback.abort(), { once: true });
    const speechSignal = turn.playback.signal;
    const current = () => open && turnAbort === controller && !signal.aborted;
    stopPlayback();

    spokenSinceCard += Number(meta.durationMs) || 0;
    let outcome = {};
    try {
      outcome = (await deps.onUserTurn(text, { ...meta, turnId })) || {};
    } catch (_e) { /* the turn still runs */ }
    if (!current()) return settle();
    if (outcome.confirmed) notify("Done — confirmed ✓");

    // Locally-decided turns: the ack is spoken, nothing goes to the model.
    if (outcome.farewell) {
      await streamTtsSentence(outcome.ack || "Bye for now.", signal).catch(() => {});
      await waitForPlaybackEnd(6000);
      if (current()) stop();
      return;
    }
    // Answered here, not by the model: the exchange is saved all the same, or
    // after a reload the history would still end on the question (1.2).
    if ((outcome.declined || outcome.answered) && outcome.ack) {
      deps.apiFetch("local", { question: text, answer: outcome.ack, display_turn_id: turnId }).catch(() => {});
    }
    // A yes or no to the card is part of that card's confirm time, not the next ask.
    if (outcome.declined || outcome.answered) spokenSinceCard = 0;
    if (outcome.declined) {
      notify("Okay — left it");
      speakPhrase(outcome.ack || "Okay — I'll leave it.", signal).catch(() => {});
      refreshStage(false);
      return;
    }
    // A yes to what's on screen is answered HERE, from what the change itself
    // reported ("Done — Bella's Feeding instructions is now …"): no second
    // model turn to go quiet or be cut off (Kris 2026-09-23: the yes worked,
    // nothing was said, and it read as a yes that hadn't).
    if (outcome.answered) {
      refreshStage(false);
      els.reply.textContent = outcome.ack || "";
      // A fixed line ("Sent.") plays from the cache; a result that names
      // the change ("Done — Bella's…") is fetched as it comes.
      if (outcome.ack) {
        await (LOCAL_LINES.includes(outcome.ack) ? speakPhrase(outcome.ack, signal)
          : streamTtsSentence(outcome.ack, signal)).catch(() => {});
      }
      await waitForPlaybackEnd(15000);
      if (!current()) return settle();
      settle();
      if (open && !signal.aborted) setPhase("listening");
      return;
    }
    if (outcome.confirmed) refreshStage(false);

    turnThinking = true;
    thinkingNote = "";
    // Commentary (Kris 2026-09-23: "queue the chip TTS, we miss some due to
    // the last not being finished"): every chip is said, in the order the
    // chips appeared, each after the one before has finished. They join the
    // same queue as the reply's sentences, so the voice never talks over
    // itself and the order you hear is the order things happened. The only
    // line skipped is an exact repeat of the one just queued ("Looking up
    // contacts. Looking up contacts.").
    let lastHint = "";
    const sayHint = (line) => {
      if (!current() || turn.silent || line === lastHint) return;
      lastHint = line;
      ttsChain = ttsChain
        .then(() => { if (current() && !turn.silent) return speakPhrase(line, speechSignal); })
        .catch(() => { /* a failed line must not sink the queue */ });
    };
    let pending = "";
    let spokenSoFar = "";
    let shown = "";          // the words on screen: each sentence as its voice starts
    let ttsChain = Promise.resolve();

    const speak = (sentence) => {
      if (!current() || turn.silent) return;
      const buf = fetchTts(sentence, speechSignal);          // requested NOW…
      const reveal = () => {
        if (!current() || turn.silent) return;
        shown = shown ? `${shown} ${sentence}` : sentence;
        els.reply.textContent = shown;
      };
      ttsChain = ttsChain
        .then(() => { if (current() && !turn.silent) return playTts(buf, speechSignal, reveal); })       // …played in its turn
        .catch(() => { /* a failed sentence must not sink the queue */ });
    };

    let reader = null;
    const cancelReader = () => { if (reader) reader.cancel().catch(() => {}); };
    signal.addEventListener("abort", cancelReader, { once: true });
    try {
      turnInFlight = turn;
      const body = deps.buildChatBody();
      if (meta.clipped) body.clipped = true;
      body.turn_id = turnId;
      body.voice_session = voiceSession;
      body.spoken_ms = Math.round(spokenSinceCard);
      if (meta.debrief) body.debrief = meta.debrief;   // the Debrief button: "start" | "next"
      const response = await stallGuard(deps.apiFetch("chat", body, signal), TURN_STALL_MS);
      if (!current()) return;
      if (!response.ok || !response.body) {
        const payload = await response.json().catch(() => ({}));
        if (!current()) return;
        setPhase("error", payload?.error || "The concierge couldn't respond.");
        // 402 = trial / fair-use cap: the server's message is the honest one.
        speak(response.status === 402 && payload?.error
          ? String(payload.error).split(".")[0] + "."
          : "Sorry, I couldn't get an answer just then.");
        await ttsChain;
        if (current()) setPhase("listening");
        return;
      }

      reader = response.body.getReader();
      const decoder = new TextDecoder();
      let sse = "";
      let finalPayload = null;
      for (;;) {
        // A server that has gone quiet mid-turn (a deploy restarting under
        // the request, live 2026-09-23: "stuck thinking for ages") must not
        // hold the screen forever: 45 s without a frame ends the turn.
        const { done, value } = await stallGuard(reader.read(), TURN_STALL_MS);
        if (done || !current()) {
          if (!current()) reader.cancel().catch(() => {});
          break;
        }
        sse += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = sse.indexOf("\n\n")) >= 0) {
          const frame = sse.slice(0, idx);
          sse = sse.slice(idx + 2);
          if (!frame.startsWith("data: ")) continue;
          let msg;
          try { msg = JSON.parse(frame.slice(6)); } catch (_e) { continue; }
          if (msg.t === "cancelled") {
            if (deps.onDiscardTurn) deps.onDiscardTurn(turnId);
            controller.abort();
            stopPlayback();
            els.reply.textContent = "";
            els.transcript.textContent = "";
            notify("Cancelled before work started");
            setPhase("listening");
            break;
          }
          if (msg.t === "speak" && typeof msg.text === "string") {
            turnThinking = false;
            lastHint = "";
            // A reply sent back to do its lookup carries on in a new round:
            // "…tomorrow." + "Nothing in…" needs the space the model didn't write.
            const glue = (/[.!?]$/.test(spokenSoFar) && /^[A-Za-z]/.test(msg.text)) ? " " : "";
            pending += glue + msg.text;
            spokenSoFar += glue + msg.text;
            // Not painted here: each sentence appears as its voice starts.
            const { sentences, rest } = splitReadySentences(pending);
            pending = rest;
            for (const sentence of sentences) speak(sentence);
          }
          if (msg.t === "tool") {
            thinkingNote = "Checking for you…";
            if (phase !== "speaking") setPhase("thinking", thinkingNote);
            // The server already words the honest chip ("Checking your
            // inbox…") and its sanitised detail — no client-side map needed.
            const base = msg.hint || "Looking that up…";
            // An email address in the chip is too long for the top of the
            // screen and adds nothing the lookup's own words don't say (Kris
            // 2026-09-24): the detail shows only when it isn't one.
            const detail = msg.detail && !String(msg.detail).includes("@") ? msg.detail : "";
            notify(detail ? `${base.replace(/…$/, "")} (${detail})…` : base);
            // Say the chip out loud: the hint only. The detail is a search
            // term, which can be a customer's name or address.
            const line = base.replace(/…$/, ".");
            // A lookup means the answer is waiting on it, even if some of the
            // reply was already said: the orb is thinking again.
            turnThinking = true;
            sayHint(line);
          }
          if (msg.t === "error") setPhase("error", msg.error || "The concierge couldn't respond.");
          if (msg.t === "done") finalPayload = msg;
        }
      }
      if (!current()) return;
      turnThinking = false;
      if (pending.trim() && !signal.aborted) speak(pending.trim());

      if (finalPayload && !signal.aborted) {
        // The words already spoken were written expecting a card that
        // couldn't be set up ("want me to go ahead?") — say what actually
        // happened, so a "yes" never lands on nothing (live 2026-09-23).
        if (finalPayload.correction) {
          spokenSoFar = finalPayload.correction;
          shown = "";
          els.reply.textContent = spokenSoFar;
          speak(finalPayload.correction);
        }
        settle();   // answered: it's in the chat now, past striking
        deps.onAssistantTurn(finalPayload);
        if ((finalPayload.proposal && finalPayload.proposal.proposal_id) || finalPayload.draft) {
          spokenSinceCard = 0;
        }
        if (finalPayload.proposal && finalPayload.proposal.proposal_id) {
          notify(finalPayload.proposal.tap_only ? "1 change ready — tap Confirm on the card" : "1 change ready — say yes to confirm");
        }
        // "Show while it speaks": a reply that carries something visual slides
        // its page in; the voice keeps talking over it.
        refreshStage(true);
      }
      await ttsChain;
      if (!current()) return;
      // Whatever the voice couldn't say (a failed sentence) still reads in full.
      if (!signal.aborted && spokenSoFar && els.reply.textContent !== spokenSoFar
          && !(finalPayload && finalPayload.correction)) els.reply.textContent = spokenSoFar;
      settle();
      if (open && phase !== "speaking" && !liveSources.length) setPhase("listening");
    } catch (error) {
      if (turnAbort !== controller || !open) return;
      const wasAborted = signal.aborted;
      turnThinking = false;
      controller.abort();
      cancelReader();
      stopPlayback();
      if (!wasAborted) {
        // Timeout/transport failure requests cancellation; a lost response
        // cannot establish acceptance or justify removing local history.
        deps.apiFetch("discard", { turn_id: turnId }).catch(() => {});
        const stalled = error && error.message === "stalled";
        setPhase("error", stalled ? "That took too long — check the thread before trying again." : "Lost the connection — check the thread and waiting list.");
        setTimeout(() => { if (open && turnAbort === controller && phase === "error") setPhase("listening"); }, 2500);
      }
    } finally {
      signal.removeEventListener("abort", cancelReader);
      if (reader) reader.cancel().catch(() => {});
      settle();
    }
  }

  // The Debrief button (src/app/debrief_walk.py). The owner's request goes through the
  // same door as anything they say — runTurn — carrying the flag the server reads
  // ("start" builds today's agenda and begins; "next" moves it on). Not while the
  // screen is still connecting or has stopped listening: a tap then belongs to the
  // mic, and the debrief would talk into a connection that is not there.
  function startDebrief(step) {
    if (!open) return false;
    if (phase === "connecting" || phase === "paused" || phase === "error" || phase === "idle") {
      notify(phase === "connecting" ? "One moment — still connecting" : "Tap the mic first");
      return false;
    }
    const next = step === "next";
    runTurn(next ? DEBRIEF_NEXT : DEBRIEF_START, { debrief: next ? "next" : "start" });
    return true;
  }

  function bargeIn() {
    delivery.interrupt();
    if (bargeTimer) {
      clearTimeout(bargeTimer);
      bargeTimer = 0;
    }
    const busy = turnBusy() || liveSources.length > 0;
    if (turnAbort) turnAbort.abort();
    stopPlayback();
    return busy;
  }

  // ---------- mic + realtime STT ----------
  function floatToPcm16(f32) {
    const i16 = new Int16Array(f32.length);
    for (let i = 0; i < f32.length; i += 1) {
      const s = Math.max(-1, Math.min(1, f32[i]));
      i16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    return i16;
  }

  function packBase64(parts) {
    let total = 0;
    for (const p of parts) total += p.length;
    const merged = new Int16Array(total);
    let off = 0;
    for (const p of parts) { merged.set(p, off); off += p.length; }
    const bytes = new Uint8Array(merged.buffer);
    let bin = "";
    for (let i = 0; i < bytes.length; i += 8192) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 8192));
    }
    return btoa(bin);
  }

  let audioAbort = new AbortController();
  let micReady = false;
  let audioGeneration = 0;
  let connectionGeneration = 0;

  async function start() {
    bargeIn();
    teardownAudio();
    audioAbort = new AbortController();
    const generation = ++audioGeneration;
    const current = () => open && audioGeneration === generation;
    ttsVoice = savedConciergeVoice();
    phraseCache.clear();
    micLine = savedMicLine();   // set in the settings since the last conversation
    voiceVolume = savedVoiceVolume();
    if (playGain) playGain.gain.value = voiceVolume;
    const gate = deps.canStart ? deps.canStart() : { ok: true };
    if (!gate.ok) {
      setPhase("error", gate.reason || "Voice isn't available right now.");
      return;
    }
    setPhase("connecting");
    ensurePlayCtx(); // inside the click gesture, or iOS never lets audio out
    // The MIC engine too, made right here while the finger is still down:
    // one created after the token fetch and getUserMedia had waited came up
    // "running" and delivered 0 frames a second after an unlock (live
    // 2026-09-24, the diagnostic line: mic 0 f/s · engine running · track live).
    if (!micCtx || micCtx.state === "closed") micCtx = new AudioContext({ sampleRate: SAMPLE_RATE });
    if (micCtx.state !== "running") micCtx.resume().catch(() => {});
    const engine = micCtx;

    try {
      const tokenRes = await stallGuard(deps.apiFetch("token", { silence_ms: 500, noise: noiseKind() }, audioAbort.signal), TURN_STALL_MS);
      const token = await stallGuard(tokenRes.json().catch(() => ({})), TURN_STALL_MS);
      if (!current()) return;
      if (!tokenRes.ok || !token.value) {
        teardownAudio();
        setPhase("error", token?.error || "Couldn't start listening.");
        return;
      }

      const acquisition = navigator.mediaDevices.getUserMedia({
        // echoCancellation helps where the platform honours it, but Web Audio
        // playback is outside the canceller's reference on many phones — the
        // real protection is the mic gate around playback (micGated above).
        // autoGainControl is OFF deliberately: after loud TTS playback, AGC
        // has wound the mic gain down and ramps it back over ~1s, recording
        // the user's first words too quietly for the VAD to trigger on.
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: false, channelCount: 1 }
      }).then((stream) => {
        if (!current()) stream.getTracks().forEach((track) => track.stop());
        return stream;
      });
      const acquiredStream = await stallGuard(acquisition, TURN_STALL_MS);
      // Muted while it was still getting ready: setMuted had no tracks to
      // switch off then, so the mic came up LIVE under a muted orb (Kris
      // 2026-09-23: muted on opening, walked away, and the room became a
      // turn). The tracks take the mute state the moment they exist.
      if (!current() || micCtx !== engine) {
        acquiredStream.getTracks().forEach((track) => track.stop());
        return;
      }
      micStream = acquiredStream;
      micStream.getAudioTracks().forEach((t) => { t.enabled = !muted; });
      const workletUrl = URL.createObjectURL(new Blob([WORKLET_SOURCE], { type: "text/javascript" }));
      try { await stallGuard(engine.audioWorklet.addModule(workletUrl), TURN_STALL_MS); }
      finally { URL.revokeObjectURL(workletUrl); }
      if (!current() || micCtx !== engine) {
        acquiredStream.getTracks().forEach((track) => track.stop());
        return;
      }
      micReady = true;
      const source = micCtx.createMediaStreamSource(micStream);
      const capture = new AudioWorkletNode(micCtx, "pcm-capture");
      source.connect(capture);

      openRealtime(token);

      let batch = [];
      let batchSamples = 0;
      capture.port.onmessage = (event) => {
        if (!current() || micCtx !== engine) return;
        dbgFrames += 1;
        // A real mic never delivers exact zeros; two seconds of them while
        // unmuted is a dead track (iOS after an interruption) — say so, and
        // the next tap rebuilds the whole chain.
        if (!muted && phase !== "error") {
          let allZero = true;
          for (let i = 0; i < event.data.length; i += 1) { if (event.data[i] !== 0) { allZero = false; break; } }
          if (!allZero) zeroSince = 0;
          else if (!zeroSince) zeroSince = Date.now();
          else if (Date.now() - zeroSince > DEAD_MIC_MS) {
            setPhase("error", "The mic went quiet — tap the mic to carry on.");
            return;
          }
        }
        if (!ws || ws.readyState !== 1) return;
        if (micGated()) {
          if (!micGateAnnounced) {
            micGateAnnounced = true;
            batch = [];
            batchSamples = 0;
            gateTailFrames = [];
            // Discard whatever echo reached the server before the gate closed,
            // or it becomes a phantom "user turn" in the assistant's own words.
            ws.send(JSON.stringify({ type: "input_audio_buffer.clear" }));
          }
          // While audio is actually PLAYING the mic hears the speaker — drop.
          // During the silent hangover tail it hears the ROOM — keep those
          // frames, because someone answering quickly starts talking exactly
          // there, and dropping them ate their first words.
          if (!liveSources.length && Date.now() >= launchHoldUntil) {
            gateTailFrames.push(floatToPcm16(event.data));
            // Worklet frames are 128 samples (~5ms) — 200 of them is ~1s.
            if (gateTailFrames.length > 200) gateTailFrames.shift();
          } else {
            gateTailFrames = [];
          }
          return;
        }
        if (micGateAnnounced) {
          micGateAnnounced = false;
          gateOpenedAt = Date.now();
          if (gateTailFrames.length) {
            ws.send(JSON.stringify({ type: "input_audio_buffer.append", audio: packBase64(gateTailFrames) }));
            gateTailFrames = [];
          }
        }
        const pcm = floatToPcm16(event.data);
        let peak = 0;
        for (let i = 0; i < event.data.length; i += 1) {
          const v = Math.abs(event.data[i]);
          if (v > peak) peak = v;
        }
        // Every frame's loudness: the assembler keeps the last moments, so a
        // one-word "Yes" is measured from the word, not the silence after it.
        turns.notePeak(peak, Date.now());
        setOrbLevel(Math.min(1, peak * 2.5));
        if (!userSpeaking && !muted) {
          // The floor learns the room only when nobody is talking, so speech
          // can never teach the floor to reject speech — nor muted silence
          // teach it the room is quieter than it is.
          ambientLevel = ambientLevel * (1 - AMBIENT_EMA) + peak * AMBIENT_EMA;
        }
        batch.push(pcm);
        batchSamples += event.data.length;
        if (batchSamples >= MIC_BATCH_SAMPLES) {
          const frame = JSON.stringify({ type: "input_audio_buffer.append", audio: packBase64(batch) });
          ws.send(frame);
          dbgSent += frame.length;
          if (captionsWs && captionsWs.readyState === 1) captionsWs.send(frame);
          batch = [];
          batchSamples = 0;
        }
      };

    } catch (error) {
      if (!current()) return;
      // getUserMedia denial lands here — the one the user has to fix themselves.
      const denied = error && (error.name === "NotAllowedError" || error.name === "SecurityError");
      setPhase("error", denied
        ? "Microphone access was blocked — allow it in your browser settings and try again."
        : "Couldn't start the microphone.");
      teardownAudio();
    }
  }

  // Which of the speech service's noise filters suits this device: a phone
  // is held close ("near"), a laptop hears the room from across the desk.
  function noiseKind() {
    try { return window.matchMedia("(pointer: coarse)").matches ? "near" : "far"; }
    catch (_e) { return "near"; }
  }

  // The captions session paints words AS THEY ARE SPOKEN (gpt-live-transcribe
  // streams deltas mid-speech, ~1.5s behind the voice). It emits no turn
  // events at all, so nothing here feeds the turn engine — display only, and
  // any failure just means captions quietly don't happen.
  async function openCaptions() {
    const generation = connectionGeneration;
    try {
      const res = await stallGuard(deps.apiFetch("token", { purpose: "captions" }, audioAbort.signal), TURN_STALL_MS);
      const token = await stallGuard(res.json().catch(() => ({})), TURN_STALL_MS);
      if (!res.ok || !token.value || !open || generation !== connectionGeneration) return;
      captionsWs = new WebSocket("wss://api.openai.com/v1/realtime", [
        "realtime",
        "openai-insecure-api-key." + token.value
      ]);
      const socket = captionsWs;
      const current = () => open && generation === connectionGeneration && captionsWs === socket;
      captionsWs.onmessage = (event) => {
        if (!current()) return;
        let msg;
        try { msg = JSON.parse(event.data); } catch (_e) { return; }
        if (String(msg.type || "").endsWith("input_audio_transcription.delta") && typeof msg.delta === "string") {
          // The captions model has no VAD — it transcribes EVERYTHING,
          // background music lyrics included. Only paint words that belong to
          // a real speech segment the main session's VAD is tracking.
          const speechish = userSpeaking || (speechStartedAt && Date.now() - speechStartedAt < 1500);
          if (!speechish) return;
          captionsLive = true;
          captionBuffer += msg.delta;
          if (captionBuffer.trim()) els.transcript.textContent = captionBuffer.trim();
        }
      };
      captionsWs.onclose = () => { if (current()) { captionsWs = null; captionsLive = false; } };
      captionsWs.onerror = () => { /* captions are best-effort */ };
    } catch (_e) { /* best-effort */ }
  }

  function teardownAudio() {
    audioAbort.abort();
    micReady = false;
    ++audioGeneration;
    ++connectionGeneration;
    if (ws) {
      try { ws.close(1000); } catch (_e) { /* already closed */ }
      ws = null;
    }
    if (captionsWs) {
      try { captionsWs.close(1000); } catch (_e) { /* already closed */ }
      captionsWs = null;
    }
    captionsLive = false;
    captionBuffer = "";
    if (micStream) {
      for (const track of micStream.getTracks()) track.stop();
      micStream = null;
    }
    if (micCtx) {
      micCtx.close().catch(() => {});
      micCtx = null;
    }
  }

  // The listening connection, apart from the mic: start() opens it once the
  // mic is up; reconnect() opens a fresh one on the SAME mic.
  function openRealtime(token) {
    ++connectionGeneration;
    const local = token.provider === "local";
    localSpeech = local;
    localCaptionItem = null;
    if (local && !["af_heart", "bf_emma", "bm_george"].includes(ttsVoice)) ttsVoice = token.voice || "af_heart";
    if (!local && !["marin", "cedar", "alloy", "sage", "coral"].includes(ttsVoice)) ttsVoice = "marin";
    ws = new WebSocket(local ? token.websocket_url : "wss://api.openai.com/v1/realtime", [
      "realtime", (local ? "blam8-ticket." : "openai-insecure-api-key.") + token.value
    ]);
    if (!local) openCaptions(); // local captions share the main connection
    attachRealtimeHandlers();
  }

  function attachRealtimeHandlers() {
      const socket = ws;
      const current = () => open && ws === socket;
      ws.onopen = () => {
        if (!current()) return;
        const hello = deps.greeting ? String(deps.greeting() || "").trim() : "";
        // The phone's fixed answers, fetched once now so they play at once.
        setTimeout(() => { if (current()) LOCAL_LINES.forEach(warmPhrase); }, hello ? 2500 : 0);
        // Connected: the in-flight ring finishes, no new loading ring (spec §3, settling).
        if (ripples) ripples.settle();
        if (!hello) {
          // Greeting Off: no TTS at all. Stillness, the green ring with its
          // water sound, a moment of quiet, then the mic opens (READY_* above).
          const holdMs = READY_STILL_MS + READY_SOUND_MS + READY_QUIET_MS;
          launchHoldUntil = Date.now() + holdMs;
          setTimeout(() => { if (!current()) return; if (ripples) ripples.ready(); playReadySound(); }, 0);
          setTimeout(() => {
            if (!current()) return;
            launchHoldUntil = 0;
            if (open && phase === "connecting") setPhase("listening");
          }, holdMs);
          return;
        }
        launchHoldUntil = Date.now() + GREETING_HOLD_MAX_MS;
        const holdLifted = () => { if (current()) launchHoldUntil = 0; };
        // The greeting plays FIRST and "Listening" appears only after it has
        // finished coming out of the speaker. The earlier ordering showed
        // "Listening — just talk" while the greeting was still fetching, the
        // user answered the label, and the gate ate their opening words.
        turnAbort = new AbortController();
        streamTtsSentence(hello, turnAbort.signal)
          .catch(() => {})
          .then(() => waitForPlaybackEnd(GREETING_HOLD_MAX_MS))
          .then(holdLifted)
          .then(() => {
            if (!current()) return;
            // Greeting On: the ready ring waits for the spoken word to finish
            // ("Listening" is what the ring announces); no water sound.
            if (open && ripples) ripples.ready();
            if (open && (phase === "connecting" || phase === "speaking")) setPhase("listening");
          });
      };
      ws.onclose = (event) => {
        if (!current()) return;
        // 1000 is our own teardown; anything else deserves a visible fault.
        if (event.code !== 1000) setPhase("error", "Listening stopped — tap the mic to carry on.");
      };
      ws.onerror = () => { if (current()) setPhase("error", "Couldn't reach the listening service — tap the mic to try again."); };
      ws.onmessage = (event) => {
        if (!current()) return;
        let msg;
        try { msg = JSON.parse(event.data); } catch (_e) { return; }
        const type = msg.type || "";
        // Every event goes to the assembler by its item id; it alone decides
        // when you've finished (voice-turns.js). What's left here is the
        // voice's own business: barge-in and the "…" you're-being-heard cue.
        const itemId = msg.item_id || "";
        if (type.endsWith("input_audio_transcription.partial")) {
          if (userSpeaking && itemId === localCaptionItem && typeof msg.transcript === "string" && msg.transcript.trim()) {
            els.transcript.textContent = msg.transcript.trim();
          }
          return;
        }
        const now = Date.now();
        if (type.endsWith("speech_started")) {
          // Started while our own voice was playing: kept (it may be you,
          // starting just before the reply), marked so an empty one — the
          // echo — ends quietly. It is never a barge-in.
          const echo = micGated();
          if (!echo) {
            userSpeaking = true;
            localCaptionItem = itemId;
            delivery.interrupt();
            speechStartedAt = now;
            if (!turns.hasPending()) captionBuffer = "";
          }
          turns.speechStarted({
            itemId, now, echo,
            clipped: gateOpenedAt > 0 && now - gateOpenedAt < CLIP_WINDOW_MS,
          });
          if (echo) return;
          if (phase === "listening") els.status.textContent = "Hearing you…";
          // Only a reply that's still coming or still playing can be
          // interrupted; a finished one stays on screen while you answer it.
          const busy = liveSources.length > 0 || turnBusy();
          if (busy && !bargeTimer) {
            bargeTimer = setTimeout(() => {
              bargeTimer = 0;
              // Sustained AND clearly louder than the room — music playing
              // through a sentence gap satisfies the first but not the second,
              // and wrongly cancelling a reply is the costliest mistake here.
              if (!segmentStoodOut(BARGE_OVER_AMBIENT)) return;
              // Sound alone only ever stops the VOICE. While it's still
              // thinking there's nothing to stop, and cancelling the question
              // on a clatter lost it (Kris 2026-09-23: "heard a little sound in
              // the background n cancelled"); words decide that, at commit.
              if (!liveSources.length) return;
              // The voice stops; its words stay on screen until what you're
              // saying is finished and the next answer starts (Kris
              // 2026-09-23: you're often answering exactly that text).
              bargeIn();
            }, BARGE_CONFIRM_MS);
          }
          return;
        }
        if (type.endsWith("speech_stopped")) {
          if (itemId === localCaptionItem) localCaptionItem = null;
          userSpeaking = false;
          turns.speechStopped({ itemId, now });
          // "Hearing you…" was set on speech_started; put the label back.
          if (phase === "listening") setPhase("listening");
          if (bargeTimer) {
            // Too short to be someone talking — a cough, a knock, the dog.
            clearTimeout(bargeTimer);
            bargeTimer = 0;
          }
          return;
        }
        if (type === "input_audio_buffer.committed") {
          turns.committed({ itemId, now });
          return;
        }
        if (type.endsWith("input_audio_transcription.delta") && typeof msg.delta === "string") {
          turns.delta({ itemId, delta: msg.delta, now });
          // Real words while we were counting down a barge — that settles
          // it for the voice; a question still being thought about is only
          // replaced when these words become the next turn.
          if (bargeTimer) {
            clearTimeout(bargeTimer);
            bargeTimer = 0;
            if (liveSources.length) bargeIn();
          }
          return;
        }
        if (type.endsWith("input_audio_transcription.completed")) {
          turns.completed({ itemId, transcript: msg.transcript, now });
          return;
        }
        // The transcriber gave up on something you said (Kris 2026-09-24,
        // View as Em: loud speech, two segments, no words back — and nothing
        // recorded why). Its reason goes on the trace; the assembler's
        // salvage still says "didn't catch that".
        if (type.endsWith("input_audio_transcription.failed") || type === "error") {
          if (localSpeech && itemId) turns.completed({ itemId, transcript: "", now });
          const err = msg.error || {};
          console.warn("[voice-live] realtime", type, err);
          logHeard("failed", { text: String(err.message || err.code || type) },
                   String(err.code || err.type || type).slice(0, 40));
        }
      };
  }

  // "Listening stopped" after a while away (Kris 2026-09-23: "would be good
  // if I could click the mic button to continue"): the mic or the orb brings
  // the session back in place — the screen, the chat and what's waiting all
  // stay as they were. The mic KEEPS RUNNING and only the
  // connection is replaced: tearing the audio down and rebuilding it left iOS
  // "listening" with a mic that sent nothing (live 2026-09-23, the first
  // reconnect). A mic that has actually died gets the full start.
  // Back from the background (or a dead mic): the one path that has never
  // failed is a fresh page (Kris 2026-09-24: "it's just best if we force
  // user to refresh … I'd prefer a reliable user experience"). The reload
  // lands on #voice — veiled dark, the voice screen paused for the tap —
  // and the chat, the cards and what's waiting are all on the server.
  function reloadIntoVoice() {
    try { history.replaceState(history.state, "", location.pathname + location.search + "#voice"); } catch (_e) { /* fine */ }
    window.location.reload();
  }

  async function reconnect() {
    if (!open) return;
    if (wasHidden || zeroSince) { reloadIntoVoice(); return; }
    turns.reset();
    clearHeld();
    gateOpenedAt = 0;
    userSpeaking = false;
    bargeIn();
    // iOS parks the audio engine ("interrupted" / "suspended") and mutes the
    // track while the app is in the background; the track still reads
    // "live". The tap that brought us here may resume it — if not, the
    // whole chain is rebuilt (live 2026-09-23: back from the background, it
    // said Listening and heard nothing, "even after tapping to continue").
    // After the background (or a dead track) nothing about the old mic is
    // trusted: a track can read live and unmuted and deliver silence (live
    // 2026-09-23, unlocked the phone, tapped, "nothing gets transcribed").
    // The rebuild starts NOW — no await between the tap and start(), or iOS
    // won't run the engine start() makes.
    const micAlive = !wasHidden && !zeroSince && micStream && micCtx && micCtx.state === "running"
      && micStream.getAudioTracks().some((t) => t.readyState === "live" && !t.muted) && micReady;
    wasHidden = false;
    zeroSince = 0;
    if (!micAlive) {
      teardownAudio();
      muted = false;
      paintMute();
      start();
      return;
    }
    if (ws) { try { ws.close(1000); } catch (_e) { /* already closed */ } ws = null; }
    if (captionsWs) { try { captionsWs.close(1000); } catch (_e) { /* already closed */ } captionsWs = null; }
    captionsLive = false;
    captionBuffer = "";
    micGateAnnounced = false;
    setPhase("connecting");
    const generation = ++connectionGeneration;
    const engine = micCtx;
    const current = () => open && generation === connectionGeneration && micCtx === engine;
    try {
      const tokenRes = await stallGuard(deps.apiFetch("token", { silence_ms: 500, noise: noiseKind() }, audioAbort.signal), TURN_STALL_MS);
      const token = await stallGuard(tokenRes.json().catch(() => ({})), TURN_STALL_MS);
      if (!current()) return;
      if (!tokenRes.ok || !token.value) {
        setPhase("error", token?.error || "Couldn't start listening — tap the mic to try again.");
        return;
      }
      openRealtime(token);
    } catch (_e) {
      if (!current()) return;
      setPhase("error", "Couldn't reach the listening service — tap the mic to try again.");
    }
  }

  function stop() {
    if (!open) return;
    delivery.interrupt();
    if (taskBeat) { clearInterval(taskBeat); taskBeat = 0; }
    if (turnInFlight) {
      deps.apiFetch("discard", { turn_id: turnInFlight.id }).catch(() => {});
      turnInFlight = null;
    }
    open = false;
    muted = false;
    const pullLabel = root && root.querySelector("#pull-refresh-label");
    if (pullLabel) document.body.appendChild(pullLabel);
    if (turnBeat) { clearInterval(turnBeat); turnBeat = 0; }
    turns.reset();
    clearHeld();
    gateOpenedAt = 0;
    bargeIn();
    teardownAudio();
    if (playCtx) {
      playCtx.close().catch(() => {});
      playCtx = null;
    }
    if (ripples) { ripples.destroy(); ripples = null; }   // rAF, canvas and sprites go with the mic
    if (root) root.classList.remove("is-open");
    document.body.classList.remove("voice-live-open");
    setPhase("idle");
    if (deps.onClose) deps.onClose();
  }

  return {
    open(opts) {
      if (open) return;
      open = true;
      voiceSession = crypto.randomUUID();
      taskBeat = setInterval(() => delivery.tick().catch(() => {}), 300);
      ensureDom();
      root.classList.add("is-open");
      document.body.classList.add("voice-live-open");
      // The app's pull-to-refresh label lives in the body, under this
      // overlay on iOS whatever its z-index: it rides inside the screen
      // while it's open (Kris 2026-09-23: "no text for the refresh").
      const pullLabel = document.getElementById("pull-refresh-label");
      if (pullLabel) root.appendChild(pullLabel);
      els.transcript.textContent = "";
      els.reply.textContent = "";
      els.stageScroll.innerHTML = "";
      root.classList.remove("has-cards");
      muted = false;
      paintMute();
      if (!ripples) ripples = createRipples({ host: root.querySelector(".vl-live"), orb: els.orb, root });
      refreshStage(false);
      turns.reset();
      clearHeld();
      if (!turnBeat) turnBeat = setInterval(() => turns.tick(Date.now()), TURN_BEAT_MS);
      // After a pull-to-refresh the screen comes straight back, paused: a
      // fresh page has no tap behind it and iOS opens no mic and plays no
      // sound without one, so the orb waits for the tap that starts it.
      if (opts && opts.paused) { setPhase("paused"); return; }
      start();
    },
    close: stop,
    isOpen: () => open,
    isMuted: () => muted,
    setMuted: (on) => { if (open) setMuted(on); },
    refreshStage: () => refreshStage(false),
    // "New task": what was heard and said leaves the screen; one line says so.
    clearScreen: (line) => {
      if (!root || !open) return;
      clearHeld();
      els.transcript.textContent = "";
      els.reply.textContent = line || "";
      paintDiscard();
      refreshStage(false);
    },
    // The settings cog's "Start the tour": the screen is open (or opening).
    startTour: () => { if (root && open) openTour(); },
    // The dev harness only (tools/voice-live-harness.html): drive the ripples by hand.
    ripples: () => ripples,
    // The Debrief button: take the owner through today, group by group.
    startDebrief,
    // A toast on the voice screen, e.g. "Done — …" when a card is confirmed
    // by tap while the conversation is open (donor 2497f80).
    notify: (text) => { if (root && open) notify(text); }
  };
}
