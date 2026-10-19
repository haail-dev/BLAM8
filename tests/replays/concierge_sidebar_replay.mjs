// The page's right-hand column (src/app/static/concierge_sidebar.js) against a tiny fake DOM.
import assert from 'node:assert/strict';
import { limitLines, ledgerLine, doneSummary, minutesWord } from '../../src/app/static/concierge_status.js';
import { mountSidebar, ageOf, waitingState, dots, DOTS_PER_ROW, DOTS_MAX, TASK_CAP, OPEN_CAP, CSS, WIDE_PX } from '../../src/app/static/concierge_sidebar.js';

class El {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.listeners = {}; this.className = ''; this.style = {}; this._text = ''; this.classList = { add: (c) => { this.className += ' ' + c; } }; }
  appendChild(c) { if (c.parentNode) c.remove(); c.parentNode = this; this.children.push(c); return c; }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((x) => x !== this); this.parentNode = null; }
  insertBefore(c, ref) { if (c.parentNode) c.remove(); c.parentNode = this; const i = ref ? this.children.indexOf(ref) : -1; if (i < 0) this.children.push(c); else this.children.splice(i, 0, c); return c; }
  get firstElementChild() { return this.children[0] || null; }
  get nextElementSibling() { return this.nextSibling; }
  get nextSibling() { const k = this.parentNode ? this.parentNode.children : []; return k[k.indexOf(this) + 1] || null; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] ?? null; }
  addEventListener(t, f) { (this.listeners[t] ||= []).push(f); }
  click() { (this.listeners.click || []).forEach((f) => f()); }
  set textContent(v) { this._text = v; if (v === '') { this.children.forEach((c) => { c.parentNode = null; }); this.children = []; } }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(' '); }
  all(pred, out = []) { this.children.forEach((c) => { if (pred(c)) out.push(c); c.all(pred, out); }); return out; }
}
const bodyClasses = new Set();
const doc = { createElement: (t) => new El(t), head: new El('head'), body: { classList: { toggle: (c, on) => (on ? bodyClasses.add(c) : bodyClasses.delete(c)) } } };
const store = new Map();
let statusReply = { open: { items: [], counts: {} }, today: { items: [], counts: {} }, limits: {}, loop: 'single' };
let aroundReply = { groups: [] };
const fetched = [];
globalThis.fetch = async (url) => { fetched.push(url); return { ok: true, json: async () => (url.includes('around') ? aroundReply : statusReply) }; };
globalThis.localStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) };

const byTitle = (node, title) => node.all((c) => c.className === 'cz-side-box').find((b) => b.children[0].children[0].textContent === title);
function setup({ tasks = [], waiting = [], ran = null, context = null } = {}) {
  const state = { tasks, waiting, reviewed: [], released: [], focused: [], fns: [], host: null };
  const api = { tasks: () => state.tasks, waiting: () => state.waiting, review: (w, inColumn) => { state.reviewed.push(w); state.inColumn = inColumn; },
                released: (node) => state.released.push(node), focusTask: (id) => state.focused.push(id), onChange: (fn) => state.fns.push(fn), host: (h) => { state.host = h; } };
  if (ran) api.activity = () => ({ busy: false, step: '', ran, links: [] });
  if (context) api.context = () => context;
  const root = new El('div');
  const sb = mountSidebar(root, api, doc);
  return { state, root, sb, boxes: () => ['Open now', 'Tasks', 'Waiting for you'].map((n) => byTitle(sb.node, n)), byTitle: (n) => byTitle(sb.node, n), rows: (b) => b.all((c) => c.className === 'cz-side-row') };
}

// 1. two boxes, both open by default, one quiet line each when empty
let t = setup();
assert.equal(t.boxes().length, 3);                              // Open now, Tasks, Waiting for you (+ the status boxes)
assert.deepEqual(t.boxes().map((b) => b.getAttribute('data-open')), ['true', 'true', 'true']);
assert.equal(t.byTitle('Limits').getAttribute('data-open'), 'false');   // the less central boxes start folded
assert.equal(t.boxes()[0].getAttribute('data-empty'), 'true');  // Open now shows nothing until a card arrives
assert.equal(t.byTitle('Tasks').getAttribute('data-empty'), 'true');   // nothing running: the box is not there (Kris 2026-10-16)
assert.match(t.boxes()[2].textContent, /Nothing waiting/);
assert.match(t.root.className, /cz-has-side/);

