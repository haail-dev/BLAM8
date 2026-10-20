import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync('src/app/static/concierge.js', 'utf8');
function actualFunction(name) {
  const start = source.indexOf('  async function ' + name + '(');
  assert(start >= 0, name + ' must remain available');
  const end = source.indexOf('\n  }', start);
  assert(end > start, name + ' must have a complete body');
  return source.slice(start, end + 4);
}

// Exercise the production caller and request builder together, with sync both
// disabled and enabled. Only rendering and the HTTP boundary are replaced.
for (const synced of [false, true]) {
  for (const fails of [false, true]) {
    const path = '/app/diary/settings';
    const requests = [];
    const deviceSync = synced ? {
      ready: Promise.resolve(), automatic: false,
      state: () => ({payload: {arrivals: []}}),
    } : null;
    const context = vm.createContext({
      deviceSync, location: {pathname: path}, window: {location: {pathname: path}},
      sending: false, sendBtn: {disabled: false},
      panel: {classList: {contains: () => false}},
      log: [{role: 'user', content: 'Earlier question'},
            {role: 'assistant', content: 'Earlier answer'},
            {role: 'user', content: 'Follow-up'}], MAX_TURNS: 2,
      setupCards: null, msgs: {appendChild() {}},
      touch() {}, showTyping() {}, hideTyping() {}, saveLog() {},
      bubbleEl() { return {}; }, appendHeldLine() {}, pinToTop() {},
      postJsonWithRetry: async (url, body) => {
        requests.push({url, body: JSON.parse(JSON.stringify(body))});
        if (deviceSync) assert.equal(deviceSync.automatic, true);
        if (fails) throw new Error('Offline failure');
        return {reply: 'Here is the setup guidance.'};
      },
    });
    vm.runInContext(actualFunction('askSilently') + '\n' + actualFunction('helpTurn'), context);
    assert.equal(await context.askSilently('Explain this page'), !fails);
    assert.deepEqual(requests, [{url: '/concierge/help', body: {
      question: 'Explain this page',
      history: [{role: 'assistant', content: 'Earlier answer'},
                {role: 'user', content: 'Follow-up'}],
      path, auto: true, card_mode: false,
    }}]);
    assert.equal(context.sending, false);
    assert.equal(context.sendBtn.disabled, false);
    if (deviceSync) {
      assert.equal(deviceSync.automatic, false);
      deviceSync.state = () => ({payload: {arrivals: ['help:' + path]}});
      assert.equal(await context.askSilently('Explain this page'), false);
      assert.equal(requests.length, 1, 'A synced arrival must not request duplicate guidance');
    }
  }
}
console.log('Automatic Help preserves path/history and sync markers on success and failure.');
