// Water: the ripple engine, with no knowledge of any page. A calm lake the size of its host element. A stone makes rings,
// a pointer moving across it leaves small ripples sized by its speed, rings spread, bounce back off the edges and run into
// one another, and the crests glow in BLAM8's colours (or a warm set while muted).
//
// It is a real 2D wave simulation (a grid, two height buffers, reflecting edges, a little damping), ported from the pond on
// haail.dev. Nothing happens on its own: the loop runs only while something is moving the water and goes to sleep once the
// lake is still. It stops when the tab is hidden, draws nothing under reduced motion, and on a slow device coarsens the lake
// and then stops itself rather than the page.
//
// What a place adds (the voice screen's orb, its voice, the text it keeps clear) comes in through `options`, so this file stays
// the same wherever the water is used. Plain ES module, no imports. A failure here must never touch the page: callers wrap it
// in try/catch.
//
// options:
//   config      overrides for DEFAULTS (water, pointer, frame rate, governor)
//   tier        'full' | 'slim' (default: detected from the device)
//   ramp, warmRamp   colour stops (default: BLAM8's, and the Mist's warm ones)
//   surface     the element whose pointer events move the water (default: host)
//   pointer     false: listen to no pointer or touch at all (water that is only moved by its place)
//   canvasClass, canvasStyle, mount(canvas, host)   how the canvas is made and placed (default: first in the host, filling it)
//   ignore(target)       true: a press on this element drops no stone
//   keep(g)      called whenever the layout is read: dim the water behind things with g.dim(x0, y0, x1, y1, strength) or by
//                writing g.keep (one number per cell, 1 = clear); g carries cell, gc, gr, left, top and feather
//   busy(now)    true while the place itself keeps the water moving (a voice)
//   onFrame(dt)  once per drawn frame, before the water steps: a place may add stones here
//   quiet()      true when the place has nothing more to add (the lake may sleep)
//   pending()    true when the place has work waiting for a wake-up after the tab was hidden
//   onClear()    the lake was emptied
// returned: drop, addStone, wake, setWarm, hold (true: hold the water still and empty), queued, snapshot, destroy, canvas

const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);
const RAMP = [[166, 87, 255], [31, 173, 255], [69, 213, 207], [87, 205, 136], [150, 206, 238], [166, 87, 255]];   // violet, cyan, teal, green, sky, violet
const WARM = [[255, 31, 9], [255, 120, 12], [255, 166, 15], [255, 215, 40], [255, 226, 140], [255, 31, 9]];       // red, orange, amber, yellow, pale gold, red: the Mist's warm colours when muted
function ramp(u, out, table) {
  const f = (u - Math.floor(u)) * (table.length - 1), i = Math.floor(f), k = f - i, a = table[i], b = table[i + 1];
  out[0] = a[0] + (b[0] - a[0]) * k; out[1] = a[1] + (b[1] - a[1]) * k; out[2] = a[2] + (b[2] - a[2]) * k;
}
const smooth = (a, b, v) => { const t = clamp((v - a) / (b - a), 0, 1); return t * t * (3 - 2 * t); };

// The brightness curve and the colour ramp as tables, so a frame does no exp, pow or ramp work per cell.
const LUT_N = 2048, LUT_STEP = 1 / 1024;
const TAIL = [-0.62, 0.42, -0.28, 0.18];
const nothing = () => {};

export const DEFAULTS = {
  // the water
  cellDiv: 320, cellMin: 3, cellMax: 9,   // a water cell is the long side of the host / cellDiv px, clamped
  speed: 0.6,            // wave speed in cells per step (squared in the maths; stable below 0.7)
  damp: 0.9935,          // velocity kept each step: rings fade over a few seconds
  stepsPerSec: 60, maxStepsPerFrame: 5,
  gain: 16, curve: 2.2, gradGain: 0.5, troughShare: 0.4, alphaCap: 0.9,   // how brightly a wave glows, and its edge
  opacity: 1,            // the whole picture, fainter (below 1) or stronger
  ringGapSteps: 8,        // steps between the successive bumps of one stone
  warmSecs: 0.6,          // how long the colours take to turn warm and back
  // taps and pointer
  tapAmp: 0.65, tapSigmaPx: 8,
  moveAmp: 0.16, moveSigmaPx: 9, moveGapMs: 28,
  flickSpeed: 1.1, flickAmp: 0.9, flickSigmaPx: 16, flickGapMs: 380, flickTravelPx: 320,
  // what a place keeps clear
  keepFeatherPx: 26, keepEveryMs: 500,
  // frame rate and cost
  fpsActive: 60, fpsSlim: 30, fpsTail: 30,
  calmLake: 0.006, calmFramesToSleep: 12,
  governorMs: 7, governorWarmupMs: 2000, governorSamples: 24, resizeKeepPx: 120,
};

