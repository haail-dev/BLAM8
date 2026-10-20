import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {createNativeTransport,taskNotice} from '../../src/app/static/voice-gptlive.js';

const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const flush=async()=>{for(let i=0;i<30;i++)await Promise.resolve();};
function rig(pause='') {
  const gate=deferred(), calls=[], captions=[], statuses=[], playbacks=[], streams=[];
  const tracks=[0,1].map(()=>({enabled:true,stops:0,stop(){this.stops++;}}));
  const media={getTracks:()=>tracks,getAudioTracks:()=>tracks};streams.push(media);
  const stage=(name,value)=>pause===name?gate.promise:Promise.resolve(value);
  const channel={closed:false,close(){this.closed=true;}};
  const peer={iceGatheringState:pause==='ice'?'gathering':'complete',closed:false,
    addTrack(){},createDataChannel(label){assert.equal(label,'oai-events');return channel;},
    createOffer:()=>stage('offer',{type:'offer',sdp:'v=offer'}),
    setLocalDescription(offer){this.localDescription=offer;return stage('local');},
    setRemoteDescription(answer){assert.equal(answer.type,'answer');return stage('remote');},
    addEventListener(){},removeEventListener(){},close(){this.closed=true;},
  };
  const audio={muted:false,paused:0,srcObject:null,play:async()=>{},pause(){this.paused++;}};
  const response={ok:true,json:()=>stage('json',{session:'owned',transport:{sdp:'v=answer'}})};
  const controlResponse={ok:true};
  const transport=createNativeTransport({media:()=>stage('media',media),peer:()=>peer,audio:()=>audio,
    request:async(action,body)=>{calls.push([action,body]);return action==='session'?stage('request',response):controlResponse;},
    caption:e=>captions.push(e),status:s=>statuses.push(s),playback:s=>playbacks.push(s)});
  return {transport,gate,tracks,media,channel,peer,audio,response,controlResponse,calls,captions,statuses,playbacks};
}

// Every native acquisition await retains the opening that owns it.
for(const phase of ['media','offer','local','ice','request','json','remote']) {
  const r=rig(phase), start=r.transport.start('voice-uuid');await flush();
  const close=r.transport.close();await flush();
  const value=phase==='media'?r.media:phase==='offer'?{type:'offer',sdp:'v=offer'}:
    phase==='request'?r.response:phase==='json'?{session:'owned',transport:{sdp:'v=answer'}}:undefined;
  r.gate.resolve(value);await start;await close;
  assert(r.tracks.every(t=>t.stops>=1),phase);
  assert.equal(r.audio.srcObject,null,phase);
  if(phase!=='media')assert(r.peer.closed && r.channel.closed,phase);
  assert.equal(r.transport.session(),undefined,phase);
  if(['request','json'].includes(phase))assert(r.calls.some(([a,b])=>a==='control'&&b.session==='owned'),phase);
}

// Repeated start/paused clicks cannot replace an in-flight owner and leak media.
{
  const r=rig('media'),first=r.transport.start('first');
  await r.transport.start('second');r.gate.resolve(r.media);await first;
  assert.equal(r.calls.filter(([a])=>a==='session').length,1);
  assert.equal(r.calls.find(([a])=>a==='session')[1].voice_session,'first');
  await r.transport.close();
}

// UI events wait for started; browser delegation metadata never executes tools.
for(const phase of ['before-start','media-pending']) {
  const r=rig('media');if(phase==='before-start')r.transport.setMuted(true);
  const start=r.transport.start('muted-start');
  if(phase==='media-pending')r.transport.setMuted(true);
  r.gate.resolve(r.media);await start;
  assert(r.tracks.every(t=>!t.enabled),phase);
  await r.transport.resume();assert(r.tracks.every(t=>!t.enabled),phase);
  r.transport.setMuted(false);assert(r.tracks.every(t=>t.enabled));await r.transport.close();
}
{
  const r=rig();await r.transport.start('s');
  const event=x=>r.channel.onmessage({data:JSON.stringify(x)});
  event({type:'session.input_transcript.delta',delta:'premature'});assert.equal(r.captions.length,0);
  event({type:'session.started'});assert(r.transport.started());
  event({type:'session.input_transcript.delta',delta:'Hello',event_id:'caption',start_ms:0,end_ms:20});
  event({type:'session.delegation.created',delegation:{id:'forged',target:'client'},offset_ms:20});
  assert.equal(r.captions.length,1);assert.equal(r.calls.length,1);
  r.transport.setMuted(true);assert(r.tracks.every(t=>!t.enabled));
  r.transport.silence();assert(r.audio.muted);r.transport.resume();assert(!r.audio.muted);
  await r.transport.close();
  event({type:'session.input_transcript.delta',delta:'stale'});assert.equal(r.captions.length,1);
}

