/* The save bar earns its place.
 *
 * A Save button parked under a form it has nothing to save is furniture: it
 * reads as unfinished work every time you look at a settings page, and it
 * tells you nothing, because it looks identical whether you have changed
 * something or not. Kris, 2026-09-01: "it feels old".
 *
 * So the bar appears when the form stops matching the state it loaded in, and
 * goes again if you undo your way back. The comparison is the form's own
 * serialised value, not per-field listeners, which means it survives fields
 * being added or removed at runtime (the intake checklist does exactly that)
 * and it correctly HIDES again when an edit is reverted by hand.
 *
 * Contract: mark the wrapper of the save controls with [data-save-row]. It
 * must sit inside the <form> it saves. A row also marked [data-save-always]
 * is left visible: a first-run page whose pre-filled defaults are meant to
 * be accepted as they are (the booking checklist) has nothing to change, and
 * a bar that only appears on a change left the owner with nothing to press
 * (first-sign-up film, 30 Sep 2026).
 *
 * Progressive enhancement, deliberately: the bar renders visible and this
 * hides it. If the script fails to load, every page degrades to an
 * always-visible Save — yesterday's behaviour — rather than a form nobody can
 * submit.
 */
(function () {
  function signature(form) {
    var parts = [];
    (window.Blam8ProposalForms ? window.Blam8ProposalForms.data(form) : new FormData(form)).forEach(function (v, k) {
      /* Files have no readable value; name+size distinguishes a swapped pick. */
      parts.push(k + '=' + (v instanceof File ? v.name + ':' + v.size : v));
    });
    return parts.join('&');
  }

  function watch(row) {
    if (row.dataset.dirtyWatched) return;
    if (row.hasAttribute('data-save-always')) { row.dataset.dirtyWatched = '1'; row.hidden = false; return; }
    var form = row.closest('form');
    if (!form) return;
    row.dataset.dirtyWatched = '1';

    var pristine = signature(form);
    function sync() { row.hidden = signature(form) === pristine; }
    sync();

    form.addEventListener('input', sync);
    form.addEventListener('change', sync);

    /* Rows a script adds or removes change the signature without any event
     * the form fires — re-check when the shape of the form itself moves. */
    new MutationObserver(sync).observe(form, { childList: true, subtree: true });

    /* After a save the page reloads, so pristine re-reads itself. Forms that
     * post without navigating tell us by dispatching this. */
    form.addEventListener('dirty-save:reset', function () {
      pristine = signature(form);
      sync();
    });
  }

  function scan() { document.querySelectorAll('[data-save-row]').forEach(watch); }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', scan);
  } else {
    scan();
  }
  /* Live fragment swaps drop new forms into the page mid-session. */
  document.addEventListener('live:swap', scan);
})();
