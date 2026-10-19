// The Concierge page's right-hand column (Kris 2026-10-17): on a computer, on the full /app/concierge page
// only, "Tasks" and "Waiting for you" sit in calm, collapsible boxes beside the chat, like the right column
// in Claude Desktop. It is built here, in JS, so the page and the modal still render one server template.
// It only READS what the panel already holds (the task rows and the waiting list) and asks the panel's own
// code to act: a tap on a waiting row opens the card through the same resurface path as the chips, so Confirm and Cancel
// stay on the card with its own checks. Layout rules apply only at a wide window (CSS media query below).

import { mountStatus, ledgerLine } from './concierge_status.js';

export var WIDE_PX = 1100;          // below this the column is hidden and the page is exactly as before
export var PAGER_PX = WIDE_PX - 1;  // narrower than the side-by-side column (a phone, a squeezed window): it is the right half of a two-page strip
export var TASK_CAP = 8;
export var CARD_TTL_MS = 300000;    // concierge_actions.PROPOSAL_TTL_S: a card is good for five minutes
export var OPEN_CAP = 6;            // cards open on the right at once; the oldest go back into the chat            // the most recent tasks shown
var OPEN_KEY = 'blam8.conciergeSide.';
var ICON = { queued: '○', working: '…', waiting: '⏳', done: '✓', needs_you: '?', failed: '✕', declined: '–' };
var WORD = { queued: 'queued', working: 'working', waiting: 'waiting for you', done: 'done', needs_you: 'needs you',
             failed: "couldn't", declined: 'not done' };

