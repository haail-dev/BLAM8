// The status column's read-only sections (Kris 2026-10-17): what the agent is doing now, what has been done,
// links, context, the wider day and the two allowances. Built in JS beside concierge_sidebar.js, which owns the
// column and passes in its helpers. Nothing here acts: it reads what the panel holds and what the server's
// own ledger says (GET /concierge/business/status, /around), and a section with nothing to show stays hidden.

// Hidden for now, kept for later (Kris 2026-10-16): the Context box stays away (it may come back as an "In focus" box), and
// Done today shows only after a turn that looked at the ledger ("what have we done today?"), while the agent answers.
export var SHOW_CONTEXT = false;
var LEDGER_LOOKUP = /awaiting your approval/i;   // the ledger lookup's own label in the trail (concierge_routes _BUSINESS_TOOL_LABELS)
export var LOAD_GAP_MS = 10000;      // at most one status read this often (it rides the panel's own refresh points)
export var AROUND_GAP_MS = 180000;   // the wider-day box is read with the status, then at most this often
var STATE_ORDER = ['Went through', 'Answered', 'Sent (mail log only)', 'Not done', 'Expired', 'Check this one', 'Parked', 'Needs you', 'Working'];

export function minutesWord(sec) {
  var m = Math.round((sec || 0) / 60);
  return m + ' min';
}

// The two allowances are never merged: questions per day (text) and live minutes per day (voice, per person).
export function limitLines(limits) {
  var out = [], t = limits && limits.text, v = limits && limits.voice;
  if (t && t.unlimited) out.push({ label: 'Typed questions today', text: 'No daily limit' });
  else if (t && !t.unavailable && t.cap != null) out.push({ label: 'Typed questions today', text: t.used + ' of ' + t.cap + ' used · ' + t.left + ' left' });
  if (v && v.enabled === false) out.push({ label: 'Live voice today', text: 'Not switched on for this account' });
  else if (v && v.enabled && v.unlimited) out.push({ label: 'Live voice today', text: 'No daily limit' });
  else if (v && v.enabled && !v.unavailable) out.push({ label: 'Live voice today', text: minutesWord(v.used_seconds) + ' of ' + minutesWord(v.limit_seconds) + ' used · ' + minutesWord(v.remaining_seconds) + ' left' });
  return out;
}

function approval(it) {
  if (!it.approved_by) return it.settled_at ? 'settled ' + it.settled_at : '';
  var how = it.approved_how === 'route not recorded' ? ' (route not recorded)' : it.approved_how ? ' ' + it.approved_how : '';
  return 'approved by ' + it.approved_by + how;
}
// One ledger item as three short lines. The words are the ledger's own (src/app/concierge_ledger.py).
export function ledgerLine(it) {
  var meta = [it.state, it.customer, approval(it), it.signed_off ? 'signed off (tap)' : '', it.age].filter(Boolean).join(' · ');
  return { line: it.title || '', meta: meta, detail: it.detail || '', state: it.state };
}
export function doneSummary(counts) {
  var c = counts || {};
  return STATE_ORDER.filter(function (s) { return c[s]; }).map(function (s) { return c[s] + ' ' + s.toLowerCase(); }).join(' · ');
}
export function sinceWord(from, now) {
  if (!from) return '';
  var d = new Date(from), n = new Date(now || Date.now());
  var time = d.getHours() + ':' + ('0' + d.getMinutes()).slice(-2);
  return d.toDateString() === n.toDateString() ? 'today at ' + time : d.getDate() + '/' + (d.getMonth() + 1) + ' at ' + time;
}

