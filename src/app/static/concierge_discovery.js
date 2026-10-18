// Text-only discovery. The server owns scope and pre-submit validation.
// Gesture state stays in this panel's memory. The one thing kept in browser storage is a copy of
// the generated map, per person, so Explore opens at once (see the block below).
(function () {
  'use strict';
  // ── a copy of the map kept in this browser ────────────────────────────────
  // Writing the map takes about 25-35 s. The server keeps it six hours; this copy lets
  // the next open paint at once and quietly check it. It is never shown to another
  // person (scope), never kept once expired, and the server re-checks every request
  // that is chosen from it, so a stale copy can only fail safe (409, "Explore again").
  var LOCAL_KEY = 'blam8.discovery.v1', RECHECK_MS = 20 * 60 * 1000, MIN_LEFT_MS = 10 * 60 * 1000;
  function pageScope() {
    var m = document.querySelector('meta[name="blam8-discovery-scope"]');
    return m ? m.content : '';
  }
  function csrfCookie() {
    var m = document.cookie.match(/(?:^|;\s*)bl_csrf=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : '';
  }
  function postDiscovery(csrf) {
    return fetch('/concierge/business/discovery', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
      body: '{}'
    });
  }
  function readLocal(scope) {
    try {
      var saved = JSON.parse(localStorage.getItem(LOCAL_KEY) || 'null');
      if (!scope || !saved || saved.scope !== scope || !saved.payload || saved.payload.scope !== scope ||
          saved.payload.source === 'fallback' || !validPayload(saved.payload) ||
          !(Date.parse(saved.payload.expires_at) - Date.now() > MIN_LEFT_MS)) return null;
      return saved;
    } catch (e) { return null; }
  }
  function writeLocal(payload) {
    try {
      if (!payload || payload.source === 'fallback' || !payload.scope || payload.scope !== pageScope()) return;
      localStorage.setItem(LOCAL_KEY, JSON.stringify({ scope: payload.scope, savedAt: Date.now(), payload: payload }));
    } catch (e) { /* private mode, quota, blocked storage: the map just isn't kept */ }
  }
  function clearLocal() { try { localStorage.removeItem(LOCAL_KEY); } catch (e) {} }
  // As soon as BLAM8 has loaded, write (or refresh) the map in the background so Explore is
  // instant. Quiet, once per 20 minutes per browser, never on data saver or a hidden tab.
  function warm() {
    var scope = pageScope(), conn = navigator.connection;
    if (!scope || document.visibilityState === 'hidden' || (conn && conn.saveData)) return;
    var local = readLocal(scope);
    if (local && Date.now() - local.savedAt < RECHECK_MS) return;
    postDiscovery(csrfCookie()).then(function (r) { return r.ok ? r.json() : null; }).then(function (payload) {
      if (payload && validPayload(payload)) writeLocal(payload);
    }).catch(function () {});
  }
  function scheduleWarm() {
    setTimeout(function () { (window.requestIdleCallback || function (f) { f(); })(warm); }, 2500);
  }
  if (document.readyState === 'complete') scheduleWarm(); else window.addEventListener('load', scheduleWarm);

  function validPayload(payload) {
    var seen = Object.create(null), total = 0;
    return payload && payload.version === 1 && typeof payload.revision === 'string' &&
      Array.isArray(payload.groups) && payload.groups.length <= 24 && payload.groups.every(function (g) {
        return g && typeof g.id === 'string' && typeof g.title === 'string' && g.title.length <= 100 &&
          Array.isArray(g.requests) && g.requests.length <= 40 && g.requests.every(function (r) {
            total++;
            if (!r || typeof r.id !== 'string' || seen[r.id] || typeof r.text !== 'string' ||
                !r.text.trim() || r.text.length > 180 || !Array.isArray(r.capability_ids) || !r.capability_ids.length) return false;
            seen[r.id] = true;
            return total <= 300;
          });
      });
  }

  window.Blam8Discovery = { mount: function (root, options) {
    var host = root.querySelector('#cz-discovery');
    if (!host) return null;
    var transcript = root.querySelector('#cz-messages');
    var explore = root.querySelector('#cz-explore');
    var viewport = host.querySelector('[data-discovery-viewport]');
    var stage = host.querySelector('[data-discovery-stage]');
    var canvas = host.querySelector('[data-discovery-canvas]');
    var status = host.querySelector('[data-discovery-status]');
    function setStatus(message, actionable) {
      status.textContent = message;
      status.parentElement.classList.toggle('cz-discovery-nonvisual', !actionable);
    }
    var data = null, loading = false, sending = false, changed = false;
    var open = false, started = false, generation = 0, laidOut = false;
    var points = new Map(), gesture = null, pendingUpdate = null, suppressClickUntil = 0;
    var scale = 1, MIN_SCALE = 0.15, MAX_SCALE = 1.25, scroll = { x: 0, y: 0 };
    var padding = { x: 0, y: 0 };
    var scene = null, needsLayout = false, initialPositioned = false;
    var clusterColours = ['79,70,229', '13,148,136', '180,83,9', '147,51,234', '190,24,93', '3,105,161', '77,124,15', '194,65,12', '161,98,7', '15,118,110', '109,40,217', '51,65,85'];

    function post() { return postDiscovery(options.csrf()); }
    var valid = validPayload;
    function setDisabled(disabled) {
      canvas.querySelectorAll('[data-discovery-request]').forEach(function (b) { b.disabled = disabled; });
    }
    function sizeStage() {
      if (!open || !viewport.clientWidth || !viewport.clientHeight) { laidOut = false; return; }
      var restoring = !laidOut, positioning = !initialPositioned;
      if (needsLayout) layoutScene();
      var dx = viewport.clientWidth - padding.x, dy = viewport.clientHeight - padding.y;
      var left = (restoring || positioning ? scroll.x : viewport.scrollLeft) + dx;
      var top = (restoring || positioning ? scroll.y : viewport.scrollTop) + dy;
      padding = { x: viewport.clientWidth, y: viewport.clientHeight };
      // A viewport of space around the map lets a world point stay under the
      // cursor even when the scaled map fits inside the viewport. Native scroll
      // bounds must not move that anchor between successive wheel/pinch events.
      stage.style.width = Math.ceil(canvas.offsetWidth * scale + padding.x * 2) + 'px';
      stage.style.height = Math.ceil(canvas.offsetHeight * scale + padding.y * 2) + 'px';
      canvas.style.left = padding.x + 'px'; canvas.style.top = padding.y + 'px';
      canvas.style.transform = 'scale(' + scale + ')';
      viewport.dataset.discoveryScale = String(scale);
      laidOut = true;
      if (restoring || positioning || dx || dy) {
        viewport.scrollLeft = left; viewport.scrollTop = top;
        scroll = { x: viewport.scrollLeft, y: viewport.scrollTop };
      }
    }
    function layoutScene() {
      if (!scene || !open || !viewport.clientWidth) return false;
      scene.nodes.forEach(function (node) {
        node.width = node.element.offsetWidth;
        node.height = node.element.offsetHeight;
      });
      if (scene.nodes.some(function (node) { return !node.width || !node.height; })) return false;
      var columns = [24, 174], occupied = [], bottom = 0;
      function bounds(node, dx, dy) {
        return { left: node.localX + dx - node.width / 2, right: node.localX + dx + node.width / 2,
          top: node.localY + dy - node.height / 2, bottom: node.localY + dy + node.height / 2 };
      }
      function overlaps(a, b) {
        return a.left < b.right + 16 && a.right > b.left - 16 && a.top < b.bottom + 16 && a.bottom > b.top - 16;
      }
      scene.groups.forEach(function (group, index) {
        var hub = group.hub, requests = group.requests;
        hub.localX = 0; hub.localY = 0;
        var half = Math.max(hub.height / 2, requests[0] ? requests[0].height / 2 : 0,
          requests[1] ? requests[1].height / 2 + 20 : 0) + 30;
        var upper = [-half, -half - 18], lower = [half, half + 18];
        requests.forEach(function (node, i) {
          if (i < 2) { node.localX = i ? 250 : -250; node.localY = i ? 20 : 0; return; }
          var arm = (i - 2) % 4, side = arm % 2, layer = Math.floor((i - 2) / 4);
          node.localX = (side ? 120 : -120) + (layer % 2 ? (side ? -6 : 6) : 0);
          if (arm < 2) { node.localY = upper[side] - node.height / 2; upper[side] -= node.height + 28; }
          else { node.localY = lower[side] + node.height / 2; lower[side] += node.height + 28; }
        });
        var members = [hub].concat(requests), column = index % 2;
        var x = column ? 1060 : 365;
        var minY = Math.min.apply(null, members.map(function (n) { return n.localY - n.height / 2; }));
        var y = columns[column] - minY;
        // The broad cluster envelopes can gently interleave. Actual labels
        // retain measured clearance, including long text and uneven groups.
        for (var attempt = 0; attempt <= occupied.length; attempt++) {
          var shift = 0;
          members.forEach(function (node) {
            var box = bounds(node, x, y);
            occupied.forEach(function (other) {
              if (overlaps(box, other)) shift = Math.max(shift, other.bottom + 18 - box.top);
            });
          });
          if (!shift) break;
          y += shift;
        }
        var groupBottom = 0;
        members.forEach(function (node, i) {
          node.x = x + node.localX; node.y = y + node.localY;
          node.element.style.left = node.x - node.width / 2 + 'px';
          node.element.style.top = node.y - node.height / 2 + 'px';
          occupied.push(bounds(node, x, y));
          groupBottom = Math.max(groupBottom, node.y + node.height / 2);
        });
        columns[column] = groupBottom + 64;
        bottom = Math.max(bottom, groupBottom);
      });
      scene.width = 1440; scene.height = bottom + 24;
      canvas.style.width = scene.width + 'px'; canvas.style.height = scene.height + 'px';
      scene.svg.setAttribute('viewBox', '0 0 ' + scene.width + ' ' + scene.height);
      scene.edges.forEach(function (edge) {
        edge.line.setAttribute('x1', edge.from.x); edge.line.setAttribute('y1', edge.from.y);
        edge.line.setAttribute('x2', edge.to.x); edge.line.setAttribute('y2', edge.to.y);
      });
      scene.initialFocus = scene.nodes.find(function (node) { return node.kind === 'request'; });
      if (!initialPositioned && scene.initialFocus) {
        scroll.x = padding.x + Math.max(0, scene.initialFocus.x - scene.initialFocus.width / 2 - 20);
        scroll.y = padding.y + Math.max(0, scene.initialFocus.y - scene.initialFocus.height / 2 - 20);
        initialPositioned = true;
      }
      needsLayout = false;
      return true;
    }
    canvas.layoutDiscoveryScene = function () { needsLayout = true; return layoutScene(); };
    function localPoint(clientX, clientY) {
      var rect = viewport.getBoundingClientRect();
      return { x: clientX - rect.left - viewport.clientLeft, y: clientY - rect.top - viewport.clientTop };
    }
    function zoom(next, point, anchor) {
      point = point || { x: viewport.clientWidth / 2, y: viewport.clientHeight / 2 };
      anchor = anchor || { x: (viewport.scrollLeft + point.x - padding.x) / scale, y: (viewport.scrollTop + point.y - padding.y) / scale };
      scale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, next));
      sizeStage();
      viewport.scrollLeft = padding.x + anchor.x * scale - point.x;
      viewport.scrollTop = padding.y + anchor.y * scale - point.y;
      scroll = { x: viewport.scrollLeft, y: viewport.scrollTop };
    }
    function render(payload) {
      var unchanged = data && JSON.stringify(data.groups) === JSON.stringify(payload.groups);
      data = payload;
      if (!unchanged) {
        canvas.replaceChildren();
        var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.classList.add('cz-discovery-connections'); svg.setAttribute('aria-hidden', 'true');
        canvas.appendChild(svg);
        scene = { nodes: [], edges: [], groups: [], width: 1440, height: 0, svg: svg };
        canvas._discoveryScene = scene;
        payload.groups.forEach(function (g, index) {
          var island = document.createElement('section');
          island.className = 'cz-discovery-island';
          island.dataset.group = g.id;
          island.style.setProperty('--cluster', clusterColours[index % clusterColours.length]);
          var title = document.createElement('h3');
          title.className = 'cz-discovery-hub';
          title.id = 'cz-discovery-group-' + index;
          title.textContent = g.title;
          island.setAttribute('aria-labelledby', title.id);
          island.appendChild(title);
          var list = document.createElement('ul'); list.setAttribute('role', 'list'); list.className = 'cz-discovery-requests'; island.appendChild(list);
          var hub = { element: title, kind: 'hub', group: g.id };
          var group = { hub: hub, requests: [] }; scene.groups.push(group); scene.nodes.push(hub);
          g.requests.forEach(function (r) {
            var item = document.createElement('li'); item.className = 'cz-discovery-node';
            var b = document.createElement('button');
            b.type = 'button';
            b.textContent = r.text;
            b.dataset.discoveryRequest = r.id;
            b.addEventListener('click', function () { choose(r); });
            item.appendChild(b); list.appendChild(item);
            var node = { element: item, kind: 'request', group: g.id }; group.requests.push(node); scene.nodes.push(node);
            var line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
            line.style.stroke = 'rgba(' + clusterColours[index % clusterColours.length] + ',0.24)';
            svg.appendChild(line); scene.edges.push({ from: hub, to: node, line: line });
          });
          canvas.appendChild(island);
        });
        needsLayout = true;
        if (observer) {
          observer.disconnect(); observer.observe(viewport); observer.observe(canvas);
          scene.nodes.forEach(function (node) { observer.observe(node.element); });
        }
      }
      var count = canvas.querySelectorAll('[data-discovery-request]').length;
      setStatus((changed ? 'Updated · ' : '') + (count ? count + ' requests' : 'Ask below to begin.'));
      if (payload.source === 'fallback' && count) setStatus('Starting points available');
      setDisabled(false);
      sizeStage();
      viewport.scrollLeft = scroll.x;
      viewport.scrollTop = scroll.y;
      writeLocal(payload);
    }
    function install(update) {
      if (update.payload) render(update.payload);
      else {
        canvas.replaceChildren(); data = null; scene = null; canvas._discoveryScene = null;
        sizeStage();
        setStatus('Suggestions unavailable. Ask below.', true);
        clearLocal();
      }
    }
    async function load(quiet) {
      if (loading) return;
      loading = true;
      var ticket = ++generation;
      // With a saved copy on screen the check is silent: nothing greys out or changes.
      if (!quiet) { setDisabled(true); setStatus('Finding requests…'); }
      var update;
      try {
        var response = await post();
        if (!response.ok) { var failure = new Error('unavailable'); failure.status = response.status; throw failure; }
        var payload = await response.json();
        if (!valid(payload)) throw new Error('invalid');
        update = { payload: payload };
      } catch (e) {
        // A failed read cannot prove an old scope is still authorised. Offline or a server
        // hiccup behind a saved copy is different: the copy stays, and the server checks
        // every request chosen from it anyway.
        update = quiet && !(e.status >= 400 && e.status < 500) ? null : { failed: true };
      } finally { loading = false; }
      if (ticket !== generation || !root.isConnected || !update) return;
      // A failed rewrite of the map must not downgrade a good copy to the five fallback requests.
      if (quiet && update.payload && update.payload.source === 'fallback' && data && data.source !== 'fallback') return;
      // Both success and failure wait for ALL fingers to leave. Nothing
      // replaces or removes a request under a stationary finger or a pinch.
      if (points.size) pendingUpdate = update;
      else install(update);
    }
    async function choose(request) {
      if (sending || loading || pendingUpdate || points.size || !open || Date.now() < suppressClickUntil || !options.canAsk() || !data) return;
      sending = true; changed = false; setDisabled(true);
      try {
        // Exact visible text, once, through the normal chat handler.
        await options.ask(request.text, { discovery_revision: data.revision, discovery_request_id: request.id });
      } catch (e) {
        setStatus('Could not send this request. Ask below.', true);
      } finally {
        sending = false;
        if (!loading && !pendingUpdate) setDisabled(false);
      }
    }
    function show() {
      started = true; open = true;
      host.hidden = false; transcript.hidden = true; explore.hidden = true;
      sizeStage(); viewport.scrollLeft = scroll.x; viewport.scrollTop = scroll.y;
      var saved = !data && readLocal(pageScope());
      if (saved) render(saved.payload);          // instant; the check below is silent
      load(!!data);
    }
    function hide(focusTranscript) {
      started = true;
      scroll = { x: viewport.scrollLeft, y: viewport.scrollTop };
      open = false;
      host.hidden = true; transcript.hidden = false; explore.hidden = false;
      if (focusTranscript) { transcript.tabIndex = -1; transcript.focus({ preventScroll: true }); }
    }
    explore.addEventListener('click', function () {
      show();
      var heading = host.querySelector('h2'); heading.tabIndex = -1; heading.focus({ preventScroll: true });
    });

    function capturePoints() {
      points.forEach(function (_, id) { if (!viewport.hasPointerCapture(id)) viewport.setPointerCapture(id); });
    }
    function startGesture(used) {
      var active = Array.from(points.values());
      if (active.length >= 2) {
        var middle = localPoint((active[0].x + active[1].x) / 2, (active[0].y + active[1].y) / 2);
        gesture = { type: 'pinch', distance: Math.max(1, Math.hypot(active[1].x - active[0].x, active[1].y - active[0].y)),
          scale: scale, anchor: { x: (viewport.scrollLeft + middle.x - padding.x) / scale, y: (viewport.scrollTop + middle.y - padding.y) / scale }, used: true };
        suppressClickUntil = Date.now() + 500;
        capturePoints();
        viewport.classList.add('is-dragging');
      } else if (active.length) {
        gesture = { type: 'pan', x: active[0].x, y: active[0].y, left: viewport.scrollLeft, top: viewport.scrollTop, used: !!used };
      } else gesture = null;
    }
    viewport.addEventListener('pointerdown', function (e) {
      if (e.button !== 0 || points.has(e.pointerId)) return;
      points.set(e.pointerId, { x: e.clientX, y: e.clientY });
      startGesture(gesture && gesture.used);
    });
    viewport.addEventListener('pointermove', function (e) {
      if (!points.has(e.pointerId) || !gesture) return;
      points.set(e.pointerId, { x: e.clientX, y: e.clientY });
      var active = Array.from(points.values());
      if (gesture.type === 'pinch' && active.length >= 2) {
        var distance = Math.hypot(active[1].x - active[0].x, active[1].y - active[0].y);
        var middle = localPoint((active[0].x + active[1].x) / 2, (active[0].y + active[1].y) / 2);
        zoom(gesture.scale * distance / gesture.distance, middle, gesture.anchor);
        e.preventDefault();
        return;
      }
      var dx = active[0].x - gesture.x, dy = active[0].y - gesture.y;
      if (!gesture.used && Math.hypot(dx, dy) <= 8) return;
      gesture.used = true;
      capturePoints(); viewport.classList.add('is-dragging');
      viewport.scrollLeft = gesture.left - dx;
      viewport.scrollTop = gesture.top - dy;
      e.preventDefault();
    });
    function endPointer(e) {
      if (!points.has(e.pointerId)) return;
      var used = gesture && gesture.used;
      if (used || pendingUpdate || e.type === 'pointercancel') suppressClickUntil = Date.now() + 500;
      points.delete(e.pointerId);
      if (viewport.hasPointerCapture(e.pointerId)) viewport.releasePointerCapture(e.pointerId);
      startGesture(used);
      scroll = { x: viewport.scrollLeft, y: viewport.scrollTop };
      if (!points.size) {
        viewport.classList.remove('is-dragging');
        if (pendingUpdate) { var update = pendingUpdate; pendingUpdate = null; install(update); }
      }
    }
    viewport.addEventListener('pointerup', endPointer);
    viewport.addEventListener('pointercancel', endPointer);
    viewport.addEventListener('lostpointercapture', function (e) {
      // Ignore capture moving from a request button to this viewport. Losing
      // OUR capture unexpectedly must not leave a gesture waiting forever.
      if (e.target === viewport && points.has(e.pointerId)) endPointer({ pointerId: e.pointerId, type: 'pointercancel' });
    });
    viewport.addEventListener('pointerleave', function (e) {
      if (e.pointerType === 'mouse' && points.has(e.pointerId) && gesture && !gesture.used) endPointer(e);
    });
    viewport.addEventListener('click', function (e) {
      if (points.size || pendingUpdate || Date.now() < suppressClickUntil) { e.preventDefault(); e.stopImmediatePropagation(); }
    }, true);
    viewport.addEventListener('wheel', function (e) {
      // Wheel zoom stays local to this map; Ctrl-wheel trackpad pinch uses
      // the same pointer anchor and sensitivity as an ordinary mouse wheel.
      e.preventDefault();
      suppressClickUntil = Date.now() + 500;
      // Normalise pixel, line and page units before applying sensitivity. A
      // conventional 100/120-pixel mouse notch changes scale by about 6.6%,
      // rather than jumping straight to a bound. Fine trackpad deltas remain
      // proportional; exceptionally large events are capped to the same step.
      var unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? viewport.clientHeight : 1;
      var pixels = Math.max(-80, Math.min(80, e.deltaY * unit));
      zoom(scale * Math.exp(-pixels * 0.0008), localPoint(e.clientX, e.clientY));
    }, { passive: false });
    viewport.addEventListener('keydown', function (e) {
      if (e.altKey || e.ctrlKey || e.metaKey) return;
      var directions = { ArrowLeft: [-180, 0], ArrowRight: [180, 0], ArrowUp: [0, -180], ArrowDown: [0, 180] };
      var d = directions[e.key];
      var focusBox = e.target.matches('[data-discovery-request]') ? e.target.getBoundingClientRect() : null;
      var centre = focusBox ? localPoint(focusBox.left + focusBox.width / 2, focusBox.top + focusBox.height / 2) : null;
      if (d) { e.preventDefault(); viewport.scrollBy(d[0], d[1]); }
      else if (e.key === '+' || e.key === '=') { e.preventDefault(); zoom(scale * 1.15, centre); }
      else if (e.key === '-') {
        e.preventDefault(); zoom(scale / 1.15, centre);
        // Keyboard overview has no mouse target to preserve. At the minimum,
        // centre the map so its full extent is easy to find on a small screen.
        if (scale === MIN_SCALE) {
          viewport.scrollLeft = padding.x + (canvas.offsetWidth * scale - viewport.clientWidth) / 2;
          viewport.scrollTop = padding.y + (canvas.offsetHeight * scale - viewport.clientHeight) / 2;
          scroll = { x: viewport.scrollLeft, y: viewport.scrollTop };
        }
      }
      else if (e.key === '0') { e.preventDefault(); zoom(1, centre); }
    });
    viewport.addEventListener('focusin', function (e) {
      if (!e.target.matches('[data-discovery-request]')) return;
      var box = e.target.getBoundingClientRect(), frame = viewport.getBoundingClientRect(), pad = 10;
      var dx = box.width > viewport.clientWidth - pad * 2 || box.left < frame.left + pad ? box.left - frame.left - pad : box.right > frame.right - pad ? box.right - frame.right + pad : 0;
      var dy = box.height > viewport.clientHeight - pad * 2 || box.top < frame.top + pad ? box.top - frame.top - pad : box.bottom > frame.bottom - pad ? box.bottom - frame.bottom + pad : 0;
      viewport.scrollBy(dx, dy);
    });
    viewport.addEventListener('scroll', function () {
      if (open && laidOut && viewport.clientWidth && viewport.clientHeight) scroll = { x: viewport.scrollLeft, y: viewport.scrollTop };
    });
    if (window.ResizeObserver) {
      var observer = new ResizeObserver(function () {
        if (!root.isConnected) { observer.disconnect(); return; }
        if (scene && scene.nodes.some(function (node) { return node.element.offsetWidth !== node.width || node.element.offsetHeight !== node.height; })) needsLayout = true;
        sizeStage();
      });
      observer.observe(viewport); observer.observe(canvas);
    }
    // Modal mounting also happens on hold-for-voice. Only text opens discovery.
    if (!root.closest('#cz-modal') && location.hash !== '#voice' && root.dataset.czEmpty === 'true') show();
    return {
      hide: hide,
      reopen: function () {
        if (open) { sizeStage(); viewport.scrollLeft = scroll.x; viewport.scrollTop = scroll.y; load(!!data); }
        else if (!started && root.dataset.czEmpty === 'true') show();
      },
      stale: function () {
        changed = true; data = null; scene = null; canvas._discoveryScene = null; canvas.replaceChildren(); clearLocal(); show();
        var heading = host.querySelector('h2'); heading.tabIndex = -1; heading.focus({ preventScroll: true });
      }
    };
  } };
})();
