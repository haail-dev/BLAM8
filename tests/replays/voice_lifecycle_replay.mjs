// Exercise actual lifecycle functions with deferred media/network ownership.
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const source = fs.readFileSync('src/app/static/voice-live.js', 'utf8');
function extract(name) {
  const pattern = new RegExp(`^  (?:async )?function ${name}\\(`, 'm');
  const start = source.search(pattern);
  assert(start >= 0, name);
  const tail = source.slice(start + 2);
  const end = tail.slice(1).search(/^  (?:async )?function \w+\(/m);
  // trailing comments/constants are harmless in the VM; don't include the
  // next function (whose tests may require different dependencies).
  return end < 0 ? tail : tail.slice(0, end + 1);
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}
const tick = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const local = process.argv.includes('--local');
const token = {ok: true, json: async () => ({value: 'ticket', ...(local ? {provider:'local', websocket_url:'wss://speech.example/v1/realtime', voice:'af_heart'} : {})})};
function stream() {
  const tracks = [0, 1].map(() => ({enabled: true, readyState: 'live', muted: false, stops: 0, stop() { this.stops++; }}));
  return {tracks, getTracks: () => tracks, getAudioTracks: () => tracks};
}
function rig(recordCaptions = false) {
  const tokens = [], media = [], worklets = [], sockets = [], phases = [];
  const c = {
    open: true, audioGeneration: 0, connectionGeneration: 0, audioAbort: new AbortController(), micReady: false,
    micCtx: null, micStream: null, ws: null, captionsWs: null, captionsLive: false, captionBuffer: '',
    muted: false, micLine: 0, voiceVolume: 1, playGain: null,
    ttsVoice: 'marin', phraseCache: new Map(), SAMPLE_RATE: 24000, WORKLET_SOURCE: '', TURN_STALL_MS: 45000,
    AbortController, Blob, URL: {createObjectURL: () => 'blob:test', revokeObjectURL() {}},
    deps: {canStart: () => ({ok: true}), apiFetch(url, body) {
      if (body?.purpose === "captions" && !recordCaptions) return new Promise(() => {});
      const d = deferred(); tokens.push(d); return d.promise; }},
    savedConciergeVoice: () => 'marin', savedMicLine: () => 0, savedVoiceVolume: () => 1,
    ensurePlayCtx() {}, bargeIn() {}, noiseKind: () => '', setPhase: (...x) => phases.push(x),
    stallGuard: p => p, navigator: {mediaDevices: {getUserMedia() { const d = deferred(); media.push(d); return d.promise; }}},
    AudioContext: class { state = 'running'; audioWorklet = {addModule() { const d = deferred(); worklets.push(d); return d.promise; }};
      resume() {return Promise.resolve();} close() {this.state = 'closed'; return Promise.resolve();}
      createMediaStreamSource() {return {connect() {}};}
    },
    AudioWorkletNode: class {port = {};},
    WebSocket: class {constructor() {sockets.push(this);} close() {this.closed = true;}},
    wasHidden: false, zeroSince: 0, turns: {reset() {}}, clearHeld() {}, gateOpenedAt: 0, userSpeaking: false,
    micGateAnnounced: false, paintMute() {}, reloadIntoVoice() {throw Error('unexpected reload');},
    console, Date, JSON, setTimeout, clearTimeout,
  };
  vm.createContext(c);
  for (const name of ['start', 'teardownAudio', 'reconnect', 'openRealtime', 'openCaptions', 'attachRealtimeHandlers']) {
    vm.runInContext(extract(name), c);
  }
  async function finish(index, owned) {
    tokens[index].resolve(token); await tick();
    media.at(-1).resolve(owned); await tick();
    worklets.at(-1).resolve(); await tick();
  }
  return {c, tokens, media, worklets, sockets, phases, finish};
}

// A late token cannot even request a mic after close/reopen.
{
  const r = rig(); const old = r.c.start();
  r.c.open = false; r.c.teardownAudio(); r.c.open = true;
  const next = r.c.start(); const owned = stream(); await r.finish(1, owned);
  r.tokens[0].resolve(token); await old; await next;
  assert.equal(r.media.length, 1); assert.equal(r.c.micStream, owned);
}
// A stale permission completion stops EVERY track without touching new media.
{
  const r = rig(); const old = r.c.start(); r.tokens[0].resolve(token); await tick();
  r.c.open = false; r.c.teardownAudio(); r.c.open = true;
  const next = r.c.start(); const owned = stream(); await r.finish(1, owned);
  const stale = stream(); r.media[0].resolve(stale); await old; await next;
  assert(stale.tracks.every(t => t.stops >= 1)); assert(owned.tracks.every(t => t.stops === 0));
  assert.equal(r.c.micStream, owned); assert.equal(r.sockets.length, 1);
}
// Old worklet completion is inert both with close/reopen and overlapping starts.
for (const close of [true, false]) {
  const r = rig(); const old = r.c.start(); r.tokens[0].resolve(token); await tick();
  const stale = stream(); r.media[0].resolve(stale); await tick();
  if (close) {r.c.open = false; r.c.teardownAudio(); r.c.open = true;}
  const next = r.c.start(); const owned = stream(); await r.finish(1, owned);
  const socket = r.c.ws; r.worklets[0].resolve(); await old; await next;
  assert(stale.tracks.every(t => t.stops >= 1)); assert.equal(r.c.micStream, owned);
  assert.equal(r.c.ws, socket); assert(!socket.closed);
}
// Reconnect token completions and socket events cannot replace a successor.
{
  const r = rig(); const initial = r.c.start(); await r.finish(0, stream()); await initial;
  const oldSocket = r.c.ws;
  const first = r.c.reconnect(), second = r.c.reconnect();
  r.tokens[2].resolve(token); await second; const currentSocket = r.c.ws;
  r.tokens[1].resolve(token); await first;
  assert.equal(r.c.ws, currentSocket);
  const count = r.phases.length; oldSocket.onclose({code: 1006}); oldSocket.onerror();
  assert.equal(r.phases.length, count);
  assert.equal(r.sockets.length, 2);
}
// Startup timeout releases its context, and a later permission stops its tracks.
{
  const r = rig(); let guardCalls = 0;
  r.c.stallGuard = p => ++guardCalls === 3 ? Promise.reject(Error('stalled')) : p;
  const pending = r.c.start(); r.tokens[0].resolve(token); await pending;
  assert.equal(r.c.micCtx, null);
  const stale = stream(); r.media[0].resolve(stale); await tick();
  assert(stale.tracks.every(t => t.stops >= 1)); assert.equal(r.sockets.length, 0);
}
// Failed token cleanup and rejected stale tokens cannot close a new engine.
{
  const r = rig(); const failed = r.c.start();
  r.tokens[0].resolve({ok: false, json: async () => ({error: 'unavailable'})}); await failed;
  assert.equal(r.c.micCtx, null); assert.equal(r.media.length, 0);
  const old = r.c.start(), next = r.c.start(); const owned = stream(); await r.finish(2, owned);
  r.tokens[1].reject(Error('stale network')); await old; await next;
  assert.equal(r.c.micStream, owned); assert(owned.tracks.every(t => t.stops === 0));
}
// A hosted caption token acquired before teardown cannot open in a new session.
{
  const r = rig(true); const old = r.c.start();
  r.tokens[0].resolve({ok: true, json: async () => ({value: 'hosted-ticket'})}); await tick();
  r.media[0].resolve(stream()); await tick(); r.worklets[0].resolve(); await old;
  assert.equal(r.tokens.length, 2); // caption request still waiting
  r.c.open = false; r.c.teardownAudio(); r.c.open = true;
  const next = r.c.start(); await r.finish(2, stream()); await next;
  const count = r.sockets.length; r.tokens[1].resolve(token); await tick();
  assert.equal(r.sockets.length, count); assert.equal(r.c.captionsWs, null);
}
// A delayed welcome completion cannot lift a newer session's mic hold.
{
  const r = rig(); const start = r.c.start(); await r.finish(0, stream()); await start;
  const greeting = deferred(); const ready = [];
  Object.assign(r.c, {LOCAL_LINES: [], warmPhrase() {}, GREETING_HOLD_MAX_MS: 6000,
    launchHoldUntil: 0, ripples: {settle() {}, ready() {ready.push('ready');}},
    streamTtsSentence: () => greeting.promise, waitForPlaybackEnd: async () => {},
    setTimeout() {}, turnAbort: null});
  r.c.deps.greeting = () => 'Hello'; r.c.ws.onopen();
  r.c.open = false; r.c.teardownAudio(); r.c.open = true;
  const next = r.c.start(); await r.finish(1, stream()); await next;
  r.c.launchHoldUntil = 123456; greeting.resolve(); await tick();
  assert.equal(r.c.launchHoldUntil, 123456); assert.deepEqual(ready, []);
}
function turnRig() {
  const chat = [], tts = [], readers = [], guards = [], effects = [], discarded = [], notifications = [];
  function reader() {
    const reads = [];
    const value = {reads, cancelled: 0, read() {const d = deferred(); reads.push(d); return d.promise;},
      cancel() {value.cancelled++; return Promise.resolve();}};
    readers.push(value); return value;
  }
  const c = {
    open: true, voiceSession: 'session-test', delivery: {interrupt() {}}, turnInFlight: null, turnAbort: null, spokenSinceCard: 0, turnThinking: false, thinkingNote: '',
    captionBuffer: '', bargeTimer: 0, localCaptionItem: null, userSpeaking: false, ws: null, phase: 'listening',
    liveSources: [], els: {reply: {textContent: ''}, transcript: {textContent: ''}},
    AbortController, TextDecoder, Date, Math, JSON, console, TURN_STALL_MS: 45000,
    clearHeld() {}, paintDiscard() {}, stopPlayback() {effects.push('stop');},
    setPhase(phase) {c.phase = phase;}, notify: x => notifications.push(x), refreshStage() {},
    turns: {discard() {}}, logHeard() {},
    setTimeout() {}, clearTimeout() {},
    deps: {
      onUserTurn: async () => ({}), buildChatBody: () => ({}),
      onAssistantTurn: payload => effects.push(payload), onDiscardTurn: id => discarded.push(id),
      async apiFetch(action, body, signal) {
        if (action === 'chat') {const d = deferred(); chat.push({d, body, signal}); return d.promise;}
        if (action === 'tts') {const d = deferred(); tts.push({d, signal}); return d.promise;}
        if (action === 'discard') {
          if (c.discardStatus === 'lost') throw Error('network');
          return {ok: true, json: async () => ({status: c.discardStatus || 'cancelled'})};
        }
      },
    },
    stallGuard(p) {const d = deferred(); guards.push(d); return Promise.race([p, d.promise]);},
    splitReadySentences: text => ({sentences: [text], rest: ''}), ttsVoice: 'af_heart',
    playPcm: () => effects.push('play'), speakPhrase: async () => {}, LOCAL_LINES: [],
  };
  vm.createContext(c);
  for (const name of ['runAssistantTurn', 'discardHeard', 'fetchTts', 'playTts']) vm.runInContext(extract(name), c);
  function send(r, payload) {
    r.reads.at(-1).resolve({done: false, value: new TextEncoder().encode(`data: ${JSON.stringify(payload)}\n\n`)});
  }
  async function response(index) {
    const r = reader(); chat[index].d.resolve({ok: true, body: {getReader: () => r}}); await tick(); return r;
  }
  return {c, chat, tts, readers, guards, effects, discarded, notifications, reader, send, response};
}
// A reader timeout aborts both transport and queued/playing TTS. Late bytes
// cannot restore the old UI or append history.
{
  const r = turnRig(); const running = r.c.runAssistantTurn('first'); await tick();
  const chatReader = await r.response(0);
  r.send(chatReader, {t: 'speak', text: 'Old speech.'}); await tick();
  const stalledChat = r.guards.at(-1);
  const ttsReader = r.reader(); r.tts[0].d.resolve({ok: true, body: {getReader: () => ttsReader}}); await tick();
  stalledChat.reject(Error('stalled')); await running;
  assert(r.chat[0].signal.aborted); assert(r.tts[0].signal.aborted);
  assert(chatReader.cancelled >= 1); assert(ttsReader.cancelled >= 1);
  assert(r.effects.includes('stop')); assert(!r.effects.includes('play'));
  const before = r.c.els.reply.textContent;
  ttsReader.reads[0].resolve({done: false, value: new Uint8Array([1, 0])});
  chatReader.reads.at(-1).resolve({done: false, value: new TextEncoder().encode('data: {"t":"done","reply":"late"}\n\n')});
  await tick(); assert.equal(r.c.els.reply.textContent, before);
  assert(!r.effects.some(x => x?.reply === 'late'));
  // The next turn still completes normally.
  const next = r.c.runAssistantTurn('next'); await tick(); const nextReader = await r.response(1);
  r.send(nextReader, {t: 'done', reply: 'New reply'}); await tick();
  nextReader.reads.at(-1).resolve({done: true}); await next;
  assert(r.effects.some(x => x?.reply === 'New reply'));
}
// Speech transport also has a bounded wait after the model stream completes.
{
  const r = turnRig(); const running = r.c.runAssistantTurn('speak'); await tick();
  const chatReader = await r.response(0);
  r.send(chatReader, {t: 'speak', text: 'Readable answer.'}); await tick();
  const ttsReader = r.reader(); r.tts[0].d.resolve({ok: true, body: {getReader: () => ttsReader}}); await tick();
  const stalledSpeech = r.guards.at(-1);
  r.send(chatReader, {t: 'done', reply: 'Readable answer.'}); await tick();
  chatReader.reads.at(-1).resolve({done: true}); await tick();
  stalledSpeech.reject(Error('stalled')); await running;
  assert(ttsReader.cancelled >= 1); assert.equal(r.c.els.reply.textContent, 'Readable answer.');
  assert(!r.effects.includes('play'));
}
// A stale failure cannot stop a newer turn or clear its in-flight identity.
{
  const r = turnRig(); const running = r.c.runAssistantTurn('speak'); await tick();
  const chatReader = await r.response(0);
  r.send(chatReader, {t: 'speak', text: 'Pending audio.'}); await tick();
  r.guards.at(-2).reject(Error('stalled')); await tick();
  assert(r.tts[0].signal.aborted); assert(!r.chat[0].signal.aborted);
  r.send(chatReader, {t: 'done', reply: 'Pending audio.'}); await tick();
  chatReader.reads.at(-1).resolve({done: true}); await running;
  r.tts[0].d.resolve({ok: true, body: {getReader: () => {throw Error('stale TTS acquired reader');}}});
  await tick(); assert(!r.effects.includes('play'));
}
// Cached-phrase fetches use the same child-request timeout ownership.
{
  const r = turnRig(); r.c.phraseCache = new Map(); vm.runInContext(extract('speakPhrase'), r.c);
  const parent = new AbortController(); const speech = r.c.speakPhrase('Sent.', parent.signal);
  r.guards[0].reject(Error('stalled'));
  await assert.rejects(speech, /stalled/);
  assert(r.tts[0].signal.aborted); assert(!parent.signal.aborted);
}
// A stale failure cannot stop a newer turn or clear its in-flight identity.
{
  const r = turnRig(); const old = r.c.runAssistantTurn('old'); await tick(); const oldReader = await r.response(0);
  const next = r.c.runAssistantTurn('next'); await tick(); const nextReader = await r.response(1);
  const identity = r.c.turnInFlight;
  oldReader.reads[0].reject(Error('old transport')); await old;
  assert.equal(r.c.turnInFlight, identity); assert(!r.chat[1].signal.aborted);
  r.send(nextReader, {t: 'done', reply: 'Current'}); await tick(); nextReader.reads.at(-1).resolve({done: true}); await next;
}
// Too-late and lost cancellation preserve submitted history and consume the
// surviving result silently. Accepted cancellation removes only its own ID.
for (const status of ['too_late', 'lost', 'cancelled']) {
  const r = turnRig(); r.c.discardStatus = status;
  const running = r.c.runAssistantTurn('write'); await tick(); const chatReader = await r.response(0);
  const id = r.c.turnInFlight.id; await r.c.discardHeard();
  if (status === 'cancelled') {
    assert.deepEqual(r.discarded, [id]); assert(r.chat[0].signal.aborted);
  } else {
    assert.deepEqual(r.discarded, []); assert(!r.chat[0].signal.aborted);
    r.send(chatReader, {t: 'done', reply: 'Ready', draft: {log_id: 'surviving'}}); await tick();
  }
  chatReader.reads.at(-1).resolve({done: true}); await running;
  if (status !== 'cancelled') assert(r.effects.some(x => x?.draft?.log_id === 'surviving'));
  assert(!r.effects.includes('play'));
}
console.log('Voice lifecycle, timeout, successor ownership and cancellation feedback regressions passed.');
