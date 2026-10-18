/* The first walk of a page (Kris 2026-09-13, at the "done" moment of the
   kennels re-run: "once the onboarding has finished and the chat has
   auto-closed, the Who-needs-you-next box outline glowing/pulsing with a
   connected speech bubble explaining briefly what that area does; when
   clicked, the same for Emails this month and CRM").

   One page at a time: the shell renders #tour-root with data-tour-page when
   this page's tour is still owed (src/app/dashboard.py hands it over once every
   setup step is done and the page has never been toured). The stops live
   here, keyed by page; each anchors to a [data-tour="…"] element on the
   page, breathes its outline, and speaks in a bubble tied to it. Next (or a
   tap on the glowing box) moves on; Done or Skip stamps the page toured, so
   it never nags. Nothing here is fetched except the one stamp. */
(function () {
  "use strict";
  var root = document.getElementById("tour-root");
  if (!root) return;
  var page = root.getAttribute("data-tour-page") || "";
  var csrf = root.getAttribute("data-csrf") || "";
  var plural = (root.getAttribute("data-kind-plural") || "").trim();

  var STOPS = {
    today: [
      { sel: '[data-tour="next"]', title: "Who needs you next",
        text: "Anyone who needs you lands here — a reply I've drafted for you to approve, " +
              "a promise coming due, today's bookings. Empty means nothing's waiting." },
      { sel: '[data-tour="order"]', title: "Today's order",
        text: "Tap the arrows to set what I put first when someone needs you — move the " +
              "topics into your own order and the cards stack that way.",
        passthrough: true },
      { sel: '[data-tour="emails"]', title: "Emails this month",
        text: "What your agent is doing against your allowance — emails drafted, chats " +
              "with me, and what customers do on your portal. Your trial covers the first " +
              "hundred emails; I'll say when you're getting close." },
      { sel: '[data-tour="crm"]', title: "CRM",
        text: function () {
          var locked = !!document.querySelector('[data-tour="crm"][data-locked]');
          return "Every customer who emails gets a card here by itself" +
                 (plural ? ", with their " + plural + " on it" : "") +
                 (locked ? ". It's already filling itself in from your inbox — unlock it on Pro to open the cards."
                         : ". Tap it any time to open your contacts.");
        } },
      // The diary rail along the bottom (Kris 2026-09-13: "a step 5 for the
      // Diary bar at the bottom, showing what's on today's plate").
      { sel: '[data-tour="diary"]', title: "Today's diary",
        text: "What's on the plate today — every booking in its slot, and how much room " +
              "is left. Tap it to open the day." },
      // Kris 2026-09-25 (plumber rehearsal): he reached for the search icon by
      // instinct, and nothing had shown him where People lives.
      { sel: '[data-tour="search"]', title: "Search, or just ask",
        text: "Find anyone or anything — a customer, a booking reference, an invoice. Or type " +
              "a question like \"how do I add staff?\" and I'll answer it and take you there." },
      { sel: '[data-tour="menu"]', title: "Settings and your team",
        text: "Everything else lives in here. Settings → People is where your team goes — " +
              "invite someone who'll use the app, or add a freelancer or helper who won't — " +
              "their jobs are sent to them as job sheets. It's free on every plan." },
    ],
    inbox: [
      // Kris 2026-09-13: "once the inbox is opened, show the top email thread
      // sliding right to display the Ignore function… do the sliding action
      // and reset a couple of times to catch the eye, with the speech bubble
      // explanation of why to ignore people now."
      { sel: '[data-tour="row"]', title: "Ignore the noise first", demo: "swipe",
        text: "Swipe a row to the right and that sender is ignored for good — newsletters, " +
              "notifications, anyone I should never answer. Do it now, before drafting is on, " +
              "and nothing gets wasted on them. Swipe left instead to open their contact card." },
      { sel: '[data-tour="row"]', title: "Your conversations",
        // The count read off the page, no added prose (Kris 2026-09-13).
        text: function () {
          var n = document.querySelectorAll("[data-before-blam8]").length;
          return (n ? n + (n === 1 ? " conversation" : " conversations") + " from before BLAM8 — brought in, never answered by me. "
                    : "One row per person, newest first. ") + "Tap a row to open it.";
        } },
      { sel: '[data-tour="pills"]', title: "What needs you",
        // Kris 2026-09-13: say that the rows take these colours.
        text: "These pills sort the inbox by state — needs a reply, draft ready, quote " +
              "sent, booked, or just for your information. Every thread from here on wears " +
              "the colour of its pill, so you can read the inbox at a glance. Tap a pill to " +
              "filter, All to clear." },
      // The switch in the last bubble (Kris 2026-09-13: "end the walk where the
      // product starts working") — the trip to Settings is where owners get lost.
      { sel: '[data-tour="drafting"]', title: "Drafting is off until you say",
        text: "I read everything that lands but write nothing yet. Robots are already filed " +
              "under Ignored. Turn drafting on now and every reply I write is one you wanted — " +
              "or leave it off for the moment.",
        choice: { yes: "Turn drafting on", no: "Not yet",
                  post: "/app/settings/drafting", body: "mode=review" } },
    ],
    diary: [
      // Kris 2026-09-13: "instead of clicking Done, we click Diary. Then run
      // a tour in the Diary." Today's cell first — the month at a glance.
      { sel: '[data-tour="today"]', title: "Your diary",
        text: function () {
          var wrap = document.querySelector("[data-tour-diary]");
          var stays = !!wrap && wrap.getAttribute("data-tour-diary") === "stays";
          return (stays ? "Every day says who's in against your spaces and what's free, with any appointments beside them. "
                        : "Every day says what's booked and what's free. ") +
                 "Today is ringed. Tap a day to see its bookings, add one by hand, or cancel one.";
        } },
      { sel: '[data-tour="colour"]', title: "How to read a day",
        // Kris 2026-09-13: "split the box depending how many diary types get
        // used, each with a bar growing upwards until capacity is full".
        text: function () {
          var wrap = document.querySelector("[data-tour-diary]");
          var n = wrap ? parseInt(wrap.getAttribute("data-tour-types") || "1", 10) || 1 : 1;
          return (n > 1
                  ? "Each day is split into one bar per diary — your spaces first, then each appointment " +
                    "service in its own colour. A bar grows from the bottom as that diary fills and reaches " +
                    "the top when it's full; the day only says Full when every bar is. "
                  : "The bar in each day grows from the bottom as it fills and reaches the top when you're full. ") +
                 "Amber is what I've offered to a customer who hasn't picked yet. Grey is closed. " +
                 "I never book past what's free, or inside your notice period.";
        } },
      { sel: '[data-tour="section-settings"]', title: "Diary settings",
        text: "Spaces, working days, notice, blackouts and your services all live behind " +
              "this cog. Change one and I book by the new rule from the next email on." },
    ],
    money: [
      // Kris 2026-09-13: "Done, now the Money page?" — the fourth door. Owed
      // on its own the first time Money opens; the Diary's Done stays Done
      // ("still allow the tour to be done at the Diary point").
      { sel: '[data-tour="sheet"]', title: "Your money, as a funnel",
        text: "Quoted, accepted, owed, overdue, collected — each row is the stage before the " +
              "next, so the sheet reads as the cash journey. Every row is a door: tap it for " +
              "the quotes or invoices behind the number." },
      { sel: '[data-tour="chase"]', title: "Late invoices chase themselves",
        text: "At 7, 14 and 30 days late I draft a reminder, firmer each time, and stand down " +
              "the moment the customer writes back. Each one waits in your Inbox for your yes. " +
              "Chase now jumps the queue." },
      { sel: '[data-tour="accountant"]', title: "Hand to your accountant",
        text: "Month by month, the source documents for whoever keeps the books — one tap to " +
              "download in the format they use. Nothing to type up at year end." },
      { sel: '[data-tour="section-settings"]', title: "Money settings",
        text: "Your terms, numbering, payment details and the reminder schedule live behind " +
              "this cog." },
    ],
  };
  // Where each page's last button leads — the next page of the walk. The
  // button wears the label when there is one (Kris 2026-09-13: "instead of
  // clicking Done, we click Diary").
  var HANDOFF = { today: { sel: '[data-tour="inbox"]', href: "/app/conversations" },
                  inbox: { sel: '[data-tour="day-door"]', href: "/app/diary", label: "Diary →" } };
  var stops = (STOPS[page] || []).filter(function (s) { return document.querySelector(s.sel); });
  if (!stops.length) return;

  var idx = -1, target = null, bubble = null, done = false;

  // Where the walk is, across a reload (Kris 2026-09-13: a swipe on the swipe
  // stop reloads the page — the walk must come back where it was, not from
  // the top). Cleared when the walk ends.
  var POS_KEY = "blam8_tour_pos:" + page;
  function savePos(i) { try { sessionStorage.setItem(POS_KEY, String(i)); } catch (e) {} }
  function loadPos() { try { return parseInt(sessionStorage.getItem(POS_KEY) || "0", 10) || 0; } catch (e) { return 0; } }
  function stamp() {
    if (done) return Promise.resolve();
    done = true;
    try { sessionStorage.removeItem(POS_KEY); } catch (e) {}
    try {
      return fetch("/app/tour/done", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/x-www-form-urlencoded", "X-CSRF-Token": csrf },
        body: "page=" + encodeURIComponent(page) + "&csrf_token=" + encodeURIComponent(csrf),
      }).catch(function () {});
    } catch (e) { return Promise.resolve(); }
  }
  // The swipe demo (Kris 2026-09-13): the top row slides right, revealing the
  // Ignore bin under it, and settles back — twice — exactly as a finger would
  // move it (the same translateX the swipe handler writes), then rests.
  var demoTimer = null;
  function demoSwipe(wrap) {
    var row = wrap.querySelector(".convo-row"), under = wrap.querySelector(".swipe-under");
    var underR = wrap.querySelector(".swipe-under-right");
    if (!row) return;
    var reduced = false;
    try { reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (e) {}
    if (reduced) return;
    var dist = Math.min(160, Math.max(90, row.offsetWidth * 0.3)) * 0.85;   // short of the arming threshold
    row.style.transition = "transform .45s cubic-bezier(.2,.8,.2,1)";
    // Right, back, right, back — then left once (the contact card), back.
    var out = function () { row.style.transform = "translateX(" + dist + "px)"; if (under) under.style.opacity = "1"; };
    var outLeft = function () { row.style.transform = "translateX(-" + dist + "px)"; if (underR) underR.style.opacity = "1"; };
    var back = function () { row.style.transform = ""; if (under) under.style.opacity = ""; if (underR) underR.style.opacity = ""; };
    var steps = [out, back, out, back, outLeft, back];
    var n = 0;
    (function tick() {
      if (n >= 6) { demoTimer = null; row.style.transition = ""; return; }
      steps[n](); n++;
      demoTimer = setTimeout(tick, n % 2 ? 900 : 650);
    })();
  }
  function endDemo() {
    if (demoTimer) { clearTimeout(demoTimer); demoTimer = null; }
    if (target) {
      var row = target.querySelector(".convo-row"), under = target.querySelector(".swipe-under");
      var underR = target.querySelector(".swipe-under-right");
      if (row) { row.style.transform = ""; row.style.transition = ""; }
      if (under) under.style.opacity = "";
      if (underR) underR.style.opacity = "";
    }
  }
  function clear() {
    endDemo();
    if (stops[idx] && stops[idx].open) { try { if (window.blam8DeckOrderOpen) window.blam8DeckOrderOpen(false); } catch (e) {} }
    if (target) { target.classList.remove("tour-glow"); target.removeEventListener("click", onTargetClick, true); }
    target = null;
    if (bubble) { bubble.remove(); bubble = null; }
  }
  function finish(handoff) {
    clear();
    var stamped = stamp();
    window.removeEventListener("resize", place);
    window.removeEventListener("scroll", place, true);
    document.removeEventListener("keydown", onKey);
    // Done (not Skip, not Escape) hands over to the next page of the walk
    // (Kris 2026-09-13: "close the dialog, make Inbox → pulse, then load
    // into the inbox"): the door breathes for a moment, then opens.
    var h = handoff ? HANDOFF[page] : null;
    var door = h ? document.querySelector(h.sel) : null;
    if (door && door.getClientRects().length) {      // on screen (the mobile strip's door is display:none on a desktop)
      door.classList.add("tour-glow");
      // 700 ms, not 1400 (Kris 2026-09-25: "Today to Inbox during the tour is slow/delayed").
      setTimeout(function () { window.location.href = door.getAttribute("href") || h.href; }, 700);
    } else if (h) {
      setTimeout(function () { window.location.href = h.href; }, 250);
    }
    return stamped;
  }
  function onTargetClick(e) {
    // A tap on the glowing box is "next" — except on the last stop, where the
    // box is the door it describes: the tour ends and the tap goes through —
    // and on a stop whose box is a control (the cog), which just works.
    if (stops[idx] && stops[idx].passthrough) return;
    if (idx < stops.length - 1) { e.preventDefault(); e.stopPropagation(); show(idx + 1); }
    else finish();
  }
  function onKey(e) { if (e.key === "Escape") finish(); }
  // A control's own panel (the cog's list) is the thing to look at while it
  // is open: the bubble steps aside and comes back when it closes (Kris,
  // 2026-09-13: "auto opens the cog with the speech bubble on top — a pain,
  // and unstoppable for mobile users").
  document.addEventListener("blam8:deck-order", function (e) {
    if (!bubble || !stops[idx] || !stops[idx].passthrough) return;
    bubble.hidden = !!(e.detail && e.detail.open);
    if (!bubble.hidden) place();
  });

  function build(stop, n, total) {
    var b = document.createElement("div");
    b.className = "tour-bubble bg-white border border-slate-200 rounded-xl shadow-xl";
    b.setAttribute("role", "dialog");
    b.setAttribute("aria-label", stop.title);
    var head = document.createElement("div");
    head.className = "flex items-baseline justify-between gap-3";
    var t = document.createElement("p");
    t.className = "text-base font-semibold text-navy";
    t.textContent = stop.title;
    // "2 of 4" with a back arrow on its left from the second stop on (Kris
    // 2026-09-13: "so users can go back a step").
    var pos = document.createElement("span");
    pos.className = "flex items-center gap-1.5 text-xs text-slate-400";
    if (n > 1) {
      var back = document.createElement("button");
      back.type = "button";
      back.className = "inline-flex h-5 w-5 items-center justify-center rounded-full text-slate-400 hover:text-brand hover:bg-slate-100 transition";
      back.setAttribute("aria-label", "Back a step");
      back.setAttribute("title", "Back");
      back.setAttribute("data-tour-back", "1");
      back.innerHTML = '<svg class="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke-width="2.2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M15.75 19.5 8.25 12l7.5-7.5"/></svg>';
      back.addEventListener("click", function () { show(idx - 1); });
      pos.appendChild(back);
    }
    var posText = document.createElement("span");
    posText.textContent = n + " of " + total;
    pos.appendChild(posText);
    head.appendChild(t); head.appendChild(pos);
    var p = document.createElement("p");
    p.className = "mt-1.5 text-sm leading-relaxed text-slate-600";
    p.textContent = typeof stop.text === "function" ? stop.text() : stop.text;
    var row = document.createElement("div");
    row.className = "mt-3 flex items-center justify-between gap-3";
    var skip = document.createElement("button");
    skip.type = "button";
    skip.className = "text-xs text-slate-400 hover:text-slate-600 hover:underline";
    skip.textContent = n < total ? "Skip the tour" : "";
    skip.addEventListener("click", function () { finish(false); });   // Skip never hands over (as a bare listener it read the click event as "handoff")
    var next = document.createElement("button");
    next.type = "button";
    next.className = "rounded-lg bg-brand px-3.5 py-2 text-sm font-semibold text-white hover:bg-blue-700 transition";
    next.textContent = n < total ? "Next →" : ((HANDOFF[page] || {}).label || "Done");
    next.setAttribute("data-tour-next", "1");
    next.addEventListener("click", function () { if (n < total) show(idx + 1); else finish(true); });
    if (n === total && stop.choice) {
      // A yes/no in place of Done: yes posts the setting (the tour root's CSRF
      // token); either answer then walks on to the next page.
      skip.textContent = stop.choice.no;
      skip.className = "rounded-lg border border-slate-300 px-3.5 py-2 text-sm font-medium text-slate-600 hover:border-slate-400 transition";
      skip.replaceWith(skip = skip.cloneNode(true));
      skip.addEventListener("click", function () { finish(true); });
      next.textContent = stop.choice.yes;
      next.setAttribute("data-tour-choice", "yes");
      next.replaceWith(next = next.cloneNode(true));
      next.addEventListener("click", function () {
        next.disabled = true;
        fetch(stop.choice.post, {
          method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/x-www-form-urlencoded", "X-CSRF-Token": csrf },
          body: stop.choice.body + "&csrf_token=" + encodeURIComponent(csrf),
        }).catch(function () {})
          .then(function () { return finish(true); });
      });
    }
    row.appendChild(skip); row.appendChild(next);
    b.appendChild(head); b.appendChild(p); b.appendChild(row);
    return b;
  }

  function place() {
    if (!target || !bubble) return;
    var r = target.getBoundingClientRect();
    var bw = bubble.offsetWidth, bh = bubble.offsetHeight;
    var vw = window.innerWidth, vh = window.innerHeight;
    var left = Math.min(Math.max(12, r.left), vw - bw - 12);
    var below = r.bottom + 14;
    var above = r.top - bh - 14;
    var useBelow = below + bh <= vh - 8 || above < 8;
    var top = useBelow ? below : above;
    bubble.style.left = left + "px";
    bubble.style.top = top + "px";
    bubble.classList.toggle("tour-bubble-above", !useBelow);
    // The arrow points at the box: a third of the way along, clamped inside.
    var ax = Math.min(Math.max(18, r.left + Math.min(r.width / 3, 120) - left), bw - 30);
    bubble.style.setProperty("--tour-arrow-x", ax + "px");
  }

  function show(i) {
    clear();
    idx = i;
    savePos(i);
    var stop = stops[i];
    target = document.querySelector(stop.sel);
    if (!target) { finish(); return; }
    if (target.closest('.today-drawer') && target.inert) {
      target.dispatchEvent(new Event('today-overview-reveal', {bubbles: true}));
    }
    target.classList.add("tour-glow");
    target.addEventListener("click", onTargetClick, true);
    // A stop about a control shows the control open (the cog's list), so the
    // bubble talks about something they can see.
    if (stop.open) { try { if (window.blam8DeckOrderOpen) window.blam8DeckOrderOpen(true); } catch (e) {} }
    bubble = build(stop, i + 1, stops.length);
    document.body.appendChild(bubble);
    try { target.scrollIntoView({ block: "center", behavior: "smooth" }); } catch (e) {}
    place();
    setTimeout(place, 350);   // after the smooth scroll settles
    if (stop.demo === "swipe") setTimeout(function () { if (target === document.querySelector(stop.sel)) demoSwipe(target); }, 450);
    var nextBtn = bubble.querySelector("[data-tour-next]");
    if (nextBtn) nextBtn.focus({ preventScroll: true });
  }

  function start() {
    // The assistant steps aside first (Kris: "once … the chat has auto-closed").
    try { if (window.blam8ConciergeClose) window.blam8ConciergeClose(); } catch (e) {}
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    document.addEventListener("keydown", onKey);
    setTimeout(function () { show(Math.min(loadPos(), stops.length - 1)); }, 250);
  }
  // A real swipe on the swipe stop submits the ignore form and reloads the
  // page: that stop is done — the walk comes back on the next one.
  document.addEventListener("submit", function (e) {
    var f = e.target;
    if (f && f.id === "ignore-remove-form" && stops[idx] && stops[idx].demo === "swipe") savePos(idx + 1);
  }, true);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", function () { setTimeout(start, 150); });
  else setTimeout(start, 150);
})();
