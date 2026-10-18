// Pond: ripples on the GPT-Live screen. The whole screen is a calm lake. A tap or click drops a stone where it lands,
// a mouse (or a finger) moving across it leaves small ripples sized by its speed, and while the person is speaking
// the orb sends rings out of its centre, bigger and quicker the louder the voice. Rings spread, bounce back off the
// screen edges and run into one another; the crests glow in BLAM8's colours, and warm ones while the microphone is muted.
//
// The water itself is water-core.js (shared with every other place that has ripples). This file is what is particular to
// this screen: where the canvas sits (under everything else on the stage), the voice (the same speech gate as the Mist, a
// stone from the orb's centre while it lasts), what is dimmed (the orb's middle, text and controls, so nothing there is
// harder to read) and the one place a press drops no stone (End conversation). A failure here must never touch the
// screen: the caller wraps it in try/catch.

const revision = new URL(import.meta.url).search;
const { createWater } = await import('./water-core.js' + revision);

const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);
const smooth = (a, b, v) => { const t = clamp((v - a) / (b - a), 0, 1); return t * t * (3 - 2 * t); };

const CFG = {
  // the voice
  micOnset: 0.07, micOnsetSecs: 0.12, micRelease: 0.035, micReleaseSecs: 0.6,   // the same gate as the Mist's speech detection
  micFloor: 0.04, micCurve: 0.8,
  ringAmp: [0.28, 0.65], ringSigmaPx: 9, ringCount: 3,   // a stone from the orb's centre: size at a quiet and at a full voice
  ringEverySecs: [0.4, 0.1],                            // gap between stones: quiet voice, loud voice
  // the screen
  orbKeep: 0.3, textKeep: 0.3, boxKeep: 0.5,
};

// What the water is dimmed behind: [selector, 'text' (the words only) or 'box' (the whole element), how much light is kept].
const KEEP = [
  ['.vl-status', 'text', CFG.textKeep], ['.vl-mic-copy p', 'text', CFG.textKeep], ['.vl-task-note', 'text', CFG.textKeep],
  ['.vl-end', 'box', CFG.textKeep], ['.vl-controls button', 'box', CFG.textKeep], ['.vl-stage-scroll', 'box', CFG.boxKeep],
  ['.vl-top-strip > *', 'box', CFG.boxKeep], ['.vl-allowance', 'box', CFG.boxKeep],
];