// 2. a box collapses, remembers it per device, and a fresh mount reads it back
t.boxes()[1].children[0].click();
assert.equal(t.boxes()[1].getAttribute('data-open'), 'false');
assert.equal(store.get('blam8.conciergeSide.tasks'), 'closed');
t = setup();
assert.deepEqual(t.boxes().map((b) => b.getAttribute('data-open')), ['true', 'false', 'true']);
t.boxes()[1].children[0].click();   // open again
assert.equal(store.get('blam8.conciergeSide.tasks'), 'open');

// 3. task rows from the panel's task data: newest first, capped, state word shown, click focuses the chat message
const now = Date.now();
const many = Array.from({ length: TASK_CAP + 3 }, (_, i) => ({ id: 't' + i, title: 'Task ' + i, status: i === 0 ? 'working' : 'done', at: now - i * 60000 }));
t = setup({ tasks: many });
let rows = t.rows(t.boxes()[1]);
assert.equal(rows.length, TASK_CAP);
assert.match(rows[0].textContent, /Task 0/); assert.match(rows[0].textContent, /working/);
assert.match(rows[1].textContent, /done/);
rows[2].click();
assert.deepEqual(t.state.focused, ['t2']);

// 4. waiting rows: kind, summary, state, age; a new item appears on change; Review goes to the panel, nothing else
const w = { proposal_id: 'p1', kind: 'send_email', summary: 'Send the quote to Dan', status: 'pending', created_at: new Date(now - 5 * 60000).toISOString() };
t = setup({ waiting: [w] });
rows = t.rows(t.boxes()[2]);
assert.equal(rows.length, 1);
assert.match(rows[0].textContent, /Send the quote to Dan/);
assert.doesNotMatch(rows[0].textContent, /send email|waiting|5 min ago|Review/);   // only its title (Kris 2026-10-16): the tap opens the card, which says the rest
rows[0].click();
assert.deepEqual(t.state.reviewed, [w]);                    // handed to the panel's own resurface path; no confirm call here
t.state.waiting = [w, { ...w, proposal_id: 'p2', summary: 'Second', status: 'superseded' }];
t.state.fns.forEach((f) => f());                              // the panel's 'cz:side' event
rows = t.rows(t.boxes()[2]);
assert.equal(rows.length, 2); assert.doesNotMatch(rows[1].textContent, /parked/);
const badge = t.boxes()[2].children[0].children[2];            // the count: small squares, not a number (after the title and the note)
assert.equal(badge.all((c) => c.className === 'cz-side-dot').length, 2);
assert.equal(badge.getAttribute('aria-label'), '2 waitings');

// 4b. a tapped waiting item opens IN the Waiting box (Kris 2026-10-16): the list steps aside, the card fills the box, Back puts it away
t = setup({ waiting: [w] });
const cardA = new El('div'); cardA.setAttribute('data-proposal-id', 'fa');
t.state.host.show(cardA, null, '', { focus: true });
t.state.host.show(cardA, null, '', { focus: true });                       // the same card again does nothing
let waitBox = t.byTitle('Waiting for you');
assert.equal(waitBox.getAttribute('data-focus'), 'true');
assert.equal(waitBox.all((c) => c === cardA).length, 1);
assert.equal(waitBox.all((c) => c.className === 'cz-side-back').length, 1);
assert.equal(t.byTitle('Open now').getAttribute('data-empty'), 'true');   // it did not join Open now
const backA = waitBox.all((c) => c.className === 'cz-side-back')[0];
assert.match(backA.textContent, /Back to the list/);
backA.click();
assert.equal(waitBox.getAttribute('data-focus'), 'false');
assert.deepEqual(t.state.released, [cardA]);                              // the panel is told, so voice lets go of it
const cardB = new El('div'); cardB.setAttribute('data-proposal-id', 'fb');
t.state.host.show(cardB, null, '', { focus: true });
const cardC = new El('div'); cardC.setAttribute('data-proposal-id', 'fc');
t.state.host.show(cardC, null, '', { focus: true });                       // one card in focus at a time
assert.equal(waitBox.all((c) => c.className === 'cz-side-back').length, 1);
assert.equal(waitBox.all((c) => c === cardB).length, 0);
cardC.remove(); t.state.fns.forEach((f) => f());                           // it fades away after it is done: the list returns
assert.equal(waitBox.getAttribute('data-focus'), 'false');
const cardH = new El('div'); cardH.setAttribute('data-proposal-id', 'fh');
t.state.host.show(cardH, null, '', { focus: true, hint: 'This may have changed since I first prepared it.' });   // a revised card says why, in the box
assert.match(waitBox.textContent, /may have changed/);
cardH.remove(); t.state.fns.forEach((f) => f());
const normal = new El('div'); normal.setAttribute('data-proposal-id', 'fn');
t.state.host.show(normal, null, '');                                       // an ordinary card still joins Open now
assert.equal(t.byTitle('Open now').getAttribute('data-empty'), 'false');
assert.equal(waitBox.getAttribute('data-focus'), 'false');
const rowClick = t.rows(waitBox)[0]; rowClick.click();
assert.deepEqual(t.state.reviewed.at(-1), w);                             // a tap on the row asks the panel to open it in the box
assert.equal(t.state.inColumn, true);

