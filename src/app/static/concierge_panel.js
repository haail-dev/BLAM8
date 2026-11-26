// The Concierge (Kris 2026-09-23): Q&A over the business's own data, with
// actions behind a Confirm card. One panel (app/_concierge_panel.html), two
// frames:
//   - the modal: the masthead's person icon (#concierge-btn) opens #cz-modal
//     on every app page; the panel is fetched from /app/concierge/panel the
//     first time and kept, so closing and reopening loses nothing;
//   - the page: /app/concierge renders the same panel inline, for deep links.
// It took over the Assistant widget's old "Your business" tab, live voice
// included; the widget keeps App help. No away-digest card or unseen badge
// (Kris 2026-09-23): the Morning debrief is how the day's news arrives.
(function () {
  "use strict";

  // Bounded, local diagnostics: no message content or network reporting.
  var timings = window.__blam8Timings = window.__blam8Timings || [];
  function timing(name, detail) {
    timings.push({ name: name, at: performance.now(), detail: detail || null });
    if (timings.length > 300) timings.splice(0, timings.length - 300);
  }
  window.__blam8Timing = timing;
  try {
    var longTasks = new PerformanceObserver(function (list) {
      list.getEntries().forEach(function (entry) {
        timing('longtask', { start: entry.startTime, duration: entry.duration });
      });
    });
    longTasks.observe({ type: 'longtask', buffered: true });
  } catch (_) {} // Not supported by every browser.
  window.__blam8ReadWorkerTimings = function () {
    return new Promise(function (resolve, reject) {
      var controller = navigator.serviceWorker && navigator.serviceWorker.controller;
      if (!controller) { reject(new Error('No controlling worker')); return; }
      var channel = new MessageChannel();
      var timer = setTimeout(function () { channel.port1.close(); reject(new Error('Worker diagnostics timed out')); }, 3000);
      channel.port1.onmessage = function (event) {
        clearTimeout(timer); channel.port1.close(); resolve(event.data);
      };
      controller.postMessage({ type: 'blam8:timings' }, [channel.port2]);
    });
  };
  function openingDeadline(promise, onTimeout) {
    var timer;
    return Promise.race([promise, new Promise(function (_, reject) {
      timer = setTimeout(function () {
        if (onTimeout) onTimeout();
        reject(new Error('Opening timed out'));
      }, 10000);
    })]).finally(function () { clearTimeout(timer); });
  }

  // Loading the voice screen's modules is slow on a phone behind the service worker
  // (measured 24 s headless, 70 s+ in Chrome), so it gets far longer than the panel.
  // Cutting it at 10 s dropped the user back to the text chat for a load that was
  // still going to finish.
  function moduleDeadline(promise) {
    var timer;
    return Promise.race([promise, new Promise(function (_, reject) {
      timer = setTimeout(function () { reject(new Error('Opening timed out')); }, 90000);
    })]).finally(function () { clearTimeout(timer); });
  }

  function keepPlatformRecordInAnotherTab(anchor, href) {
    if (location.pathname !== '/concierge/live' || !/^\/(?:app|onboarding)(?:\/|$)/.test(href || '')) return;
    anchor.target = '_blank';
    anchor.rel = 'noopener noreferrer';
  }
  function openPlatformRecord(href) {
    if (location.pathname === '/concierge/live' && /^\/(?:app|onboarding)(?:\/|$)/.test(href || '')) {
      window.open(href, '_blank', 'noopener,noreferrer');
      return;
    }
    window.location.href = href;
  }
  function closeVoiceBeforeSettings(voice, show) {
    var closing;
    try { closing = voice ? voice.close('settings') : null; }
    catch (_) { closing = null; }
    return Promise.resolve(closing).catch(function () {}).then(show);
  }

  function csrfToken() {
    var m = document.cookie.match(/(?:^|;\s*)bl_csrf=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : '';
  }

  // ── the panel ──────────────────────────────────────────────────────────────
  // Everything is looked up INSIDE root, so the modal and the page share one
  // implementation. Returns the handful of hooks the frame needs.
  function mount(root) {
    var messages = root.querySelector('#cz-messages');
    var form = root.querySelector('#cz-form');
    var input = root.querySelector('#cz-input');
    var send = root.querySelector('#cz-send');
    var mic = root.querySelector('#cz-mic');
    var discovery = null;
    var busy = false;
    var history = [];   // this visit's turns, replayed for conversational context
    var historyReady = Promise.resolve();
    // After a reload (the voice screen's own pull-to-refresh, or the reload
    // that brings the mic back after the phone was locked) the transcript is
    // on screen but the model's memory would be empty — "yes, do that" would
    // have no "that" (Kris 2026-09-24: "losing context on refresh if user is
    // in the middle of a task"). The recent turns come back from the server:
    // the last 8 rounds (16 messages), and only from the last three hours.
    // The context window, one number everywhere (Kris 2026-09-24): the turns a
    // reload brings back, the turns sent with each question, the Context bar.
    // Counted in ROUNDS — one of yours and her answer (Kris 2026-09-24: "it's
    // classing each person message as a turn, so the context fills in 6").
    // 8 rounds = the last 16 messages, sent with every question. That is what the
    // server keeps (MAX_HISTORY_TURNS in src/app/concierge_routes.py): the chip must
    // not promise more than reaches the model. Change them together.
    var CONTEXT_ROUNDS = 8, CONTEXT_TURNS = CONTEXT_ROUNDS * 2;
    var HISTORY_RECENT_MS = 3 * 60 * 60 * 1000, HISTORY_SEED_TURNS = CONTEXT_TURNS;
    // "New task" (Kris 2026-09-24): everything said before the tap is set
    // aside — this visit's history empties, and a reload on this device
    // brings back nothing older than the tap. The chat itself keeps it all.
    var CONTEXT_FROM_KEY = 'blam8.czContextFrom';
    function contextFrom() {
      try { return parseInt(localStorage.getItem(CONTEXT_FROM_KEY) || '0', 10) || 0; } catch (e) { return 0; }
    }
    function newTask() {
      history.length = 0;
      try { localStorage.setItem(CONTEXT_FROM_KEY, String(Date.now())); } catch (e) {}
      var line = document.createElement('div');
      line.className = 'cz-new-task-line';
      line.textContent = 'New task';
      messages.appendChild(line);
      scrollDown();
      if (voiceLiveInstance && voiceLiveInstance.isOpen()) {
        clearVoiceCard();
        waitingOpen = false;
        doneOpen = false;
        paintBadge();
        paintDoneBadge();
        // The screen empties too (Kris 2026-09-24: "doesn't clear the text").
        if (activeVoiceMode === 'gptlive' && voiceLiveInstance.clearScreen) {
          voiceLiveInstance.clearScreen('Cleared context, reconnecting.');
        }
      }
    }
    function seedHistory() {
      var controller = new AbortController();
      var timeout = setTimeout(function () { controller.abort(); }, 5000);
      return fetch('/concierge/business/history?context_only=true', { credentials: 'same-origin', signal: controller.signal })
        .then(function (r) { return r.ok ? r.json() : { messages: [] }; })
        .then(function (d) {
          if (history.length) return;   // a turn already happened here: it wins
          var cutoff = Math.max(Date.now() - HISTORY_RECENT_MS, contextFrom());
          var recent = (d.messages || []).filter(function (m) {
            var t = m.created_at ? Date.parse(m.created_at) : NaN;
            return m.role && m.content && m.content.indexOf('[Voice transcript segment — provisional; playback unverified]') !== 0 && (isNaN(t) || t >= cutoff);
          }).slice(-HISTORY_SEED_TURNS);
          recent.forEach(function (m) {
            history.push({ role: m.role, content: m.content, lookups: lookupsOf(m.trail), at: m.created_at || null });
          });
        })
        .catch(function () {})
        .finally(function () { clearTimeout(timeout); });
    }

    function scrollDown() { messages.scrollTop = messages.scrollHeight; }
    // A long reply is read from its top (Kris): when a newly delivered assistant bubble is taller than the
    // visible chat area, put its top edge at the top of the area, under the same padding the first message
    // has, instead of leaving the end in view. "Taller" = the bubble's height against messages.clientHeight,
    // which already shrinks with the on-screen keyboard (fitToKeyboard). A reply that fits keeps today's
    // scroll-to-bottom. The text chat delivers each reply whole (only status lines stream), so this runs
    // once per reply, and once for a long answer that arrives while the live voice screen closes. Wherever the
    // reader happens to be scrolled (Kris 2026-10-16: "regardless"): a reply longer than the area lands at its top.
    // History loaded on open never calls this.
    function pinLongReply(node) {
      if (!node) return;
      if (node.offsetHeight <= messages.clientHeight) return;
      var cs = getComputedStyle(messages), ns = getComputedStyle(node);
      var top = node.getBoundingClientRect().top - messages.getBoundingClientRect().top + messages.scrollTop;
      var buffer = (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.scrollPaddingTop) || 0) + (parseFloat(ns.scrollMarginTop) || 0);
      messages.scrollTop = Math.max(0, top - buffer);
    }
    function markOrigin(node, origin, turnId, role) {
      origin = ['typed','voice','live'].indexOf(origin) >= 0 ? origin : 'unknown';
      node.setAttribute('data-cz-origin', origin);
      node.setAttribute('data-cz-turn-id', turnId || '');
      node.setAttribute('data-cz-role', role || 'assistant');
      node.setAttribute('data-cz-created', new Date().toISOString());
      var label = node.querySelector(':scope > .cz-origin-label') || document.createElement('span');label.className='cz-origin-label';
      label.textContent={typed:'Typed',voice:'Voice',live:'Live',unknown:'Unknown origin'}[origin];
      node.insertBefore(label,node.firstChild);
      return node;
    }
    var activeVoiceVisitId = '', activeVoiceMode = '';
    var modeObserver = null;
    // The newest section's gradient carries on under the composer on a phone (panel CSS): its mode picks the colour, its height places
    // the next stretch of the same gradient, so the join does not show. Kept current by a ResizeObserver on that one section.
    var tailSection = null;
    var tailObserver = window.ResizeObserver ? new ResizeObserver(function () { fitTail(); }) : null;
    function fitTail() {
      if (!tailSection || !tailSection.isConnected) { root.removeAttribute('data-cz-tail-mode'); return; }
      root.style.setProperty('--cz-last-h', tailSection.offsetHeight + 'px');
    }
    function setTail(sec) {
      if (tailSection !== sec) {
        if (tailSection && tailObserver) tailObserver.unobserve(tailSection);
        tailSection = sec;
        if (sec && tailObserver) tailObserver.observe(sec);
      }
      if (sec) root.setAttribute('data-cz-tail-mode', sec.getAttribute('data-cz-mode') || 'unknown'); else root.removeAttribute('data-cz-tail-mode');
      fitTail();
    }
    function transcriptItems() {
      var items = [];
      Array.prototype.forEach.call(messages.querySelectorAll(':scope > *'), function (node) {
        if (node.classList.contains('cz-mode-section')) {
          Array.prototype.forEach.call(node.querySelectorAll(':scope > *'), function (item) { items.push(item); });
        } else items.push(node);
      });
      return items;
    }
    function paintModeBlocks() {
      if (modeObserver) modeObserver.disconnect();
      var items = transcriptItems(), userOrigins = Object.create(null), turnOrigins = Object.create(null);
      function known(origin) { return ['typed', 'voice', 'live'].indexOf(origin) >= 0; }
      items.forEach(function (node) {
        var turn = node.getAttribute('data-cz-turn-id'), origin = node.getAttribute('data-cz-origin');
        if (turn && known(origin)) {
          turnOrigins[turn] = origin;
          if (node.getAttribute('data-cz-role') === 'user') userOrigins[turn] = origin;
        }
      });
      var previousMode = '', run = 0, section = null, used = [], ordered = [];
      items.forEach(function (node) {
        if (node === waitDotRow) return;
        var turn = node.getAttribute('data-cz-turn-id') || node.getAttribute('data-cz-owner-turn');
        // A response belongs to the input it answers. Never infer old input
        // modes from authors, current settings, nearby text or message content.
        var ownOrigin = node.getAttribute('data-cz-origin');
        var origin = (turn && userOrigins[turn]) || (known(ownOrigin) && ownOrigin) ||
          (turn && turnOrigins[turn]) || 'unknown';
        var mode = origin === 'typed' ? 'text' : origin === 'voice' ? 'live' : origin === 'live' ? 'vc' : 'unknown';
        if (!node.hasAttribute('data-cz-origin') && !turn) {
          ordered.push(node); section = null; previousMode = ''; return;
        }
        var begins = !section || mode !== previousMode;
        if (begins) {
          run++;
          var parent = node.parentElement;
          section = parent && parent.classList.contains('cz-mode-section') &&
            parent.getAttribute('data-cz-mode') === mode && used.indexOf(parent) < 0 ? parent : document.createElement('section');
          section.className = 'cz-mode-section';
          section.setAttribute('data-cz-mode', mode);
          section.setAttribute('data-cz-mode-run', String(run));
          section.setAttribute('aria-label', {text:'Text',live:'Live',vc:'VC',unknown:'Unknown origin'}[mode]);
          used.push(section); ordered.push(section);
        }
        node.setAttribute('data-cz-mode', mode);
        node.setAttribute('data-cz-mode-run', String(run));
        node.classList.toggle('cz-mode-item', node.hasAttribute('data-cz-origin'));
        node.classList.remove('cz-mode-start');
        node.classList.toggle('cz-mode-opening', begins);
        var label = node.querySelector(':scope > .cz-origin-label');
        if (label) {
          // The section's tag says the mode once for the whole block; no per-message repeat (Kris 2026-10-16).
          var labelText = {text:'Text',live:'Live',vc:'VC',unknown:'Unknown origin'}[mode];
          if (label.textContent !== labelText) label.textContent = labelText;
          label.classList.add('cz-mode-label');
          label.hidden = !begins;
        }
        if (node.parentElement !== section) section.appendChild(node);
        previousMode = mode;
      });
      messages.querySelectorAll(':scope > .cz-mode-section').forEach(function (old) { if (used.indexOf(old) < 0) old.remove(); });
      var cursor = messages.firstElementChild;
      ordered.forEach(function (node) {
        if (node !== cursor) messages.insertBefore(node, cursor);
        cursor = node.nextElementSibling;
      });
      // The newest tinted section runs on down behind the composer (phone, see _concierge_panel.html). Only when it is the last
      // thing in the thread: a card after it keeps the tint ending where it does today. The wait-dot row does not count.
      var newestSection = null;
      messages.querySelectorAll(':scope > .cz-mode-section').forEach(function (sec) { newestSection = sec; });
      var flush = !!newestSection;
      for (var after = newestSection && newestSection.nextElementSibling; after; after = after.nextElementSibling) {
        if (after !== waitDotRow && !after.hidden) { flush = false; break; }
      }
      messages.querySelectorAll(':scope > .cz-mode-section').forEach(function (sec) { sec.classList.toggle('cz-mode-last', flush && sec === newestSection); });
      setTail(flush ? newestSection : null);
      fitEdge();
      if (modeObserver) modeObserver.observe(messages, { childList: true, subtree: true });
    }
    function bubble(role, text, origin, turnId) {
      var wrap = document.createElement('div');
      if (role === 'user') {
        wrap.className = 'flex flex-col items-end';
        var b = document.createElement('div');
        b.className = 'cz-message-bubble cz-message-user rounded-2xl bg-brand text-white px-4 py-2.5 text-sm max-w-[95%] whitespace-pre-wrap';
        b.textContent = text;
        wrap.appendChild(b);
      } else {
        wrap.className = 'cz-message-bubble cz-message-assistant rounded-2xl bg-white border border-slate-200 px-4 pt-3 pb-4 text-sm text-slate-700 max-w-[95%] whitespace-pre-wrap';
        setRich(wrap, text);
      }
      markOrigin(wrap,origin,turnId,role);
      messages.appendChild(wrap);
      paintModeBlocks();
      scrollDown();
      return wrap;
    }
    var historyReadGeneration = 0, historyLifecycle = 0;
    function hydrateTaskSlots(host) {
      Array.prototype.slice.call(host.querySelectorAll('[data-cz-tasks-json]')).forEach(function (slot) {
        try {
          var batch=JSON.parse(slot.getAttribute('data-cz-tasks-json'));
          var existing=Array.prototype.find.call(root.querySelectorAll('[data-cz-tasks]'),function(node){return node.getAttribute('data-cz-tasks')===batch.batch_id;});
          if(existing&&existing._czStub){(batch.tasks||[]).forEach(applyTask);slot.remove();}   // already open on the right
          else if(existing){(batch.tasks||[]).forEach(applyTask);slot.replaceWith(existing);}
          else slot.replaceWith(taskListNode(batch));
        } catch(e) {}
      });
    }
    function mergeHistory(data) {
      var addedMessages = false;
      var parsed=document.createElement('template');parsed.innerHTML=data.html||'';
      // Provisional observed groups are display-only and can coalesce as words arrive.
      messages.querySelectorAll('[data-cz-observation]').forEach(function(node){node.remove();});
      Array.prototype.slice.call(parsed.content.querySelectorAll('[data-cz-message-id]')).forEach(function(row){
        var id=row.getAttribute('data-cz-message-id');if(!id)return;
        var existing=Array.prototype.find.call(messages.querySelectorAll('[data-cz-message-id]'),function(node){return node.getAttribute('data-cz-message-id')===id;});
        if(existing){
          existing.setAttribute('data-cz-created',row.getAttribute('data-cz-created')||'');
          existing.setAttribute('data-cz-order',row.getAttribute('data-cz-order')||'0');
          existing.setAttribute('data-cz-visit-id',row.getAttribute('data-cz-visit-id')||'');
          existing.setAttribute('data-cz-origin',row.getAttribute('data-cz-origin')||'unknown');
          existing.setAttribute('data-cz-turn-id',row.getAttribute('data-cz-turn-id')||'');
          var words=existing.querySelector('[data-native-segment] p'), fresh=row.querySelector('[data-native-segment] p');
          if(words&&fresh)words.textContent=fresh.textContent;
          hydrateTaskSlots(row);
          var task=row.querySelector('[data-cz-tasks]');if(task)existing.appendChild(task);
          return;
        }
        var turn=row.getAttribute('data-cz-turn-id'),role=row.getAttribute('data-cz-role');
        if(turn)Array.prototype.slice.call(messages.querySelectorAll('[data-cz-turn-id]')).forEach(function(node){
          if(node.getAttribute('data-cz-turn-id')===turn && node.getAttribute('data-cz-role')===role && !node.hasAttribute('data-cz-message-id'))node.remove();
        });
        addedMessages = true;
        messages.appendChild(row);
        row.querySelectorAll('[data-cz-md]').forEach(function(node){setRich(node,node.textContent);});
        hydrateTaskSlots(row);
        if(turn && role==='assistant')Array.prototype.slice.call(messages.querySelectorAll('[data-cz-owner-turn]')).forEach(function(node){if(node.getAttribute('data-cz-owner-turn')===turn)row.appendChild(node);});
      });
      // Stable server timestamps/IDs order rich messages and supplementary observations.
      Array.prototype.slice.call(messages.querySelectorAll('[data-cz-message-id]')).sort(function(a,b){
        return (Date.parse(a.getAttribute('data-cz-created'))||0)-(Date.parse(b.getAttribute('data-cz-created'))||0) || Number(a.getAttribute('data-cz-order')||0)-Number(b.getAttribute('data-cz-order')||0) || a.getAttribute('data-cz-message-id').localeCompare(b.getAttribute('data-cz-message-id'));
      }).forEach(function(node){messages.appendChild(node);});
      paintModeBlocks();
      if (addedMessages && discovery) discovery.hide();
      scrollDown();
    }
    function refreshHistory(opts) {
      var generation=++historyReadGeneration;
      return fetch('/concierge/business/history',{credentials:'same-origin'}).then(function(r){if(!r.ok)throw Error('History unavailable');return r.json();}).then(function(data){
        if(generation!==historyReadGeneration || busy || (voiceLiveInstance&&voiceLiveInstance.isOpen()))return;
        var answers = messages.querySelectorAll('.cz-message-assistant').length;
        mergeHistory(data);
        // Only when the live voice screen has just closed: a new, longer-than-the-area answer lands at its top.
        if (opts && opts.pinNewest) {
          var now = messages.querySelectorAll('.cz-message-assistant');
          if (now.length > answers) pinLongReply(now[now.length - 1]);
        }
      }).catch(function(){});
    }
    var sharedHistoryReading=false;
    function syncSharedHistory(){
      if(sharedHistoryReading || !root.isConnected || document.visibilityState==='hidden' || busy ||
          (root.getClientRects && !root.getClientRects().length) ||
          (voiceLiveInstance && voiceLiveInstance.isOpen()))return;
      sharedHistoryReading=true;
      refreshHistory().finally(function(){sharedHistoryReading=false;});
    }
    window.addEventListener('blam8:device-sync',syncSharedHistory);
    window.addEventListener('online',syncSharedHistory);
    document.addEventListener('visibilitychange',syncSharedHistory);
    setInterval(syncSharedHistory,3000);
    function linkRow(links) {
      var row = document.createElement('div');
      row.className = 'mt-2 -mb-1 flex flex-wrap gap-2 whitespace-normal';
      row.setAttribute('data-cz-links', '');   // the live screen lifts this row out of the cards and stands it under the mic line
      (links || []).forEach(function (l) {
        if (!l || !l.href || l.href.charAt(0) !== '/') return;
        var a = document.createElement('a');
        a.href = l.href;
        keepPlatformRecordInAnotherTab(a, l.href);
        a.className = 'inline-flex items-center gap-1 rounded-lg bg-brand-50 text-brand px-2.5 py-1 text-xs font-semibold hover:bg-blue-100 transition';
        a.textContent = (l.label || 'Open') + ' →';
        row.appendChild(a);
      });
      return row;
    }
    function addLinks(el, links) {
      if (!links || !links.length) return;
      el.appendChild(linkRow(links));
      scrollDown();
    }
    function setRich(el, text) {
      // Minimal markdown: **bold** and *italic* — the model writes both despite
      // plain-text nudges. Built with DOM nodes (textContent), never innerHTML,
      // so tool-sourced data can't inject markup.
      el.textContent = '';
      String(text || '').split('**').forEach(function (p, i) {
        if (!p) return;
        var host = el;
        if (i % 2 === 1) { host = document.createElement('strong'); el.appendChild(host); }
        p.split(/\*(?=\S)([^*]+?)(?<=\S)\*/).forEach(function (q, j) {
          if (!q) return;
          if (j % 2 === 1) { var em = document.createElement('em'); em.textContent = q; host.appendChild(em); }
          else host.appendChild(document.createTextNode(q));
        });
      });
    }
    // Replayed history arrives server-rendered as plain text (raw ** and * on
    // every reload until 2026-09-06) — give it the same treatment on load.
    root.querySelectorAll('[data-cz-md]').forEach(function (el) { setRich(el, el.textContent); });
    paintModeBlocks();
    modeObserver = new MutationObserver(paintModeBlocks);
    modeObserver.observe(messages, { childList: true, subtree: true });
    // Phone (Kris 2026-10-16): the newest tinted section's gradient carries on behind the composer and the two links to the foot of the
    // screen. The footer's height is the one number CSS cannot know (waiting chips, the explorer), so it is read here into --cz-foot-h;
    // data-cz-tail arms the rules in the panel CSS.
    function fitFoot() {
      var top = null;
      for (var below = messages.nextElementSibling; below && top === null; below = below.nextElementSibling) {
        if (below.hidden || !below.offsetHeight) continue;
        var pos = getComputedStyle(below).position;
        if (pos === 'absolute' || pos === 'fixed') continue;
        top = below.getBoundingClientRect().top;
      }
      root.style.setProperty('--cz-foot-h', Math.round(top === null ? 0 : Math.max(0, root.getBoundingClientRect().bottom - top)) + 'px');
    }
    // The fade where the chat meets the composer (panel CSS) is as deep as the chat that lies below the edge, up to 56px.
    function fitEdge() {
      var hidden = Math.max(0, Math.min(56, Math.round(messages.scrollHeight - messages.scrollTop - messages.clientHeight)));
      root.style.setProperty('--cz-edge', hidden + 'px');
      root.style.setProperty('--cz-tail-o', String(Math.round((1 - hidden / 56) * 100) / 100));
    }
    messages.addEventListener('scroll', fitEdge, { passive: true });
    if (window.ResizeObserver) {
      var footObserver = new ResizeObserver(fitFoot);
      footObserver.observe(root);
      for (var part = root.firstElementChild; part; part = part.nextElementSibling) { if (part !== messages) footObserver.observe(part); }
      new ResizeObserver(fitEdge).observe(messages);
      fitFoot();
      root.setAttribute('data-cz-tail', '');
    }
    fitEdge();
    // ── ghost trail: lookups become pills that STAY above the answer ─────────
    // The lookups behind a reply ride along in the history we send back, so
    // the next turn knows its earlier answers were checked, not guessed.
    function lookupsOf(trail) {
      return (trail || []).slice(0, 8).map(function (t) {
        var line = t.detail ? t.label + ' (' + t.detail + ')' : t.label;
        // What it found (a customer's address, a booking ref) - reused next turn.
        return t.found ? line + ' → found ' + t.found : line;
      }).filter(Boolean);
    }

    function trailStrip(origin, turnId) {
      var wrap = document.createElement('div');
      wrap.className = 'max-w-[95%] space-y-1';
      wrap.setAttribute('data-cz-trail', '');
      var row = document.createElement('div');
      row.className = 'cz-tool-trail';
      var detail = document.createElement('p');
      detail.className = 'hidden text-[11px] text-slate-400 italic px-1 break-words';
      detail.setAttribute('data-cz-trail-detail', '');
      wrap.appendChild(row);
      wrap.appendChild(detail);
      if (origin) markOrigin(wrap, origin, turnId, 'assistant');
      messages.appendChild(wrap);
      paintModeBlocks();
      scrollDown();
      return { wrap: wrap, row: row };
    }
    function trailPill(strip, item, active) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'cz-tool-chip' + (item.detail ? '' : ' cursor-default');
      if (active) b.setAttribute('data-cz-tool-active', '');
      b.textContent = active ? (item.hint || 'Searching your data').replace(/[.\u2026\s]+$/, '')
                             : (item.label || 'Searched');
      if (item.detail) {
        b.setAttribute('data-cz-pill', '');
        b.setAttribute('data-cz-detail', item.detail);
      }
      if (strip.row.childElementCount) strip.row.appendChild(document.createTextNode(', '));
      strip.row.appendChild(b);
      scrollDown();
      return b;
    }
    function settlePill(pill, item) {
      if (!pill) return;
      pill.removeAttribute('data-cz-tool-active');
      pill.textContent = (item && item.label) || 'Searched';
    }
    function finishTrail(strip) {
      if (!strip || strip.row.hasAttribute('data-cz-tools-finished')) return;
      strip.row.setAttribute('data-cz-tools-finished', '');
      strip.row.appendChild(document.createTextNode('.'));
    }
    // One delegated handler covers server-rendered history pills AND live ones.
    messages.addEventListener('click', function (e) {
      var pill = e.target.closest('[data-cz-pill]');
      if (!pill) return;
      var strip = pill.closest('[data-cz-trail]');
      var detail = strip && strip.querySelector('[data-cz-trail-detail]');
      if (!detail) return;
      var open = !detail.classList.contains('hidden') && detail.getAttribute('data-for') === pill.textContent;
      detail.setAttribute('data-for', pill.textContent);
      detail.textContent = 'Looked for: “' + pill.getAttribute('data-cz-detail') + '”';
      detail.classList.toggle('hidden', open);
    });
    function statusBubble(origin, turnId) {
      var el = document.createElement('div');
      el.className = 'cz-tool-trail max-w-[95%]';
      var words = document.createElement('span');
      words.className = 'cz-tool-chip';
      words.setAttribute('data-cz-tool-active', '');
      words.textContent = 'Looking at your request';
      el.appendChild(words);
      el.setAttribute('data-cz-pending', '');
      markOrigin(el, origin, turnId, 'assistant');
      messages.appendChild(el);
      scrollDown();
      return el;
    }

    // ── action proposal card (Phase 2): nothing runs until Confirm ───────────
    var KIND_LABELS = {
      draft_reply: 'Email draft', move_booking: 'Booking move', cancel_booking: 'Booking cancel', confirm_booking: 'Booking confirm', block_date: 'Diary block', unblock_date: 'Diary reopen', assign_person: 'On the job', update_contact: 'Record update', update_dependant: 'Record update', request_renewal: 'Renewal request', change_stay: 'Stay dates',
      update_deal_stage: 'Deal stage', update_deal_value: 'Deal value',
      create_followup: 'Follow-up',
      create_contact: 'New contact', add_kb_entry: 'Knowledge entry',
      invoice_create: 'New invoice', invoice_send: 'Send invoice',
      invoice_mark_paid: 'Mark invoice paid', invoice_reminder_draft: 'Payment reminder',
      bulk: 'Several changes'
    };

    // Builds the card without placing it: the transcript appends it, and the
    // voice overlay's second page holds its own copy.
    var DECLINED_LINE = 'No problem — nothing changed.';
    // Why a card did not run, in words (Kris 2026-10-16: the card said only "not run" for one that had expired).
    var SUPERSEDED_LINE = 'Replaced by a newer card — nothing changed.';
    var NOT_RUN_WHY = {
      expired: 'expired. A card must be confirmed within 5 minutes.',
      failed: 'the change did not go through.'
    };
    // Fade something out after `wait` ms, then take it away (or just hide it).
    function fadeAway(el, then, wait, keep) {
      setTimeout(function () {
        el.style.transition = 'opacity 500ms ease';
        el.style.opacity = '0';
        setTimeout(function () {
          if (keep) el.style.display = 'none'; else if (el.parentNode) el.parentNode.removeChild(el);
          if (then) then();
        }, 520);
      }, wait == null ? 3000 : wait);
    }
    var UNKNOWN_PROPOSAL = "The outcome isn't confirmed. Work may have happened — check the record before taking another action.";
    async function proposalRequest(url, options) {
      var controller = new AbortController(), timer;
      try {
        return await Promise.race([
          (async function () {
            var res = await fetch(url, Object.assign({}, options, { signal: controller.signal }));
            if (!res.ok) throw new Error('Response unavailable');
            return res.json();
          })(),
          new Promise(function (_, reject) {
            timer = setTimeout(function () { controller.abort(); reject(new Error('Response timed out')); }, 10000);
          })
        ]);
      } finally { clearTimeout(timer); }
    }
    function validProposalResult(data, id) {
      return data && data.proposal_id === id && typeof data.summary === 'string' &&
        ['pending', 'executing', 'completed', 'failed', 'unknown', 'cancelled', 'expired', 'superseded'].indexOf(data.state) !== -1 &&
        data.ok === (data.state === 'completed');
    }
    async function checkProposal(id, reconcile) {
      try {
        var data = await proposalRequest(reconcile ? '/concierge/business/reconcile' : '/concierge/business/status/' + encodeURIComponent(id),
          reconcile ? { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken() },
            body: JSON.stringify({ proposal_id: id }) } : { credentials: 'same-origin' });
        if (!validProposalResult(data, id)) throw new Error('Invalid receipt');
        return data;
      } catch (e) { return { proposal_id: id, ok: false, state: 'unknown', summary: UNKNOWN_PROPOSAL }; }
    }
    var pendingConfirmations = 0;
    async function submitProposal(p, input, endpoint) {
      pendingConfirmations += 1;
      try {
        var data = await proposalRequest(endpoint || '/concierge/business/execute', {
          method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken() },
          body: JSON.stringify(Object.assign({ proposal_id: p.proposal_id, display_origin: p.origin || 'unknown' }, input || {}))
        });
        if (!validProposalResult(data, p.proposal_id)) throw new Error('Invalid receipt');
        return data;
      } catch (e) {
        // A lost response is never authority to dispatch again.
        return await checkProposal(p.proposal_id, false);
      } finally {
        pendingConfirmations -= 1;
      }
    }
    function proposalCardNode(p) {
      // The draft card's shape, buttons and all (Kris 2026-09-24): what it
      // will do on the top line with 👍 and the red bin, the details under
      // it, the state bottom right. Same endpoints, same voice yes/no.
      var card = document.createElement('div');
      card.className = 'cz-draft cz-proposal';
      card.setAttribute('data-proposal-id',p.proposal_id);
      var top = document.createElement('div');
      top.className = 'cz-draft-head';
      var body = document.createElement('p');
      body.className = 'cz-proposal-title';
      body.textContent = p.summary || '';
      var acts = document.createElement('div');
      acts.className = 'cz-draft-acts';
      top.appendChild(body);
      top.appendChild(acts);
      card.appendChild(top);
      if (p.type === 'recovery') {
        var provenance = document.createElement('p'); provenance.className = 'cz-proposal-detail';
        provenance.textContent = 'Earlier action · ' + waitingDate(p) + ' · ' + (p.action_summary || 'Saved result unverified');
        card.appendChild(provenance);
      }
      (p.details || []).forEach(function (line) {
        var d = document.createElement('p');
        d.className = 'cz-proposal-detail';
        d.textContent = line;
        card.appendChild(d);
      });
      // Sleek by default (Kris 2026-10-16): the change on one line with the thumbs-up and the bin. Tap the title, or
      // anywhere on the card except the buttons, to open the details; tap again to close them.
      if ((p.details && p.details.length) || p.type === 'recovery') {
        card.setAttribute('data-collapsible', '');
        card.setAttribute('data-open', 'false');
        body.setAttribute('role', 'button');
        body.tabIndex = 0;
        body.setAttribute('aria-expanded', 'false');
        var toggleDetail = function () {
          var open = card.getAttribute('data-open') !== 'true';
          card.setAttribute('data-open', open ? 'true' : 'false');
          body.setAttribute('aria-expanded', open ? 'true' : 'false');
        };
        card.addEventListener('click', function (e) {
          if (e.target.closest && e.target.closest('button, a, textarea, input, label, select')) return;
          toggleDetail();
        });
        body.addEventListener('keydown', function (e) {
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleDetail(); }
        });
      }
      var head = document.createElement('p');
      head.className = 'cz-draft-label';
      // No caption while it waits (Kris 2026-09-24: "remove the … confirm to
      // run text") — the 👍 and the bin say it. The footer only appears to say
      // what happened: Running…, done ✓, not run.
      var HEAD_READY = '';
      var foot = document.createElement('div');
      foot.className = 'cz-draft-foot cz-proposal-foot';
      function setState(text) { head.textContent = text; foot.hidden = !text; }
      setState(HEAD_READY);
      var ta = null;
      if (p.draft_text) {
        ta = document.createElement('textarea');
        ta.maxLength = 8000;
        ta.className = 'cz-draft-text';
        ta.setAttribute('aria-label', 'The email — edit before confirming');
        ta.value = p.draft_text;
        // No caption under the box (Kris 2026-09-24): the text is plainly
        // editable, and the card's own head says what Confirm does.
        card.appendChild(ta);
      }
      // Bulk (2026-09-06, option 1): every item with its own tick box, one Confirm.
      var boxes = [];
      if (p.kind === 'bulk' && Array.isArray(p.items)) {
        var list = document.createElement('div');
        list.className = 'space-y-1.5';
        p.items.forEach(function (it, idx) {
          var lab = document.createElement('label');
          lab.className = 'flex items-start gap-2 rounded-lg bg-white/70 px-2 py-1.5 ' + (it.ok ? '' : 'opacity-70');
          var cb = document.createElement('input');
          cb.type = 'checkbox'; cb.checked = !!it.ok; cb.disabled = !it.ok; cb.className = 'mt-0.5';
          cb.setAttribute('data-idx', String(idx));
          boxes.push(cb);
          var txt = document.createElement('div');
          txt.className = 'min-w-0 flex-1';
          var s1 = document.createElement('p');
          s1.className = 'text-sm font-medium whitespace-pre-wrap break-words';
          s1.textContent = it.summary || (KIND_LABELS[it.kind] || it.kind);
          txt.appendChild(s1);
          if (it.ok) {
            (it.details || []).slice(0, 3).forEach(function (line) {
              var d = document.createElement('p');
              d.className = 'text-[11px] text-slate-500 whitespace-pre-wrap break-words';
              d.textContent = line; txt.appendChild(d);
            });
          } else {
            var er = document.createElement('p');
            er.className = 'text-[11px] text-rose-600 whitespace-pre-wrap break-words';
            er.textContent = "Can't be done: " + (it.error || 'not prepared'); txt.appendChild(er);
          }
          lab.appendChild(cb); lab.appendChild(txt); list.appendChild(lab);
        });
        card.appendChild(list);
      }
      function selectedIdx() {
        return boxes.filter(function (b) { return b.checked && !b.disabled; })
                    .map(function (b) { return parseInt(b.getAttribute('data-idx'), 10); });
      }
      var note = document.createElement('p');
      note.className = 'hidden text-xs text-rose-600';
      var ok = document.createElement('button');
      ok.type = 'button';
      ok.className = 'cz-draft-btn cz-draft-send';
      ok.setAttribute('aria-label', 'Confirm');
      ok.title = 'Confirm';
      ok.innerHTML = THUMB_UP;
      var no = document.createElement('button');
      no.type = 'button';
      no.className = 'cz-draft-btn cz-draft-bin';
      no.setAttribute('aria-label', 'Cancel — nothing changes');
      no.title = 'Cancel';
      no.innerHTML = SKIP_BIN;
      var groupCount = p.kind === 'bulk' ? (p.items || []).filter(function(item){return item.ok;}).length : 0;
      if(groupCount > 1){
        no.setAttribute('aria-label','Cancel entire group — '+groupCount+' prepared items');
        no.title='Cancel entire group';
        no.style.width='auto';no.style.padding='0 0.5rem';no.style.gap='0.25rem';no.style.fontSize='11px';
        var groupLabel=document.createElement('span');groupLabel.textContent='Cancel group ('+groupCount+')';no.appendChild(groupLabel);
      }
      acts.appendChild(ok);
      acts.appendChild(no);

      function finish(text, okOutcome, link, results, why) {
        card._cz.settled = true;
        if (!okOutcome && (text === DECLINED_LINE || why === 'superseded')) {
          // A "no" or the bin (Kris 2026-09-25): the card becomes just the one
          // line — not its old text again — and fades away after 3 s.
          card.textContent = '';
          card.className = 'rounded-2xl border border-slate-200 bg-white px-3 py-1.5 text-sm text-slate-500 max-w-[85%]';
          card.setAttribute('data-state', 'skipped');
          card.textContent = why === 'superseded' ? SUPERSEDED_LINE : text;
          loadWaiting();
          fadeAway(card, function () {
            if (voiceStage.cardNode === card) {
              voiceStage.cardNode = null; voiceStage.cardAnchor = null; voiceStage.proposal = null;
              if (voiceLiveInstance && voiceLiveInstance.isOpen()) voiceLiveInstance.refreshStage();
            }
          });
          return;
        }
        acts.remove();
        if (ta) ta.remove();
        note.remove();
        boxes.forEach(function (b) { b.disabled = true; });
        card.setAttribute('data-state', okOutcome ? 'sent' : 'skipped');
        setState(okOutcome ? (KIND_LABELS[p.kind] || (p.kind === 'bulk' ? 'Changes' : 'Action')) + ' · done ✓'
          : NOT_RUN_WHY[why] ? 'Not run — ' + NOT_RUN_WHY[why]
          : (KIND_LABELS[p.kind] || (p.kind === 'bulk' ? 'Changes' : 'Action')) + ' · not run');
        body.textContent = text;
        (results || []).forEach(function (r) {
          var line = document.createElement('p');
          line.className = 'text-xs whitespace-pre-wrap break-words ' + (r.ok ? 'text-emerald-700' : 'text-rose-600');
          line.textContent = (r.ok ? '✓ ' : '✗ ') + (r.summary || '');
          card.appendChild(line);
        });
        // The state sits right under the title, and the link waits inside the details (Kris 2026-10-16: sleeker).
        if (card.insertBefore) card.insertBefore(foot, top.nextSibling);
        if (link && link.href && link.href.charAt(0) === '/') {
          var linkNode = linkRow([link]);
          if (card.hasAttribute && card.hasAttribute('data-collapsible')) linkNode.classList.add('cz-proposal-links');
          card.appendChild(linkNode);
        }
        scrollDown();
        loadWaiting();   // answered: it isn't waiting any more (nor its twins)
        if (okOutcome) {
          loadDone();
          // Dealt with: a card open on the right fades away and shows in Done today instead (Kris 2026-10-16).
          if (card.closest && card.closest('.cz-side-item')) { agentNow.cardDone = true; sideChanged(); fadeAway(card, sideChanged, 5000); }
        }
      }

      var checks = 0, checkButton = null, terminalOutcome = false, partialResults = null;
      function applyOutcome(data) {
        if (terminalOutcome) return;
        if (data.state === 'pending') {
          card._cz.settled = false;
          ok.hidden = false; no.hidden = false;
          if (ta) ta.disabled = false;
          boxes.forEach(function (b, index) { b.disabled = !p.items[index].ok; });
          if (checkButton) { checkButton.remove(); checkButton = null; }
          card.setAttribute('data-state', 'pending');
          body.textContent = p.summary || data.summary;
          card._cz.unlock(); return;
        }
        card._cz.settled = true;
        if (data.state !== 'executing' && data.state !== 'unknown') {
          terminalOutcome = true;
          if (partialResults) partialResults.remove();
          finish(data.state === 'cancelled' ? DECLINED_LINE : data.summary, data.ok, data.link, data.results, data.state);
          return;
        }
        ok.disabled = true; no.disabled = true;
        if (ta) ta.disabled = true;
        boxes.forEach(function (b) { b.disabled = true; });
        card.setAttribute('data-state', data.state);
        setState(data.state === 'executing' ? 'Still running' : 'Outcome unconfirmed');
        body.textContent = data.summary;
        if (partialResults) partialResults.remove();
        partialResults = document.createElement('div');
        (data.results || []).forEach(function (result) {
          var line = document.createElement('p');
          line.className = 'text-xs whitespace-pre-wrap break-words';
          line.textContent = (result.state === 'completed' ? '✓ ' : result.state === 'failed' ? '✗ ' : '? ') + result.summary;
          partialResults.appendChild(line);
        });
        card.appendChild(partialResults);
        ok.hidden = true; no.hidden = true;
        if (!checkButton) {
          checkButton = document.createElement('button'); checkButton.type = 'button';
          checkButton.textContent = 'Check result'; acts.appendChild(checkButton);
          checkButton.addEventListener('click', async function () {
            var button = checkButton; button.disabled = true;
            applyOutcome(await checkProposal(p.proposal_id, true));
            button.disabled = false;
          });
        }
        if (data.link && data.link.href && data.link.href.charAt(0) === '/' && !card._cz.recoveryLink) {
          addLinks(card, [data.link]); card._cz.recoveryLink = true;
        }
        if (data.state === 'executing' && checks++ < 3) {
          setTimeout(async function () { if (card.isConnected) applyOutcome(await checkProposal(p.proposal_id, false)); }, 2000);
        }
        loadWaiting();
      }

      ok.addEventListener('click', async function () {
        ok.disabled = true; no.disabled = true; setState('Running…');
        try {
          // confirm_via 'tap' is what makes this a tap: the server runs a delete (a tap-only kind)
          // for nothing else, so a spoken yes (below) can never stand in for this button.
          var data = await submitProposal(p, { draft_text: ta ? ta.value : '', selected: p.kind === 'bulk' ? selectedIdx() : null, confirm_via: 'tap' });
          applyOutcome(data);
          settledByTap(card, data);
        } catch (e) {
          applyOutcome({ state: 'unknown', summary: UNKNOWN_PROPOSAL });
        }
      });
      no.addEventListener('click', async function () {
        if(groupCount > 1 && !confirm('Cancel all '+groupCount+' prepared items in this group? Tick boxes only choose what Confirm runs. Cancel group cancels every prepared item, including unchecked items; no action will run.'))return;
        ok.disabled = true; no.disabled = true;
        try {
          var data = await submitProposal(p, {}, '/concierge/business/cancel');
          applyOutcome(data);
          settledByTap(card, data);
        } catch (e) { applyOutcome({ state: 'unknown', summary: UNKNOWN_PROPOSAL }); }
      });

      // What the voice path needs to run THIS card the way a tap would: the
      // ticked items of a bulk card and the edited draft text, not the
      // defaults (a spoken yes on a bulk card used to run every item, ticked
      // or not); plus a way to settle it on screen and lock it while it runs.
      card._cz = {
        settled: false,
        selected: function () { return p.kind === 'bulk' ? selectedIdx() : null; },
        draft: function () { return ta ? ta.value : ''; },
        lock: function () { ok.disabled = true; no.disabled = true; setState('Running…'); },
        unlock: function () { ok.disabled = false; no.disabled = false; setState(HEAD_READY); },
        finish: finish,
        applyOutcome: applyOutcome,
      };

      card.appendChild(note);
      foot.appendChild(head);
      card.appendChild(foot);
      if (p.type === 'recovery') applyOutcome(p);
      return card;
    }
    function proposalCard(p) {
      var card=proposalCardNode(p);
      card._czColumn = !!p._czColumn;   // tapped in the column's Waiting box
      card._czHint = p._czColumn && p.notice && !SILENT_NOTICE.test(p.notice) ? p.notice : '';   // only a revised card needs saying why
      addCard(card, (p.summary || 'A change') + ' is ready, shown on the right.');
      scrollDown();return card;
    }

    // ── waiting cards (Kris 2026-09-23) ──────────────────────────────────────
    // "A pending chip, for times like that when something gets ignored. It can
    // be resurfaced at any time until confirmation." One chip per card nobody
    // answered; a tap brings it back rebuilt against today's data (the server
    // re-checks it and it becomes the one live card); ✕ lets it go. The card
    // just shown is not "waiting" for its first five minutes.
    var waitingBox = root.querySelector('#cz-waiting');
    var waitingShortcut = root.querySelector('#cz-waiting-shortcut');
    var waitingItems = [];
    var justShown = {};          // proposal_id → when this screen showed it
    var waitTimer = null;
    function noteShown(p) {
      var id = p && (p.proposal_id || p.log_id);
      if (id) justShown[id] = Date.now();
    }
    var resurfaceNewestOnLoad = false;   // the reload landing asks for the card it left
    function loadWaiting() {
      if (typeof pollTasks === 'function') setTimeout(pollTasks, 600);   // a card answered may settle a task
      clearTimeout(waitTimer);
      waitTimer = setTimeout(function () {
        fetch('/concierge/business/waiting', { credentials: 'same-origin' })
          .then(function (r) { return r.ok ? r.json() : { waiting: [] }; })
          .then(function (d) {
            var now = Date.now();
            waitingItems = (d.waiting || []).filter(function (w) {
              var at = justShown[w.proposal_id || w.log_id];
              return !(at && now - at < 5 * 60 * 1000);
            }).concat(d.recovery || []);
            paintWaiting();
            if (voiceLiveInstance && voiceLiveInstance.isOpen() && voiceLiveInstance.notifyRecords) voiceLiveInstance.notifyRecords('Pending', (d.waiting || []).concat(d.recovery || []));
            if (resurfaceNewestOnLoad) {
              resurfaceNewestOnLoad = false;
              // Mid-task when the phone locked: the card set up in the last
              // ten minutes comes straight back onto the stage.
              var fresh = waitingItems.filter(function (w) {
                var t = w.created_at ? Date.parse(w.created_at) : NaN;
                return w.type !== 'recovery' && !isNaN(t) && Date.now() - t < 10 * 60 * 1000;
              });
              if (fresh.length) resurface(fresh[0]);
            }
          })
          .catch(function () {});
      }, 250);
    }
    // On the voice screen the chips are discreet (Kris 2026-09-23): a circle
    // top-left with the count; tapped, the list appears at the bottom, each
    // line tappable to carry on. Picking one, or a new card, closes the list.
    var waitingOpen = false;
    var waitBadge = null;
    function paintBadge() {
      if (!waitBadge) return;
      var n = waitingItems.length;
      if (!n) waitingOpen = false;
      waitBadge.hidden = !n;
      // Open, the circle is the way out (Kris 2026-09-24): a cross, not the count.
      waitBadge.textContent = waitingOpen && n ? '✕' : String(n);
      waitBadge.setAttribute('aria-label', waitingOpen ? 'Close the waiting list' : waitingCounts());
      waitBadge.setAttribute('aria-expanded', String(waitingOpen && n > 0));
      waitBadge.classList.toggle('is-open', waitingOpen);
    }
    // Its twin (Kris 2026-09-24): a second circle counting what's been DONE
    // today — cards confirmed, emails sent — opening a list of just those
    // actions, "not a whole thread of chat". Each line opens where it landed.
    var doneItems = [];
    var doneOpen = false;
    var doneBadge = null;
    var doneTimer = null;
    function loadDone() {
      clearTimeout(doneTimer);
      doneTimer = setTimeout(function () {
        fetch('/concierge/business/done', { credentials: 'same-origin' })
          .then(function (r) { return r.ok ? r.json() : { done: [] }; })
          .then(function (d) {
            doneItems = d.done || []; paintDoneBadge();
            if (voiceLiveInstance && voiceLiveInstance.isOpen() && voiceLiveInstance.notifyRecords) voiceLiveInstance.notifyRecords('Actions', doneItems);
          })
          .catch(function () {});
      }, 250);
    }
    function paintDoneBadge() {
      if (!doneBadge) return;
      var n = doneItems.length;
      if (!n) doneOpen = false;
      doneBadge.hidden = !n;
      doneBadge.textContent = doneOpen && n ? '✕' : String(n);
      doneBadge.setAttribute('aria-label', doneOpen ? 'Close the done list' : n + ' done today');
      doneBadge.setAttribute('aria-expanded', String(doneOpen && n > 0));
      doneBadge.classList.toggle('is-open', doneOpen);
      if (voiceLiveInstance && voiceLiveInstance.isOpen()) voiceLiveInstance.refreshStage();
    }
    function doneTime(iso) {
      var t = iso ? new Date(iso) : null;
      if (!t || isNaN(t.getTime())) return '';
      var sameDay = t.toDateString() === new Date().toDateString();
      return sameDay ? t.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
                     : t.toLocaleDateString([], { weekday: 'short' }) + ' ' + t.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }
    // The done list is a DECK (Kris 2026-09-24: "the same stacking effect as
    // the Today page, the bottom third of the screen for the whole stack"):
    // one glass card per action, sticky-piled top and bottom as home.html's
    // deck is, newest on top, the same three-state z-order and the frosted
    // words while a card is mostly buried.
    // Kris 2026-09-24: "take less screen space, only show 1 unblurred card at
    // a time, compress the stack height a little". Tighter pile steps than
    // Today's (12/10), and the stage is sized to ONE card plus the bottom
    // pile's edges (sizeStage), so only one face is ever fully shown. Room
    // for the top pile too let the second card cover the newest at rest; the
    // landed edges share the few pixels above the face instead.
    // The pile shows one edge per done action behind the face, up to nine
    // (Kris 2026-09-24: "the stack layers match the done action counter up
    // until 10, then the stack stays that size") — so the steps are thin.
    var DONE_TOP = 4, DONE_TOP_STEP = 5, DONE_BOT_STEP = 5, DONE_LAYERS = 9;
    function doneRow() {
      if (!doneItems.length) return null;
      var deck = document.createElement('div');
      deck.className = 'cz-done-deck';
      var n = doneItems.length;
      var cards = doneItems.map(function (d, i) {
        var ti = Math.min(i, DONE_LAYERS), ri = Math.min(n - 1 - i, DONE_LAYERS);
        var card = document.createElement(d.href && d.href.charAt(0) === '/' ? 'a' : 'div');
        card.className = 'cz-done-card glass-card';
        if (card.tagName === 'A') { card.href = d.href; card.title = 'Open it'; keepPlatformRecordInAnotherTab(card, d.href); }
        card.style.top = (DONE_TOP + ti * DONE_TOP_STEP) + 'px';
        card.style.bottom = (ri * DONE_BOT_STEP) + 'px';
        card.style.zIndex = String(i + 1);
        var face = document.createElement('div');
        face.className = 'cz-done-face';
        var title = document.createElement('p');
        title.className = 'cz-done-title';
        title.textContent = d.summary || 'A change';
        var fact = document.createElement('p');
        fact.className = 'cz-done-fact';
        var what = document.createElement('span');
        what.className = 'cz-done-what';
        what.textContent = '✓ ' + (d.outcome || (d.type === 'sent' ? 'Sent' : 'Done'));
        var when = document.createElement('span');
        when.className = 'cz-done-when';
        when.textContent = doneTime(d.done_at);
        fact.appendChild(what);
        fact.appendChild(when);
        face.appendChild(title);
        face.appendChild(fact);
        card.appendChild(face);
        deck.appendChild(card);
        return card;
      });
      // home.html's restack, on the voice stage's own scroller: waiting cards
      // (on the bottom pile) descend so the next one up shows its face,
      // landed cards (top pile) ascend, a travelling card rides over both.
      // The stage is the voice screen's scroller, not the deck's parent: the
      // deck sits in voiceRenderStage's wrapper, whose box is the whole deck's
      // height — measured against it, every middle card read as travelling
      // and stayed frosted (Kris 2026-09-24: only the first and last sharpen).
      function stageOf() { return deck.closest('.vl-stage-scroll') || deck.parentNode; }
      // One card tall (Kris 2026-09-24: "only show 1 unblurred card at a
      // time"), so the Today page's geometry test can't work here: in a stage
      // one card high the newest card is pinned top AND bottom at once and
      // read as the waiting pile's face forever. The face is picked by how far
      // the deck has scrolled instead — one card's travel per card — landed
      // cards below it in arrival order, waiting ones below it next-up first,
      // and every card but the face frosted.
      function restack() {
        var stage = stageOf();
        if (!stage || !deck.isConnected) return;
        var step = n > 1 ? cards[1].offsetHeight + (parseFloat(getComputedStyle(deck).rowGap) || 0) : 1;
        var face = Math.min(n - 1, Math.max(0, Math.round(stage.scrollTop / Math.max(1, step))));
        cards.forEach(function (c, i) {
          c.style.zIndex = String(i === face ? 600 : (i < face ? i + 1 : 300 - i));
          c.classList.toggle('is-buried', i !== face);
        });
      }
      var queued = false;
      function schedule() {
        if (queued) return;
        queued = true;
        requestAnimationFrame(function () { queued = false; restack(); });
      }
      // The deck is built off-screen; once the stage has placed it, its
      // scroller is the thing that moves the piles.
      function sizeStage(stage) {
        var cardH = cards[0].offsetHeight, piles = Math.min(n - 1, DONE_LAYERS);
        if (!cardH) return;
        stage.style.setProperty('--cz-deck-h',
          (DONE_TOP + cardH + piles * Math.max(DONE_TOP_STEP, DONE_BOT_STEP)) + 'px');
      }
      requestAnimationFrame(function () {
        var stage = stageOf();
        if (stage) sizeStage(stage);
        if (stage && !stage._czDeckBound) {
          stage._czDeckBound = true;
          stage.addEventListener('scroll', function () { if (deck.isConnected) schedule(); }, { passive: true });
        }
        restack();
      });
      return deck;
    }
    // A badge tap must show its list: a settled card still on the stage (sent,
    // done) is parked back into the chat first, since the stage shows one thing
    // (Kris 2026-09-24: the sent email "is blocking the Pending from opening").
    // Kris 2026-09-24: opening a waiting draft and then tapping ⏳ or ✓ did
    // nothing — the draft held the stage. Any card on screen, settled or not,
    // steps aside for the list; an unanswered one is back in the waiting list
    // (nothing lost), a tap brings it back.
    function stageForList() {
      if (voiceStage.cardNode) clearVoiceCard();
    }
    function mountBadge(root) {
      if (root.id !== 'voice-gptlive-root') return;
      var box = document.createElement('div');
      box.className = 'cz-badges';
      waitBadge = document.createElement('button');
      waitBadge.type = 'button';
      waitBadge.className = 'cz-wait-badge';
      waitBadge.hidden = true;
      waitBadge.setAttribute('aria-controls', 'cz-waiting-list');
      waitBadge.setAttribute('aria-expanded', 'false');
      waitBadge.addEventListener('click', function (e) {
        e.stopPropagation();
        waitingOpen = !waitingOpen;
        doneOpen = false;
        stageForList();
        paintBadge();
        paintDoneBadge();
        if (voiceLiveInstance) voiceLiveInstance.refreshStage();
      });
      if (root.id !== 'voice-gptlive-root') {
      doneBadge = document.createElement('button');
      doneBadge.type = 'button';
      doneBadge.className = 'cz-wait-badge cz-done-badge';
      doneBadge.hidden = true;
      doneBadge.setAttribute('aria-controls', 'cz-done-list');
      doneBadge.setAttribute('aria-expanded', 'false');
      doneBadge.addEventListener('click', function (e) {
        e.stopPropagation();
        doneOpen = !doneOpen;
        waitingOpen = false;
        stageForList();
        paintBadge();
        paintDoneBadge();
        if (voiceLiveInstance) voiceLiveInstance.refreshStage();
      });
      }
      box.appendChild(waitBadge);
      if (root.id !== 'voice-gptlive-root') box.appendChild(doneBadge);
      root.appendChild(box);
      paintBadge();
      paintDoneBadge();
      loadDone();
      mountDebrief(root);   // the Debrief button beside the pending-review badge
    }
    function waitingDate(w) {
      var date = new Date(w.created_at || '');
      return isNaN(date.getTime()) ? 'Date unavailable' : date.toLocaleString(undefined,
        { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
    }
    function waitingCounts() {
      var recovery = waitingItems.filter(function (w) { return w.type === 'recovery'; }).length;
      return (waitingItems.length - recovery) + ' awaiting approval / review; ' + recovery + ' unverified results';
    }
    function waitingRow(asList) {
      if (!waitingItems.length) return null;
      var row = document.createElement('div');
      row.className = 'cz-wait-row' + (asList ? ' cz-wait-list' : '');
      if (asList) { row.setAttribute('role', 'region'); row.setAttribute('aria-label', 'Pending items awaiting review'); }
      if (!asList) {
        var label = document.createElement('span');
        label.className = 'cz-wait-label';
        label.textContent = waitingCounts();
        row.appendChild(label);
      }
      var previousGroup = null;
      waitingItems.forEach(function (w) {
        var group = w.type === 'recovery' ? 'Unverified earlier results — check before repeating' : 'Awaiting approval / review';
        if (group !== previousGroup) {
          var heading = document.createElement('p');
          heading.className = 'cz-wait-label'; heading.textContent = group;
          heading.style.width = '100%'; row.appendChild(heading); previousGroup = group;
        }
        var chip = document.createElement('span');
        chip.className = 'cz-wait';
        var go = document.createElement('button');
        go.type = 'button';
        go.className = 'cz-wait-go';
        go.textContent = (w.type === 'recovery' ? '? Result unverified: ' : w.type === 'draft' ? '✉ Draft awaiting approval: ' : w.type === 'link' ? '£ Draft for review: ' : '⏳ Awaiting approval: ') + (w.action_summary || w.summary || 'A change') + ' · ' + waitingDate(w);
        go.title = go.textContent + (w.type === 'recovery' ? ' — inspect saved result' : ' — tap to review');
        go.addEventListener('click', function (e) {
          e.stopPropagation();
          if (w.type === 'recovery') { if (!focusOpenCard(w.proposal_id)) { addCard(proposalCardNode(w), 'An unverified result is open, shown on the right.'); scrollDown(); } }
          else resurface(w, go);
        });
        var x = document.createElement('button');
        x.type = 'button';
        x.className = 'cz-wait-x';
        x.setAttribute('aria-label', 'Let it go');
        x.textContent = '✕';
        x.addEventListener('click', function (e) { e.stopPropagation(); dismissWaiting(w); });
        chip.appendChild(go);
        if (w.type !== 'link' && w.type !== 'recovery') chip.appendChild(x);
        row.appendChild(chip);
      });
      return row;
    }
    // ── the Debrief button (Kris 2026-10-14) ────────────────────────────────
    // Top right of the live screen: "go through today with me". The first tap
    // starts the walk (src/app/debrief_walk.py reads the day fresh and takes the
    // groups in the order set on Today); while it runs the button wears the
    // group count and a tap means "next group". The newest reply says where in
    // the walk we are (payload.debrief). Owner and managers only — the walk
    // crosses every door — which the panel root declares in data-debrief.
    var debriefBtns = [], debriefState = null, debriefRequest = null;
    var DEBRIEF_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2v8"/><path d="m4.93 10.93 1.41 1.41"/><path d="M2 18h2"/><path d="M20 18h2"/><path d="m19.07 10.93-1.41 1.41"/><path d="M22 22H2"/><path d="m8 6 4-4 4 4"/><path d="M16 18a4 4 0 0 0-8 0"/></svg>';
    function debriefAllowed() { return root.getAttribute('data-debrief') === 'on'; }
    function paintDebrief() {
      var s = debriefState, running = !!(s && !s.finished);
      debriefBtns.forEach(function (btn) {
        var step = btn.querySelector('.cz-debrief-step');
        btn.classList.toggle('is-running', running);
        btn.disabled = !!debriefRequest;
        btn.setAttribute('aria-busy', String(btn.disabled));
        if (step) {
          step.hidden = !running && !btn.disabled;
          step.textContent = btn.disabled ? 'Working…' : running ? 'Topic ' + s.step + '/' + s.of : '';
        }
        btn.setAttribute('aria-label', btn.disabled ? 'Preparing your debrief' : running
          ? 'Debrief, group ' + s.step + ' of ' + s.of + (s.label ? ' — ' + s.label : '') + (s.step === s.of ? '. Tap to finish.' : '. Tap for the next group.')
          : 'Debrief — go through today with the Concierge');
        btn.title = running ? (s.label ? s.label + ' — tap for the next group' : 'Tap for the next group') : 'Debrief';
      });
    }
    // The button's request is a new message to the screen: what was up on it (a card,
    // the last reply's links) is set aside, as for any new message — and nothing is guessed.
    function settleForDebrief() {
      if (voiceStage.cardNode || voiceStage.links.length) clearVoiceCard();
      return {};
    }
    function debriefPress() {
      var live = voiceLiveInstance;
      if (debriefRequest || !live || !live.isOpen() || typeof live.startDebrief !== 'function') return;
      var request = {};
      debriefRequest = request;
      paintDebrief();
      function finished() {
        if (debriefRequest === request) { debriefRequest = null; paintDebrief(); }
      }
      try {
        Promise.resolve(live.startDebrief(debriefState && !debriefState.finished ? 'next' : 'start', finished))
          .then(function (accepted) { if (!accepted) finished(); }, finished);
      } catch (_) { finished(); }
    }
    function mountDebrief(overlay) {
      if (!debriefAllowed() || overlay.querySelector('.cz-debrief')) return;
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'cz-debrief';
      btn.innerHTML = DEBRIEF_ICON + '<span class="cz-debrief-step" hidden></span>';
      btn.addEventListener('click', function (e) { e.stopPropagation(); debriefPress(); });
      overlay.appendChild(btn);
      overlay.classList.add('has-debrief');
      debriefBtns.push(btn);
      paintDebrief();
    }
    // The typed thread (Kris 2026-09-29): the waiting list is not a strip above the
    // composer any more. It is the voice screen's circle (count, amber dashed ring)
    // sitting in the gutter beside the Concierge's newest bubble. It hides while the owner's
    // message is being answered and reappears with the answer. Tapped, the same list opens
    // centred above the composer; a line brings its card back, a tap outside closes it.
    var waitDot = null, waitDotRow = null, waitDotMo = null, dotOpen = false;
    function openWaitingItems(e) {
      e.stopPropagation();
      if (sideHost && sideHost.focusable()) { sideHost.revealWaiting(); return; }
      dotOpen = !dotOpen;
      paintWaiting();
    }
    function mountWaitDot() {
      if (waitDot || !messages) return;
      waitDotRow = document.createElement('div');
      waitDotRow.className = 'cz-waitdot-row';
      waitDot = document.createElement('button');
      waitDot.type = 'button';
      waitDot.className = 'cz-wait-dot';
      waitDot.hidden = true;
      waitDot.addEventListener('click', function (e) {
        e.stopPropagation();
        dotOpen = !dotOpen;
        paintWaiting();
      });
      if (waitingShortcut) waitingShortcut.addEventListener('click', openWaitingItems);
      waitDotRow.appendChild(waitDot);
      messages.appendChild(waitDotRow);
      // Every bubble, card and task list is appended to the thread: follow the newest.
      waitDotMo = new MutationObserver(function () { placeWaitDot(); });
      waitDotMo.observe(messages, { childList: true, subtree: true });
      window.addEventListener('resize', placeWaitDot);
      document.addEventListener('click', function (e) {
        if (!dotOpen || (waitingBox && waitingBox.contains(e.target))) return;
        dotOpen = false;
        paintWaiting();
      });
    }
    function placeWaitDot() {
      if (!waitDotRow || !messages) return;
      // The newest thing in the thread. While the owner's message is unanswered (their bubble,
      // the "Searching…" bubble or its lookup pills) the circle steps out of sight; when the
      // Concierge's answer lands it reappears beside that.
      var newest = null, entries = transcriptItems();
      for (var i = entries.length - 1; i >= 0; i--) {
        var k = entries[i];
        if (k === waitDotRow || k.hidden) continue;
        newest = k; break;
      }
      var waiting = !!newest && (newest.getAttribute('data-cz-role') === 'user' || newest.classList.contains('justify-end') ||
        newest.hasAttribute('data-cz-pending') || newest.hasAttribute('data-cz-trail'));
      var atEnd = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 80;
      // A draft or offer is full width and leaves no gutter (Kris 2026-09-30): the circle steps
      // back up to the Concierge's message that introduced it and sits beside that instead.
      var anchor = newest;
      if (newest && !waiting && messages.clientWidth - newest.offsetWidth < 48) {
        for (var j = entries.indexOf(newest) - 1; j >= 0; j--) {
          var b = entries[j];
          if (b === waitDotRow || b.hidden) continue;
          if (b.getAttribute('data-cz-role') === 'user' || b.classList.contains('justify-end')) break; // the owner's message: stop
          if (messages.clientWidth - b.offsetWidth >= 48) { anchor = b; break; }
        }
      }
      if (waitDotMo) waitDotMo.disconnect();
      if (anchor && anchor.nextSibling !== waitDotRow) anchor.parentNode.insertBefore(waitDotRow, anchor.nextSibling);
      else if (!anchor && messages.lastChild !== waitDotRow) messages.appendChild(waitDotRow);
      var gutter = anchor && !waiting ? messages.clientWidth - anchor.offsetWidth : 0;
      // Beside the bubble when there is a gutter to sit in; just below it when a full-width
      // card leaves none and no earlier Concierge message is there to sit beside.
      waitDotRow.className = 'cz-waitdot-row' + (waiting ? ' is-hidden' : gutter >= 48 ? ' is-beside' : '')
        + (waitingItems.length ? '' : ' is-empty');
      if (atEnd) scrollDown();   // a line added beneath a full-width card: keep the newest in view
      if (waitDotMo) waitDotMo.observe(messages, { childList: true, subtree: true });
    }
    function paintWaitDot() {
      mountWaitDot();
      var n = waitingItems.length;
      if (!n) dotOpen = false;
      if (waitDot) {
        waitDot.hidden = !n;
        waitDot.textContent = dotOpen && n ? '✕' : String(n);
        waitDot.setAttribute('aria-label', dotOpen ? 'Close the waiting list' : waitingCounts());
        waitDot.setAttribute('aria-expanded', dotOpen ? 'true' : 'false');
        waitDot.classList.toggle('is-open', dotOpen && n > 0);
      }
      if (waitingShortcut) {
        waitingShortcut.hidden = !n;
        waitingShortcut.textContent = 'Waiting · ' + n;
        waitingShortcut.setAttribute('aria-label', 'Waiting: ' + waitingCounts());
      }
      if (waitDotRow) waitDotRow.classList.toggle('is-empty', !n);
      placeWaitDot();
    }
    function sideChanged() { root.dispatchEvent(new CustomEvent('cz:side')); }
    // What the typed agent is doing right now, for the column's "Doing now" box (read-only for it).
    var agentNow = { busy: false, step: '', ran: [], links: [], cardDone: false };
    function agentStep(step) { agentNow.step = step; sideChanged(); }
    // Full-screen page on a computer (Kris 2026-10-17): the big things (cards, drafts, task lists) open in
    // the right-hand column's "Open now" area instead of interrupting the chat, and the thread keeps a
    // one-line note. The card NODE is the same one built here, so Confirm / Cancel keep their own checks.
    // sideHost is set by concierge_sidebar.js; without it (phones, the pop-up, a narrow window) the card
    // goes into the thread exactly as before.
    var sideHost = null;
    // the server's plain "prepared it again" line (concierge_actions.REPREPARED_SILENT): said nowhere when the card opens in the Waiting box
    var SILENT_NOTICE = /^Prepared again for you to confirm\.?$/;
    var refused = {};   // proposal_id -> why a Review could not bring it back (shown on its row, not as chat bubbles)
    // A card for this id is already on screen (open on the right or in the thread): bring that one into view
    // instead of adding the same card again.
    function focusOpenCard(id) {
      if (!id || !/^[A-Za-z0-9_-]+$/.test(id)) return false;
      var node = root.querySelector('[data-proposal-id="' + id + '"], [data-log-id="' + id + '"]');
      if (!node || !node.isConnected) return false;
      if (node.scrollIntoView) node.scrollIntoView({ block: 'nearest' });
      node.classList.add('cz-side-fresh');
      setTimeout(function () { node.classList.remove('cz-side-fresh'); }, 1700);
      return true;
    }
    // The server keeps ONE live confirmation card per business: a new card replaces any waiting one. Ask the
    // others where they stand, so a replaced card says so and goes, instead of sitting beside the new one.
    function recheckOthers(fresh) {
      var id = fresh.getAttribute && fresh.getAttribute('data-proposal-id');
      if (!id) return;
      Array.prototype.forEach.call(root.querySelectorAll('.cz-proposal[data-proposal-id]'), function (other) {
        var otherId = other.getAttribute('data-proposal-id');
        if (otherId === id || !other._cz || other._cz.settled) return;
        checkProposal(otherId, false).then(function (data) {
          if (other.isConnected && data) other._cz.applyOutcome(data);
        }).catch(function () {});
      });
    }
    function addCard(node, note) {
      if (sideHost && node._czColumn && sideHost.focusable()) {
        // Opened from the Waiting box: it lives in that box and nowhere in the thread; Back puts it away, still waiting.
        sideHost.show(node, null, '', { focus: true, hint: node._czHint || '' });
      } else if (sideHost && sideHost.active()) {
        var stub = document.createElement('p');
        stub.className = 'cz-side-stub text-xs text-slate-400 px-2';
        stub.textContent = note;
        messages.appendChild(stub);
        node._czStub = stub;
        sideHost.show(node, stub, note);
      } else {
        messages.appendChild(node);
      }
      recheckOthers(node);
      return node;
    }
    function paintWaiting() {
      sideChanged();   // the page's right-hand column repaints
      paintBadge();
      paintWaitDot();
      if (waitingBox) {
        waitingBox.textContent = '';
        var row = dotOpen ? waitingRow(true) : null;
        if (row) waitingBox.appendChild(row);
        waitingBox.hidden = !row;
        waitingBox.classList.toggle('cz-waiting-pop', !!row);
      }
      if (voiceLiveInstance && voiceLiveInstance.isOpen()) voiceLiveInstance.refreshStage();
    }
    function postJson(url, body) {
      return fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken() },
        body: JSON.stringify(body)
      }).then(function (r) { if (!r.ok) throw new Error('http ' + r.status); return r.json(); });
    }
    function resurface(w, btn) {
      dotOpen = false;
      waitingOpen = false;
      var inColumn = !!(w._czColumn && sideHost && sideHost.focusable());   // tapped in the column's Waiting box: the card opens in that box
      if (!(voiceLiveInstance && voiceLiveInstance.isOpen()) && focusOpenCard(w.proposal_id || w.log_id)) return;
      // The card is added to the thread, which the Explore actions board covers when the chat is empty
      // (after Clear history): step the board aside so the card is where the owner is looking.
      if (discovery && !(voiceLiveInstance && voiceLiveInstance.isOpen())) discovery.hide();
      // A draft invoice waits to be looked at and sent: the tap opens it.
      if (w.type === 'link') { if (w.href && w.href.charAt(0) === '/') openPlatformRecord(w.href); return; }
      if (w.type === 'draft') {
        // A draft needs no rebuild: the same card, thumbs-up and ✕, comes back.
        noteShown(w);
        var node = draftCardNode(w);
        if (voiceLiveInstance && voiceLiveInstance.isOpen()) {
          parkVoiceCard();
          voiceStage.proposal = null;
          voiceStage.cardNode = node;
          voiceStage.draft = node;
          voiceStage.draftInvited = true;
          voiceStage.cardAnchor = null;
          voiceStage.links = [];
          node._czColumn = inColumn;
          voiceLiveInstance.notify('Back — say yes to send it');
        } else {
          node._czColumn = inColumn;
          addCard(node, 'The email draft is ready, shown on the right.');
          scrollDown();
        }
        loadWaiting();
        return;
      }
      if (btn) { btn.disabled = true; btn.textContent = 'Bringing it back…'; }
      postJson('/concierge/business/resurface', { proposal_id: w.proposal_id })
        .then(function (d) {
          if (w.current && !w.current()) return;
          if (!d.ok) {
            var why = d.error || "That one can't come back.";
            if (voiceLiveInstance && voiceLiveInstance.isOpen()) voiceLiveInstance.notify(why);
            else if (sideHost && sideHost.active()) { refused[w.proposal_id] = why; sideChanged(); }   // said on its row in the column
            else bubble('assistant', why);
            return;
          }
          var p = d.proposal;
          noteShown(p);
          // A parked card comes back as a NEW card (prepared again, or revised): say which.
          if (p.notice && !inColumn && !(voiceLiveInstance && voiceLiveInstance.isOpen())) bubble('assistant', p.notice);
          if (voiceLiveInstance && voiceLiveInstance.isOpen()) {
            // On the voice screen it takes the stage, and "yes" confirms it.
            parkVoiceCard();
            voiceStage.proposal = p;
            voiceStage.draft = null;
            voiceStage.cardNode = proposalCardNode(p);
            voiceStage.cardNode._czColumn = inColumn;
            voiceStage.cardAnchor = null;
            voiceStage.links = [];
            voiceLiveInstance.notify(p.notice ? p.notice : (p.tap_only ? 'Back — tap Confirm on the card' : 'Back — say yes to confirm'));
          } else {
            p._czColumn = inColumn;
            proposalCard(p);
          }
        })
        .catch(function () {
          if (btn) { btn.disabled = false; btn.textContent = 'Retry opening — ' + (w.summary || 'A change'); }
          var message = 'Could not load the approval card. No action was submitted by this tap. Try opening it again.';
          if (voiceLiveInstance && voiceLiveInstance.isOpen()) voiceLiveInstance.notify(message);
          else bubble('assistant', message);
        })
        .then(loadWaiting);
    }
    function dismissWaiting(w) {
      waitingItems = waitingItems.filter(function (x) { return x !== w; });
      paintWaiting();
      var gone = w.type === 'draft'
        ? postForm('/app/drafts/' + encodeURIComponent(w.log_id) + '/skip', { return_to: '/app/concierge' })
        : postJson('/concierge/business/dismiss', { proposal_id: w.proposal_id });
      gone.catch(function () {}).then(loadWaiting);
    }
    // A card settled by TAP while it is the one voice is holding: voice must
    // let go of it (a later spoken yes re-ran an already-used proposal), and
    // with the conversation open the voice screen says so (donor 2497f80).
    function settledByTap(card, data) {
      if (voiceStage.cardNode !== card) return;
      voiceStage.proposal = null;
      if (data && voiceLiveInstance && voiceLiveInstance.isOpen()) {
        // Executor receipts already read "Done — …"; only a bare outcome gets the prefix.
        var said = data.summary || (data.ok ? 'confirmed' : 'Nothing changed');
        voiceLiveInstance.notify(data.ok && !/^Done\b/.test(said) ? 'Done — ' + said : said);
      }
    }

    // ── a draft the Drafting agent wrote for a voice request ─────────────────
    // (src/app/concierge_draft.py, Kris 2026-09-23). The thread's draft card, slimmed
    // for the voice screen's bottom third: the same header, the same editable
    // text, the same thumbs-up and skip — and they hit the thread's own
    // endpoints, so sending from here IS sending from the thread.
    var THUMB_UP = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 10v11H4a1 1 0 0 1-1-1v-9a1 1 0 0 1 1-1h3Zm0 0 4-8a2.5 2.5 0 0 1 2.5 2.5V9h5.2a2 2 0 0 1 2 2.3l-1.3 8a2 2 0 0 1-2 1.7H7"/></svg>';
    // A bin, in red (Kris 2026-09-24): the cross read as "close this", not
    // "throw the draft away".
    var SKIP_BIN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3"/></svg>';
    function postForm(url, fields) {
      var body = new URLSearchParams(fields);
      body.set('csrf_token', csrfToken());
      // The routes answer a form post with a redirect; following it would
      // fetch a whole page for nothing. An opaque redirect IS the success.
      return fetch(url, { method: 'POST', body: body, redirect: 'manual', credentials: 'same-origin' })
        .then(function (r) {
          if (r.type === 'opaqueredirect' || r.ok) return true;
          if (r.status === 409) {
            return r.json().then(function (data) {
              var error = new Error('HTTP 409');
              error.holdReason = typeof data.detail === 'string' ? data.detail : '';
              throw error;
            });
          }
          throw new Error('HTTP ' + r.status);
        });
    }
    function draftCardNode(d) {
      var card = document.createElement('div');
      card.className = 'cz-draft';
      card.setAttribute('data-log-id', d.log_id);
      var head = document.createElement('div');
      head.className = 'cz-draft-head';
      var label = document.createElement('p');
      label.className = 'cz-draft-label';
      var to = document.createElement('p');
      to.className = 'cz-draft-to';
      to.textContent = 'To ' + (d.name || d.contact) + (d.subject ? ' · ' + d.subject : '');
      var acts = document.createElement('div');
      acts.className = 'cz-draft-acts';
      // Kris 2026-09-24: the 👍 and ✕ sit on the "To …" line; the state
      // ("Draft ready · Editable before approval") moves to the bottom right,
      // opposite "Open the thread →".
      head.appendChild(to);
      head.appendChild(acts);
      var ta = document.createElement('textarea');
      ta.className = 'cz-draft-text';
      ta.value = d.text || '';
      ta.setAttribute('aria-label', 'The draft — edit before sending');
      var foot = document.createElement('a');
      foot.className = 'cz-draft-open';
      foot.href = d.href;
      keepPlatformRecordInAnotherTab(foot, d.href);
      foot.textContent = 'Open the thread →';
      var footRow = document.createElement('div');
      footRow.className = 'cz-draft-foot';
      footRow.appendChild(foot);
      footRow.appendChild(label);
      card.appendChild(head);
      card.appendChild(ta);
      card.appendChild(footRow);
      var reason = document.createElement('p');
      reason.className = 'cz-proposal-detail';
      reason.textContent = d.reason || '';
      reason.hidden = !d.reason;
      card.appendChild(reason);

      var state = d.status || 'pending';
      var LABELS = {
        sending: 'Sending…',
        skipping: 'Skipping…',
        sent: d.auto ? 'Sent ✓ · full-auto' : 'Sent ✓',
        skipped: 'Skipped · no reply sent',
        left: 'Waiting in the thread for your approval',
        failed: "Couldn't send — it's waiting in the thread",
        empty: d.reason ? 'Held for review' : 'No draft written — this one is yours to write',
      };
      function paint(next) {
        state = next;
        card.setAttribute('data-state', next);
        acts.textContent = '';
        ta.readOnly = next !== 'pending';
        if (next !== 'pending') { label.textContent = LABELS[next] || ''; return; }
        label.textContent = 'Draft ready · Editable before approval';
        var send = document.createElement('button');
        send.type = 'button';
        send.className = 'cz-draft-btn cz-draft-send';
        send.setAttribute('aria-label', 'Approve and send');
        send.title = 'Approve and send';
        send.innerHTML = THUMB_UP;
        send.addEventListener('click', function () {
          api.send().then(function (ok) { tapped(ok ? 'Sent.' : null); });
        });
        var skip = document.createElement('button');
        skip.type = 'button';
        skip.className = 'cz-draft-btn cz-draft-bin';
        skip.setAttribute('aria-label', 'Bin this draft — nothing is sent');
        skip.title = 'Bin this draft';
        skip.innerHTML = SKIP_BIN;
        skip.addEventListener('click', function () {
          api.skip().then(function (ok) { tapped(ok ? 'Skipped.' : null); });
        });
        acts.appendChild(send);
        if (d.log_id) {
          var fix = document.createElement('a');
          fix.className = 'cz-draft-btn cz-draft-fix';
          fix.href = '/app/drafts/' + encodeURIComponent(d.log_id) + '#correct-draft';
          fix.title = 'Correct this draft with the assistant';
          fix.setAttribute('aria-label', fix.title);
          fix.innerHTML = THUMB_UP;
          acts.appendChild(fix);
        }
        acts.appendChild(skip);
      }
      var api = {
        pending: function () { return state === 'pending'; },
        send: function () {
          if (state !== 'pending') return Promise.resolve(false);
          var text = ta.value.trim();
          if (!text) return Promise.resolve(false);
          paint('sending');
          return postForm('/app/drafts/' + encodeURIComponent(d.log_id) + '/send',
                          { draft_reply: text, return_to: '/app/concierge' })
            .then(function () { paint('sent'); loadWaiting(); return true; })
            .catch(function (error) {
              if (error.holdReason) {
                reason.textContent = error.holdReason;
                reason.hidden = false;
                LABELS.empty = 'Held for review';
                paint('empty');
              } else { paint('pending'); }
              return false;
            });
        },
        skip: function () {
          if (state !== 'pending') return Promise.resolve(false);
          // The buttons go the moment it's tapped, as Send's do: the skip
          // takes a beat on the server, and with the ✕ still live each extra
          // tap was another skip (Kris 2026-09-23: three taps, three
          // "Skipped" toasts).
          paint('skipping');
          return postForm('/app/drafts/' + encodeURIComponent(d.log_id) + '/skip',
                          { return_to: '/app/concierge' })
            .then(function () { paint('skipped'); loadWaiting(); return true; })
            .catch(function () { paint('pending'); return false; });
        },
        leave: function () { if (state === 'pending') paint('left'); },
      };
      // A tap settles the card the voice is holding: voice lets go of it, and
      // with the conversation open the voice screen says what happened.
      function tapped(said) {
        if (voiceStage.cardNode === card) {
          voiceStage.draft = null;
          // Skipped is done with: off the screen now (Kris 2026-09-23: "clicking
          // a cross ... doesn't clear it"). Sent shows its tick for a moment,
          // then goes too ("it says sent but the draft box is still showing") —
          // it stays in the chat and the thread.
          if (state === 'skipped') clearVoiceCard();
          else if (state === 'sent') settledCardGoes(card);
        }
        if (state === 'sent') loadDone();
        if (said && voiceLiveInstance && voiceLiveInstance.isOpen()) voiceLiveInstance.notify(said);
      }
      // The box grows with the draft up to its CSS max-height, then scrolls.
      // The card is built off-screen (nothing to measure yet), so it fits
      // once it is laid out, whenever its width changes, and as you type.
      function fit() {
        if (!ta.isConnected || !ta.clientWidth) return;
        ta.style.height = 'auto';
        ta.style.height = ta.scrollHeight + 'px';
      }
      ta.addEventListener('input', fit);
      // The voice screen appends the card straight after building it; a few
      // timed tries catch that (fit() does nothing until it's on screen).
      [0, 60, 300].forEach(function (ms) { setTimeout(fit, ms); });
      if (window.ResizeObserver) {
        var lastW = 0;
        new ResizeObserver(function () {
          if (ta.clientWidth === lastW) return;
          lastW = ta.clientWidth;
          fit();
        }).observe(ta);
      }
      card._draft = api;
      if (d.log_id) card.dataset.logId = d.log_id;   // the waiting chip's key
      paint(state);
      return card;
    }

    async function ask(question, discoveryRequest) {
      if (busy || !question) return;
      busy = true; setComposerBusy(true);
      if (discovery) discovery.hide(!!discoveryRequest);
      var displayTurnId=crypto.randomUUID();
      // A discovery request is shown as a turn only after the same endpoint
      // accepts its capability revision. Stale suggestions never enter chat.
      if (!discoveryRequest) bubble('user', question, 'typed', displayTurnId);
      var status = statusBubble('typed', displayTurnId);
      var strip = null, livePill = null, liveItem = null;
      agentNow.busy = true; agentNow.ran = []; agentNow.links = []; agentNow.cardDone = false; agentStep('Looking at your question');
      // Live 2026-09-06: a 90 s API stall sat on "Searching your data…" for four
      // minutes. After 15 s say it is slow, not stuck.
      var slow = setTimeout(function () {
        status.textContent = 'Still working — a lookup is taking longer than usual…';
        agentStep('Still working — a lookup is taking longer than usual');
      }, 15000);
      try {
        await historyReady;
        var res = await fetch('/concierge/business', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken() },
          body: JSON.stringify(Object.assign({ question: question, history: history.slice(-CONTEXT_TURNS), surface: 'page', display_turn_id: displayTurnId }, discoveryRequest || {}))
        });
        if (res.status === 409 && discoveryRequest && discovery) {
          status.remove(); clearTimeout(slow);
          busy = false; setComposerBusy(false);
          discovery.stale();
          return;
        }
        if (!res.ok) throw new Error('http ' + res.status);
        if (discoveryRequest) {
          var submitted = bubble('user', question, 'typed', displayTurnId);
          status.parentNode.insertBefore(submitted, status);
          paintModeBlocks();
        }
        var reader = res.body.getReader();
        var decoder = new TextDecoder();
        var buf = '';
        var done = null;
        while (true) {
          var chunk = await reader.read();
          if (chunk.done) break;
          buf += decoder.decode(chunk.value, { stream: true });
          var lines = buf.split('\n');
          buf = lines.pop();
          lines.forEach(function (line) {
            if (!line.trim()) return;
            var ev;
            try { ev = JSON.parse(line); } catch (e) { return; }
            if (ev.type === 'tool') {
              // First lookup: swap the italic status bubble for the trail strip.
              if (!strip) { status.remove(); strip = trailStrip('typed', displayTurnId); }
              settlePill(livePill, liveItem);
              if (liveItem && liveItem.label) agentNow.ran.push(liveItem.label);
              liveItem = ev;
              livePill = trailPill(strip, ev, true);
              agentStep(ev.hint || 'Searching your data');
            }
            if (ev.type === 'done') done = ev;
          });
        }
        clearTimeout(slow);
        settlePill(livePill, liveItem);
        finishTrail(strip);
        if (liveItem && liveItem.label) agentNow.ran.push(liveItem.label);
        agentNow.busy = false; agentNow.step = ''; agentNow.links = (done && done.links) || [];
        sideChanged();
        if (!strip) status.remove();
        if(strip){strip.wrap.setAttribute('data-cz-turn-id',displayTurnId);strip.wrap.setAttribute('data-cz-role','assistant');markOrigin(strip.wrap,'typed',displayTurnId,'assistant');}
        var reply = (done && done.reply) || 'I had trouble with that one — could you try asking differently?';
        var el = bubble('assistant', reply, 'typed', displayTurnId);
        addLinks(el, done && done.links);
        if (done && done.proposal && done.proposal.proposal_id) { noteShown(done.proposal); var shownCard = proposalCard(done.proposal); (shownCard._czStub || shownCard).setAttribute('data-cz-owner-turn',displayTurnId); }
        if (done && done.tasks && done.tasks.batch_id) { var taskNode = taskListNode(done.tasks); addCard(taskNode, 'Your task list is started, shown on the right.'); (taskNode._czStub || taskNode).setAttribute('data-cz-owner-turn', displayTurnId); scrollDown(); }
        pinLongReply(el);
        loadWaiting();   // a new card pushes the last one aside: it now waits
        history.push({ role: 'user', content: question });
        history.push({ role: 'assistant', content: reply, lookups: lookupsOf(done && done.trail) });
      } catch (e) {
        clearTimeout(slow);
        settlePill(livePill, liveItem);
        finishTrail(strip);
        agentNow.busy = false; agentNow.step = ''; sideChanged();
        if (!strip) status.remove();
        bubble('assistant', 'I had trouble processing that. Could you try asking differently?', 'typed', displayTurnId);
      }
      busy = false; setComposerBusy(false);
      // Returning focus must not pull the phone strip back from the action just presented.
      input.focus({ preventScroll: true });
    }

    // ── live voice (moved from the widget's business tab) ────────────────────
    // The app-side half of /static/voice-live.js: the module owns audio and the
    // overlay; everything BLAM8-specific arrives through the deps below. Voice
    // rides the SAME thread as typing — same brain, same proposals, and every
    // spoken turn lands in the transcript as it happens.
    var VOICE_ON = root.getAttribute('data-voice') === 'true';
    var voiceLiveInstance = null;
    var voiceInstances = {};
    var voiceModeGeneration = 0;
    var voiceModeChoice = null;
    var VOICE_MODE_KEY = 'blam8.voiceMode:' + (root.getAttribute('data-voice-user') || 'anonymous');
    var GPTLIVE_ON = root.getAttribute('data-gptlive') === 'on';
    function savedVoiceMode() {
      if (voiceModeChoice) return voiceModeChoice;
      try {
        var saved = localStorage.getItem(VOICE_MODE_KEY);
        if (saved === 'standard') return 'standard';
        if (saved === 'gptlive') return GPTLIVE_ON ? 'gptlive' : 'standard';
        return GPTLIVE_ON ? 'gptlive' : 'standard';
      } catch (e) { return GPTLIVE_ON ? 'gptlive' : 'standard'; }
    }
    root.querySelectorAll('[name="cz-voice-mode"]').forEach(function (radio) {
      radio.checked = radio.value === savedVoiceMode();
      radio.addEventListener('change', function () {
        if (!radio.checked || ['standard', 'gptlive'].indexOf(radio.value) < 0) return;
        var modeGeneration = ++voiceModeGeneration;
        voiceModeChoice = radio.value;
        paintModeSections();
        if (sheet && !sheet.hidden) { stopMeter(); startMeter(); }   // the line card keeps a line per mode: read it again
        var reopen = voiceLiveInstance && voiceLiveInstance.isOpen();
        var closing = voiceLiveInstance ? Promise.resolve(voiceLiveInstance.close('mode_switch')) : Promise.resolve();
        voiceLiveInstance = null;
        try { localStorage.setItem(VOICE_MODE_KEY, radio.value); } catch (e) {}
        closing.then(function () { if (reopen && modeGeneration === voiceModeGeneration) openVoiceLive(); });
      });
    });
    // The newest reply's cards for the overlay's second page. Card nodes are
    // CACHED per proposal so a stage refresh re-attaches the same node (its
    // listeners and its confirmed/cancelled state survive) instead of
    // resurrecting a fresh Confirm button for an already-run proposal.
    var voiceStage = { links: [], proposal: null, draft: null, cardNode: null, cardAnchor: null, fresh: false,
                       draftInvited: false };

    // Local spoken-turn matchers, mirrored from the donor (OurTab main.js).
    // The vocab lists are JSON literals so tests/test_voice_live_client.py can
    // parse them straight out of this file and run the must-NOT-fire lists —
    // a false positive here mutates real business data.
    // "send" is a yes to what's on screen: "Yes, send it" / "send that off"
    // (Kris 2026-09-23: each went to the model, which drafted the email again
    // — three copies to Daniel). "Send it to Daniel instead" still isn't.
    var VOICE_AFFIRM_VOCAB = ["y", "yes", "yep", "yeah", "yup", "aye", "sure", "ok", "okay", "k",
      "confirm", "confirmed", "correct", "right", "thats", "that's", "that",
      "is", "all", "both",
      "definitely", "absolutely", "go", "ahead", "for", "do", "it", "please",
      "perfect", "great", "lovely", "sounds", "good", "fine", "now", "then",
      "thanks", "thank", "you", "send", "this", "off", "one"];
    var VOICE_AFFIRM_CORE = ["y", "yes", "yep", "yeah", "yup", "aye", "sure", "confirm", "confirmed",
      "correct", "definitely", "absolutely", "ahead", "do", "please", "okay", "ok", "send"];
    var VOICE_DECLINE_VOCAB = ["no", "nah", "nope", "not", "now", "yet", "dont", "do", "it", "that",
      "cancel", "leave", "skip", "forget", "never", "mind", "stop",
      "thanks", "thank", "you", "please", "ta", "actually", "them", "those"];
    var VOICE_DECLINE_CORE = ["no", "nah", "nope", "not", "cancel", "dont", "leave", "skip", "forget", "stop", "mind"];
    var VOICE_FAREWELLS = ["bye", "byebye", "bye bye", "goodbye", "good bye", "cheerio", "ta ra", "tara",
      "night", "good night", "goodnight",
      "cheers", "cheers bye", "cheers goodbye", "cheers thanks", "cheers then",
      "see you", "see ya", "see you later",
      "thats all", "that is all", "thats all thanks", "thats all for now",
      "thats it", "that is it", "thats it thanks",
      "thats enough", "that is enough", "enough",
      "thats everything", "that is everything", "thats the lot",
      "nothing else", "nothing more", "nothing thanks", "no more",
      "no thanks", "no thank you", "no im good", "im good", "im done", "i am done",
      "all done", "we are done", "were done", "all good",
      "go away", "no go away", "leave it", "leave me alone", "shut up"];

    function voiceWords(text) {
      return String(text || "").toLowerCase()
        .replace(/['’]/g, "")
        .replace(/[^a-z\s]/g, " ")
        .trim()
        .split(/\s+/)
        .filter(function (w) { return !!w; });
    }
    // Words that mean nothing except "yes, do it". Anything outside the
    // vocabulary — "yes but move it to Friday" — is a normal message and goes
    // to the model instead of silently executing.
    function isBareAffirmation(text) {
      var words = String(text || "").toLowerCase()
        .replace(/[^a-z'\s]/g, " ")
        .split(/\s+/)
        .filter(function (w) { return !!w; });
      if (!words.length || words.length > 6) return false;
      if (!words.every(function (w) { return VOICE_AFFIRM_VOCAB.indexOf(w) !== -1; })) return false;
      return words.some(function (w) { return VOICE_AFFIRM_CORE.indexOf(w) !== -1; });
    }
    function isBareDecline(text) {
      var words = voiceWords(text);
      if (!words.length || words.length > 6) return false;
      if (!words.every(function (w) { return VOICE_DECLINE_VOCAB.indexOf(w) !== -1; })) return false;
      return words.some(function (w) { return VOICE_DECLINE_CORE.indexOf(w) !== -1; });
    }
    function isVoiceFarewell(text) {
      var clean = voiceWords(text).join(" ");
      if (!clean) return false;
      if (clean.split(" ").length > 4) return false;
      return VOICE_FAREWELLS.indexOf(clean) !== -1;
    }

    // Claim + run the pending proposal through the SAME single-use execute path
    // the card's Confirm button uses — a spoken yes and a tapped Confirm are the
    // same authority.
    async function voiceExecuteProposal(p, card) {
      var c = card && card._cz;
      return submitProposal(p, { draft_text: c ? c.draft() : '', selected: c ? c.selected() : null, confirm_via: 'words' });
    }

    // A bare "yes" sends the draft on screen only when the Concierge's last question was about
    // sending it (2026-09-30: it asked "confirm it in the diary?" with a draft up, the yes sent
    // the email). The last question, not any mention of sending: anything else goes to the model.
    function lastAssistantAskedToSend() {
      if (voiceStage.draftInvited) return true;     // "Back — say yes to send it"
      for (var i = history.length - 1; i >= 0; i--) {
        if (history[i].role !== 'assistant') continue;
        var qs = String(history[i].content || '').replace(/\[Proposed:[^\]]*\]/g, ' ').match(/[^.!?\n]*\?/g);
        if (!qs) return false;
        return /\b(send|sending|approve|go ahead|go out)\b/i.test(qs[qs.length - 1]);
      }
      return false;
    }

    function voiceLog(entry) {
      history.push({ role: entry.role, content: entry.content, lookups: entry.lookups, turn_id: entry.turn_id });
      if (discovery) discovery.hide();
      var el = bubble(entry.role, entry.content, entry.origin || (savedVoiceMode()==='gptlive'?'live':'voice'), entry.turn_id);
      if (entry.origin === 'voice' || entry.origin === 'live' || (!entry.origin && activeVoiceVisitId)) {
        el.setAttribute('data-cz-visit-id', entry.visit_id || activeVoiceVisitId);
      }
      paintModeBlocks();
      if (entry.links) addLinks(el, entry.links);
      return el;
    }

    // Tapping the heard words on the voice screen strikes the turn just sent: it leaves
    // the history the next turn sends, and its bubble leaves the chat.
    var voiceLastUser = null;
    var voiceUserBubbles = new Map();
    function voiceDiscardTurn(turnId) {
      for (var i = history.length - 1; i >= 0; i--) {
        if (history[i].role === 'user' && history[i].turn_id === turnId) { history.splice(i, 1); break; }
      }
      var node = voiceUserBubbles.get(turnId);
      if (node && node.parentNode) node.remove();
      voiceUserBubbles.delete(turnId);
    }

    var SAY_AGAIN = "Sorry — was that a yes? Say yes or no.";   // = voice-live.js LOCAL_LINES
    async function voiceUserTurn(text, meta) {
      voiceLastUser = voiceLog({ role: "user", content: text, turn_id: meta.turnId, origin:meta.origin });
      voiceUserBubbles.set(meta.turnId, voiceLastUser);
      // The Debrief button is a deliberate request, not heard words: none of the
      // yes / no / goodbye guesses below apply to it (a lone "Next." with a card up
      // would read as a mis-heard yes). What was on screen is set aside, as for any
      // new message.
      if (meta && meta.debrief) return settleForDebrief();
      var p = voiceStage.proposal;
      // Order matters: "no thanks" appears in the farewell list too, but with a
      // card waiting it means "not that" — context decides.
      var card = voiceStage.cardNode;
      // A draft waiting on the screen: "yes" sends it (the thumbs-up's own
      // endpoint), "no" leaves it in the thread for later.
      var dr = voiceStage.draft;
      if (dr && dr._draft && dr._draft.pending()) {
        if (isBareDecline(text)) {
          dr._draft.leave();
          clearVoiceCard();
          voiceLog({ origin:meta.origin, turn_id:meta.turnId, role: "assistant", content: "Okay — it's waiting in the thread." });
          return { declined: true, ack: "Okay — it's waiting in the thread." };
        }
        if (isBareAffirmation(text) && lastAssistantAskedToSend()) {
          voiceStage.draft = null;
          var sent = await dr._draft.send();
          var line = sent ? "Sent." : "That didn't send — it's still waiting in the thread.";
          // Sent is done with: the tick shows for a moment, then the card
          // goes (Kris 2026-09-24: "once a draft is sent, it should auto
          // close" — it was sitting on the stage, blocking the lists). It
          // stays in the chat and the thread, and in the done list.
          if (sent) settledCardGoes(dr);
          else voiceStage.draft = dr;
          voiceLog({ origin:meta.origin, turn_id:meta.turnId, role: "assistant", content: line });
          // Said straight back, never a second model turn (Kris 2026-09-23:
          // a yes that ran in silence read as a yes that didn't work).
          return { confirmed: sent, answered: true, ack: line };
        }
      }
      if (p && isBareDecline(text)) {
        if (card && card._cz) card._cz.lock();
        voiceStage.proposal = null;
        var cancelled = await submitProposal(p, {}, '/concierge/business/cancel');
        if (card && card._cz) card._cz.applyOutcome(cancelled);
        if (cancelled.state === 'pending') voiceStage.proposal = p;
        if (cancelled.state === 'cancelled') clearVoiceCard();
        voiceLog({ origin:meta.origin, turn_id:meta.turnId, role: 'assistant', content: cancelled.summary });
        return { declined: cancelled.state === 'cancelled', answered: true, ack: cancelled.summary };
      }
      // Ending the session needs REAL speech behind it: the STT model
      // transcribes coughs and knocks as short phrases ("go away" — donor,
      // live 2026-08-23), and a hallucinated goodbye that hangs up is the worst
      // false positive here. 450ms is comfortably under any spoken farewell;
      // 0 means unknown, and unknown must not block a genuine goodbye.
      var dur = Number(meta && meta.durationMs) || 0;
      if (isVoiceFarewell(text) && (dur === 0 || dur >= 450)) {
        voiceLog({ origin:meta.origin, turn_id:meta.turnId, role: "assistant", content: "Bye for now." });
        return { farewell: true, ack: "Bye for now." };
      }
      // A bare yes runs what's waiting BEFORE the model replies, so its answer
      // already knows the outcome (the execute endpoint also writes the outcome
      // into the server transcript).
      if (p && isBareAffirmation(text)) {
        voiceStage.proposal = null;
        if (card && card._cz) card._cz.lock();
        try {
          var data = await voiceExecuteProposal(p, card);
          var said = data.summary;
          if (card && card._cz) card._cz.applyOutcome(data);
          if (data.state === 'pending') voiceStage.proposal = p;
          // A confirmed card goes the same way a sent draft does: the outcome
          // is spoken and listed under ✓; the card itself needn't hold the stage.
          // …unless the result hands the owner something to do — "Open
          // INV-1021" (Kris 2026-09-24: the draft invoice "only displayed for a
          // second or 2"). That card stays until the next message or a badge.
          var actOn = data.link && /^\/app\/invoices\//.test(data.link.href || '');
          if (card && data.ok && !actOn) settledCardGoes(card);
          voiceLog({ origin:meta.origin, turn_id:meta.turnId, role: "assistant", content: said });
          return { confirmed: !!data.ok, answered: true, ack: said };
        } catch (e) {
          if (card && card._cz) card._cz.applyOutcome({ state: 'unknown', summary: UNKNOWN_PROPOSAL });
          voiceLog({ origin:meta.origin, turn_id:meta.turnId, role: "assistant", content: UNKNOWN_PROPOSAL });
          return { answered: true, ack: UNKNOWN_PROPOSAL };
        }
      }
      // One word, a card or draft waiting, and it's neither yes nor no: most
      // likely a yes the transcriber heard as a name (Kris 2026-09-24: "Yes."
      // came through as "Jess." — a customer's name — and cost two model
      // turns). Asked again on the spot; the card stays up.
      var stillWaiting = (dr && dr._draft && dr._draft.pending()) || p;
      if (stillWaiting && voiceWords(text).length === 1 && !isVoiceFarewell(text)) {
        voiceLog({ origin:meta.origin, turn_id:meta.turnId, role: "assistant", content: SAY_AGAIN });
        return { answered: true, ack: SAY_AGAIN };
      }
      // Not a yes or no to what's on screen: this is a new message, so the
      // screen starts clean for its answer (Kris 2026-09-23: "the UI state
      // doesn't reset on the next message").
      if (voiceStage.cardNode || voiceStage.links.length) clearVoiceCard();
      return {};
    }

    // The voice screen shows the newest reply's card; the thread keeps every
    // one. A card goes back under the reply that brought it when a newer one
    // takes the screen, and when the conversation closes.
    function parkVoiceCard() {
      var c = voiceStage.cardNode, anchor = voiceStage.cardAnchor;
      if (!c || messages.contains(c) || c._czColumn) return;   // a card opened from the Waiting box is not put in the thread
      if (anchor && messages.contains(anchor)) anchor.parentNode.insertBefore(c, anchor.nextSibling);
      else messages.appendChild(c);
    }

    // Take the card (and the last reply's links) off the voice screen: the
    // card goes back into the chat under the reply that brought it, and voice
    // lets go of it, so a later "yes" can't act on something no longer shown.
    // The draft itself is untouched: it stays pending (or skipped) in its thread.
    // A card that's DONE (sent, confirmed) leaves the stage on its own after a
    // beat — long enough to see the tick, not long enough to be in the way.
    var SETTLED_MS = 2500;
    function settledCardGoes(card) {
      loadDone();
      setTimeout(function () { if (voiceStage.cardNode === card) clearVoiceCard(); }, SETTLED_MS);
    }
    function clearVoiceCard() {
      // Set aside unanswered (the next thing said wasn't a yes or no): it's
      // waiting from now, not five minutes from now.
      if (voiceStage.proposal && voiceStage.proposal.proposal_id) delete justShown[voiceStage.proposal.proposal_id];
      if (voiceStage.draft && voiceStage.draft.dataset && voiceStage.draft.dataset.logId) delete justShown[voiceStage.draft.dataset.logId];
      parkVoiceCard();
      voiceStage.cardNode = null;
      voiceStage.cardAnchor = null;
      voiceStage.proposal = null;
      voiceStage.draft = null;
      voiceStage.draftInvited = false;
      voiceStage.links = [];
      if (voiceLiveInstance && voiceLiveInstance.isOpen()) voiceLiveInstance.refreshStage();
      loadWaiting();   // a card set aside unanswered becomes a chip
    }

    function voiceAssistantTurn(payload) {
      // Where we are in the debrief walk, when one is on (null = it just finished).
      if (payload && Object.prototype.hasOwnProperty.call(payload, 'debrief')) { debriefState = payload.debrief; paintDebrief(); }
      var replyEl = voiceLog({ role: "assistant", content: payload.reply || "", links: payload.links || [],
                              lookups: lookupsOf(payload.trail), turn_id: payload.turn_id || payload.display_turn_id, origin: payload.origin });
      voiceStage.links = payload.links || [];
      if (payload.proposal && payload.proposal.proposal_id) {
        parkVoiceCard();
        voiceStage.proposal = payload.proposal;
        voiceStage.draft = null;
        noteShown(payload.proposal);
        voiceStage.cardNode = proposalCardNode(payload.proposal);
        voiceStage.cardAnchor = replyEl;
      } else if (payload.tasks && payload.tasks.batch_id) {
        // A task list by voice: it takes the stage like a card, and fills in live.
        parkVoiceCard();
        voiceStage.proposal = null;
        voiceStage.draft = null;
        voiceStage.cardNode = taskListNode(payload.tasks);
        voiceStage.cardAnchor = replyEl;
      } else if (payload.draft && payload.draft.log_id) {
        parkVoiceCard();
        voiceStage.proposal = null;
        voiceStage.cardNode = draftCardNode(payload.draft);
        voiceStage.draft = payload.draft.status === 'pending' ? voiceStage.cardNode : null;
        voiceStage.draftInvited = false;
        voiceStage.cardAnchor = replyEl;
      }
      if(voiceStage.cardNode && (payload.turn_id || payload.display_turn_id))voiceStage.cardNode.setAttribute('data-cz-owner-turn',payload.turn_id || payload.display_turn_id);
      if(payload.trail && payload.trail.length){var strip=trailStrip(payload.origin || (savedVoiceMode()==='gptlive'?'live':'voice'),payload.turn_id || payload.display_turn_id);payload.trail.forEach(function(item){trailPill(strip,item,false);});finishTrail(strip);replyEl.parentNode.insertBefore(strip.wrap,replyEl);}
      voiceStage.fresh = !!(voiceStage.links.length || (payload.proposal && payload.proposal.proposal_id)
                            || (payload.draft && payload.draft.log_id)
                            || (payload.tasks && payload.tasks.batch_id));
      loadWaiting();
    }

    function voiceRenderStage() {
      var box = document.createElement("div");
      var links = voiceStage.links.filter(function (l) { return l && l.href && l.label && l.href.charAt(0) === "/"; });
      if (links.length) box.appendChild(linkRow(links));
      if (voiceStage.cardNode) {
        var held = voiceStage.cardNode;
        // Tapped in the column's Waiting box: the card fills that box and is not also at the bottom of this screen. Once it
        // has faded away from there it is not put back.
        if (held._czColumn && sideHost && sideHost.focusable()) {
          if (!(held._czShown && !held.isConnected)) { held._czShown = true; sideHost.show(held, null, '', { focus: true }); }
        } else box.appendChild(held);
      } else if (waitingOpen) {
        // The badge was tapped: the list of what's waiting, at the bottom.
        var wrow = waitingRow(true);
        if (wrow) { wrow.id = 'cz-waiting-list'; box.appendChild(wrow); }
      } else if (doneOpen) {
        var drow = doneRow();
        if (drow) { drow.id = 'cz-done-list'; box.appendChild(drow); }
      }
      if (!box.children.length) return { node: null, fresh: false };
      var fresh = voiceStage.fresh;
      voiceStage.fresh = false;   // auto-present once per reply, never on refresh
      return { node: box, fresh: fresh };
    }

    // The spoken greeting is a per-device choice (Kris 2026-09-30, docs/spec-voice-ripples.md):
    // Off for everyone unless this device turned it On. Off = a silent open; the ripples and
    // the water sound say the screen is ready. No stored value = Off.
    var VOICE_GREETING_KEY = 'blam8.voiceGreeting';
    function savedVoiceGreeting() {
      try { return localStorage.getItem(VOICE_GREETING_KEY) === 'on' ? 'on' : 'off'; }
      catch (e) { return 'off'; }
    }
    function saveVoiceGreeting(value) {
      try { localStorage.setItem(VOICE_GREETING_KEY, value === 'on' ? 'on' : 'off'); return true; }
      catch (e) { return false; }
    }
    // Launch by button (#talk): start listening by itself when opened from a launch button. Per device,
    // default Off. Only #talk with this On ever tries it; #voice, #concierge, push links and reloads never do.
    var TALK_AUTO_KEY = 'blam8.talkAutoStart';
    function savedTalkAuto() {
      try { return localStorage.getItem(TALK_AUTO_KEY) === 'on' ? 'on' : 'off'; }
      catch (e) { return 'off'; }
    }
    function saveTalkAuto(value) {
      try { localStorage.setItem(TALK_AUTO_KEY, value === 'on' ? 'on' : 'off'); return true; }
      catch (e) { return false; }
    }
    // Open on the voice screen: the installed app lands on voice from its start page (base.html's head
    // script decides first-load-of-a-launch; the resume path is in the modal frame). Per device, default Off.
    var OPEN_VOICE_KEY = 'blam8.openOnVoice';
    function savedOpenOnVoice() {
      try { return localStorage.getItem(OPEN_VOICE_KEY) === 'on' ? 'on' : 'off'; }
      catch (e) { return 'off'; }
    }
    function saveOpenOnVoice(value) {
      try { localStorage.setItem(OPEN_VOICE_KEY, value === 'on' ? 'on' : 'off'); return true; }
      catch (e) { return false; }
    }
    // Can this page make sound without a tap? A real tap or a running AudioContext says yes. iOS
    // says no to a fresh load, so the voice screen stays on its "tap the mic" prompt. One probe, no retries.
    function soundAllowedWithoutTap() {
      try {
        if (navigator.userActivation && navigator.userActivation.isActive) return Promise.resolve(true);
        var Ctx = window.AudioContext || window.webkitAudioContext;
        if (!Ctx) return Promise.resolve(false);
        var probe = new Ctx();
        var done = function (ok) { try { probe.close(); } catch (e) {} return ok; };
        if (probe.state === 'running') return Promise.resolve(done(true));
        return Promise.race([
          probe.resume().then(function () { return probe.state === 'running'; }, function () { return false; }),
          new Promise(function (r) { setTimeout(function () { r(false); }, 400); }),
        ]).then(done, function () { return done(false); });
      } catch (e) { return Promise.resolve(false); }
    }
    function voiceGreeting() {
      if (savedVoiceGreeting() !== 'on') return "";
      var hour = new Date().getHours();
      var part = hour < 12 ? "Morning" : hour < 18 ? "Afternoon" : "Evening";
      // One word (Kris 2026-09-23): it plays inside the launch hold, and the
      // "Listening" label that follows is the invitation to talk.
      return part + ".";
    }

    function veil(on) {
      document.documentElement.classList.toggle('voice-landing', !!on);
    }
    // GPT-Live asks for the microphone the moment the user presses, in parallel with
    // loading the voice screen's modules, so the permission prompt and the module load
    // overlap instead of queueing. voice-gptlive.js takes the stream when it reaches
    // its capture stage; anything unclaimed is released after 60 s.
    function releasePreMic() {
      var pre = window.__blam8PreMic; window.__blam8PreMic = null;
      if (pre) pre.promise.then(function (stream) { stream.getTracks().forEach(function (t) { t.stop(); }); }, function () {});
    }
    // The mic opened here is handed to the voice screen, so it asks for what that screen would: automatic gain
    // off when the owner set a GPT-Live volume line and is not using hold to talk (= captureConstraints() in
    // voice-gptlive.js; a test pins the two equal). The keys are defined with the settings below.
    function liveAgcOff() {
      try {
        var v = parseFloat(localStorage.getItem(LIVE_LINE_KEY) || '');
        return v > 0 && v < 1 && localStorage.getItem(TALK_KEY) !== 'hold';
      } catch (e) { return false; }
    }
    function startPreMic() {
      releasePreMic();
      try {
        var pre = { at: performance.now(), promise: navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: !liveAgcOff() } }) };
        pre.promise.catch(function () {});
        window.__blam8PreMic = pre;
        setTimeout(function () { if (window.__blam8PreMic === pre) releasePreMic(); }, 60000);
      } catch (e) {}
    }
    async function openVoiceLive(opts) {
      timing('voice.click', { mode: savedVoiceMode() });
      debriefRequest = null; debriefState = null; paintDebrief();   // a new conversation starts with no walk
      if (savedVoiceMode() === 'standard' && !VOICE_ON) { veil(false); return; }
      if (savedVoiceMode() === 'gptlive' && root.getAttribute('data-gptlive') !== 'on') {
        veil(false); bubble('assistant', 'GPT-Live is unavailable here. Choose Standard or try later.'); return;
      }
      veil(true);
      if (savedVoiceMode() === 'gptlive' && !(opts && opts.paused)) startPreMic();
      try {
        await openVoiceLiveInner(opts);
      } catch (_) {
        timing('voice.open-error');
        releasePreMic();
        bubble('assistant', "Couldn't open voice just now. Tap the mic to try again.");
      } finally {
        veil(false);   // the voice screen is up (or couldn't be): the page shows again
      }
    }
    async function openVoiceLiveInner(opts) {
      var requestedMode = savedVoiceMode(), modeGeneration = voiceModeGeneration;
      if (requestedMode === 'standard') {
        await historyReady;
        if (modeGeneration !== voiceModeGeneration || savedVoiceMode() !== requestedMode) { releasePreMic(); return; }
      }
      if (!voiceLiveInstance && voiceInstances[requestedMode]) voiceLiveInstance = voiceInstances[requestedMode];
      if (!voiceLiveInstance) {
        var rev = "";
        try {
          var self = document.querySelector('script[src^="/static/concierge_panel.js"]');
          rev = self ? (self.src.split("?v=")[1] || "") : "";
        } catch (e) {}
        timing('voice.import-start', { mode: requestedMode });
        var mod = await moduleDeadline(import('/static/' + (requestedMode === 'gptlive' ? 'voice-gptlive.js' : 'voice-live.js') + (rev ? '?v=' + rev : '')));
        timing('voice.import-end', { mode: requestedMode });
        if (modeGeneration !== voiceModeGeneration || savedVoiceMode() !== requestedMode) { releasePreMic(); return; }
        if (voiceLiveInstance) { voiceLiveInstance.open(opts); return; }
        voiceLiveInstance = mod.createVoiceLive({
          onSettings: openSettingsFromVoice,
          confirmationBusy: function () { return pendingConfirmations > 0; },
          approvalWaiting: function () {
            var draft = voiceStage.draft;
            return Boolean(voiceStage.proposal || (draft && draft._draft && draft._draft.pending()));
          },
          taskRead: async function (session, signal) {
            var response = await fetch('/concierge/business/session-tasks/' + encodeURIComponent(session), { credentials: 'same-origin', signal: signal });
            if (!response.ok) throw new Error('Task read unavailable');
            return (await response.json()).tasks || [];
          },
          taskTransition: async function (body, signal) {
            var response = await fetch('/concierge/business/task-delivery', {
              method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken() },
              body: JSON.stringify(body), signal: signal,
            });
            if (!response.ok) throw new Error('Task delivery unavailable');
            return response.json();
          },
          apiFetch: function (action, body, signal) {
            return fetch("/concierge/voice/" + action, {
              method: "POST",
              headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
              body: JSON.stringify(body || {}),
              signal: signal,
            });
          },
          // Typed chat and voice send IDENTICAL payload shapes — the question is
          // the newest user turn, the history is everything before it.
          buildChatBody: function () {
            var turns = history.slice(-33);
            var last = turns.length ? turns[turns.length - 1] : null;
            return {
              question: last && last.role === "user" ? last.content : "",
              history: turns.slice(0, -1).slice(-CONTEXT_TURNS).map(function (m) {
                return { role: m.role, content: m.content, lookups: m.lookups, at: m.at || null };
              }),
            };
          },
          // A yes/no while a card or draft is waiting for one IS the answer:
          // it goes straight through, never held as "too quiet" (Kris
          // 2026-09-24: "When I say Yes to send the email… it removes it n
          // does nothing").
          expectsAnswer: function (text) {
            var dr = voiceStage.draft;
            var waiting = (dr && dr._draft && dr._draft.pending()) || Boolean(voiceStage.proposal);
            return waiting && (isBareAffirmation(text) || isBareDecline(text));
          },
          onUserTurn: function(text,meta){return voiceUserTurn(text,Object.assign({},meta,{origin:requestedMode==='gptlive'?'live':'voice'}));},
          onNativeQuestion: function (text, turnId) {
            voiceUserBubbles.set(turnId, voiceLog({role:'user', content:text, turn_id:turnId, origin:'live'}));
            // A delegated question is a new message unless it is a yes/no to the card
            // on screen (a bare one never reaches here: the server answers it with
            // "use the card"): the card steps aside as a waiting chip and the last
            // reply's links go, as voiceUserTurn does for Standard voice.
            var dr = voiceStage.draft;
            var waiting = (dr && dr._draft && dr._draft.pending()) || Boolean(voiceStage.proposal);
            if (waiting && (isBareAffirmation(text) || isBareDecline(text))) return;
            if (voiceStage.cardNode || voiceStage.links.length) clearVoiceCard();
          },
          onNativeTasks: function (rows) { (rows || []).forEach(applyTask); },
          onNativeTaskOpen: function (task, current) {
            if (task.proposal_id && task.result_kind === 'waiting') resurface({proposal_id:task.proposal_id, current:current});
            else if (task.proposal_id) {
              checkProposal(task.proposal_id, false).then(function (outcome) {
                if (!current()) return;
                voiceStage.proposal=null;voiceStage.cardNode=proposalCardNode(Object.assign({type:'recovery'}, outcome));
                if(voiceLiveInstance)voiceLiveInstance.refreshStage();
              });
            }
          },
          onAssistantTurn: function(payload){voiceAssistantTurn(Object.assign({},payload,{origin:requestedMode==='gptlive'?'live':'voice'}));},
          onDiscardTurn: voiceDiscardTurn,
          greeting: voiceGreeting,
          renderStage: voiceRenderStage,
          // Closing lets go of the card, not just parks it: the next
          // conversation starts on a clean screen (Kris 2026-09-23: a draft
          // from the last conversation was still there on reopening). The
          // card stays in the chat, still live, and in its thread.
          onClose: function (closing) {
            activeVoiceVisitId = '';
            activeVoiceMode = '';
            waitingOpen = false; doneOpen = false; clearVoiceCard(); scrollDown();
            paintBadge(); paintDoneBadge();
            var lifecycle=++historyLifecycle;
            Promise.resolve(closing).then(function(){
              refreshHistory({pinNewest:true});
              [500,1500,5000].forEach(function(delay){setTimeout(function(){if(lifecycle===historyLifecycle)refreshHistory({pinNewest:true});},delay);});
            });
          },
          onMount: mountBadge,
          onSessionStart: function (sessionId) { activeVoiceVisitId = sessionId || ''; activeVoiceMode = requestedMode; },
          contextFrom: function () { var cutoff = contextFrom(); return cutoff ? new Date(cutoff).toISOString() : null; },
          canStart: function () { return { ok: true }; },
        });
        voiceInstances[requestedMode] = voiceLiveInstance;
      }
      historyLifecycle++;historyReadGeneration++;
      voiceLiveInstance.open(opts);
      loadWaiting();
    }

    // ── the composer (acts as OurTab's) ─────────────────────────────────────
    // An empty box shows the mic, any text shows Send: one slot, toggled by
    // .has-text on the form (the CSS in the panel does the showing), so it is
    // a swap, not a reflow — and focusing alone never swaps. The send that
    // clears the box flows through here, so the mic comes straight back.
    function syncComposer() {
      form.classList.toggle('has-text', Boolean(input.value.trim()));
    }
    function setComposerBusy(on) {
      input.disabled = on;
      send.disabled = on;
      if (mic) mic.disabled = on;
      syncComposer();
    }
    input.addEventListener('input', function () {
      // One line, as an <input> would be: a pasted newline becomes a space.
      if (/[\r\n]/.test(input.value)) input.value = input.value.replace(/[\r\n]+/g, ' ');
      syncComposer();
    });
    // The mic is always there; where voice can't run (no OpenAI key on this
    // server, or switched off) the tap says so rather than doing nothing.
    function startVoice() {
      if (VOICE_ON || savedVoiceMode() === 'gptlive') { openVoiceLive(); return; }
      veil(false);
      bubble('assistant', "Voice conversations aren't switched on here yet — type your question instead.");
    }
    if (mic) mic.addEventListener('click', startVoice);
    syncComposer();

    // ── settings: the voice line + the tour (Kris 2026-09-23) ─────────────────
    // A meter shows the room (grey) and you (blue) off the mic, the same
    // constraints voice uses, and the slider sets the line a segment must
    // reach to count as you. Saved per device under voice-live.js's key;
    // voice reads it at the start of each conversation. The mic runs only
    // while this sheet is open.
    var sheet = root.querySelector('#cz-settings');
    var settingsFromLive = false;
    var VOICE_CHOICE_KEY = 'blam8.conciergeVoice';
    var VOICE_CHOICES = Array.from(root.querySelectorAll('[name="cz-voice"]'), function (radio) { return radio.value; });
    function savedConciergeVoice() {
      try {
        var value = localStorage.getItem(VOICE_CHOICE_KEY);
        return VOICE_CHOICES.indexOf(value) >= 0 ? value : (VOICE_CHOICES[0] || 'marin');
      } catch (e) { return VOICE_CHOICES[0] || 'marin'; }
    }
    function saveConciergeVoice(value) {
      if (VOICE_CHOICES.indexOf(value) < 0) return false;
      try { localStorage.setItem(VOICE_CHOICE_KEY, value); return true; }
      catch (e) { return false; }
    }
    function syncVoiceChoice() {
      if (!sheet) return;
      sheet.querySelectorAll('[name="cz-voice"]').forEach(function (radio) {
        radio.checked = radio.value === savedConciergeVoice();
      });
    }
    // A card shows only in the voice mode that uses it: data-cz-voices says "standard", "gptlive" or
    // "both". The sheet never offers a setting the chosen mode ignores. The mode is read each time the
    // sheet opens and each time the mode radio changes (the sheet cannot be opened during a conversation).
    function paintModeSections() {
      if (!sheet) return;
      var mode = savedVoiceMode();
      sheet.querySelectorAll('[data-cz-voices]').forEach(function (node) {
        var wants = node.getAttribute('data-cz-voices');
        node.hidden = !(wants === 'both' || wants === mode);
      });
    }
    var sampleAudio = null, sampleAbort = null, sampleContext = null;
    function stopVoiceSample() {
      if (sampleAbort) sampleAbort.abort();
      sampleAbort = null;
      if (sampleAudio) { sampleAudio.onended = null; try { sampleAudio.stop(); } catch (e) {} }
      sampleAudio = null;
      if (sampleContext) sampleContext.close().catch(function () {});
      sampleContext = null;
    }
    async function playVoiceSample(voice) {
      stopVoiceSample();
      var status = sheet.querySelector('#cz-voice-status');
      var controller = new AbortController();
      sampleAbort = controller;
      status.textContent = 'Loading sample…';
      try {
        // Unlock audio during the gesture, before the network wait (mobile).
        var AudioEngine = window.AudioContext || window.webkitAudioContext;
        var engine = new AudioEngine();
        sampleContext = engine;
        await engine.resume();
        if (controller.signal.aborted) return;
        var response = await fetch('/concierge/voice/sample', {
          method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken() },
          body: JSON.stringify({ voice: voice }), signal: controller.signal
        });
        if (!response.ok) {
          var error = await response.json().catch(function () { return {}; });
          throw new Error(response.status === 429 ? 'Sample limit reached. Please try again in an hour.' :
            (error.error || 'Couldn’t play the sample. Please try again.'));
        }
        var bytes = await response.arrayBuffer();
        if (controller.signal.aborted) return;
        var buffer = await engine.decodeAudioData(bytes);
        if (controller.signal.aborted) return;
        var audio = engine.createBufferSource();
        sampleAudio = audio;
        audio.buffer = buffer;
        audio.connect(engine.destination);
        audio.onended = function () { status.textContent = ''; stopVoiceSample(); };
        audio.start();
        status.textContent = 'Playing ' + voice.charAt(0).toUpperCase() + voice.slice(1) + ' sample.';
      } catch (e) {
        if (controller.signal.aborted) return;
        status.textContent = e.message || 'Couldn’t play the sample. Please try again.';
        stopVoiceSample();
      }
    }
    if (sheet) {
      sheet.querySelectorAll('[name="cz-voice"]').forEach(function (radio) {
        radio.addEventListener('change', function () {
          var saved = saveConciergeVoice(radio.value);
          sheet.querySelector('#cz-voice-status').textContent = saved ? 'Saved on this device.' : 'Couldn’t save the voice on this device.';
          syncVoiceChoice();
        });
      });
      sheet.querySelectorAll('[data-cz-voice-sample]').forEach(function (button) {
        button.addEventListener('click', function () { playVoiceSample(button.dataset.czVoiceSample); });
      });
      syncVoiceChoice();
    }
    var MIC_LINE_KEY = 'blam8.voiceMicLine';   // = voice-live.js MIC_LINE_KEY
    var LIVE_LINE_KEY = 'blam8.liveMicLine';   // = voice-line.js LINE_KEY
    var AUTO_FLOOR = 0.06, AUTO_OVER_ROOM = 4; // = MIN_SPEECH_PEAK, SPEECH_OVER_AMBIENT
    var LIVE_OFF_LEVEL = 0.5;                  // where an unset GPT-Live line is drawn: the right-hand edge of the meter
    var meter = null;
    // Levels are drawn on a square-root scale (0.5 = the top), so quiet rooms
    // and ordinary speech both get room on the bar.
    function toPos(level) { return Math.max(0, Math.min(1, Math.sqrt(Math.max(0, level) / 0.5))); }
    function toLevel(pos) { return pos * pos * 0.5; }
    // Each voice mode keeps its own line: GPT-Live's browser gain is on until a line is set and Standard's is
    // off, so the numbers do not carry over, and a Standard line must not start gating GPT-Live.
    function lineKey() { return savedVoiceMode() === 'gptlive' ? LIVE_LINE_KEY : MIC_LINE_KEY; }
    function readLine() {
      try { var v = parseFloat(localStorage.getItem(lineKey()) || ''); return v > 0 && v < 1 ? v : null; }
      catch (e) { return null; }
    }
    function writeLine(v) {
      try { if (v) localStorage.setItem(lineKey(), String(v)); else localStorage.removeItem(lineKey()); }
      catch (e) { /* private mode: the line lasts this visit only */ }
    }
    // The readout under the meter. GPT-Live has no automatic line: until the owner sets one it says so.
    function lineReadout(m, line) {
      var base = 'Room ' + Math.round(toPos(m.room) * 100) + ' · You ' + Math.round(toPos(m.you) * 100);
      if (m.live) return base + (m.mine ? ' · Line ' + Math.round(toPos(line) * 100) + ' (yours)' : ' · Line off');
      return base + ' · Line ' + Math.round(toPos(line) * 100) + (m.mine ? ' (yours)' : ' — drag to set');
    }
    // "Turn the line off" (GPT-Live): the slider cannot clear itself, its smallest value is 1.
    function turnLineOff() {
      writeLine(null);
      if (meter) { meter.mine = false; meter.line = LIVE_OFF_LEVEL; }
      var slider = sheet && sheet.querySelector('#cz-mic-line');
      var mark = sheet && sheet.querySelector('.cz-meter-line');
      if (slider) slider.value = '100';
      if (mark) mark.style.left = '100%';
    }
    function startMeter() {
      if (meter || !sheet || !navigator.mediaDevices) return;
      var read = sheet.querySelector('#cz-mic-read');
      var warn = sheet.querySelector('#cz-mic-warn');
      var slider = sheet.querySelector('#cz-mic-line');
      var roomBar = sheet.querySelector('.cz-meter-room');
      var nowBar = sheet.querySelector('.cz-meter-now');
      var lineMark = sheet.querySelector('.cz-meter-line');
      var offBtn = sheet.querySelector('#cz-mic-off');
      if (!slider) return;
      // The line only ever moves under your finger (Kris 2026-09-23: an
      // "automatic" line followed the room and dragged the thumb away mid-set
      // — "automatic is pointless"). Not set yet: it starts ONCE where voice's
      // own rule puts it for this room, after a moment of listening, then
      // stays put until you drag it.
      var saved = readLine(), live = savedVoiceMode() === 'gptlive';   // GPT-Live: no automatic line, the owner sets it or it is off
      var m = meter = { stream: null, ctx: null, raf: 0, room: 0, you: 0, youAt: 0, live: live,
                        line: saved || (live ? LIVE_OFF_LEVEL : AUTO_FLOOR), mine: !!saved, placedAt: saved || live ? 1 : 0 };
      function placeLine(level) {
        m.line = level;
        var pos = toPos(level);
        lineMark.style.left = (pos * 100) + '%';
        slider.value = String(Math.max(1, Math.round(pos * 100)));
      }
      slider.oninput = function () {
        m.line = toLevel(Number(slider.value) / 100);
        m.mine = true;
        m.placedAt = 1;
        writeLine(m.line);
        lineMark.style.left = (toPos(m.line) * 100) + '%';
      };
      placeLine(m.line);
      if (offBtn) offBtn.onclick = turnLineOff;
      navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: false, channelCount: 1 }
      }).then(function (stream) {
        if (meter !== m) { stream.getTracks().forEach(function (t) { t.stop(); }); return; }
        m.stream = stream;
        var Ctx = window.AudioContext || window.webkitAudioContext;
        m.ctx = new Ctx();
        var an = m.ctx.createAnalyser();
        an.fftSize = 1024;
        m.ctx.createMediaStreamSource(stream).connect(an);
        var buf = new Float32Array(an.fftSize);
        function tick() {
          if (meter !== m) return;
          an.getFloatTimeDomainData(buf);
          var peak = 0;
          for (var i = 0; i < buf.length; i += 1) { var a = Math.abs(buf[i]); if (a > peak) peak = a; }
          var now = performance.now();
          // The room: falls fast in the quiet, climbs slowly, so a word barely
          // moves it but a telly left on does. You: the recent loudest.
          if (!m.room) m.room = peak;
          else m.room = peak < m.room ? m.room * 0.9 + peak * 0.1 : m.room * 0.995 + peak * 0.005;
          if (!m.placedAt) m.placedAt = now;
          else if (!m.mine && m.placedAt > 1 && now - m.placedAt > 1500) {
            placeLine(Math.max(m.room * AUTO_OVER_ROOM, AUTO_FLOOR));   // once
            m.placedAt = 1;
          }
          var line = m.line;
          if (peak >= m.you || now - m.youAt > 2500) { m.you = peak; m.youAt = now; }
          nowBar.style.width = (toPos(peak) * 100) + '%';
          roomBar.style.width = (toPos(m.room) * 100) + '%';
          read.textContent = lineReadout(m, line);
          if (offBtn) offBtn.hidden = !(m.live && m.mine);
          var msg = '';
          if (m.live && !m.mine) msg = '';
          else if (toPos(line) - toPos(m.room) < 0.06) msg = 'The line is down at the room — background noise could count as you. Move it up.';
          else if (m.you > 0.01 && m.you < line && now - m.youAt < 2500 && toPos(m.you) > toPos(m.room) + 0.1)
            msg = "Your voice isn't reaching the line — move it down a little.";
          warn.hidden = !msg;
          warn.textContent = msg;
          m.raf = requestAnimationFrame(tick);
        }
        tick();
      }).catch(function () {
        if (meter !== m) return;
        read.textContent = "Couldn't open the mic — allow it for this site, then open settings again.";
      });
    }
    function stopMeter() {
      var m = meter;
      meter = null;
      if (!m) return;
      if (m.raf) cancelAnimationFrame(m.raf);
      if (m.stream) m.stream.getTracks().forEach(function (t) { t.stop(); });
      if (m.ctx) { try { m.ctx.close(); } catch (e) {} }
    }
    function showSettings(on) {
      if (!sheet) return;
      if (on === undefined) on = sheet.hidden;
      sheet.hidden = !on;
      if (on) { syncVoiceChoice(); paintTalk(); paintModeSections(); startMeter(); }
      else { stopMeter(); stopVoiceSample(); }
    }
    function openSettingsFromVoice() {
      var voice = voiceLiveInstance;
      settingsFromLive = true;
      voiceLiveInstance = null;
      return closeVoiceBeforeSettings(voice, function () {
        if (!settingsFromLive) return;
        window.history.pushState(Object.assign({}, window.history.state, { czVoiceSettings: true }), '');
        showSettings(true);
      });
    }
    function leaveSettings() {
      if (settingsFromLive && window.history.state && window.history.state.czVoiceSettings) { window.history.back(); return; }
      var resume = settingsFromLive;
      settingsFromLive = false;
      showSettings(false);
      if (resume) openVoiceLive({ paused: true });
    }
    window.addEventListener('popstate', function () {
      if (settingsFromLive && !(window.history.state && window.history.state.czVoiceSettings)) leaveSettings();
    });
    document.addEventListener('keydown', function (event) {
      if (event.key !== 'Escape' || !sheet || sheet.hidden) return;
      event.preventDefault(); event.stopImmediatePropagation(); leaveSettings();
    }, true);
    root.addEventListener('click', function (event) {
      if (event.target.closest && event.target.closest('[data-cz-settings]')) showSettings();
    });
    // Her voice's loudness (Kris 2026-09-23: "very quiet and my phone is full
    // volume"): a percentage over the phone's own volume, per device, under
    // voice-live.js's key; voice reads it at the start of each conversation.
    var VOICE_VOLUME_KEY = 'blam8.voiceVolume', VOICE_VOLUME_DEFAULT = 1.6;   // = voice-live.js
    function paintVolume() {
      var slider = sheet && sheet.querySelector('#cz-voice-vol');
      if (!slider) return;
      var v = VOICE_VOLUME_DEFAULT;
      try { var x = parseFloat(localStorage.getItem(VOICE_VOLUME_KEY) || ''); if (x >= 0.5 && x <= 3) v = x; } catch (e) {}
      slider.value = String(Math.round(v * 100));
      sheet.querySelector('#cz-voice-vol-read').textContent = Math.round(v * 100) + '%'
        + (Math.abs(v - VOICE_VOLUME_DEFAULT) < 0.001 ? ' (default)' : '');
    }
    if (sheet && sheet.querySelector('#cz-voice-vol')) {
      sheet.querySelector('#cz-voice-vol').addEventListener('input', function (e) {
        try { localStorage.setItem(VOICE_VOLUME_KEY, String(Number(e.target.value) / 100)); } catch (err) {}
        paintVolume();
      });
      sheet.querySelector('#cz-voice-vol-reset').addEventListener('click', function () {
        try { localStorage.removeItem(VOICE_VOLUME_KEY); } catch (err) {}
        paintVolume();
      });
      paintVolume();
    }
    function paintGreeting() {
      if (!sheet) return;
      var current = savedVoiceGreeting();
      sheet.querySelectorAll('[name="cz-greeting"]').forEach(function (radio) { radio.checked = radio.value === current; });
    }
    if (sheet && sheet.querySelector('[name="cz-greeting"]')) {
      sheet.querySelectorAll('[name="cz-greeting"]').forEach(function (radio) {
        radio.addEventListener('change', function () {
          var saved = saveVoiceGreeting(radio.value);
          sheet.querySelector('#cz-greeting-status').textContent = saved ? 'Saved on this device.' : 'Couldn’t save this on this device.';
          paintGreeting();
        });
      });
      paintGreeting();
    }
    function paintTalkAuto() {
      if (!sheet) return;
      var current = savedTalkAuto();
      sheet.querySelectorAll('[name="cz-talk-auto"]').forEach(function (radio) { radio.checked = radio.value === current; });
    }
    if (sheet && sheet.querySelector('[name="cz-talk-auto"]')) {
      sheet.querySelectorAll('[name="cz-talk-auto"]').forEach(function (radio) {
        radio.addEventListener('change', function () {
          var saved = saveTalkAuto(radio.value);
          sheet.querySelector('#cz-talk-auto-status').textContent = saved ? 'Saved on this device.' : 'Couldn’t save this on this device.';
          paintTalkAuto();
        });
      });
      paintTalkAuto();
    }
    function paintOpenOnVoice() {
      if (!sheet) return;
      var current = savedOpenOnVoice();
      sheet.querySelectorAll('[name="cz-open-voice"]').forEach(function (radio) { radio.checked = radio.value === current; });
    }
    if (sheet && sheet.querySelector('[name="cz-open-voice"]')) {
      sheet.querySelectorAll('[name="cz-open-voice"]').forEach(function (radio) {
        radio.addEventListener('change', function () {
          var saved = saveOpenOnVoice(radio.value);
          sheet.querySelector('#cz-open-voice-status').textContent = saved ? 'Saved on this device.' : 'Couldn’t save this on this device.';
          paintOpenOnVoice();
        });
      });
      paintOpenOnVoice();
    }
    // How you talk (GPT-Live only): hands-free, the usual, or hold Mist to talk. Per device, under
    // voice-talk.js's key; the voice screen reads it each time it opens, so a change applies from the next
    // conversation. Hands-free is stored as no key at all.
    var TALK_KEY = 'blam8.liveTalk';   // = voice-talk.js TALK_KEY
    function savedTalk() {
      try { return localStorage.getItem(TALK_KEY) === 'hold' ? 'hold' : 'hands_free'; }
      catch (e) { return 'hands_free'; }
    }
    function saveTalk(value) {
      if (value !== 'hold' && value !== 'hands_free') return false;
      try { if (value === 'hold') localStorage.setItem(TALK_KEY, 'hold'); else localStorage.removeItem(TALK_KEY); return true; }
      catch (e) { return false; }
    }
    function paintTalk() {
      if (!sheet) return;
      var current = savedTalk();
      sheet.querySelectorAll('[name="cz-talk"]').forEach(function (radio) { radio.checked = radio.value === current; });
      // The line is not used together with hold to talk: say so on the line card.
      var note = sheet.querySelector('#cz-mic-holdnote');
      if (note) note.hidden = current !== 'hold';
    }
    if (sheet && sheet.querySelector('[name="cz-talk"]')) {
      sheet.querySelectorAll('[name="cz-talk"]').forEach(function (radio) {
        radio.addEventListener('change', function () {
          var saved = saveTalk(radio.value);
          sheet.querySelector('#cz-talk-status').textContent = saved ? 'Saved on this device.' : 'Couldn’t save this on this device.';
          paintTalk();
        });
      });
      paintTalk();
    }
    if (sheet) {
      sheet.querySelector('[data-cz-settings-close]').addEventListener('click', leaveSettings);
      var tourBtn = sheet.querySelector('#cz-tour-start');
      if (tourBtn) tourBtn.addEventListener('click', function () {
        settingsFromLive = false;
        if (window.history.state && window.history.state.czVoiceSettings) window.history.back();
        showSettings(false);   // hands the mic back before voice asks for it
        if (!VOICE_ON) { startVoice(); return; }   // says why voice can't run here
        openVoiceLive().then(function () { if (voiceLiveInstance) voiceLiveInstance.startTour(); });
      });
    }

    // ── wiring ────────────────────────────────────────────────────────────────
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var q = input.value.trim();
      if (!q) return;
      input.value = '';
      syncComposer();
      ask(q);
    });
    // Enter sends — always, as in a one-line field (no Shift+Enter newline).
    // Not mid-composition: an IME's Enter picks the word.
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
    });
    if (window.Blam8Discovery) discovery = window.Blam8Discovery.mount(root, {
      csrf: csrfToken, ask: ask, canAsk: function () { return !busy; }
    });
    root.querySelector('#cz-clear').addEventListener('click', async function () {
      if (!confirm('Clear your concierge chat history?')) return;
      try {
        await fetch('/concierge/business/clear', {
          method: 'POST',
          headers: { 'X-CSRF-Token': csrfToken() }
        });
      } catch (e) {}
      if (root.closest('#cz-modal')) { history = []; reloadPanel(); }
      else window.location.reload();
    });
    scrollDown();
    loadWaiting();
    historyReady = seedHistory();

    // ── The task list (docs/plan-concierge-task-list.md, 2026-09-25) ──────────
    // Several separate things in one message are worked in the background;
    // this is the list of them, under her reply (typed) or on the stage
    // (voice), filled in live from the stream (live.js → 'blam8:task').
    var TASK_ICON = { queued: '○', working: '…', waiting: '⏳', done: '✓', needs_you: '?', failed: '✕', declined: '–' };
    var TASK_TONE = { queued: 'text-slate-400', working: 'text-brand animate-pulse', waiting: 'text-amber-600',
                      done: 'text-emerald-600', needs_you: 'text-indigo-600', failed: 'text-rose-600',
                      declined: 'text-slate-400' };
    var TASK_OPEN = { queued: 1, working: 1 };
    var taskRows = {};   // task id -> row element, across every list on screen
    // The list's header is its summary and its fold (Kris 2026-09-25: "The
    // tasks list should be collapsable too"): "4 tasks · 1 done · 3 waiting".
    function paintTaskHead(box) {
      var rows = Array.prototype.slice.call(box.querySelectorAll('[data-task]'));
      var n = { done: 0, waiting: 0, needs_you: 0, failed: 0, declined: 0, open: 0 };
      rows.forEach(function (r) {
        var st = r.getAttribute('data-status');
        if (TASK_OPEN[st]) n.open++; else if (n[st] !== undefined) n[st]++;
      });
      var bits = [rows.length + ' task' + (rows.length === 1 ? '' : 's')];
      if (n.open) bits.push(n.open + ' in progress');
      if (n.done) bits.push(n.done + ' done');
      if (n.waiting) bits.push(n.waiting + ' waiting');
      if (n.needs_you) bits.push(n.needs_you + ' need' + (n.needs_you === 1 ? 's' : '') + ' you');
      if (n.failed) bits.push(n.failed + " couldn't");
      if (n.declined) bits.push(n.declined + ' not done');
      box.querySelector('[data-t-count]').textContent = bits.join(' · ');
    }
    function verifiedReadLines(reads) {
      var lines = [];
      function visit(value, label, depth) {
        if (lines.length >= 30 || depth > 3 || value == null) return;
        if (Array.isArray(value)) { value.slice(0, 8).forEach(function (item) { visit(item, label, depth + 1); }); return; }
        if (typeof value === 'object') {
          Object.keys(value).forEach(function (key) {
            if (/^(id|tenant_id|user_id|payload|metadata|usage|tool|token|secret)$|_id$/.test(key)) return;
            visit(value[key], key.replace(/_/g, ' '), depth + 1);
          });
          return;
        }
        var text = String(value).slice(0, 240);
        lines.push((label ? label.charAt(0).toUpperCase() + label.slice(1) + ': ' : '') + text);
      }
      (reads || []).forEach(function (read) { visit(read.result, '', 0); });
      return lines.join('\n');
    }
    function paintTaskRow(row, t) {
      row._task = t;
      if (t.revision) {
        row.setAttribute('data-revision', t.revision);
        row.setAttribute('data-row-version', t.row_version || 0);
        row.removeAttribute('data-gone');
        row.style.display = '';
      }
      row.setAttribute('data-status', t.status || 'queued');
      var unverifiedDone = t.revision && t.status === 'done' && t.result_kind !== 'lookup_answer' && t.result_kind !== 'action_completed';
      row.querySelector('[data-t-icon]').textContent = unverifiedDone ? '?' : TASK_ICON[t.status] || '○';
      row.querySelector('[data-t-icon]').className = 'flex-none w-4 text-center font-semibold ' + (TASK_TONE[t.status] || 'text-slate-400');
      var out = row.querySelector('[data-t-out]');
      var said = t.status === 'working' ? 'Working on it…'
               : t.status === 'waiting' ? (t.outcome ? 'Preparation complete · Waiting for approval — ' + t.outcome + ' — tap to review' : 'Waiting for approval — tap to review')
               : (t.outcome || '');
      out.textContent = said;
      out.hidden = !said || t.status === 'queued';
      if (t.proposal_id) row.setAttribute('data-proposal', t.proposal_id);
      else row.removeAttribute('data-proposal');
      var reads = row.querySelector('[data-t-reads]');
      if (reads) reads.textContent = t.question || verifiedReadLines(t.reads);
      row.classList.toggle('cursor-pointer', t.status === 'waiting');
      row.classList.toggle('hover:bg-slate-50', t.status === 'waiting');
      var box = row.closest('[data-cz-tasks]');
      if (box) paintTaskHead(box);
      if (typeof sideChanged === 'function') sideChanged();   // the page's right-hand column repaints
    }
    function taskListNode(batch) {
      var box = document.createElement('div');
      box.className = 'rounded-2xl border border-slate-200 bg-white max-w-[85%] overflow-hidden';
      box.setAttribute('data-cz-tasks', batch.batch_id);
      var head = document.createElement('button');
      head.type = 'button';
      head.className = 'w-full flex items-center gap-2 px-3 py-1.5 text-left';
      head.setAttribute('aria-expanded', 'true');
      head.innerHTML = '<span class="text-[11px] font-semibold uppercase tracking-wider text-slate-400">Task list</span>'
        + '<span class="flex-1 min-w-0 truncate text-xs text-slate-500" data-t-count></span>'
        + '<svg class="h-4 w-4 flex-none text-slate-400 transition-transform" data-t-chev fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" aria-hidden="true"><path stroke-linecap="round" stroke-linejoin="round" d="m19.5 8.25-7.5 7.5-7.5-7.5"/></svg>';
      head.addEventListener('click', function () {
        var open = head.getAttribute('aria-expanded') !== 'true';
        head.setAttribute('aria-expanded', open ? 'true' : 'false');
        head.querySelector('[data-t-chev]').style.transform = open ? '' : 'rotate(-90deg)';
        Array.prototype.slice.call(box.querySelectorAll('[data-task]')).forEach(function (r) {
          // style, not the hidden attribute: the row's `flex` class beats [hidden]
          // (Kris 2026-09-25: the arrow turned, the rows stayed).
          r.style.display = (!open || r.hasAttribute('data-gone')) ? 'none' : '';
        });
      });
      box.appendChild(head);
      (batch.tasks || []).forEach(function (t) {
        var row = document.createElement('div');
        row.className = 'flex items-start gap-2 px-3 py-1.5 border-t border-slate-100 text-sm';
        row.setAttribute('data-task', t.id);
        var icon = document.createElement('span'); icon.setAttribute('data-t-icon', '');
        var body = document.createElement('div'); body.className = 'flex-1 min-w-0';
        var title = document.createElement('p'); title.className = 'font-medium text-navy'; title.textContent = t.title;
        var out = document.createElement('p'); out.className = 'text-xs text-slate-500 whitespace-normal'; out.setAttribute('data-t-out', '');
        body.appendChild(title); body.appendChild(out);
        row.appendChild(icon); row.appendChild(body);
        if (t.revision) {
          var reads = document.createElement('pre');
          reads.setAttribute('data-t-reads', '');
          reads.className = 'text-xs whitespace-pre-wrap break-words';
          body.appendChild(reads);
          var replacement = document.createElement('textarea');
          replacement.placeholder = 'Complete replacement request, including the target';
          replacement.className = 'w-full text-sm border rounded';
          replacement.hidden = true;
          replacement.addEventListener('click', function (event) { event.stopPropagation(); });
          body.appendChild(replacement);
          ['Revise', 'Cancel task'].forEach(function (label) {
            var button = document.createElement('button');
            button.type = 'button'; button.textContent = label;
            button.className = 'text-xs underline mr-3';
            button.addEventListener('click', async function (event) {
              event.stopPropagation();
              if (label === 'Revise' && replacement.hidden) { replacement.hidden = false; replacement.focus(); return; }
              var current = row._task;
              button.disabled = true;
              try {
                var response = await fetch('/concierge/business/task-control', {
                  method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken() },
                  body: JSON.stringify({ task_id: current.id, revision: current.revision,
                    operation: label === 'Revise' ? 'revise' : 'cancel', instruction: replacement.value }),
                });
                var result = await response.json();
                if (result.task) applyTask(result.task);
                if (!result.ok) out.textContent = result.state === 'too_late'
                  ? 'Execution has already started. Check the original result; a further change needs a new request.'
                  : result.summary || 'This task changed. Check its current state and try again.';
                else replacement.hidden = true;
              } catch (_e) { out.textContent = 'The response was lost. Check the task before trying again.'; }
              finally { button.disabled = false; pollTasks(); }
            });
            body.appendChild(button);
          });
        }
        row.addEventListener('click', async function () {
          var pid = row.getAttribute('data-proposal');
          if (row.getAttribute('data-status') === 'waiting' && pid) resurface({ proposal_id: pid });
          if (row.getAttribute('data-status') === 'needs_you' && pid) {
            if (focusOpenCard(pid)) return;
            var outcome = await checkProposal(pid, false);
            addCard(proposalCardNode(Object.assign({ type: 'recovery' }, outcome)), 'An unverified result is open, shown on the right.');
            scrollDown();
          }
        });
        taskRows[t.id] = row;
        box.appendChild(row);
        paintTaskRow(row, t);
        if (!t.revision && (t.status === 'done' || t.status === 'declined')) { row.setAttribute('data-gone', ''); row.style.display = 'none'; }
      });
      paintTaskHead(box);
      // A finished list drawn after a reload starts folded: the count says it all.
      if (batch.collapsed) head.click();
      return box;
    }
    // Lists drawn by the server (a reload) come alive the same way.
    hydrateTaskSlots(root);
    // One place a task's state lands, from the stream or from a poll.
    // Ticked (and "not done") rows fade after 3 s (Kris 2026-09-25); the
    // header still counts them.
    function applyTask(t) {
      var row = taskRows[t.id];
      if (!row) return false;
      var was = row.getAttribute('data-status');
      if (t.revision) {
        var oldRevision = Number(row.getAttribute('data-revision') || 0);
        var oldVersion = Number(row.getAttribute('data-row-version') || 0);
        if (t.revision < oldRevision || (t.revision === oldRevision && Number(t.row_version || 0) <= oldVersion)) return false;
        // Equal revision and version = nothing changed. Reporting it as "moved" made every
        // poll refresh the waiting/done lists, whose refresh polls tasks again: a loop of
        // ~3 requests a second per open Concierge while any task was open.
        paintTaskRow(row, t);
        return true;
      }
      if (was === t.status && (row.getAttribute('data-proposal') || null) === (t.proposal_id || null)) return false;
      paintTaskRow(row, t);
      if ((t.status === 'done' || t.status === 'declined') && !row.hasAttribute('data-gone')) {
        row.setAttribute('data-gone', '');
        fadeAway(row, null, 3000, true);
      }
      return true;
    }
    window.addEventListener('blam8:task', function (e) {
      // SSE is an opaque invalidation hint. Current, scoped DB rows decide
      // what the panel displays; tenant-wide events never supply private text.
      pollTasks();
    });
    // The safety net: while any row on screen is unfinished, ask every 3 s —
    // a phone that dropped the stream mid-list still catches up.
    var TASK_POLL = { queued: 1, working: 1, waiting: 1 };
    function pollTasks() {
      var ids = [];
      Object.keys(taskRows).forEach(function (id) {
        var row = taskRows[id];
        if (!row.isConnected || !TASK_POLL[row.getAttribute('data-status')]) return;
        var box = row.closest('[data-cz-tasks]');
        var batch = box && box.getAttribute('data-cz-tasks');
        if (batch && ids.indexOf(batch) < 0) ids.push(batch);
      });
      if (!ids.length) return;
      fetch('/concierge/business/tasks?batch=' + encodeURIComponent(ids.join(',')), { credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : { tasks: [] }; })
        .then(function (d) {
          var moved = false;
          (d.tasks || []).forEach(function (t) { if (applyTask(t)) moved = true; });
          if (moved) { loadWaiting(); loadDone(); }
        })
        .catch(function () {});
    }
    function anyTaskRunning() {
      return Object.keys(taskRows).some(function (id) {
        var row = taskRows[id], status = row.getAttribute('data-status');
        return row.isConnected && (status === 'queued' || status === 'working');
      });
    }
    setInterval(function () { if (document.visibilityState === 'visible' && anyTaskRunning()) pollTasks(); }, 3000);
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'visible') pollTasks();
    });

    // The brief's cog (Kris 2026-09-25): what the brief tells you, set on the
    // bubble itself — the same list and the same saved setting as Settings ›
    // Notifications. Only rendered on the newest brief, for an owner or admin.
    root.addEventListener('click', function (e) {
      var cog = e.target.closest('[data-cz-brief-cog]');
      if (cog) {
        var box = cog.closest('[data-cz-brief]').querySelector('[data-cz-brief-topics]');
        var open = box.classList.toggle('hidden') === false;
        cog.setAttribute('aria-expanded', open ? 'true' : 'false');
        if (open) box.scrollIntoView({ block: 'nearest' });
        return;
      }
      var save = e.target.closest('[data-cz-brief-save]');
      if (!save) return;
      var panel = save.closest('[data-cz-brief-topics]');
      var status = panel.querySelector('[data-cz-brief-status]');
      var picked = Array.prototype.slice.call(panel.querySelectorAll('input[type=checkbox]'))
        .filter(function (c) { return c.checked; }).map(function (c) { return c.value; });
      save.disabled = true;
      status.textContent = 'Saving…';
      fetch('/app/settings/debrief/topics', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken() },
        body: JSON.stringify({ topics: picked })
      }).then(function (r) {
        if (!r.ok) return r.text().then(function (t) { throw new Error(r.status + ' ' + t.slice(0, 80)); });
        status.textContent = 'Saved ✓ — from the next brief.';
      }).catch(function (err) {
        // Say why (2026-09-25: a save on the phone never arrived and the page
        // gave no clue) — the status code and the server's first words.
        status.textContent = "Couldn't save (" + String(err && err.message || err) + ") — try again.";
      }).then(function () { save.disabled = false; });
    });


    function afterVoiceRefresh() {
        // Back on the voice screen itself, paused for the tap (Kris
        // 2026-09-23: landing in the text chat "is not smooth at all").
        if (VOICE_ON) { resurfaceNewestOnLoad = true; openVoiceLive({ paused: true }); return; }
        veil(false);
        bubble('assistant', 'Refreshed — tap the mic to carry on talking.');
    }

    return {
      // Opening never focuses the textarea (Kris 2026-09-23): on a phone that
      // pops the keyboard over the chat before you've decided to type.
      show: function(options){scrollDown();refreshHistory();if(discovery && !(options && options.voiceOnly))discovery.reopen();},
      refreshHistory: refreshHistory,
      scrollDown: scrollDown,
      voice: startVoice,
      settings: function () { showSettings(); },
      hideSettings: function () {
        if (settingsFromLive && window.history.state && window.history.state.czVoiceSettings) window.history.back();
        settingsFromLive = false; showSettings(false);
      },
      // What the page's right-hand column (concierge_sidebar.js) reads and asks for. Read-only except
      // review(), which goes through the same code as a tap on a waiting chip (the card keeps its own checks).
      sidebar: {
        tasks: function () {
          return Object.keys(taskRows).map(function (id) { return taskRows[id]._task; }).filter(Boolean).map(function (t) {
            return { id: t.id, title: t.title, status: t.status, outcome: t.outcome || '', at: t.created_at ? Date.parse(t.created_at) : 0 };
          });
        },
        waiting: function () { return waitingItems.slice(); },
        refused: function () { return Object.assign({}, refused); },
        review: function (w, inColumn) {
          w._czColumn = !!inColumn;
          if (w.type === 'recovery') { if (focusOpenCard(w.proposal_id)) return; if (discovery) discovery.hide(); var rc = proposalCardNode(w); rc._czColumn = !!inColumn; addCard(rc, 'An unverified result is open, shown on the right.'); scrollDown(); }
          else resurface(w);
        },
        // The card in the Waiting box was put back ("Back to the list"): voice lets go of it, and it is waiting again from now.
        released: function (node) {
          var rid = node.getAttribute && (node.getAttribute('data-proposal-id') || node.getAttribute('data-log-id'));
          if (rid) delete justShown[rid];
          if (voiceStage.cardNode === node) clearVoiceCard(); else loadWaiting();
        },
        focusTask: function (id) {
          var r = taskRows[id], box = r && r.closest('[data-cz-tasks]');
          if (box && box.isConnected) box.scrollIntoView({ block: 'center' });
        },
        activity: function () { return { busy: agentNow.busy, step: agentNow.step, ran: agentNow.ran.slice(), links: agentNow.links.slice(), cardDone: agentNow.cardDone }; },
        done: function () { return doneItems.slice(); },
        // The chat's own context: where it starts (the New task tap) and what has been looked up since.
        context: function () {
          var seen = [], found = '';
          history.forEach(function (m) { (m.lookups || []).forEach(function (l) {
            var at = l.indexOf(' → found ');
            if (at > -1) found = l.slice(at + 9);
            var label = at > -1 ? l.slice(0, at) : l;
            if (seen.indexOf(label) < 0) seen.push(label);
          }); });
          return { from: contextFrom(), sources: seen, found: found };
        },
        onChange: function (fn) { root.addEventListener('cz:side', fn); },
        host: function (h) {
          sideHost = h;
          // A fast reply or a restored task list can arrive before the sidebar import finishes.
          if (sideHost && sideHost.active()) {
            Array.prototype.slice.call(messages.querySelectorAll('.cz-draft, [data-cz-tasks]')).forEach(function (node) {
              addCard(node, 'Your prepared item is shown on the right.');
            });
          }
        },
      },
      // Opened from a launch button (#talk). Landing is the same as #voice: the voice screen paused on
      // "tap the mic". Only with the per-device setting On, and only if the page may make sound without
      // a tap, does it start as a tap on the mic would (same start path, same allowance checks). Anything
      // short of that leaves the tap prompt up. One attempt; no retry.
      afterTalkLaunch: function () {
        var fallback = afterVoiceRefresh;
        if (!VOICE_ON && root.getAttribute('data-gptlive') !== 'on') { fallback(); return; }
        if (savedTalkAuto() !== 'on') { fallback(); return; }
        soundAllowedWithoutTap().then(function (ok) {
          if (!ok) { fallback(); return; }
          resurfaceNewestOnLoad = true;
          openVoiceLive();
        }, fallback);
      },
      afterVoiceRefresh: afterVoiceRefresh,
    };
  }

  var btn = document.getElementById('concierge-btn');

  // Hold the person icon for a second and the Concierge opens in live voice
  // (Kris 2026-09-23); a tap still opens the text chat. It fires at the one
  // second mark, finger still down, and the click that follows the release is
  // swallowed so it doesn't also toggle the modal. Moving off the icon or a
  // scroll cancels it.
  var HOLD_MS = 1000;
  // The press is the earliest sign voice may be wanted. On pointerdown (the hold takes a full second to
  // register) this starts loading the voice screen's modules, independent of the panel, so they are in
  // by the time the hold fires: ES module imports of one URL share a single load, so the import in
  // openVoiceLiveInner just receives the finished module. It opens nothing and never asks for the
  // microphone; a plain tap has only warmed the cache.
  function prepareVoiceModule() {
    try {
      // The saved mode is per person ('blam8.voiceMode:<user>') and the panel that knows the user is not
      // loaded yet, so any key on this device saying gptlive counts (a wrong guess only warms the cache).
      var mode = 'standard';
      try {
        for (var i = 0; i < localStorage.length; i++) {
          var k = localStorage.key(i);
          if (k && k.indexOf('blam8.voiceMode:') === 0 && localStorage.getItem(k) === 'gptlive') { mode = 'gptlive'; break; }
        }
      } catch (e) {}
      var self = document.querySelector('script[src^="/static/concierge_panel.js"]');
      var rev = self ? (self.src.split('?v=')[1] || '') : '';
      import('/static/' + (mode === 'gptlive' ? 'voice-gptlive.js' : 'voice-live.js') + (rev ? '?v=' + rev : ''))
        .catch(function () {});
    } catch (e) {}
  }
  function holdForVoice(el, onHold, onDown) {
    var timer = null, held = false;
    function cancel() { clearTimeout(timer); timer = null; el.classList.remove('cz-holding'); }
    el.addEventListener('pointerdown', function (e) {
      if (e.button !== 0) return;
      held = false;
      cancel();
      el.classList.add('cz-holding');
      if (onDown) { try { onDown(); } catch (err) {} }
      timer = setTimeout(function () {
        timer = null;
        held = true;
        el.classList.remove('cz-holding');
        if (navigator.vibrate) { try { navigator.vibrate(15); } catch (err) {} }
        onHold();
      }, HOLD_MS);
    });
    ['pointerup', 'pointerleave', 'pointercancel'].forEach(function (t) { el.addEventListener(t, cancel); });
    // A long press on a phone would otherwise raise the context menu.
    el.addEventListener('contextmenu', function (e) { e.preventDefault(); });
    el.addEventListener('click', function (e) {
      if (!held) return;
      held = false;
      e.preventDefault();
      e.stopImmediatePropagation();
    }, true);
  }

  // ── the page frame (/app/concierge) ───────────────────────────────────────
  var pageRoot = document.getElementById('cz-root');
  if (pageRoot && !pageRoot.closest('#cz-modal')) {
    var pageApi = mount(pageRoot);
    // A computer (fine pointer that can hover, the same test as "Concierge opens as") gets the right-hand
    // column on the full page; the window width is decided by the column's own CSS. A touch device and a
    // phone-width window get it too, as the right half of a swipeable strip. Never in the modal.
    try {
      if (window.matchMedia('(hover: hover) and (pointer: fine), (pointer: coarse), (max-width: 767px)').matches) {
        import('/static/concierge_sidebar.js').then(function (m) { m.mountSidebar(pageRoot, pageApi.sidebar); }).catch(function () {});
      }
    } catch (e) {}
    if (btn) {
      holdForVoice(btn, function () {
        document.documentElement.classList.add('voice-landing');
        pageApi.voice();
      }, prepareVoiceModule);
      btn.addEventListener('click', function () { pageApi.show(); });
    }
    if (location.hash === '#voice') {   // the voice screen's pull-to-refresh landing
      try { history.replaceState(null, '', location.pathname + location.search); } catch (e) {}
      pageApi.afterVoiceRefresh();
    } else if (location.hash === '#talk' || location.pathname === '/concierge/live') {   // a launch button (manifest shortcut, Shortcuts app)
      try { history.replaceState(null, '', location.pathname + location.search); } catch (e) {}
      pageApi.afterTalkLaunch();
    }
    return;
  }

  // ── the modal frame (every other app page) ────────────────────────────────
  var modal = document.getElementById('cz-modal');
  var slot = document.getElementById('cz-modal-body');
  if (!modal || !slot) return;
  var api = null, loading = null;

  function load() {
    if (api) return Promise.resolve(api);
    if (loading) return loading;
    var controller = new AbortController();
    slot.textContent = 'Opening…';
    timing('panel.fetch-start');
    var response = fetch('/app/concierge/panel', { credentials: 'same-origin', headers: { 'Accept': 'text/html' }, signal: controller.signal })
      .then(function (r) {
        timing('panel.headers', { status: r.status });
        if (!r.ok) throw new Error('http ' + r.status);
        return r.text();
      });
    // Race the entire body read. A late response cannot insert or mount a panel.
    loading = openingDeadline(response, function () { controller.abort(); })
      .then(function (html) {
        timing('panel.body');
        slot.innerHTML = html;   // our own server-rendered partial; data inside is escaped by Jinja
        var root = slot.querySelector('#cz-root');
        if (!root) throw new Error('Panel unavailable');
        // The strip's wrapper (display: contents until a phone switches it on, base.html): the panel, and beside it the column.
        var strip = document.createElement('div');
        strip.id = 'cz-pager'; strip.className = 'cz-pager';
        while (slot.firstChild) strip.appendChild(slot.firstChild);
        slot.appendChild(strip);
        timing('panel.mount-start');
        api = mount(root);
        // Live voice can start from the pop-up at any width.
        import('/static/concierge_sidebar.js').then(function (m) { m.mountSidebar(root, api.sidebar); }).catch(function () {});
        timing('panel.mount-end');
        return api;
      })
      .catch(function () {
        timing('panel.open-error');
        slot.textContent = '';
        var p = document.createElement('p');
        p.className = 'p-6 text-sm text-slate-500';
        p.textContent = "Couldn't open the Concierge just now. ";
        var retry = document.createElement('button');
        retry.type = 'button';
        retry.textContent = 'Try again';
        retry.className = 'underline';
        retry.addEventListener('click', open);
        p.appendChild(retry);
        slot.appendChild(p);
        return null;
      })
      .then(function (a) { loading = null; return a; });
    return loading;
  }
  function reloadPanel() {
    api = null;
    load().then(function (a) { if (a) a.show(); });
  }
  function isOpen() { return !modal.classList.contains('hidden'); }
  // Escape belongs to the voice overlay while it is up (voice-live.js).
  function voiceOverlayUp() {
    var v = document.getElementById('voice-live-root');
    return !!(v && !v.hidden && v.offsetParent !== null);
  }
  function open(voiceOnly) {
    timing('panel.click');
    // One overlay at a time: the search sheet and the App help chat step aside.
    var sb = document.getElementById('search-box');
    if (sb) sb.classList.add('hidden');
    if (window.blam8ConciergeClose) window.blam8ConciergeClose();
    modal.classList.remove('hidden');
    modal.classList.add('flex');
    document.documentElement.classList.add('cz-modal-open');
    if (btn) btn.setAttribute('aria-expanded', 'true');
    load().then(function (a) {
      if (!a || !isOpen()) return;
      var strip = slot.querySelector('#cz-pager');
      if (strip) strip.scrollLeft = 0;   // it opens on the Concierge, not on the column it was left on
      a.show({ voiceOnly: voiceOnly === true });
      timing('panel.interactive');
      checkForNewBrief();
    });
  }
  // The panel is loaded once and kept, so a brief that landed after that
  // (the 07:30 one, or /rig/brief) never showed until a reload (Kris
  // 2026-09-25). On each open, ask for the newest message; if it is a brief
  // newer than what's on screen, reload the conversation. Only for a brief —
  // your own chat is already on screen as you type it.
  function checkForNewBrief() {
    var root = slot.querySelector('#cz-root');
    var shown = root ? (root.getAttribute('data-cz-newest') || '') : '';
    fetch('/concierge/business/history', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        var msgs = (d && d.messages) || [];
        var last = msgs[msgs.length - 1];
        if (!last || last.role !== 'assistant' || !last.created_at || last.created_at <= shown) return;
        if (!/^\s*(Morning|Afternoon)\./.test(last.content || '')) return;
        if (isOpen() && !voiceOverlayUp()) reloadPanel();   // never under a live voice session
      })
      .catch(function () {});
  }
  // Kris 2026-09-25, second try: "I don't think new brief showed up in the
  // Concierge" — it was already OPEN, and the check above only ran on open.
  // Now it also runs when the live stream says a brief has landed
  // (live.js 'blam8:brief'), and when the app comes back to the front.
  window.addEventListener('blam8:brief', function () { if (isOpen()) checkForNewBrief(); });
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible' && isOpen()) checkForNewBrief();
  });
  function close() {
    modal.classList.remove('cz-kb');
    modal.style.height = '';
    modal.style.transform = '';
    modal.classList.add('hidden');
    modal.classList.remove('flex');
    document.documentElement.classList.remove('cz-modal-open');
    if (btn) { btn.setAttribute('aria-expanded', 'false'); }
    if (api) api.hideSettings();   // the meter's mic goes off with the Concierge
  }
  // iOS keyboard (Kris 2026-09-23): the keyboard doesn't shrink the page on
  // iOS, it pans it, so the fixed full-screen modal slid up and the navy
  // header left the screen. While the keyboard is up (the visible area is
  // clearly shorter than the window), the modal takes exactly the visible
  // area; otherwise the CSS height stands. The CSS height matters on its own
  // because iOS 26.0 standalone under-reports the viewport, so it is only
  // overridden while typing.
  var vv = window.visualViewport;
  function fitToKeyboard() {
    var kb = !!(vv && isOpen() && window.matchMedia('(max-width: 767px)').matches
                && modal.contains(document.activeElement)
                && vv.height < window.innerHeight * 0.85);
    modal.classList.toggle('cz-kb', kb);
    modal.style.height = kb ? vv.height + 'px' : '';
    modal.style.transform = kb ? 'translateY(' + vv.offsetTop + 'px)' : '';
    // Typing in the composer: keep the newest message in view above the keys.
    // (A card's draft box keeps its own place.)
    if (kb && api && document.activeElement && document.activeElement.id === 'cz-input') api.scrollDown();
  }
  if (vv) {
    vv.addEventListener('resize', fitToKeyboard);
    vv.addEventListener('scroll', fitToKeyboard);
    modal.addEventListener('focusin', function () { setTimeout(fitToKeyboard, 50); });
    modal.addEventListener('focusout', function () { setTimeout(fitToKeyboard, 50); });
  }

  // "Concierge opens as: Pop-up / Full screen" (per device, default Pop-up). Full screen only counts on a
  // computer (fine pointer that can hover, never the screen width), whatever another device saved. It
  // only decides what the button and the menu links open; #voice/#talk/#concierge landings, the hold for
  // voice and the voice screen are unchanged, and on /app/concierge itself this frame never runs.
  function fullScreenWanted() {
    try {
      return localStorage.getItem('blam8.conciergeOpensAs') === 'full'
        && window.matchMedia('(hover: hover) and (pointer: fine)').matches;
    } catch (e) { return false; }
  }
  function openConcierge() {
    if (fullScreenWanted()) { location.assign('/app/concierge'); return; }
    open();
  }
  window.blam8ConciergeOpen = openConcierge;
  window.blam8ConciergeHide = function () { if (isOpen()) close(); };

  if (btn) {
    holdForVoice(btn, function () {
      document.documentElement.classList.add('voice-landing');   // dark now, voice next
      if (!isOpen()) open(true);
      load().then(function (a) {
        if (a && isOpen()) a.voice();
        else document.documentElement.classList.remove('voice-landing');
      });
    }, function () {
      // Finger down: fetch and mount the panel and load the voice modules NOW, in parallel, during the
      // second the hold takes to register, so the hold finds both ready instead of starting them (about a
      // second of panel plus the module load, behind the navy screen). A plain tap gets the same head start.
      load();
      prepareVoiceModule();
    });
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      if (isOpen()) close(); else openConcierge();
    });
  }
  modal.addEventListener('click', function (e) {
    if (e.target === modal || (e.target.closest && e.target.closest('[data-cz-close]'))) close();
    // The cog in the bar: the panel's settings sheet (opens or closes it).
    else if (e.target.closest && e.target.closest('[data-cz-settings]')) {
      load().then(function (a) { if (a) a.settings(); });
    }
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && isOpen() && !voiceOverlayUp()) close();
  });
  // Any "Concierge" link (the menu's entry) opens the modal in place. Capture
  // phase: the menu stops its clicks bubbling, so a bubbling listener here
  // never heard it and the link navigated.
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('[data-concierge-open]');
    if (!a) return;
    if (fullScreenWanted()) return;   // the link's own href is /app/concierge: let it navigate
    e.preventDefault();
    e.stopPropagation();
    var menu = document.getElementById('menu');
    if (menu) menu.classList.add('hidden');
    var mb = document.getElementById('menu-btn');
    if (mb) mb.setAttribute('aria-expanded', 'false');
    open();
  }, true);
  // A link from elsewhere (a push, the old page address) arrives as #concierge.
  // #voice is the voice screen's own pull-to-refresh landing: the Concierge
  // reopens and says to tap the mic — a fresh page has no tap behind it yet,
  // and iOS won't open the mic or play sound without one.
  if (location.hash === '#concierge' || location.hash === '#voice' || location.hash === '#talk') {
    var wantVoice = location.hash === '#voice' || location.hash === '#talk', wantTalk = location.hash === '#talk';
    try { history.replaceState(null, '', location.pathname + location.search); } catch (e) {}
    open(wantVoice);
    if (wantVoice) load().then(function (a) {
      if (a) { if (wantTalk) a.afterTalkLaunch(); else a.afterVoiceRefresh(); }
      else document.documentElement.classList.remove('voice-landing');   // no panel: show the page
    });
  }
  // "Open on the voice screen": see base.html's head script for the first-load rule. Here: open it, unless a
  // tapped notification owns this landing. The panel's own afterTalkLaunch then applies (paused on "tap the
  // mic", or the auto-start when 'Launch by button' is On too).
  function openVoiceFromLaunch() {
    open(true);
    load().then(function (a) {
      if (a) a.afterTalkLaunch();
      else document.documentElement.classList.remove('voice-landing');
    });
  }
  function tapOwnsLanding() {
    return Promise.resolve(window.blam8TapLanding ? window.blam8TapLanding() : false).catch(function () { return false; });
  }
  if (window.__blam8OpenVoice && !location.hash) {
    window.__blam8OpenVoice = false;
    tapOwnsLanding().then(function (tapped) {
      if (tapped) document.documentElement.classList.remove('voice-landing');
      else openVoiceFromLaunch();
    });
  }
  // iOS often RESUMES a still-open web app (no reload) when the Action button opens it. Coming forward after
  // being away longer than VOICE_RESUME_MS, while on the plain start page with the setting On, counts as a launch.
  // Ten minutes: long enough that a glance at a text or another app never throws you out of what you were
  // doing, short enough that a press of the button later in the day still lands. Never while voice is open.
  function readOpenOnVoice() { try { return localStorage.getItem('blam8.openOnVoice'); } catch (e) { return null; } }
  var VOICE_RESUME_MS = 10 * 60 * 1000, hiddenAt = 0;
  function onStartPage() { return location.pathname.replace(/\/$/, '') === '/app' && !location.search && !location.hash; }
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'hidden') { hiddenAt = Date.now(); return; }
    var away = hiddenAt ? Date.now() - hiddenAt : 0;
    hiddenAt = 0;
    if (away < VOICE_RESUME_MS || readOpenOnVoice() !== 'on' || !onStartPage() || isOpen() || voiceOverlayUp()) return;
    tapOwnsLanding().then(function (tapped) { if (!tapped) openVoiceFromLaunch(); });
  });
})();
