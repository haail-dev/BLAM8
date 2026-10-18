/* Say something is happening the moment it starts happening.
 *
 * These pages are server-rendered: a tap on a link or a form's submit means a
 * round trip and a fresh page, and until that page paints the app looks
 * exactly as it did before the tap. In a browser the tab's own spinner covers
 * that gap — but installed as a PWA there IS no browser chrome, so a second of
 * work reads as a dead button (Kris 2026-09-01: "it still takes a second to
 * load stuff sometimes... when a button is clicked I want it to show a
 * spinning loading symbol").
 *
 * So the control that was clicked wears the wait itself: a faint tint of its
 * own colour and a spinner in the middle of it. Whichever thing you touched is
 * the thing that says it heard you.
 *
 * The spinner is never dismissed by this script on the way OUT — the page it
 * belongs to is about to be replaced, and clearing it early would flash the
 * control back to normal just before it disappears. It is cleared on the way
 * IN instead (pageshow), which is what a Back out of the browser's
 * back/forward cache fires — otherwise a restored page would come back frozen
 * mid-spin, forever.
 *
 * The bottom strip is the one exception to "the whole control" (Kris
 * 2026-09-20: "the buttons at the bottom row turn into a spinning loading
 * logo when clicked — only the clicked icon"). A door there marks its icon's
 * holder with data-nav-icon, and the spinner takes the icon's place instead
 * of washing over the door: the label stays, the other doors stay.
 */
