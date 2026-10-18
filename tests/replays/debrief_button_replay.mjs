// The Debrief button, replayed against the REAL functions: voice-live.js's
// startDebrief / runAssistantTurn and concierge_panel.js's mountDebrief /
// debriefPress / paintDebrief / voiceUserTurn / voiceAssistantTurn, each sliced
// out of the shipped source into a VM with just enough around it. Run by
// tests/test_debrief_button.py; exits non-zero with the failing scenario.
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const liveSource = fs.readFileSync('src/app/static/voice-live.js', 'utf8');
const panelSource = fs.readFileSync('src/app/static/concierge_panel.js', 'utf8');

function slice(source, indent, name) {
  const pattern = new RegExp(`^${indent}(?:async )?function ${name}\\(`, 'm');
  const start = source.search(pattern);
  assert(start >= 0, `function ${name} not found`);
  const tail = source.slice(start + indent.length);
  const next = tail.slice(1).search(new RegExp(`^${indent}(?:async )?function \\w+\\(`, 'm'));
  return next < 0 ? tail : tail.slice(0, next + 1);
}
const live = name => slice(liveSource, '  ', name);
const panel = name => slice(panelSource, '    ', name);
const constant = name => {
  const m = liveSource.match(new RegExp(`^const ${name} = "([^"]*)";`, 'm'));
  assert(m, `const ${name} not found`);
  return m[1];
};
// Objects built inside a VM have that VM's prototypes; compare them as plain data.
const plain = x => JSON.parse(JSON.stringify(x));
const tick = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return {promise, resolve, reject}; };

// ── voice-live.js: startDebrief ─────────────────────────────────────────────
function startRig(phase = 'listening', open = true) {
  const turns = [], toasts = [];
  const c = {open, phase, DEBRIEF_START: constant('DEBRIEF_START'), DEBRIEF_NEXT: constant('DEBRIEF_NEXT'),
    notify: t => toasts.push(t), runTurn: (text, meta) => turns.push([text, meta])};
  vm.createContext(c);
  vm.runInContext(live('startDebrief'), c);
  return {c, turns, toasts};
}
{
  const r = startRig('listening');
  assert.equal(r.c.startDebrief('start'), true);
  assert.deepEqual(plain(r.turns), [['Run me through my debrief.', {debrief: 'start'}]]);
  assert.equal(r.c.startDebrief(), true);                       // no step = start
  assert.deepEqual(plain(r.turns[1]), ['Run me through my debrief.', {debrief: 'start'}]);
  assert.equal(r.c.startDebrief('next'), true);
  assert.deepEqual(plain(r.turns[2]), ['Next.', {debrief: 'next'}]);
  assert.equal(r.c.startDebrief('rewind'), true);               // anything else is a start, never a wrong move
  assert.deepEqual(plain(r.turns[3][1]), {debrief: 'start'});
}
for (const phase of ['speaking', 'thinking']) {                 // mid-answer is fine: the new turn takes over
  const r = startRig(phase); assert.equal(r.c.startDebrief('next'), true); assert.equal(r.turns.length, 1);
}
for (const [phase, said] of [['connecting', 'One moment — still connecting'], ['paused', 'Tap the mic first'],
  ['error', 'Tap the mic first'], ['idle', 'Tap the mic first']]) {
  const r = startRig(phase);
  assert.equal(r.c.startDebrief('start'), false); assert.deepEqual(plain(r.turns), []); assert.deepEqual(plain(r.toasts), [said]);
}
{
  const r = startRig('listening', false);
  assert.equal(r.c.startDebrief('start'), false); assert.deepEqual(plain(r.turns), []); assert.deepEqual(plain(r.toasts), []);
}