// 5. wording helpers and the layout rule only at a computer-wide window
assert.equal(waitingState({ type: 'recovery' }), 'result unverified');
assert.equal(waitingState({ type: 'draft' }), 'draft waiting');
assert.equal(ageOf(now - 30000, now), 'just now');
assert.equal(WIDE_PX, 1100);
assert.ok(CSS.startsWith('@media (min-width:1100px) and (hover:hover) and (pointer:fine){'));
assert.ok(CSS.includes('.cz-side{display:none;'));           // hidden unless that media query matches
// 6. the count squares: 5 to a row then they stack; capped with "+N"; zero shows none
const mk = (tag, cls, text) => { const n = new El(tag); n.className = cls; if (text != null) n.textContent = text; return n; };
const probe = (n) => { const node = new El('span'); dots(node, n, 'task', mk); return node; };
assert.equal(DOTS_PER_ROW, 5);
assert.equal(probe(3).all((c) => c.className === 'cz-side-dot').length, 3);
assert.equal(probe(3).style.width, '27px');                    // 3 squares wide
assert.equal(probe(8).style.width, '47px');                    // a full row of 5, the rest stack beneath
assert.equal(probe(0).all(() => true).length, 0);
assert.equal(probe(1).getAttribute('aria-label'), '1 task');
const many2 = probe(DOTS_MAX + 4);
assert.equal(many2.all((c) => c.className === 'cz-side-dot').length, DOTS_MAX);
assert.match(many2.textContent, /\+4/);

// 7. Open now: the panel hands over the card it built, the thread keeps a note; nothing is re-built here
const thread = new El('div');
function card(id) { const n = new El('div'); n.setAttribute('data-proposal-id', id); return n; }
function give(c) { const stub = new El('p'); thread.appendChild(stub); thread.appendChild(c); stub.parentNode = thread; t.state.host.show(c, stub); thread.children = thread.children.filter((x) => x !== c); return stub; }
// a card open on the right is not also listed as waiting (Kris 2026-10-16): it waits only once it has been left alone
t = setup({ waiting: [{ proposal_id: 'a', kind: 'update_contact', summary: 'Same card', status: 'pending', created_at: new Date().toISOString() },
                      { proposal_id: 'z', kind: 'update_contact', summary: 'Another one', status: 'pending', created_at: new Date().toISOString() }] });
