// Ripples behind the page: a soft moving water background for the app, switched on per device (Settings, next to Light / Dark).
// One canvas fills the viewport behind everything (z-index -1 inside an isolated <body>), so every chip, card and bar stays exactly
// as it is. A tap or a press drops a stone, a mouse (or a finger, scrolling) leaves a gentle trail, and the rings spread, bounce
// off the edges and fade. The water is water-core.js; this file chooses the look for the theme and watches the two attributes
// the theme script keeps on <html>: data-water (on / off) and data-theme (light / dark).
//
// It is quiet by design: nothing moves until the page is touched, the loop sleeps when the lake is still, it is off under reduced
// motion or data saver, and it holds still while the live voice screen (which has its own water) is open.
// A failure here must never touch the page: everything is wrapped, and the page works the same without it.

const revision = new URL(import.meta.url).search;
const { createWater } = await import('./water-core.js' + revision);

// The same simulation, drawn two ways. Light: pigment on white (deeper colours, low opacity, multiplied in). Dark: light on dark
// (BLAM8's colours, lower than the voice screen so a working page stays calm).
const LOOK = {
  light: {
    blend: 'multiply',
    ramp: [[140, 70, 245], [0, 150, 235], [20, 175, 170], [50, 175, 105], [90, 170, 225], [140, 70, 245]],
    config: {damp: 0.991, gain: 14, curve: 2.0, alphaCap: 0.5, tapAmp: 0.5, moveAmp: 0.12, flickAmp: 0.6, flickSigmaPx: 12},
  },
  dark: {
    blend: 'normal',
    ramp: null,
    config: {damp: 0.991, gain: 14, curve: 2.0, alphaCap: 0.65, tapAmp: 0.5, moveAmp: 0.12, flickAmp: 0.6, flickSigmaPx: 12},
  },
};

const root = document.documentElement;
let host = null, water = null, held = false;

// where the water sits: <body>, or the element a page marks with data-water-host (the landing's deck paints its own background, so
// the water must sit inside it)
const place = () => document.querySelector('[data-water-host]') || document.body;
const wanted = () => root.getAttribute('data-water') === 'on' && !navigator.connection?.saveData;
const theme = () => (root.getAttribute('data-theme') === 'dark' ? 'dark' : 'light');

function start() {
  if (water || !wanted() || !document.body) return;
  try {
    const look = LOOK[theme()];
    host = document.createElement('div');
    host.id = 'water-host'; host.setAttribute('aria-hidden', 'true');
    host.style.cssText = 'position:fixed;inset:0;z-index:-1;pointer-events:none;';
    place().append(host);
    water = createWater(host, {
      surface: window,
      ramp: look.ramp || undefined,
      config: look.config,
      canvasStyle: 'position:absolute;inset:0;width:100%;height:100%;pointer-events:none;mix-blend-mode:' + look.blend + ';',
    });
    water.hold(held);
  } catch (_) { stop(); }
}
function stop() {
  try { water?.destroy(); } catch (_) {}
  host?.remove();
  water = host = null;
}
// The Concierge pop-up (#cz-modal) covers the page with an opaque card, so the page's water cannot show there. While it is open it
// gets its own lake inside the card, behind the chat and above the card's colour, with the same look and the same switch
// (Kris 2026-10-16: "the ripple effect is still not on the modal"). Taps and the pointer on the card move it.
let modalHost = null, modalWater = null;
const modalCard = () => document.getElementById('cz-card');
const modalOpen = () => { const m = document.getElementById('cz-modal'); return !!m && !m.classList.contains('hidden'); };
function startModal() {
  const card = modalCard();
  if (modalWater || !card || !wanted() || !modalOpen()) return;
  try {
    const look = LOOK[theme()];
    modalHost = document.createElement('div');
    modalHost.id = 'water-modal-host'; modalHost.setAttribute('aria-hidden', 'true');
    modalHost.style.cssText = 'position:absolute;inset:0;z-index:-1;pointer-events:none;';
    card.prepend(modalHost);
    modalWater = createWater(modalHost, {
      surface: card,
      ramp: look.ramp || undefined,
      config: look.config,
      canvasStyle: 'position:absolute;inset:0;width:100%;height:100%;pointer-events:none;mix-blend-mode:' + look.blend + ';',
    });
    modalWater.hold(held);
  } catch (_) { stopModal(); }
}
function stopModal() {
  try { modalWater?.destroy(); } catch (_) {}
  modalHost?.remove();
  modalWater = modalHost = null;
}
const syncModal = () => { if (modalOpen()) { if (!modalWater) startModal(); } else stopModal(); };

// the switch or the theme changed: rebuild with the right look (the lake is empty after a change, which is what a change deserves)
function sync() { stop(); start(); stopModal(); startModal(); }

// <body> (or the marked element) is its own stacking context, so the canvas (z-index -1) sits above its colour and below everything else
const style = document.createElement('style');
style.textContent = 'html[data-water="on"] body,html[data-water="on"] [data-water-host],html[data-water="on"] #cz-card{isolation:isolate}';
document.head.append(style);

new MutationObserver(sync).observe(root, {attributes: true, attributeFilter: ['data-water', 'data-theme']});
// the live voice screen has its own water and covers the page: hold this one still while it is open
const holdWhenVoiceOpen = () => { held = document.body.classList.contains('voice-live-open'); water?.hold(held); modalWater?.hold(held); };
new MutationObserver(holdWhenVoiceOpen).observe(document.body, {attributes: true, attributeFilter: ['class']});
holdWhenVoiceOpen();
start();
// the pop-up opens and closes by its class: give it water while it is open, take it away when it closes
{ const modal = document.getElementById('cz-modal'); if (modal) new MutationObserver(syncModal).observe(modal, {attributes: true, attributeFilter: ['class']}); }
syncModal();
// read-only, for the tests (the same idea as the voice screen's pondState)
window.blam8WaterState = () => (water ? water.snapshot() : null);
window.blam8ModalWaterState = () => (modalWater ? modalWater.snapshot() : null);