// ── voice-live.js: the flag reaches the server ──────────────────────────────
function turnRig() {
  const chat = [], effects = [];
  const c = {
    open: true, voiceSession: 'session-test', delivery: {interrupt() {}}, turnInFlight: null, turnAbort: null,
    spokenSinceCard: 0, turnThinking: false, thinkingNote: '', captionBuffer: '', bargeTimer: 0, userSpeaking: false,
    phase: 'listening', liveSources: [], els: {reply: {textContent: ''}, transcript: {textContent: ''}},
    AbortController, TextDecoder, Date, Math, JSON, console, TURN_STALL_MS: 45000,
    clearHeld() {}, paintDiscard() {}, stopPlayback() {}, setPhase(p) {c.phase = p;}, notify() {}, refreshStage() {},
    turns: {discard() {}}, logHeard() {}, setTimeout() {}, clearTimeout() {},
    deps: {onUserTurn: async () => ({}), buildChatBody: () => ({question: 'q', history: []}),
      onAssistantTurn: payload => effects.push(payload), onDiscardTurn() {},
      async apiFetch(action, body) {
        if (action === 'chat') { const d = deferred(); chat.push({d, body}); return d.promise; }
        return {ok: true, json: async () => ({})};
      }},
    stallGuard: p => p, splitReadySentences: text => ({sentences: [text], rest: ''}),
    speakPhrase: async () => {}, LOCAL_LINES: [], ttsVoice: 'marin',
  };
  vm.createContext(c);
  vm.runInContext(live('runAssistantTurn'), c);
  return {c, chat, effects};
}
for (const [meta, expected] of [[{debrief: 'start'}, 'start'], [{debrief: 'next'}, 'next'], [{}, undefined]]) {
  const r = turnRig(); const running = r.c.runAssistantTurn('hello', meta); await tick();
  assert.equal(r.chat.length, 1);
  assert.equal(r.chat[0].body.debrief, expected);
  assert.equal(r.chat[0].body.voice_session, 'session-test');          // the walk is keyed by this
  assert.equal('debrief' in r.chat[0].body, expected !== undefined);   // an ordinary turn is byte-for-byte as before
  r.chat[0].d.resolve({ok: false, json: async () => ({error: 'x'})}); await running.catch(() => {});
}

// ── concierge_panel.js: the button and the screen's state ───────────────────
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.listeners = {}; this.classes = new Set(); this.hidden = false; this.textContent = ''; this.type = ''; }
  set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return [...this.classes].join(' '); }
  get classList() {
    const s = this;
    return {add: c => s.classes.add(c), remove: c => s.classes.delete(c), contains: c => s.classes.has(c),
      toggle: (c, on) => { const want = on === undefined ? !s.classes.has(c) : !!on; want ? s.classes.add(c) : s.classes.delete(c); return want; }};
  }
  set innerHTML(v) { this._inner = v; this.step = new El('span'); this.step.className = 'cz-debrief-step'; this.step.hidden = true; }
  get innerHTML() { return this._inner || ''; }
  querySelector(sel) {
    if (sel === '.cz-debrief-step') return this.step || null;
    if (sel === '.cz-debrief') return this.children.find(c => c.classes.has('cz-debrief')) || null;
    return null;
  }
  appendChild(c) { this.children.push(c); return c; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  click() { const e = {stopped: false, stopPropagation() { this.stopped = true; }}; (this.listeners.click || []).forEach(f => f(e)); return e; }
}
function panelRig({allowed = true, instance} = {}) {
  const panelRoot = new El('div'); panelRoot.setAttribute('data-debrief', allowed ? 'on' : 'off');
  const c = {root: panelRoot, document: {createElement: tag => new El(tag)}, voiceLiveInstance: instance || null, Object};
  vm.createContext(c);
  for (const name of ['debriefAllowed', 'paintDebrief', 'debriefPress', 'mountDebrief']) vm.runInContext(panel(name), c);
  vm.runInContext(panelSource.match(/^    var debriefBtns = \[\], debriefState = null;$/m)[0], c);
  const icon = panelSource.match(/^    var DEBRIEF_ICON = .*$/m)[0];
  vm.runInContext(icon, c);
  return {c, panelRoot};
}
{
  const calls = [];
  const instance = {isOpen: () => true, startDebrief: s => calls.push(s)};
  const {c} = panelRig({instance});
  const overlay = new El('div');
  c.mountDebrief(overlay); c.mountDebrief(overlay);                 // idempotent: one button per screen
  const btn = overlay.children[0];
  assert.equal(overlay.children.length, 1); assert(btn.classes.has('cz-debrief')); assert(overlay.classes.has('has-debrief'));
  assert.match(btn.innerHTML, /<svg/); assert.equal(btn.type, 'button');
  assert.equal(btn.attrs['aria-label'], 'Debrief — go through today with the Concierge');
  assert.equal(btn.step.hidden, true);
  // idle → a tap starts; the tap is the button's own, not the screen's
  const e = btn.click(); assert.deepEqual(calls, ['start']); assert.equal(e.stopped, true);
  // the newest reply says the walk is on: the button wears the count and a tap is "next"
  c.debriefState = {step: 2, of: 5, label: 'Drafts waiting', finished: false}; c.paintDebrief();
  assert.equal(btn.step.hidden, false); assert.equal(btn.step.textContent, '2/5'); assert(btn.classes.has('is-running'));
  assert.match(btn.attrs['aria-label'], /group 2 of 5 — Drafts waiting\. Tap for the next group/);
  btn.click(); assert.deepEqual(calls, ['start', 'next']);
  // finished (or cleared): back to a start button
  c.debriefState = {step: 5, of: 5, label: '', finished: true}; c.paintDebrief();
  assert.equal(btn.step.hidden, true); assert(!btn.classes.has('is-running'));
  btn.click(); assert.equal(calls.at(-1), 'start');
  c.debriefState = null; c.paintDebrief(); assert.equal(btn.title, 'Debrief');
}
{
  // not the owner's or a manager's: no button at all
  const {c} = panelRig({allowed: false});
  const overlay = new El('div'); c.mountDebrief(overlay);
  assert.equal(overlay.children.length, 0); assert(!overlay.classes.has('has-debrief'));
}
{
  // a tap with no live conversation, or one that cannot take it, does nothing and says nothing
  for (const instance of [null, {isOpen: () => false, startDebrief() { throw Error('closed'); }}, {isOpen: () => true}]) {
    const {c} = panelRig({instance}); const overlay = new El('div'); c.mountDebrief(overlay);
    overlay.children[0].click();
  }
}

