// Press-and-hold on the person icon: the voice preparation starts on pointerdown (not after the one
// second hold), a cancelled press still only prepared, and the hold itself fires once.
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const source = fs.readFileSync('src/app/static/concierge_panel.js', 'utf8').replace(/\r\n/g, '\n');
const start = source.indexOf('  function holdForVoice(el, onHold, onDown) {');
assert(start > 0, 'holdForVoice(el, onHold, onDown) must exist');
const body = source.slice(start, source.indexOf('  // ── the page frame', start));

function rig() {
  const timers = [];
  const handlers = {};
  const el = {
    classList: { add() {}, remove() {} },
    addEventListener(type, fn) { (handlers[type] ||= []).push(fn); },
  };
  const ctx = { HOLD_MS: 1000, navigator: {}, el, clearTimeout: id => { const t = timers.find(x => x.id === id); if (t) t.dead = true; },
    setTimeout: (fn, ms) => { const t = { id: timers.length + 1, fn, ms, dead: false }; timers.push(t); return t.id; } };
  vm.createContext(ctx);
  vm.runInContext(body + '\nthis.holdForVoice = holdForVoice;', ctx);
  const calls = [];
  ctx.holdForVoice(el, () => calls.push('hold'), () => calls.push('down'));
  const fire = type => (handlers[type] || []).forEach(fn => fn({ button: 0, preventDefault() {}, stopImmediatePropagation() {} }));
  return { calls, fire, timers, tick: () => timers.filter(t => !t.dead).forEach(t => { t.dead = true; t.fn(); }) };
}

{ // down -> prepare at once; hold fires only when the timer does
  const r = rig();
  r.fire('pointerdown');
  assert.deepEqual(r.calls, ['down']);
  assert.equal(r.timers[0].ms, 1000);
  r.tick();
  assert.deepEqual(r.calls, ['down', 'hold']);
}
{ // released early: prepared, never held
  const r = rig();
  r.fire('pointerdown');
  r.fire('pointerup');
  r.tick();
  assert.deepEqual(r.calls, ['down']);
}
{ // a failing preparation never breaks the hold
  const timers = [];
  const handlers = {};
  const el = { classList: { add() {}, remove() {} }, addEventListener(t, fn) { (handlers[t] ||= []).push(fn); } };
  const ctx = { HOLD_MS: 1000, navigator: {}, clearTimeout() {}, setTimeout: fn => { timers.push(fn); return 1; } };
  vm.createContext(ctx);
  vm.runInContext(body + '\nthis.holdForVoice = holdForVoice;', ctx);
  const calls = [];
  ctx.holdForVoice(el, () => calls.push('hold'), () => { throw new Error('boom'); });
  handlers.pointerdown.forEach(fn => fn({ button: 0 }));
  timers.forEach(fn => fn());
  assert.deepEqual(calls, ['hold']);
}
// Both frames pass a preparation, and the modal one fetches the panel first.
assert(source.includes('}, prepareVoiceModule);'), 'page frame must prepare the voice module on press');
assert(/load\(\);\s+prepareVoiceModule\(\);/.test(source), 'modal frame must start the panel fetch and the voice module together on press');
assert(source.includes('function prepareVoiceModule()'), 'prepareVoiceModule exists');
console.log('Press-and-hold prepares the voice screen on pointerdown and still holds for one second.');
