// Offline DOM contract harness: executes the production typer, without dependencies.
const assert = require('node:assert/strict');
const fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
const sandbox = {module: {exports: {}}};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../../src/app/static/ghost.js'), 'utf8'), sandbox);
const createTyper = sandbox.module.exports;

function fixture(reduced = false) {
  let queue = new Map(), serial = 0, listener;
  const clock = {
    setTimeout(fn) { queue.set(++serial, fn); return serial; },
    clearTimeout(id) { queue.delete(id); }
  };
  const doc = {createTreeWalker(el) {
    let index = 0;
    return {nextNode() { return el.nodes[index++] || null; }};
  }};
  const motion = {matches: reduced, addEventListener(type, fn) { listener = fn; }};
  const typer = createTyper(doc, clock, motion);
  function line(parts) {
    const el = {isConnected: true, nodes: parts.map(part => ({
      nodeValue: part.text, parentElement: {closest() { return part.control ? {} : null; }}
    }))};
    el.text = () => el.nodes.map(node => node.nodeValue).join('');
    return el;
  }
  function step() {
    const pending = [...queue.values()]; queue.clear(); pending.forEach(fn => fn());
  }
  return {typer, line, step, motion, reduce() { motion.matches = true; listener(); },
          drain() { for (let n = 0; queue.size && n < 100; n++) step(); assert.equal(queue.size, 0); }};
}

{
  const f = fixture(), text = 'Searched for: the actual customer message';
  const first = f.line([{text}]);
  f.typer.reveal(first, 'round-1');
  assert.equal(first.text(), text.slice(0, 2));
  f.step(); const shown = first.text();
  assert(shown.length > 2 && shown.length < text.length);
  first.isConnected = false;
  const replacement = f.line([{text}]);
  f.typer.reveal(replacement, 'round-1');
  assert.equal(replacement.text(), shown, 'repaint resumes at the same character');
  f.drain(); assert.equal(replacement.text(), text);
  const again = f.line([{text}]); f.typer.reveal(again, 'round-1');
  assert.equal(again.text(), text, 'completed text never retypes');
  const nextRound = f.line([{text}]); f.typer.reveal(nextRound, 'round-2');
  assert.equal(nextRound.text().length, 2, 'the next round is new work');
}
{
  const f = fixture(), first = f.line([{text: 'Checked the diary.'}]);
  f.typer.reveal(first, 'event-1'); f.drain();
  const more = f.line([{text: 'Checked the diary. Two times available.'}]);
  f.typer.reveal(more, 'event-1');
  assert.equal(more.text(), 'Checked the diary.', 'an extension reveals only new text');
  // live.js flushes the previous line when a real event arrives, without waiting.
  f.typer.finish();
  const last = f.line([{text: 'More customer details needed.'}]);
  f.typer.reveal(last, 'event-2');
  assert.equal(more.text(), 'Checked the diary. Two times available.');
  assert(last.text().startsWith('Mo')); f.drain();
}
{
  const f = fixture(), long = f.line([{text: 'x'.repeat(1000)}]);
  f.typer.reveal(long, 'long'); f.step();
  assert(long.text().length > 50, 'backlog speeds up');
  f.reduce(); assert.equal(long.text().length, 1000);
  const f2 = fixture(true), instant = f2.line([{text: 'No animation here.'}]);
  f2.typer.reveal(instant, 'reduced'); assert.equal(instant.text(), 'No animation here.');
}
{
  const f = fixture(), link = {text: '2 knowledge matches'}, countdown = {text: '2h 01m', control: true};
  const mixed = f.line([{text: 'Search: '}, link, countdown]);
  const linkNode = mixed.nodes[1], buttonNode = mixed.nodes[2];
  f.typer.reveal(mixed, 'links'); f.drain();
  assert.equal(mixed.nodes[1], linkNode, 'link node survives');
  assert.equal(buttonNode.nodeValue, '2h 01m', 'live countdown is not animated');
  const malicious = '<img src=x onerror=alert(1)>';
  const safe = f.line([{text: malicious}]); f.typer.reveal(safe, 'unsafe'); f.drain();
  assert.equal(safe.text(), malicious, 'input stays literal text');
}
console.log('Ghost DOM replay passed');

{
  const f = fixture(), text = 'Knowledge search: 2 matches returned.';
  const live = f.line([{text}]); f.typer.reveal(live, 'live-1', true); f.typer.finish();
  const saved = f.line([{text}]); f.typer.reveal(saved, 'round-1');
  assert.equal(saved.text(), text, 'landing must not retype completed live text');
  f.typer.begin();
  const next = f.line([{text}]); f.typer.reveal(next, 'round-2');
  assert.equal(next.text().length, 2, 'a new draft has its own live bridge');
}
