// The masthead search (Kris 2026-09-21): the magnifying glass beside the cog.
//
// One box on every page: a booking, quote or invoice by its reference, a
// customer by name or email, suggested as you type (GET /app/search?q=…,
// src/app/search.py). The list is grouped in the order Kris named them —
// Bookings, Quotes, Invoices — then Customers; arrows move, Enter opens the
// lit row (or the first), Escape closes. "/" anywhere outside a field, or
// Ctrl/Cmd+K, opens it. Rows are built as DOM nodes with textContent, never
// from a markup string with data in it.
//
// /help (Kris 2026-09-23): the App help assistant lost its floating tab and
// lives here now. The /help link under the hint opens it; "/help how do I…"
// typed in the box offers one row that opens it AND asks the question
// (concierge.js, blam8ConciergeAsk — which lands on the App help tab).
(function () {
  var btn = document.getElementById('search-btn');
  var box = document.getElementById('search-box');
  if (!btn || !box) return;
  var input = document.getElementById('search-input');
  var list = document.getElementById('search-results');
  var hint = document.getElementById('search-hint');
  var helpLine = document.getElementById('search-help-line');
  var helpLink = document.getElementById('search-help');
  var menu = document.getElementById('menu');
  var menuBtn = document.getElementById('menu-btn');
  var LABELS = { help: 'App help', booking: 'Bookings', quote: 'Quotes', invoice: 'Invoices', contact: 'Customers' };
  var ORDER = ['help', 'booking', 'quote', 'invoice', 'contact'];
  var timer = null, seq = 0, items = [], active = -1, lastQ = '';

  function isOpen() { return !box.classList.contains('hidden'); }
  function open() {
    if (window.blam8ConciergeHide) window.blam8ConciergeHide();   // one overlay at a time
    if (menu) menu.classList.add('hidden');
    if (menuBtn) menuBtn.setAttribute('aria-expanded', 'false');
    box.classList.remove('hidden');
    btn.setAttribute('aria-expanded', 'true');
    input.focus();
    input.select();
  }
  function close() {
    box.classList.add('hidden');
    btn.setAttribute('aria-expanded', 'false');
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function setActive(i) {
    var rows = list.querySelectorAll('[data-i]');
    active = i;
    rows.forEach(function (r) {
      var on = Number(r.getAttribute('data-i')) === i;
      r.classList.toggle('bg-brand-50', on);
      r.setAttribute('aria-selected', on ? 'true' : 'false');
      if (on && r.scrollIntoView) r.scrollIntoView({ block: 'nearest' });
    });
  }

  // "/help …" → the question after it ('' = just open). null = not a help
  // command. "/", "/h", "/he"… count too, so the row is there as they type.
  function helpQuery(q) {
    var m = /^\/(\S*)\s*([\s\S]*)$/.exec(q);
    if (!m) return null;
    var cmd = m[1].toLowerCase();
    if (cmd === 'help') return m[2].trim();
    if ('help'.indexOf(cmd) === 0 && !m[2]) return '';   // "/", "/h", "/he"… on the way
    return null;
  }
  function openHelp(question) {
    close();
    if (window.blam8ConciergeAsk) { window.blam8ConciergeAsk(question || ''); return; }
    window.location.href = '/help';   // the widget failed to load — the public help page
  }

  function render(results) {
    items = results || [];
    list.textContent = '';
    active = -1;
    if (helpLine) helpLine.hidden = !!lastQ;
    if (!items.length) {
      hint.hidden = false;
      hint.textContent = lastQ.length < 2
        ? 'A booking, quote or invoice by its reference — or a customer by name or email.'
        : 'Nothing matches "' + lastQ + '". Try a reference like BK-1003, or the start of a name or email.';
      return;
    }
    hint.hidden = true;
    var byKind = {};
    items.forEach(function (it, i) { (byKind[it.kind] = byKind[it.kind] || []).push([it, i]); });
    ORDER.forEach(function (kind) {
      if (!byKind[kind]) return;
      list.appendChild(el('p', 'px-3 pt-2 pb-1 text-[10px] font-semibold uppercase tracking-wider text-slate-400', LABELS[kind] || kind));
      byKind[kind].forEach(function (pair) {
        var it = pair[0], i = pair[1];
        var a = el('a', 'block px-3 py-2 hover:bg-slate-50 transition');
        if (it.href) a.href = it.href; else { a.href = '#'; a.addEventListener('click', function (e) { e.preventDefault(); go(i); }); }
        a.setAttribute('data-i', String(i));
        a.setAttribute('role', 'option');
        a.appendChild(el('span', 'block text-sm font-medium text-navy truncate', it.label));
        if (it.sub) a.appendChild(el('span', 'block text-xs text-slate-400 truncate', it.sub));
        a.addEventListener('mousemove', function () { if (active !== i) setActive(i); });
        list.appendChild(a);
      });
    });
  }

  function go(i) {
    var it = items[i];
    if (!it) return;
    if (it.kind === 'help') { openHelp(it.question); return; }
    var row = list.querySelector('[data-i="' + i + '"]');
    if (row && window.blam8Wait) window.blam8Wait(row);   // the row wears the wait, as any link does
    window.location.href = it.href;
  }

  function fetchNow() {
    var q = input.value.trim();
    lastQ = q;
    if (!q) { render([]); return; }
    var hq = helpQuery(q);
    if (hq !== null) {
      seq++;   // a search still in flight must not paint over the help row
      render([{ kind: 'help', question: hq, label: hq ? 'Ask App help: ' + hq : 'Open App help', sub: hq ? 'Opens the assistant and asks' : 'Type your question after /help, or open it now' }]);
      return;
    }
    var mine = ++seq;
    fetch('/app/search?q=' + encodeURIComponent(q), { headers: { 'Accept': 'application/json' }, credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : { results: [] }; })
      .then(function (d) { if (mine === seq) render(d.results || []); })
      .catch(function () { if (mine === seq) render([]); });
  }

  input.addEventListener('input', function () {
    clearTimeout(timer);
    timer = setTimeout(fetchNow, 120);
  });
  input.addEventListener('keydown', function (e) {
    if (e.key === 'ArrowDown') { e.preventDefault(); if (items.length) setActive(active < items.length - 1 ? active + 1 : 0); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); if (items.length) setActive(active > 0 ? active - 1 : items.length - 1); }
    else if (e.key === 'Enter') { e.preventDefault(); if (items.length) go(active >= 0 ? active : 0); }
    else if (e.key === 'Escape') { e.preventDefault(); close(); btn.focus(); }
  });
  if (helpLink) helpLink.addEventListener('click', function (e) { e.preventDefault(); openHelp(''); });
  btn.addEventListener('click', function (e) {
    e.stopPropagation();
    if (isOpen()) close(); else open();
  });
  if (menuBtn) menuBtn.addEventListener('click', function () { close(); });
  document.addEventListener('click', function (e) { if (isOpen() && !box.contains(e.target)) close(); });
  box.addEventListener('click', function (e) { e.stopPropagation(); });

  function typing(t) {
    if (!t) return false;
    var tag = (t.tagName || '').toLowerCase();
    return tag === 'input' || tag === 'textarea' || tag === 'select' || t.isContentEditable;
  }
  document.addEventListener('keydown', function (e) {
    if (isOpen() && e.key === 'Escape') { close(); return; }
    if ((e.key === 'k' || e.key === 'K') && (e.ctrlKey || e.metaKey)) { e.preventDefault(); open(); return; }
    if (e.key === '/' && !e.ctrlKey && !e.metaKey && !e.altKey && !typing(e.target)) { e.preventDefault(); open(); }
  });
})();
