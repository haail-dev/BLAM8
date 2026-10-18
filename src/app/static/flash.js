/* A confirmation is a moment, not a fixture.
 *
 * Kris, 2026-09-01, on "Saved. New drafts will use these instructions." still
 * sitting on the settings page: "For some reason theres a 'Saved...' toast,
 * permentaly on page?"
 *
 * It looked like a toast and behaved like furniture. Saving redirects to
 * ?saved=1 and the banner is rendered from that parameter, so it never timed
 * out, and — worse — the parameter stayed in the address bar. Reload, hit Back,
 * or restore the tab tomorrow and the app cheerfully announced a save that
 * happened last week.
 *
 * Two things fix it, and the app already had both in one place (the inbox's
 * ignore toast): scrub the parameter so a refresh cannot re-announce anything,
 * and retire the message on a timer.
 *
 * Scope: the URL is scrubbed for every notice parameter, success or failure —
 * a stale error is as wrong as a stale success. Only successes fade. An error
 * stays until you leave the page, because it is telling you something you may
 * still need to act on. Mark a success banner with [data-flash].
 */
(function () {
  /* Notice parameters only. `code`, `state`, `token`, `ref`, `mailbox`, `fix`
   * and friends address the page itself — removing those would break OAuth
   * returns and deep links. */
  var NOTICE = ['saved', 'msg', 'err', 'error', 'sched', 'portal', 'intake',
                'voice', 'ref_err',
                'undo'];   /* the job page's "X unlinked — Undo": a reload must not re-offer it */
  var LIFETIME = 7000;      // matches the ignore toast — long enough to read
  var FADE = 320;

  try {
    var url = new URL(location.href);
    var scrubbed = false;
    NOTICE.forEach(function (k) {
      if (url.searchParams.has(k)) { url.searchParams.delete(k); scrubbed = true; }
    });
    /* Keep the hash: settings can be linked straight to a row (#sending-mode). */
    if (scrubbed) {
      history.replaceState(history.state, '',
                           url.pathname + url.search + url.hash);
    }
  } catch (e) {}

  function retire(el) {
    /* Inline banners sit in the flow, so fading alone would leave a hole and
     * removing outright would snap the page upward. Collapse what it occupies
     * over the same beat as the fade. */
    var h = el.offsetHeight;
    var cs = getComputedStyle(el);
    el.style.overflow = 'hidden';
    el.style.height = h + 'px';
    el.style.marginTop = cs.marginTop;
    el.style.marginBottom = cs.marginBottom;
    /* Force a frame so the browser has a height to animate FROM. */
    el.offsetHeight;                                        // eslint-disable-line
    el.style.transition = 'opacity ' + FADE + 'ms ease, height ' + FADE +
                          'ms ease, margin ' + FADE + 'ms ease';
    el.style.opacity = '0';
    el.style.height = '0px';
    el.style.marginTop = '0px';
    el.style.marginBottom = '0px';
    setTimeout(function () { el.remove(); }, FADE + 40);
  }

  function arm(el) {
    if (el.dataset.flashArmed) return;
    el.dataset.flashArmed = '1';
    /* Announced before it goes: a message that only ever existed for seven
     * seconds is no use to a screen reader that never heard it. */
    if (!el.getAttribute('role')) el.setAttribute('role', 'status');

    /* [data-flash-hold]: a count the reader must see (every booking cancelled —
     * run #8: gone before the 8 s read) stays until tapped. */
    if (el.dataset.flashHold === undefined) {
      if (matchMedia('(prefers-reduced-motion: reduce)').matches) {
        setTimeout(function () { el.remove(); }, LIFETIME);
      } else {
        setTimeout(function () { retire(el); }, LIFETIME);
      }
    }
    /* Tap to dismiss early, the way the ignore toast does — but never steal a
     * click meant for a link inside the message. */
    el.style.cursor = 'pointer';
    el.addEventListener('click', function (e) {
      if (!e.target.closest('a, button')) retire(el);
    });
  }

  function scan() { document.querySelectorAll('[data-flash]').forEach(arm); }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', scan);
  } else {
    scan();
  }
  document.addEventListener('live:swap', scan);
})();