// The two voice screens: voice-gptlive.js (GPT-Live) keeps its water, a canvas.vl-pond with the glow .vl-ambient behind it, in the stage beside the
// orb's column .vl-live; voice-live.js (the first) keeps a canvas.vl-ripples inside .vl-live. Roots and stage are laid out alike.
var VOICE_ROOTS = ['#voice-live-root.is-open', '#voice-gptlive-root.is-open'];
var PHONE_VOICE = 'body.voice-live-open.cz-pager-body ';
// What pans with the column: everything on the voice screen except .vl-ripples, the first screen's water, which stays where it is behind both halves.
// GPT-Live's water is NOT excluded: it is made as wide as the whole canvas (below) and pans with it, so the water runs all the way across.
var VOICE_FOREGROUND = [].concat.apply([], VOICE_ROOTS.map(function (r) {
  return [r + '>:not(.vl-stage)', r + ' .vl-stage>:not(.vl-ripples):not(.vl-live)', r + ' .vl-live>:not(.vl-ripples)'];
}));
// GPT-Live's lake and its glow are laid out two screens wide (the engine sizes the lake from its canvas: water-core.js lakeFromCanvas), so ripples
// run on past the voice screen's right edge into the column's half instead of bouncing back off it. !important: the canvas carries inline sizes.
var VOICE_WATER = ['#voice-gptlive-root.is-open .vl-stage>.vl-pond', '#voice-gptlive-root.is-open .vl-stage>.vl-ambient'];
export var CSS = '@media (min-width:' + WIDE_PX + 'px) and (hover:hover) and (pointer:fine){'
  + '#cz-root.cz-has-side{--cz-w:min(48rem,calc(100vw - 21rem - 2rem));width:var(--cz-w);max-width:none;'
  + 'margin-left:max(1rem,calc((100vw - var(--cz-w) - 21rem)/2));margin-right:0}'
  + '#cz-root.cz-has-side .cz-side{display:flex}'
  + '#cz-root.cz-has-side .cz-waitdot-row{display:none}}'
  + '.cz-side{display:none;position:absolute;top:1rem;bottom:1rem;left:calc(100% + 1rem);width:20rem;flex-direction:column;gap:.75rem;overflow-y:auto;overscroll-behavior:contain}'
  + '.cz-side-box{flex:none;border:1px solid rgb(var(--c-bd-slate-200,226 232 240));border-radius:1rem;background:rgb(var(--c-bg-white,255 255 255));overflow:hidden}'
  + '.cz-side-head{display:flex;align-items:center;gap:.5rem;width:100%;padding:.6rem .8rem;background:none;border:0;text-align:left;cursor:pointer;color:inherit;font:inherit}'
  + '.cz-side-title{flex:0 1 auto;font-size:.8rem;font-weight:600;color:rgb(var(--c-tx-navy,11 18 32))}'
  // What has just been dealt with shows beside the box's name, in green, not as a line inside the card (Kris 2026-10-16).
  + '.cz-side-note{flex:1;min-width:0;margin-left:.5rem;font-size:.7rem;font-weight:600;color:#15803d;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'
  + '.cz-side-item .cz-proposal[data-state="sent"] .cz-proposal-foot{display:none}'
  + '.cz-side-count{display:flex;flex-wrap:wrap;gap:3px;align-content:center;justify-content:flex-start}'
  + '.cz-side-dot{width:7px;height:7px;border-radius:2px;background:rgb(var(--c-tx-brand,37 99 235))}'
  + '.cz-side-more{font-size:.65rem;line-height:7px;color:rgb(var(--c-tx-slate-400,148 163 184));margin-left:2px}'
  + '.cz-side-chev{width:1rem;flex:none;transition:transform .15s;color:rgb(var(--c-tx-slate-400,148 163 184))}'
  + '.cz-side-box[data-open="false"] .cz-side-chev{transform:rotate(-90deg)}'
  + '.cz-side-box[data-open="false"] .cz-side-body{display:none}'
  + '.cz-side-body{border-top:1px solid rgb(var(--c-bd-slate-100,241 245 249));max-height:22rem;overflow-y:auto}'
  + '.cz-side-empty{margin:0;padding:.7rem .8rem;font-size:.75rem;color:rgb(var(--c-tx-slate-400,148 163 184))}'
  + '.cz-side-row{display:flex;gap:.5rem;width:100%;padding:.5rem .8rem;border:0;border-top:1px solid rgb(var(--c-bd-slate-100,241 245 249));background:none;text-align:left;font:inherit;cursor:pointer;color:inherit}'
  + '.cz-side-body>.cz-side-row:nth-of-type(1){border-top:0}.cz-side-row:hover{background:rgb(var(--c-bg-slate-100,241 245 249)/.6)}'
  + '.cz-side-main{flex:1;min-width:0}.cz-side-line{margin:0;font-size:.8rem;color:rgb(var(--c-tx-navy,11 18 32));overflow:hidden;text-overflow:ellipsis;white-space:nowrap}'
  + '.cz-side-meta{margin:0;font-size:.7rem;color:rgb(var(--c-tx-slate-400,148 163 184))}'
  + '.cz-side-box[data-empty="true"]{display:none}'
  + '.cz-side-static{cursor:default}a.cz-side-row{text-decoration:none}.cz-side-line{white-space:normal}'
  + '.cz-side-item{padding:.5rem;border-top:1px solid rgb(var(--c-bd-slate-100,241 245 249))}.cz-side-item:empty{display:none}'
  + '.cz-side-item>*{max-width:100%!important;box-sizing:border-box}.cz-side-item textarea{width:100%;box-sizing:border-box}'
  // The thread keeps no note about a card that is open on the right: it plainly appears there (Kris 2026-10-16).
  + '.cz-side-stub{display:none}'
  // A card in the column sits straight in the box, not in a rounded box of its own inside it (Kris 2026-10-16).
  + '.cz-side-item>.cz-draft{border:0;border-radius:0;background:none;box-shadow:none;padding:0}'
  + '.cz-side-fresh{animation:cz-side-fresh 1.6s ease-out}@keyframes cz-side-fresh{from{background:rgb(var(--c-bg-slate-100,241 245 249))}to{background:transparent}}'
  + '.cz-side-body.cz-side-open{max-height:none}'
  // The live voice screen (a fixed full-window layer on <body>) shares the whole window with the column while it is open
  // (Kris 2026-10-16: the cards go ON TOP of the ripples, to the right, not on a solid strip of their own). The layer AND its
  // stage run the full width, so the water and the gradients are one backdrop behind the cards; what is ON the stage keeps
  // clear of the column: its in-flow content by 22rem of right padding, and the few things placed from the right or across
  // the width (the Debrief button, the pending badges, the top strip, the allowance bar) by the same 22rem. Only when the
  // column is on (body.cz-side-wide, set below) and the window is wide; the voice files are not touched.
  + '@media (min-width:' + WIDE_PX + 'px) and (hover:hover) and (pointer:fine){'
  + 'body.cz-side-wide.voice-live-open #voice-live-root.is-open .vl-stage,body.cz-side-wide.voice-live-open #voice-gptlive-root.is-open .vl-stage{padding-right:22rem}'
  + 'body.cz-side-wide.voice-live-open #voice-gptlive-root.is-open .vl-stage>.vl-top-strip,body.cz-side-wide.voice-live-open #voice-live-root.is-open .vl-toasts{right:22rem}'
  + 'body.cz-side-wide.voice-live-open #voice-gptlive-root.is-open .vl-allowance{left:calc(50% - 11rem)}'
  + 'body.cz-side-wide.voice-live-open #voice-live-root.is-open .cz-debrief,body.cz-side-wide.voice-live-open #voice-gptlive-root.is-open .cz-debrief{right:calc(22rem + .8rem)}'
  + 'body.cz-side-wide.voice-live-open #voice-live-root.is-open .cz-badges,body.cz-side-wide.voice-live-open #voice-gptlive-root.is-open .cz-badges{right:calc(22rem + .8rem)}'
  + 'body.cz-side-wide.voice-live-open #voice-live-root.has-debrief .cz-badges,body.cz-side-wide.voice-live-open #voice-gptlive-root.has-debrief .cz-badges{right:calc(22rem + .8rem + 2.75rem + .5rem)}'
  + 'body.cz-side-wide.voice-live-open #cz-root.cz-has-side .cz-side{position:fixed;top:1rem;bottom:1rem;right:1rem;left:auto;z-index:91}}'
  // A waiting item that has been tapped takes the whole Waiting box (Kris 2026-10-16): the list steps aside, the card fills the box, and
  // "Back" puts it away again. The card keeps its own Confirm and Cancel.
  + '.cz-side-box[data-focus="true"] .cz-side-list{display:none}'
  + '.cz-side-box[data-focus="true"] .cz-side-body{max-height:none;overflow:visible}'
  + '.cz-side-focus:empty{display:none}'
  // Minimal rows (Kris 2026-10-16): a row is its title; what else there is to say opens under it when the row is tapped.
  + '.cz-side-xrow{border-top:1px solid rgb(var(--c-bd-slate-100,241 245 249))}.cz-side-xrow>.cz-side-row{border-top:0}'
  + '.cz-side-xmore{padding:0 .8rem .55rem 2.2rem}.cz-side-xmore[hidden]{display:none}'
  + '.cz-side-hint{margin:0 0 .4rem;font-size:.7rem;color:rgb(var(--c-tx-slate-500,100 116 139))}'
  + '.cz-side-back{display:block;width:100%;margin:0 0 .35rem;padding:0;border:0;background:none;text-align:left;font:inherit;font-size:.7rem;font-weight:600;cursor:pointer;color:rgb(var(--c-tx-brand,37 99 235))}'
  // On a phone the column is the right half of one strip twice the phone's width (Kris 2026-10-16): swipe, or open an item from the chat; the strip
  // snaps to a half and never rests between. concierge.html's #cz-pager is the strip; it only scrolls sideways.
  + '.cz-pager.cz-pager-on{display:flex;width:100%;height:100%;overflow-x:auto;overflow-y:hidden;scroll-snap-type:x mandatory;overscroll-behavior-x:contain;-webkit-overflow-scrolling:touch;scrollbar-width:none}'
  + '.cz-pager.cz-pager-on::-webkit-scrollbar{display:none}'
  + '.cz-pager.cz-pager-on>#cz-root{flex:0 0 100%;width:100%;max-width:none;margin:0;min-width:0;scroll-snap-align:start;scroll-snap-stop:always}'
  + '.cz-pager.cz-pager-on>.cz-side{display:flex;position:static;flex:0 0 100%;width:100%;min-width:0;height:100%;box-sizing:border-box;padding:1rem 1rem max(1rem,env(safe-area-inset-bottom));scroll-snap-align:start;scroll-snap-stop:always}'
  // The live voice screen is a fixed layer over the whole page, so the strip under it cannot be swiped to while it is up. On a phone the view
  // therefore pans (see the canvas rules below): the column comes in from the right while the voice screen's foreground slides off to the left.
  // The column has no backdrop of its own anywhere: it sits over the voice screen's water.
  + 'body.voice-live-open .cz-pager.cz-pager-on>.cz-side{position:fixed;top:0;right:0;bottom:0;left:auto;width:100%;height:auto;z-index:93;transform:translateX(100%);visibility:hidden;transition:transform .25s ease,visibility 0s .25s}'
  + 'body.voice-live-open.cz-voice-side .cz-pager.cz-pager-on>.cz-side{transform:none;visibility:visible;transition:transform .25s ease}'
  // The pop-up is a fixed layer of its own (z-index 80), so a column left inside it sits UNDER the voice screen (z-index 90) however high its own
  // z-index: the swipe worked and the panel opened out of sight (Kris 2026-10-17). While the voice screen is up the column is moved onto the page body.
  //
  // One canvas twice the phone's width (Kris 2026-10-17): the voice screen (orb, words, controls) is its left half and the column its right half, and the
  // water runs under both. --cz-pan on the body is how far across it is (0 the voice screen, 1 the column; --cz-w is the screen's width). Everything that
  // moves, the water included, moves by the same number, in step, with the individual `translate` property so an element's own transform and transitions
  // (the orb's) are untouched. The first voice screen's water stays where it is instead (see VOICE_FOREGROUND).
  // concierge_sidebar.js sets the number: a finger sets it while dragging and a release glides it to 0 or 1. Phones only (.cz-pager-body).
  + 'body>.cz-side.cz-side-over{display:flex;position:fixed;top:0;right:0;bottom:0;left:auto;width:100%;box-sizing:border-box;padding:1rem 1rem max(1rem,env(safe-area-inset-bottom));z-index:93;background:none;touch-action:pan-y;translate:calc((1 - var(--cz-pan,0)) * var(--cz-w,100vw)) 0;visibility:hidden}'
  + 'body.cz-voice-side>.cz-side.cz-side-over,body.cz-panning>.cz-side.cz-side-over{visibility:visible}'
  + VOICE_FOREGROUND.map(function (sel) { return PHONE_VOICE + sel; }).join(',') + '{translate:calc(var(--cz-pan,0) * -1 * var(--cz-w,100vw)) 0}'
  + VOICE_ROOTS.map(function (r) { return PHONE_VOICE + r; }).join(',') + '{touch-action:pan-y}'
  + VOICE_WATER.map(function (sel) { return PHONE_VOICE + sel; }).join(',') + '{width:calc(2 * var(--cz-w,100vw)) !important;max-width:none !important}'
  + '.cz-side-over>.cz-side-home{display:block;flex:none;align-self:flex-start;padding:0;border:0;background:none;cursor:pointer;font:inherit;font-size:.75rem;font-weight:600;color:rgb(var(--c-tx-brand,37 99 235))}'
  + '.cz-side-home{display:none}'
  + '.cz-pager-on>.cz-side>.cz-side-home{display:block;flex:none;align-self:flex-start;padding:0;border:0;background:none;cursor:pointer;font:inherit;font-size:.75rem;font-weight:600;color:rgb(var(--c-tx-brand,37 99 235))}';

