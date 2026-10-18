// Hold to talk for GPT-Live (a per-device setting). Two pieces, both free of timers and of anything the
// page owns, so a replay can drive them with a fake clock and fake events:
//
//   createTalkController  the little state machine: idle, pressed, tail, latched. While the person is not
//                         holding Mist the sent microphone track is disabled (the transport decides that
//                         from talking()), so the agent hears digital silence: a television cannot start a
//                         turn or take one over. A short tap latches the mic open (the way in for anyone who
//                         cannot hold); another press closes it. A latched mic is open but not held, so it does
//                         not hold the idle clock (holding() is true only for a press and its tail): it is
//                         watched through the level like a hands-free mic, and the idle close ends it.
//   installTalkGestures   turns pointer and keyboard events on Mist into controller calls, and lets go of
//                         the mic at once when the page is hidden, loses focus or is left.
//
// Nothing here reads or stores speech. The report at the end of a conversation is counts and milliseconds.
export const TALK_KEY = 'blam8.liveTalk';      // = concierge_panel.js TALK_KEY
export const TALK_TAIL_MS = 400;               // the mic stays open this long after a release, so a finger lifted a hair early does not clip the last word
export const TALK_TAP_MS = 250;                // a press that ends within this is a tap: it latches the mic open instead
export const TALK_WATCHDOG_MS = 120000;        // a press this long is taken as stuck (a lost pointer that never reported its release) and ends
export const TALK_KEY_ID = 'key';              // the pointer id the keyboard uses

// 'hold' only when the person chose it; anything else, an unreadable store included, is the usual hands-free.
export function savedTalkMode(storage) {
  try { return (storage ?? globalThis.localStorage)?.getItem(TALK_KEY) === 'hold' ? 'hold' : 'hands_free'; }
  catch (_) { return 'hands_free'; }
}

const cap = (n, max) => Math.min(max, Math.max(0, Math.round(n)));

export function createTalkController({now = () => Date.now(), onChange, tailMs = TALK_TAIL_MS, tapMs = TALK_TAP_MS, watchdogMs = TALK_WATCHDOG_MS} = {}) {
  let state = 'idle', pointer = null, downAt = 0, tailUntil = 0, wasLatched = false, allowLatch = true, since = 0;
  let presses = 0, latches = 0, watchdogStops = 0, talkMs = 0;
  function set(next, reason) {
    if (next === state) return;
    const at = now();
    if (state === 'idle') since = at;
    if (next === 'idle') talkMs = cap(talkMs + (at - since), 86400000);
    state = next;
    onChange?.(state, reason);
  }
  // One finger (or the keyboard) at a time: a second pointer while one is down is a palm or a stray touch.
  function press(id, {latch = true} = {}) {
    if (pointer !== null) return false;
    pointer = id; downAt = now(); wasLatched = state === 'latched'; allowLatch = latch !== false;
    presses = cap(presses + 1, 65535);
    set(wasLatched ? 'latched' : 'pressed', 'press');
    return true;
  }
  // 'latch' (a tap opened the mic and left it open), 'unlatch' (a press while latched closes it on release,
  // after the tail, however long it was held), 'release' (a hold ended), or null (not the pointer that is
  // down, or nothing to end).
  function release(id) {
    if (pointer === null || id !== pointer) return null;
    const held = now() - downAt;
    pointer = null;
    if (wasLatched && state === 'latched') {tailUntil = now() + tailMs; set('tail', 'unlatch'); return 'unlatch';}
    if (state !== 'pressed') return null;
    if (held <= tapMs && allowLatch) {latches = cap(latches + 1, 65535); set('latched', 'latch'); return 'latch';}
    tailUntil = now() + tailMs; set('tail', 'release'); return 'release';
  }
  // Let go of the mic at once, with no tail: hidden page, lost focus, Escape, a muted conversation, a stuck press.
  function cancel(reason = 'cancel') {
    const was = state !== 'idle';
    pointer = null;
    if (reason === 'watchdog' && was) watchdogStops = cap(watchdogStops + 1, 65535);
    set('idle', reason);
    return was;
  }
  // The caller's own interval: ends the tail, and a press that has been down too long. Nothing resets the
  // watchdog: words arriving (a television's, or the agent's own answer to them) say nothing about whether the
  // finger is still down, and a continuous press that long is not a real hold.
  function tick(at = now()) {
    if (state === 'tail' && at >= tailUntil) set('idle', 'tail');
    else if (state === 'pressed' && pointer !== null && at - downAt >= watchdogMs) return cancel('watchdog');
    return false;
  }
  return {
    press, release, cancel, tick,
    talking: () => state !== 'idle',                    // the mic is open
    holding: () => state === 'pressed' || state === 'tail',   // Mist is held, or was let go a moment ago: mid-turn. A latched mic is open but not held.
    held: () => pointer !== null,
    state: () => state,
    report() {
      const open = state !== 'idle' ? Math.max(0, now() - since) : 0;
      return {mode: 'hold', presses, talk_ms: cap(talkMs + open, 86400000), latches, watchdog_stops: watchdogStops};
    },
  };
}

// Pointer and keyboard events on `el` (Mist) into `talk` calls. `talk` offers press(id, {latch}),
// release(id) and cancel(reason); enabled() says whether the gestures apply right now (hold mode, a live
// conversation); ready() whether a press can be taken yet; notReady() is told when it cannot; warning()
// says whether the idle warning is showing. A press or a tap during the warning only keeps the conversation
// going: it is not allowed to latch the mic open. The warning is read before the idle clock sees the touch
// (a capture listener on the document, which runs before one on the overlay), because that touch clears it.
export function installTalkGestures(el, {talk, enabled, ready, notReady, warning, doc = globalThis.document, win = globalThis.window}) {
  let warned = false;
  const snapshot = () => {warned = Boolean(warning?.());};
  const pressed = id => {
    if (!ready()) {notReady?.(); return false;}
    return talk.press(id, {latch: !warned});
  };
  const listeners = [];
  const on = (target, type, fn, capture) => {
    if (!target?.addEventListener) return;
    target.addEventListener(type, fn, capture);
    listeners.push(() => target.removeEventListener?.(type, fn, capture));
  };
  on(doc, 'pointerdown', snapshot, true);
  on(doc, 'keydown', snapshot, true);
  on(el, 'pointerdown', event => {
    if (!event.isTrusted || event.button !== 0 || !enabled()) return;
    if (pressed(event.pointerId)) {try {el.setPointerCapture?.(event.pointerId);} catch (_) {}}
  });
  // A release is always passed on, even if the screen has stopped being live meanwhile: it can only end a press.
  for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) on(el, type, event => talk.release(event.pointerId));
  on(el, 'keydown', event => {
    if (!enabled()) return;
    if (event.key === 'Escape') {talk.cancel('escape'); return;}
    if (event.key !== ' ' && event.key !== 'Enter') return;
    event.preventDefault();
    if (event.repeat || !event.isTrusted) return;
    pressed(TALK_KEY_ID);
  });
  on(el, 'keyup', event => {
    if (!enabled() || (event.key !== ' ' && event.key !== 'Enter')) return;
    event.preventDefault();
    talk.release(TALK_KEY_ID);
  });
  on(el, 'blur', () => talk.release(TALK_KEY_ID));
  on(win, 'blur', () => talk.cancel('blur'));
  on(win, 'pagehide', () => talk.cancel('hidden'));
  on(doc, 'visibilitychange', () => {if (doc.hidden) talk.cancel('hidden');});
  return () => {for (const off of listeners.splice(0)) off();};
}