assert.equal(t.rows(t.byTitle('Waiting for you')).length, 2);          // nothing is open yet: both wait
give(card('a'));
t.state.fns.forEach((f) => f());
assert.equal(t.rows(t.byTitle('Waiting for you')).length, 1);          // 'a' is open on the right, so only 'z' waits
assert.doesNotMatch(t.byTitle('Waiting for you').textContent, /Same card/); assert.match(t.byTitle('Waiting for you').textContent, /Another one/);
t = setup();
assert.ok(t.state.host.active());
const c1 = card('a'); const s1 = give(c1);
assert.equal(t.boxes()[0].getAttribute('data-empty'), 'false');
assert.equal(t.sb.openItems(), 1);
assert.ok(t.boxes()[0].all((c) => c === c1).length === 1);            // the same node, now in the column
// a card just dealt with says so beside "Open now" (Kris 2026-10-16), and the note goes with the card
const dn = card('done1'); dn.setAttribute('data-state', 'sent'); dn.querySelector = () => ({ textContent: 'Follow-up · done ✓' });
give(dn);
t.state.fns.forEach((f) => f());
assert.equal(t.byTitle('Open now').all((c) => c.className === 'cz-side-note')[0].textContent, 'Follow-up · done ✓');
dn.parentNode.children = dn.parentNode.children.filter((x) => x !== dn); dn.parentNode = null;   // it faded away
t.state.fns.forEach((f) => f());
assert.equal(t.byTitle('Open now').all((c) => c.className === 'cz-side-note')[0].textContent, '');
const c1b = card('a'); give(c1b);                                      // the same proposal again replaces the old copy
assert.equal(t.sb.openItems(), 1); assert.match(s1.textContent, /Replaced/);
for (let i = 0; i < OPEN_CAP + 2; i++) give(card('x' + i));            // past the cap the oldest go back to the chat
assert.equal(t.sb.openItems(), OPEN_CAP);
c1b.remove(); t.state.fns.forEach((f) => f());                         // a card that removed itself drops out
assert.equal(t.sb.openItems(), OPEN_CAP);

// 8. the status sections: the ledger's own words, the two allowances kept apart, nothing invented
assert.deepEqual(limitLines({ text: { unlimited: true }, voice: { enabled: false } }).map((l) => l.text), ['No daily limit', 'Not switched on for this account']);
assert.deepEqual(limitLines({ text: { cap: 40, used: 7, left: 33 }, voice: { enabled: true, limit_seconds: 600, used_seconds: 120, remaining_seconds: 480 } }),
  [{ label: 'Typed questions today', text: '7 of 40 used · 33 left' }, { label: 'Live voice today', text: '2 min of 10 min used · 8 min left' }]);
assert.deepEqual(limitLines({ text: { unavailable: true }, voice: { unavailable: true } }), []);   // unreadable shows nothing, not a guess
assert.equal(minutesWord(0), '0 min');
const led = ledgerLine({ title: 'Send the quote', state: 'Went through', customer: 'Dan', approved_by: 'you', approved_how: 'by tap', signed_off: true, age: '5 min ago', detail: 'Receipt checked.' });
assert.equal(led.meta, 'Went through · Dan · approved by you by tap · signed off (tap) · 5 min ago');
assert.match(ledgerLine({ title: 'x', state: 'Went through', approved_by: 'you', approved_how: 'route not recorded' }).meta, /approved by you \(route not recorded\)/);
assert.equal(doneSummary({ 'Went through': 3, 'Not done': 1 }), '3 went through · 1 not done');

statusReply = { open: { items: [{ id: 'p1', title: 'Send the quote to Dan', state: 'Parked', customer: 'Dan', age: '2 h ago', detail: 'Prepared, not tapped.' },
                                { id: 'p9', title: 'Old change', state: 'Expired', age: '3 d ago', detail: 'Timed out.' }], counts: {} },
  today: { items: [{ id: 'd1', title: 'Moved the visit', state: 'Went through', approved_by: 'you', approved_how: 'by voice', age: '1 h ago' }], counts: { 'Went through': 1 }, more: 0 },
  limits: { text: { cap: 40, used: 7, left: 33 }, voice: { enabled: true, limit_seconds: 600, used_seconds: 0, remaining_seconds: 600 } }, loop: 'split' };
aroundReply = { groups: [{ key: 'followups', label: 'Follow-ups', count: 2, more: 0, items: [{ who: 'Dan', chip: 'due today', text: 'Ring about the quote' }] }], unread: [] };
t = setup({ waiting: [{ proposal_id: 'p1', kind: 'send_email', summary: 'Send the quote to Dan', status: 'superseded', created_at: new Date(now - 7200000).toISOString() }],
            tasks: [{ id: 'k1', title: 'Chase the quote', status: 'waiting' }],
            ran: ["Checked what's awaiting your approval (recent)"], context: { found: 'Dan', sources: ['Looked up contacts (Dan)'] } });