export function ageOf(then, now) {
  if (!then || isNaN(then)) return '';
  var s = Math.max(0, Math.round(((now || Date.now()) - then) / 1000));
  if (s < 60) return 'just now';
  if (s < 3600) return Math.floor(s / 60) + ' min ago';
  if (s < 86400) return Math.floor(s / 3600) + ' h ago';
  return Math.floor(s / 86400) + ' d ago';
}

export function waitingState(w) {
  if (w.type === 'recovery') return 'result unverified';
  if (w.type === 'draft') return 'draft waiting';
  if (w.type === 'link') return 'draft for review';
  return w.status === 'superseded' ? 'parked' : 'waiting';
}
var KIND_WORD = { recovery: 'Unverified result', draft: 'Email draft', link: 'Draft' };

function readOpen(name, dflt) { try { var v = localStorage.getItem(OPEN_KEY + name); return v == null ? dflt !== false : v !== 'closed'; } catch (e) { return dflt !== false; } }
function saveOpen(name, open) { try { localStorage.setItem(OPEN_KEY + name, open ? 'open' : 'closed'); } catch (e) {} }

// The count is a few small squares, not a number: DOTS_PER_ROW to a row, then they stack, up to DOTS_MAX
// (past that "+N"). The exact count stays in the accessible label.
export var DOTS_PER_ROW = 5;
export var DOTS_MAX = 15;
export function dots(node, n, noun, make) {
  node.textContent = '';
  node.setAttribute('role', 'img');
  node.setAttribute('aria-label', n + ' ' + noun + (n === 1 ? '' : 's'));
  node.style.width = (Math.min(Math.max(n, 1), DOTS_PER_ROW) * 10 - 3) + 'px';
  for (var i = 0; i < Math.min(n, DOTS_MAX); i++) node.appendChild(make('span', 'cz-side-dot'));
  if (n > DOTS_MAX) node.appendChild(make('span', 'cz-side-more', '+' + (n - DOTS_MAX)));
}