// Source slicing uses LF markers on both Windows and Unix checkouts.
const native=fs.readFileSync('src/app/static/voice-gptlive.js','utf8').replace(/\r\n/g,'\n');
assert.equal(taskNotice({result_kind:'waiting',proposal_id:'p',title:'Draft saved'}).group,'Pending');
assert.equal(taskNotice({result_kind:'action_completed',proposal_id:'p',title:'Verified action'}).state,'done');
assert.equal(taskNotice({result_kind:'lookup_answer',title:'Read only'}),null);
assert.equal(taskNotice({result_kind:'failed',proposal_id:'p',title:'Failed'}),null);
assert.equal(taskNotice({status:'working',title:'Not finished'}),null);
// Playback rejection is visible; explicit retry preserves microphone mute and guard latch.
{
  const r=rig();await r.transport.start('audio');let attempts=0;
  r.audio.play=()=>{attempts++;return Promise.reject(Object.assign(Error('private detail'),{name:'NotAllowedError'}));};
  r.peer.ontrack({streams:[r.media]});await flush();
  assert.match(r.statuses.at(-1),/blocked.*Retry audio/);
  assert(!r.statuses.join('').includes('private detail'));
  r.transport.silence();r.peer.ontrack({streams:[r.media]});await flush();assert.equal(attempts,1);
  r.transport.setMuted(true);r.audio.play=async()=>{};await r.transport.resume();
  assert.match(r.statuses.at(-1),/Speaker enabled/);assert(r.tracks.every(t=>!t.enabled));
  const obsolete=deferred();r.audio.play=()=>obsolete.promise;r.transport.resume();
  r.transport.silence();r.audio.play=async()=>{};await r.transport.resume();
  const enabledCount=r.statuses.length;obsolete.reject(Error('obsolete blocked'));await flush();
  assert.equal(r.statuses.length,enabledCount);assert.match(r.statuses.at(-1),/Speaker enabled/);
  const late=deferred();r.audio.play=()=>late.promise;r.transport.resume();
  await r.transport.close();const count=r.statuses.length;late.reject(Error('late'));await flush();
  assert.equal(r.statuses.length,count);assert.equal(r.audio.srcObject,null);
}
const controlSource='async function control(operation) {'+native.split('  async function control(operation) {')[1].split('  function ensureDom()')[0];
// Audio-only interruption and uncertain discard preserve backend work; only a confirmed cancellation ends its turn.
for(const mode of ['interrupt','cancelled','too_late','failed','reopened']) {
  const response=deferred(), discarded=[], statuses=[];let visualPaints=0;
  const c=vm.createContext({els:{activity:{textContent:'Checking business data'}},open:true,generation:1,lastTurn:'old',terminalTurn:false,activities:new Map([['old',{state:'running'}],['other',{state:'running'}]]),knownTurns:new Set(['old','other']),finishedTurns:new Set(),paintFeed(){visualPaints++},transport:{silence(){},session:()=> 'owned'},
    boundedRequest:()=>response.promise,status:s=>statuses.push(s),deps:{onDiscardTurn:id=>discarded.push(id)}});
  vm.runInContext(controlSource,c);const run=c.control(mode==='interrupt'?'interrupt':'discard');
  assert.equal(c.activities.has('old'),true);assert.equal(c.activities.has('other'),true);
  assert.equal(c.finishedTurns.size,0);
  if(mode==='reopened'){c.generation=2;c.lastTurn='new';}
  response.resolve({response:{ok:mode!=='failed'},data:{turns:[{turn_id:'old',status:mode==='reopened'?'cancelled':mode}]}});
  await run;assert.equal(c.activities.has('other'),true);assert.equal(c.activities.has('old'),mode!=='cancelled');
  assert.equal(c.finishedTurns.has('old'),mode==='cancelled');assert.equal(c.finishedTurns.has('other'),false);
  assert.equal(c.terminalTurn,mode==='cancelled');assert.equal(visualPaints,mode==='cancelled'?1:0);
  assert.deepEqual(discarded,mode==='cancelled'?['old']:[]);
  if(mode==='reopened')assert.equal(statuses.length,0);
}