await new Promise((r) => setTimeout(r, 20));
assert.ok(fetched.includes('/concierge/business/status'));
assert.ok(fetched.includes('/concierge/business/around'));                // the wider day is read with the status, so its dots show before it is opened
const waitRows = t.rows(t.byTitle('Waiting for you'));
assert.equal(waitRows.length, 2);                                         // each by its title only: the tappable one, and the expired one
assert.doesNotMatch(waitRows[0].textContent, /Parked|2 h ago/);          // the tappable one says nothing more: the card does
assert.match(t.byTitle('Waiting for you').textContent, /Old change/);     // needs you, but not on the chat's list
// the expired one opens its reason under its title when tapped, and closes again
const oldRow = t.byTitle('Waiting for you').all((c) => c.className === 'cz-side-xrow')[0];
const oldMore = oldRow.children[1];
assert.equal(oldMore.hidden, true); assert.match(oldMore.textContent, /Expired/); assert.match(oldMore.textContent, /Timed out\./);
oldRow.children[0].click(); assert.equal(oldMore.hidden, false); assert.equal(oldRow.children[0].getAttribute('aria-expanded'), 'true');
oldRow.children[0].click(); assert.equal(oldMore.hidden, true);
assert.match(t.byTitle('Done today').textContent, /Moved the visit.*approved by you by voice/);
assert.match(t.byTitle('Doing now').textContent, /1 waiting for you/);
assert.match(t.byTitle('Doing now').textContent, /planned, done and then checked/);
assert.equal(t.byTitle('Limits').getAttribute('data-empty'), 'false');
assert.match(t.byTitle('Limits').textContent, /7 of 40 used/); assert.match(t.byTitle('Limits').textContent, /0 min of 10 min used/);
assert.match(t.byTitle('Around the business').textContent, /Follow-ups \(2\)/);
// minimal (Kris 2026-10-16): the group by its name and count; a tap shows its items by name; a tap on one opens what else there is; Back returns
const aroundBox = t.byTitle('Around the business');
assert.doesNotMatch(aroundBox.textContent, /Dan/);
aroundBox.all((c) => c.className === 'cz-side-row')[0].click();
assert.match(aroundBox.textContent, /Back to the list/); assert.match(aroundBox.textContent, /Dan/);
const itemRow = aroundBox.all((c) => c.className === 'cz-side-xrow')[0];
assert.equal(itemRow.children[1].hidden, true); assert.match(itemRow.children[1].textContent, /due today/); assert.match(itemRow.children[1].textContent, /Ring about the quote/);
itemRow.children[0].click(); assert.equal(itemRow.children[1].hidden, false);
aroundBox.all((c) => c.className === 'cz-side-back')[0].click();
assert.doesNotMatch(aroundBox.textContent, /Dan/); assert.match(aroundBox.textContent, /Follow-ups \(2\)/);
assert.equal(aroundBox.children[0].children[2].getAttribute('aria-label'), '1 item');   // the squares count the groups
assert.equal(t.byTitle('Around the business').getAttribute('data-empty'), 'false');   // the dots are there from the start
assert.equal(t.byTitle('Around the business').getAttribute('data-open'), 'false');    // still folded until opened
// hidden for now (Kris 2026-10-16): Context never shows; Done today shows only after a turn looked at the ledger
assert.equal(t.byTitle('Context').getAttribute('data-empty'), 'true');
const quiet = setup({ ran: ['Looked up contacts (Dan)'] });
await new Promise((r) => setTimeout(r, 20));
assert.equal(quiet.byTitle('Done today').getAttribute('data-empty'), 'true');
assert.equal(quiet.byTitle('Tasks').getAttribute('data-empty'), 'true');
assert.equal(setup().byTitle('Done today').getAttribute('data-empty'), 'true');
// no search box in the column (Kris 2026-10-16): there is not enough in it to need one
assert.equal(t.sb.node.all((c) => c.className === 'cz-side-search').length, 0);
// status reads are spaced out: a burst of panel events makes at most one more read
const before = fetched.length;
for (let i = 0; i < 20; i++) t.state.fns.forEach((f) => f());
assert.ok(fetched.length - before <= 0);

