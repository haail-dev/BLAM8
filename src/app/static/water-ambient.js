// Ripples inside a card: a small stone every few seconds, so the card keeps a very subtle life of its own. A page marks the cards with
// data-water-card. The water is clipped to the card's rounded corners and sits behind its text (the card becomes its own stacking
// context, the canvas is z-index -1 inside it), so nothing on the card changes.
//
// It moves nothing but itself and costs little: it runs only while the card is on screen and actually visible (a page that stacks its
// slides leaves hidden ones "on screen"), at 24 frames a second with coarse cells, and the water sleeps between stones. It stops when the
// tab is hidden and draws nothing under reduced motion or data saver. A failure here must never touch the page.

const revision = new URL(import.meta.url).search;
const { createWater } = await import('./water-core.js' + revision);

const EVERY = [2600, 4800];   // ms between stones
const AMP = 0.45, SIGMA_PX = 9;
const LOOK = {
  ramp: [[140, 70, 245], [0, 150, 235], [20, 175, 170], [50, 175, 105], [90, 170, 225], [140, 70, 245]],   // the light page look (water-page.js)
  config: {damp: 0.991, gain: 14, curve: 2.0, alphaCap: 0.5, opacity: 1.2, cellMin: 6, fpsActive: 24, fpsSlim: 24, fpsTail: 24},
};

function attach(card) {
  if (card.dataset.waterAttached) return;
  card.dataset.waterAttached = '1';
  try {
    card.style.isolation = 'isolate';
    if (getComputedStyle(card).position === 'static') card.style.position = 'relative';
    const water = createWater(card, {
      pointer: false,
      ramp: LOOK.ramp,
      config: LOOK.config,
      canvasStyle: 'position:absolute;inset:0;width:100%;height:100%;z-index:-1;pointer-events:none;border-radius:inherit;mix-blend-mode:multiply;',
    });
    let onScreen = false, timer = 0;
    const showing = () => onScreen && !document.hidden && (!card.checkVisibility || card.checkVisibility({opacityProperty: true, visibilityProperty: true}));
    function stone() {
      if (!showing()) return;
      const w = card.clientWidth, h = card.clientHeight, m = 0.14;
      water.drop(w * (m + (1 - 2 * m) * Math.random()), h * (m + (1 - 2 * m) * Math.random()), AMP * (0.75 + 0.5 * Math.random()), SIGMA_PX);
    }
    function schedule() {
      clearTimeout(timer);
      if (!onScreen) return;
      timer = setTimeout(() => { stone(); schedule(); }, EVERY[0] + (EVERY[1] - EVERY[0]) * Math.random());
    }
    new IntersectionObserver(entries => { onScreen = entries.some(e => e.isIntersecting); if (onScreen) schedule(); else clearTimeout(timer); }).observe(card);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) schedule(); });
    card.addEventListener('blam8-water-state', e => { e.detail.snapshot = water.snapshot(); });   // read-only, for the tests
  } catch (_) { card.querySelector(':scope > canvas')?.remove(); }
}

if (!navigator.connection?.saveData && typeof IntersectionObserver === 'function') {
  for (const card of document.querySelectorAll('[data-water-card]')) attach(card);
}