const panel=fs.readFileSync('src/app/static/concierge_panel.js','utf8').replace(/\r\n/g,'\n');
const inner='async function openVoiceLiveInner(opts) {'+panel.split('    async function openVoiceLiveInner(opts) {')[1].split('    // ── the composer')[0];
const rewritten=inner.replace(/await (?:openingDeadline|moduleDeadline)\(import\([^;]+\)\)/,'await loadModule(requestedMode)');
function panelRig(mode = "gptlive") {
  const module=deferred();let created=0,opened=0;
  const c={historyReady:Promise.resolve(),voiceLiveInstance:null,voiceInstances:{},voiceModeGeneration:0,historyLifecycle:0,historyReadGeneration:0,savedVoiceMode:()=> mode,
    timing(){},releasePreMic(){},document:{querySelector:()=>null},loadModule:()=>module.promise,
    voiceUserTurn(){},voiceAssistantTurn(){},voiceDiscardTurn(){},voiceGreeting(){},voiceRenderStage(){},mountBadge(){},loadWaiting(){},openSettingsFromVoice(){},
    module,create:()=>{created++;return {open(){opened++;}};}};
  vm.createContext(c);vm.runInContext(rewritten,c);
  return {c,module,get created(){return created;},get opened(){return opened;}};
}
// Same-generation repeated mic taps coalesce after the dynamic import await.
{
  const r=panelRig(),a=r.c.openVoiceLiveInner(),b=r.c.openVoiceLiveInner();
  r.module.resolve({createVoiceLive:r.c.create});await Promise.all([a,b]);
  assert.equal(r.created,1);assert.equal(r.opened,2);
  r.c.voiceLiveInstance=null;
  await r.c.openVoiceLiveInner();assert.equal(r.created,1);assert.equal(r.opened,3);
}
// Switching mode while an import is pending prevents the obsolete instance.
{
  const r=panelRig(),a=r.c.openVoiceLiveInner();r.c.voiceModeGeneration++;
  r.module.resolve({createVoiceLive:r.c.create});await a;assert.equal(r.created,0);
}
// Standard voice waits for restored context; changing mode during that wait
// prevents an obsolete voice instance from opening.
{
  const r=panelRig('standard'),history=deferred();let loads=0;
  r.c.historyReady=history.promise;
  r.c.loadModule=()=>{loads++;return r.module.promise;};
  const opening=r.c.openVoiceLiveInner();
  await Promise.resolve();assert.equal(loads,0);
  r.c.voiceModeGeneration++;
  history.resolve();await opening;
  assert.equal(loads,0);assert.equal(r.opened,0);
}
{
  const r=panelRig('standard'),history=deferred();let loads=0;
  r.c.historyReady=history.promise;
  r.c.loadModule=()=>{loads++;return r.module.promise;};
  const opening=r.c.openVoiceLiveInner();
  await Promise.resolve();assert.equal(loads,0);
  history.resolve();await Promise.resolve();await Promise.resolve();
  assert.equal(loads,1);
  r.module.resolve({createVoiceLive:r.c.create});await opening;
  assert.equal(r.opened,1);
}
// Preference is per authenticated user/device and defaults to GPT-Live when available.
const preference='function savedVoiceMode() {'+panel.split('    function savedVoiceMode() {')[1].split('    root.querySelectorAll')[0];
const values=new Map();
for(const user of ['one','two']) {
  const c=vm.createContext({VOICE_MODE_KEY:'blam8.voiceMode:'+user,voiceModeChoice:null,GPTLIVE_ON:true,localStorage:{getItem:k=>values.get(k)}});
  vm.runInContext(preference,c);assert.equal(c.savedVoiceMode(),'gptlive');
  if(user==='one'){values.set('blam8.voiceMode:one','gptlive');assert.equal(c.savedVoiceMode(),'gptlive');}
  else {values.set('blam8.voiceMode:two','standard');assert.equal(c.savedVoiceMode(),'standard');values.set('blam8.voiceMode:two','invalid');assert.equal(c.savedVoiceMode(),'gptlive');}
}
{
  const c=vm.createContext({VOICE_MODE_KEY:'user',voiceModeChoice:'gptlive',GPTLIVE_ON:true,localStorage:{getItem(){throw Error('blocked');}}});
  vm.runInContext(preference,c);assert.equal(c.savedVoiceMode(),'gptlive');
}
{
  const c=vm.createContext({VOICE_MODE_KEY:'user',voiceModeChoice:null,GPTLIVE_ON:false,localStorage:{getItem(){return null;}}});
  vm.runInContext(preference,c);assert.equal(c.savedVoiceMode(),'standard');
}
console.log('Native acquisition/close/data-channel, discard ownership and mode import/preference replays passed.');

// A real adapter timeout must also release media that resolves after failure.
{
  const realSet=globalThis.setTimeout,realClear=globalThis.clearTimeout,timers=new Map();let id=0;
  globalThis.setTimeout=fn=>{timers.set(++id,fn);return id;};globalThis.clearTimeout=i=>timers.delete(i);
  try {
    const r=rig('media'),start=r.transport.start('late');await flush();
    [...timers.values()][0]();await start;
    r.gate.resolve(r.media);await flush();assert(r.tracks.every(t=>t.stops>=1));
    assert.equal(r.transport.session(),undefined);
    assert(r.calls.some(([a,b])=>a==='control'&&b.session==='late'));
  } finally {globalThis.setTimeout=realSet;globalThis.clearTimeout=realClear;}
}

// Actual bounded UI fetch/body helper aborts on timeout and opening teardown.
const bounded='async function boundedRequest(url, body, g) {'+native.split('  async function boundedRequest(url, body, g) {')[1].split('  let els =')[0];
for(const reason of ['timeout','close','late-body']) {
  const timers=new Map(),requests=new Set(),body=deferred();let id=0,signal;
  const c=vm.createContext({open:true,generation:1,requests,AbortController,
    setTimeout:fn=>{timers.set(++id,fn);return id;},clearTimeout:i=>timers.delete(i),
    fetch:async(url,opts)=>{signal=opts.signal;return {json:()=>reason==='late-body'?body.promise:new Promise((_,reject)=>signal.addEventListener('abort',()=>reject(Error('aborted'))))};},deps:{}});
  vm.runInContext(bounded,c);const pending=c.boundedRequest('/events',null,1);await flush();
  if(reason==='timeout')[...timers.values()][0]();
  if(reason==='close'){c.open=false;c.generation=2;for(const controller of requests)controller.abort();}
  if(reason==='late-body'){c.generation=2;body.resolve({events:[{kind:'result',payload:'obsolete'}]});}
  await assert.rejects(pending);assert.equal(requests.size,0);
  if(reason!=='late-body')assert(signal.aborted);
}
console.log('Native timeout late-media and bounded poll/body abort replays passed.');