// 9. a delegated card past its five minutes, or one a Review was refused for, offers no Review button
const old = { proposal_id: 'o1', kind: 'create_followup', summary: 'Follow up Dan', status: 'pending', delegated: true, created_at: new Date(now - 3 * 3600000).toISOString() };
const fresh = { ...old, proposal_id: 'o2', summary: 'Fresh one', delegated: true, created_at: new Date(now - 60000).toISOString() };
const legacy = { ...old, proposal_id: 'o3', summary: 'Legacy parked', delegated: false };
t = setup({ waiting: [old, fresh, legacy] });
const wb = t.byTitle('Waiting for you');
assert.deepEqual(t.rows(wb).map((r) => r.tag), ['button', 'button', 'button']);   // each is its title; the expired one opens its reason, the others open their card
assert.ok(t.rows(wb).every((r) => !r.textContent.includes('Review')));
assert.match(wb.textContent, /Follow up Dan.*expired, ask me to prepare it again/);   // the reason is under its title (closed until tapped)
assert.deepEqual(t.rows(wb).map((r) => r.textContent.includes('expired')), [false, false, false]);   // the rows themselves carry only the title
assert.equal(t.rows(wb).length, 3);
t.rows(wb)[1].click(); assert.deepEqual(t.state.reviewed.map((x) => x.proposal_id), ['o2']);   // the fresh one asks the panel to open its card

// 9b. the phone strip (Kris 2026-10-16): on a phone the column is the second half of #cz-pager; nothing is read until it is shown
{
  const scrolled = [], classes = new Set();
  const pagerEl = new El('div');
  pagerEl.classList = { toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)), add: (c) => classes.add(c) };
  pagerEl.scrollWidth = 800; pagerEl.clientWidth = 400; pagerEl.scrollLeft = 0;
  pagerEl.scrollTo = (o) => scrolled.push(o.left);
  const phone = { ...doc, getElementById: (id) => (id === 'cz-pager' ? pagerEl : null),
                  defaultView: { matchMedia: (q) => ({ matches: q.includes('1099px'), addEventListener() {} }) } };
  const root = new El('div'), st = { fns: [], host: null, reviewed: [] };
  const api = { tasks: () => [], waiting: () => [w], review: (x, inCol) => st.reviewed.push([x, inCol]), focusTask() {}, onChange: (f) => st.fns.push(f), host: (h) => { st.host = h; } };
  const before = fetched.length;
  const sb = mountSidebar(root, api, phone);
  assert.ok(classes.has('cz-pager-on'));
  assert.ok(pagerEl.children.includes(sb.node) && !root.children.includes(sb.node));   // the column is the strip's second half
  assert.equal(fetched.length, before);                                                  // not shown yet: no reads
  assert.ok(!root.children.some((c) => /cz-(pager|voice)-tab/.test(c.className)));
  assert.equal(st.host.active(), true);                                                  // newly offered cards go to the right-hand page
  assert.equal(st.host.focusable(), true);                                               // a tapped waiting item can open in the column
  st.fns.forEach((f) => f()); assert.equal(fetched.length, before);                       // events alone read nothing while it is off screen
  const waitingSection = sb.node.all((c) => c.className === 'cz-side-box').find((c) => /Waiting for you/.test(c.textContent));
  waitingSection.children[0].click();
  assert.equal(waitingSection.getAttribute('data-open'), 'false');
  st.host.revealWaiting();
  assert.equal(waitingSection.getAttribute('data-open'), 'true');
  assert.equal(waitingSection.children[0].getAttribute('aria-expanded'), 'true');
  assert.deepEqual(scrolled, [400]);                                                     // Waiting opens its section and slides across
  await new Promise((r) => setTimeout(r, 420));
  assert.equal(pagerEl.scrollLeft, 400);                                                 // and if the smooth scroll never starts, it goes there at once
  await new Promise((r) => setTimeout(r, 20));
  assert.ok(fetched.length > before);                                                    // and now the status is read
  const homeEl = sb.node.all((c) => c.className === 'cz-side-home')[0];
  assert.match(homeEl.textContent, /Concierge/); homeEl.click();
  assert.deepEqual(scrolled, [400, 0]);                                                  // back to the chat
  const held = new El('div'); held.setAttribute('data-proposal-id', 'ph1');
  st.host.show(held, null, '', { focus: true });
  assert.equal(scrolled.at(-1), 400);                                                    // a card opened in the column slides you to it
  assert.equal(sb.node.all((c) => c === held).length, 1);
  // a wide window or an ordinary desktop never gets the strip
  const wide = { ...doc, getElementById: () => pagerEl, defaultView: { matchMedia: (q) => ({ matches: q.includes('1100px'), addEventListener() {} }) } };
  const r2 = new El('div'), cls2 = new Set();
  pagerEl.classList = { toggle: (c, on) => (on ? cls2.add(c) : cls2.delete(c)), add: (c) => cls2.add(c) };
  mountSidebar(r2, api, wide);
  assert.ok(!cls2.has('cz-pager-on'));
  assert.ok(CSS.includes('.cz-pager.cz-pager-on{display:flex;') && CSS.includes('scroll-snap-type:x mandatory') && CSS.includes('scroll-snap-stop:always'));
}

