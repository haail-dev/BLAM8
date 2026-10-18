/* The turn assembler: what the voice screen heard, put together into what
 * you SAID — one owner for "you've finished", keyed by the speech service's
 * own item ids (reports/Voice agent turn taking practices.md, 2026-09-24).
 *
 * It replaces a merger that appended text by arrival order. That merger
 * doubled a sentence when a late transcript followed the fallback's copy of
 * the same words ("Do we have a draft waiting for Do we have a draft waiting
 * for Laura Finch?", Kris 2026-09-24), mixed one segment's words into the
 * next, and lost count of what was still coming. Here every segment is a
 * record by item id: its final transcript REPLACES its streamed words, a
 * transcript for a segment already sent is ignored, and a struck segment's
 * late words can never land in the next sentence.
 *
 * Pure: no DOM, no audio, no timers of its own. voice-live.js feeds it the
 * server's events and calls tick() on a steady beat; tests replay recorded
 * event sequences through it with a fake clock (tests/replays/voice_replay.mjs).
 */

// Quiet after the last thing heard before the words go: people pause that
// long mid-thought ("Good morning. <pause> what's on today").
export const SETTLE_MS = 600;
// Closed but never transcribed: the streamed words ARE the transcript (the
// final one has been seen 25 s late, and sometimes never).
export const SALVAGE_MS = 4000;
// Started and stopped but never closed by the server (the buffer was cleared
// under it): nothing more is coming.
export const ORPHAN_MS = 8000;
// "Still speaking" with words in hand and nothing new for this long is the
// room holding the segment open (a fan, a telly): what's done goes.
export const NOISE_HOLD_MAX_MS = 2500;
// How far back a segment's loudness reaches: the speech service says
// "started" a few hundred ms after the sound, and a one-word "Yes" is over
// by then.
export const RECENT_PEAK_MS = 1500;