// voiceUserTurn: the button's request is not "heard words"
function userTurnRig() {
  const logged = [], cleared = [];
  const c = {
    voiceLog: entry => { logged.push(entry); return {entry}; }, voiceUserBubbles: new Map(), voiceLastUser: null,
    voiceStage: {proposal: null, cardNode: null, links: [], draft: null},
    clearVoiceCard: () => cleared.push(1),
    isBareDecline: () => { throw Error('heard-word guess ran for a button request'); },
    isBareAffirmation: () => { throw Error('heard-word guess ran for a button request'); },
    isVoiceFarewell: () => { throw Error('heard-word guess ran for a button request'); },
  };
  vm.createContext(c);
  vm.runInContext(panel('settleForDebrief'), c);
  vm.runInContext(panel('voiceUserTurn'), c);
  return {c, logged, cleared};
}
{
  const r = userTurnRig();
  const out = await r.c.voiceUserTurn('Next.', {turnId: 't1', origin: 'voice', debrief: 'next'});
  assert.deepEqual(plain(out), {});
  assert.equal(r.logged.length, 1); assert.equal(r.logged[0].content, 'Next.'); assert.equal(r.logged[0].role, 'user');
  assert.deepEqual(plain(r.cleared), []);
  // with a card up, it is set aside like any new message — and still no guessing
  const r2 = userTurnRig(); r2.c.voiceStage.cardNode = {}; r2.c.voiceStage.proposal = {};
  assert.deepEqual(plain(await r2.c.voiceUserTurn('Next.', {turnId: 't2', origin: 'voice', debrief: 'next'})), {});
  assert.equal(r2.cleared.length, 1);
}

// voiceAssistantTurn: the newest reply carries where we are in the walk
function assistantRig() {
  const painted = [];
  const c = {
    debriefState: 'untouched', paintDebrief: () => painted.push(c.debriefState),
    voiceLog: () => ({}), lookupsOf: () => [], parkVoiceCard() {}, noteShown() {}, voiceStage: {links: [], cardNode: null, proposal: null, draft: null, fresh: false},
    loadWaiting() {}, savedVoiceMode: () => 'standard', Object,
  };
  vm.createContext(c);
  vm.runInContext(panel('voiceAssistantTurn'), c);
  return {c, painted};
}
{
  const r = assistantRig();
  r.c.voiceAssistantTurn({reply: 'Morning.', debrief: {step: 1, of: 3, label: 'Money', finished: false}});
  assert.deepEqual(plain(r.c.debriefState), {step: 1, of: 3, label: 'Money', finished: false});
  r.c.voiceAssistantTurn({reply: 'Thanks.'});                  // an ordinary turn leaves the walk as it was
  assert.equal(r.c.debriefState.step, 1); assert.equal(r.painted.length, 1);
  r.c.voiceAssistantTurn({reply: 'Done.', debrief: null});      // the server says there is no walk
  assert.equal(r.c.debriefState, null);
}

console.log('Debrief button regressions passed.');
