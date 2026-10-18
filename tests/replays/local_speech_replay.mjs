import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { createTurnAssembler } from '../../src/app/static/voice-turns.js';

const source = readFileSync(new URL('../../src/app/static/voice-live.js', import.meta.url), 'utf8');
const connection = 'function openRealtime(token)' + source.split('function openRealtime(token)')[1].split('\n  function attachRealtimeHandlers')[0];
const sockets = [];
const context = vm.createContext({
  connectionGeneration: 0, localSpeech: false, localCaptionItem: null, ttsVoice: 'marin', ws: null, captions: 0,
  WebSocket: function(url, protocols) { sockets.push({url, protocols}); },
  attachRealtimeHandlers() {},
});
vm.runInContext('function openCaptions() { captions++; }\n' + connection, context);
context.openRealtime({provider: 'local', websocket_url: 'wss://speech.example/v1/realtime', value: 'single-use', voice: 'af_heart'});
assert.equal(sockets[0].url, 'wss://speech.example/v1/realtime');
assert.equal(sockets[0].protocols[1], 'blam8-ticket.single-use');
assert.equal(context.captions, 0);
assert.equal(context.ttsVoice, 'af_heart');
assert.equal(context.localSpeech, true);
context.openRealtime({value: 'hosted-key'});
assert.equal(sockets[1].url, 'wss://api.openai.com/v1/realtime');
assert.equal(context.captions, 1);
assert.equal(context.ttsVoice, 'marin');

const utterances = [];
const events = [];
const turns = createTurnAssembler({transcriptionWaitMs: () => 15000,
  onUtterance: u => utterances.push(u), onEvent: name => events.push(name)});
turns.speechStarted({itemId: 'slow', now: 100});
turns.speechStopped({itemId: 'slow', now: 1000});
turns.committed({itemId: 'slow', now: 1000});
turns.tick(6000);
assert.equal(events.includes('salvaged'), false);
assert.equal(utterances.length, 0);
turns.completed({itemId: 'slow', transcript: 'Check the diary.', now: 6500});
turns.tick(7200);
assert.equal(utterances.length, 1);
assert.equal(utterances[0].text, 'Check the diary.');
turns.completed({itemId: 'slow', transcript: 'Check the diary.', now: 8000});
turns.tick(9000);
assert.equal(utterances.length, 1);
console.log('Local transport and delayed transcript replay passed.');

// Actual live socket handler: captions never enter the business assembler;
// failed finals clear a pending segment and replaced sockets lose ownership.
const handler = 'function attachRealtimeHandlers()' + source.split('function attachRealtimeHandlers()')[1].split('\n  function reloadIntoVoice')[0];
const called = [], caption = {textContent: ''};
const socket = {};
const c = vm.createContext({ws: socket, open: true, localSpeech: true, localCaptionItem: null,
  userSpeaking: false, phase: 'listening', speechStartedAt: 0, captionBuffer: '', bargeTimer: 0,
  gateOpenedAt: 0, CLIP_WINDOW_MS: 100, BARGE_OVER_AMBIENT: 2, BARGE_CONFIRM_MS: 100,
  els: {transcript: caption, status: {}}, delivery: {interrupt() {}}, micGated: () => false,
  liveSources: [], turnBusy: () => false, setPhase() {}, logHeard() {}, console: {warn() {}},
  turns: {hasPending: () => false, speechStarted: x => called.push(['start', x]),
    speechStopped: x => called.push(['stop', x]), committed: x => called.push(['commit', x]),
    completed: x => called.push(['final', x]), delta: x => called.push(['delta', x])},
  Date, JSON, setTimeout, clearTimeout});
vm.runInContext(handler, c);c.attachRealtimeHandlers();
const event = (type, extra = {}) => socket.onmessage({data: JSON.stringify({type, item_id: 'a', ...extra})});
event('input_audio_buffer.speech_started');
event('conversation.item.input_audio_transcription.partial', {transcript: 'Check Sam'});
event('conversation.item.input_audio_transcription.partial', {transcript: 'Check Sandra'});
assert.equal(caption.textContent, 'Check Sandra');
assert.equal(called.length, 1);
event('input_audio_buffer.speech_stopped');event('input_audio_buffer.committed');
event('conversation.item.input_audio_transcription.completed', {transcript: 'Check Sandra tomorrow'});
assert.equal(called.at(-1)[1].transcript, 'Check Sandra tomorrow');
event('conversation.item.input_audio_transcription.failed', {error: {code: 'busy'}});
assert.equal(called.at(-1)[1].transcript, '');
const count = called.length;c.ws = {};event('conversation.item.input_audio_transcription.completed', {transcript: 'stale'});
assert.equal(called.length, count);
// Missing final cannot act, and reset/discard rejects a late old item.
const missing = [];const t = createTurnAssembler({transcriptionWaitMs: () => 15000, onUtterance: u => missing.push(u)});
t.speechStarted({itemId:'lost',now:100});t.speechStopped({itemId:'lost',now:1000});t.committed({itemId:'lost',now:1000});
t.tick(17000);assert.equal(missing.filter(u => u.text).length,0);t.discard();
t.completed({itemId:'lost',transcript:'obsolete',now:18000});t.tick(19000);assert.equal(missing.filter(u => u.text).length,0);
assert.equal(context.connectionGeneration, 2);
console.log('Actual local handler caption/final/failure/stale connection replay passed.');

// The actual voice delivery TTS adapter buffers local PCM; intervening speech
// invalidates its quiet reservation before any playback or delivery start.
const {createTaskDelivery} = await import('../../src/app/static/task-delivery.js');
const adapterSource = source.split('    tts: async (text, signal) => {')[1].split('    play: async')[0];
let completeBody, now = 0, gap = true, played = 0, deliveryState = null;
const body = new Promise(resolve => {completeBody = resolve;});
const adapterContext = vm.createContext({Uint8Array, ttsVoice:'bf_emma', deps:{apiFetch:async(path,payload,signal)=>{
  assert.equal(path,'tts');assert.equal(payload.voice,'bf_emma');assert.equal(payload.text,'No change was executed.');
  return {ok:true,arrayBuffer:()=>body};}}});
const ttsAdapter = vm.runInContext('(async (text, signal) => {' + adapterSource.replace(/},\s*$/, '}') + ')', adapterContext);
const task = {id:'local-task', revision:1,event_id:'local-event',voice_session:'same-session',speakable:true,outcome:'No change was executed.'};
const scheduler = createTaskDelivery({now:()=>now,id:()=> 'attempt',gap:()=>gap,
  read:async()=>[{...task,delivery:{state:deliveryState}}],
  transition:async(job,op)=>{if(op==='claim')deliveryState='claimed';if(op==='release')deliveryState=null;return {ok:true,task};},
  tts:ttsAdapter,play:async()=>{played++;return true;}});
const flush=async()=>{for(let i=0;i<20;i++)await Promise.resolve();};
await scheduler.tick();now=600;const announcement=scheduler.tick();await flush();
gap=false;await scheduler.tick();gap=true;completeBody(new Uint8Array([1,0,2,0]).buffer);await announcement;
assert.equal(played,0);assert.equal(deliveryState,null);
console.log('Actual local PCM delivery adapter rejects audio across user activity.');
