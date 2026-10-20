/* BLAM8 concierge widget (docs/modules/module-concierge.md).
 *
 * A dockable chat present on every page. Modes:
 *  - Q&A (public default): answers questions about BLAM8; offers "Start my
 *    setup" when the visitor is ready. Hits POST /concierge/ask.
 *  - Onboarding co-pilot (data-onboarding-step set): drives the EXISTING wizard
 *    — fills the visible fields, the user clicks the real Continue button.
 *    Hits POST /concierge/onboard; writes nothing.
 *  - App help (in the app): product help via POST /concierge/help, opened
 *    from the search box's /help. Questions about the business itself are the
 *    Concierge's (static/concierge_panel.js — the masthead's person icon); the
 *    "Your business" tab that used to live here moved there 2026-09-23.
 *  - Fix (transient, blam8ConciergeFix): diagnose one draft via /concierge/fix.
 *
 * Signed-in transcripts use the shared workspace when enabled. Public and
 * rollout-disabled transcripts use sessionStorage across page navigation.
 */
(function () {
  "use strict";

  var root = document.getElementById("blam8-concierge");
  if (!root) return;

  var AUTHED = root.getAttribute("data-authed") === "true";
  var deviceSync = AUTHED && window.Blam8DeviceSync;
  // Browser storage is per-BROWSER, not per-account. Live 2026-09-04: a second
  // tenant signed in on the same machine inherited the first's snooze stamps and
  // "setup done" flag, and the entire tuning walk-through never appeared. Every
  // key the widget writes carries the tenant's scope; signed out (public pages)
  // it is empty and the keys read exactly as they always did.
  var SCOPE = (root.getAttribute("data-scope") || "").trim();
  function k(name) { return SCOPE ? name + ":" + SCOPE : name; }
  var ONB_STEP = (root.getAttribute("data-onboarding-step") || "").trim();
  var ONB_MODE = !!ONB_STEP;
  // Logged in but not in the onboarding wizard → in-app help mode.
  var HELP_MODE = AUTHED && !ONB_MODE;
  // In the app shell (data-in-app="true" = the app base includes us). The
  // guided settings hand-off keys off this. The business thread that used to
  // be a tab here is the Concierge now (Kris 2026-09-23: the masthead's
  // person icon, static/concierge_panel.js) — in the app this widget is App
  // help, opened from the search box's /help.
  var IN_APP = HELP_MODE && root.getAttribute("data-in-app") === "true";

  var KEY_LOG = k("blam8_concierge_log");
  var KEY_OPEN = k("blam8_concierge_open");
  var KEY_IND = k("blam8_concierge_industry");
  var KEY_FIX = k("blam8_concierge_fix");      // the draft fix mode is scoped to (survives page loads)
  var KEY_TS = k("blam8_concierge_ts");        // last-activity stamp, for the idle timeout
  var KEY_ONB_STEP0 = k("blam8_concierge_onb_step0"); // wizard step the thread began on
  var KEY_ONB_LAST = k("blam8_concierge_onb_step");   // wizard step last rendered
  var MAX_TURNS = 16;
  var IDLE_MS = 30 * 60 * 1000;             // auto-clear a thread left idle this long

  // Wizard fields the co-pilot can read/fill, by step (matches the templates 1:1).
  var STEP_FIELDS = {
    interview: ["business_name", "website", "industry", "business_brief", "goals"],
    industry: ["business_name", "industry", "industry_other"],
    voice: ["agent_name", "tone", "sign_off"],
  };

  var panel = document.getElementById("bc-panel");
  var bubble = document.getElementById("bc-bubble");
  var closeBtn = document.getElementById("bc-close");
  var msgs = document.getElementById("bc-messages");
  var cta = document.getElementById("bc-cta");
  var form = document.getElementById("bc-form");
  var input = document.getElementById("bc-input");
  var sendBtn = document.getElementById("bc-send");
  var titleEl = document.getElementById("bc-title");
  var usageEl = document.getElementById("bc-usage");
  var setupCards = null;
  var questionLimit = 20;
  var questionsRemaining = null;
  var allowanceUnavailable = false;

  function paintQuestionAllowance() {
    if (!usageEl) return;
    if (questionsRemaining === null) {
      usageEl.textContent = allowanceUnavailable
        ? "Your remaining session questions could not be checked. The assistant checks before answering. Fair-use limits: up to 12 per minute and 150 per day per IP address."
        : "20 questions per browser session. Fair use: up to 12 per minute and 150 per day per IP address.";
      return;
    }
    var left = Math.max(0, Number(questionsRemaining) || 0);
    if (left === 0) {
      usageEl.textContent = "You’ve used all " + questionLimit + " questions in this browser session. Start a new browser session to continue. Fair-use limits also apply: up to 12 per minute and 150 per day per IP address.";
    } else if (left <= 2) {
      usageEl.textContent = left + (left === 1 ? " question left" : " questions left") + " in this browser session. Ask your most important questions now. Fair-use limits also apply: up to 12 per minute and 150 per day per IP address.";
    } else if (left <= 5) {
      usageEl.textContent = left + " questions left in this browser session. Fair-use limits also apply: up to 12 per minute and 150 per day per IP address.";
    } else {
      usageEl.textContent = left + " of " + questionLimit + " questions left in this browser session. Fair use: up to 12 per minute and 150 per day per IP address.";
    }
  }
  function applyQuestionAllowance() {
    if (!usageEl) return;
    var exhausted = questionsRemaining !== null && Number(questionsRemaining) <= 0 && !SCENARIO;
    if (sendBtn) sendBtn.disabled = exhausted || sending;
    if (input) input.setAttribute("aria-describedby", "bc-usage");
    paintQuestionAllowance();
  }
  async function loadQuestionAllowance() {
    if (!usageEl) return;
    try {
      var res = await fetch("/concierge/ask-limit", { credentials: "same-origin" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      var data = await res.json();
      questionLimit = Number(data.limit) || 20;
      questionsRemaining = Number(data.remaining);
      allowanceUnavailable = false;
    } catch (e) {
      // Keep the visible allowance clear even if the status read is unavailable;
      // /ask still enforces it and returns the authoritative count.
      questionsRemaining = null;
      allowanceUnavailable = true;
    }
    applyQuestionAllowance();
  }
  loadQuestionAllowance();

  // Which route this page runs (set by page context, not per message). Shown in
  // the title once the conversation starts so it's always clear what the
  // assistant is set up to do. FIX is transient: entered from a draft's
  // thumbs-down (blam8ConciergeFix), cleared with the thread.
  // Round three, 1 Oct 2026 (F25): fix mode PERSISTS for the draft — it was a
  // bare in-memory variable, so any page load dropped the owner's next correction
  // into the help assistant (which cannot redraft). It is now kept in
  // sessionStorage beside the thread and ends only when the thread is cleared
  // (the header clear button, the idle timeout, another thumbs-down) or the
  // owner leaves the draft's page.
  // It belongs to the PAGE the draft is on: closing the panel to read the
  // redrafted reply and opening it again stays in fix mode (on a phone the panel
  // covers the draft, so that is exactly what an owner does), while opening the
  // assistant on any other page is the ordinary assistant again.
  var FIX_PAGE = location.pathname + location.search;
  var FIX_LOG_ID = (function () {
    try {
      var saved = JSON.parse(sessionStorage.getItem(KEY_FIX) || "null");
      if (saved && saved.id && saved.page === FIX_PAGE) return saved.id;
      if (saved) sessionStorage.removeItem(KEY_FIX);
    } catch (e) {}
    return null;
  })();
  function setFixLogId(id) {
    FIX_LOG_ID = id || null;
    try {
      if (FIX_LOG_ID) sessionStorage.setItem(KEY_FIX, JSON.stringify({ id: FIX_LOG_ID, page: FIX_PAGE }));
      else sessionStorage.removeItem(KEY_FIX);
    } catch (e) {}
  }
  function modeLabel() {
    if (ONB_MODE) return "Onboard";
    return HELP_MODE ? "Help" : "Ask";
  }
  function refreshTitle() {
    if (titleEl) titleEl.textContent = FIX_LOG_ID
      ? "BLAM8 assistant — Fix a draft"
      : (log.length ? "BLAM8 assistant — " + modeLabel() : "BLAM8 assistant");
  }

  // EU AI Act Art 50(1): the FIRST message in every mode says "AI assistant"
  // explicitly (the panel footer repeats it permanently) — a visitor always
  // knows they're talking to AI (docs/security/eu-ai-act-compliance.md §3, action 2).
  // The knowledge page says which website it is reading on arrival (the one
  // given at step 1) — the step's greeting then says so instead of asking.
  var ONB_SITE = (function () {
    var el = document.querySelector("[data-onb-site]");
    return el ? (el.getAttribute("data-onb-site") || "").trim() : "";
  })();
  // The FIRST message says what is about to happen — the four steps, that the
  // inbox connected at step 2 is a separate choice from the login, and what to
  // tell the assistant now (Kris 2026-09-13: a greeting that only asked a
  // question left people unsure what the assistant was for).
  // Honest count (Kris 2026-09-13): the wizard is four steps, but five more
  // follow inside the app (src/app/setup_handoff.py: diary, booking requirements,
  // Dependants, CRM, invoicing — every sign-up has scheduling + CRM on, so
  // the count is the same for every business; the TAILORING is inside each
  // step). A "four steps" that turned into nine reads as a bait.
  var ONB_INTRO = "Welcome to BLAM8! I'm the AI assistant — you talk, I fill the forms in, you check them. Setup is in two parts.\n"
    + "Here, four short steps:\n"
    + "1. About you — this step\n"
    + "2. Your customer inbox — connecting the mailbox customers email (separate from the account you just signed in with)\n"
    + "3. Voice — how your replies should sound\n"
    + "4. Knowledge — the facts it can quote: prices, hours, policies (your website can fill most of this)\n"
    + "Then inside the app, five more with me, tailored to your business: your diary, booking requirements, what your bookings are about (dogs, cars, children…), contacts, and getting paid. Nine in all, none hidden.\n"
    + "To start: what does your business do, and who are your customers?";
  // The mailbox step's Gmail gate is a page state (invite box present or
  // not) — the message mentions a code only when the box is really there
  // (kennels run #10: the text told an owner to enter a code into a box the
  // page didn't have).
  var GATE_BOX = !!document.querySelector('form[action="/oauth/google-invite"]');
  var MAILBOX_INVITE = GATE_BOX
    ? " Gmail is invite-only while Google verifies our app — enter your invite code on this page (Outlook needs no invite)."
    : "";
  var ONB_GREETINGS = {
    interview: ONB_INTRO,
    industry: "Let's get you set up — I'm the BLAM8 AI assistant. Tell me what your business does and I'll pick your sector and fill it in for you.",
    voice: "Now let's give your agent a voice — I'm the BLAM8 AI assistant. Describe your business and how you'd like it to sound, and I'll fill these in for you to review.",
    mailbox: "Step 2 of 4 here: connect the inbox your customers email — I'm the BLAM8 AI assistant. The page names the account you signed in with; that's your login. This step gives your agent permission to work the mailbox customers actually write to — the same account if that's where they write, or a different one (like info@). Press Connect with Google or Connect with Microsoft: the provider's own screen lists what you're approving (reading new mail and sending replies, never your password) and brings you back here. Google shows an “unverified app” notice while it verifies us — continue via Advanced; that's expected." + MAILBOX_INVITE + " I can't press it for you, but ask me anything.",
    knowledge: ONB_SITE
      ? "I'm the BLAM8 AI assistant. Once " + ONB_SITE + " has been read, we'll go through your actual notes together. Tell me what to keep or correct; I'll ask about anything that needs your decision. Nothing from the drafts is available to the agent until you save the review."
      : "I'm the BLAM8 AI assistant. We'll go through your notes together. Tell me what to keep or correct, and I'll ask about anything needing your decision. Pending drafts are separate from saved Knowledge. Unknown details can stay unknown. Nothing from the drafts is available to the agent until you save the review.",
    data: "Last step — which systems do you run on? I'm the BLAM8 AI assistant: tell me and I'll point you to the right boxes to tick.",
  };
  // Mid-thread step hand-offs: same job as the greetings but WITHOUT the
  // self-introduction — the thread's first message already disclosed the AI
  // (Art 50 is satisfied; the footer repeats it permanently), and
  // re-introducing mid-conversation reads wrong (Julius). Falls back to
  // ONB_GREETINGS for any step without an entry.
  var ONB_HANDOFFS = {
    interview: "Back on the about-you step — tell me what's changed and I'll update the form for you.",
    industry: "Picking your sector from the list — tell me what your business does and I'll pick the closest match for you.",
    voice: "Next: your agent's voice — how replies should sound and sign off. With your mailbox connected you can have it learned from your sent emails in one click (it only ever runs on your say-so), or just describe it and I'll fill the form for you to review.",
    mailbox: "Step 2 of 4 here: connect the inbox your customers email. The page names the account you signed in with; that's your login. This step gives your agent permission to work the mailbox customers actually write to — the same account if that's where they write, or a different one (like info@). Press Connect with Google or Connect with Microsoft: the provider's own screen lists what you're approving (reading new mail and sending replies, never your password) and brings you back here. Google shows an “unverified app” notice while it verifies us — continue via Advanced; that's expected." + MAILBOX_INVITE + " I can't press it for you, but ask me anything.",
    knowledge: ONB_SITE
      ? "I'll help you review the notes prepared from " + ONB_SITE + ". We'll check each note and any decisions it needs together. Drafts and corrections stay pending until you save your reviewed decisions."
      : "I'll help you review each note and any decisions it needs. Unknown details can stay unknown. The drafts stay pending until you save your reviewed decisions, or you can choose to leave them for later.",
    data: "Where your live data lives: calendar, job software, spreadsheets. Tick what you use on the page; tell me what you run on and I'll point you to the right boxes.",
  };
  var GREETING = ONB_MODE
    ? (ONB_GREETINGS[ONB_STEP] || "I'm the BLAM8 AI assistant — here to help you finish setting up, just ask.")
    : (AUTHED
        ? "Hi! I'm the BLAM8 AI assistant. Ask me about any module, your settings, or how something works — or tell me about a problem or an idea and I'll draft it for the team."
        : "Hi! I'm the BLAM8 AI assistant. Ask me anything — what it does, pricing, security, or whether it fits your business.");
  // Fix mode opens by ASKING (Kris 2026-09-21: "the agent should just ask, rather
  // than presume"). The thumbs-down used to post a canned "something's wrong" in
  // the operator's name, which told the model nothing and licensed it to guess —
  // a diagnosis nobody asked for, at their tokens. The operator knows what's
  // wrong; the first server turn carries their own words.
  var FIX_GREETING = "What's wrong with it? Tell me in a line — a wrong fact, something it shouldn't have said, a detail it should have asked for — and I'll fix the draft, and remember a rule if it needs one.";
  // Scenario mode (public pages only): opened from the landing FAQ's "Run it past
  // BLAM8" link. One situation in, one grounded answer out — POST /concierge/scenario,
  // no history, a couple a day. Ends when the panel is closed.
  var SCENARIO = false;
  var SCENARIO_GREETING = "Describe one real situation from your business, in a sentence or two, and I'll tell you how BLAM8 would deal with it. You can run a couple a day, so make it a good one.";
  var SCENARIO_MAX = 400;
  var ASK_PLACEHOLDER = null;
  function greeting() { return FIX_LOG_ID ? FIX_GREETING : (SCENARIO ? SCENARIO_GREETING : GREETING); }
  function setScenario(on) {
    SCENARIO = on;
    if (!input) return;
    if (ASK_PLACEHOLDER === null) ASK_PLACEHOLDER = input.getAttribute("placeholder") || "";
    input.setAttribute("placeholder", on ? "Describe a situation from your business…" : ASK_PLACEHOLDER);
    input.maxLength = on ? SCENARIO_MAX : 4000;
    applyQuestionAllowance();
  }

  // ── transcript persistence ────────────────────────────────────────────────
  function loadLog() {
    if (deviceSync) return [];
    try { return JSON.parse(sessionStorage.getItem(KEY_LOG)) || []; }
    catch (e) { return []; }
  }
  function saveLog(l) {
    if (deviceSync && deviceSync.isReady() && !FIX_LOG_ID && !SCENARIO) deviceSync.transcript(l).catch(function () {});
    try { sessionStorage.setItem(KEY_LOG, JSON.stringify(l.slice(-MAX_TURNS * 2))); }
    catch (e) { /* private mode / quota — in-memory only */ }
  }
  var log = loadLog();

  // ── idle timeout + manual clear ───────────────────────────────────────────
  function lastActivity() { return parseInt(sessionStorage.getItem(KEY_TS) || "0", 10) || 0; }
  function touch() { try { sessionStorage.setItem(KEY_TS, String(Date.now())); } catch (e) {} }
  function isStale() {
    if (deviceSync) return false;
    return log.length > 0 && lastActivity() > 0 && (Date.now() - lastActivity()) > IDLE_MS;
  }
  function wipeStored() {
    try {
      sessionStorage.removeItem(KEY_LOG);
      sessionStorage.removeItem(KEY_FIX);
      sessionStorage.removeItem(KEY_IND);
      sessionStorage.removeItem(KEY_TS);
      sessionStorage.removeItem(KEY_ONB_STEP0);
      sessionStorage.removeItem(KEY_ONB_LAST);
    } catch (e) {}
  }
  // Reset the thread to a fresh greeting — used by the header "clear" button and the
  // idle-timeout. With device sync enabled, clear the shared transcript too.
  function clearThread() {
    if (deviceSync && !FIX_LOG_ID && !SCENARIO) deviceSync.clear().catch(function () {});
    log.length = 0;
    setFixLogId(null);
    wipeStored();
    // A cleared thread restarts on the CURRENT step — it owns the top greeting.
    if (ONB_MODE) {
      GREETING = ONB_GREETINGS[ONB_STEP] || GREETING;
      try {
        sessionStorage.setItem(KEY_ONB_STEP0, ONB_STEP);
        sessionStorage.setItem(KEY_ONB_LAST, ONB_STEP);
      } catch (e) {}
    }
    hideTyping();
    if (cta) { cta.classList.add("hidden"); cta.innerHTML = ""; }
    paintSetupCta();   // the guided strip survives a thread clear
    renderAll();
    if (input) { input.value = ""; autosize(); }
  }
  // A thread left idle past the timeout is dropped on the next load (and again on a
  // timer + on open, below).
  if (isStale()) { log.length = 0; wipeStored(); FIX_LOG_ID = null; }

  // ── onboarding step hand-off ──────────────────────────────────────────────
  // The thread survives navigation, so a NEW wizard step must announce itself
  // as the newest message at the BOTTOM of the chat — renderAll()'s top
  // greeting scrolls away with history, and swapping it there leaves the user
  // staring at the previous step's closing message. The step the thread
  // started on keeps the top greeting; same-step reloads append nothing.
  var HANDOFF = false;   // a step greeting was appended on THIS load
  if (ONB_MODE) {
    try {
      var lastStep = sessionStorage.getItem(KEY_ONB_LAST);
      if (log.length && lastStep && lastStep !== ONB_STEP) {
        log.push({ role: "assistant",
                   content: ONB_HANDOFFS[ONB_STEP] || ONB_GREETINGS[ONB_STEP] || GREETING });
        saveLog(log);
        HANDOFF = true;
        var step0 = sessionStorage.getItem(KEY_ONB_STEP0);
        if (step0 && ONB_GREETINGS[step0]) GREETING = ONB_GREETINGS[step0];
      } else if (!log.length || !sessionStorage.getItem(KEY_ONB_STEP0)) {
        sessionStorage.setItem(KEY_ONB_STEP0, ONB_STEP);
      }
      sessionStorage.setItem(KEY_ONB_LAST, ONB_STEP);
    } catch (e) {}
  }

  // ── rendering ─────────────────────────────────────────────────────────────
  function bubbleEl(role, text) {
    var wrap = document.createElement("div");
    wrap.dataset.bcRole = role;
    wrap.className = "flex " + (role === "user" ? "justify-end" : "justify-start");
    var b = document.createElement("div");
    b.className = role === "user"
      ? "max-w-[85%] rounded-2xl rounded-br-sm bg-brand px-3 py-2 text-white whitespace-pre-wrap break-words"
      : "max-w-[85%] rounded-2xl rounded-bl-sm bg-white border border-slate-200 px-3 py-2 text-slate-700 whitespace-pre-wrap break-words";
    if (role === "user") { b.textContent = text; } else { setRich(b, text); }
    wrap.appendChild(b);
    return wrap;
  }
  function setRich(el, text) {
    // Minimal markdown: **bold** and *italic* — DOM nodes (textContent), never
    // innerHTML, so nothing in the text can inject markup. (Italics were raw
    // asterisks on every live reply until 2026-09-06.)
    el.textContent = "";
    String(text || "").split("**").forEach(function (p, i) {
      if (!p) return;
      var host = el;
      if (i % 2 === 1) { host = document.createElement("strong"); el.appendChild(host); }
      p.split(/\*(?=\S)([^*]+?)(?<=\S)\*/).forEach(function (q, j) {
        if (!q) return;
        if (j % 2 === 1) { var em = document.createElement("em"); em.textContent = q; host.appendChild(em); }
        else host.appendChild(document.createTextNode(q));
      });
    });
  }
  function scrollDown() { msgs.scrollTop = msgs.scrollHeight; }
  // Incoming-message pinning (Julius): every assistant reply lands with its
  // TOP at the top of the viewport — read downward, scroll up for history.
  // A temporary bottom spacer lets a SHORT tail rest at the top (without it
  // the scroll clamps and the message sits at the bottom, so a long reply
  // must be read bottom-up). The spacer clears when the conversation moves
  // on (send / re-render). `el` plus everything after it (nav links, cards)
  // counts as the pinned tail.
  function clearPin() { msgs.style.paddingBottom = ""; }
  function pinToTop(el) {
    if (!el) return;
    var tail = 0, n = el;
    while (n) { tail += n.offsetHeight + 10; n = n.nextElementSibling; }
    var pad = msgs.clientHeight - tail - 4;
    msgs.style.paddingBottom = pad > 0 ? pad + "px" : "";
    // Scroll to the message's own top edge: measuring back from the bottom was
    // off by anything drawn after it (the pinned caveat line under the greeting).
    var top = el.getBoundingClientRect().top - msgs.getBoundingClientRect().top + msgs.scrollTop;
    msgs.scrollTop = Math.max(0, top - 8);
  }
  function pinLastToTop() { pinToTop(msgs.lastElementChild); }

  // ── ghost trail (docs/modules/module-concierge-transparency.md §A) ────────────────
  // Every lookup a streaming answer runs becomes a pill that STAYS, stacked
  // above the answer bubble — the live pill pulses with the in-flight hint,
  // then settles to its past-tense label. Tapping a pill reveals what it
  // actually looked for (the server's sanitised detail — never raw tool input).
  function trailStrip() {
    var wrap = document.createElement("div");
    wrap.className = "flex justify-start";
    var col = document.createElement("div");
    col.className = "max-w-[85%] space-y-1";
    var row = document.createElement("div");
    row.className = "flex flex-wrap items-center gap-1";
    var detail = document.createElement("p");
    detail.className = "hidden text-[11px] text-slate-400 italic px-1 break-words";
    col.appendChild(row);
    col.appendChild(detail);
    wrap.appendChild(col);
    return { wrap: wrap, row: row, detail: detail };
  }
  function trailPill(strip, item, active) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "inline-flex items-center rounded-full border border-indigo-100 bg-indigo-50 px-2 py-0.5 text-[10px] font-medium text-indigo-600 transition" +
                  (active ? " animate-pulse" : "") +
                  (item.detail ? " hover:bg-indigo-100" : " cursor-default");
    b.textContent = active ? "🔍 " + (item.hint || item.label || "Searching…")
                           : (item.label || "Searched");
    if (item.detail) {
      b.addEventListener("click", function () {
        var open = !strip.detail.classList.contains("hidden") && strip.detail._for === b;
        strip.detail._for = b;
        strip.detail.textContent = "Looked for: “" + item.detail + "”";
        strip.detail.classList.toggle("hidden", open);
      });
    }
    strip.row.appendChild(b);
    return b;
  }
  function settleTrailPill(pill, item) {
    if (!pill) return;
    pill.classList.remove("animate-pulse");
    pill.textContent = (item && item.label) || "Searched";
  }
  // Settled strip straight from a persisted trail (history replay).
  function appendTrailStrip(items) {
    if (!items || !items.length) return;
    var strip = trailStrip();
    items.forEach(function (it) { trailPill(strip, it || {}, false); });
    msgs.appendChild(strip.wrap);
  }

  // The honesty line (moved out of the composer, Kris 2026-08-31): while the
  // chat is empty it sits at the FOOT of the sheet; once anything is said it
  // rides at the very top of the transcript and scrolls away with it.
  var CAVEAT_TEXT = ONB_STEP
    ? "AI assistant · may be imperfect · it infers your business type, so double-check what it fills in"
    : "AI assistant · may be imperfect · nothing sends without your approval";
  function caveatEl(pinned) {
    var el = document.createElement("p");
    el.id = "bc-caveat";
    el.className = "px-1 pb-0.5 text-[10px] text-slate-400 text-center flex-none"
      + (pinned ? " mt-auto order-last" : "");
    el.textContent = CAVEAT_TEXT;
    return el;
  }
  function unpinCaveat() {
    var el = document.getElementById("bc-caveat");
    if (el) el.classList.remove("mt-auto", "order-last");
  }

  function renderAll() {
    clearPin();
    msgs.innerHTML = "";
    msgs.appendChild(caveatEl(log.length === 0));
    var lastAssistant = bubbleEl("assistant", greeting());
    msgs.appendChild(lastAssistant);
    log.forEach(function (m) {
      if (m.role === "assistant") appendTrailStrip(m.trail);
      var el = bubbleEl(m.role, m.content);
      msgs.appendChild(el);
      if (m.role === "assistant") lastAssistant = el;
      if (m.role === "assistant" && m.knowledge_card) {
        var restoredCard = knowledgeCardNode(m.knowledge_card, !m.knowledge_card.selected && knowledgeCardCurrent(m.knowledge_card));
        if (restoredCard) msgs.appendChild(restoredCard);
      }
      if (m.role === "assistant" && m.links) {
        m.links.forEach(function (l) {
          // Same rule as the live reply sites: no chip to the current page.
          if (l && l.href && l.label && l.href.charAt(0) === "/"
              && l.href !== window.location.pathname) appendNavLink(l);
        });
      }
    });
    refreshTitle();
    // Opening the panel shows the latest assistant message from its first line,
    // like a live reply (Kris 30 Sep: the long welcome opened at its bottom).
    if (!log.length || log[log.length - 1].role === "assistant") pinToTop(lastAssistant);
    else scrollDown();
  }

  var typingEl = null;
  function showTyping() {
    if (setupCards) setupCards.setWaiting(true);
    typingEl = document.createElement("div");
    typingEl.className = "flex justify-start";
    typingEl.innerHTML = '<div class="bc-typing rounded-2xl rounded-bl-sm bg-white border border-slate-200 px-3 py-2 text-slate-400"><span class="bc-dot"></span><span class="bc-dot"></span><span class="bc-dot"></span></div>';
    msgs.appendChild(typingEl);
    scrollDown();
  }
  function hideTyping() { if (typingEl) { typingEl.remove(); typingEl = null; } if (setupCards) setupCards.setWaiting(false); }

  // ── Q&A conversion CTA (public pages only) ─────────────────────────────────
  // Rendered inline in the message flow, right under the reply that offered it —
  // not pinned to the panel footer, so it stays with its message as the chat
  // moves on. The footer slot (#bc-cta) is only the onboarding Continue-hint's.
  function showCTA() {
    if (AUTHED) return;
    var wrap = document.createElement("div");
    wrap.className = "flex justify-start";
    var a = document.createElement("a");
    a.href = "/login";
    a.className = "inline-block rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-white hover:bg-blue-700 transition";
    a.textContent = "Start my setup";
    wrap.appendChild(a);
    msgs.appendChild(wrap);
  }
  function rememberIndustry(ind) {
    if (ind) { try { sessionStorage.setItem(KEY_IND, ind); } catch (e) {} }
  }

  // ── onboarding co-pilot: read + fill the visible wizard ────────────────────
  function fieldEls(name) { return document.querySelectorAll('[name="' + name + '"]'); }

  function readCurrent() {
    var names = STEP_FIELDS[ONB_STEP] || [];
    var cur = {};
    names.forEach(function (name) {
      var els = fieldEls(name);
      if (!els.length) return;
      if (els[0].type === "radio") {
        els.forEach(function (r) { if (r.checked) cur[name] = r.value; });
      } else if (els[0].value) {
        cur[name] = els[0].value;
      }
    });
    return cur;
  }

  function flash(el) {
    if (!el) return;
    try {
      el.style.transition = "box-shadow .15s ease";
      el.style.boxShadow = "0 0 0 3px rgba(37,99,235,.35)";
      setTimeout(function () { el.style.boxShadow = ""; }, 900);
    } catch (e) {}
  }

  function applyFill(fill) {
    // Voice step: the Adjust form starts hidden behind the learned-profile
    // review. A fill landing in hidden fields must REVEAL the form first —
    // otherwise the review card (old values) stays on screen, "have a look"
    // points at nothing, and the visible "Looks good" button would apply the
    // learned profile and silently discard this fill (say-vs-do).
    var adjust = document.getElementById("voice-adjust");
    if (adjust && Object.keys(fill || {}).some(function (n) {
          var el = fill[n] && fieldEls(n)[0];
          return el && el.offsetParent === null;
        })) adjust.click();
    Object.keys(fill || {}).forEach(function (name) {
      var value = fill[name];
      if (!value) return;                 // never blank an existing field
      var els = fieldEls(name);
      if (!els.length) return;
      if (els[0].type === "radio") {
        els.forEach(function (r) {
          if (r.value === value) {
            r.checked = true;
            r.dispatchEvent(new Event("change", { bubbles: true }));
            flash(r.closest("label") || r);
          }
        });
      } else {
        els[0].value = value;
        els[0].dispatchEvent(new Event("input", { bubbles: true }));
        flash(els[0]);
      }
    });
  }

  // The step's own advance button, when it's on screen. Steps mark theirs with
  // data-onb-continue; the voice step has two (review vs adjust) and only one
  // is ever visible, so visibility picks the right one.
  function pageContinueBtn() {
    var btns = document.querySelectorAll("[data-onb-continue]");
    for (var i = 0; i < btns.length; i++) {
      if (btns[i].offsetParent !== null) return btns[i];
    }
    return null;
  }

  // The step is filled — offer Continue HERE. On three of the steps the panel
  // sits over the page's own Continue (2026-09-04 run): telling someone to go
  // and press a button they can't see is not a hand-off, it's a dead end. The
  // panel's button presses the real one, so the form's validation and submit
  // behave exactly as if they'd clicked it themselves.
  // Unsaved knowledge cards in the panel (LittleDogs P1, re-run 2026-09-13:
  // "it shows the Next button before submission, so the info could be left
  // out"). While any is pending, the panel's Next is replaced by a hold line,
  // and the page's own Next saves them first (see the capture listener below).
  var PENDING_KB_CARDS = [];
  var ADVANCE_WANTED = false;
  function showContinueHint() {
    function notice(text, working) {
      var line = document.createElement("p");
      line.className = "bc-notice" + (working ? " bc-busy" : "");
      line.setAttribute("role", "status");
      var words = document.createElement("span");
      words.className = "bc-notice-text" + (working ? " bc-busy-text" : "");
      words.textContent = text;
      line.appendChild(words);
      return line;
    }
    ADVANCE_WANTED = true;
    var btn = pageContinueBtn();
    var state = window.Blam8OnboardingJourney ? window.Blam8OnboardingJourney.read(PENDING_KB_CARDS.length) : {ready:!!btn, action:btn};
    cta.innerHTML = "";
    if (!state.ready) {
      if (state.blocker === "pending_reviews") {
        var later = document.querySelector("[data-knowledge-later]");
        if (later && later.hidden) later.hidden = false; // reveal the existing explicit choice; never select it
      }
      var reason = notice(state.reason || "No Continue action is available on this page.", state.blocker === "busy");
      cta.appendChild(reason);
      cta.classList.remove("hidden");
      return;
    }
    if (PENDING_KB_CARDS.length) {
      var hold = notice(PENDING_KB_CARDS.length === 1
        ? "Save (or clear) the notes card above first — then Next."
        : "Save (or clear) the " + PENDING_KB_CARDS.length + " notes cards above first — then Next.");
      cta.appendChild(hold);
      cta.classList.remove("hidden");
      return;
    }
    if (btn) {
      var go = document.createElement("button");
      go.type = "button";
      go.className = "w-full rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-white hover:bg-blue-700 transition";
      go.textContent = (btn.textContent || "Continue").trim() + " →";
      go.addEventListener("click", function () {
        // Resolve the page's button NOW, not at render time: the knowledge
        // step's notes save refreshes <main> in place, which replaces the
        // Finish button node — a click on the detached original submitted
        // nothing (run #9, 2026-09-05: "Finish setup →" dead after a save).
        var live = pageContinueBtn();
        var currentState = window.Blam8OnboardingJourney ? window.Blam8OnboardingJourney.read(PENDING_KB_CARDS.length) : {ready:!!live};
        if (!live || !currentState.ready || live.disabled) { showContinueHint(); return; }
        go.disabled = true;
        go.className += " opacity-60";
        live.click();
        // If the page is still here in a moment (a validation stop, a form
        // that didn't submit), give the button back rather than leaving it
        // greyed for good.
        setTimeout(function () {
          go.disabled = false;
          go.className = go.className.replace(" opacity-60", "");
        }, 4000);
      });
      cta.appendChild(go);
      var p = notice("Or check the form over first — it's filled in behind me.");
      p.className += " mt-1.5";
      cta.appendChild(p);
    } else {
      var hint = notice("Looks good? Review the form and hit Continue.");
      cta.appendChild(hint);
    }
    cta.classList.remove("hidden");
  }

  // ── open / close ──────────────────────────────────────────────────────────
  // Phones only: the open panel blankets the page, so grow the page by exactly
  // the height it covers — everything can then scroll up past the panel header
  // instead of sitting stuck behind it (Kris 2026-08-13: the Today chips).
  // A spacer element (not container padding) because the two bases scroll
  // differently: app pages scroll the <main> overflow container, public pages
  // scroll the body — the spacer goes wherever the scrolling actually happens.
  var pageSpacer = null;
  function syncPageSpace() {
    var open = !panel.classList.contains("hidden");
    // Side dock (_concierge.html): the app scroller's scrollbar width, so the
    // reserved-column maths reads the scroller's inner width rather than the
    // full viewport. 0 on shells that scroll the body and with overlay
    // scrollbars.
    var sky = document.querySelector("main.app-sky");
    document.documentElement.style.setProperty(
      "--bc-sbw", (sky ? Math.max(0, sky.offsetWidth - sky.clientWidth) : 0) + "px");
    var px = 0;
    // 767px = the sheet breakpoint in _concierge.html (side dock above it).
    if (open && window.matchMedia("(max-width: 767px)").matches) {
      px = Math.max(0, window.innerHeight - panel.getBoundingClientRect().top);
    }
    if (!px) {
      if (pageSpacer) pageSpacer.style.height = "0px";
      return;
    }
    if (!pageSpacer) {
      pageSpacer = document.createElement("div");
      pageSpacer.setAttribute("aria-hidden", "true");
      var main = document.querySelector("main");
      var scroller = (main && /(auto|scroll)/.test(getComputedStyle(main).overflowY)) ? main : document.body;
      scroller.appendChild(pageSpacer);
    }
    pageSpacer.style.height = px + "px";
  }
  window.addEventListener("resize", syncPageSpace);
  // Height transitions and the on-screen keyboard can change the sheet after
  // an input event. Keep the page's scroll clearance tied to its actual size.
  if (window.ResizeObserver) new ResizeObserver(syncPageSpace).observe(panel);
  else panel.addEventListener("transitionend", syncPageSpace);

  // On phones the blue header resizes the sheet; its buttons keep their taps.
  // Keep a proportion per tenant/tab so navigation and rotation retain the choice.
  var resizeHeader = panel.querySelector("[data-bc-resize]");
  var resizeHandle = document.getElementById("bc-resize-handle");
  var KEY_HEIGHT = k("blam8_concierge_height");
  var heightRatio = null;
  try {
    var storedHeight = Number(sessionStorage.getItem(KEY_HEIGHT));
    if (storedHeight > 0 && storedHeight <= 1) heightRatio = storedHeight;
  } catch (e) {}
  var sheetDrag = null;
  function mobileSheetBounds() {
    var viewport = window.visualViewport;
    var height = viewport ? viewport.height : window.innerHeight;
    var bar = document.querySelector("[data-pull-bar], body > header.sticky");
    var top = bar ? Math.max(0, bar.getBoundingClientRect().bottom - (viewport ? viewport.offsetTop : 0)) : 48;
    var max = Math.max(1, Math.floor(height - top - 16));
    return { viewport: height, min: Math.min(240, max), max: max };
  }
  function sizeMobileSheet(height) {
    if (!window.matchMedia("(max-width: 767px)").matches) return;
    var bounds = mobileSheetBounds();
    var next = Math.round(Math.max(bounds.min, Math.min(bounds.max,
      height == null ? bounds.viewport * (heightRatio || 0.6) : height)));
    var viewport = window.visualViewport;
    var bottom = viewport ? Math.max(0, window.innerHeight - viewport.height - viewport.offsetTop) : 0;
    panel.style.setProperty("--bc-mobile-bottom", bottom + "px");
    panel.style.setProperty("--bc-mobile-limit", bounds.max + "px");
    panel.style.setProperty("--bc-mobile-height", next + "px");
    if (resizeHandle) {
      resizeHandle.setAttribute("aria-valuemin", String(bounds.min));
      resizeHandle.setAttribute("aria-valuemax", String(bounds.max));
      resizeHandle.setAttribute("aria-valuenow", String(next));
      resizeHandle.setAttribute("aria-valuetext", next + " pixels tall");
    }
    syncPageSpace();
    return next;
  }
  function rememberSheetHeight(height) {
    heightRatio = height / mobileSheetBounds().viewport;
    try { sessionStorage.setItem(KEY_HEIGHT, String(heightRatio)); } catch (e) {}
  }
  function finishSheetDrag(event) {
    if (!sheetDrag || (event && event.pointerId !== sheetDrag.id)) return;
    var drag = sheetDrag;
    sheetDrag = null;
    panel.classList.remove("bc-resizing");
    if (resizeHeader.hasPointerCapture(drag.id)) resizeHeader.releasePointerCapture(drag.id);
    if (drag.changed) rememberSheetHeight(panel.getBoundingClientRect().height);
  }
  if (resizeHeader && resizeHandle) {
    resizeHeader.addEventListener("pointerdown", function (event) {
      if (!window.matchMedia("(max-width: 767px)").matches || !event.isPrimary || event.button !== 0 ||
          sheetDrag || event.target.closest("button, a, input, textarea, select")) return;
      event.preventDefault();
      sheetDrag = { id: event.pointerId, y: event.clientY, height: panel.getBoundingClientRect().height, changed: false };
      resizeHeader.setPointerCapture(event.pointerId);
      panel.classList.add("bc-resizing");
    });
    resizeHeader.addEventListener("pointermove", function (event) {
      if (!sheetDrag || event.pointerId !== sheetDrag.id) return;
      sizeMobileSheet(sheetDrag.height + sheetDrag.y - event.clientY);
      sheetDrag.changed = true;
    });
    ["pointerup", "pointercancel", "lostpointercapture"].forEach(function (name) {
      resizeHeader.addEventListener(name, finishSheetDrag);
    });
    resizeHandle.addEventListener("keydown", function (event) {
      if (!window.matchMedia("(max-width: 767px)").matches) return;
      var bounds = mobileSheetBounds();
      // Repeated keys build on the requested height, not an animation frame.
      var height = Number(resizeHandle.getAttribute("aria-valuenow")) || panel.getBoundingClientRect().height;
      if (event.key === "ArrowUp") height += 32;
      else if (event.key === "ArrowDown") height -= 32;
      else if (event.key === "Home") height = bounds.min;
      else if (event.key === "End") height = bounds.max;
      else return;
      event.preventDefault();
      rememberSheetHeight(sizeMobileSheet(height));
    });
    function resizeMobileViewport() {
      // Rotation/keyboard changes invalidate a drag's starting coordinates.
      if (sheetDrag) sheetDrag.changed = false;
      finishSheetDrag();
      sizeMobileSheet();
    }
    window.addEventListener("resize", resizeMobileViewport);
    if (window.visualViewport) {
      window.visualViewport.addEventListener("resize", resizeMobileViewport);
      window.visualViewport.addEventListener("scroll", resizeMobileViewport);
    }
    sizeMobileSheet();
  }

  function setOpen(open) {
    // Save the drag's actual height before display:none makes its bounds zero.
    if (!open) finishSheetDrag();
    panel.classList.toggle("hidden", !open);
    panel.classList.toggle("flex", open);
    bubble.classList.toggle("hidden", open);   // panel replaces the bubble while open
    bubble.setAttribute("aria-expanded", open ? "true" : "false");
    if (open && resizeHeader && resizeHandle) sizeMobileSheet();
    if (!open && SCENARIO) { setScenario(false); clearThread(); }   // scenario mode ends when the panel closes
    syncPageSpace();
    try { sessionStorage.setItem(KEY_OPEN, open ? "1" : "0"); } catch (e) {}
    if (open) {
      if (isStale()) clearThread(); else renderAll();
      maybeSetupGreet();
      maybeOnbAutoAsk();
      setTimeout(function () { input.focus(); }, 50);
    }
  }

  // Onboarding's knowledge step interviews the owner itself: the page carries
  // the ask for the kinds still missing ([data-onb-autoask], written server-
  // side from the coverage), and the panel sends it once per session as a
  // silent trigger — no fabricated user bubble, the assistant just asks.
  var KEY_ONB_AUTOASK = k("blam8_onb_autoask");
  function maybeOnbAutoAsk() {
    if (deviceSync && !deviceSync.isReady()) { deviceSync.ready.then(maybeOnbAutoAsk).catch(function(){}); return; }
    if (deviceSync && (deviceSync.state().payload.arrivals || []).indexOf('onboard:'+ONB_STEP)!==-1) return;
    if (!ONB_MODE || ONB_STEP !== "knowledge" || sending) return;
    var el = document.querySelector("[data-onb-autoask]");
    if (!el) return;
    var text = el.getAttribute("data-onb-autoask") || "";
    if (!text) return;
    try { if (sessionStorage.getItem(KEY_ONB_AUTOASK) === ONB_STEP) return; } catch (e) {}
    // The page is reading the website from step 1 (data-onb-autoask-after=
    // "site"): hold the interview until it has finished. Cards landed → say
    // nothing, saving them reloads this page with fresh coverage and the ask
    // fires then; nothing landed → interview now.
    if (el.getAttribute("data-onb-autoask-after") === "site") {
      var st = window.blam8SiteRead;
      if (!st || !st.done) {
        document.addEventListener("blam8:site-read", function () { maybeOnbAutoAsk(); }, { once: true });
        return;
      }
      if (st.ok) return;
    }
    sending = true; sendBtn.disabled = true;
    touch();
    showTyping();
    // The stamp lands only once the assistant actually spoke (the same rule
    // maybeSetupAutoAsk learned 2026-09-04): stamped up front, a turn lost to
    // a deploy restart left the owner with an apology and an interview that
    // could never start again this session (run #9, 2026-09-05).
    if (deviceSync) deviceSync.automatic=true;
    onboardTurn(text, log.slice(-MAX_TURNS)).then(function (ok) {
      if (deviceSync) deviceSync.automatic=false;
      sending = false; sendBtn.disabled = false;
      if (ok) { try { sessionStorage.setItem(KEY_ONB_AUTOASK, ONB_STEP); } catch (e) {} }
    }, function () { if(deviceSync)deviceSync.automatic=false; sending = false; sendBtn.disabled = false; });
  }

  // The first walk of a page (tour.js) starts with the panel closed (Kris
  // 2026-09-13: "once … the chat has auto-closed").
  window.blam8ConciergeClose = function () { if (!panel.classList.contains("hidden")) setOpen(false); };

  // ── post-onboarding guided settings hand-off ──────────────────────────────
  // The wizard's Finish isn't the end of setup: the diary, CRM and invoice
  // settings are tuned in-app. /concierge/setup-status says whether THIS
  // tenant should still be guided (src/app/setup_handoff.py); while it does, the
  // widget carries onboarding's energy — ZERO user initiative (Julius,
  // 2026-08-02): the panel announces the NEXT step itself (right tab, panel
  // open, a next-step message with a nav link to the page), and a persistent
  // chip strip in the footer slot (#bc-cta — the onboarding Continue-hint's
  // slot, unused in-app) keeps the whole walk-through visible. Every
  // affordance NAVIGATES to the settings page, where the existing flows take
  // over ("Help me set this up" → recommended values pre-filled for review).
  // Nothing is applied from here: saving stays each page's own Save. The
  // strip dismisses itself once everything's configured (the server stops
  // saying show) or on the explicit ×.
  // The wizard's own step count, mirrored from app/setup_handoff.WIZARD_STEPS.
  var WIZARD_STEPS = 4;
  var KEY_SETUP_SNOOZE = k("blam8_setup_snooze");  // "don't refetch until" stamp
  var KEY_SETUP_GREET = k("blam8_setup_greeted");  // step KEY last announced this session
  var KEY_SETUP_DONE = k("blam8_setup_done");      // completion wrap-up announced (once ever)
  var SETUP_SNOOZE_MS = 30 * 60 * 1000;
  var setupState = null;                        // {show, steps} once fetched
  // One message per step, announced unprompted when that step becomes the
  // next one. EU AI Act Art 50(1): the diary message opens the first in-app
  // thread, so it discloses the AI itself (the later ones follow the
  // ONB_HANDOFFS precedent — the top greeting already disclosed).
  // {POS} becomes "step N of M" at announce time — the count is honest about
  // how much is actually left (Julius, 2026-09-02: "one more thing" while
  // four surfaces remained). Order lives server-side in setup_handoff.steps.
  // No "then press Help me set this up" in any of these (Julius, 2026-09-03:
  // "you shouldn't have to ask for help when it's prompting you") — arriving
  // on the step's page auto-starts the recommend flow (maybeSetupAutoAsk),
  // so the copy promises exactly that: tap the button, I'll take it from
  // there.
  var SETUP_MESSAGES = {
    diary: "Four steps done — {TOTAL} to go, and I'll fill in what you've " +
      "already told me. I'm the BLAM8 AI assistant. This is {POS}: your diary " +
      "settings, so it can offer times and take bookings. Tap the button " +
      "below and I'll size the settings up for your business as soon as " +
      "we're there — ready for you to review and save.",
    requirements: "Now {POS}: your booking requirements — the checklist of " +
      "details the agent must collect before it books anyone in. It's " +
      "already pre-filled for your type of business; tap the button below " +
      "and I'll talk you through what's there and what to tweak.",
    dependants: "Now {POS}: dependants — what a booking is about that belongs " +
      "to the customer: their horses, dogs, children, vehicles, tubs. Name the " +
      "kind and what to know about each one (asked once, confirmed each time, " +
      "or a date that can hold a booking back), and every one becomes its own " +
      "record on the customer's card. Tap the button below and I'll propose " +
      "the kind for your business from what you told me — and if nothing of " +
      "the sort applies (the customer themselves is who the booking is about), " +
      "I'll say so and this step is done.",
    crm: "Now {POS}: your CRM settings — the pipeline stages your enquiries " +
      "move through, and when I should flag a contact as gone quiet. Tap " +
      "the button below and I'll recommend a set for your business as soon " +
      "as we're there.",
    invoicing: "Now {POS} — and this bit is on you: how you get paid. " +
      "Your business name, address, payment terms and bank details are " +
      "facts only you know, so I can't fill them in for you — but tap the " +
      "button below and I'll explain the page and what's worth setting.",
  };

  function nextSetupStep() {
    if (!setupState || !setupState.show) return null;
    var steps = setupState.steps || [];
    for (var i = 0; i < steps.length; i++) { if (!steps[i].done) return steps[i]; }
    return null;
  }
  function setupStepHere() {
    var step = nextSetupStep();
    return (step && step.href === window.location.pathname) ? step : null;
  }
  // Arriving on the step's own page (via the chip or otherwise) IS the
  // initiative — the assistant kicks off the page's recommend flow itself
  // instead of asking for a second "Help me set this up" click (Julius,
  // 2026-08-02). The ask text comes from the page's own [data-concierge-ask]
  // button — one source of truth. Since 2026-09-02 EVERY step page carries
  // one: invoice details has nothing to recommend, so its ask is an
  // explainer ("this bit is on you") rather than a recommend flow.
  // Once per step per session.
  var KEY_SETUP_AUTOASK = k("blam8_setup_autoask");
  function maybeSetupAutoAsk() {
    if (ONB_MODE || FIX_LOG_ID || sending) return;
    var step = setupStepHere();
    if (!step) return;
    try { if (sessionStorage.getItem(KEY_SETUP_AUTOASK) === step.key) return; } catch (e) {}
    var btn = document.querySelector("[data-concierge-ask]");
    if (!btn) return;
    // The stamp lands only once the assistant has actually spoken: live
    // 2026-09-04 the key was stamped up front, the reply came back empty (an
    // empty bubble, no error) and the step could never ask again.
    askSilently(btn.getAttribute("data-concierge-ask")).then(function (ok) {
      if (ok) { try { sessionStorage.setItem(KEY_SETUP_AUTOASK, step.key); } catch (e) {} }
    });
  }
  // The silent variant of send(): the canned question is only the TRIGGER —
  // no fabricated user bubble in the thread or the stored log (Julius,
  // 2026-08-02: putting words in the user's mouth reads wrong). The page
  // just loads and the assistant speaks: what this page is, what it needs,
  // the recommendation card. The assistant's reply lands in the log as
  // usual, so follow-up questions keep their context.
  async function askSilently(text) {
    if (deviceSync) { await deviceSync.ready; if ((deviceSync.state().payload.arrivals || []).indexOf('help:'+location.pathname)!==-1) return false; }
    if (sending) return false;
    if (panel.classList.contains("hidden")) setOpen(true);
    sending = true; sendBtn.disabled = true;
    touch();
    showTyping();
    if(deviceSync)deviceSync.automatic=true;
    var ok;
    try { ok = await helpTurn(text, log.slice(-MAX_TURNS), true); }
    finally { if(deviceSync)deviceSync.automatic=false; }
    sending = false; sendBtn.disabled = false;
    return ok;
  }
  function setupGreeted() {
    var step = nextSetupStep();
    if (!step) return true;
    try { return sessionStorage.getItem(KEY_SETUP_GREET) === step.key; } catch (e) { return true; }
  }
  // Announce the next step UNPROMPTED — the continuation of onboarding, not a
  // capability to discover: switch to the help tab (the guidance thread),
  // open the panel, append the step message + a nav link to its page. Runs
  // once per step per session (the stamp is the step key, so completing a
  // step announces the next one on the following load). The stamp is set
  // BEFORE setOpen so the setOpen→greet path can never re-enter.
  function maybeSetupGreet() {
    if (ONB_MODE || FIX_LOG_ID) return;
    var step = nextSetupStep();
    if (!step || setupGreeted()) return;
    try { sessionStorage.setItem(KEY_SETUP_GREET, step.key); } catch (e) {}
    if (panel.classList.contains("hidden")) setOpen(true);
    // No in-thread nav link: the strip chip right below the thread IS the
    // button for this step — two identical buttons an inch apart read as a
    // mistake (Julius, 2026-08-02).
    // The honest position: which rung this is and how many the ladder has.
    var all = (setupState && setupState.steps) || [];
    var doneCount = 0;
    all.forEach(function (s) { if (s.done) doneCount++; });
    // One flow of nine (Kris 2026-09-13): positions count the wizard's four
    // steps too — "step 5 of 9", never "step 1 of 5" after four are done.
    var msg = SETUP_MESSAGES[step.key] || ("Next step: " + step.label + ".");
    msg = msg.replace("{POS}", "step " + (WIZARD_STEPS + doneCount + 1) + " of " + (WIZARD_STEPS + all.length))
             .replace("{TOTAL}", String(all.length - doneCount));
    // This bubble fires on the page of the step just SAVED, where the shell's
    // strip still says "Step 7 of 9 · Dependants" (Kris, re-run 2026-09-13:
    // "the assistant says step 8 but we're still seeing 7"). So on a page
    // whose own step is done it reads as the hand-off from 7 to 8, never as
    // "now step 8" while they are looking at 7.
    var savedHere = null;
    all.forEach(function (s) { if (s.done && s.href === window.location.pathname) savedHere = s; });
    if (savedHere) {
      msg = msg.replace(/^(Now|This is) (step \d+ of \d+)/, "Next, $2");
      msg = savedHere.label + " saved \u2713 \u2014 that's " + (WIZARD_STEPS + doneCount) +
            " of " + (WIZARD_STEPS + all.length) + ". " + msg;
    }
    // Mid-conversation, no re-introduction (Julius, 2026-09-03: the thread
    // carries straight over from onboarding, so "I'm the BLAM8 AI assistant"
    // reads robotic one bubble after it already spoke). The self-disclosure
    // stays ONLY when this genuinely opens an empty thread — which is the
    // case EU AI Act Art 50(1) exists for.
    if (log.length) msg = msg.replace("I'm the BLAM8 AI assistant. ", "");
    log.push({ role: "assistant", content: msg });
    saveLog(log);
    var announceEl = bubbleEl("assistant", msg);
    msgs.appendChild(announceEl);
    pinToTop(announceEl);
  }
  function setupChip(step, isNext) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "inline-flex items-center rounded-full border px-2.5 py-1 text-[11px] font-semibold transition " +
      (step.done ? "border-slate-200 bg-slate-50 text-slate-400"
       : isNext ? "border-brand bg-brand text-white hover:bg-blue-700"
                : "border-slate-300 bg-white text-slate-600 hover:border-brand hover:text-brand");
    b.textContent = step.done ? "✓ " + step.label : step.label + (step.status ? " · " + step.status : "") + " →";
    b.addEventListener("click", function () {
      if (step.href && step.href.charAt(0) === "/") window.location.href = step.href;
    });
    return b;
  }
  function paintSetupCta() {
    if (ONB_MODE || !cta) return;
    cta.classList.add("hidden");
    cta.innerHTML = "";
    if (!setupState || !setupState.show || FIX_LOG_ID) return;
    var head = document.createElement("div");
    head.className = "flex items-center justify-between";
    var label = document.createElement("p");
    label.className = "text-[10px] font-semibold uppercase tracking-wide text-slate-400";
    label.textContent = "Finish tuning your settings";
    // No dismiss (Kris 2026-09-13): the five tuning steps are the second
    // half of setup, not a tip — the strip stays until they are done (the
    // shell's own strip carries the same position on every page).
    head.appendChild(label);
    var row = document.createElement("div");
    row.className = "mt-1.5 flex flex-wrap gap-1.5";
    // ONE step at a time (Julius, 2026-08-02): all three chips at once read
    // as a to-do pile. Show only the next incomplete step — finishing it
    // promotes the following one — and carry the position in the label.
    var steps = setupState.steps || [];
    var next = null, done = 0;
    steps.forEach(function (s) { if (s.done) done++; else if (!next) next = s; });
    if (!next) return;   // nothing left — the server stops saying show
    // Standing ON the step's page: the strip has nothing to add (Julius) —
    // the auto-ask below is the guidance here.
    if (next.href === window.location.pathname) return;
    // Mirrors the shell's strip ("Setup · 7 of 9 done") so the two never
    // disagree about where they are (Kris, re-run 2026-09-13).
    label.textContent = "Setup · " + (WIZARD_STEPS + done) + " of " + (WIZARD_STEPS + steps.length) +
      " done · next: step " + (WIZARD_STEPS + done + 1) + " of " + (WIZARD_STEPS + steps.length);
    row.appendChild(setupChip(next, true));
    cta.appendChild(head); cta.appendChild(row);
    cta.classList.remove("hidden");
  }
  async function fetchSetupStatus() {
    try {
      var until = parseInt(sessionStorage.getItem(KEY_SETUP_SNOOZE) || "0", 10);
      if (until && Date.now() < until) return;
    } catch (e) {}
    try {
      var res = await fetch("/concierge/setup-status");
      if (!res.ok) return;
      var data = await res.json();
      if (!data.show) {
        // Completing the LAST step by hand left the thread dangling on that
        // step's instructions with nothing marking the finish (Julius,
        // 2026-09-03). A genuinely completed ladder (steps present, all
        // done — a dismissed/stale one comes back with no steps) gets ONE
        // wrap-up message, and only in a browser where the walk-through
        // actually spoke (the greet stamp).
        try {
          var allDone = (data.steps || []).length > 0 &&
            data.steps.every(function (s) { return s.done; });
          if (allDone && !localStorage.getItem(KEY_SETUP_DONE)
              && sessionStorage.getItem(KEY_SETUP_GREET)) {
            localStorage.setItem(KEY_SETUP_DONE, "1");
            var doneMsg = "That's every settings step done — your agent is " +
              "fully set up. From here it reads incoming mail and drafts " +
              "replies for your review. Drafting starts on hold: have a " +
              "look at what arrives, ignore any senders it shouldn't " +
              "answer, then switch drafting on in Settings when you're " +
              "ready.";
            log.push({ role: "assistant", content: doneMsg });
            saveLog(log);
            if (!panel.classList.contains("hidden")) {
              msgs.appendChild(bubbleEl("assistant", doneMsg));
              scrollDown();
            }
            // The walk-through ends on Today, not on the last settings page
            // (Kris 2026-09-04): the wrap-up shows, then the page moves on.
            if (window.location.pathname !== "/app/" && window.location.pathname !== "/app") {
              appendNavLink({ href: "/app/", label: "Go to Today →" });
              setTimeout(function () { window.location.href = "/app/"; }, 2500);
            }
          }
        } catch (e) { /* the wrap-up is a nicety, never a break */ }
        // Past the hand-off (configured, dismissed or stale) — stop asking on
        // every page view for a while.
        try { sessionStorage.setItem(KEY_SETUP_SNOOZE, String(Date.now() + SETUP_SNOOZE_MS)); } catch (e) {}
        return;
      }
      setupState = data;
      paintSetupCta();
      // On the step's own page the announcement stands down too — the
      // auto-ask is the guidance there; everywhere else, announce as usual.
      if (setupStepHere()) maybeSetupAutoAsk(); else maybeSetupGreet();
    } catch (e) { /* a nudge, nothing lost */ }
  }
  if (IN_APP) fetchSetupStatus();

  // ── send ──────────────────────────────────────────────────────────────────
  var sending = false;
  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 112) + "px";
  }

  async function askTurn(text, history) {
    var reply, offer = false, industry = "", ticket = null, ticketStatus = null;
    try {
      var res = await fetch("/concierge/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: text, history: history }),
      });
      var data = await res.json();
      if (Number.isFinite(Number(data.questions_remaining))) {
        questionsRemaining = Number(data.questions_remaining);
        questionLimit = Number(data.questions_limit) || questionLimit;
      }
      if (res.status === 429 && data.detail && data.detail.code === "question_allowance_exhausted") {
        questionsRemaining = 0;
        reply = "You’ve used all " + (Number(data.detail.limit) || questionLimit) + " questions in this browser session. Your conversation is still here, and you can come back in a new browser session to continue.";
        applyQuestionAllowance();
      } else if (res.status === 429) {
        reply = "You’ve reached a fair-use request limit for this IP address. The limit allows up to 12 questions per minute and 150 per day. Please wait a little and try again.";
      } else if (!res.ok) {
        throw new Error("HTTP " + res.status);
      }
      if (reply) { /* allowance message already selected */ }
      else {
        reply = data.reply; offer = !!data.offer_signup; industry = data.suggested_industry || "";
        ticket = data.ticket || null; ticketStatus = data.ticket_status || null;
      }
    } catch (e) {
      reply = "Sorry — I couldn't reach the assistant just now. You can email hello@blam8.com and the team will help.";
    }
    hideTyping();
    log.push({ role: "assistant", content: reply }); saveLog(log);
    var replyEl = bubbleEl("assistant", reply);
    msgs.appendChild(replyEl);
    rememberIndustry(industry);
    if (ticket && ticket.summary && ticket.email) appendTicketCard(ticket);
    if (ticketStatus) appendStatusCard(ticketStatus);
    if (offer) showCTA();
    pinToTop(replyEl);
    applyQuestionAllowance();
  }

  // One scenario -> one answer. Stateless server side (no history sent); the reply
  // is text only (bubbleEl uses textContent). 429 = today's allowance or the
  // site-wide pause; both are said plainly, never a silent failure.
  async function scenarioTurn(text) {
    var reply;
    try {
      var res = await fetch("/concierge/scenario", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
        body: JSON.stringify({ question: text.slice(0, SCENARIO_MAX), website: "" }),
      });
      var data = {};
      try { data = await res.json(); } catch (e) {}
      if (res.status === 429) {
        reply = data.detail === "scenario_busy"
          ? "A lot of people are trying this today, so it's paused until tomorrow. The free trial lets you test BLAM8 on your own business."
          : "That's today's scenarios used (or you asked a little too quickly). Come back tomorrow, or start the free trial to test it on your own business.";
      } else if (res.ok && typeof data.reply === "string") {
        reply = data.reply;
      } else {
        throw new Error("HTTP " + res.status);
      }
    } catch (e) {
      reply = "Sorry — that didn't go through. Please try again, or email hello@blam8.com.";
    }
    hideTyping();
    log.push({ role: "assistant", content: reply }); saveLog(log);
    var replyEl = bubbleEl("assistant", reply);
    msgs.appendChild(replyEl);
    pinToTop(replyEl);
  }

  // ── support ticket review card (ask mode) ─────────────────────────────────
  // The assistant DRAFTS the ticket; nothing is filed until the visitor presses
  // "Raise ticket" here (POST /concierge/ticket — the draft-first house rule).
  var TICKET_CATEGORY_LABELS = {
    login_auth: "Sign-in / access", mailbox_connection: "Mailbox connection",
    billing: "Billing", bug: "Something broken", feature_request: "Feature request",
    access_request: "Access request", other: "Other",
  };

  function cardLine(parent, label, value) {
    if (!value) return;
    var p = document.createElement("p");
    p.className = "text-xs text-slate-500 break-words";
    var b = document.createElement("span");
    b.className = "font-semibold text-slate-600";
    b.textContent = label + ": ";
    p.appendChild(b);
    p.appendChild(document.createTextNode(value));
    parent.appendChild(p);
  }

  function appendTicketCard(ticket) {
    var wrap = document.createElement("div");
    wrap.className = "flex justify-start";
    var card = document.createElement("div");
    card.className = "max-w-[85%] rounded-2xl rounded-bl-sm border border-brand/40 bg-brand-50 px-3 py-2.5 space-y-2";
    var head = document.createElement("p");
    head.className = "text-[11px] font-semibold uppercase tracking-wide text-brand";
    head.textContent = "Support ticket — review before it's sent";
    var body = document.createElement("p");
    body.className = "text-slate-700 whitespace-pre-wrap break-words";
    body.textContent = ticket.summary;
    var details = document.createElement("div");
    details.className = "space-y-0.5";
    if (ticket.description && ticket.description !== ticket.summary)
      cardLine(details, "Details", ticket.description);
    cardLine(details, "Error message", ticket.error_message);
    cardLine(details, "Category", TICKET_CATEGORY_LABELS[ticket.category] || ticket.category);
    cardLine(details, "Account", ticket.account_type === "microsoft" ? "Microsoft"
      : ticket.account_type === "google" ? "Google" : "");
    cardLine(details, "Reply to", ticket.email);
    var note = document.createElement("p");
    note.className = "hidden text-xs text-rose-600";
    var row = document.createElement("div");
    row.className = "flex gap-2";
    var ok = document.createElement("button");
    ok.type = "button";
    ok.className = "rounded-lg bg-brand px-3 py-1.5 text-xs font-semibold text-white hover:bg-blue-700 transition disabled:opacity-40";
    ok.textContent = "Raise ticket";
    var no = document.createElement("button");
    no.type = "button";
    no.className = "rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-semibold text-slate-500 hover:bg-slate-100 transition disabled:opacity-40";
    no.textContent = "Not now";

    ok.addEventListener("click", async function () {
      ok.disabled = true; no.disabled = true; note.classList.add("hidden");
      ok.textContent = "Raising…";
      try {
        var res = await fetch("/concierge/ticket", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            email: ticket.email, category: ticket.category || "other",
            summary: ticket.summary, description: ticket.description || "",
            error_message: ticket.error_message || "",
            account_type: ticket.account_type || "",
            page_url: String(window.location.href).slice(0, 500),
          }),
        });
        var data = null;
        try { data = await res.json(); } catch (e2) { /* non-JSON error page */ }
        if (!res.ok || !data || !data.ok) {
          var detail = data && data.detail;
          throw new Error(typeof detail === "string" ? detail : "HTTP " + res.status);
        }
        var ref = data.reference || "";
        head.textContent = ref ? "Ticket " + ref + " raised ✓" : "Ticket raised ✓";
        row.remove(); details.innerHTML = "";
        var done = [
          "The team will look into this and reply at " + ticket.email + ".",
          ref ? "Quote " + ref + " in any follow-up — or ask me “what’s the status of " + ref + "?” any time." : "",
          data.deduped ? "(This looks like a ticket you already raised just now, so I’ve given you its existing reference rather than filing a duplicate.)" : "",
          data.redacted ? "(Something that looked like a password or card number was removed before filing — support never needs those.)" : "",
        ].filter(Boolean).join("\n");
        body.textContent = ticket.summary + "\n\n" + done;
        scrollDown();
      } catch (e) {
        ok.disabled = false; no.disabled = false; ok.textContent = "Raise ticket";
        note.textContent = (e && e.message && e.message.indexOf("HTTP") !== 0 && e.message.indexOf("Failed") !== 0)
          ? e.message
          : "Couldn't raise the ticket just now — try again, or email hello@blam8.com.";
        note.classList.remove("hidden");
      }
    });
    no.addEventListener("click", function () { wrap.remove(); });

    row.appendChild(ok); row.appendChild(no);
    card.appendChild(head); card.appendChild(body); card.appendChild(details);
    card.appendChild(note); card.appendChild(row);
    wrap.appendChild(card);
    msgs.appendChild(wrap);
  }

  // ── ticket status card (ask mode) ─────────────────────────────────────────
  // Rendered from the SERVER's lookup result — the assistant never invents a
  // status; when the system is unreachable the card says so honestly.
  function appendStatusCard(st) {
    var wrap = document.createElement("div");
    wrap.className = "flex justify-start";
    var card = document.createElement("div");
    card.className = "max-w-[85%] rounded-2xl rounded-bl-sm border border-slate-300 bg-white px-3 py-2.5 space-y-1";
    var head = document.createElement("p");
    head.className = "text-[11px] font-semibold uppercase tracking-wide text-slate-500";
    var body = document.createElement("p");
    body.className = "text-slate-700 whitespace-pre-wrap break-words text-sm";
    if (!st.ok) {
      head.textContent = "Ticket status";
      body.textContent = "I can't check ticket status right now — please email hello@blam8.com with your reference number and the team will help.";
    } else if (!st.found) {
      head.textContent = "Ticket status";
      body.textContent = "I couldn't find a ticket matching that reference and email address. Double-check both and try again — or email hello@blam8.com and the team will dig it out.";
    } else {
      head.textContent = "Ticket " + st.reference + " — " + st.status_label;
      var lines = [];
      if (st.summary) lines.push(st.summary);
      if (st.status_note) lines.push("Latest from the team: " + st.status_note);
      if (st.created_at) lines.push("Raised: " + st.created_at);
      body.textContent = lines.join("\n");
    }
    card.appendChild(head); card.appendChild(body);
    wrap.appendChild(card);
    msgs.appendChild(wrap);
  }

  // One POST with one retry. A deploy restart mid-turn (the app redeploys on
  // every push) drops the connection or answers 5xx for a moment; the model
  // had usually already answered. Run #9 (2026-09-05): the knowledge step
  // OPENED with the apology because of exactly that, and nothing retried.
  // Network errors and 5xx retry once after a short pause; 4xx do not.
  async function postJsonWithRetry(url, body) {
    var attempt = 0;
    var syncOperation = deviceSync ? crypto.randomUUID() : null;
    for (;;) {
      attempt++;
      try {
        var res = await (deviceSync && !FIX_LOG_ID && !SCENARIO ? deviceSync.request : fetch)(url, {
          method: "POST",
          syncOperation: syncOperation,
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (res.status >= 500 && attempt < 2) throw new Error("HTTP " + res.status);
        if (!res.ok) { var err = new Error("HTTP " + res.status); err.final = true; throw err; }
        return await res.json();
      } catch (e) {
        if (e && e.final) throw e;
        if (attempt >= 2) throw e;
        await new Promise(function (r) { setTimeout(r, 2500); });
      }
    }
  }

  // "Kept for later: Diary settings, Invoice details" — a muted line under a
  // reply, written ONLY from the server's held_for (what it actually
  // recorded), never from the assistant's prose.
  // Names what was kept ("Dependants: Vaccinations (Dog)"); the "filled in
  // for you" explainer rides the first receipt of the thread only (Kris,
  // re-run 2026-09-13: identical lines under two replies read as a repeat).
  var heldExplained = false;
  var pendingKnowledgeChoice = null;
  function knowledgeCardCurrent(card) {
    var guide = window.Blam8KnowledgeGuide;
    var latest = log.slice().reverse().find(function(entry){return entry.knowledge_card;});
    return !!(latest && latest.knowledge_card === card && guide && guide.isCurrent(card.focus_hash, card.batch_id, card.version));
  }
  function paintKnowledgeCard(group, card, live) {
    group.querySelectorAll('[data-knowledge-guide-choice]').forEach(function (button) {
      var selected = card.selected === button.getAttribute('data-knowledge-guide-choice');
      button.disabled = !!card.selected || !live;
      button.setAttribute('aria-pressed', selected ? 'true' : 'false');
      button.className = selected
        ? 'rounded-lg border border-emerald-600 bg-transparent px-3 py-2 text-sm text-emerald-700'
        : card.selected || !live
        ? 'rounded-lg border border-slate-300 bg-transparent px-3 py-2 text-sm text-slate-400 cursor-not-allowed'
        : 'rounded-lg border border-brand px-3 py-2 text-sm text-brand';
      if (button.getAttribute('data-knowledge-guide-choice') === 'edit') button.className += ' ml-auto';
    });
    var status = group.querySelector('[data-knowledge-guide-status]');
    if (status) status.textContent = card.state === 'editing'
      ? 'Editing this note · not saved'
      : card.state === 'preview'
      ? 'Correction preview · not applied or saved'
      : card.selected
      ? 'Chosen during review · not saved. Later choices may replace this.'
      : !live ? 'Earlier card · ask to show the current note to continue.' : '';
  }
  function settleKnowledgeChoice(decision) {
    // A receipt names the submitted item, not the next item returned by the planner.
    if (decision && ['prepared', 'editing', 'preview'].includes(decision.state)) {
      for (var i = log.length - 1; i >= 0; i--) {
        var card = log[i].knowledge_card;
        if (card && card.batch_id === decision.batch_id && card.version === decision.version &&
            card.focus_hash === decision.focus_hash && card.focus.kind === decision.kind &&
            card.focus.id === decision.id) {
          card.selected = decision.action; card.state = decision.state;
          break;
        }
      }
      saveLog(log);
    }
    msgs.querySelectorAll('[data-knowledge-guide-actions]').forEach(function (group) {
      if (group.knowledgeCard) paintKnowledgeCard(group, group.knowledgeCard, knowledgeCardCurrent(group.knowledgeCard));
    });
    pendingKnowledgeChoice = null;
  }
  function knowledgeCardNode(card, live) {
    if (!card || !card.focus || !Array.isArray(card.actions) || typeof card.focus_hash !== 'string') return null;
    var group = document.createElement('div');
    group.setAttribute('data-knowledge-guide-actions', '');
    group.className = 'flex flex-wrap gap-2 px-1 py-2';
    group.knowledgeCard = card;
    function addChoice(choice) {
      var button = document.createElement('button'); button.type = 'button';
      button.setAttribute('data-knowledge-guide-choice', choice.action);
      button.textContent = choice.label;
      if (choice.action === 'edit') button.setAttribute('aria-label', 'Edit this note before saving');
      button.addEventListener('click', function () {
        if (sending || !live || card.selected) return;
        if (!knowledgeCardCurrent(card)) { input.value = 'Show the current note'; send({silentUser:true}); return; }
        var guide = window.Blam8KnowledgeGuide;
        if (choice.action === 'edit') {
          if (guide.edit(card.focus_hash, card.focus.id)) settleKnowledgeChoice({batch_id:card.batch_id,
            version:card.version,focus_hash:card.focus_hash,kind:card.focus.kind,id:card.focus.id,action:'edit',state:'editing'});
          return;
        }
        pendingKnowledgeChoice = {group:group};
        input.value = choice.message; send({ silentUser: true });
      });
      group.appendChild(button);
    }
    card.actions.forEach(addChoice);
    if (card.focus.kind === 'note') addChoice({action:'edit',label:'✎ Edit note'});
    if (card.save && live) {
      var save = document.createElement('button'); save.type = 'button';
      save.className = 'rounded-lg bg-brand px-3 py-2 text-sm font-semibold text-white';
      save.textContent = 'Save reviewed decisions';
      save.addEventListener('click', function () {
        if (!sending && knowledgeCardCurrent(card)) window.Blam8KnowledgeGuide.save();
      });
      group.appendChild(save);
    }
    var status = document.createElement('p');
    status.setAttribute('data-knowledge-guide-status','');
    status.className = 'w-full text-xs text-slate-500';
    group.appendChild(status);
    paintKnowledgeCard(group,card,live);
    return group;
  }
  function appendHeldLine(receipts) {
    if (!receipts || !receipts.length) return;
    var wrap = document.createElement("div");
    wrap.className = "flex justify-start";
    var p = document.createElement("p");
    p.className = "max-w-[85%] px-1 text-[11px] text-emerald-700";
    p.textContent = "✓ Kept for later — " + receipts.join(" · ")
      + (heldExplained ? "" : " — filled in for you when you get there.");
    heldExplained = true;
    wrap.appendChild(p);
    msgs.appendChild(wrap);
  }

  async function onboardTurn(text, history) {
    var current = readCurrent();
    var reply, fill = {}, advance = false, notes = [], spoke = true, heldFor = [], reviewLink = null;
    try {
      var knowledgeGuide = ONB_STEP === "knowledge" && window.Blam8KnowledgeGuide;
      if (knowledgeGuide) msgs.querySelectorAll('[data-knowledge-guide-actions] button').forEach(function (button) { button.disabled = true; });
      var sourceReview = ONB_STEP === "knowledge" && window.Blam8KnowledgeSourceReview;
      var data = knowledgeGuide && knowledgeGuide.available()
        ? await knowledgeGuide.ask(text)
        : sourceReview && sourceReview.available()
        ? await sourceReview.ask(text)
        : await postJsonWithRetry("/concierge/onboard",
            { step: ONB_STEP, question: text, history: history, current: current });
      reply = data.reply; fill = data.form_fill || {}; advance = !!data.ready_to_advance;
      notes = Array.isArray(data.notes) ? data.notes : [];
      heldFor = Array.isArray(data.held_for) ? data.held_for : [];
      if (data.review_link) {
        notes = [];
        reviewLink = data.review_link;
      }
      if (!reply || !String(reply).trim()) throw new Error("empty reply");
      if (ONB_STEP === "interview") {
        reply += "\n\nForm edits are for review and are not saved until you submit this form. "
          + "This chat turn has not changed sending settings. "
          + (heldFor.length
            ? "The items listed as kept for later below were recorded as suggestions, not applied settings or saved Knowledge."
            : "No new items were recorded as kept for later in this turn.");
      }
      if (knowledgeGuide) settleKnowledgeChoice(data.review_decision);
    } catch (e) {
      spoke = false;
      if (window.Blam8DeviceSync && window.Blam8DeviceSync.automatic) { hideTyping(); return false; }
      if (knowledgeGuide) settleKnowledgeChoice(null);
      reply = knowledgeGuide && knowledgeGuide.available() || sourceReview && sourceReview.available() ? (e.message || "The review could not be confirmed. Your drafts remain pending; check the review before retrying.") : "Sorry — I couldn't help just now. Send that again in a moment and I'll pick it up, or fill this step in directly — I'll be here if you need me.";
    }
    hideTyping();
    var entry = { role: "assistant", content: reply };
    if (data && data.focus_hash && knowledgeGuide && data.batch_id && data.version) {
      entry.knowledge_card = {batch_id:data.batch_id,version:data.version,focus_hash:data.focus_hash,
        focus:{kind:data.focus.kind,id:data.focus.id},actions:data.guide_actions || [],save:!!data.guide_save};
    }
    log.push(entry); saveLog(log);
    var replyEl = bubbleEl("assistant", reply);
    msgs.appendChild(replyEl);
    if (entry.knowledge_card) {
      msgs.appendChild(knowledgeCardNode(entry.knowledge_card, true));
      settleKnowledgeChoice(null);
    }
    appendHeldLine(heldFor);
    applyFill(fill);
    if (reviewLink) {
      msgs.appendChild(navLinkNode(reviewLink));
      window.dispatchEvent(new CustomEvent("blam8:knowledge-review"));
    }
    // Knowledge via chat: the notes the assistant wrote from the interview,
    // as the same card the settings pages use — saved on the customer's click.
    if (notes.length) {
      appendRecommendCard({ page: "knowledge", label: "Knowledge notes", href: "/app/knowledge",
        values: notes.map(function (n) { return { name: "note", value: n.label + " | " + n.content }; }) });
    }
    showContinueHint();
    pinToTop(replyEl);
    return spoke;
  }

  // Node builder + appender split so the voice overlay's stage can render the
  // SAME links and cards (listeners included) into its own container.
  function navLinkNode(link) {
    var wrap = document.createElement("div");
    wrap.className = "flex justify-start";
    var a = document.createElement("a");
    a.href = link.href;
    a.className = "inline-flex items-center gap-1 rounded-lg border border-brand bg-brand-50 px-3 py-1.5 text-sm font-semibold text-brand hover:bg-brand hover:text-white transition";
    a.textContent = link.label + " →";
    wrap.appendChild(a);
    return wrap;
  }
  function appendNavLink(link) {
    msgs.appendChild(navLinkNode(link));
  }

  // ── feature request / issue report draft card (help mode) ─────────────────
  // The assistant DRAFTS the submission; nothing is stored until the customer
  // presses Submit here (POST /concierge/submit, CSRF double-submit via header).
  function csrfToken() {
    var m = document.cookie.match(/(?:^|;\s*)bl_csrf=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  function appendSubmitCard(submit) {
    var kindLabel = submit.kind === "bug" ? "Issue report" : "Feature request";
    var wrap = document.createElement("div");
    wrap.className = "flex justify-start";
    var card = document.createElement("div");
    card.className = "max-w-[85%] rounded-2xl rounded-bl-sm border border-brand/40 bg-brand-50 px-3 py-2.5 space-y-2";
    var head = document.createElement("p");
    head.className = "text-[11px] font-semibold uppercase tracking-wide text-brand";
    head.textContent = kindLabel + " — draft";
    var body = document.createElement("p");
    body.className = "text-slate-700 whitespace-pre-wrap break-words";
    body.textContent = submit.summary;
    var note = document.createElement("p");
    note.className = "hidden text-xs text-rose-600";
    var row = document.createElement("div");
    row.className = "flex gap-2";
    var ok = document.createElement("button");
    ok.type = "button";
    ok.className = "rounded-lg bg-brand px-3 py-1.5 text-xs font-semibold text-white hover:bg-blue-700 transition disabled:opacity-40";
    ok.textContent = "Submit to the team";
    var no = document.createElement("button");
    no.type = "button";
    no.className = "rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-semibold text-slate-500 hover:bg-slate-100 transition disabled:opacity-40";
    no.textContent = "Dismiss";

    ok.addEventListener("click", async function () {
      ok.disabled = true; no.disabled = true; note.classList.add("hidden");
      try {
        var res = await fetch("/concierge/submit", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
          body: JSON.stringify({ kind: submit.kind, summary: submit.summary }),
        });
        if (!res.ok) throw new Error("HTTP " + res.status);
        var data = await res.json();
        head.textContent = kindLabel + " — sent ✓" + (data.reference ? " · " + data.reference : "");
        row.remove();
        if (data.link && data.link.href && data.link.label) appendNavLink(data.link);
        scrollDown();
      } catch (e) {
        ok.disabled = false; no.disabled = false;
        note.textContent = "Couldn't send just now — try again, or email hello@blam8.com.";
        note.classList.remove("hidden");
      }
    });
    no.addEventListener("click", function () { wrap.remove(); });

    row.appendChild(ok); row.appendChild(no);
    card.appendChild(head); card.appendChild(body); card.appendChild(note); card.appendChild(row);
    wrap.appendChild(card);
    msgs.appendChild(wrap);
  }

  // ── recommended-settings card (help mode — docs/modules/module-concierge.md) ──────
  // The assistant recommends concrete values for ONE settings page. Nothing is
  // applied from the chat: for diary settings / booking details the button
  // stashes the values and navigates, and the page's real form is pre-filled
  // for review (applySettingsPrefill below) — saving stays the form's own Save.
  // CRM settings has no single form, so its button POSTs /concierge/crm-apply
  // (adds missing stages / sets the threshold) and links to the page to review.
  var KEY_PREFILL = k("blam8_settings_prefill");
  var PREFILL_TTL_MS = 30 * 60 * 1000;

  function prettyName(name) {
    var s = name.replace(/_/g, " ");
    return s.charAt(0).toUpperCase() + s.slice(1);
  }

  function appendRecommendCard(rec) {
    // Knowledge overlaps are shown in the review workspace; never filter them by label here.
    var wrap = document.createElement("div");
    wrap.className = "flex justify-start";
    var card = document.createElement("div");
    card.className = "max-w-[85%] rounded-2xl rounded-bl-sm border border-brand/40 bg-brand-50 px-3 py-2.5 space-y-2";
    var head = document.createElement("p");
    head.className = "text-[11px] font-semibold uppercase tracking-wide text-brand";
    head.textContent = "Recommended " + (rec.label || "settings").toLowerCase() + " — nothing applied yet";
    card.appendChild(head);
    var listWrap = document.createElement("div");
    listWrap.className = "space-y-0.5";
    // Every value renders; past the first 14 they start hidden behind an
    // "…and N more" toggle (run #7, Kris 2026-09-05: the line was plain text,
    // so the owner could never read the lines the card was about to apply).
    // Compact by default (Kris, re-run 2026-09-13: "it doesn't need to be a
    // massive list to scroll through") — wrapped lines, the first 14 shown —
    // with one Edit link that turns every line into a box; edits write straight
    // back into rec.values, so what applies is what the owner left there.
    var LONG = { note: 1, service: 1, resource: 1, field: 1, group: 1, ladder: 1, stage: 1,
                 contact_field: 1, package: 1, blackout_dates: 1 };
    var BOX = "w-full rounded-md border border-slate-300 bg-white px-2 py-1 text-xs text-slate-700 focus:border-brand focus:ring-1 focus:ring-brand/30 focus:outline-none";
    function growBox(el) { el.style.height = "auto"; el.style.height = el.scrollHeight + "px"; }
    function readLine(v) {
      var line = document.createElement("p");
      line.className = "text-xs text-slate-700 break-words whitespace-pre-wrap";
      if (v.name === "note") {
        var parts = String(v.value).split("|");
        var strong = document.createElement("span");
        strong.className = "font-semibold";
        strong.textContent = parts.shift().trim();
        line.appendChild(strong);
        line.appendChild(document.createTextNode(" — " + parts.join("|").trim()));
      } else {
        // The server says a stored code in the owner's words when it can
        // ("Working days: Monday to Saturday", not "mon,tue,wed,thu,fri,sat").
        line.textContent = v.display || (prettyName(v.name) + ": " + v.value);
      }
      return line;
    }
    function editLine(v) {
      var line = document.createElement("div");
      line.className = "space-y-0.5 text-xs text-slate-700";
      var key = document.createElement("p");
      key.className = "text-[11px] text-slate-500";
      key.textContent = prettyName(v.name);
      line.appendChild(key);
      if (v.name === "note") {
        // "Label | content" — two boxes, recombined on every keystroke.
        var parts = String(v.value).split("|");
        var noteLabel = parts.shift().trim(), noteBody = parts.join("|").trim();
        var lab = document.createElement("input");
        lab.type = "text"; lab.value = noteLabel; lab.placeholder = "Label";
        lab.setAttribute("aria-label", "Note label");
        lab.className = BOX + " font-semibold";
        var body = document.createElement("textarea");
        body.value = noteBody; body.rows = 2;
        body.setAttribute("aria-label", "Note");
        body.className = BOX + " resize-none overflow-hidden";
        function recombine() { v.value = lab.value.trim() + " | " + body.value.trim(); }
        lab.addEventListener("input", recombine);
        body.addEventListener("input", function () { growBox(body); recombine(); });
        line.appendChild(lab); line.appendChild(body);
        requestAnimationFrame(function () { growBox(body); });
      } else if (LONG[v.name] || String(v.value).length > 40) {
        var box = document.createElement("textarea");
        box.value = v.value; box.rows = 1;
        box.setAttribute("aria-label", prettyName(v.name));
        box.className = BOX + " resize-none overflow-hidden";
        box.addEventListener("input", function () { growBox(box); v.value = box.value; });
        line.appendChild(box);
        requestAnimationFrame(function () { growBox(box); });
      } else {
        var inp = document.createElement("input");
        inp.type = "text"; inp.value = v.value;
        inp.setAttribute("aria-label", prettyName(v.name));
        inp.className = BOX;
        inp.addEventListener("input", function () { v.value = inp.value; });
        line.appendChild(inp);
      }
      return line;
    }
    var editing = false;
    function renderLines() {
      listWrap.textContent = "";
      var hiddenLines = [];
      rec.values.forEach(function (v, i) {
        var line = editing ? editLine(v) : readLine(v);
        if (!editing && i >= 14) { line.classList.add("hidden"); hiddenLines.push(line); }
        listWrap.appendChild(line);
      });
      if (hiddenLines.length) {
        var more = document.createElement("button");
        more.type = "button";
        more.className = "text-xs text-brand underline underline-offset-2";
        more.textContent = "…and " + hiddenLines.length + " more — show all";
        more.addEventListener("click", function () {
          var open = hiddenLines[0].classList.contains("hidden");
          hiddenLines.forEach(function (l) { l.classList.toggle("hidden", !open); });
          more.textContent = open ? "Show fewer" : "…and " + hiddenLines.length + " more — show all";
        });
        listWrap.appendChild(more);
      }
    }
    renderLines();
    // The Edit link sits beside the apply button (Kris 2026-09-13), not in
    // the header; it goes with the button once the card has applied.
    var editLink = document.createElement("button");
    editLink.type = "button";
    editLink.className = "text-xs font-semibold text-brand hover:underline";
    editLink.textContent = "Edit";
    editLink.setAttribute("data-card-edit", "1");
    editLink.addEventListener("click", function () {
      editing = !editing;
      editLink.textContent = editing ? "Done" : "Edit";
      renderLines();
    });
    card.appendChild(listWrap);
    var note = document.createElement("p");
    note.className = "hidden text-xs text-rose-600";
    var row = document.createElement("div");
    row.className = "flex flex-wrap gap-2 items-center";
    var ok = document.createElement("button");
    ok.type = "button";
    ok.className = "rounded-lg bg-brand px-3 py-1.5 text-xs font-semibold text-white hover:bg-blue-700 transition disabled:opacity-40";
    ok.textContent = rec.page === "knowledge" ? "Review these notes" : "Use recommended settings";
    var hint = document.createElement("span");
    hint.className = "text-[11px] text-slate-500";
    hint.textContent = rec.page === "crm_settings"
      ? "applies at once — this page has no Save; anything not listed is removed"
      : (rec.page === "knowledge"
         ? "opens an evidence and duplicate review before anything is saved"
         : "fills the page — you review, then save");
    row.appendChild(ok); row.appendChild(editLink); row.appendChild(hint);

    // A knowledge card is pending until saved: the panel's Next waits for it
    // and the page's Next saves it first. `save` is the one routine both use.
    var pendingEntry = null;
    async function saveKnowledgeCard() {
      ok.disabled = true; note.classList.add("hidden");
      var kres = await fetch("/concierge/knowledge-apply", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
        body: JSON.stringify({ values: rec.values }),
      });
      if (!kres.ok) throw new Error("HTTP " + kres.status);
      var kdata = await kres.json();
      head.textContent = "Knowledge notes — pending review";
      if (kdata.link) msgs.appendChild(navLinkNode(kdata.link));
      window.dispatchEvent(new CustomEvent("blam8:knowledge-review"));
      hint.textContent = kdata.summary || "";
      ok.remove(); editLink.remove();
      if (pendingEntry) {
        var at = PENDING_KB_CARDS.indexOf(pendingEntry);
        if (at !== -1) PENDING_KB_CARDS.splice(at, 1);
        pendingEntry = null;
        if (!PENDING_KB_CARDS.length && ADVANCE_WANTED) showContinueHint();
      }
      return kdata;
    }
    if (rec.page === "knowledge") {
      pendingEntry = { save: saveKnowledgeCard, labels: (rec.values || []).map(function (v) {
        return String(v.value || "").split("|")[0].trim().toLowerCase(); }).filter(Boolean) };
      PENDING_KB_CARDS.push(pendingEntry);
      if (ADVANCE_WANTED) showContinueHint();   // swap a shown Next for the hold line
    }

    ok.addEventListener("click", async function () {
      if (["diary_settings", "booking_details", "dependants"].indexOf(rec.page) !== -1) {
        ok.disabled = true; note.classList.add("hidden");
        try {
          var checkedResponse = await fetch("/concierge/settings-rows", {
            method: "POST", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({page: rec.page, values: rec.values})
          });
          var checkedCard = await checkedResponse.json();
          if (!checkedResponse.ok) throw new Error(checkedCard.detail || "Your settings could not be checked.");
          if (!checkedCard.ok) throw new Error((checkedCard.values || []).flatMap(function (v) {
            return v.problems || [];
          }).join(" ") || "Please check the settings on this card.");
          rec.values = checkedCard.values;
        } catch (e) {
          ok.disabled = false;
          note.textContent = e.message;
          note.classList.remove("hidden");
          return;
        }
      }
      if (rec.page === "knowledge") {
        try {
          await saveKnowledgeCard();
          scrollDown();
          // The review-fragment event preserves existing form edits; never replace all of main here.
        } catch (e) {
          ok.disabled = false;
          note.textContent = "Couldn't stage the review just now — try again in a moment.";
          note.classList.remove("hidden");
        }
        return;
      }
      if (rec.page === "crm_settings") {
        ok.disabled = true; note.classList.add("hidden");
        try {
          var res = await fetch("/concierge/crm-apply", {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
            body: JSON.stringify({ values: rec.values }),
          });
          if (!res.ok) throw new Error("HTTP " + res.status);
          var data = await res.json();
          head.textContent = "Recommended CRM settings — applied ✓";
          hint.textContent = data.summary || "";
          ok.remove(); editLink.remove();
          if (data.link && data.link.href && data.link.label) appendNavLink(data.link);
          scrollDown();
          // The page BEHIND the panel still shows its pre-apply state ("No
          // stages yet" beside an applied-✓ card — Julius, 2026-09-03). On
          // the settings page itself, reload so the pipeline appears.
          if (window.location.pathname === "/app/crm/settings") {
            window.location.href = "/app/crm/settings?msg=saved";
          }
        } catch (e) {
          ok.disabled = false;
          note.textContent = "Couldn't apply just now — try again in a moment.";
          note.classList.remove("hidden");
        }
        return;
      }
      // Same-origin guard, then stash-and-navigate: the destination page's form
      // is filled on arrival and NOTHING is saved until its own Save is pressed.
      if (!rec.href || rec.href.charAt(0) !== "/") return;
      try {
        sessionStorage.setItem(KEY_PREFILL, JSON.stringify(
          { page: rec.page, href: rec.href, values: rec.values, ts: Date.now() }));
      } catch (e) { /* storage unavailable → just navigate; the page shows as-is */ }
      window.location.href = rec.href;
    });

    card.appendChild(note); card.appendChild(row);
    wrap.appendChild(card);
    msgs.appendChild(wrap);
  }

  // `auto` = the widget asked by itself on arriving at a setup step; with
  // the path, the server keeps a reply's cards to the page in front of the
  // owner while the walk is on (re-run 2026-09-13: five finished pages
  // re-offered on the Invoice step).
  async function helpTurn(text, history, auto) {
    var reply, link = null, submit = null, recommend = null, spoke = true, closedStep = null, heldFor = [];
    try {
      var sendHelp = setupCards ? window.Blam8SetupCards.request : postJsonWithRetry;
      var data = await sendHelp("/concierge/help", { question: text, history: history,
                               path: window.location.pathname, auto: !!auto, card_mode: !!setupCards });
      reply = data.reply; link = data.link || null; submit = data.submit || null;
      recommend = data.recommend || null;
      closedStep = data.closed_step || null;
      heldFor = Array.isArray(data.held_for) ? data.held_for : [];
      // An empty reply is a failure that looks like silence (an empty bubble,
      // live 2026-09-04) — say so, and tell the caller nothing was said.
      if (!reply || !String(reply).trim()) throw new Error("empty reply");
    } catch (e) {
      spoke = false;
      reply = "Sorry — I couldn't answer just now. Reload the page and I'll try again, or email hello@blam8.com and the team will help.";
    }
    hideTyping();
    log.push({ role: "assistant", content: reply }); saveLog(log);
    var replyEl = bubbleEl("assistant", reply);
    msgs.appendChild(replyEl);
    appendHeldLine(heldFor);
    if (setupCards) setupCards.load();
    // Never a nav chip to the page we're standing on (Julius, 2026-08-02) —
    // it duplicates the recommend card's own link and reads as clutter.
    if (link && link.href && link.label && link.href !== window.location.pathname) appendNavLink(link);
    if (submit && submit.kind && submit.summary) appendSubmitCard(submit);
    // One card per recommended page (a list since 2026-09-02 — a single slot
    // silently dropped the second page of a two-page recommendation).
    var recs = Array.isArray(recommend) ? recommend : (recommend ? [recommend] : []);
    recs.forEach(function (r) {
      if (r && r.page && r.values && r.values.length) appendRecommendCard(r);
    });
    pinToTop(replyEl);
    // A hand-off step closed by this very turn (the Dependants "nothing of
    // the sort applies" verdict): refetch the walk-through now so the strip
    // moves on to the next open step in the same breath — it used to keep
    // offering the step just declined until the owner gave up (run #9).
    if (closedStep) {
      try { sessionStorage.removeItem(KEY_SETUP_SNOOZE); } catch (e) {}
      fetchSetupStatus();
    }
    return spoke;
  }

  // ── fix mode: diagnose a bad draft, apply ONE approved action ─────────────
  // The endpoint STREAMS newline-JSON: "tool" events (a grounding search just
  // started — surfaced live in place of the typing ellipsis) then one "done".
  async function fixTurn(text, history) {
    var reply = null, action = null, trail = [], fixQueue = [];
    var strip = null, livePill = null, liveItem = null;
    try {
      var res = await fetch("/concierge/fix", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ log_id: FIX_LOG_ID, question: text, history: history }),
      });
      if (!res.ok || !res.body) throw new Error("HTTP " + res.status);
      var reader = res.body.getReader();
      var dec = new TextDecoder();
      var buf = "";
      while (true) {
        var chunk = await reader.read();
        if (chunk.done) break;
        buf += dec.decode(chunk.value, { stream: true });
        var lines = buf.split("\n");
        buf = lines.pop();
        for (var i = 0; i < lines.length; i++) {
          if (!lines[i].trim()) continue;
          var ev = JSON.parse(lines[i]);
          if (ev.type === "tool") {
            if (!strip) {
              strip = trailStrip();
              if (typingEl) { msgs.insertBefore(strip.wrap, typingEl); } else { msgs.appendChild(strip.wrap); }
            }
            settleTrailPill(livePill, liveItem);
            liveItem = ev;
            livePill = trailPill(strip, ev, true);
            scrollDown();
          } else if (ev.type === "done") {
            reply = ev.reply; action = ev.action || null; trail = ev.trail || [];
            fixQueue = Array.isArray(ev.queue) ? ev.queue : [];
          }
        }
      }
      if (reply === null) throw new Error("empty stream");
      // A blank reply with nothing proposed never renders as an empty bubble.
      if (!String(reply).trim() && !(action && action.kind)) throw new Error("empty reply");
    } catch (e) {
      reply = "Sorry — I couldn't help just now. You can edit the draft directly, or add what's missing on the Knowledge page.";
    }
    settleTrailPill(livePill, liveItem);
    hideTyping();
    log.push({ role: "assistant", content: reply, trail: trail }); saveLog(log);
    var replyEl = bubbleEl("assistant", reply);
    msgs.appendChild(replyEl);
    if (action && action.kind) appendFixCard(action, fixQueue);
    pinToTop(replyEl);
  }

  // The owner's own words from this fix thread (round three, 1 Oct 2026, F25),
  // sent with every apply so a never-rule / note / requirement redrafts the
  // reply with ALL of what they said, not just the one line the card saved.
  // fixDirection() joins the user turns (the thread began when fix mode opened
  // for this draft); over the 2000-char server cap the oldest whole turns go.
  var FIX_DIRECTION_MAX = 2000;
  function fixDirection() {
    var turns = log.filter(function (m) { return m.role === "user" && m.content; })
                   .map(function (m) { return String(m.content).trim(); })
                   .filter(Boolean);
    while (turns.length > 1 && turns.join("\n").length > FIX_DIRECTION_MAX) turns.shift();
    return turns.join("\n").slice(0, FIX_DIRECTION_MAX);
  }

  var FIX_LABELS = {
    never: "Never-say rule",
    knowledge: "Add knowledge",
    requirement: "Booking requirement",
    redraft: "Re-draft",
  };

  // `queue`: the further fixes behind this one (Kris 2026-09-04: a task list,
  // one card at a time). The next card appears once this one is applied or
  // declined — never two open at once, never a "which first?" question.
  // One open fix card at a time, and never the same card twice: the model
  // re-proposes the note it already offered when the operator says "yes, that
  // wording works", and a card already applied must not come back with a live
  // Save (Kris 2026-09-04, screenshot).
  var pendingFixCards = [];   // cards from later turns, waiting for the open one
  var fixCardsOpen = {};      // key → true while a card is open
  var fixCardsApplied = {};   // key → true once applied this session
  function fixCardKey(action) {
    return [action.kind, (action.label || "").trim().toLowerCase(),
            (action.content || "").trim().toLowerCase().slice(0, 400)].join("|");
  }
  function appendFixCard(action, queue) {
    var logId = FIX_LOG_ID;   // captured — the card must apply to ITS draft even after a reset
    var isKb = action.kind === "knowledge";
    var rest = Array.isArray(queue) ? queue.slice() : [];
    var cardKey = fixCardKey(action);
    if (fixCardsOpen[cardKey] || fixCardsApplied[cardKey]) {
      // Already offered (or done): skip to whatever is genuinely new behind it.
      var n0 = rest.shift();
      if (n0 && n0.kind) appendFixCard(n0, rest);
      return;
    }
    if (Object.keys(fixCardsOpen).length) {
      // Another card is still open — this one waits its turn behind it.
      pendingFixCards.push({ action: action, rest: rest });
      return;
    }
    fixCardsOpen[cardKey] = true;
    function nextCard() {
      delete fixCardsOpen[cardKey];
      if (rest.length) {
        var n = rest.shift();
        if (n && n.kind) { appendFixCard(n, rest); scrollDown(); return; }
      }
      var w = pendingFixCards.shift();
      if (w) { appendFixCard(w.action, w.rest); scrollDown(); }
    }
    var wrap = document.createElement("div");
    wrap.className = "flex justify-start";
    var card = document.createElement("div");
    card.className = "max-w-[85%] rounded-2xl rounded-bl-sm border border-brand/40 bg-brand-50 px-3 py-2.5 space-y-2";
    var head = document.createElement("p");
    head.className = "text-[11px] font-semibold uppercase tracking-wide text-brand";
    head.textContent = (FIX_LABELS[action.kind] || "Fix") + " — proposed"
      + (rest.length ? " · " + rest.length + " more after this" : "");
    // A knowledge fix is a NOTE the operator is about to keep, so they get to name
    // it and edit the wording here — the assistant drafted it, the panel saves it.
    // Everything else is applied as proposed (a rule / requirement is one line the
    // assistant already agreed with them in the chat).
    var kbName = null, body;
    if (isKb) {
      kbName = document.createElement("input");
      kbName.type = "text";
      kbName.maxLength = 80;
      kbName.value = action.label || "";
      kbName.placeholder = "Note name";
      kbName.className = "w-full rounded-lg border border-brand/30 bg-white px-2 py-1 text-xs font-semibold text-slate-700";
      body = document.createElement("textarea");
      body.rows = 4;
      body.value = action.content || "";
      body.className = "w-full rounded-lg border border-brand/30 bg-white px-2 py-1.5 text-slate-700 resize-y";
    } else {
      body = document.createElement("p");
      body.className = "text-slate-700 whitespace-pre-wrap break-words";
      body.textContent = action.content || "Regenerate this draft from scratch.";
    }
    var note = document.createElement("p");
    note.className = "hidden text-xs text-rose-600";
    var row = document.createElement("div");
    row.className = "flex gap-2";
    var ok = document.createElement("button");
    ok.type = "button";
    ok.className = "rounded-lg bg-brand px-3 py-1.5 text-xs font-semibold text-white hover:bg-blue-700 transition disabled:opacity-40";
    ok.textContent = isKb ? "Save & re-draft" : "Apply & re-draft";
    var no = document.createElement("button");
    no.type = "button";
    no.className = "rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-semibold text-slate-500 hover:bg-slate-100 transition disabled:opacity-40";
    no.textContent = "Not that";

    ok.addEventListener("click", async function () {
      var content = isKb ? body.value.trim() : (action.content || "");
      var label = isKb ? kbName.value.trim() : "";
      if (isKb && !content) {
        note.textContent = "Write the note first — what should the agent know?";
        note.classList.remove("hidden");
        body.focus();
        return;
      }
      ok.disabled = true; no.disabled = true; note.classList.add("hidden");
      ok.textContent = isKb ? "Saving…" : "Applying…";
      try {
        var res = await fetch("/concierge/fix/apply", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
          body: JSON.stringify({ log_id: logId, kind: action.kind, content: content, label: label,
                                 direction: fixDirection() }),
        });
        if (!res.ok) {
          // A refusal with a reason (a rewrite that would shrink a note, run #8)
          // reads as the reason — "try again in a moment" would have hidden it.
          var why = "";
          try { why = String(((await res.json()) || {}).detail || ""); } catch (e2) { why = ""; }
          var err = new Error("HTTP " + res.status); err.why = why.slice(0, 400); throw err;
        }
        var data = await res.json();
        head.textContent = (data.applied || "Applied") + " ✓ — draft regenerated";
        fixCardsApplied[cardKey] = true;
        if (isKb) {
          // The note is saved: freeze the editors so the card reads as a receipt.
          kbName.disabled = true; body.disabled = true;
          kbName.className += " opacity-70"; body.className += " opacity-70";
        }
        row.remove();
        // Swap the fresh draft into the page's textarea, if the thread is showing it.
        var ta = document.getElementById("draft-" + logId);
        if (ta && data.draft) {
          ta.value = data.draft;
          ta.dispatchEvent(new Event("input", { bubbles: true }));
        }
        scrollDown();
        nextCard();
      } catch (e) {
        ok.disabled = false; no.disabled = false;
        ok.textContent = isKb ? "Save & re-draft" : "Apply & re-draft";
        note.textContent = (e && e.why) ? e.why : "Couldn't apply just now — try again in a moment.";
        note.classList.remove("hidden");
      }
    });
    no.addEventListener("click", function () { wrap.remove(); nextCard(); });

    row.appendChild(ok); row.appendChild(no);
    card.appendChild(head);
    if (kbName) card.appendChild(kbName);
    card.appendChild(body);
    if (isKb) {
      var hint = document.createElement("p");
      hint.className = "text-xs text-slate-500";
      hint.textContent = "Saved to your knowledge base as this note — edit either box first if you want.";
      card.appendChild(hint);
    }
    card.appendChild(note); card.appendChild(row);
    wrap.appendChild(card);
    msgs.appendChild(wrap);
  }

  // Re-fetch the current page and swap its <main> — the panel (and its open
  // cards) lives outside main, so nothing the assistant offered is lost.
  async function refreshMainInPlace() {
    try {
      var res = await fetch(window.location.pathname, { credentials: "same-origin",
                                                        headers: { "X-Requested-With": "fetch" } });
      if (!res.ok) return;
      var html = await res.text();
      var doc = new DOMParser().parseFromString(html, "text/html");
      var fresh = doc.querySelector("main"), here = document.querySelector("main");
      if (fresh && here) here.innerHTML = fresh.innerHTML;
    } catch (e) { /* the page keeps its old state; the notes are saved regardless */ }
  }

  async function send(options) {
    if (deviceSync) { try { await deviceSync.ready; await deviceSync.flush(); } catch (_) { return; } }
    options = options || {};
    var silentUser = !!options.silentUser;
    var text = input.value.trim();
    if (!text || sending) return;
    if (!AUTHED && !SCENARIO && questionsRemaining !== null && questionsRemaining <= 0) {
      applyQuestionAllowance();
      settleKnowledgeChoice(null);
      return;
    }
    sending = true; sendBtn.disabled = true; input.value = ""; autosize();

    if (!silentUser) {
      log.push({ role: "user", content: text }); saveLog(log);
      refreshTitle();
      msgs.appendChild(bubbleEl("user", text));
    }
    touch();
    clearPin();
    unpinCaveat();
    scrollDown(); showTyping();

    var history = (silentUser ? log : log.slice(0, -1)).slice(-MAX_TURNS);
    if (FIX_LOG_ID) { await fixTurn(text, history); }
    else if (ONB_MODE) { await onboardTurn(text, history); }
    else if (HELP_MODE) { await helpTurn(text, history); }
    else if (SCENARIO) { await scenarioTurn(text); }
    else { await askTurn(text, history); }

    sending = false; sendBtn.disabled = false; applyQuestionAllowance(); input.focus();
    if (window.Blam8KnowledgeGuide) window.Blam8KnowledgeGuide.start();
  }

  // ── wiring ────────────────────────────────────────────────────────────────
  // The bottom-right logo is a normal tap/click launcher.
  bubble.addEventListener("click", function () { setOpen(panel.classList.contains("hidden")); });
  closeBtn.addEventListener("click", function () { setOpen(false); });
  var clearBtn = document.getElementById("bc-clear");
  if (clearBtn) clearBtn.addEventListener("click", function () { clearThread(); if (input) input.focus(); });
  form.addEventListener("submit", function (e) { e.preventDefault(); send(); });
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !panel.classList.contains("hidden")) setOpen(false);
  });

  // Programmatic open + ask: any element with [data-concierge-ask="a question"] opens the
  // assistant and asks it straight away — e.g. a "Help me set this up" button on the Diary
  // settings page. In-app this runs Help mode (which knows the customer + their settings).
  function openWith(text) {
    setOpen(true);
    if (text && input) { input.value = text; autosize(); send(); }
  }
  // Programmatic asks are HELP questions (e.g. "Help me set this up" on Diary
  // settings, or /help in the search box).
  window.blam8ConciergeAsk = function (text) {
    openWith(text);
  };

  // The landing FAQ's "Run it past BLAM8" link: [data-scenario-open] opens the chat
  // in scenario mode with a fresh thread (public pages only).
  if (!AUTHED) {
    document.addEventListener("click", function (e) {
      var t = e.target && e.target.closest ? e.target.closest("[data-scenario-open]") : null;
      if (!t) return;
      e.preventDefault();
      clearThread();
      setScenario(true);
      renderAll();
      setOpen(true);
    });
  }

  // Thumbs-down on a draft: open the assistant scoped to THAT draft (fix mode) with
  // a fresh thread. It opens by asking what's wrong (FIX_GREETING) and waits —
  // nothing is sent until the operator says. Replaces the old "Something's wrong"
  // panel + the legacy /app/drafts fix-page detour.
  window.blam8ConciergeFix = function (logId) {
    clearThread();
    setFixLogId(logId);
    paintSetupCta();   // fix mode owns the panel — the guided strip steps aside
    refreshTitle();
    touch();           // a fresh thread, never judged stale on open
    setOpen(true);     // paints the question; the first turn is theirs to start
  };
  document.addEventListener("click", function (e) {
    var trigger = e.target.closest && e.target.closest("[data-concierge-ask]");
    if (!trigger) return;
    e.preventDefault();
    // The canned question is the TRIGGER, not the owner's words: a page chip
    // ("What goes here?", "Help me set this up") used to post a paragraph as
    // a user bubble in their name (run #9, 2026-09-05). Same rule as the
    // auto-ask — the assistant just speaks.
    var text = trigger.getAttribute("data-concierge-ask") || "";
    if (text && !ONB_MODE) { askSilently(text); return; }
    openWith(text);
  });

  // ── Recommended-settings prefill (destination side) ─────────────────────────
  // The "Use recommended settings" card stashed {page, values} and navigated here.
  // Fill the page's REAL form and tell the user to review — nothing is saved
  // until they press the form's own Save. Values came whitelisted from the server
  // (src/app/settings_recommend.py); anything that doesn't match an input is skipped.
  function pfEvents(el) {
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    // Open the fold the value landed in: the banner says "review each
    // setting" while the collapsed faces still read the OLD receipts ("No
    // buffer", "Recurring Off") — the inputs behind them held the new values
    // but nothing on screen said so (run #9, 2026-09-05).
    var fold = el.closest ? el.closest("details") : null;
    if (fold) fold.open = true;
  }
  function pfNumber(raw) {
    var m = String(raw == null ? "" : raw).match(/\d+(\.\d+)?/);
    return m ? m[0] : "";
  }
  // Held-for-later prefill mode (src/app/setup_held.py): on a page the owner has
  // already saved, held facts land in EMPTY fields only — a saved value is
  // never overwritten, and checkbox/select groups (which are never "empty")
  // are left alone. A never-saved page fills freely.
  var PF_ONLY_EMPTY = false;
  // Fill one scalar field by name: checkbox groups take a CSV of values to tick,
  // lone checkboxes take "1"/"0", selects only accept an existing option.
  function pfSetField(form, name, value) {
    var els = form.querySelectorAll('[name="' + name + '"]');
    if (!els.length) return false;
    var el = els[0];
    if (PF_ONLY_EMPTY && (el.type === "checkbox" || el.tagName === "SELECT" || String(el.value || "").trim())) return false;
    if (el.type === "radio") {
      // A two-way choice (e.g. whether prices include VAT): tick the option
      // whose value matches; never write into a radio's own value.
      var hit = false;
      els.forEach(function (r) {
        var on = r.value === value;
        if (on) hit = true;
        if (r.checked !== on) { r.checked = on; pfEvents(r); }
      });
      return hit;
    }
    if (el.type === "checkbox") {
      if (els.length > 1 || el.value !== "1") {
        var want = value.toLowerCase().split(",").map(function (s) { return s.trim(); });
        els.forEach(function (c) {
          var on = want.indexOf(c.value.toLowerCase()) !== -1;
          if (c.checked !== on) { c.checked = on; pfEvents(c); }
        });
      } else {
        var on1 = value === "1" || value.toLowerCase() === "true";
        if (el.checked !== on1) { el.checked = on1; pfEvents(el); }
      }
      return true;
    }
    if (el.tagName === "SELECT") {
      var has = Array.prototype.some.call(el.options, function (o) { return o.value === value; });
      if (has) { el.value = value; pfEvents(el); }
      return has;
    }
    el.value = value;
    pfEvents(el);
    return true;
  }
  // Booking settings are gated (src/app/booking_policy.py): capacity, working days,
  // opening hours and timezone each carry a state select ("Not set yet" until
  // the owner applies a value) and a confirm tick. The form posts the select
  // whatever the input holds, so a value the card typed with the select still
  // on "Not set yet" was dropped by Save (first-sign-up film, 30 Sep 2026: the
  // owner did "Use recommended settings" then "Save diary settings" and the
  // diary still could not book — four "Not set yet" lines). When the card
  // fills a gated field, the gate is set to "Apply value" and ticked: the
  // owner's own Save is the confirmation. Europe/London is the only timezone
  // the form offers, so an empty timezone is set with them.
  var PF_GATES = { capacity_per_window: "capacity", working_days: "working_days",
                   open_time: "opening_hours", close_time: "opening_hours" };
  function pfOpenGates(form, gates) {
    var keys = Object.keys(gates);
    if (!keys.length) return;
    var tz = form.querySelector('select[name="timezone"]');
    if (tz && !tz.value) {
      var hasTz = Array.prototype.some.call(tz.options, function (o) { return o.value === "Europe/London"; });
      if (hasTz) { tz.value = "Europe/London"; pfEvents(tz); keys.push("timezone"); }
    }
    keys.forEach(function (k) {
      var st = form.querySelector('select[name="' + k + '_state"]');
      if (st && st.value !== "set") { st.value = "set"; pfEvents(st); }
      var c = form.querySelector('input[name="confirm_booking_setting"][value="' + k + '"]');
      if (c && !c.checked) { c.checked = true; pfEvents(c); }
    });
  }
  // Rebuild a repeating-row group from "a | b | c" entries using the page's own
  // <template>, filling the given input names positionally (numeric names get
  // digit-extraction so "90 min" still lands).
  // "Name | 14:00 | 1" and "Name | 22:00 | Sun | yes | 140 | 65" onto the
  // matching service row's own boxes (docs/plan-service-urgency.md, D1). A
  // row the card didn't name is left alone; who decides stays "ask me first".
  function pfFillServiceRules(urgent, ooh, afterJob) {
    function rowFor(name) {
      var want = String(name || "").trim().toLowerCase();
      return Array.prototype.find.call(document.querySelectorAll("#svc-rows .svc-row"), function (r) {
        var n = r.querySelector('[name="service_name"]');
        return n && n.value.trim().toLowerCase() === want;
      }) || null;
    }
    function put(row, name, v) {
      var el = row.querySelector('[name="' + name + '"]');
      if (!el || v == null || v === "") return;
      el.value = v; pfEvents(el);
    }
    urgent.forEach(function (v) {
      var p = v.split("|").map(function (x) { return x.trim(); });
      var row = rowFor(p[0]); if (!row) return;
      put(row, "service_same_day", p[1]); put(row, "service_notice", pfNumber(p[2] || ""));
    });
    ooh.forEach(function (v) {
      var p = v.split("|").map(function (x) { return x.trim(); });
      var row = rowFor(p[0]); if (!row) return;
      put(row, "service_ooh_until", p[1]); put(row, "service_ooh_days", p[2]);
      put(row, "service_ooh_bh", /^(y|yes|1|true|bank)/i.test(p[3] || "") ? "1" : "");
      put(row, "service_ooh_price", pfNumber(p[4] || "")); put(row, "service_ooh_after", pfNumber(p[5] || ""));
    });
    // After the job: "Service | Task — N days; Task — N working days".
    (afterJob || []).forEach(function (v) {
      var cut = v.indexOf("|"); if (cut < 0) return;
      var row = rowFor(v.slice(0, cut)); if (!row) return;
      put(row, "service_after_job", v.slice(cut + 1).trim());
    });
  }
  function pfFillRows(rowsId, tplId, entries, inputNames, numeric) {
    var rows = document.getElementById(rowsId), tpl = document.getElementById(tplId);
    if (!rows || !tpl || !entries.length) return;
    if (PF_ONLY_EMPTY && rows.children.length) return;   // saved rows stay as they are
    rows.innerHTML = "";
    // A "time | half | day" token belongs to service_timing wherever the model
    // put it: live 2026-09-04 the card said "Full set of shoes | 60 | half" (no
    // empty price slot) and "half" landed in the numeric price column and was
    // dropped — every service saved as an exact time.
    var timingAt = inputNames.indexOf("service_timing");
    entries.forEach(function (value) {
      // An entry may arrive as an ARRAY of parts (first-walk findings, 1 Oct 2026:
      // a checklist row scoped to two services was joined with "|" and then split
      // again here, so the second service landed in the next column).
      var parts = Array.isArray(value)
        ? value.map(function (s) { return String(s == null ? "" : s).trim(); })
        : value.split("|").map(function (s) { return s.trim(); });
      if (timingAt !== -1) {
        var found = null;
        parts = parts.filter(function (p, i) {
          if (i > 0 && found === null && /^(time|half|day)$/i.test(p)) { found = p.toLowerCase(); return false; }
          return true;
        });
        if (found !== null) {
          while (parts.length < timingAt) parts.push("");
          parts[timingAt] = found;
        }
      }
      rows.appendChild(tpl.content.cloneNode(true));
      var row = rows.lastElementChild;
      inputNames.forEach(function (name, i) {
        var el = row.querySelector('[name="' + name + '"]');
        if (!el || parts[i] == null || parts[i] === "") return;
        var v = numeric.indexOf(name) !== -1 ? pfNumber(parts[i]) : parts[i];
        if (el.tagName === "SELECT") {
          var lower = v.toLowerCase();
          var hasOpt = Array.prototype.some.call(el.options, function (o) { return o.value === lower; });
          if (hasOpt) el.value = lower;
        } else if (v !== "") {
          el.value = v;
        }
        pfEvents(el);
      });
    });
  }
  function pfBanner(form, held) {
    var div = document.createElement("div");
    div.className = "rounded-lg bg-brand-50 border border-brand/30 px-4 py-3 text-sm text-navy mb-4";
    div.innerHTML = "";
    var strong = document.createElement("span");
    strong.className = "font-semibold";
    strong.textContent = held ? "Filled in from what you told the assistant." : "Recommended settings filled in.";
    div.appendChild(strong);
    div.appendChild(document.createTextNode(held
      ? " Check each one, change anything, then Save — nothing is applied until you do."
      : " Review each setting, then press Save — nothing is applied until you do."));
    form.parentNode.insertBefore(div, form);
  }
  function pluck(values, name) {
    return values.filter(function (v) { return v.name === name; })
                 .map(function (v) { return v.value; });
  }
  function applySettingsPrefill() {
    var raw;
    try { raw = sessionStorage.getItem(KEY_PREFILL); } catch (e) { return applyHeldPrefill(); }
    if (!raw) return applyHeldPrefill();
    var data;
    try { data = JSON.parse(raw); } catch (e) { sessionStorage.removeItem(KEY_PREFILL); return applyHeldPrefill(); }
    if (!data || !data.ts || Date.now() - data.ts > PREFILL_TTL_MS) {
      sessionStorage.removeItem(KEY_PREFILL); return applyHeldPrefill();
    }
    if (window.location.pathname !== data.href) return applyHeldPrefill();   // keep until they arrive (or it expires)
    applyPrefillData(data);
  }
  // Held for later (src/app/setup_held.py): the shell hands the page what the
  // owner told the assistant for it before arriving (#setup-prefill). Only
  // the pages whose prefill just TYPES into a form — never crm_fields, whose
  // "prefill" posts rows. A saved page takes empty fields only.
  var HELD_PAGES = ["diary_settings", "booking_details", "dependants", "invoice_settings", "packages"];
  function applyHeldPrefill() {
    var el = document.getElementById("setup-prefill");
    if (!el) return;
    var data;
    try { data = JSON.parse(el.textContent || ""); } catch (e) { return; }
    if (!data || !data.page || HELD_PAGES.indexOf(data.page) === -1) return;
    if (!data.values || !data.values.length) return;
    PF_ONLY_EMPTY = !data.fresh;
    try {
      applyPrefillData({ page: data.page, href: window.location.pathname, values: data.values, held: true });
    } finally {
      PF_ONLY_EMPTY = false;
    }
  }
  var prefillRevision = 0;
  function prefillSnapshot() {
    return JSON.stringify(Array.prototype.map.call(document.querySelectorAll("main input, main select, main textarea"),
      function (el) { return [el.name, el.value, el.checked]; }));
  }
  async function applyPrefillData(data) {
    var revision = ++prefillRevision, onlyEmpty = PF_ONLY_EMPTY, before = prefillSnapshot();
    // Other pages retain their existing fill contract in this migration slice.
    if (["diary_settings", "booking_details", "dependants"].indexOf(data.page) === -1) {
      applyValidatedPrefillData(data);
      return;
    }
    try {
      var response = await fetch("/concierge/settings-rows", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({page: data.page, values: data.values})
      });
      var checked = await response.json();
      if (revision !== prefillRevision) return;
      if (!response.ok) throw new Error(checked.detail || "Your saved settings could not be checked.");
      var problems = (checked.values || []).flatMap(function (v) { return v.problems || []; });
      if (!checked.ok) throw new Error(problems.join(" ") || "There are no settings to fill in.");
      if (prefillSnapshot() !== before) throw new Error("The page changed while the card was checked. Please review the card again.");
      PF_ONLY_EMPTY = onlyEmpty;
      try { applyValidatedPrefillData(Object.assign({}, data, {values: checked.values})); }
      finally { PF_ONLY_EMPTY = false; }
    } catch (err) {
      msgs.appendChild(bubbleEl("assistant", "Settings were not filled in: " + (err.message || "Please try again.")));
      setOpen(true);
    }
  }
  function pfFillNamedRows(rowsId, tplId, values, kind, mapping) {
    var rows = document.getElementById(rowsId), tpl = document.getElementById(tplId);
    if (!rows || !tpl || (PF_ONLY_EMPTY && rows.childElementCount)) return;
    var identity = mapping.name || mapping.label;
    values.filter(function (v) { return v.name === kind && v.fields; }).forEach(function (v) {
      var fields = v.fields, key = String(fields.name || fields.label).toLowerCase();
      var row = Array.prototype.find.call(rows.querySelectorAll(":scope > *"), function (r) {
        var input = r.querySelector('[name="' + identity + '"]');
        return input && input.value.trim().toLowerCase() === key;
      });
      if (!row) {
        rows.appendChild(tpl.content.cloneNode(true));
        row = rows.lastElementChild;
      }
      Object.keys(mapping).forEach(function (key) {
        if (v.provided && v.provided.indexOf(key) === -1) return;
        if (!Object.prototype.hasOwnProperty.call(fields, key)) return;
        var value = fields[key];
        if (key === "days") {
          value = value.map(function (day) { return ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].indexOf(day); });
          row.querySelectorAll(".svc-day").forEach(function (button) {
            button.setAttribute("aria-pressed", value.indexOf(Number(button.getAttribute("data-day"))) !== -1 ? "true" : "false");
          });
        }
        if (key === "kind") value = value === "document" ? "1" : "0";
        if (Array.isArray(value)) value = value.join(key === "services" ? "|" : ",");
        if (typeof value === "boolean") value = value ? "1" : "0";
        pfSetField(row, mapping[key], value == null ? "" : String(value));
      });
    });
  }
  function applyValidatedPrefillData(data) {

    var values = (data.values || []).filter(function (v) { return v && v.name && v.value != null; });
    var form = null;
    if (data.page === "diary_settings") {
      form = document.getElementById("scheduling");
      if (!form) return;
      var gates = {};
      values.forEach(function (v) {
        if (v.name === "service" || v.name === "resource") return;
        if (v.name === "stay_mode") {
          var modeForm = document.querySelector('form[action="/app/diary/settings/booking-mode"]');
          if (modeForm) pfSetField(modeForm, "booking_mode", /^(1|true|yes)$/i.test(String(v.value)) ? "stay" : "appointment");
          return;
        }
        // blackout_dates is one-per-line; accept a comma-separated value too.
        var wrote = pfSetField(form, v.name, v.name === "blackout_dates" ? v.value.replace(/,\s*/g, "\n") : v.value);
        if (wrote && PF_GATES[v.name]) gates[PF_GATES[v.name]] = true;
      });
      pfOpenGates(form, gates);
      pfFillNamedRows("rt-rows", "rt-template", values, "resource", {
        name: "resource_name", count: "resource_count", rate: "resource_rate", rate_unit: "resource_rate_unit"
      });
      pfFillNamedRows("svc-rows", "svc-template", values, "service", {
        name: "service_name", minutes: "service_minutes", price: "service_price", timing: "service_timing",
        resource: "service_resource", days: "service_days", at_once: "service_at_once"
      });
      pfFillServiceRules(pluck(values, "service_urgent"), pluck(values, "service_out_of_hours"),
                         pluck(values, "service_after_job"));
      // Recommended values often live under the collapsed Advanced panel — open it
      // so "review each setting" doesn't hide half of them.
      var adv = form.querySelector("details");
      if (adv) adv.open = true;
    } else if (data.page === "booking_details") {
      form = document.querySelector('form[action="/app/diary/intake"]');
      if (!form) return;
      values.forEach(function (v) {
        // Old prefill cards can still be opened after the control moved.
        if (v.name === "stay_mode") {
          var modeForm = document.querySelector('form[action="/app/diary/settings/booking-mode"]');
          if (modeForm) pfSetField(modeForm, "booking_mode", /^(1|true|yes)$/i.test(String(v.value)) ? "stay" : "appointment");
        }
      });
      pfFillNamedRows("rows", "row-template", values, "field", {
        label: "label", required: "required", hint: "hint", services: "services", remember: "remember", kind: "doc"
      });
      // The intake page's live preview listens on #rows input events — already
      // fired per field by pfFillRows, so it repaints itself.
    } else if (data.page === "crm_fields") {
      // Customer fields (4b nit): "Label | type | expires N | hint" rows — the
      // Fields page adds one per submit, so post each in turn, then reload.
      var addForm = [].slice.call(document.querySelectorAll('form[action="/app/crm/fields"]')).filter(function (f) {
        var e = f.querySelector('input[name="entity"]'); return e && e.value === "contact";
      })[0];
      if (!addForm) return;
      var tok = addForm.querySelector('input[name="csrf_token"]').value;
      var rows = pluck(values, "contact_field").slice(0, 10).map(function (v) {
        var p = v.split("|").map(function (s) { return s.trim(); });
        var type = (p[1] || "text").toLowerCase();
        // "dropdown: Hereford, Worcester" carries its options (run #8: a dropdown
        // with none was refused by the route and vanished from the page).
        var optm = (p[1] || "").match(/^(?:drop\s*-?down|one of|choice)\s*[:(]\s*(.+?)\)?\s*$/i);   // the raw part keeps the options' case
        var options = optm ? optm[1].split(/,|;|\//).map(function (s) { return s.trim(); }).filter(Boolean) : [];
        type = /number/.test(type) ? "number" : /date/.test(type) ? "date" : /drop|one of|choice/.test(type) ? "dropdown" : /yes|bool/.test(type) ? "bool" : "text";
        if (type === "dropdown" && options.length < 2) type = "text";
        var exp = (p[2] || "").match(/expires?\s*(\d+)?/i);
        var val = (p[2] || "").match(/valid\s*(\d+)/i);          // "expires 60 valid 24" — months (run #8)
        return {label: p[0], field_type: exp ? "date" : type, durability: exp ? "expires" : "once",
                recall_days: exp && exp[1] ? exp[1] : "", valid_months: val ? val[1] : "",
                options: options.join(","), extraction_hint: p[3] || ""};
      }).filter(function (r) { return r.label; });
      // Each POST answers with a redirect carrying ?msg=saved or ?err=…; the
      // outcomes are read off the final URL and the page opens with them —
      // a refused row (a level beside a ladder, a duplicate) used to vanish
      // without a word (run #8, live 2026-09-05).
      var added = 0, errs = [];
      (function next(i) {
        if (i >= rows.length) {
          var qs = new URLSearchParams();
          if (added) { qs.set("msg", "saved"); qs.set("added", String(added)); }
          if (errs.length) qs.set("err", errs.join(" · "));
          window.location.href = "/app/crm/fields" + (qs.toString() ? "?" + qs.toString() : "");
          return;
        }
        var body = new URLSearchParams({csrf_token: tok, entity: "contact"});
        Object.keys(rows[i]).forEach(function (k) { body.append(k, rows[i][k]); });
        fetch("/app/crm/fields", {method: "POST", body: body}).then(function (res) {
          var q = new URL(res.url, window.location.origin).searchParams;
          if (q.get("msg") === "saved") added += 1;
          else errs.push(q.get("err") || (rows[i].label + ": not added"));
        }).catch(function () { errs.push(rows[i].label + ": not added"); }).finally(function () { next(i + 1); });
      })(0);
    } else if (data.page === "packages") {
      // Packages (src/app/wallet.py): "Name | quantity | session/hour | price |
      // valid days | services…" rows onto the Invoice settings' Packages face.
      form = document.querySelector('form[action="/app/invoices/settings"]');
      var rowsEl = document.getElementById("pkg-rows"), addPkg = document.getElementById("pkg-add");
      if (!form || !rowsEl || !addPkg) return;
      rowsEl.innerHTML = "";
      pluck(values, "package").slice(0, 12).forEach(function (v) {
        var p = v.split("|").map(function (s) { return s.trim(); });
        if (!p[0]) return;
        addPkg.click();
        var row = rowsEl.lastElementChild;
        row.querySelector('[name="pkg_name"]').value = p[0];
        row.querySelector('[name="pkg_quantity"]').value = p[1] || "10";
        row.querySelector('[name="pkg_unit"]').value = /hour/i.test(p[2] || "") ? "hour" : "session";
        row.querySelector('[name="pkg_price"]').value = (p[3] || "").replace(/[^0-9.]/g, "");
        row.querySelector('[name="pkg_valid_months"]').value = (p[4] || "0").replace(/[^0-9]/g, "") || "0";
        row.querySelector('[name="pkg_services"]').value = p[5] || "";
      });
      ["packages_paid_only", "package_low_at", "package_expiry_days"].forEach(function (k) {
        var v = pluck(values, k)[0], el = form.querySelector('[name="' + k + '"]');
        if (el && v !== undefined) el.value = v;
      });
      var det = document.getElementById("packages"); if (det) det.open = true;
    } else if (data.page === "invoice_settings") {
      // Held facts only (invoice details are never recommended): deposit %,
      // VAT, terms, quote validity into the page's own inputs.
      form = document.querySelector('form[action="/app/invoices/settings"]');
      if (!form) return;
      values.forEach(function (v) { pfSetField(form, v.name, v.value); });
    } else if (data.page === "dependants") {
      // "Kind | plural | one or several" then "Kind | Label | required | once |
      // gate | hint": the editor's own add buttons build the rows, so the
      // page's script owns the shape and this only types into it.
      form = document.getElementById("dep-form");
      if (!form) return;
      if (PF_ONLY_EMPTY && document.getElementById("groups").children.length) return;
      var groupsEl = document.getElementById("groups");
      var addGroup = document.getElementById("add-group");
      var byKind = {};
      groupsEl.querySelectorAll(".dep-group").forEach(function (g) {
        var label = g.querySelector('[name="g_label"]');
        if (label && label.value.trim()) byKind[label.value.trim().toLowerCase()] = g;
      });
      values.filter(function (v) { return v.name === "group" && v.fields; }).forEach(function (v) {
        var kd = v.fields, g = byKind[kd.label.toLowerCase()];
        if (!g) {
          groupsEl.appendChild(document.getElementById("group-template").content.cloneNode(true));
          g = groupsEl.lastElementChild;
        }
        pfSetField(g, "g_label", kd.label);
        pfSetField(g, "g_plural", kd.plural);
        pfSetField(g, "g_cardinality", kd.cardinality);
        byKind[kd.label.toLowerCase()] = g;
      });
      values.filter(function (v) { return v.name === "field" && v.fields; }).forEach(function (v) {
        var f = v.fields, g = byKind[f.kind.toLowerCase()];
        if (!g) throw new Error("The subject kind is no longer on this page.");
        var row = Array.prototype.find.call(g.querySelectorAll(".dep-fields > *"), function (r) {
          var label = r.querySelector('[name="f_label"]');
          return label && label.value.trim().toLowerCase() === f.label.toLowerCase();
        });
        if (!row) { g.querySelector(".add-field").click(); row = g.querySelector(".dep-fields").lastElementChild; }
        pfSetField(row, "f_label", f.label);
        pfSetField(row, "f_type", f.type);
        pfSetField(row, "f_options", (f.options || []).join(", "));
        pfSetField(row, "f_required", f.required ? "1" : "0");
        row.querySelector(".f-required").checked = f.required;
        pfSetField(row, "f_durability", f.durability);
        if (f.recall_days != null) pfSetField(row, "f_recall", String(f.recall_days));
        var rw = row.querySelector(".f-recall-wrap");
        if (rw) rw.hidden = f.durability !== "expires";
        pfSetField(row, "f_gate", f.gate ? "1" : "0");
        row.querySelector(".f-gate").checked = f.gate;
        if (!v.provided || v.provided.indexOf("hint") !== -1) pfSetField(row, "f_hint", f.hint);
      });
      groupsEl.querySelectorAll(".dep-group").forEach(function (g, gi) {
        g.querySelectorAll('input[name="f_group"]').forEach(function (h) { h.value = gi; });
      });
      if (addGroup && groupsEl.children.length >= 3) addGroup.hidden = true;
      // Progress ladders (slice 1, 2026-09-05): "Who | Label | L1, L2, … |
      // approve/auto/off | Service = level; …" → the Progress section's rows.
      var ladEl = document.getElementById("ladders"), addLadder = document.getElementById("add-ladder");
      if (ladEl && addLadder) {
        ladEl.innerHTML = "";
        pluck(values, "ladder").slice(0, 3).forEach(function (v) {
          var p = v.split("|").map(function (s) { return s.trim(); });
          if (!p[1] || !p[2]) return;
          ladEl.appendChild(document.getElementById("ladder-template").content.cloneNode(true));
          var row = ladEl.lastElementChild;
          var who = row.querySelector('[name="l_who"]');
          var wanted = (p[0] || "customer").toLowerCase();
          var opt = Array.prototype.find.call(who.options, function (o) {
            return o.value.toLowerCase() === wanted || o.textContent.toLowerCase().indexOf(wanted) !== -1;
          });
          who.value = opt ? opt.value : "contact";
          row.querySelector('[name="l_label"]').value = p[1];
          row.querySelector('[name="l_levels"]').value = p[2];
          var n = (p[3] || "approve").toLowerCase();
          row.querySelector('[name="l_notify"]').value = /auto/.test(n) ? "auto" : (/off|no|record/.test(n) ? "off" : "approve");
          row.querySelector('[name="l_gates"]').value = (p[4] || "").split(";").map(function (s) { return s.trim(); }).filter(Boolean).join("\n");
        });
        addLadder.hidden = ladEl.children.length >= 3;
      }
    } else {
      if (!data.held) { try { sessionStorage.removeItem(KEY_PREFILL); } catch (e) {} }
      return;
    }
    if (!data.held) { try { sessionStorage.removeItem(KEY_PREFILL); } catch (e) {} }
    pfBanner(form, !!data.held);
    if (data.held) {
      form.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    // A card just filled the page: the page's own Save is the write, and it
    // sat behind the panel (run log F5, plumber take 2026-09-25 — the next
    // step only appeared once Save was found). Drop the panel to its bubble,
    // bring Save into view and make it hard to miss.
    try { if (!panel.classList.contains("hidden")) setOpen(false); } catch (e) {}
    var save = pfSaveButton(form);
    if (!save) { form.scrollIntoView({ behavior: "smooth", block: "start" }); return; }
    setTimeout(function () {
      save.scrollIntoView({ behavior: "smooth", block: "center" });
      var on = true, n = 0;
      var t = setInterval(function () {
        save.style.boxShadow = on ? "0 0 0 4px rgba(29,79,215,.35)" : "";
        on = !on;
        if (++n >= 8) { clearInterval(t); save.style.boxShadow = ""; }
      }, 450);
    }, 350);
  }
  // The form's own Save: the save row's button, else its last submit button.
  function pfSaveButton(form) {
    var row = form.querySelector("[data-save-row] button");
    if (row) return row;
    var subs = form.querySelectorAll('button[type="submit"], button:not([type])');
    return subs.length ? subs[subs.length - 1] : null;
  }
  applySettingsPrefill();

  // ── Hero fix-mode demo hooks (landing page) ─────────────────────────────────
  // Just enough primitives for the landing page's scripted thumbs-down demo to
  // run a canned exchange inside the real panel (see landing.html #hero-fixdemo).
  // Demo bubbles are ephemeral: nothing here writes the persisted thread log or
  // calls the server, and a Clear/reopen simply re-renders the real thread.
  window.blam8ConciergeDemo = {
    open: function () { setOpen(true); },
    title: function (t) { if (titleEl && t) { titleEl.textContent = t; } },
    assistant: function (text) { msgs.appendChild(bubbleEl("assistant", text)); scrollDown(); },
    user: function (text) { msgs.appendChild(bubbleEl("user", text)); scrollDown(); },
    node: function (el) { msgs.appendChild(el); scrollDown(); },
    typing: function (on) { if (on) { showTyping(); } else { hideTyping(); } },
    cta: showCTA,
  };

  // The page's Next/Continue with knowledge cards still unsaved: save them
  // first, then let the click through (LittleDogs P1: Finish lost the one
  // fact the agent most needed). Capture phase — before the form submits.
  var savingPending = false;
  document.addEventListener("click", function (e) {
    if (!ONB_MODE || !PENDING_KB_CARDS.length) return;
    var btn = e.target && e.target.closest ? e.target.closest("[data-onb-continue]") : null;
    if (!btn || savingPending) return;
    e.preventDefault(); e.stopImmediatePropagation();
    savingPending = true;
    var pending = PENDING_KB_CARDS.slice();
    (async function () {
      for (var i = 0; i < pending.length; i++) {
        try { await pending[i].save(); } catch (err) { /* the card shows its own error */ }
      }
      savingPending = false;
      if (!PENDING_KB_CARDS.length) btn.click();   // nothing pending now → the real Next
    })();
  }, true);

  // Start from actual stored drafts when they arrive. Keep browser integration
  // outside the onboarding-turn function and its standalone replay boundary.
  window.blam8KnowledgeGuideStart = async function () {
    if (!ONB_MODE || ONB_STEP !== "knowledge" || sending) return false;
    sending = true; sendBtn.disabled = true; showTyping();
    if(deviceSync)deviceSync.automatic=true;
    try { return await onboardTurn("", []); }
    finally { if(deviceSync)deviceSync.automatic=false; sending = false; sendBtn.disabled = false; }
  };

  // Restore open state across navigation. On a setup step, open by default so the
  // co-pilot is right there — unless the user explicitly closed it this session.
  if (IN_APP && window.Blam8SetupCards) {
    var setupFormGuard = new window.Blam8SetupCards.FormGuard(root.getAttribute("data-setup-scope") || "");
    setupCards = new window.Blam8SetupCards.Panel({ messages: msgs, form: form,
      onApplying: function (card) { setupFormGuard.notify(card); },
      onApplied: function () { fetchSetupStatus(); } });
  }

  var openFlag = sessionStorage.getItem(KEY_OPEN);
  if (openFlag === "1" || (ONB_MODE && openFlag !== "0")) setOpen(true);

  // Held for later, on the wizard's knowledge step: rules and facts stated at
  // earlier steps arrive as a ready note card — saved with the owner's click,
  // exactly like the interview's cards. After setOpen: renderAll() rebuilds
  // the thread and would wipe a card appended before it.
  (function maybeHeldNotesCard() {
    if (!ONB_MODE || ONB_STEP !== "knowledge") return;
    var el = document.getElementById("held-notes");
    if (!el) return;
    var values;
    try { values = JSON.parse(el.getAttribute("data-onb-held-notes") || "[]"); } catch (e) { return; }
    if (!Array.isArray(values) || !values.length) return;
    appendRecommendCard({ page: "knowledge", label: "knowledge notes — from what you told me earlier",
                          href: "/app/knowledge", values: values });
  })();

  // A step hand-off reads like a NEW thread (Julius): pin the just-appended
  // step message to the TOP of the view — history sits above, one scroll up.
  // rAF so the panel's layout has settled before measuring.
  if (HANDOFF) requestAnimationFrame(pinLastToTop);

  // Availability is application-owned, including arrival and manual changes.
  if (ONB_MODE) {
    showContinueHint();
    document.addEventListener("input", function (event) { if (event.target.closest("main")) showContinueHint(); });
    document.addEventListener("change", function (event) { if (event.target.closest("main")) showContinueHint(); });
    window.addEventListener("blam8:onboarding-readiness", showContinueHint);
    var journeyMain = document.querySelector("main");
    if (journeyMain && typeof MutationObserver !== "undefined") {
      new MutationObserver(showContinueHint).observe(journeyMain, {subtree:true, childList:true,
        attributes:true, attributeFilter:["disabled", "hidden", "class"]});
    }
  }

  // Expire an idle thread even while the tab just sits open.
  if (deviceSync) deviceSync.register({
    busy: function () { return sending || !!FIX_LOG_ID || SCENARIO; },
    set: function (messages) {
      if (FIX_LOG_ID || SCENARIO || JSON.stringify(log) === JSON.stringify(messages)) return;
      log = messages.slice(-MAX_TURNS * 2);
      renderAll();
    }
  });
  setInterval(function () { if (isStale()) clearThread(); }, 60 * 1000);
})();