// Actual snapshot reader: revisions, duplicates, permission removals and old sessions.
const taskReader='async function readTasks(g) {'+native.split('  async function readTasks(g) {')[1].split('  async function poll(g)')[0];
{
  let now=4000, rows=[], delivered=[];
  const c=vm.createContext({open:true,generation:1,session:'current',taskReading:false,tasksSeeded:false,tasksFresh:false,tasksAt:0,taskHint:true,taskSettled:t=>t.status==='done'||t.stale==='expired',tasks:new Map(),requests:new Set(),
    Date:{now:()=>now},AbortController,setTimeout,clearTimeout,paintTasks(){},els:{taskNote:{}},
    taskNotice:()=>null,addNotice(){},flash(){},deps:{taskRead:async()=>rows,onNativeTasks:value=>delivered.push(value)}});
  vm.runInContext(taskReader,c);
  const base={id:'one',voice_session:'current',revision:2,row_version:4,status:'waiting',result_kind:'waiting'};
  rows=[base];await c.readTasks(1);assert.equal(c.tasks.get('one').revision,2);
  now+=4000;rows=[{...base,revision:1,row_version:9},{id:'foreign',voice_session:'other',revision:1}];await c.readTasks(1);
  assert.equal(c.tasks.get('one').revision,2);assert(!c.tasks.has('foreign'));assert.equal(delivered.at(-1).length,1);
  now+=4000;rows=[{...base,voice_session:'other'}];await c.readTasks(1);
  assert.equal(c.tasks.size,0);assert.equal(delivered.at(-1).length,0);
  now+=4000;c.taskHint=true;rows=[base];await c.readTasks(1);
  now+=4000;rows=[];await c.readTasks(1);assert.equal(c.tasks.size,0);
  // Idle: nothing live and no stream hint means no request at all.
  let reads=0;c.deps.taskRead=async()=>{reads++;return rows;};
  now+=4000;await c.readTasks(1);assert.equal(reads,0);
  c.taskHint=true;now+=4000;await c.readTasks(1);assert.equal(reads,1);assert.equal(c.taskHint,false);
  // Expired rows are settled: they stop the safety-net poll.
  rows=[{...base,stale:'expired'}];c.taskHint=true;now+=4000;await c.readTasks(1);assert.equal(reads,2);
  now+=4000;await c.readTasks(1);assert.equal(reads,2);
  now+=4000;c.taskHint=true;c.tasks=new Map();
  now+=4000;c.deps.taskRead=()=>Promise.reject(Error('read unavailable'));await c.readTasks(1);
  assert.match(c.els.taskNote.textContent,/last checked state/);
}
const taskHelpers=native.slice(native.indexOf('export function sameTaskVersion'),native.indexOf('export function createVoiceLive')).replaceAll('export function','function');
{
 const c=vm.createContext({});vm.runInContext(taskHelpers,c);
 const task={id:'one',voice_session:'s',revision:1,row_version:3,proposal_id:'p',result_kind:'waiting'};
 assert(c.sameTaskVersion(task,{...task}));assert(!c.sameTaskVersion(task,{...task,revision:2}));assert(!c.sameTaskVersion(task,null));
 assert.equal(c.taskPresentation({status:'done',result_kind:'unknown'}).icon,'?');
 assert.equal(c.taskBannerText(1,2,0),'1 needs you · 2 working');assert.equal(c.taskBannerText(0,0,1),'1 stopped');
 assert.equal(c.taskTier({result_kind:'waiting'}),0);assert.equal(c.taskTier({priority:3,result_kind:'waiting'}),3);
 assert(c.taskSettled({stale:'stalled',status:'working'}));assert(!c.taskSettled({status:'working'}));
 assert.match(c.taskPresentation({status:'waiting',stale:'nudge',age_seconds:2400}).label,/waiting 40m/);
 assert.match(c.taskPresentation({status:'waiting',stale:'expired'}).label,/Expired/);
 assert.equal(c.taskPresentation({status:'done',result_kind:'action_completed'}).icon,'✓');
 assert.equal(c.taskPresentation({status:'working',proposal_state:'executing'}).label,'Still running — result not confirmed');
}
console.log('Current scoped task snapshots, stale revisions, removals, read failure and stable async card binding passed.');


// Hidden Standard captions never turn held, uncertain speech into an automatic dispatch.
const standard=fs.readFileSync('src/app/static/voice-live.js','utf8').replace(/\r\n/g,'\n');
const held=standard.slice(standard.indexOf('  function holdWords'),standard.indexOf('  function sendHeld'));
{
 const notices=[],logged=[];
 const c=vm.createContext({heldWords:null,els:{transcript:{classList:{add(){},remove(){}},setAttribute(){},removeAttribute(){}}},notify:text=>notices.push(text),logHeard:(...value)=>logged.push(value)});
 vm.runInContext(held,c);
 for(const reason of ['quiet','unlikely','mid_thought']){c.holdWords({text:'Uncertain words'},reason);assert.match(notices.at(-1),/repeat/);assert(!notices.at(-1).includes('tap the words'));c.clearHeld();assert.equal(c.heldWords,null);}
 assert.equal(logged.length,3);
}
console.log('Standard hidden-caption held speech retains gates and prompts repeat without dispatch.');