export function createPond(host, orb, options = {}) {
  // the voice: a gate (the same onset and release as the Mist), a smoothed loudness, and the clock that drops its stones
  const mic = {on: false, onset: 0, quiet: 0, target: 0, env: 0, timer: 0, fedAt: 0, fedPerf: 0, x: 0, y: 0};
  let water = null, dead = false;

  /** Where the orb is, and the orb and text the water must stay out of the way of. */
  function keepClear(g) {
    const o = orb.getBoundingClientRect(), ox = o.left - g.left + o.width / 2, oy = o.top - g.top + o.height / 2, orad = Math.max(1, o.width / 2);
    mic.x = ox; mic.y = oy;
    // the orb: the rings leave it from behind, nearly clear in the middle, so the B and the Mist read clearly
    const j0 = Math.max(0, Math.floor((oy - orad * 1.2) / g.cell)), j1 = Math.min(g.gr - 1, Math.ceil((oy + orad * 1.2) / g.cell));
    const i0 = Math.max(0, Math.floor((ox - orad * 1.2) / g.cell)), i1 = Math.min(g.gc - 1, Math.ceil((ox + orad * 1.2) / g.cell));
    for (let j = j0; j <= j1; j++) for (let i = i0; i <= i1; i++) {
      const d = Math.hypot((i + 0.5) * g.cell - ox, (j + 0.5) * g.cell - oy);
      g.keep[j * g.gc + i] *= CFG.orbKeep + (1 - CFG.orbKeep) * smooth(orad * 0.5, orad * 1.2, d);
    }
    for (const [selector, kind, strength] of KEEP) {
      for (const el of host.querySelectorAll(selector)) {
        if (el.hidden || (kind === 'text' && !el.textContent.trim())) continue;
        let b;
        if (kind === 'text') { const range = document.createRange(); range.selectNodeContents(el); b = range.getBoundingClientRect(); }
        else b = el.getBoundingClientRect();
        if (b.width > 0 && b.height > 0) g.dim(b.left - g.left, b.top - g.top, b.right - g.left, b.bottom - g.top, strength);
      }
    }
  }

  water = createWater(host, {
    tier: options.tier,
    // Listen on the whole screen, not just the stage: the context bar, the pending-review badge and the Debrief button are
    // added to the screen's root beside the stage, and a press on them is a press on the screen like any other.
    surface: host.parentElement || host,
    canvasClass: 'vl-pond',
    lakeFromCanvas: true,   // on a phone the lake is laid out as wide as the page you can swipe across to (concierge_sidebar.js), not just the stage
    mount(canvas) { const ambient = host.querySelector('.vl-ambient'); host.insertBefore(canvas, ambient ? ambient.nextSibling : host.firstChild); },
    ignore: target => Boolean(target.closest?.('.vl-end')),   // ending the conversation is not a place to drop a stone
    keep: keepClear,
    busy(now) {
      if (now - mic.fedPerf > 300) mic.target = 0;   // nothing is supplying the voice: it has stopped
      return mic.env > 0.004 || mic.target > 0;
    },
    onFrame(dt) {
      mic.env += (mic.target - mic.env) * (1 - Math.exp(-dt * (mic.target > mic.env ? 24 : 9)));
      if (mic.env < 0.004 && mic.target === 0) mic.env = 0;
      // while the voice is on, the orb's centre drops a stone, bigger and more often the louder it is
      if (mic.env > 0.05) {
        mic.timer -= dt;
        if (mic.timer <= 0 && water.queued() < 72) {
          const e = clamp(mic.env, 0, 1);
          water.addStone(mic.x, mic.y, CFG.ringAmp[0] + (CFG.ringAmp[1] - CFG.ringAmp[0]) * e, CFG.ringSigmaPx, CFG.ringCount);
          mic.timer = CFG.ringEverySecs[0] + (CFG.ringEverySecs[1] - CFG.ringEverySecs[0]) * e;
        }
      } else mic.timer = 0;
    },
    quiet: () => mic.env === 0,
    pending: () => mic.target > 0,
    onClear: () => { mic.timer = 0; },
  });

  /** The signals the Mist already reads, once per Mist frame: the microphone's level and whether it is muted. */
  function hear(signals) {
    if (dead || !signals) return;
    water.setWarm(signals.muted ? 1 : 0);   // muted: the ripples turn warm, like the B
    const nowPerf = performance.now(), dt = clamp(mic.fedAt ? (nowPerf - mic.fedAt) / 1000 : 1 / 30, 0.001, 0.1);
    mic.fedAt = nowPerf;
    const valid = signals.ready === true && signals.inputActive === true && !signals.muted && signals.analysisAvailable !== false;
    let level = 0;
    if (valid) {
      let peak = 0;
      if (signals.wave) for (const sample of signals.wave) { const a = Math.abs(sample); if (a > peak) peak = a; }
      level = Math.max(clamp(signals.user || 0, 0, 1), peak * 0.35);
    }
    if (!valid) { mic.on = false; mic.onset = mic.quiet = 0; }
    else if (level > CFG.micOnset) { mic.onset = Math.min(0.3, mic.onset + dt); mic.quiet = 0; if (mic.onset >= CFG.micOnsetSecs) mic.on = true; }
    else { mic.onset = Math.max(0, mic.onset - dt * 3); if (level < CFG.micRelease) mic.quiet += dt; else mic.quiet = 0; if (mic.quiet >= CFG.micReleaseSecs) mic.on = false; }
    mic.target = mic.on ? Math.pow(clamp((level - CFG.micFloor) / (1 - CFG.micFloor), 0, 1), CFG.micCurve) : 0;
    mic.fedPerf = nowPerf;
    if (mic.target > 0) water.wake();
  }

  return {
    hear,
    drop: water.drop,
    snapshot: () => ({...water.snapshot(), envelope: mic.env, voiceOn: mic.on, target: mic.target, centre: [mic.x, mic.y]}),
    destroy() { dead = true; water.destroy(); },
  };
}
