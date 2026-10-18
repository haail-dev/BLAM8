/* Today only. Measure the email row, not a remembered pixel offset or the
   diary's assumed height. The existing shell owns safe areas and navigation. */
(function () {
  var root = document.querySelector('.today-drawer');
  if (!root) return;
  var regions = root.parentElement;
  var viewport = root.querySelector('.today-drawer-viewport');
  var sheet = root.querySelector('.today-drawer-sheet');
  var handle = root.querySelector('.today-drawer-handle');
  var email = sheet.querySelector('[data-tour="emails"]');
  var rows = Array.from(sheet.querySelectorAll(':scope > *'));
  if (!rows.length) { root.hidden = true; return; }
  var restingRow = email || rows[0];
  var extra = rows.slice(rows.indexOf(restingRow) + 1);
  var expanded = false, closedHeight = 0, openHeight = 0, drag = null;
  var suppressClick = false, clickTimer;
  handle.hidden = !extra.length;
  if (extra.length) {
    // Keep the control inside the email section without nesting a button in
    // its Usage link. No extra row or height is added to the sheet.
    var restingSection = document.createElement('div');
    restingSection.className = 'today-drawer-resting';
    restingRow.before(restingSection);
    restingSection.append(restingRow, handle);
    root.setAttribute('data-hint', '');
  }

  function reveal(height) { root.style.setProperty('--today-reveal', height + 'px'); }
  function measure() {
    // offsetTop isn't reliable here: links don't establish an offset parent.
    closedHeight = restingRow.getBoundingClientRect().bottom - sheet.getBoundingClientRect().top;
    var available = Math.max(0, regions.clientHeight);
    var constrained = closedHeight > available;
    root.dataset.constrained = String(constrained);
    if (!expanded && !constrained) viewport.scrollTop = 0;
    if (constrained) closedHeight = available * .75;
    // Keep a usable slice of the cards on short/landscape screens. The sheet
    // scrolls independently when all its rows cannot fit at once.
    openHeight = Math.max(closedHeight, Math.min(sheet.scrollHeight,
      available - Math.min(96, available * .25)));
    reveal(expanded ? openHeight : closedHeight);
  }
  function setOpen(open, focusHandle) {
    expanded = open && extra.length > 0;
    root.dataset.expanded = String(expanded);
    handle.setAttribute('aria-expanded', String(expanded));
    handle.setAttribute('aria-label', expanded ? 'Show less business overview' : 'Show more business overview');
    if (!expanded) {
      if (focusHandle || extra.some(function (row) { return row.contains(document.activeElement); })) handle.focus({preventScroll: true});
      viewport.scrollTop = 0;
    }
    extra.forEach(function (row) { row.inert = !expanded; });
    measure();
  }
  function interacted() { root.removeAttribute('data-hint'); }
  handle.addEventListener('click', function () { interacted(); setOpen(!expanded); });
  root.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && expanded) {
      event.preventDefault(); interacted(); setOpen(false, true);
    } else if (event.target === handle && (event.key === 'ArrowUp' || event.key === 'ArrowDown')) {
      event.preventDefault(); interacted(); setOpen(event.key === 'ArrowUp');
    }
  });
  // Clicks still follow the usage link; only a deliberate vertical pull
  // captures the pointer and suppresses that gesture's subsequent click.
  root.addEventListener('pointerdown', function (event) {
    if (!event.isPrimary || event.button !== 0 || !extra.length || drag) return;
    // The email header pulls the sheet up and pushes it back down; the arrow
    // does both from its own corner. Constrained rows keep their own scroll.
    if (!handle.contains(event.target) && (root.dataset.constrained === 'true' || !email || !email.contains(event.target))) return;
    interacted(); suppressClick = false; clearTimeout(clickTimer);
    drag = {id: event.pointerId, x: event.clientX, y: event.clientY,
      height: viewport.clientHeight, delta: 0, moved: false};
  });
  window.addEventListener('pointermove', function (event) {
    if (!drag || event.pointerId !== drag.id) return;
    var dy = drag.y - event.clientY, dx = Math.abs(drag.x - event.clientX);
    if (!drag.moved && Math.abs(dy) > 8 && Math.abs(dy) > dx) {
      drag.moved = true; root.setPointerCapture(event.pointerId);
    }
    if (!drag.moved) return;
    event.preventDefault(); drag.delta = dy;
    reveal(Math.max(closedHeight, Math.min(openHeight, drag.height + dy)));
  });
  root.addEventListener('dragstart', function (event) {
    if (drag) event.preventDefault(); // a usage-link drag must not become a native URL drag
  });
  function finish(event) {
    if (!drag || event.pointerId !== drag.id) return;
    var gesture = drag; drag = null;
    if (root.hasPointerCapture(event.pointerId)) root.releasePointerCapture(event.pointerId);
    if (gesture.moved) {
      suppressClick = true;
      clickTimer = setTimeout(function () { suppressClick = false; }, 400);
    }
    var cancelled = event.type !== 'pointerup';
    setOpen(!cancelled && Math.abs(gesture.delta) > 24 ? gesture.delta > 0 : expanded);
  }
  window.addEventListener('pointerup', finish);
  window.addEventListener('pointercancel', finish);
  root.addEventListener('lostpointercapture', function (event) {
    // Touch starts with implicit capture on the touched element. Transferring
    // that capture to the drawer is not cancellation of the active gesture.
    if (event.target === root) finish(event);
  });
  root.addEventListener('click', function (event) {
    if (!suppressClick) return;
    event.preventDefault(); event.stopImmediatePropagation(); suppressClick = false;
  }, true);
  // The Today tour can reveal its CRM stop before trying to scroll to it.
  root.addEventListener('today-overview-reveal', function () { interacted(); setOpen(true); });
  root.dataset.ready = '';
  setOpen(false);
  var pending = false;
  function scheduleMeasure() {
    if (pending) return;
    pending = true;
    requestAnimationFrame(function () { pending = false; if (!drag) measure(); });
  }
  if (window.ResizeObserver) {
    var observer = new ResizeObserver(scheduleMeasure);
    observer.observe(regions); observer.observe(sheet); observer.observe(restingRow);
  }
  function typing() {
    var el = document.activeElement;
    return !!el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
  }
  function fitToKeyboard() {
    // Mobile keyboards may resize only the visual viewport. Keep this page's
    // shell above that edge, including its existing safe-area/nav geometry,
    // but only while a keyboard is plausibly up: a text field has focus and
    // the visible area is clearly shorter than the window. iOS 26.0 installed
    // apps report a visual viewport ~50pt short of the screen (see base.html)
    // and sometimes skip the event when the keyboard closes; the shell and
    // its bottom bar must not be left lifted off the screen edge.
    // Pinch zoom keeps normal browser panning instead of reflowing the drawer.
    var visible = window.visualViewport, shell = root.closest('[data-pull-root]');
    if (visible && shell) {
      if (Math.abs(visible.scale - 1) < .01 && typing() && visible.height < window.innerHeight * .85) {
        shell.style.setProperty('--today-visible-height',
          Math.min(document.documentElement.clientHeight, visible.height + visible.offsetTop) + 'px');
      } else shell.style.removeProperty('--today-visible-height');
    }
  }
  function resize() {
    if (drag) finish({pointerId: drag.id, type: 'pointercancel'});
    fitToKeyboard();
    measure();
  }
  window.addEventListener('resize', resize);
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', resize);
    window.visualViewport.addEventListener('scroll', resize);
    // Focus moving in or out of a text field changes whether a keyboard counts;
    // that never cancels a pull in progress.
    var refit = function () { if (!drag) { fitToKeyboard(); measure(); } };
    document.addEventListener('focusin', function () { setTimeout(refit, 50); });
    document.addEventListener('focusout', function () { setTimeout(refit, 300); });
  }
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(scheduleMeasure);
  resize();
})();
