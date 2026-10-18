// /pages — the filter box. Hides pages whose label, path or note don't contain
// the typed words (all of them), then hides groups and sections left empty.
// Progressive: with no JS the full index is simply all there.
(function () {
  var box = document.getElementById('pages-filter');
  var count = document.getElementById('pages-filter-count');
  var empty = document.getElementById('pages-empty');
  if (!box) return;
  var pages = Array.prototype.slice.call(document.querySelectorAll('[data-page]'));
  var groups = Array.prototype.slice.call(document.querySelectorAll('[data-group]'));
  var sections = Array.prototype.slice.call(document.querySelectorAll('[data-section]'));

  function apply() {
    var words = box.value.toLowerCase().split(/\s+/).filter(Boolean);
    var shown = 0;
    pages.forEach(function (li) {
      var text = li.getAttribute('data-text') || '';
      var hit = words.every(function (w) { return text.indexOf(w) !== -1; });
      li.hidden = !hit;
      if (hit) shown += 1;
    });
    groups.forEach(function (g) {
      g.hidden = !g.querySelector('[data-page]:not([hidden])');
    });
    sections.forEach(function (s) {
      s.hidden = !s.querySelector('[data-group]:not([hidden])');
    });
    if (words.length) {
      count.textContent = shown + (shown === 1 ? ' page' : ' pages') + ' match';
      count.hidden = false;
    } else {
      count.hidden = true;
    }
    empty.hidden = !(words.length && shown === 0);
  }
  box.addEventListener('input', apply);
  if (box.value) apply();
})();
