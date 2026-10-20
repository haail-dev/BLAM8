/* Live inbox — the "instant chat" client.
 *
 * One EventSource per tab on /app/stream (src/app/sse.py). The server publishes
 * tiny events the moment the pipeline acts (drafting / drafting_done /
 * draft_ready / draft_sent / status_changed); this file repaints the affected
 * pieces by fetching their fragment endpoints and swapping innerHTML in place:
 *   - #nav-draft-badge   ← /app/nav/badge                       (every page)
 *   - #convo-pills/#convo-list ← /app/conversations/fragment    (inbox list)
 *   - #thread-live       ← /app/conversations/view/fragment     (open thread)
 * Swapping innerHTML (never the elements) keeps the delegated long-press
 * handlers on #convo-list alive; the thread page re-runs blam8ThreadInit after
 * its swap because its bindings are per-element.
 * EventSource reconnects by itself; a reconnect's `hello` triggers a full
 * repaint so nothing missed while offline stays stale.
 */
(function () {
  'use strict';

  var list = document.getElementById('convo-list');
  var thread = document.getElementById('thread-live');
  var badge = document.getElementById('nav-draft-badge');
  if (!badge && !list && !thread) return;   // not an app page

  function debounce(fn, ms) {
    var t = null;
    return function () { clearTimeout(t); t = setTimeout(fn, ms); };
  }

  function threadContact() {
    return ((thread && thread.dataset.contact) || '').toLowerCase();
  }

  // ── Fragment refreshers ────────────────────────────────────────────────────
  var refreshBadge = debounce(function () {
    if (!badge) return;
    fetch('/app/nav/badge').then(function (r) { return r.ok ? r.text() : null; })
      .then(function (html) { if (html !== null) badge.innerHTML = html; })
      .catch(function () {});
  }, 300);

  var refreshList = debounce(function () {
    if (!list) return;
    // Never yank rows out from under an active multi-select, a press that may
    // still become a click (swapping the anchor mid-press makes the click land
    // on #convo-list and die), or an /open navigation in progress — the next
    // event (or a page navigation) repaints once it's done.
    if (list.classList.contains('selecting') || list.classList.contains('pressing')
        || list.classList.contains('opening')) return;
    fetch(list.dataset.fragmentUrl).then(function (r) { return r.ok ? r.text() : null; })
      .then(function (html) {
        if (html === null) return;
        var doc = new DOMParser().parseFromString(html, 'text/html');
        var main = document.querySelector('main');
        var keep = main ? main.scrollTop : 0;
        ['convo-pills', 'convo-list'].forEach(function (id) {
          var from = doc.getElementById(id), to = document.getElementById(id);
          if (from && to) to.innerHTML = from.innerHTML;
        });
        if (main) main.scrollTop = keep;
      })
      .catch(function () {});
  }, 300);

  var refreshThread = debounce(function () {
    if (!thread) return;
    // Never blow away in-progress edits: a focused field inside the thread
    // (someone rewording the draft) blocks the swap; the state is still in the
    // DB, so the next event or reload shows it.
    var ae = document.activeElement;
    if (ae && thread.contains(ae) && /^(TEXTAREA|INPUT)$/.test(ae.tagName)) return;
    fetch(thread.dataset.fragmentUrl).then(function (r) { return r.ok ? r.text() : null; })
      .then(function (html) {
        if (html === null) return;
        thread.innerHTML = html;
        var identity = thread.querySelector('#thread-stage');
        var heading = document.querySelector('[data-thread-contact-name]');
        if (identity && heading) {
          heading.textContent = identity.dataset.contactName || thread.dataset.contact;
          heading.href = identity.dataset.contactHref || heading.href;
          document.title = heading.textContent + ' · BLAM8';
        }
        if (window.blam8ThreadInit) window.blam8ThreadInit();
        renderDraftSteps();   // the swap brings the ticker container; refill it
        if (window.blam8Ghost) window.blam8Ghost.scan(thread);
      })
      .catch(function () {});
  }, 300);

  // ── Live ghost — the drafting_step lines ARE the ghost being born ─────────
  // (Kris 2026-08-31: the toast is just the toast; the streamed tool use
  // builds up centre-stage as fresh ghost text, waiting for the rest.)
  // Buffered here (not in the DOM) because the fragment swap that introduces
  // #stage-ghost-live often lands AFTER the first steps arrive.
  var draftSteps = [];
  var draftKey = '', stepSequence = 0;
  function renderDraftSteps() {
    var box = document.getElementById('stage-ghost-live');
    if (!box) return;
    box.textContent = '';
    draftSteps.forEach(function (step) {
      var p = document.createElement('p');
      p.className = 'text-sm text-slate-500';
      p.textContent = step.label;
      box.appendChild(p);
      if (window.blam8Ghost) window.blam8Ghost.reveal(p, step.key, true);
    });
  }

  // ── Typing bubble (thread page) — appended instantly, no fetch ────────────
  // The inbound being answered exists in NO table until the draft lands, so the
  // drafting event/hello carry its subject + body preview and we paint a
  // provisional incoming bubble here — otherwise the typing indicator shows
  // with no email above it ("replying before the message arrived"). Built with
  // textContent (never innerHTML): the email body is untrusted. The fragment
  // swap on draft_ready/drafting_done replaces it with the real row.
  function showIncoming(data) {
    var msgs = document.getElementById('thread-msgs');
    if (!msgs || (!data.subject && !data.body)) return;
    var key = data.key || '';
    var dup = Array.prototype.some.call(
      msgs.querySelectorAll('[data-draft-key]'),
      function (el) { return el.dataset.draftKey === key; });
    if (dup) return;
    var wrap = document.createElement('div');
    wrap.className = 'flex';
    wrap.dataset.draftKey = key;
    var bubble = document.createElement('div');
    bubble.className = 'max-w-[85%] rounded-2xl rounded-tl-sm bg-white border border-slate-200 p-4';
    var head = document.createElement('p');
    head.className = 'text-xs text-slate-400';
    var who = document.createElement('span');
    who.className = 'font-semibold text-slate-500';
    var identity = thread.querySelector('#thread-stage');
    var name = identity && identity.dataset.contactName;
    who.textContent = (name && name.toLowerCase() !== threadContact() ? name : '')
      || data.from_name || data.contact || data.from_address || '';
    head.appendChild(who);
    head.appendChild(document.createTextNode(
      ' · Just now' + (data.subject ? ' · ' + data.subject : '')));
    bubble.appendChild(head);
    if (data.body) {
      var body = document.createElement('div');
      body.className = 'mt-2 text-sm text-slate-700 whitespace-pre-wrap leading-relaxed max-h-[30vh] overflow-auto';
      body.textContent = data.body;
      bubble.appendChild(body);
    }
    wrap.appendChild(bubble);
    msgs.insertBefore(wrap, document.getElementById('thread-typing'));
    var main = document.querySelector('main');
    if (main) main.scrollTop = main.scrollHeight;
  }

  function showTyping() {
    var msgs = document.getElementById('thread-msgs');
    if (!msgs || document.getElementById('thread-typing')) return;
    var el = document.createElement('div');
    el.id = 'thread-typing';
    el.className = 'flex flex-col items-end';
    el.innerHTML =
      '<div class="max-w-[85%] rounded-2xl rounded-tr-sm bg-brand-50 border border-brand/30 px-4 py-3">' +
        '<p class="text-xs font-semibold text-brand inline-flex items-center gap-1.5">Agent writing reply' +
          '<span class="typing-dots inline-flex gap-0.5">' +
            '<span class="h-1 w-1 rounded-full bg-current"></span>' +
            '<span class="h-1 w-1 rounded-full bg-current"></span>' +
            '<span class="h-1 w-1 rounded-full bg-current"></span>' +
          '</span></p></div>';
    msgs.appendChild(el);
    var main = document.querySelector('main');
    if (main) main.scrollTop = main.scrollHeight;
  }

  function hideTyping() {
    var el = document.getElementById('thread-typing');
    if (el) el.remove();
  }

  function isForThisThread(data) {
    return thread && (data.contact || '').toLowerCase() === threadContact();
  }

  // ── The stream ─────────────────────────────────────────────────────────────
  var es = new EventSource('/app/stream');
  ['device_sync', 'concierge_message'].forEach(function(name){
    es.addEventListener(name,function(){window.dispatchEvent(new Event('blam8:device-sync'));});
  });
  var firstHello = true;

  es.addEventListener('hello', function (e) {
    var data; try { data = JSON.parse(e.data); } catch (err) { return; }
    var mine = (data.drafting || []).filter(function (d) {
      return (d.from_address || '').toLowerCase() === threadContact();
    });
    if (thread && mine.length) {
      draftKey = mine[mine.length - 1].key || '';
      mine.forEach(showIncoming);
      showTyping();
      refreshThread();   // the first hello may already know more than the page
    }
    if (firstHello) { firstHello = false; return; }  // fresh page — already current
    // Reconnect (deploy, laptop lid): repaint everything missed while away.
    refreshBadge(); refreshList(); refreshThread();
  });

  // A morning brief landed (2026-09-25: an open Concierge never showed it).
  // The Concierge panel listens for this and reloads its conversation.
  es.addEventListener('notification', function (e) {
    var data; try { data = JSON.parse(e.data); } catch (err) { return; }
    if (data.kind === 'morning_debrief') {
      try { window.dispatchEvent(new CustomEvent('blam8:brief')); } catch (err) {}
    }
  });

  // A task on the Concierge's task list moved (docs/plan-concierge-task-list.md).
  es.addEventListener('concierge_task', function (e) {
    var data; try { data = JSON.parse(e.data); } catch (err) { return; }
    try { window.dispatchEvent(new CustomEvent('blam8:task', { detail: data })); } catch (err) {}
  });

  es.addEventListener('drafting', function (e) {
    var data; try { data = JSON.parse(e.data); } catch (err) { data = {}; }
    // The instant bubbles land in #thread-msgs — the HISTORY layer since the
    // Duet stage (2026-08-31), invisible while the blind is up. The fragment
    // swap is what puts the typing pill on the stage dock, so fetch it too.
    if (isForThisThread(data)) {
      draftSteps = [];   // a new draft starts its ticker clean
      draftKey = data.key || '';
      if (window.blam8Ghost) window.blam8Ghost.begin();
      showIncoming(data); showTyping(); refreshThread();
    }
    refreshList();
  });

  // One mechanical step of the draft in flight — appended under the typing pill.
  es.addEventListener('drafting_step', function (e) {
    var data; try { data = JSON.parse(e.data); } catch (err) { return; }
    if (!isForThisThread(data) || !data.label) return;
    if (window.blam8Ghost) window.blam8Ghost.finish();
    draftSteps.push({label: data.label, key: 'live-' + draftKey + '-' + (++stepSequence)});
    if (draftSteps.length > 8) draftSteps.shift();
    renderDraftSteps();
  });

  es.addEventListener('drafting_done', function (e) {
    var data; try { data = JSON.parse(e.data); } catch (err) { return; }
    if (data.key && draftKey && data.key !== draftKey) return;
    if (window.blam8Ghost) window.blam8Ghost.finish();
    draftSteps = [];
    hideTyping();
    refreshList();
    // Repaint the thread from the DB: on success the provisional inbound is
    // replaced by the real row (already inserted by now); on a crashed draft
    // it simply disappears — either way the DOM stops guessing.
    refreshThread();
  });

  ['draft_ready', 'draft_sent', 'status_changed'].forEach(function (type) {
    es.addEventListener(type, function (e) {
      var data; try { data = JSON.parse(e.data); } catch (err) { data = {}; }
      refreshBadge();
      refreshList();
      if (!data.contact || isForThisThread(data)) refreshThread();
    });
  });

  if (thread && window.blam8Ghost) window.blam8Ghost.scan(thread);
})();