// A close during provider creation must settle that acquisition before any fresh create.
{
  const r=rig('request');const first=r.transport.start('old');await flush();
  r.peer.connectionState='connected';r.channel.readyState='open';
  let settled=false;const closing=r.transport.close('end').then(()=>{settled=true;});
  const next=r.transport.start('fresh');await flush();
  assert(!settled);assert.equal(r.calls.filter(([a])=>a==='session').length,1);
  assert(r.tracks.every(t=>t.stops>0));
  const initial=r.calls.find(([a,b])=>a==='control'&&b.operation==='close')[1];
  assert.equal(initial.close_reason,'end');assert.equal(initial.startup_stage,'create');assert.equal(initial.peer_state,'connected');
  r.channel.onclose();await flush();assert.equal(r.calls.filter(([a])=>a==='session').length,1);
  r.gate.resolve(r.response);await first;await closing;await next;
  assert.equal(r.calls.filter(([a])=>a==='session').length,2);
  assert(r.calls.some(([a,b])=>a==='control'&&b.session==='owned'&&b.close_reason==='end'));
  await r.transport.close('mode_switch');
}
console.log('Pending-create close/start serialization and first-trigger telemetry passed.');


// Deadline is not proof that the underlying create vanished: clean up a late answer, never attach/retry.
for(const phase of ['request','json']){
 const realSet=globalThis.setTimeout,realClear=globalThis.clearTimeout,timers=new Map();let id=0;
 globalThis.setTimeout=fn=>{timers.set(++id,fn);return id;};globalThis.clearTimeout=i=>timers.delete(i);
 try{
  const r=rig(phase),pending=r.transport.start('timeout-owned');await flush();
  [...timers.values()][0]();await pending;assert(!r.transport.active());
  await r.transport.start('must-wait');assert.equal(r.calls.filter(([a])=>a==='session').length,1);assert(r.transport.closing());
  r.gate.resolve(phase==='request'?r.response:{session:'late-owned',transport:{sdp:'v=late'}});await flush();
  assert(r.calls.some(([a,b])=>a==='control'&&b.session===(phase==='request'?'owned':'late-owned')));
  assert.equal(r.calls.filter(([a])=>a==='session').length,1);assert(r.peer.closed);
  assert(!r.transport.closing());await r.transport.start('explicit-fresh');assert.equal(r.calls.filter(([a])=>a==='session').length,2);await r.transport.close();
 }finally{globalThis.setTimeout=realSet;globalThis.clearTimeout=realClear;}
}
console.log('Timed-out provider exchanges retain late answers solely for owned cleanup.');

// Historical readiness survives guard silence/disconnect, whichever prerequisite arrives last.
for(const last of ['started','playing','peer','channel']) {
  const r=rig();await r.transport.start('readiness');
  const transitions={
    started:()=>r.channel.onmessage({data:JSON.stringify({type:'session.started'})}),
    playing:async()=>{r.peer.ontrack({streams:[r.media]});await flush();},
    peer:()=>{r.peer.connectionState='connected';r.peer.onconnectionstatechange();},
    channel:()=>{r.channel.readyState='open';r.channel.onopen();},
  };
  for(const name of Object.keys(transitions).filter(name=>name!==last))await transitions[name]();
  assert(!r.transport.ready(),last);await transitions[last]();assert(r.transport.ready(),last);
  r.transport.silence('guard');assert(!r.transport.ready());
  r.transport.silence('connection');await r.transport.recoverConnection();assert(r.audio.muted);
  // Existing explicit resume clears guard without changing microphone ownership.
  await r.transport.resume();assert(!r.audio.muted);assert(r.transport.ready());
  r.transport.silence('guard');r.peer.connectionState='disconnected';r.peer.onconnectionstatechange();
  await r.transport.settled();
  const report=r.calls.find(([a,b])=>a==='control'&&b.operation==='close')[1];
  assert.equal(report.reached_ready,true);assert.equal(report.startup_stage,'awaiting_ready');
  assert.equal(report.playback_state,'muted');assert.equal(report.peer_state,'disconnected');
  await r.transport.start('fresh');await r.transport.close();
  assert.equal(r.calls.filter(([a,b])=>a==='control'&&b.operation==='close').at(-1)[1].reached_ready,false);
}
for(const missing of ['started','playing','peer','channel']) {
  const r=rig();await r.transport.start('never-ready');
  if(missing!=='started')r.channel.onmessage({data:JSON.stringify({type:'session.started'})});
  if(missing!=='peer'){r.peer.connectionState='connected';r.peer.onconnectionstatechange();}
  if(missing!=='channel'){r.channel.readyState='open';r.channel.onopen();}
  if(missing!=='playing'){r.peer.ontrack({streams:[r.media]});await flush();}
  assert(!r.transport.ready());await r.transport.close();
  const report=r.calls.find(([a,b])=>a==='control'&&b.operation==='close')[1];
  assert.equal(report.reached_ready,false,missing);
}
console.log('Historical readiness, incomplete startup, fresh-session reset and explicit guard release passed.');

