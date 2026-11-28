// Execute the actual widget turn and fill functions with synthetic fields and a fake server.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync('src/app/static/concierge.js', 'utf8');
function between(start, end) {
  const at = source.indexOf(start);
  assert(at >= 0 && source.indexOf(end, at) > at);
  return source.slice(at, source.indexOf(end, at));
}
const turn = between('async function onboardTurn(', '// Node builder + appender');
// The real turn reads this closure state even on a failed non-Knowledge turn.
// Include the shipped declaration/helper rather than stubbing out its behaviour.
const choices = between('var pendingKnowledgeChoice = null;', 'function appendHeldLine(');
const fill = between('function applyFill(', '// The step\'s own advance button');
for (const [step, held, fails] of [
  ['interview', [], false], ['interview', ['Knowledge notes: Work limits'], false],
  ['interview', [], true], ['voice', [], false],
]) {
  const elements = Object.fromEntries(['business_name', 'industry', 'business_brief', 'goals', 'website']
    .map(name => [name, {value: '', dispatchEvent() {}}]));
  const events = [], log = [], bubbles = [], requests = [];
  const values = {business_name: 'Example (Rig Demo)', industry: 'Music teaching',
    business_brief: 'Fictional/test-only online tuition for adults.',
    goals: 'Collect enquiries for human review; tutor schedules lessons.', website: 'example.test'};
  const context = vm.createContext({
    window: {}, voiceGuide: {propose: values => events.push(["proposed", values]), mount() {}}, scrollDown() {},
    ONB_STEP: step, log, String, Object, Array, Event: class {},
    document: {getElementById() { return null; }},
    fieldEls: name => elements[name] ? [elements[name]] : [], flash() {}, readCurrent: () => ({}),
    async postJsonWithRetry(url, body) {
      requests.push({url, body});
      if (fails) throw new Error('unavailable');
      return {reply: 'Please review.', form_fill: values, ready_to_advance: false, held_for: held};
    },
    hideTyping() {}, saveLog() {}, bubbleEl: (role, text) => ({role, text}),
    msgs: {appendChild: bubble => bubbles.push(bubble), querySelectorAll: () => []},
    appendHeldLine: receipts => events.push(['held', receipts]),
    showContinueHint: () => events.push(['continue']), pinToTop() {},
    // Unexpected navigation, record creation or submission has no fake implementation.
  });
  vm.runInContext(fill + '\n' + choices + '\n' + turn, context);
  const spoke = await context.onboardTurn("I'll check it here first.", []);
  assert.equal(spoke, !fails);
  assert.equal(context.pendingKnowledgeChoice, null);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, '/concierge/onboard');
  assert(events.some(e => e[0] === 'continue')); // availability refresh is independent of model advance=false
  assert.equal(log[0].content, bubbles[0].text);
  if (fails) {
    assert(Object.values(elements).every(el => el.value === ''));
    assert(!bubbles[0].text.includes('This chat turn has not changed'));
  } else {
    for (const [name, value] of Object.entries(values)) assert.equal(elements[name].value, step === "voice" ? "" : value);
    if (step === "voice") assert.deepEqual(events.find(e => e[0] === "proposed")[1], values);
    if (step === 'interview') {
      assert(bubbles[0].text.includes('not saved until you submit this form'));
      assert(bubbles[0].text.includes('has not changed sending settings'));
      assert.equal(bubbles[0].text.includes('recorded as suggestions'), held.length > 0);
      assert.equal(bubbles[0].text.includes('No new items'), held.length === 0);
      if (held.length) assert(bubbles[0].text.includes('not applied settings or saved Knowledge'));
    } else assert.equal(bubbles[0].text, 'Please review.');
  }
}
console.log('Onboarding capture replay passed');
