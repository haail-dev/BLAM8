// The volume line for GPT-Live: an owner-set loudness below which the phone sends silence instead of sound.
// A per-device setting, OFF unless the owner sets it. Pure: no DOM, no timers; the caller reads the level
// of a copy of the mic track every tick and passes it in, so a replay can drive it with a fake clock.
//
// What it is, and is not. The line only tells "quieter than X" from "louder than X"; it cannot tell a
// voice from a television. It DROPS what is under it: the sent audio has no buffer, so unlike the Standard
// voice (which holds a quiet sentence and shows it) nothing under the line can be kept, and the first few
// milliseconds of every sentence after a silence are lost while the gate opens. That is why it is off by
// default, set only by the person, and fail-open: whenever the phone cannot measure the room properly the
// gate is held open (today's behaviour) and the screen says so. Nothing here reads or stores speech.
export const LINE_KEY = 'blam8.liveMicLine';   // = concierge_panel.js LIVE_LINE_KEY
export const LINE_HOLD_MS = 900;               // the level must stay under the close threshold this long before the gate shuts
export const LINE_CLOSE_RATIO = .6;            // ...the close threshold: this fraction of the line (about 4.4 dB of hysteresis)
export const LINE_ZERO_MS = 500;               // a reading of exactly zero for this long is a dead analyser, not a quiet room
export const LINE_WARMUP_MS = 1000;            // after the session starts the gate stays open this long
export const LINE_GAP_MS = 75;                 // a tick this late means timers are throttled: the gate cannot be trusted
export const LINE_PRESS_OPEN_MS = 3000;        // a press or tap on Mist holds the gate open this long
export const LINE_RETURN_OPEN_MS = 1000;       // coming back to a hidden page holds it open this long
export const LINE_NEAR_RATIO = .5;             // a level between this fraction of the line and the line, with the gate shut, is a near miss...
export const LINE_NEAR_MS = 400;               // ...once it has lasted this long
export const LINE_FAIL_OPEN_REASONS = ['no_clone', 'track', 'context', 'analyser', 'hidden', 'gap', 'press', 'visible', 'warmup', 'zero'];
// The reasons that mean the line cannot work on this phone (as opposed to a moment of being held open on purpose),
// and how long one must last before the screen says so (a context that is still waking up is not yet a failure).
export const LINE_BROKEN_REASONS = ['no_clone', 'track', 'context', 'analyser', 'zero'];
export const LINE_BROKEN_MS = 1500;
export const LINE_BELOW_MS = 300;              // the gate must have been shut this long before the orb dims

// A line between 0 and 1 (exclusive), the same square-root slider mapping the settings sheet draws; anything
// else, or an unreadable store, is no line.
export function savedLiveLine(storage) {
  try {
    const value = parseFloat((storage ?? globalThis.localStorage)?.getItem(LINE_KEY) ?? '');
    return value > 0 && value < 1 ? value : null;
  } catch (_) { return null; }
}

const cap = (n, max) => Math.min(max, Math.max(0, Math.round(n)));

export function createLineGate({line, holdMs = LINE_HOLD_MS, closeRatio = LINE_CLOSE_RATIO, zeroMs = LINE_ZERO_MS, warmupMs = LINE_WARMUP_MS,
  gapMs = LINE_GAP_MS, nearMs = LINE_NEAR_MS} = {}) {
  const valid = typeof line === 'number' && line > 0 && line < 1;
  let open = true, lastAt = null, startedAt = null, belowSince = null, zeroSince = null, nearSince = null, nearArmed = true, closedSince = null;
  let forcedUntil = 0, forcedWhy = 'press', wasHidden = false, reason = valid ? 'open' : 'off';
  let closedMs = 0, opens = 0, nearMisses = 0;
  const failOpen = Object.fromEntries(LINE_FAIL_OPEN_REASONS.map(name => [name, 0]));
  const noteReason = next => {
    if (next !== reason && next in failOpen) failOpen[next] = cap(failOpen[next] + 1, 65535);
    reason = next;
  };
  // Hold the gate open for `ms` from `at` (a press on Mist, a return to the page).
  function forceOpen(ms, at, why = 'press') {
    if (at + ms > forcedUntil) {forcedUntil = at + ms; forcedWhy = why;}
  }
  // One reading. `peak` is the largest absolute sample of the last analyser window (0 to 1), or null when it
  // could not be read. Returns {open, reason}; `open` false means send silence.
  function step({peak, contextState, hidden, tapAvailable, tapLive, started}, at) {
    if (!valid) return {open: true, reason: 'off'};
    const gap = lastAt === null ? 0 : at - lastAt;
    if (!open) closedMs = cap(closedMs + Math.min(gap, 100), 86400000);
    lastAt = at;
    if (started && startedAt === null) startedAt = at;
    // Dead silence is "unknown", never "quiet".
    if (peak === 0) {if (zeroSince === null) zeroSince = at;} else zeroSince = null;
    let why = null;
    if (!tapAvailable) why = 'no_clone';
    else if (!tapLive) why = 'track';
    else if (contextState !== 'running') why = contextState === null ? 'analyser' : 'context';
    else if (typeof peak !== 'number' || !Number.isFinite(peak)) why = 'analyser';
    else if (hidden) {wasHidden = true; why = 'hidden';}
    else if (wasHidden) {wasHidden = false; forceOpen(LINE_RETURN_OPEN_MS, at, 'visible'); why = 'visible';}
    else if (gap > gapMs) why = 'gap';
    else if (at < forcedUntil) why = forcedWhy;
    else if (startedAt === null || at - startedAt < warmupMs) why = 'warmup';
    else if (zeroSince !== null && at - zeroSince >= zeroMs) why = 'zero';
    if (why) {
      noteReason(why);
      belowSince = nearSince = null;
      if (!open) {open = true; closedSince = null;}
      return {open: true, reason: why};
    }
    if (peak >= line) {
      belowSince = nearSince = null; nearArmed = true;
      if (!open) {open = true; closedSince = null; opens = cap(opens + 1, 65535);}
    } else if (peak < line * closeRatio) {
      if (belowSince === null) belowSince = at;
      if (open && at - belowSince >= holdMs) {open = false; closedSince = at;}
      if (peak < line * LINE_NEAR_RATIO) nearArmed = true;
    } else {
      belowSince = null;   // between the close threshold and the line: not yet quiet enough to shut, not loud enough to open
    }
    // Something near the line was not sent: sustained, once until the level leaves the band or the gate opens.
    if (!open && peak >= line * LINE_NEAR_RATIO && peak < line) {
      if (nearSince === null) nearSince = at;
      if (nearArmed && at - nearSince >= nearMs) {nearMisses = cap(nearMisses + 1, 65535); nearArmed = false;}
    } else nearSince = null;
    noteReason(open ? 'open' : 'closed');
    return {open, reason};
  }
  return {
    step, forceOpen,
    isOpen: () => open,
    reason: () => reason,
    closedFor: at => (!open && closedSince !== null ? Math.max(0, at - closedSince) : 0),
    nearMisses: () => nearMisses,
    // Counts and milliseconds only. Reasons never seen are left out.
    report: ({agcRequestedOff = false} = {}) => ({
      line: valid ? line : null, closed_ms: closedMs, opens, near_misses: nearMisses,
      fail_open: Object.fromEntries(Object.entries(failOpen).filter(([, count]) => count > 0)), agc_requested_off: Boolean(agcRequestedOff),
    }),
  };
}