// The close report says what the microphone was actually given and the loudness shape of the
// room (counts of RMS buckets, never audio): measurement only, nothing is gated on it.
{
  const realInterval=globalThis.setInterval,realClearInterval=globalThis.clearInterval,realContext=globalThis.AudioContext;
  const ticks=new Map();let id=0,level=.05,context=null;
  globalThis.setInterval=fn=>{ticks.set(++id,fn);return id;};
  globalThis.clearInterval=i=>ticks.delete(i);
  globalThis.AudioContext=class{
    constructor(){context=this;this.state='running';}
    createMediaStreamSource(){return {connect(){},disconnect(){}};}
    createAnalyser(){return {fftSize:0,getFloatTimeDomainData(samples){samples.fill(level);},disconnect(){}};}
    resume(){return Promise.resolve();}close(){return Promise.resolve();}
  };
  const tick=()=>{for(const fn of [...ticks.values()])fn();};
  try {
    const r=rig();
    r.tracks[0].getSettings=()=>({echoCancellation:true,noiseSuppression:true,autoGainControl:true,channelCount:1,sampleRate:48000,deviceId:'private-device',label:'Private microphone'});
    await r.transport.start('levels');
    assert.equal(ticks.size,1,'one sampler while the conversation is owned');
    tick();                                                    // not started yet: nothing is counted
    r.channel.onmessage({data:JSON.stringify({type:'session.started'})});
    tick();tick();                                             // RMS .05 -> the .04-.08 bucket
    level=.7;tick();                                           // the top bucket
    level=0;tick();                                            // the bottom bucket
    r.transport.setMuted(true);level=.3;tick();                // a muted mic is not sampled
    r.transport.setMuted(false);
    context.state='suspended';tick();                          // unreadable: the ninth count
    await r.transport.close('end');
    assert.equal(ticks.size,0,'the sampler stops with the conversation');
    const report=r.calls.find(([a,b])=>a==='control'&&b.operation==='close')[1];
    assert.deepEqual(report.levels,[1,0,0,2,0,0,0,1,1]);
    assert.deepEqual(report.capture,{echoCancellation:true,noiseSuppression:true,autoGainControl:true,channelCount:1,sampleRate:48000});
    assert(!JSON.stringify(report).includes('private-device') && !JSON.stringify(report).includes('Private microphone'));
  } finally {globalThis.setInterval=realInterval;globalThis.clearInterval=realClearInterval;globalThis.AudioContext=realContext;}
}
// A browser that cannot say what it opened, or a settings read that throws, leaves capture null; levels still report.
const unknownCapture={echoCancellation:null,noiseSuppression:null,autoGainControl:null,channelCount:null,sampleRate:null};
for(const [getSettings,expected] of [[undefined,null],[()=>{throw Error('settings unavailable');},null],
    [()=>({echoCancellation:'all',channelCount:1.5,sampleRate:-1}),unknownCapture]]) {
  const r=rig();if(getSettings)r.tracks[0].getSettings=getSettings;
  await r.transport.start('capture-unknown');await r.transport.close('end');
  const report=r.calls.find(([a,b])=>a==='control'&&b.operation==='close')[1];
  assert.equal(report.levels.length,9);
  assert.deepEqual(report.capture,expected);
}
console.log('Close report capture settings and microphone level buckets passed.');

// Missing readiness events and a permanently pending play release local media.
for(const missing of ['started','playing','peer','channel','blocked-without-started']) {
 const realSet=globalThis.setTimeout,realClear=globalThis.clearTimeout,timers=new Map();let id=0;
 globalThis.setTimeout=fn=>{timers.set(++id,fn);return id;};globalThis.clearTimeout=i=>timers.delete(i);
 try {
  const r=rig();await r.transport.start('ready-timeout');
  if(!['started','blocked-without-started'].includes(missing))r.channel.onmessage({data:JSON.stringify({type:'session.started'})});
  if(missing!=='peer'){r.peer.connectionState='connected';r.peer.onconnectionstatechange();}
  if(missing!=='channel'){r.channel.readyState='open';r.channel.onopen();}
  if(missing==='playing')r.audio.play=()=>new Promise(()=>{});
  if(missing==='blocked-without-started')r.audio.play=()=>Promise.reject(Error('blocked'));
  r.peer.ontrack({streams:[r.media]});await flush();
  const expire=[...timers.values()][0];assert(expire,missing);expire();await flush();
  assert(!r.transport.active(),missing);assert(r.tracks.every(t=>t.stops>0),missing);
  r.channel.onmessage({data:JSON.stringify({type:'session.started'})});assert(!r.transport.ready());
  await r.transport.start('fresh-ready');expire();assert(r.transport.active(),'old timer cannot close new owner');
  assert(r.transport.startupTimings().every(x=>Number.isFinite(x.duration_ms) && x.duration_ms>=0));
  await r.transport.close();
 }finally{globalThis.setTimeout=realSet;globalThis.clearTimeout=realClear;}
}

// A lost create response permits explicit recovery only on server-confirmed
// settled ownership. The retained late answer remains cleanup-only.
{
 const realSet=globalThis.setTimeout,realClear=globalThis.clearTimeout,timers=new Map();let id=0;
 globalThis.setTimeout=fn=>{timers.set(++id,fn);return id;};globalThis.clearTimeout=i=>timers.delete(i);
 try {
  const r=rig('request'),pending=r.transport.start('lost-create');await flush();
  [...timers.values()][0]();await pending;
  // Existing rig's non-JSON control is unconfirmed, so reconnect stays blocked.
  await r.transport.start('fresh');assert(r.transport.closing());
  r.controlResponse.json=async()=>({ok:true,retry_allowed:false});
  await r.transport.start('fresh');assert.equal(r.calls.filter(([a])=>a==='session').length,1);
  r.controlResponse.json=async()=>({ok:true,retry_allowed:true});
  const fresh=r.transport.start('fresh');await flush();
  assert.equal(r.calls.filter(([a])=>a==='session').length,2);
  r.gate.resolve(r.response);await flush();assert(!r.transport.closing());
  await fresh;assert(r.transport.active());
  assert(r.calls.some(([a,b])=>a==='control'&&b.session==='owned'));
  await r.transport.close();
 }finally{globalThis.setTimeout=realSet;globalThis.clearTimeout=realClear;}
}
console.log('Readiness deadlines, pending playback, stale events and unconfirmed cleanup recovery passed.');

