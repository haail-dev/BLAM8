/* Pull down to refresh — every shell, one gesture.
 *
 * The landing page had this first (templates/landing.html, Kris 2026-09-05):
 * at the top of the page a downward drag pulls the opener down with the
 * finger, with resistance; a label says "Pull to refresh", then "Release to
 * refresh"; letting go past the threshold reloads the page. Installed as an
 * app there is no browser chrome and no native pull, so anywhere else a page
 * that had gone stale had no way of being asked again (Kris 2026-09-20: "add
 * the current refresh application wide").
 *
 * Same numbers, same words, same feel as the landing — only what moves
 * differs. There the video stage came down and the bar stayed locked; here
 * the whole shell comes down, bar and all, and the label sits in the gap it
 * opens under the phone's strip.
 *
 * Two ways in (Kris):
 *   1. Pull the page. Only when EVERY scroller under the finger — the page,
 *      the app's main sheet, a card body, a message box — is at its very top.
 *      A page scrolled anywhere just scrolls; scroll it to the top, then pull.
 *   2. Pull the top bar. Works wherever the page is scrolled, so a thread
 *      whose stage owns the drag (pulling down there opens history) still has
 *      a handle for a refresh.
 *
 * Markup it reads:
 *   data-pull-bar            the handle (the app bar, a shell's header)
 *   data-pull-root           what moves (the app shell's wrapper); with no
 *                            root, the body's children move together
 *   data-pull-refresh="off"  a touch that starts inside never pulls: the
 *                            thread's stage, the concierge widget, the menu
 *                            dropdown, the bottom strip, the dock rail — unless
 *                            it starts on a handle INSIDE that zone, which
 *                            pulls (the Concierge modal's own bar)
 *   data-pull-moves="sel"    on a handle: move only that element, not the
 *                            shell (the Concierge's card, over the page)
 *   data-pull-reopen="hash"  on a handle: reload to #hash, so what was open
 *                            opens again (#concierge reopens the Concierge)
 *   data-pull-refresh-own    on <html>: the page draws its own pull (the
 *                            landing) and this script stands down
 *
 * Touch only: a mouse has a refresh button, and only touch fires these events.
 */