export function createTurnAssembler({ onUtterance, onDisplay, onEvent, transcriptionWaitMs = () => SALVAGE_MS } = {}) {
  const segs = new Map();     // item id -> segment
  let order = [];             // ids of the utterance being built, in the order heard
  const spent = new Set();    // ids already sent or struck: their late words are ignored
  const spentOrder = [];
  let activeId = null;        // the segment being spoken right now
  let lastActivityAt = 0;
  let lastWordsAt = 0;
  let recent = [];            // [when, peak]
  let anon = 0;

  const emitEvent = (name, detail) => { if (onEvent) onEvent(name, detail || {}); };

  function spend(id) {
    if (spent.has(id)) return;
    spent.add(id);
    spentOrder.push(id);
    while (spentOrder.length > 300) spent.delete(spentOrder.shift());
  }

  function recentPeak(now) {
    const since = now - RECENT_PEAK_MS;
    let top = 0;
    for (const [t, p] of recent) if (t >= since && p > top) top = p;
    return top;
  }

  // The event's segment. An event without an id belongs to the one being
  // spoken, else the newest; a spent id has no segment (late words).
  function seg(itemId, now) {
    let id = itemId || activeId || order[order.length - 1] || `anon-${++anon}`;
    if (spent.has(id)) return null;
    let s = segs.get(id);
    if (!s) {
      s = { id, startedAt: 0, stoppedAt: 0, committedAt: 0, deltas: "", final: null,
            done: false, salvaged: false, peak: recentPeak(now), clipped: false, echo: false };
      segs.set(id, s);
      order.push(id);
    }
    return s;
  }

  const textOf = (s) => (s.final !== null ? s.final : s.deltas).trim();

  function display() {
    if (!onDisplay) return;
    const words = order.map((id) => textOf(segs.get(id))).filter(Boolean).join(" ");
    onDisplay(words || (activeId ? "…" : ""), { speaking: Boolean(activeId), pending: order.length > 0 });
  }

  function emit(ids, why) {
    const parts = ids.map((id) => segs.get(id));
    const text = parts.map(textOf).filter(Boolean).join(" ").trim();
    const utterance = {
      text,
      why,
      segments: parts.length,
      peak: Math.max(0, ...parts.map((s) => s.peak || 0)),
      clipped: Boolean(parts[0] && parts[0].clipped),
      salvaged: parts.some((s) => s.salvaged),
      echo: parts.every((s) => s.echo),
      durationMs: parts.reduce((n, s) => n + (s.startedAt && s.stoppedAt ? s.stoppedAt - s.startedAt : 0), 0),
    };
    for (const id of ids) { segs.delete(id); spend(id); }
    order = order.filter((id) => !ids.includes(id));
    if (onUtterance) onUtterance(utterance);
    display();
  }

  return {
    // The mic's loudness, every frame: the loudest moment of each segment is
    // its evidence of being you rather than the room.
    notePeak(peak, now) {
      recent.push([now, peak]);
      while (recent.length && now - recent[0][0] > RECENT_PEAK_MS) recent.shift();
      const s = activeId && segs.get(activeId);
      if (s && peak > s.peak) s.peak = peak;
    },

    // echo: it started while our own voice was playing (the mic was shut) —
    // if it comes to nothing, that's the echo, not you, and nobody is told.
    speechStarted({ itemId, now, clipped = false, echo = false }) {
      const s = seg(itemId, now);
      if (!s) return;
      s.startedAt = now;
      s.peak = Math.max(s.peak, recentPeak(now));
      s.clipped = clipped;
      s.echo = echo;
      activeId = s.id;
      lastActivityAt = now;
      display();
    },

    speechStopped({ itemId, now }) {
      const s = seg(itemId, now);
      if (s) s.stoppedAt = now;
      if (!itemId || activeId === itemId) activeId = null;
      lastActivityAt = now;
    },

    committed({ itemId, now }) {
      const s = seg(itemId, now);
      if (s) s.committedAt = now;
      lastActivityAt = now;
    },

    delta({ itemId, delta, now }) {
      if (itemId && spent.has(itemId)) return;
      const s = seg(itemId, now);
      if (!s || s.done) return;
      s.deltas += delta;
      lastWordsAt = now;
      lastActivityAt = now;
      display();
    },

    // The final transcript REPLACES the streamed words — never added to them.
    completed({ itemId, transcript, now }) {
      if (itemId && spent.has(itemId)) {
        emitEvent("late_ignored", { itemId, text: String(transcript || "").trim() });
        return;
      }
      const s = seg(itemId, now);
      if (!s) return;
      const text = String(transcript || "").trim();
      if (s.salvaged) {
        // The fallback already took the streamed words; the real transcript
        // is better, so it replaces them — still one copy.
        s.final = text || s.final;
      } else {
        s.final = text || s.deltas.trim();
      }
      s.done = true;
      if (text) lastWordsAt = now;
      lastActivityAt = now;
      display();
    },

    // The beat: decides, from the segments alone, whether you've finished.
    tick(now) {
      for (const id of order) {
        const s = segs.get(id);
        if (s.done) continue;
        if (s.committedAt && now - s.committedAt >= transcriptionWaitMs()) {
          s.done = true;
          s.salvaged = true;
          s.final = s.deltas.trim();
          emitEvent("salvaged", { itemId: id, text: s.final });
        } else if (!s.committedAt && s.stoppedAt && id !== activeId && now - s.stoppedAt >= ORPHAN_MS) {
          s.done = true;
          s.final = s.deltas.trim();
          emitEvent("orphaned", { itemId: id, text: s.final });
        }
      }
      if (!order.length) return;
      const all = order.map((id) => segs.get(id));
      if (activeId) {
        // Still "speaking" — unless the words stopped long ago and it's the
        // room keeping the segment open: then what's finished goes now.
        const doneIds = [];
        for (const s of all) { if (!s.done || s.id === activeId) break; doneIds.push(s.id); }
        const doneText = doneIds.map((id) => textOf(segs.get(id))).join("");
        if (doneIds.length && doneText && now - lastWordsAt > NOISE_HOLD_MAX_MS) emit(doneIds, "noise_hold");
        return;
      }
      if (all.every((s) => s.done) && now - lastActivityAt >= SETTLE_MS) {
        emit(order.slice(), "settled");
      }
    },

    // Struck by a tap: everything heard so far is dropped, and its late
    // words are ignored by id — never swallowing the next real sentence.
    discard() {
      for (const id of order) spend(id);
      if (activeId) spend(activeId);
      segs.clear();
      order = [];
      activeId = null;
      display();
    },

    // A fresh connection: ids from the old one never come again.
    reset() {
      segs.clear();
      order = [];
      activeId = null;
      recent = [];
      lastActivityAt = 0;
      lastWordsAt = 0;
    },

    isSpeaking: () => Boolean(activeId),
    hasPending: () => order.length > 0,
    activePeak: () => { const s = activeId && segs.get(activeId); return s ? s.peak : 0; },
    recentPeak,
  };
}