async function test_guard_resume_lifecycle() {
  const r=rig();await r.transport.start('guard-lifecycle');
  let rows=[{kind:'guard',seq:1,text:'Unverified'}];
  const pollSource='async function poll(g) {'+native.split('  async function poll(g) {')[1].split('  async function control(operation)')[0];
  const c=vm.createContext({transport:r.transport,open:true,generation:1,polling:false,after:0,
    activities:new Map(),knownTurns:new Set(),tasksFresh:true,flash(){},status(){},readTasks(){},
    boundedRequest:async()=>({response:{ok:true},data:{events:rows}})});
  vm.runInContext(pollSource,c);
  await c.poll(1);assert(r.audio.muted);
  rows=[];await c.poll(1);assert(r.audio.muted); // Polling cannot clear guard.
  r.transport.silence('user');r.transport.silence('connection');
  await r.transport.resume();assert(r.audio.muted); // Explicit resume removes only user/guard.
  await r.transport.recoverConnection();assert(!r.audio.muted);
  await r.transport.close();
  const report=r.calls.find(([a,b])=>a==='control'&&b.operation==='close')[1];
  const events=report.playback_events;
  assert.deepEqual(events.filter(e=>e.action==='silence').map(e=>e.reason),['guard','user','connection']);
  assert.equal(events.find(e=>e.reason==='guard').guard_seq,1);
  const resume=events.find(e=>e.action==='resume');
  assert.deepEqual(resume.silence_reasons,['connection']);assert.equal(resume.playback_state,'muted');
  assert.equal(resume.guard_seq,1);assert.deepEqual(report.silence_reasons,[]);
  assert(events.every(e=>Number.isFinite(Date.parse(e.observed_at))));
  assert(events.every((e,i)=>i===0 || e.sequence>events[i-1].sequence));
}
await test_guard_resume_lifecycle();

// Resume audio is for a live session only (Kris 2026-10-14: after "Conversation ended" the
// button did nothing and Mist had to be held): once the session is gone the panel is told to hide it.
async function test_resume_audio_hides_once_the_conversation_has_ended() {
  const r=rig();await r.transport.start('resume-ended');
  r.channel.onmessage({data:JSON.stringify({type:'session.started'})});
  r.transport.silence('guard');
  assert.equal(r.playbacks.at(-1),'muted');           // a live, guard-muted session still offers Resume audio
  r.channel.onmessage({data:JSON.stringify({type:'session.closed'})});await r.transport.settled();
  assert(r.statuses.includes('Conversation ended. Hold Mist to start again; results remain in chat.'));
  assert.equal(r.playbacks.at(-1),'closed');
  assert.equal(r.transport.active(),false);
  const muted=r.audio.muted;await r.transport.resume();assert.equal(r.audio.muted,muted);
}
await test_resume_audio_hides_once_the_conversation_has_ended();

async function test_historical_readiness() {
  const r=rig();await r.transport.start('channel-close');
  r.channel.onmessage({data:JSON.stringify({type:'session.started'})});
  r.peer.connectionState='connected';r.peer.onconnectionstatechange();
  r.channel.readyState='open';r.channel.onopen();r.peer.ontrack({streams:[r.media]});await flush();
  assert(r.transport.ready());r.transport.silence('guard',9);
  r.channel.readyState='closed';r.channel.onclose();await r.transport.settled();
  const report=r.calls.find(([a,b])=>a==='control'&&b.operation==='close')[1];
  assert.equal(report.reached_ready,true);assert.equal(report.channel_state,'closed');
  assert.equal(report.peer_state,'connected');assert.equal(report.playback_state,'muted');
  assert.deepEqual(report.silence_reasons,['guard']);
  assert.equal(report.playback_events.at(-1).guard_seq,9);
}
await test_historical_readiness();
{
  const r=rig();await r.transport.start('bounded-history');
  for(let i=0;i<40;i++){r.transport.silence('guard',i+1);await r.transport.resume();}
  await r.transport.close();
  const report=r.calls.find(([a,b])=>a==='control'&&b.operation==='close')[1];
  assert.equal(report.playback_events.length,32);assert(report.playback_events_omitted>0);
  const frozen=JSON.stringify(report);await r.transport.resume();assert.equal(JSON.stringify(report),frozen);
}
console.log('test_guard_resume_lifecycle and test_historical_readiness: poll, silence reasons, bounded transition history passed.');
// The actual panel loader must bound headers AND body, permit retry, and never
// mount a response which completes after its deadline (even ignoring abort).
const deadlineSource = panel.slice(panel.indexOf('  function openingDeadline('), panel.indexOf('  function csrfToken('));
const loadSource = panel.slice(panel.indexOf('  function load() {'), panel.indexOf('  function reloadPanel()'));
for (const stall of ['headers', 'body']) {
  const gate = deferred(), clock = new Map(), marks = [], children = [];
  let mounts = 0, calls = 0, signal;
  const slot = {innerHTML:'',textContent:'',querySelector:()=>({}),appendChild:n=>children.push(n)};
  const c = vm.createContext({api:null, loading:null, slot, AbortController,
    setTimeout:fn=>{clock.set(1,fn);return 1;},clearTimeout:id=>clock.delete(id),
    timing:name=>marks.push(name),open(){},
    document:{createElement:()=>({appendChild(n){this.child=n;},addEventListener(){}})},
    mount:()=>{mounts++;return {mounted:true};},
    fetch:(_, options)=>{
      signal=options.signal;calls++;
      const response={ok:true,status:200,text:()=>stall==='body'&&calls===1?gate.promise:Promise.resolve('<panel>')};
      return stall==='headers'&&calls===1?gate.promise:Promise.resolve(response);
    },
  });
  vm.runInContext(deadlineSource+loadSource,c);
  const first=c.load();assert.equal(first,c.load());await flush();
  clock.get(1)();assert.equal(await first,null);assert(signal.aborted);
  assert.equal(mounts,0);assert.equal(children[0].child.textContent,'Try again');
  await c.load();assert.equal(mounts,1);assert.equal(calls,2);
  gate.resolve(stall==='headers'?{ok:true,status:200,text:async()=>'<obsolete>'}:'<obsolete>');
  await flush();assert.equal(mounts,1);assert.equal(slot.innerHTML,'<panel>');
  assert.equal(clock.size,0);assert(marks.includes('panel.open-error'));
}
console.log('Panel headers/body deadline, coalesced load, retry and late-response ownership passed.');