(function () {
  'use strict';

  // The landing's numbers: half the finger's travel shows (resistance), and
  // 60px shown — 120px of finger — is the point of no return (80 at first;
  // Kris 2026-09-20: "a little less pull down"). The pull stops right there,
  // the moment the words change (Kris: "lock the pull down distance to just
  // as the text changes, it doesn't need to drag any further").
  var PULL_MAX = 60, PULL_FIRE = 60, RESIST = 0.5;
  // A tap wobbles; a pull is a pull only after this much travel, and only
  // when it is going more down than sideways (the inbox rows swipe, the dock
  // rail scrolls sideways).
  var SLOP = 8;
  // The gap kept open under "Refreshing" while the reload is on its way.
  var HOLD = 48;
  // A reload that never comes (offline, a hung server) gives the page back.
  var STUCK = 10000;

  var doc = document.documentElement;
  if (!document.body || doc.hasAttribute('data-pull-refresh-own')) return;

  var css = document.createElement('style');
  css.textContent =
    // The browser's own bounce and pull are off, as on the landing: this is
    // the only pull there is, and the bar never drags with the viewport.
    'html, body { overscroll-behavior-y: none; }\n' +
    '[data-pull-root] > main { overscroll-behavior-y: none; }\n' +
    '.pull-refresh-moving { will-change: transform; }\n' +
    '.pull-refresh-settling { transition: transform .28s cubic-bezier(.2,.8,.2,1); }\n' +
    '@media (prefers-reduced-motion: reduce) { .pull-refresh-settling { transition: none; } }\n' +
    // The label: the landing's, word for word and in its type, centred in the
    // gap the pull opens under the phone's strip (which paints above it, so
    // nothing ever shows through the strip). Its ink is a variable for a shell
    // whose canvas needs something other than the landing's slate.
    '#pull-refresh-label { position: fixed; left: 0; right: 0; top: env(safe-area-inset-top, 0px); height: 0;' +
    ' display: flex; align-items: center; justify-content: center; z-index: 59; pointer-events: none;' +
    ' font: 600 11px/1 Inter, system-ui, sans-serif; letter-spacing: .08em; text-transform: uppercase;' +
    ' color: var(--pull-refresh-ink, #8493A5); opacity: 0; transition: opacity .15s; }';
  document.head.appendChild(css);

  var label = document.createElement('div');
  label.id = 'pull-refresh-label';
  label.setAttribute('aria-hidden', 'true');
  label.textContent = 'Pull to refresh';
  document.body.appendChild(label);

  function roots() {
    var marked = document.querySelectorAll('[data-pull-root]');
    if (marked.length) return Array.prototype.slice.call(marked);
    return Array.prototype.filter.call(document.body.children, function (el) {
      return el !== label && !/^(SCRIPT|STYLE|LINK|TEMPLATE)$/.test(el.tagName);
    });
  }

  // Every scroller between the finger and the document is at its top. Only a
  // scroll container can have a scrollTop above zero, so no style lookups are
  // needed; the sub-pixel tolerance is iOS's.
  function atTop(el) {
    for (; el && el !== doc; el = el.parentElement) if (el.scrollTop > 1) return false;
    return (window.scrollY || doc.scrollTop || 0) <= 1;
  }

  var moving = null, target = null, fromBar = false, handle = null;
  var y0 = null, x0 = 0, pull = 0, pulling = false, fired = false, settleTimer = null;

  function setPull(px) {
    pull = px;
    var t = px ? 'translate3d(0,' + px + 'px,0)' : '';
    for (var i = 0; i < moving.length; i++) moving[i].style.transform = t;
    label.style.height = px + 'px';
    label.style.opacity = px > 24 ? '1' : '0';
    if (!fired) label.textContent = px >= PULL_FIRE ? 'Release to refresh' : 'Pull to refresh';
  }

  function each(cls, on) {
    for (var i = 0; i < moving.length; i++) moving[i].classList.toggle(cls, on);
  }

  // Ease to px; at 0 the roots are handed back exactly as they were.
  function settle(px) {
    if (!moving) return;
    each('pull-refresh-settling', true);
    setPull(px);
    clearTimeout(settleTimer);
    settleTimer = setTimeout(function () {
      if (!moving) return;
      each('pull-refresh-settling', false);
      if (!px) { each('pull-refresh-moving', false); moving = null; }
    }, 320);
  }

  function fire() {
    // The refresh action (Kris): reload the page. The label holds
    // "Refreshing" in a little of the gap while the reload is on its way. A
    // handle that names a hash reloads to it, so the Concierge comes back
    // open — the reload still picks up a fresh deploy, and the conversation
    // is re-read from the server.
    fired = true;
    label.textContent = 'Refreshing';
    settle(HOLD);
    var reopen = handle && handle.getAttribute('data-pull-reopen');
    if (reopen) {
      try { history.replaceState(history.state, '', location.pathname + location.search + '#' + reopen); } catch (err) {}
    }
    window.location.reload();
    setTimeout(function () {
      fired = false;
      label.textContent = 'Pull to refresh';
      settle(0);
    }, STUCK);
  }

  document.addEventListener('touchstart', function (e) {
    y0 = null;
    if (e.touches.length !== 1 || fired) return;
    var t = e.target && e.target.nodeType === 1 ? e.target : (e.target && e.target.parentElement);
    if (!t) return;
    var bar = t.closest('[data-pull-bar]'), off = t.closest('[data-pull-refresh="off"]');
    // An opted-out zone stays out — except its own handle (the Concierge's bar).
    if (off && !(bar && off.contains(bar))) return;
    fromBar = !!bar;
    handle = bar;
    if (!fromBar && !atTop(t)) return;
    target = t;
    y0 = e.touches[0].clientY;
    x0 = e.touches[0].clientX;
    pulling = false;
  }, { passive: true });

  document.addEventListener('touchmove', function (e) {
    if (y0 === null || e.touches.length !== 1) return;
    var dy = e.touches[0].clientY - y0, dx = e.touches[0].clientX - x0;
    if (!pulling) {
      if (Math.abs(dx) > SLOP && Math.abs(dx) > Math.abs(dy)) { y0 = null; return; }   // sideways: a swipe, a rail
      if (dy <= SLOP) return;
      if (!fromBar && !atTop(target)) { y0 = null; return; }                          // the content scrolled after all
      pulling = true;
      // A handle that names what moves brings only that down (the Concierge's
      // card, over the page); the label rides above it while it does.
      var sel = handle && handle.getAttribute('data-pull-moves');
      var own = sel ? document.querySelector(sel) : null;
      moving = own ? [own] : roots();
      label.style.zIndex = own ? '81' : '';
      each('pull-refresh-settling', false);
      each('pull-refresh-moving', true);
    } else if (!fromBar && !atTop(target)) {
      // Pulled, pushed back up past the start (the sheet scrolled), pulled
      // again: the sheet has the finger now, not the shell.
      y0 = null; pulling = false; settle(0); return;
    }
    setPull(Math.max(0, Math.min(PULL_MAX, Math.round((dy - SLOP) * RESIST))));
  }, { passive: true });

  function endPull() {
    if (y0 === null) return;
    y0 = null;
    if (!pulling) return;
    pulling = false;
    if (pull >= PULL_FIRE) fire(); else settle(0);
  }
  document.addEventListener('touchend', endPull, { passive: true });
  document.addEventListener('touchcancel', endPull, { passive: true });

  // A page restored from the back/forward cache comes back as it was left —
  // never mid-pull.
  window.addEventListener('pageshow', function (e) {
    if (e.persisted) { fired = false; label.textContent = 'Pull to refresh'; settle(0); }
  });
})();
