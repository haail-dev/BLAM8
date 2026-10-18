// /architecture — renders the n8n-style canvas into the .arch section.
// External file on purpose: the CSP's script-src allows 'self', and this page
// must not depend on 'unsafe-inline'. All positioning goes through the CSSOM
// (el.style.*), which the CSP does not restrict — never setAttribute('style').
//
// The node/group data here is duplicated in src/app/architecture.py, which renders
// the crawlable text fallback beneath the canvas. tests/test_architecture_page.py
// asserts every label/sublabel in the Python copy appears in this file, so keep
// the two in step.
(function () {
  'use strict';

  const CX = 90, CY = 330, DX = 232, DY = 196, W = 100, HT = 100;
  const X = c => CX + c * DX, Y = r => CY + r * DY;

  const N = [
  // inbound
  ['a1',0,0,'trg','✉','Graph notification','constant-time clientState'],
  ['a2',0,1,'trg','✉','Gmail push','ACK 204, draft after'],
  ['a3',0,2,'trg','◷','Gmail poller','shares the push backoff'],
  ['a4',0,3,'trg','☎','Twilio voice + SMS','HMAC-SHA1 verified'],
  ['a5',0,4,'trg','✎','You press draft','never auto-sends'],
  // guards
  ['b1',1,0,'grd','⊘','No-reply / bounce','mailer-daemon · postmaster'],
  ['b2',1,1,'grd','£','Payment receipt','links invoice, stops'],
  ['b3',1,2,'grd','⊗','Ignored contact','dropped entirely'],
  ['b4',2,0,'grd','⟳','Self-loop guard','own + sibling mailboxes'],
  ['b5',2,1,'grd','⇩','Suppression','3 ignored drafts → stop'],
  ['b6',2,2,'grd','◔','Trial gate','stops before the spend'],
  // context
  ['c1',3,0,'','⊞','Effective config','tenant ⊕ mailbox'],
  ['c2',3,1,'','☰','Knowledge retrieval','Voyage · pgvector'],
  ['c3',3,2,'','≡','Thread history','provider thread scoped'],
  ['c4',3,3,'dia','▦','Their bookings','reference, don’t re-offer'],
  ['c5',4,0,'crm','◈','CRM context','deals · open promises'],
  ['c6',4,1,'crm','◎','Portal activity','what they did on your site'],
  ['c7',4,2,'','▤','Stock + purchases','storefront tenants'],
  ['c8',4,3,'grd','⚠','Credit limit','arithmetic, not vibes'],
  ['c9',5,0,'','✧','Industry vocabulary','config, not code'],
  ['c10',5,1,'','⚒','Tool injection','active modules only'],
  // draft
  ['d1',6,0,'ai','✦','Structured output','schema-enforced'],
  ['d2',6,1,'ai','⚒','Tool loop','diary · handoff · shop'],
  ['d3',6,2,'ai','⟲','Corrective retry','one, then it gives up'],
  ['d4',6,3,'grd','⊘','Say-vs-do guard','“booked” with no hold'],
  ['d5',7,0,'grd','⚑','Knowledge gap','admits it, flags it'],
  ['d6',7,1,'grd','∅','Empty after retry','queues with no text'],
  ['d7',7,2,'','¶','Markdown stripped','plain text out'],
  // label
  ['e1',8,0,'ai','◉','Model category','needs reply · fyi · auto'],
  ['e2',8,1,'','⊙','Computed override','escalation wins'],
  ['e3',8,2,'','⊝','Informational → skipped','no queue, no push'],
  ['e4',8,3,'','⇠','Legacy derived','at read time'],
  // diary
  ['f1',9,0,'dia','▦','15-minute grid','96 blocks a day','source of truth','dia'],
  ['f2',9,1,'dia','◫','Window modes','all-day · am/pm · slots'],
  ['f3',9,2,'dia','✓','Required intake','no hold until answered'],
  ['f4',9,3,'dia','⊡','Atomic capacity RPC','six variants, Postgres-side'],
  ['f5',9,4,'dia','◷','Hold placed','proposed, linked to draft'],
  ['f6',10,0,'dia','⊗','Duplicate webhook','this run’s holds cancelled'],
  ['f7',10,1,'dia','⇢','Offered slots','first to accept wins'],
  ['f8',10,2,'dia','◔','Pre-expiry nudge','every 30 min'],
  ['f9',10,3,'dia','⊘','Expiry sweep','capacity comes back'],
  ['f10',10,4,'dia','#','Reference minted','Postgres, not the model'],
  // approval
  ['g1',11,0,'gate','➤','Send','as-is or edited','human','gate'],
  ['g2',11,1,'','⊘','Skip','releases the hold'],
  ['g3',11,2,'','⟲','Redraft',''],
  ['g4',12,0,'','✎','Correct','teaches the chunk'],
  ['g5',12,1,'','⚑','Flag a gap','files it against the KB'],
  ['g6',12,2,'','☰','Grounded-on','shows its sources'],
  // auto-send
  ['h1',13,0,'','⚡','Two axes required','config AND caller'],
  ['h2',13,1,'','⊟','Tri-state per mailbox','orders@ auto, complaints@ never'],
  ['h3',13,2,'grd','⚠','Escalation keyword','forces the queue'],
  ['h4',14,0,'grd','☹','Angry sentiment','held, Core and up'],
  ['h5',14,1,'grd','⊗','Dup · sibling · credit','held'],
  ['h6',14,2,'','§','EU AI Act footer','auto-sent only'],
  // after
  ['i1',15,0,'dia','✓','Holds confirmed','proposed → booked'],
  ['i2',15,1,'dia','⇄','Calendar push','only its own events'],
  ['i3',15,2,'crm','◈','CRM ingest, zero LLM','every tier, even Starter'],
  ['i4',16,0,'crm','◈','Deals clustered','one Haiku call'],
  ['i5',16,1,'crm','◈','Promises both ways','yours and theirs'],
  // quote to cash
  ['m1',17,0,'','£','Quote auto-drafts','from the priced reply'],
  ['m2',17,1,'','◫','Public accept page','accept · decline · ask'],
  ['m3',17,2,'','◔','Chase job','day 3, day 7, expiry'],
  ['m4',18,0,'','▤','Invoice + PDF','deposit netted off'],
  ['m5',18,1,'','⇥','Xero / QuickBooks','integrate, don’t compete'],
  // money
  ['n1',19,0,'mon','£','Subscriptions','platform · hosted Checkout'],
  ['n2',19,1,'mon','⇄','They get paid','direct charge, their account'],
  ['n3',19,2,'mon','⚠','Booking deposits','platform account','known gap','warn'],
  ['n4',20,0,'mon','%','Commission ledger','earned = collected'],
  ['n5',20,1,'mon','⊖','Refund clawback','automatic'],
  ['n6',20,2,'mon','➤','Partner payout','bank transfer, not Stripe'],
  // logs
  ['o1',21,0,'','▤','Every model call','prompt + tool rounds'],
  ['o2',21,1,'','◉','Execution trace','proposals · retrievals'],
  ['o3',21,2,'','≡','Your agent log','tenant-facing narrative'],
  ['o4',21,3,'','£','Cost metering','per call, exportable'],
  ['o5',21,4,'','⊗','Retention purge','90 days, 03:30'],
  // lower deck — portal
  ['j1',3,6,'trg','◎','Portal at /c/you','public, no login'],
  ['j2',3,7,'','⌕','Identity','ref code, or name + email'],
  ['j3',4,6,'ai','✦','Same brain, their side','model never sets tenant'],
  ['j4',4,7,'','⇌','Reschedule mode','apply now, or ask you'],
  // concierge
  ['k1',6,6,'ai','✦','Pre-login Q&A','rate-limited, stateless'],
  ['k2',6,7,'ai','✦','Onboarding co-pilot',''],
  ['k3',7,6,'ai','✦','Setup help',''],
  ['k4',7,7,'ai','✦','Diagnose and repair',''],
  ['k5',8,6,'ai','✦','Business questions','fixed data catalogue'],
  ['k6',8,7,'','⊙','Proposal re-validated','ids owned, windows fit'],
  ['k7',9,6,'gate','✓','Single-use card','five-minute expiry','human','gate'],
  ['k8',9,7,'','▤','Support ticket','secrets redacted'],
  // personas
  ['l1',11,6,'ai','⇄','Transport specialist','ask_transport handoff'],
  ['l2',11,7,'ai','⚒','Engineer desk','the trades twin'],
  ['l3',12,6,'ai','◷','The 4pm planner','proposes consolidations'],
  ['l4',12,7,'','⚡','Planner self-apply','a safe class, if you allow'],
  // dark
  ['p1',15,6,'off','☎','Missed call → text','built, being tested','dark','off'],
  ['p2',15,7,'off','✉','Gmail','100 sign-ups till verified','capped','off'],
  ['p3',16,6,'off','%','Partner credit','off behind a flag','off','off'],
  // not built
  ['q1',18,6,'off','◐','Morning debrief','no code — jobs are global UTC','planned','off'],
  ['q2',18,7,'off','✈','WhatsApp · Telegram','no code, no table','planned','off'],
  ['q3',19,6,'off','⟲','Auto-enrol on triggers','metadata, no engine','planned','off'],
  ['q4',19,7,'off','⌕','Gone-quiet outreach','a badge, not a sweep','planned','off'],
  ];

  const GROUPS = [
   ['Inbound','four ways in',['a1','a5']],
   ['Guards','before a single token is spent',['b1','b6']],
   ['Context','what it is allowed to know',['c1','c10']],
   ['The draft','one call, four refusals',['d1','d7']],
   ['Label','computed truth overrides the model',['e1','e4']],
   ['Diary and holds','the only thing that can say yes',['f1','f10']],
   ['Approval','the gate',['g1','g6']],
   ['Auto-send','off by default',['h1','h6']],
   ['After it goes','the part that fills itself in',['i1','i5']],
   ['Quote to cash','a second money spine',['m1','m5']],
   ['Money','three rails, three accounts',['n1','n6']],
   ['Seeing what it did','four logs',['o1','o5']],
   ['Your customer’s side','no login',['j1','j4']],
   ['Concierge','five modes, one engine',['k1','k8']],
   ['Three more personas','handoff, keeps your voice',['l1','l4']],
   ['Built, but dark','waiting on someone else',['p1','p3']],
   ['Not built','shown because hiding it would be lying',['q1','q4']],
  ];

  const E = [
   ['a1','b1'],['a2','b1'],['a3','b1'],['a4','b1'],['a5','b1'],
   ['b1','b2'],['b2','b3'],['b3','b4'],['b4','b5'],['b5','b6'],['b6','c1'],
   ['c1','c2'],['c2','c3'],['c3','c4'],['c4','c5'],['c5','c6'],['c6','c7'],
   ['c7','c8'],['c8','c9'],['c9','c10'],['c10','d1'],
   ['d1','d2'],['d2','d3'],['d3','d4'],['d3','d5'],['d3','d6'],['d6','d7'],['d7','e1'],
   ['e1','e2'],['e2','e3'],['e2','e4'],
   ['d2','f1'],['f1','f2'],['f2','f3'],['f3','f4'],['f4','f5'],
   ['f5','f6'],['f5','f7'],['f7','f8'],['f8','f9'],['f4','f10'],
   ['e2','g1'],['e2','g2'],['e2','g3'],['e2','g4'],['e2','g5'],['e2','g6'],['f5','g1'],
   ['g1','h1'],['h1','h2'],['h2','h3'],['h2','h4'],['h2','h5'],['h2','h6'],
   ['h6','i1'],['i1','i2'],['i2','i3'],['i3','i4'],['i4','i5'],
   ['i5','m1'],['m1','m2'],['m2','m3'],['m2','m4'],['m4','m5'],
   ['m4','n2'],['n2','n3'],['n1','n4'],['n4','n5'],['n5','n6'],
   ['h6','o1'],['o1','o2'],['o2','o3'],['o3','o4'],['o4','o5'],
   ['j1','j2'],['j2','j3'],['j3','j4'],
   ['k1','k6'],['k2','k6'],['k3','k6'],['k4','k6'],['k5','k6'],['k6','k7'],['k5','k8'],
   ['d2','l1'],['d2','l2'],['l3','l4'],
  ];
  // purple feedback, routed above the deck
  const FB = [['g4','c2',150],['g5','c2',120],['i5','c5',186],['i3','c5',216],
   ['i2','c4',246],['i1','b5',60],['j3','c6',90],['f7','f9',276]];
  // teal — everything books through one capacity check
  const CR = [['j3','f4',30],['k6','f4',0],['l3','f4',-30]];

  const NOTES = [
   [X(11)-30,Y(4)+40,420,`<b>The AI proposes. The diary decides.</b>Every hold goes through a Postgres
     capacity check, whatever the email says. Inbox, portal, concierge and the planner all call the
     same six RPCs — not four implementations that drift apart.`],
   [X(17)-30,Y(3)+10,400,`<b>Why this isn’t a canvas.</b>Every node here is a function with tests
     around it, in one deployment. Thousands of them, across one codebase. A new customer is a row behind
     row-level security — not a duplicated workflow, not another forty credentials to rotate.`],
   [X(1)-30,Y(6)+10,420,`<b>The cheapest reply is the one you never generate.</b>Six guards run before
     a single token is spent. Suppression stops drafting entirely after three ignored drafts, and the
     trial gate stops before the spend rather than billing you for it.`],
  ];

  const vp = document.getElementById('arch-vp');
  const cv = document.getElementById('arch-cv');
  const sv = document.getElementById('arch-sv');
  if (!vp || !cv || !sv) return;

  const by = {};
  N.forEach(([id, c, r, k, ic, t, s, pill, pk]) => {
    const o = { id, x: X(c), y: Y(r) };
    by[id] = o;
    const el = document.createElement('div');
    el.className = 'nd' + (k ? ' ' + k : '');
    el.style.left = o.x + 'px';
    el.style.top = o.y + 'px';
    el.style.width = W + 'px';
    el.innerHTML = `<div class="tile">${pill ? `<span class="pill ${pk || ''}">${pill}</span>` : ''}
      <span class="port l"></span>${ic}<span class="port r"></span></div>
      <div class="lbl"><div class="t">${t}</div>${s ? `<div class="s">${s}</div>` : ''}</div>`;
    cv.appendChild(el);
  });

  GROUPS.forEach(([t, s, [a, b]]) => {
    const ids = Object.keys(by), i0 = ids.indexOf(a), i1 = ids.indexOf(b);
    const mem = ids.slice(i0, i1 + 1).map(k => by[k]);
    const x0 = Math.min(...mem.map(m => m.x)) - 30, x1 = Math.max(...mem.map(m => m.x)) + W + 30;
    const y0 = Math.min(...mem.map(m => m.y)) - 44, y1 = Math.max(...mem.map(m => m.y)) + HT + 58;
    const d = document.createElement('div');
    d.className = 'grp';
    d.style.left = x0 + 'px';
    d.style.top = y0 + 'px';
    d.style.width = (x1 - x0) + 'px';
    d.style.height = (y1 - y0) + 'px';
    d.innerHTML = `<b>${t}</b><i>${s}</i>`;
    cv.appendChild(d);
  });

  function draw(d, col, dash) {
    const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    p.setAttribute('d', d);
    p.setAttribute('fill', 'none');
    p.setAttribute('stroke', col);
    p.setAttribute('stroke-width', 1.7);
    if (dash) p.setAttribute('stroke-dasharray', '7 5');
    else p.setAttribute('marker-end', 'url(#arch-arrow)');
    sv.appendChild(p);
  }

  E.forEach(([f, t]) => {
    const a = by[f], b = by[t];
    const x1 = a.x + W, y1 = a.y + HT / 2, x2 = b.x, y2 = b.y + HT / 2;
    const k = Math.max(70, (x2 - x1) * .5);
    draw(`M${x1},${y1} C${x1 + k},${y1} ${x2 - k},${y2} ${x2},${y2}`, '#4a6d9c');
  });

  FB.forEach(([f, t, up]) => {
    const a = by[f], b = by[t], ty = CY - 90 - up;
    draw(`M${a.x + W / 2},${a.y} C${a.x + W / 2},${ty} ${b.x + W / 2},${ty} ${b.x + W / 2},${b.y}`, '#8b6bd6', 1);
  });
  CR.forEach(([f, t, dn]) => {
    const a = by[f], b = by[t], ty = CY + 5 * DY + 60 + dn;
    draw(`M${a.x + W / 2},${a.y + HT} C${a.x + W / 2},${ty} ${b.x + W / 2},${ty} ${b.x + W / 2},${b.y + HT}`, '#3f8f97', 1);
  });

  // The yellow run: one inbound email's journey through the machine, drawn on
  // top of the flow edges it travels — every hop is an existing edge in E, so
  // the yellow beziers land exactly on the blue ones. Marching dashes (CSS
  // animation on .run) plus a yellow arrowhead per hop show the direction.
  const RUN = ['a1','b1','b2','b3','b4','b5','b6',
    'c1','c2','c3','c4','c5','c6','c7','c8','c9','c10',
    'd1','d2','f1','f2','f3','f4','f5','g1',
    'h1','h2','h6','i1','i2','i3','i4','i5','m1','m2','m4','m5'];
  for (let i = 0; i < RUN.length - 1; i++) {
    const a = by[RUN[i]], b = by[RUN[i + 1]];
    const x1 = a.x + W, y1 = a.y + HT / 2, x2 = b.x, y2 = b.y + HT / 2;
    const k = Math.max(70, (x2 - x1) * .5);
    const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    p.setAttribute('d', `M${x1},${y1} C${x1 + k},${y1} ${x2 - k},${y2} ${x2},${y2}`);
    p.setAttribute('class', 'run');
    // Screen-space stroke: the journey stays a bold 2.4px thread even at the
    // 17% fit view, where a scaled stroke would vanish to half a pixel.
    p.setAttribute('vector-effect', 'non-scaling-stroke');
    p.setAttribute('marker-end', 'url(#arch-arrow-run)');
    sv.appendChild(p);
  }

  NOTES.forEach(([x, y, w, h]) => {
    const d = document.createElement('div');
    d.className = 'sn';
    d.style.left = x + 'px';
    d.style.top = y + 'px';
    d.style.width = w + 'px';
    d.innerHTML = h;
    cv.appendChild(d);
  });

  const CWD = X(21) + W + 260, CHT = Y(7) + HT + 180;
  sv.setAttribute('width', CWD);
  sv.setAttribute('height', CHT);
  cv.style.width = CWD + 'px';
  cv.style.height = CHT + 'px';

  // ── pan / zoom ────────────────────────────────────────────────────────────
  const pc = document.getElementById('arch-pc');
  let sc = 1, tx = 0, ty = 0;
  const ap = () => {
    cv.style.transform = `translate(${tx}px,${ty}px) scale(${sc})`;
    pc.textContent = Math.round(sc * 100) + '%';
  };
  function fit() {
    const p = 44;
    sc = Math.min((vp.clientWidth - p * 2) / CWD, (vp.clientHeight - p * 2) / CHT);
    tx = (vp.clientWidth - CWD * sc) / 2;
    ty = (vp.clientHeight - CHT * sc) / 2;
    ap();
  }
  function za(cx, cy, f) {
    const n = Math.min(3, Math.max(.05, sc * f));
    tx = cx - (cx - tx) * (n / sc);
    ty = cy - (cy - ty) * (n / sc);
    sc = n;
    ap();
  }
  document.getElementById('arch-fit').addEventListener('click', fit);
  document.getElementById('arch-zi').addEventListener('click',
    () => za(vp.clientWidth / 2, vp.clientHeight / 2, 1.25));
  document.getElementById('arch-zo').addEventListener('click',
    () => za(vp.clientWidth / 2, vp.clientHeight / 2, .8));

  vp.addEventListener('wheel', e => {
    e.preventDefault();
    const r = vp.getBoundingClientRect();
    za(e.clientX - r.left, e.clientY - r.top, e.deltaY < 0 ? 1.12 : .89);
  }, { passive: false });

  // Pointer pan, plus two-pointer pinch zoom (touch-action:none on the
  // viewport hands us the raw touches; the rest of the page still scrolls).
  const ptrs = new Map();
  let dg = null, pinch = null;
  vp.addEventListener('pointerdown', e => {
    ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
    vp.setPointerCapture(e.pointerId);
    if (ptrs.size === 2) {
      const [p1, p2] = [...ptrs.values()];
      pinch = { d: Math.hypot(p2.x - p1.x, p2.y - p1.y) };
      dg = null;
    } else if (ptrs.size === 1) {
      dg = { x: e.clientX - tx, y: e.clientY - ty };
      vp.classList.add('drag');
    }
  });
  vp.addEventListener('pointermove', e => {
    if (!ptrs.has(e.pointerId)) return;
    ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pinch && ptrs.size === 2) {
      const [p1, p2] = [...ptrs.values()];
      const d = Math.hypot(p2.x - p1.x, p2.y - p1.y);
      if (d > 0 && pinch.d > 0) {
        const r = vp.getBoundingClientRect();
        za((p1.x + p2.x) / 2 - r.left, (p1.y + p2.y) / 2 - r.top, d / pinch.d);
      }
      pinch.d = d;
    } else if (dg) {
      tx = e.clientX - dg.x;
      ty = e.clientY - dg.y;
      ap();
    }
  });
  ['pointerup', 'pointercancel'].forEach(t => vp.addEventListener(t, e => {
    ptrs.delete(e.pointerId);
    if (ptrs.size < 2) pinch = null;
    if (ptrs.size === 0) { dg = null; vp.classList.remove('drag'); }
  }));

  // Keyboard on the viewport itself (it's tabindex="0"), not the window —
  // arrow keys must keep scrolling the rest of the page normally.
  vp.addEventListener('keydown', e => {
    const s = 70;
    if (e.key === 'ArrowLeft') tx += s;
    else if (e.key === 'ArrowRight') tx -= s;
    else if (e.key === 'ArrowUp') ty += s;
    else if (e.key === 'ArrowDown') ty -= s;
    else if (e.key === '0') { fit(); e.preventDefault(); return; }
    else return;
    e.preventDefault();
    ap();
  });

  addEventListener('resize', fit);
  fit();
})();