// Execute the worker with controlled CacheStorage/network lifetimes. Responses
// may settle before writes; waitUntil must still retain the write, including a
// failed write. Search parameters must never be ignored or cross-cache matched.
const workerSource=fs.readFileSync('src/app/static/sw.js','utf8').replace(/\r\n/g,'\n');
function workerRig() {
  const handlers={}, entries=new Map(), writes=[], requests=[], put=deferred();
  let broken=false,offline=false;
  const cache={match:async(req,opts)=>{assert.equal(opts?.ignoreSearch,false);if(broken)throw Error('cache failure');return entries.get(req.url);},
    put:(req,body)=>{writes.push([req.url,body]);return put.promise;},keys:async()=>[]};
  const c=vm.createContext({URL,performance,Response,
    self:{location:{origin:'https://fixture.test'},addEventListener:(name,fn)=>handlers[name]=fn},
    caches:{open:async name=>{assert.equal(name,'blam8-v4');return cache;},match:()=>{throw Error('Cross-cache lookup forbidden');},keys:async()=>['blam8-v4']},
    fetch:async(req,opts)=>{requests.push([req.url,opts]);if(offline)throw Error('offline');return new Response('network');},
  });
  vm.runInContext(workerSource,c);
  const dispatch=(url,method='GET')=>{
    let response;const waits=[];
    handlers.fetch({request:{url:'https://fixture.test'+url,method},respondWith:p=>response=p,waitUntil:p=>waits.push(p)});
    return {response,waits};
  };
  return {dispatch,entries,writes,requests,put,handlers,set broken(v){broken=v;},set offline(v){offline=v;}};
}
{
  const r=workerRig();r.entries.set('https://fixture.test/static/a.js?v=old',new Response('old'));
  const e=r.dispatch('/static/a.js?v=new');assert.equal(await (await e.response).text(),'network');
  let retained=true;e.waits[0].then(()=>retained=false);await flush();assert(retained);assert.equal(r.writes.length,1);
  r.put.reject(Error('quota'));await e.waits[0];assert(!retained);
  r.entries.set('https://fixture.test/static/a.js?v=new',new Response('new'));
  assert.equal(await (await r.dispatch('/static/a.js?v=new').response).text(),'new');assert.equal(r.requests.length,1);
  r.offline=true;r.entries.set('https://fixture.test/static/a.js',new Response('offline copy'));
  assert.equal(await (await r.dispatch('/static/a.js').response).text(),'offline copy');
  assert.equal(r.requests.at(-1)[1].cache,'no-cache');
  assert.equal(r.dispatch('/app/concierge/panel').response,undefined);
  assert.equal(r.dispatch('/static/a.js','POST').response,undefined);
}
{
  const r=workerRig();r.broken=true;
  const e=r.dispatch('/static/a.js?v=new');assert.equal(await (await e.response).text(),'network');r.put.resolve();await e.waits[0];
}
console.log('Worker exact revision, named cache, retained failed writes, cache errors and offline revalidation passed.');

// The production page recorder cannot accumulate an unbounded session history.
{
  const source=panel.slice(panel.indexOf('  var timings ='),panel.indexOf('  try {\n    var longTasks'));
  const c=vm.createContext({window:{},performance:{now:()=>42}});vm.runInContext(source,c);
  for(let n=0;n<350;n++)c.window.__blam8Timing('sample-'+n);
  assert.equal(c.window.__blam8Timings.length,300);assert.equal(c.window.__blam8Timings[0].name,'sample-50');
}
console.log('Bounded production page timing recorder passed.');