// api = { tasks(): [{id,title,status,at}], waiting(): [item], review(item, inColumn), released(node), focusTask(id), onChange(fn) }
export function mountSidebar(root, api, doc) {
  doc = doc || document;
  var seen = {};
  function el(tag, cls, text) { var n = doc.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; }
  var style = el('style'); style.textContent = CSS; doc.head.appendChild(style);
  var col = el('aside', 'cz-side'); col.setAttribute('aria-label', 'Open items, tasks and items waiting for you');
  function box(name, title, opts) {
    opts = opts || {};
    var b = el('section', 'cz-side-box'), head = el('button', 'cz-side-head'), body = el('div', 'cz-side-body');
    head.type = 'button';
    var count = el('span', 'cz-side-count'), chev = el('span', 'cz-side-chev', '▾');
    var note = el('span', 'cz-side-note');
    head.appendChild(el('span', 'cz-side-title', title)); head.appendChild(note); head.appendChild(count); head.appendChild(chev);
    function paintOpen(open) { b.setAttribute('data-open', open ? 'true' : 'false'); head.setAttribute('aria-expanded', open ? 'true' : 'false'); }
    head.addEventListener('click', function () {
      var open = b.getAttribute('data-open') !== 'true'; paintOpen(open); saveOpen(name, open);
      if (open && opts.onOpen) opts.onOpen();
    });
    paintOpen(readOpen(name, !opts.closed));
    b.appendChild(head); b.appendChild(body);
    return { node: b, body: body, count: count, note: note, open: function () { paintOpen(true); saveOpen(name, true); } };
  }
  var openBox = box('open', 'Open now'), tasksBox = box('tasks', 'Tasks'), waitBox = box('waiting', 'Waiting for you');
  openBox.body.className += ' cz-side-open'; openBox.node.setAttribute('data-empty', 'true');
  var waitList = el('div', 'cz-side-list'), waitFocus = el('div', 'cz-side-focus');
  waitBox.body.appendChild(waitList); waitBox.body.appendChild(waitFocus);
  tasksBox.node.setAttribute('data-empty', 'true');
  var status = mountStatus({ el: el, box: box, row: row, expand: expandRow, dots: dots, api: api, repaint: function () { paint(); },
    fetchJson: function (url) { return fetch(url, { credentials: 'same-origin' }).then(function (r) { if (!r.ok) throw new Error('http ' + r.status); return r.json(); }); } });
  var home = el('button', 'cz-side-home', '\u2039 Concierge'); home.type = 'button'; col.appendChild(home);
  [openBox, status.boxes[0], waitBox, tasksBox, status.boxes[1], status.boxes[2], status.boxes[3], status.boxes[4], status.boxes[5]].forEach(function (b) { col.appendChild(b.node); });
  root.appendChild(col); root.classList.add('cz-has-side');
  // The phone strip: concierge.html's #cz-pager holds the panel and, on a phone, the column. Neither exists in the unit tests' fake DOM.
  var view = doc.defaultView || (typeof window !== 'undefined' ? window : null);
  var mq = view && view.matchMedia ? view.matchMedia('(min-width:' + WIDE_PX + 'px) and (hover:hover) and (pointer:fine)') : null;
  var pager = (doc.getElementById && doc.getElementById('cz-pager')) || null;
  var pmq = pager && view && view.matchMedia ? view.matchMedia('(max-width:' + PAGER_PX + 'px)') : null;
  var pagerOn = false, revealed = false, loaded = false;
  // The column is on screen: wide window, or the strip's right half has been shown. Its reads wait until then.
  function visible() { return pagerOn ? revealed : (!mq || mq.matches); }
  function ensureLoaded() { if (loaded || !visible()) return; loaded = true; status.load(); }
  function calm() { return !!(view && view.matchMedia && view.matchMedia('(prefers-reduced-motion: reduce)').matches); }
  function slideTo(left) {
    if (!pager) return;
    var from = pager.scrollLeft;
    if (pager.scrollTo) pager.scrollTo({ left: left, behavior: calm() ? 'auto' : 'smooth' }); else pager.scrollLeft = left;
    // A smooth scroll that never starts (some browsers drop it while snapping is on) would leave the tap doing nothing: if it has not moved, go there.
    setTimeout(function () { if (pager.scrollLeft === from && Math.abs(from - left) > 2) pager.scrollLeft = left; }, 350);
  }
  // Slide to the column (a tap on a link, a card opened there, or the Waiting chip); the strip then rests on that half.
  function reveal() {
    if (!pagerOn || !pager) return;
    revealed = true; ensureLoaded();
    if (voiceUp()) { floatSide(true); sideOverVoice(true); return; }   // over the voice screen the column is a panel on top
    slideTo(Math.max(0, pager.scrollWidth - pager.clientWidth));
  }
  function setPager(on) {
    if (!pager || on === pagerOn) return;
    pagerOn = on;
    pager.classList.toggle('cz-pager-on', on);
    if (body && body.classList && body.classList.toggle) body.classList.toggle('cz-pager-body', on);
    if (!on) sideOverVoice(false, true);
    floatSide(false);
    (on ? pager : root).appendChild(col);
    if (on && voiceUp()) floatSide(true);
    if (!on) revealed = false;
    ensureLoaded();
  }
  // Over the live voice screen the column is a panel on top, not the strip's second half.
  var body = doc.body || null;
  function voiceUp() { return !!(body && body.classList && body.classList.contains && body.classList.contains('voice-live-open')); }
  // --cz-pan: 0 the voice screen, 1 the column. A finger sets it while dragging; otherwise it glides to 0 or 1. All of it is driven from here, in one
  // number, so the foreground and the column can never drift apart (CSS transitions would be one clock per element, and would replace the orb's own).
  var panNow = 0, panRaf = 0;
  function paintPan(p) {
    panNow = p;
    if (body && body.style && body.style.setProperty) {
      body.style.setProperty('--cz-pan', String(p));
      body.style.setProperty('--cz-w', width() + 'px');
    }
  }
  function stopGlide() { if (panRaf && view && view.cancelAnimationFrame) view.cancelAnimationFrame(panRaf); panRaf = 0; }
  function setPan(p, dragging) {
    if (!body) return;
    stopGlide();
    if (body.classList && body.classList.toggle) body.classList.toggle('cz-panning', !!dragging);
    paintPan(p);
  }
  function glide(to) {
    stopGlide();
    var from = panNow, raf = view && view.requestAnimationFrame && view.requestAnimationFrame.bind(view);
    if (!raf || calm() || Math.abs(to - from) < 0.002) { setPan(to, false); return; }
    var t0 = null, ms = 300 * Math.max(0.4, Math.abs(to - from));
    if (body.classList && body.classList.toggle) body.classList.toggle('cz-panning', true);
    function frame(t) {
      if (t0 === null) t0 = t;
      var k = Math.min(1, (t - t0) / ms), e = 1 - Math.pow(1 - k, 3);   // ease out
      paintPan(from + (to - from) * e);
      if (k < 1) panRaf = raf(frame); else { panRaf = 0; setPan(to, false); }
    }
    panRaf = raf(frame);
  }
  function sideOpen() { return !!(body && body.classList && body.classList.contains && body.classList.contains('cz-voice-side')); }
  function sideOverVoice(on, instant) {
    if (body && body.classList && body.classList.toggle) body.classList.toggle('cz-voice-side', !!on);
    if (instant) setPan(on ? 1 : 0, false); else glide(on ? 1 : 0);
  }
  // Over the voice screen the column lives on the body, outside any fixed layer (the pop-up) that would stack it beneath; it goes home with the voice screen.
  function floatSide(on) {
    if (!body || !body.appendChild) return;
    if (on) {
      if (!pagerOn || /(^| )cz-side-over( |$)/.test(col.className)) return;
      col.className += ' cz-side-over'; body.appendChild(col);
    } else if (/(^| )cz-side-over( |$)/.test(col.className)) {
      col.className = col.className.replace(/(^| )cz-side-over( |$)/g, ' ').trim();
      (pagerOn && pager ? pager : root).appendChild(col);
    }
  }
  home.addEventListener('click', function () { if (voiceUp()) sideOverVoice(false); else slideTo(0); });
  // The voice screen and the column are one canvas twice the phone's width: a finger drags it, it follows, and on release it settles on whichever half
  // is nearer (or the way it was flicked). Mostly-vertical movement is a scroll and is left alone; a tap or a press never gets this far.
  var drag = null;
  function now() { return Date.now(); }
  function width() { return (view && view.innerWidth) || (body && body.clientWidth) || 390; }
  function touchOf(e, list) { return e && e[list] && e[list][0] || null; }
  function settle(e, cancelled) {
    var d = drag; drag = null;
    if (!d) return;
    var t = touchOf(e, 'changedTouches');
    var dx = t ? t.clientX - d.x : 0, dy = t ? t.clientY - d.y : 0, target;
    if (d.locked && !cancelled) {
      var v = dx / Math.max(1, now() - d.t);                                 // px per ms: negative is leftwards
      target = d.from === 0 ? ((d.p > 0.3 || v < -0.45) ? 1 : 0) : ((d.p < 0.7 || v > 0.45) ? 0 : 1);
    } else if (d.locked) {
      target = d.from;                                                       // the browser took the touch: back to where it was
    } else if (!cancelled && t && Math.abs(dx) >= 70 && Math.abs(dx) >= Math.abs(dy) * 1.5) {
      target = dx < 0 ? 1 : 0;                                               // a flick that reported no moves in between
    } else return;
    if (target === 1) reveal(); else sideOverVoice(false);
  }
  if (doc.addEventListener) {
    doc.addEventListener('touchstart', function (e) {
      var t = touchOf(e, 'touches');
      drag = t && pagerOn && voiceUp() && (!e.touches || e.touches.length === 1)
        ? { x: t.clientX, y: t.clientY, t: now(), from: sideOpen() ? 1 : 0, locked: false, p: null } : null;
    }, { passive: true });
    doc.addEventListener('touchmove', function (e) {
      var t = drag && touchOf(e, 'touches');
      if (!t) return;
      var dx = t.clientX - drag.x, dy = t.clientY - drag.y;
      if (!drag.locked) {
        if (Math.abs(dx) < 12 && Math.abs(dy) < 12) return;
        if (Math.abs(dx) < Math.abs(dy) * 1.2) { drag = null; return; }      // mostly vertical: a scroll, not ours
        drag.locked = true;
        floatSide(true); revealed = true; ensureLoaded();                    // the column's reads start as it comes into view
      }
      drag.p = Math.min(1, Math.max(0, drag.from - dx / width()));
      setPan(drag.p, true);
    }, { passive: true });
    doc.addEventListener('touchend', function (e) { settle(e, false); }, { passive: true });
    doc.addEventListener('touchcancel', function (e) { settle(e, true); }, { passive: true });
  }
  // The panel does not outlast the voice screen it slid over.
  if (typeof MutationObserver !== 'undefined' && body && body.nodeType) {
    new MutationObserver(function () { if (voiceUp()) floatSide(true); else { sideOverVoice(false, true); floatSide(false); } }).observe(body, { attributes: true, attributeFilter: ['class'] });
  }
  if (pager) {
    pager.addEventListener('scroll', function () { if (pagerOn && pager.scrollLeft > 8 && !revealed) { revealed = true; ensureLoaded(); } }, { passive: true });
    if (pmq) { setPager(pmq.matches); if (pmq.addEventListener) pmq.addEventListener('change', function () { setPager(pmq.matches); paint(); }); }
  }

  // A row is a button when it acts, a link when it opens a page, and plain text otherwise.
  function row(icon, line, meta, onClick, reviewLabel, opts) {
    opts = opts || {};
    var r = el(opts.href ? 'a' : onClick ? 'button' : 'div', onClick || opts.href ? 'cz-side-row' : 'cz-side-row cz-side-static');
    if (onClick) r.type = 'button';
    if (opts.href) r.setAttribute('href', opts.href);
    if (icon) r.appendChild(el('span', 'cz-side-icon', icon));
    var main = el('div', 'cz-side-main'); main.appendChild(el('p', 'cz-side-line', line)); if (meta) main.appendChild(el('p', 'cz-side-meta', meta));
    if (opts.detail) main.appendChild(el('p', 'cz-side-meta', opts.detail));
    r.appendChild(main);
    if (reviewLabel) r.appendChild(el('span', 'cz-side-review', reviewLabel));
    if (onClick) r.addEventListener('click', onClick);
    return r;
  }
  // A row that is only its title until it is tapped; the lines under it open and close with the tap. With nothing more to say it is a plain row.
  function expandRow(icon, line, lines) {
    var more = (lines || []).filter(Boolean);
    if (!more.length) return row(icon, line, '', null);
    var wrap = el('div', 'cz-side-xrow'), under = el('div', 'cz-side-xmore');
    under.hidden = true;
    more.forEach(function (t) { under.appendChild(el('p', 'cz-side-meta', t)); });
    var head = row(icon, line, '', function () {
      under.hidden = !under.hidden; head.setAttribute('aria-expanded', String(!under.hidden));
    });
    head.setAttribute('aria-expanded', 'false');
    wrap.appendChild(head); wrap.appendChild(under);
    return wrap;
  }
  // "Open now": the panel hands over the card it built (the same node, so Confirm and Cancel keep their own
  // checks) plus the one-line note it left in the thread. A narrow screen presents the same cards on its right-hand page.
  var items = [];
  function toChat(it) {
    if (it.stub && it.stub.parentNode) { it.stub.parentNode.insertBefore(it.node, it.stub.nextSibling); it.stub.hidden = true; }
    it.wrap.remove();
  }
  function shownOpen() { return items.filter(function (it) { return !it.focus; }); }
  // Back: the card in focus goes back where the thread kept it, and the list returns. It stays waiting.
  function release(it) {
    var at = items.indexOf(it); if (at > -1) items.splice(at, 1);
    toChat(it); paintOpen();
    if (api.released) api.released(it.node);
  }
  function paintOpen() {
    items = items.filter(function (it) {
      if (it.node.parentNode === it.wrap) return true;
      it.wrap.remove(); if (it.stub) it.stub.textContent = 'That one has been closed.';
      return false;
    });
    var open = shownOpen(), focused = items.filter(function (it) { return it.focus; });
    dots(openBox.count, open.length, 'open item', el);
    // the state of a card just dealt with ("Follow-up · done ✓") shows beside the box's name until the card fades away
    function said(list) {
      var line = '';
      list.forEach(function (it) {
        if (!it.node.getAttribute || it.node.getAttribute('data-state') !== 'sent' || !it.node.querySelector) return;
        var lab = it.node.querySelector('.cz-draft-label');
        if (lab && lab.textContent) line = lab.textContent;
      });
      return line;
    }
    openBox.note.textContent = said(open);
    waitBox.note.textContent = said(focused);
    openBox.node.setAttribute('data-empty', open.length ? 'false' : 'true');
    waitBox.node.setAttribute('data-focus', focused.length ? 'true' : 'false');
  }
  function markBody(on) { if (doc.body && doc.body.classList) doc.body.classList.toggle('cz-side-wide', !!on); }
  function relayout() {
    var wide = !mq || mq.matches;
    markBody(wide);
    var on = wide || pagerOn;   // cards held by the column stay in it on a phone as well
    items.forEach(function (it) {
      if (on) { if (it.stub) it.stub.hidden = false; it.wrap.appendChild(it.node); (it.focus ? waitFocus : openBox.body).appendChild(it.wrap); }
      else toChat(it);
    });
    paintOpen(); ensureLoaded();
  }
  if (mq && mq.addEventListener) mq.addEventListener('change', relayout);
  if (typeof MutationObserver !== 'undefined') {
    var watch = new MutationObserver(function () { paintOpen(); });
    watch.observe(openBox.body, { childList: true, subtree: true }); watch.observe(waitFocus, { childList: true, subtree: true });
  }
  markBody(!mq || mq.matches);
  var host = {
    active: function () { return pagerOn || !mq || mq.matches; },
    // A column is there to hold a card the owner tapped open: a wide window, or the phone strip.
    focusable: function () { return pagerOn || !mq || mq.matches; },
    reveal: reveal,
    revealWaiting: function () {
      waitBox.open();
      reveal();
      if (waitBox.node.scrollIntoView) waitBox.node.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    },
    // opts.focus: a waiting item that was tapped takes the Waiting box instead of joining "Open now". The stub may be
    // null (the voice screen keeps no thread note); showing the same card again does nothing.
    show: function (node, stub, note, opts) {
      if (items.some(function (it) { return it.node === node; })) return;
      var focus = !!(opts && opts.focus);
      var id = node.getAttribute && node.getAttribute('data-proposal-id');
      if (id) items.filter(function (it) { return it.node.getAttribute('data-proposal-id') === id; }).forEach(function (it) {
        it.wrap.remove(); it.node.remove(); if (it.stub) it.stub.textContent = 'Replaced by the newer copy on the right.'; items.splice(items.indexOf(it), 1);
      });
      if (focus) items.filter(function (it) { return it.focus; }).forEach(function (it) { items.splice(items.indexOf(it), 1); toChat(it); });   // one card in focus at a time
      var wrap = el('div', 'cz-side-item cz-side-fresh'), it = { node: node, stub: stub, wrap: wrap, focus: focus };
      if (focus) {
        var back = el('button', 'cz-side-back', '‹ Back to the list'); back.type = 'button';
        back.addEventListener('click', function () { release(it); });
        wrap.appendChild(back);
        if (opts && opts.hint) wrap.appendChild(el('p', 'cz-side-hint', opts.hint));
      }
      wrap.appendChild(node);
      (focus ? waitFocus : openBox.body).appendChild(wrap);
      items.push(it);
      while (shownOpen().length > OPEN_CAP) { var oldest = shownOpen()[0]; items.splice(items.indexOf(oldest), 1); toChat(oldest); }
      (focus ? waitBox : openBox).open(); paintOpen();
      if (wrap.scrollIntoView) wrap.scrollIntoView({ block: 'nearest' });
      reveal();
    }
  };
  if (api.host) api.host(host);
  function paint() {
    paintOpen();
    var now = Date.now();
    var tasks = (api.tasks() || []).map(function (t) {
      seen[t.id] = seen[t.id] || now;
      return { t: t, at: t.at || seen[t.id] };
    }).sort(function (a, b) { return b.at - a.at; }).slice(0, TASK_CAP);
    tasksBox.body.textContent = ''; dots(tasksBox.count, tasks.length, 'task', el);
    tasksBox.node.setAttribute('data-empty', tasks.length ? 'false' : 'true');   // nothing running: the box is not there (Kris 2026-10-16)
    tasks.forEach(function (x) {
      tasksBox.body.appendChild(row(ICON[x.t.status] || '○', x.t.title || 'Task', (WORD[x.t.status] || x.t.status) + ' · ' + ageOf(x.at, now),
        function () { api.focusTask(x.t.id); }));
    });
    // A card that is open on the right is not also waiting (Kris 2026-10-16): it waits only once it has been left alone.
    var openIds = {};
    items.forEach(function (it) { var id = it.node.getAttribute && it.node.getAttribute('data-proposal-id'); if (id) openIds[id] = 1; });
    var waiting = (api.waiting() || []).filter(function (w) { return !openIds[w.proposal_id || w.log_id]; });
    waitList.textContent = ''; dots(waitBox.count, waiting.length + status.extras(waiting, openIds).length, 'waiting', el);
    var extra = status.extras(waiting, openIds);
    if (!waiting.length && !extra.length) waitList.appendChild(el('p', 'cz-side-empty', 'Nothing waiting'));
    waiting.forEach(function (w) {
      var when = w.created_at ? Date.parse(w.created_at) : NaN, known = status.enrich(w);
      // A prepared card is good for five minutes (the server's PROPOSAL_TTL_S). A delegated one older than that,
      // or one the ledger already calls expired, cannot be brought back by a tap: say so instead of making it tappable.
      var lapsed = (w.delegated && !isNaN(when) && now - when > CARD_TTL_MS) || (known && known.state === 'Expired');
      var why = (api.refused && api.refused() || {})[w.proposal_id];
      if (lapsed || why) {
        waitList.appendChild(expandRow('?', w.action_summary || w.summary || 'A change',
          [(KIND_WORD[w.type] || String(w.kind || 'Change').replace(/_/g, ' ')) + ' · ' + (why || 'expired, ask me to prepare it again') + (ageOf(when, now) ? ' · ' + ageOf(when, now) : '')]));
        return;
      }
      // Only its title (Kris 2026-10-16): the tap opens the card, which says the rest.
      waitList.appendChild(row('⏳', w.action_summary || w.summary || 'A change', '', function () { api.review(w, true); }, null, {}));
    });
    extra.forEach(function (it) {   // needs you, but not on the chat's waiting list (expired, unverified)
      var l = ledgerLine(it);
      waitList.appendChild(expandRow('?', l.line, [l.meta, l.detail]));
    });
    status.paint();
  }
  api.onChange(function () { paint(); if (visible()) status.schedule(); });
  paint();
  ensureLoaded();
  return { paint: paint, node: col, host: host, relayout: relayout, openItems: function () { return items.length; } };
}
