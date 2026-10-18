/* Spare line rows (Kris 2026-09-24, on a quote: "There's 2 extra fields that
 * aren't used, there's no need to display them if they're empty. User would
 * just need a little plus symbol to add a new row").
 *
 * Contract: blank rows carry [data-spare-row] and render hidden; a button
 * [data-add-row] in the same form shows the next one and puts the cursor in
 * it, and hides itself once they are all showing. A form with no filled line
 * shows its first spare at once — an empty list with only a plus reads as
 * broken. Blank rows post blank, exactly as before: the server skips them.
 */
(function () {
  function wire(form) {
    if (form.dataset.spareWired) return;
    form.dataset.spareWired = '1';
    var btn = form.querySelector('[data-add-row]');
    function spares() { return Array.prototype.slice.call(form.querySelectorAll('[data-spare-row]')); }
    function sync() {
      if (btn) btn.hidden = !spares().some(function (r) { return r.hidden; });
    }
    function showNext(focus) {
      var next = spares().filter(function (r) { return r.hidden; })[0];
      if (!next) return;
      next.hidden = false;
      if (focus) {
        var f = next.querySelector('input:not([type=hidden]), select, textarea');
        if (f) f.focus();
      }
      sync();
    }
    if (!form.querySelector('[data-filled-row]')) showNext(false);
    if (btn) btn.addEventListener('click', function (e) { e.preventDefault(); showNext(true); });
    sync();
  }
  function scan() {
    document.querySelectorAll('[data-spare-row]').forEach(function (r) {
      var form = r.closest('form');
      if (form) wire(form);
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', scan);
  else scan();
  document.addEventListener('live:swap', scan);
})();
