/* The voice screen's ripples (docs/spec-voice-ripples.md, Kris 2026-09-30).
 *
 * The mic button is a stone dropped into a pond. While the screen is loading,
 * rings of particles leave the orb and travel outward, fading as they go; when
 * it is ready the surface goes still and one larger green ring rolls out. It
 * replaces the spoken "Morning." as the thing that tells you the screen is
 * alive, so it must feel like water, not a radar: slow, soft, a little uneven.
 *
 * One <canvas> behind the orb, one rAF loop that runs only while a ring is
 * alive (idle = stopped, zero cost). Particles are a pre-rendered soft sprite
 * blitted with "lighter" blending, never per-frame arc fills. Nothing here
 * gates the mic: voice-live.js decides when to listen, this only draws.
 *
 * The emitter (emit / setDrive) is built so a later driver can fire rings from
 * the mic level (you) or the playback level (her) — the waveform of section 4
 * of the spec. Not built yet; only the timer-driven states below exist.
 */

const SPRITE_PX = 64;             // the soft dot, drawn once
const RING_SLOTS = 6;             // rings alive at once (the loading cadence never needs more)
const POOL_FULL = 600, POOL_LOW = 300;
const SLOTS_LOW = 2;
const RING_MS = 2600;             // T: a ring's life
const RING_SPREAD = 0.6;          // R: of the screen's shorter side
const CADENCE_MS = 1150;          // a new loading ring this often
const PEAKS = [1, 0.8, 0.65, 0.55];   // each loading ring a little fainter, then hold
const FIRST_RING_MS = 90;         // the impact hold before ring one leaves
const READY_STILL_MS = 250;       // the beat of stillness before the ready ring
const DROP_CLASS_MS = 420;        // how long the drop class stays for its keyframes
const FULL_ORB_REM = 10.5;        // .vl-orb at full size; has-cards shrinks it and the radii with it
const BUDGET_FRAME_MS = 20, BUDGET_FRAMES = 30;
const JITTER_DEG = 1.5, JITTER_PX = 4, CREST_SHARE = 0.08, CREST_GAIN = 1.2;
// A wave, not a line (Kris, 30 Sep): particles sit at different depths behind
// the crest. The band starts tight and broadens as the ring spreads; the back
// of the wave is softer, larger and dimmer than the crest.
const BAND_MIN_PX = 6, BAND_GROW_PX = 34;
// The error beat (Kris, 30 Sep): two rings close together, a rest, again,
// until it's dealt with (the phase leaves error).
const ERROR_BEAT_MS = 380, ERROR_REST_MS = 2400, ERROR_ALPHA = 0.32;

// Palette: the orb's own gradient stops, the mic ring's green, the error red.
export const COLOURS = {
  blue: ["#7dd3fc", "#2563eb"],
  green: ["#bbf7d0", "#86efac"],
  rose: ["#fecdd3", "#fda4af"],
};
// What the orb's ring wears at a birth (Kris, 30 Sep): the wave's colour on
// the border, its halo in the glow, held while the crest leaves the ring.
const TINT = {
  blue: ["rgba(125, 211, 252, 0.95)", "rgb(37 99 235)"],
  green: ["rgba(187, 247, 208, 1)", "rgb(74 222 128)"],
  rose: ["rgba(254, 205, 211, 0.95)", "rgb(244 63 94)"],
};
const TINT_HOLD_MS = 700;