(function () {
  'use strict';

  // Long enough that a navigation which was already instant never flashes a
  // spinner at you; short enough to read as a response to your own tap.
  var DELAY = 120;
  // Nothing in this app legitimately takes this long. If a navigation was
  // cancelled (Stop, a download, a 204) the spinner would otherwise outlive
  // its reason and sit there spinning at a page that has finished.
  var GIVE_UP = 20000;

  var timer = null, giveUp = null, busy = null, host = null;

  function clear() {
    if (timer) { clearTimeout(timer); timer = null; }
    if (giveUp) { clearTimeout(giveUp); giveUp = null; }
    disarm();
    if (busy) {
      var spin = host.querySelector(':scope > .nav-spin');
      if (spin) spin.remove();
      host.classList.remove('nav-icon-busy');
      busy.classList.remove('nav-busy');
      busy.removeAttribute('aria-busy');
      if (host.dataset.navPos) { host.style.position = ''; delete host.dataset.navPos; }
      busy = null; host = null;
    }
  }

  function show(el, where, lock) {
    busy = el;
    host = where;
    // The overlay is absolutely positioned, so it costs the control no
    // layout at all — nothing shifts under the thumb that just tapped it.
    if (getComputedStyle(host).position === 'static') {
      host.style.position = 'relative';
      host.dataset.navPos = '1';
    }
    var spin = document.createElement('span');
    spin.className = 'nav-spin';
    spin.setAttribute('aria-hidden', 'true');
    host.appendChild(spin);
    if (host !== el) host.classList.add('nav-icon-busy');   // the icon steps aside
    if (lock) el.classList.add('nav-busy');                  // one tap is the whole request
    el.setAttribute('aria-busy', 'true');
    giveUp = setTimeout(clear, GIVE_UP);
  }

  // A bottom-strip door: a link whose icon has a holder (base.html).
  function door(target) {
    var a = target && target.closest ? target.closest('a[href]') : null;
    return a && a.querySelector(':scope > [data-nav-icon]') ? a : null;
  }

  function start(el, lock) {
    if (!el || el.hasAttribute('data-busy')) return;   // something else owns this wait
    clear();
    // Where the spinner lands: the icon's holder when the control has one
    // (the bottom strip), else the control itself. A door swaps at once —
    // it always leaves the page, so there is no instant navigation to spare
    // a flash; everything else waits out the delay.
    var where = el.querySelector(':scope > [data-nav-icon]') || el;
    if (where !== el) { show(el, where, lock !== false); return; }
    timer = setTimeout(function () { timer = null; show(el, el, true); }, DELAY);
  }

  function navigates(a) {
    if (a.hasAttribute('download')) return false;
    if (a.target && a.target !== '_self') return false;
    var href = a.getAttribute('href');
    if (!href || href.charAt(0) === '#') return false;
    if (/^(mailto|tel|sms|javascript|blob|data):/i.test(href)) return false;
    var url;
    try { url = new URL(a.href, location.href); } catch (e) { return false; }
    // Same page, different fragment: the browser scrolls, it does not load.
    if (url.hash && url.href.split('#')[0] === location.href.split('#')[0]) return false;
    return true;
  }

  // Swap first, leave second (Kris 2026-09-20, live: "the icon doesn't turn
  // into the loading symbol"). A change made inside a link's click and the
  // navigation that click starts share one task, and iOS paints nothing of
  // the old page once the navigation has begun — so a swap made in the click
  // was real and never seen. A door therefore swaps as the finger LANDS
  // (pointerdown), stays tappable meanwhile (a door locked against pointer
  // events would hand the click to the bar under it), and its click holds
  // the navigation back for two frames so the swap is on screen before the
  // browser starts leaving.
  var armed = null;          // a press that never becomes a tap puts the icon back

  function disarm() { if (armed) { clearTimeout(armed); armed = null; } }

  function pressed() {       // a door swapped by a finger and not yet leaving
    return busy && door(busy) && !busy.classList.contains('nav-busy');
  }

  document.addEventListener('pointerdown', function (e) {
    if (e.defaultPrevented || e.button !== 0) return;
    var a = door(e.target);
    if (a && navigates(a) && busy !== a) start(a, false);
  });
  document.addEventListener('pointerup', function () {
    if (!pressed()) return;
    disarm();
    armed = setTimeout(function () { armed = null; if (pressed()) clear(); }, 400);
  });
  ['pointercancel', 'contextmenu'].forEach(function (t) {
    document.addEventListener(t, function () { if (pressed()) clear(); });
  });

  function leave(href) {
    var gone = false;
    function go() { if (gone) return; gone = true; location.assign(href); }
    // Two frames: the first paints the swap, the second makes sure of it —
    // and a frame that never comes must not hold the door.
    requestAnimationFrame(function () { requestAnimationFrame(go); });
    setTimeout(go, 100);
  }

  document.addEventListener('click', function (e) {
    // defaultPrevented covers every handler that already decided this click
    // means something else — the swipe that swallows its own click, a menu
    // toggle, a modal opener. Modified clicks open a new tab; this one stays.
    if (e.defaultPrevented || e.button !== 0) return;
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var a = e.target.closest ? e.target.closest('a[href]') : null;
    if (!a || !navigates(a)) return;
    if (door(a)) {
      e.preventDefault();
      disarm();
      if (busy !== a) start(a, true);          // a keyboard tap has no finger
      else a.classList.add('nav-busy');        // the finger's swap, now leaving
      leave(a.href);
      return;
    }
    start(a);
  });

  document.addEventListener('submit', function (e) {
    if (e.defaultPrevented) return;
    var form = e.target;
    if (form.target && form.target !== '_self') return;
    // The button carries the wait when there is one — that is what was
    // pressed. A form submitted by the Enter key has none, so the form does.
    start(form.querySelector('button[type="submit"], input[type="submit"], button:not([type])')
          || form);
  });

  // For a control that navigates by script (the app bar's back, which goes
  // through history): the same wait, on the same terms.
  window.blam8Wait = function (el) { start(el, true); };

  window.addEventListener('pageshow', clear);

  /* ── The thumbs-up on a draft (Kris 2026-09-22) ──────────────────────────
   *
   * "The thumbs up on the drafts doesn't obviously change when clicked."
   * The generic wait above is a white ring and a faint tint in currentColor:
   * legible on a wide navy button, all but invisible on a 36px emerald
   * square. So a control marked data-send-ack answers in its own two beats —
   * grey as the finger lands, then breathing green while the send is in
   * flight (styles in app/base.html). data-busy on those buttons is what
   * keeps start() off them: this IS their wait.
   *
   * It lives HERE, not with the thread's own script, for the reason the
   * first two attempts failed on Kris's phone: bound at the foot of a page's
   * script it is hostage to every line above it, and it is not what a
   * template is for either — "the control that was clicked wears the wait"
   * is this file's whole job.
   *
   * Same rule as the doors: the press only COLOURS it. A button locked
   * against pointer events under a finger never gets its click at all, so
   * the lock waits for the submit — which is also the keyboard's only way
   * in, since it never lands a pointer. */
  var ack = null, ackArm = null;

  function ackSending() { return !!ack && ack.classList.contains('send-ack-lock'); }

  function ackClear() {
    if (ackArm) { clearTimeout(ackArm); ackArm = null; }
    if (!ack) return;
    ack.classList.remove('send-ack', 'send-ack-go', 'send-ack-lock');
    ack.removeAttribute('aria-busy');
    ack = null;
  }

  function ackHold(btn) {
    if (!btn || btn === ack) return;
    ackClear();
    ack = btn;
    btn.classList.add('send-ack');          // grey, at once, no fade to miss
    btn.setAttribute('aria-busy', 'true');
  }

  function ackTarget(e) {
    return e.target && e.target.closest ? e.target.closest('[data-send-ack]') : null;
  }

  // No defaultPrevented check: a page whose own gesture handler claims the
  // press (the thread stage's swipe) must not cost the button its answer.
  document.addEventListener('pointerdown', function (e) {
    if (e.button !== 0) return;
    ackHold(ackTarget(e));
  }, true);

  // A press that never became a send puts the button back — a drag off it, a
  // long press, a swipe that started on it. The click lands after pointerup,
  // so this waits to see whether one does.
  document.addEventListener('pointerup', function () {
    if (!ack || ackSending()) return;
    if (ackArm) clearTimeout(ackArm);
    ackArm = setTimeout(function () { ackArm = null; if (!ackSending()) ackClear(); }, 500);
  }, true);
  document.addEventListener('pointercancel', function () { if (!ackSending()) ackClear(); }, true);

  // Capture, so this runs before the POST.
  document.addEventListener('submit', function (e) {
    var form = e.target;
    var btn = form.querySelector ? form.querySelector('[data-send-ack]') : null;
    if (!btn) return;
    ackHold(btn);                            // a keyboard send landed no finger
    if (ackArm) { clearTimeout(ackArm); ackArm = null; }
    btn.classList.add('send-ack-go');        // ...and now it is going
    btn.classList.add('send-ack-lock');      // one tap is the whole send
    // ...and says so in words (Kris 2026-09-24: "Sending…" then "Sent ✓").
    // A deposit or bookings riding along take a moment longer, so it says that too.
    var st = form.querySelector('[data-send-status]');
    if (st) {
      st.textContent = form.querySelector('[data-sends-with]') ? 'Sending — with the deposit…' : 'Sending…';
      st.classList.add('animate-pulse');
    }
  }, true);

  window.addEventListener('pageshow', ackClear);
})();