// 9c. over the live voice screen the column is a panel on top (Kris 2026-10-16): a swipe left brings it in, a swipe right puts it away
{
  const bodyOn = new Set(['voice-live-open']), handlers = {}, slid = [];
  const body = { classList: { toggle: (c, on) => (on ? bodyOn.add(c) : bodyOn.delete(c)), contains: (c) => bodyOn.has(c) }, appendChild() {} };
  const pagerV = new El('div');
  pagerV.classList = { toggle() {}, add() {} }; pagerV.scrollWidth = 800; pagerV.clientWidth = 400; pagerV.scrollLeft = 0; pagerV.scrollTo = (o) => slid.push(o.left);
  const phoneV = { ...doc, body, addEventListener: (t, f) => { handlers[t] = f; }, getElementById: (id) => (id === 'cz-pager' ? pagerV : null),
                   defaultView: { matchMedia: (q) => ({ matches: q.includes('1099px'), addEventListener() {} }) } };
  const rootV = new El('div'), host = { h: null };
  const apiV = { tasks: () => [], waiting: () => [], refused: () => ({}), onChange() {}, host: (h) => { host.h = h; }, focusTask() {}, review() {} };
  const sbV = mountSidebar(rootV, apiV, phoneV);
  assert.ok(bodyOn.has('cz-pager-body'));                                                // voice swipe navigation is enabled
  const swipe = (x1, y1, x2, y2) => { handlers.touchstart({ touches: [{ clientX: x1, clientY: y1 }] }); handlers.touchend({ changedTouches: [{ clientX: x2, clientY: y2 }] }); };
  swipe(300, 400, 120, 410);                                                             // a swipe left
  assert.ok(bodyOn.has('cz-voice-side')); assert.deepEqual(slid, []);                    // a panel over the voice screen, not a slide of the strip
  swipe(100, 300, 260, 305);                                                             // a swipe right
  assert.ok(!bodyOn.has('cz-voice-side'));
  swipe(300, 400, 250, 402); assert.ok(!bodyOn.has('cz-voice-side'));                    // too short: a tap or a press is never a swipe
  swipe(300, 400, 150, 650); assert.ok(!bodyOn.has('cz-voice-side'));                    // mostly vertical
  host.h.show(new El('div'), null, '', { focus: true });                                 // a card opened in the column comes in over the voice screen too
  assert.ok(bodyOn.has('cz-voice-side')); assert.deepEqual(slid, []);
  sbV.node.all((c) => c.className === 'cz-side-home')[0].click();                        // "Concierge" puts it away
  assert.ok(!bodyOn.has('cz-voice-side'));
  bodyOn.delete('voice-live-open');                                                      // no voice screen: a swipe does nothing here (the strip is for that)
  swipe(300, 400, 120, 410); assert.ok(!bodyOn.has('cz-voice-side'));
  assert.ok(CSS.includes('body.voice-live-open .cz-pager.cz-pager-on>.cz-side{position:fixed;') && CSS.includes('transform:translateX(100%);visibility:hidden'));
  assert.ok(CSS.includes('body.voice-live-open.cz-voice-side .cz-pager.cz-pager-on>.cz-side{transform:none;visibility:visible'));
}

// 10. the voice screen leaves room for the column (CSS only, gated on the body class the column sets)
assert.ok(bodyClasses.has('cz-side-wide'));
assert.ok(CSS.includes('body.cz-side-wide.voice-live-open #voice-live-root.is-open'));
assert.ok(CSS.includes('#voice-gptlive-root.is-open .vl-stage{padding-right:22rem}'));
assert.ok(!CSS.includes('.vl-stage{right:22rem}'));   // the water runs the full width behind the cards
assert.ok(CSS.includes('#voice-gptlive-root.is-open .cz-debrief{right:calc(22rem + .8rem)}'));
assert.ok(!CSS.includes('#voice-gptlive-root.is-open{right:22rem}'));
assert.ok(!CSS.includes('voice-live-open #cz-root.cz-has-side .cz-side-box'));   // the cards keep their own colour
console.log('ok');