export function reducedMotion() {
  try { return window.matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (_e) { return false; }
}

function easeOutCubic(x) { x = Math.min(1, Math.max(0, x)); return 1 - Math.pow(1 - x, 3); }

function hexToRgb(hex) {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function makeSprite(core, halo) {
  const c = document.createElement("canvas");
  c.width = c.height = SPRITE_PX;
  const ctx = c.getContext("2d");
  const [r1, g1, b1] = hexToRgb(core), [r2, g2, b2] = hexToRgb(halo);
  const g = ctx.createRadialGradient(SPRITE_PX / 2, SPRITE_PX / 2, 0, SPRITE_PX / 2, SPRITE_PX / 2, SPRITE_PX / 2);
  g.addColorStop(0, `rgba(${r1},${g1},${b1},1)`);
  g.addColorStop(0.35, `rgba(${r2},${g2},${b2},0.55)`);
  g.addColorStop(1, `rgba(${r2},${g2},${b2},0)`);
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, SPRITE_PX, SPRITE_PX);
  return c;
}

/* prefers-reduced-motion: no particles. The orb's own glow does one gentle
   brightness pulse on the drop and one on ready; the labels carry the state. */
function createPulseFallback(root) {
  let timer = 0;
  const pulse = () => {
    if (!root) return;
    root.classList.remove("vl-pulse");
    void root.offsetWidth;             // restart the keyframes
    root.classList.add("vl-pulse");
    clearTimeout(timer);
    timer = setTimeout(() => root && root.classList.remove("vl-pulse"), 800);
  };
  return {
    reduced: true,
    drop: pulse, settle() {}, ready: pulse, error() {}, still() {},
    emit() {}, setDrive() {}, pause() {}, resume() {}, relayout() {},
    destroy() { clearTimeout(timer); if (root) root.classList.remove("vl-pulse"); root = null; },
  };
}

export function createRipples({ host, orb, root }) {
  if (reducedMotion()) return createPulseFallback(root);

  let canvas = document.createElement("canvas");
  canvas.className = "vl-ripples";
  canvas.setAttribute("aria-hidden", "true");
  host.insertBefore(canvas, host.firstChild);
  const ctx = canvas.getContext("2d");
  let sprites = { blue: makeSprite(...COLOURS.blue), green: makeSprite(...COLOURS.green), rose: makeSprite(...COLOURS.rose) };

  // Geometry, re-read on resize / rotate / has-cards (never hard-coded).
  let dpr = 1, W = 0, H = 0, ox = 0, oy = 0, r0 = 0, R = 0;
  function relayout() {
    if (!canvas) return;
    const hr = host.getBoundingClientRect();
    const or = orb.getBoundingClientRect();
    dpr = Math.min(2, window.devicePixelRatio || 1);
    W = Math.max(1, Math.round(hr.width)); H = Math.max(1, Math.round(hr.height));
    if (canvas.width !== Math.round(W * dpr) || canvas.height !== Math.round(H * dpr)) {
      canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
      canvas.style.width = W + "px"; canvas.style.height = H + "px";
    }
    ox = or.left - hr.left + or.width / 2;
    oy = or.top - hr.top + or.height / 2;
    // Born at the green RING, not the button (Kris, 30 Sep): the ring sits
    // outside the orb and swells with the mic level, and its rect is read at
    // each birth so the water leaves from wherever the ring is right then.
    const ring = orb.querySelector("span");
    const rr = ring ? ring.getBoundingClientRect() : null;
    r0 = (rr && rr.width ? rr.width : or.width) / 2;
    const rem = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
    const scale = Math.min(1, Math.max(0.5, or.width / (FULL_ORB_REM * rem)));   // has-cards: smaller orb, shorter rings
    // Judged by eye on a phone (30 Sep): 60% of the shorter side sent a ring
    // off the sides while it was still bright, so it escaped instead of dying.
    // A ring's full reach ends at the nearest side edge, where its alpha is 0.
    const nearestEdge = Math.min(ox, W - ox, oy, H - oy);
    R = Math.min(RING_SPREAD * Math.min(W, H) * scale, Math.max(60, nearestEdge - r0));
  }
  relayout();
  let ro = null;
  if (typeof ResizeObserver !== "undefined") { ro = new ResizeObserver(() => relayout()); ro.observe(host); }
  const onResize = () => relayout();
  window.addEventListener("resize", onResize);
  window.addEventListener("orientationchange", onResize);

  // The ring pool: fixed slots, per-particle jitter allocated once per slot.
  // Judged by eye (30 Sep): at ~140 particles a wide ring read as a string of
  // beads; closer spacing with a smaller, softer dot reads as a band of light.
  const MAX_N = 200;
  const rings = [];
  for (let s = 0; s < RING_SLOTS; s += 1) {
    const angJ = new Float32Array(MAX_N), radJ = new Float32Array(MAX_N), phase = new Float32Array(MAX_N);
    const depth = new Float32Array(MAX_N);
    const crest = new Uint8Array(MAX_N);
    for (let i = 0; i < MAX_N; i += 1) {
      angJ[i] = (Math.random() * 2 - 1) * JITTER_DEG * Math.PI / 180;
      radJ[i] = (Math.random() * 2 - 1) * JITTER_PX;
      phase[i] = Math.random() * Math.PI * 2;
      depth[i] = Math.pow(Math.random(), 1.5);          // most of the water is near the crest
      crest[i] = depth[i] < 0.2 && Math.random() < CREST_SHARE / 0.2 ? 1 : 0;
    }
    rings.push({ alive: false, born: 0, n: 0, r0: 0, R: 0, T: RING_MS, peak: 1, sprite: "blue", angJ, radJ, phase, depth, crest });
  }
  let budget = POOL_FULL, slots = RING_SLOTS;
  function liveParticles() { let n = 0; for (const r of rings) if (r.alive) n += r.n; return n; }

  // emit({ radius, alpha, hue, speed }): one ring. radius/speed scale R/T (1 =
  // the loading ring); hue is a COLOURS key. The waveform driver will call this.
  function emit(opts = {}) {
    if (!canvas) return false;
    relayout();                       // the ring's edge as it is now (it swells with your voice)
    const spread = (opts.radius || 1) * R;
    // Even spacing (~7 px at the ring's mid-life radius) however far it spreads.
    const n = Math.max(90, Math.min(MAX_N, Math.round(Math.PI * 2 * (r0 + spread * 0.5) / 7)));
    let slot = null, used = 0, oldest = null;
    for (const r of rings) {
      if (r.alive) { used += 1; if (!oldest || r.born < oldest.born) oldest = r; }
      else if (!slot) slot = r;
    }
    // The budget and the slot cap only ever thin the loading cadence. A
    // priority ring (ready, error) is the state itself: it always goes out,
    // taking the oldest ring's slot if it has to.
    if (!opts.priority) {
      if (liveParticles() + n > budget) return false;
      if (!slot || used >= slots) return false;
    } else if (!slot) {
      slot = oldest;
    }
    slot.alive = true; slot.born = performance.now(); slot.n = n;
    slot.r0 = r0; slot.R = spread; slot.T = RING_MS / (opts.speed || 1);
    slot.peak = opts.alpha == null ? 1 : opts.alpha; slot.sprite = opts.hue || "blue";
    tint(slot.sprite);
    startLoop();
    return true;
  }
  // The ring slides into the wave's colour (fast, via the vl-ring-tint class),
  // holds, then the class goes and it eases back to its own green.
  let tintTimer = 0;
  function tint(hue) {
    if (!root) return;
    const c = TINT[hue] || TINT.blue;
    root.style.setProperty("--vl-ring", c[0]);
    root.style.setProperty("--vl-glow", c[1]);
    root.classList.add("vl-ring-tint");
    clearTimeout(tintTimer);
    tintTimer = setTimeout(untint, TINT_HOLD_MS);
  }
  function untint() {
    tintTimer = 0;
    if (!root) return;
    root.classList.remove("vl-ring-tint");
    root.style.removeProperty("--vl-ring");
    root.style.removeProperty("--vl-glow");
  }
  let drive = null;
  function setDrive(fn) { drive = typeof fn === "function" ? fn : null; }

  // ---- the frame loop (only while something is alive) ----
  let raf = 0, hidden = false, lastFrame = 0, budgetAcc = 0, budgetN = 0;
  function frame(now) {
    raf = 0;
    if (!canvas || hidden) return;
    // A stall (tab switch, screenshot, GC pause) is not a slow device: frames
    // over 100 ms are left out of the average, or one hiccup would degrade
    // the effect for the rest of the conversation.
    if (lastFrame && now - lastFrame < 100) {
      budgetAcc += now - lastFrame; budgetN += 1;
      if (budgetN >= BUDGET_FRAMES) {
        if (budgetAcc / budgetN > BUDGET_FRAME_MS && budget !== POOL_LOW) { budget = POOL_LOW; slots = SLOTS_LOW; }
        budgetAcc = 0; budgetN = 0;
      }
    }
    lastFrame = now;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    // Nothing is ever painted inside the green ring: a wave's wash and the
    // sprite's bloom would otherwise show inside the orb and the water would
    // look as if it came from the button. It emerges through the ring.
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, 0, W, H);
    ctx.arc(ox, oy, Math.max(0, r0 - 1), 0, Math.PI * 2, true);
    ctx.clip("evenodd");
    ctx.globalCompositeOperation = "lighter";
    let any = false;
    for (const ring of rings) {
      if (!ring.alive) continue;
      const t = (now - ring.born) / ring.T;
      if (t >= 1) { ring.alive = false; continue; }
      any = true;
      const eased = easeOutCubic(t);
      const r = ring.r0 + ring.R * eased;
      const alpha = ring.peak * Math.pow(1 - t, 1.6);
      const base = (10 + ring.R * 0.016) * (1 - 0.45 * t);          // shrinks 1.0 -> 0.55 over the life
      const band = BAND_MIN_PX + BAND_GROW_PX * eased;               // the wave broadens as it spreads
      const sprite = sprites[ring.sprite] || sprites.blue;
      const step = (Math.PI * 2) / ring.n;
      const drift = now / 900;
      for (let i = 0; i < ring.n; i += 1) {
        const d = ring.depth[i];
        const a = i * step + ring.angJ[i];
        const rad = r - d * band + ring.radJ[i] * Math.sin(drift + ring.phase[i]);
        const size = base * (1 + 0.9 * d);                           // the back of the wave: broader, softer
        const x = ox + Math.cos(a) * rad, y = oy + Math.sin(a) * rad;
        if (x < -size || y < -size || x > W + size || y > H + size) continue;
        const gain = (ring.crest[i] ? CREST_GAIN : 1) * (1 - 0.65 * d) * 0.85;   // bright crest, dim wash; neighbours blend
        ctx.globalAlpha = Math.min(1, alpha * gain);
        ctx.drawImage(sprite, x - size / 2, y - size / 2, size, size);
      }
    }
    ctx.globalAlpha = 1;
    ctx.restore();
    if (drive) drive(now);
    if (any || drive) raf = requestAnimationFrame(frame);
    else { lastFrame = 0; ctx.clearRect(0, 0, W, H); }
  }
  function startLoop() { if (!raf && canvas && !hidden) raf = requestAnimationFrame(frame); }

  // ---- in time with the orb (Kris, 30 Sep) ----
  // The orb's thin green ring breathes on a CSS pulse (vl-ring-live, 1.8 s,
  // widest at the halfway point). Every particle ring is born as that pulse
  // reaches its widest, so the water leaves the orb on its beat. The phase is
  // read from the live animation, never assumed.
  function pulsePeakIn(minMs = 0) {
    let period = 1800, phase = 0;
    try {
      const span = orb.querySelector("span");
      const anims = span && span.getAnimations ? span.getAnimations() : [];
      const a = anims.find((x) => /^vl-ring-/.test(x.animationName || "")) || anims[0];
      if (a && a.currentTime != null) {
        period = Number(a.effect.getTiming().duration) || period;
        phase = ((a.currentTime % period) + period) % period;
      }
    } catch (_e) { /* no animation to follow: fall back to the plain timers */ }
    let d = period / 2 - phase;
    while (d < minMs) d += period;
    return d;
  }

  // ---- the states ----
  let mode = "still";             // still | loading | error
  let cadence = 0, ringNo = 0, readyTimer = 0, dropTimer = 0;
  function clearTimers() { clearTimeout(cadence); clearTimeout(readyTimer); clearTimeout(dropTimer); cadence = readyTimer = dropTimer = 0; }
  function nextLoadingRing() {
    cadence = 0;
    if (mode !== "loading" || hidden) return;
    emit({ radius: 1, alpha: PEAKS[Math.min(ringNo, PEAKS.length - 1)], hue: "blue", speed: 1 });
    ringNo += 1;
    cadence = setTimeout(nextLoadingRing, pulsePeakIn(CADENCE_MS));   // the next pulse peak, at least a cadence away
  }
  // The stone hits: the mic dips, the orb shudders once, then ring one leaves.
  function drop() {
    if (mode === "loading") return;          // already falling (reconnect calls connecting again)
    clearTimers();                           // an error beat ends here: the stone is dropping again
    mode = "loading"; ringNo = 0;
    relayout();
    if (root) {
      root.classList.remove("vl-drop");
      void root.offsetWidth;
      root.classList.add("vl-drop");
      dropTimer = setTimeout(() => root && root.classList.remove("vl-drop"), DROP_CLASS_MS);
    }
    cadence = setTimeout(nextLoadingRing, pulsePeakIn(FIRST_RING_MS));
  }
  // The socket is open: no new loading ring, the one in flight finishes.
  // (Also ends an error beat: the fault has been dealt with.)
  function settle() { mode = "still"; clearTimeout(cadence); cadence = 0; }
  // Listening: a beat of stillness, then one larger green ring.
  function ready() {
    settle();
    clearTimeout(readyTimer);
    readyTimer = setTimeout(() => { readyTimer = 0; relayout(); emit({ radius: 1.35, alpha: 1, hue: "green", speed: 1 / 1.25, priority: true }); }, pulsePeakIn(READY_STILL_MS));
  }
  // A fault: a faint red double-beat (ring, ring, rest) that keeps going
  // until the phase moves on: the next drop, ready or still ends it.
  function errorBeat(second) {
    cadence = 0;
    if (mode !== "error" || hidden) return;
    emit({ radius: 1, alpha: ERROR_ALPHA, hue: "rose", speed: 1 / 1.5, priority: true });
    // the pair's first ring rides a pulse peak; the second follows it closely
    cadence = setTimeout(() => errorBeat(!second), second ? pulsePeakIn(ERROR_REST_MS) : ERROR_BEAT_MS);
  }
  function error() {
    if (mode === "error") return;
    settle();
    clearTimeout(readyTimer); readyTimer = 0;
    mode = "error";
    relayout();
    cadence = setTimeout(() => errorBeat(false), pulsePeakIn(0));
  }
  function still() { settle(); clearTimeout(readyTimer); readyTimer = 0; }

  // Tab hidden: stop drawing and the cadence; back: no backlog, the cadence
  // simply resumes from now if the screen is still loading.
  function pause() {
    hidden = true;
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
    clearTimeout(cadence); cadence = 0;
  }
  function resume() {
    if (!hidden) return;
    hidden = false; lastFrame = 0;
    for (const r of rings) r.alive = false;    // rings born before the sleep would all arrive at once
    if (mode === "loading") cadence = setTimeout(nextLoadingRing, pulsePeakIn(CADENCE_MS));
    else if (mode === "error") cadence = setTimeout(() => errorBeat(false), pulsePeakIn(ERROR_REST_MS));
    startLoop();
  }

  function destroy() {
    clearTimers();
    clearTimeout(tintTimer); untint();
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
    if (ro) { ro.disconnect(); ro = null; }
    window.removeEventListener("resize", onResize);
    window.removeEventListener("orientationchange", onResize);
    if (canvas && canvas.parentNode) canvas.parentNode.removeChild(canvas);
    if (root) root.classList.remove("vl-drop");
    canvas = null; sprites = null; drive = null; root = null;
  }

  return { reduced: false, drop, settle, ready, error, still, emit, setDrive, pause, resume, relayout, destroy };
}