// c = { el, box, row, api, doc, fetchJson } from the sidebar; returns { paint, schedule, boxes }.
export function mountStatus(c) {
  var el = c.el, S = { data: null, around: null, aroundKey: null, aroundAt: 0, loadedAt: 0, timer: null, loading: false };
  // The less central boxes start folded (a person opens what they want; the choice is remembered per device).
  var nowBox = c.box('now', 'Doing now'), doneBox = c.box('done', 'Done today'), linkBox = c.box('links', 'Links', { closed: true }),
      ctxBox = c.box('context', 'Context', { closed: true }), aroundBox = c.box('around', 'Around the business', { closed: true, onOpen: function () { loadAround(); } }),
      limitBox = c.box('limits', 'Limits', { closed: true });
  var boxes = [nowBox, doneBox, linkBox, ctxBox, aroundBox, limitBox];
  boxes.forEach(function (b) { b.node.setAttribute('data-empty', 'true'); });

  function fill(b, rows) {
    b.body.textContent = '';
    rows.forEach(function (r) { b.body.appendChild(r); });
    b.hasRows = rows.length > 0;
    dotsFor(b, rows.length);
    b.node.setAttribute('data-empty', rows.length ? 'false' : 'true');
  }
  function dotsFor(b, n) { c.dots(b.count, n, 'item', el); }
  function note(text) { return c.row('', text, '', null); }

  function load(force) {
    if (S.loading || !c.fetchJson) return;
    if (!force && typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
    S.loading = true; S.loadedAt = Date.now();
    c.fetchJson('/concierge/business/status').then(function (d) { S.data = d; }).catch(function () {})
      .then(function () {
        S.loading = false; paint(); if (c.repaint) c.repaint();
        loadAround();   // read with the status (at most every AROUND_GAP_MS), so the dots show before the box is opened
      });
  }
  // Ride the panel's own refresh points (the 'cz:side' event): one read now, or one trailing read after the gap.
  function schedule() {
    var wait = S.loadedAt + LOAD_GAP_MS - Date.now();
    if (wait <= 0) { load(); return; }
    if (S.timer) return;
    S.timer = setTimeout(function () { S.timer = null; load(); }, wait);
    if (S.timer && S.timer.unref) S.timer.unref();
  }
  function loadAround() {
    if (!c.fetchJson || Date.now() - S.aroundAt < AROUND_GAP_MS) return;
    S.aroundAt = Date.now();
    c.fetchJson('/concierge/business/around').then(function (d) { S.around = d; paint(); if (c.repaint) c.repaint(); }).catch(function () {});
  }

  function paintNow() {
    var a = c.api.activity ? c.api.activity() : { busy: false, ran: [] }, rows = [];
    if (a.busy) rows.push(c.row('…', a.step || 'Working on your question', 'Now', null));
    if (a.ran && a.ran.length) rows.push(c.row('', (a.busy ? 'Looked at so far: ' : 'Last request looked at: ') + a.ran.join(', '), '', null));
    var tasks = c.api.tasks() || [], count = {};
    tasks.forEach(function (t) { count[t.status] = (count[t.status] || 0) + 1; });
    var bits = ['working', 'queued', 'waiting', 'needs_you', 'done', 'failed'].filter(function (s) { return count[s]; })
      .map(function (s) { return count[s] + ' ' + ({ working: 'working', queued: 'queued', waiting: 'waiting for you', needs_you: 'need you', done: 'done', failed: "couldn't be done" })[s]; });
    if (bits.length) rows.push(c.row('', 'Task list: ' + bits.join(' · '), '', null));
    tasks.filter(function (t) { return t.status === 'waiting' || t.status === 'needs_you'; }).slice(0, 4).forEach(function (t) {
      rows.push(c.row(t.status === 'waiting' ? '⏳' : '?', t.title || 'Task',
        t.status === 'waiting' ? 'Waiting because it needs your approval' : 'Waiting because it needs an answer from you' + (t.outcome ? ' — ' + t.outcome : ''), null));
    });
    if (S.data && S.data.loop === 'split' && (a.busy || tasks.length)) rows.push(c.row('', 'Each task is planned, done and then checked', 'The step it is on is not shown', null));
    fill(nowBox, rows);
  }
  function paintDone() {
    var ran = (c.api.activity ? c.api.activity().ran : []) || [];
    var cardDone = !!(c.api.activity && c.api.activity().cardDone);
    if (!cardDone && !ran.some(function (l) { return LEDGER_LOOKUP.test(String(l)); })) { fill(doneBox, []); return; }
    var t = S.data && S.data.today, rows = [];
    if (t && t.items && t.items.length) {
      var s = doneSummary(t.counts);
      if (s) rows.push(c.row('', 'Today: ' + s, '', null));
      t.items.slice(0, 10).forEach(function (it) { var l = ledgerLine(it); rows.push(c.row(it.state === 'Went through' ? '✓' : '', l.line, l.meta, null, null, { detail: l.detail })); });
      if (t.more) rows.push(c.row('', '+ ' + t.more + ' more', '', null));
    }
    if (t && t.unavailable) rows.push(note('The record of what was done could not be read just now'));
    fill(doneBox, rows);
  }
  function paintLinks() {
    var a = c.api.activity ? c.api.activity() : { links: [] }, rows = [], seen = {};
    function add(label, href, meta) {
      if (!href || href.charAt(0) !== '/' || seen[href]) return;
      seen[href] = 1; rows.push(c.row('→', label || 'Open', meta, null, null, { href: href }));
    }
    (a.links || []).forEach(function (l) { add(l.label, l.href, 'From the last answer'); });
    (c.api.done ? c.api.done() : []).slice(0, 8).forEach(function (d) { add(d.summary || d.outcome, d.href, 'Done today'); });
    (c.api.waiting() || []).forEach(function (w) { if (w.type === 'draft' || w.type === 'link') add(w.action_summary || w.summary, w.href, 'Draft'); });
    fill(linkBox, rows);
  }
  function paintContext() {
    if (!SHOW_CONTEXT) { fill(ctxBox, []); return; }
    var x = c.api.context ? c.api.context() : null, rows = [];
    if (x) {
      if (x.found) rows.push(c.row('', 'In focus: ' + x.found, '', null));
      rows.push(c.row('', x.from ? 'Chat context starts ' + sinceWord(x.from) : 'Chat context: this visit', x.from ? 'You tapped New task' : '', null));
      if (x.sources && x.sources.length) rows.push(c.row('', 'Looked at: ' + x.sources.slice(0, 8).join(', '), '', null));
    }
    if (rows.length === 1 && !(x && (x.found || (x.sources && x.sources.length) || x.from))) rows = [];
    fill(ctxBox, rows);
  }
  // Minimal (Kris 2026-10-16): the box lists each group by its name and count; a tap opens that group's items, each by its name;
  // a tap on an item opens what else there is to say about it. Back returns to the groups.
  function paintAround() {
    var rows = [], groups = (S.around && S.around.groups) || [];
    var inside = groups.filter(function (g) { return g.key === S.aroundKey; })[0];
    if (!inside) S.aroundKey = null;
    if (inside) {
      var bar = el('div', 'cz-side-item'), back = el('button', 'cz-side-back', '\u2039 Back to the list');
      back.type = 'button';
      back.addEventListener('click', function () { S.aroundKey = null; paintAround(); });
      bar.appendChild(back); bar.appendChild(el('p', 'cz-side-hint', inside.label + ' (' + inside.count + ')'));
      rows.push(bar);
      inside.items.forEach(function (i) {
        var title = i.who || i.text || inside.label;
        rows.push(c.expand('', title, [i.chip, i.who && i.text ? i.text : '']));
      });
      if (inside.more) rows.push(note('+ ' + inside.more + ' more'));
    } else {
      groups.forEach(function (g) {
        rows.push(c.row('', g.label + ' (' + g.count + ')', '', function () { S.aroundKey = g.key; paintAround(); }));
      });
    }
    if (!inside && S.around && S.around.unread && S.around.unread.length) rows.push(note('Could not read: ' + S.around.unread.join(', ')));
    fill(aroundBox, rows);
    if (inside) dotsFor(aroundBox, groups.length);   // the squares count the groups, not the lines of the one that is open
    if (!rows.length) aroundBox.node.setAttribute('data-empty', S.around ? 'true' : 'false');
    if (!rows.length && !S.around) aroundBox.body.appendChild(note('Open to read the wider day'));
  }
  function paintLimits() {
    fill(limitBox, limitLines(S.data && S.data.limits).map(function (l) { return c.row('', l.label, l.text, null); }));
  }
  function paint() { paintNow(); paintDone(); paintLinks(); paintContext(); paintAround(); paintLimits(); }

  // What the ledger knows about a waiting item (its own wording: Needs you, Parked, Expired …) and the
  // open items the chat's waiting list does not hold (an expired card, an unverified result).
  function open() { return (S.data && S.data.open && S.data.open.items) || []; }
  function enrich(w) {
    var id = w && (w.proposal_id || w.log_id);
    var it = id && open().filter(function (x) { return x.id === id; })[0];
    return it ? ledgerLine(it) : null;
  }
  function extras(waiting, skip) {
    var ids = {};
    Object.keys(skip || {}).forEach(function (k) { ids[k] = 1; });   // cards open on the right are not listed again
    waiting.forEach(function (w) { ids[w.proposal_id || w.log_id] = 1; });
    return open().filter(function (it) { return !ids[it.id] && (it.state === 'Needs you' || it.state === 'Check this one' || it.state === 'Expired'); });
  }
  function hasRows() { return boxes.some(function (b) { return b.hasRows; }); }
  return { hasRows: hasRows, paint: paint, schedule: schedule, load: load, boxes: boxes, enrich: enrich, extras: extras, nodes: boxes.map(function (b) { return b.node; }) };
}