export function createWater(host, options = {}) {
  const CFG = Object.assign({}, DEFAULTS, options.config);
  const cool = options.ramp || RAMP, hot = options.warmRamp || WARM;
  const BRIGHT = new Float32Array(LUT_N), COLOURS = new Uint8Array(256 * 3);
  for (let n = 0; n < LUT_N; n++) BRIGHT[n] = Math.min(CFG.alphaCap, Math.pow(1 - Math.exp(-CFG.gain * n * LUT_STEP), CFG.curve)) * CFG.opacity;
  /** The colour table for a mix of the cool ramp (0) and the warm one (1). */
  function fillColours(warmth) {
    const a = [0, 0, 0], b = [0, 0, 0];
    for (let n = 0; n < 256; n++) {
      ramp(n / 256, a, cool); ramp(n / 256, b, hot);
      for (let c = 0; c < 3; c++) COLOURS[n * 3 + c] = a[c] + (b[c] - a[c]) * warmth;
    }
  }
  fillColours(0);
  const win = window, doc = document;
  const reduced = win.matchMedia('(prefers-reduced-motion: reduce)');
  const forced = win.matchMedia('(forced-colors: active), (prefers-contrast: more)');
  const slim = options.tier ? options.tier === 'slim' : !(win.matchMedia('(hover: hover) and (pointer: fine)').matches && (navigator.hardwareConcurrency || 4) > 2 && !navigator.connection?.saveData);
  const canvas = doc.createElement('canvas');
  if (options.canvasClass) canvas.className = options.canvasClass;
  canvas.setAttribute('aria-hidden', 'true'); canvas.setAttribute('role', 'presentation');
  canvas.style.cssText = options.canvasStyle || 'position:absolute;inset:0;width:100%;height:100%;z-index:0;pointer-events:none;';
  const ctx = canvas.getContext('2d', {alpha: true});
  const field = doc.createElement('canvas'), fctx = field.getContext('2d');
  if (!ctx || !fctx) throw Error('Canvas unavailable');
  if (options.mount) options.mount(canvas, host); else host.insertBefore(canvas, host.firstChild);
  const surface = options.surface || host;
  // The lake is the size of its host. With lakeFromCanvas it is the size and place of its CANVAS instead (the same thing while the canvas fills its host, which
  // is its default): a place can then lay the canvas out wider than the host, or move it, and the water, the taps and what it keeps clear follow it.
  const lakeBox = () => (options.lakeFromCanvas && canvas.clientWidth > 0 && canvas.clientHeight > 0 ? canvas : host);
  const keepHook = options.keep || nothing, busyHook = options.busy || (() => false), frameHook = options.onFrame || nothing;
  const quietHook = options.quiet || (() => true), pendingHook = options.pending || (() => false), clearHook = options.onClear || nothing;
  const ignoreHook = options.ignore || (() => false);

  let dead = false, frozen = false, raf = 0;
  let vw = 0, vh = 0, left = 0, top = 0, cell = 8, gc = 0, gr = 0, dpr = 1, cellBoost = 1, dprCap = 1.5;
  let hgt = new Float32Array(0), prev = new Float32Array(0), next = new Float32Array(0), keep = new Float32Array(0), hue = new Float32Array(0), tint = new Float32Array(0), tintAt = -1;
  let img = null;
  const pulses = [];
  let stepCount = 0, simTime = 0, simDebt = 0;
  const ptr = {x: 0, y: 0, seen: false, last: 0, speed: 0, travel: 0, lastDrop: 0, lastFlick: 0, activeUntil: 0};
  const stat = {drops: 0, ticks: 0, fps: 0, fpsCount: 0, fpsStamp: 0, work: [], calm: 0, lakeMax: 0, sleeps: 0, wakes: 0};
  let warm = 0, warmTarget = 0;   // 0 = the cool colours, 1 = the warm ones
  let held = false;               // a place can hold the water still (something else is covering the screen)
  let lastDraw = 0, lastKeep = 0, governed = 0, governorStart = 0, resizeTimer = 0, laidOut = false;

  const allowed = () => !dead && !frozen && !held && !doc.hidden && !reduced.matches && !forced.matches;

  /* ---- layout */
  function layout() {
    const box = lakeBox();
    vw = box.clientWidth; vh = box.clientHeight;
    if (!(vw > 0 && vh > 0)) { laidOut = false; return; }
    const r = box.getBoundingClientRect(); left = r.left; top = r.top;
    cell = clamp(Math.round(Math.max(vw, vh) / CFG.cellDiv * cellBoost), CFG.cellMin, CFG.cellMax * cellBoost);
    const ncols = Math.ceil(vw / cell) + 1, nrows = Math.ceil(vh / cell) + 1;
    if (ncols !== gc || nrows !== gr || !hgt.length) {
      gc = ncols; gr = nrows;
      hgt = new Float32Array(gc * gr); prev = new Float32Array(gc * gr); next = new Float32Array(gc * gr);
      keep = new Float32Array(gc * gr).fill(1); hue = new Float32Array(gc * gr); tint = new Float32Array(gc * gr); tintAt = -1;
      for (let j = 0; j < gr; j++) for (let i = 0; i < gc; i++) hue[j * gc + i] = i * cell * 0.0062 - j * cell * 0.0044;   // the colour drifts slowly across the surface
      field.width = gc; field.height = gr;
      img = fctx.createImageData(gc, gr);
      pulses.length = 0; simDebt = 0;
    }
    dpr = Math.min(win.devicePixelRatio || 1, slim ? 1 : dprCap);
    while (dpr > 0.5 && vw * dpr * vh * dpr > 2.4e6) dpr = Math.max(0.5, dpr - 0.1);
    canvas.width = Math.round(vw * dpr); canvas.height = Math.round(vh * dpr);
    laidOut = true;
    refreshGeometry();
  }

  /** What the place wants kept clear. Read now and then, never every frame. */
  function refreshGeometry() {
    if (!laidOut) return;
    const hr = lakeBox().getBoundingClientRect(); left = hr.left; top = hr.top;
    keep.fill(1);
    const f = CFG.keepFeatherPx;
    const dim = (x0, y0, x1, y1, strength) => {
      const i0 = Math.max(0, Math.floor((x0 - f) / cell)), i1 = Math.min(gc - 1, Math.ceil((x1 + f) / cell));
      const j0 = Math.max(0, Math.floor((y0 - f) / cell)), j1 = Math.min(gr - 1, Math.ceil((y1 + f) / cell));
      for (let j = j0; j <= j1; j++) {
        const cy = (j + 0.5) * cell, dy = Math.max(y0 - cy, 0, cy - y1);
        for (let i = i0; i <= i1; i++) {
          const cx = (i + 0.5) * cell, dx = Math.max(x0 - cx, 0, cx - x1);
          const k = j * gc + i, edge = smooth(0, f, Math.hypot(dx, dy));   // 0 on the box, 1 a feather away
          keep[k] *= strength + (1 - strength) * edge;
        }
      }
    };
    keepHook({keep, dim, cell, gc, gr, left, top, feather: f});
  }

  /* ---- the water */
  function bump(x, y, amp, sigmaPx) {
    const gx = x / cell - 0.5, gy = y / cell - 0.5, s = Math.max(1, sigmaPx / cell), rad = Math.ceil(s * 3);
    const i0 = Math.max(0, Math.floor(gx - rad)), i1 = Math.min(gc - 1, Math.ceil(gx + rad));
    const j0 = Math.max(0, Math.floor(gy - rad)), j1 = Math.min(gr - 1, Math.ceil(gy + rad));
    const inv = 1 / (2 * s * s);
    for (let j = j0; j <= j1; j++) for (let i = i0; i <= i1; i++) {
      const v = amp * Math.exp(-((i - gx) * (i - gx) + (j - gy) * (j - gy)) * inv), k = j * gc + i;
      hgt[k] += v; prev[k] += v;   // no starting velocity: the bump collapses into rings
    }
  }
  /** A stone: a bump, then a few weaker ones of alternating sign, so it leaves a set of rings and not one. */
  function addStone(x, y, amp, sigmaPx, rings = 4) {
    bump(x, y, amp, sigmaPx);
    stat.drops++;
    if (pulses.length > 96) return;
    for (let n = 1; n < rings; n++) pulses.push({x, y, amp: TAIL[n - 1] * amp, sigma: sigmaPx, at: stepCount + n * CFG.ringGapSteps});
  }
  function step() {
    stepCount++;
    for (let q = pulses.length - 1; q >= 0; q--) if (pulses[q].at <= stepCount) { const p = pulses[q]; bump(p.x, p.y, p.amp, p.sigma); pulses.splice(q, 1); }
    const c2 = CFG.speed * CFG.speed, damp = CFG.damp, w = gc, h = gr;
    for (let j = 0; j < h; j++) {
      const ro = j * w, rm = (j > 0 ? j - 1 : j) * w, rp = (j < h - 1 ? j + 1 : j) * w;
      for (let i = 0; i < w; i++) {
        const k = ro + i, c = hgt[k];   // the edge reflects: a neighbour off the screen counts as this cell itself
        const lap = hgt[ro + (i > 0 ? i - 1 : i)] + hgt[ro + (i < w - 1 ? i + 1 : i)] + hgt[rm + i] + hgt[rp + i] - 4 * c;
        next[k] = c + (c - prev[k]) * damp + c2 * lap;
      }
    }
    const t = prev; prev = hgt; hgt = next; next = t;
  }
  /** Water a stone adds has nowhere to go in a closed lake: take the average level out so the lake can settle completely. */
  function removeMean() {
    let sum = 0;
    for (let q = 0; q < hgt.length; q++) sum += hgt[q];
    const mean = sum / hgt.length;
    let mx = 0;
    for (let q = 0; q < hgt.length; q++) { hgt[q] -= mean; prev[q] -= mean; const v = hgt[q] < 0 ? -hgt[q] : hgt[q]; if (v > mx) mx = v; }
    stat.lakeMax = mx;
  }

  /* ---- drawing */
  function draw(t) {
    const data = img.data, gg = CFG.gradGain, trough = CFG.troughShare, w = gc, h = gr;
    // the colour drifts slowly across the surface: refresh it a few times a second, not every frame
    if (tintAt < 0 || t - tintAt > 0.12) { tintAt = t; const ph = t * 0.14; for (let k = 0; k < tint.length; k++) tint[k] = 0.5 + 0.5 * Math.sin(hue[k] + ph); }
    for (let j = 0; j < h; j++) {
      const ro = j * w, rm = (j > 0 ? j - 1 : j) * w, rp = (j < h - 1 ? j + 1 : j) * w;
      for (let i = 0; i < w; i++) {
        const k = ro + i, o = k * 4, v = hgt[k];
        const slope = Math.abs(hgt[ro + (i < w - 1 ? i + 1 : i)] - hgt[ro + (i > 0 ? i - 1 : i)]) + Math.abs(hgt[rp + i] - hgt[rm + i]);
        const val = (v > 0 ? v : -v * trough) + gg * slope;
        if (val < 0.0015) { data[o + 3] = 0; continue; }   // still water: nothing to compute here
        const a = BRIGHT[Math.min(LUT_N - 1, (val * 1024) | 0)] * keep[k];
        if (a < 0.004) { data[o + 3] = 0; continue; }
        let u = tint[k] + (v < 0 ? 0.18 : 0); if (u >= 1) u -= 1;
        const c = ((u * 255) | 0) * 3;
        data[o] = COLOURS[c]; data[o + 1] = COLOURS[c + 1]; data[o + 2] = COLOURS[c + 2]; data[o + 3] = (a * 255 + 0.5) | 0;
      }
    }
    fctx.putImageData(img, 0, 0);
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(field, 0, 0, gc * cell * dpr, gr * cell * dpr);
  }
  function clearAll() {
    hgt.fill(0); prev.fill(0); next.fill(0); pulses.length = 0; simDebt = 0; clearHook();
    if (img) img.data.fill(0);
    ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, canvas.width, canvas.height);
  }

  /* ---- loop (it exists only while something is moving the water) */
  function sleep(clear) {
    cancelAnimationFrame(raf); raf = 0; lastDraw = 0; stat.calm = 0;
    if (clear) clearAll();
    stat.sleeps++;
  }
  function wake() {
    if (raf || !laidOut || !allowed()) return;
    lastDraw = 0; stat.calm = 0; stat.wakes++;
    refreshGeometry();
    raf = requestAnimationFrame(frame);
  }
  function frame(now) {
    raf = 0;
    if (!allowed()) { lastDraw = 0; return; }
    const placeBusy = busyHook(now);
    const pointerActive = now < ptr.activeUntil;
    const busy = pointerActive || placeBusy || pulses.length > 0;
    raf = requestAnimationFrame(frame);
    if (now - lastDraw < 1000 / (busy ? (slim ? CFG.fpsSlim : CFG.fpsActive) : CFG.fpsTail) - 2) return;
    const dtc = clamp(lastDraw ? (now - lastDraw) / 1000 : 1 / 60, 0.001, 0.1);   // time since the last drawn frame, read each callback
    lastDraw = now;
    const w0 = performance.now();
    if (warm !== warmTarget) {   // the colours slide to warm when asked, and back
      const d = warmTarget - warm, s = dtc / CFG.warmSecs;
      warm = Math.abs(d) <= s ? warmTarget : warm + Math.sign(d) * s; fillColours(warm);
    }
    frameHook(dtc);
    simDebt += dtc * CFG.stepsPerSec;
    let steps = Math.floor(simDebt); simDebt -= steps;
    if (steps > CFG.maxStepsPerFrame) steps = CFG.maxStepsPerFrame;
    for (let s = 0; s < steps; s++) step();
    simTime += dtc;
    if (stat.ticks % 6 === 0) removeMean();
    if (now - lastKeep > CFG.keepEveryMs) { lastKeep = now; refreshGeometry(); }
    draw(simTime);
    // a still lake: stop the loop, empty the canvas, and wait for the next stone, finger or voice
    const calm = stat.lakeMax < CFG.calmLake && pulses.length === 0 && quietHook() && !pointerActive;
    stat.calm = calm ? stat.calm + 1 : 0;
    if (stat.calm > CFG.calmFramesToSleep) { cancelAnimationFrame(raf); sleep(true); return; }
    const ms = performance.now() - w0;
    stat.work.push(ms); if (stat.work.length > 300) stat.work.shift();
    stat.ticks++; stat.fpsCount++;
    if (!stat.fpsStamp) stat.fpsStamp = now;
    if (now - stat.fpsStamp > 1000) { stat.fps = stat.fpsCount * 1000 / (now - stat.fpsStamp); stat.fpsCount = 0; stat.fpsStamp = now; }
    // a slow device: first a coarser, lower-resolution lake; if it is still too slow, stop the effect rather than the page
    if (!governorStart) governorStart = now;
    if (now - governorStart > CFG.governorWarmupMs && stat.work.length >= CFG.governorSamples && governed < 2) {
      const recent = stat.work.slice(-CFG.governorSamples);
      if (recent.reduce((a, b) => a + b, 0) / recent.length > CFG.governorMs) {
        governed++; governorStart = now; stat.work.length = 0;
        if (governed === 1) { dprCap = 1; cellBoost = 1.4; layout(); }
        else { frozen = true; cancelAnimationFrame(raf); sleep(true); canvas.style.display = 'none'; }
      }
    }
  }

  /** Hold the water still and empty (true), or let it move again (false): for when something else is covering the screen. */
  function hold(on) { held = Boolean(on); if (held) sleep(true); }

  /** The colours turn warm (1) or back to the cool ones (0). */
  function setWarm(target) {
    warmTarget = target ? 1 : 0;
    if (!raf && warm !== warmTarget) { warm = warmTarget; fillColours(warm); }   // nothing on screen to fade: switch at once
  }

  /* ---- the pointer */
  // A lake laid out from its canvas can be moved (swiped across) between the times its place is measured, so a press measures it again.
  function syncOrigin() { if (options.lakeFromCanvas) { const r = lakeBox().getBoundingClientRect(); left = r.left; top = r.top; } }
  function local(e) { syncOrigin(); return [e.clientX - left, e.clientY - top]; }
  function drop(x, y, amp = CFG.tapAmp, sigmaPx = CFG.tapSigmaPx) {
    if (!laidOut || !allowed()) return;
    addStone(x, y, amp, sigmaPx);
    ptr.activeUntil = performance.now() + 1500;
    wake();
  }
  function onDown(e) {
    if (e.button !== 0 || e.isPrimary === false) return;
    if (ignoreHook(e.target)) return;   // a place may have somewhere a stone does not belong
    const [x, y] = local(e);
    ptr.last = 0; ptr.travel = 0; ptr.x = x; ptr.y = y; ptr.seen = true;
    drop(x, y);
  }
  function move(x, y) {
    if (!laidOut || !allowed()) return;
    const now = performance.now(), dtMs = Math.max(1, now - ptr.last);
    if (now - ptr.last > 150) ptr.travel = 0;
    if (ptr.last && ptr.seen) {
      const dist = Math.hypot(x - ptr.x, y - ptr.y);
      ptr.speed += (dist / dtMs - ptr.speed) * 0.35; ptr.travel += dist;
      if (ptr.speed < CFG.flickSpeed * 0.5) ptr.travel = 0;
    }
    ptr.x = x; ptr.y = y; ptr.seen = true; ptr.last = now;
    if (pulses.length >= 96) return;
    let dropped = false;
    if (now - ptr.lastDrop > CFG.moveGapMs && ptr.speed > 0.05) { ptr.lastDrop = now; dropped = true; addStone(x, y, Math.min(0.6, CFG.moveAmp * ptr.speed + 0.03), CFG.moveSigmaPx); }
    if (ptr.speed > CFG.flickSpeed && ptr.travel > CFG.flickTravelPx && now - ptr.lastFlick > CFG.flickGapMs) { ptr.lastFlick = now; ptr.travel = 0; dropped = true; addStone(x, y, CFG.flickAmp, CFG.flickSigmaPx); }
    if (dropped) { ptr.activeUntil = now + 1500; wake(); }   // a pointer that is only resting on the screen leaves the lake asleep
  }
  function onMove(e) { if (e.pointerType !== 'touch') { const [x, y] = local(e); move(x, y); } }
  function onTouch(e) { const t = e.touches[0]; if (t) { syncOrigin(); move(t.clientX - left, t.clientY - top); } }
  function onLeave() { ptr.seen = false; ptr.speed = 0; ptr.travel = 0; ptr.last = 0; }

  /* ---- lifecycle */
  function onResize() {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (dead) return;
      const box = lakeBox(), nw = box.clientWidth, nh = box.clientHeight;
      if (laidOut && nw === vw && Math.abs(nh - vh) < CFG.resizeKeepPx) { refreshGeometry(); return; }   // a phone toolbar sliding in does not rebuild the lake
      const was = raf; sleep(true); layout();
      if (was) wake();
    }, 150);
  }
  function onVisible() { if (doc.hidden) { cancelAnimationFrame(raf); raf = 0; lastDraw = 0; } else if (stat.lakeMax >= CFG.calmLake || pulses.length || pendingHook()) wake(); }
  function onMotion() { if (reduced.matches || forced.matches) { sleep(true); canvas.style.display = 'none'; } else if (!frozen) canvas.style.display = ''; }
  const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(onResize) : null;
  ro?.observe(host);
  if (options.lakeFromCanvas) ro?.observe(canvas);   // the canvas can change size without its host doing so
  const listening = options.pointer !== false;
  if (listening) {
    surface.addEventListener('pointerdown', onDown, {capture: true, passive: true});
    surface.addEventListener('pointermove', onMove, {passive: true});
    surface.addEventListener('touchmove', onTouch, {passive: true});
    surface.addEventListener('pointerleave', onLeave);
    win.addEventListener('blur', onLeave);
  }
  doc.addEventListener('visibilitychange', onVisible);
  reduced.addEventListener?.('change', onMotion); forced.addEventListener?.('change', onMotion);
  layout();
  onMotion();

  return {
    drop, addStone, wake, setWarm, hold, canvas,
    queued: () => pulses.length,
    snapshot: () => {
      const sorted = stat.work.slice().sort((a, b) => a - b);
      return {running: Boolean(raf), laidOut, frozen, cell, cols: gc, rows: gr, dpr, slim, governed,
        drops: stat.drops, ticks: stat.ticks, fps: Math.round(stat.fps), lakeMax: +stat.lakeMax.toFixed(4), sleeps: stat.sleeps, wakes: stat.wakes,
        workMsMedian: sorted.length ? +sorted[Math.floor(sorted.length / 2)].toFixed(2) : 0, workMsP95: sorted.length ? +sorted[Math.floor(sorted.length * 0.95)].toFixed(2) : 0};
    },
    destroy() {
      if (dead) return;
      dead = true; cancelAnimationFrame(raf); raf = 0; clearTimeout(resizeTimer);
      ro?.disconnect();
      if (listening) {
        surface.removeEventListener('pointerdown', onDown, true);
        surface.removeEventListener('pointermove', onMove); surface.removeEventListener('touchmove', onTouch); surface.removeEventListener('pointerleave', onLeave);
        win.removeEventListener('blur', onLeave);
      }
      doc.removeEventListener('visibilitychange', onVisible);
      reduced.removeEventListener?.('change', onMotion); forced.removeEventListener?.('change', onMotion);
      canvas.remove();
    },
  };
}
