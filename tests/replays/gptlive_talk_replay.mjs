// GPT-Live hold to talk, no real microphone, provider or audio. The controller and the gestures are pure
// enough to drive with a fake clock and fake events; the transport is driven the way the screen drives it.
// Run: node tests/replays/gptlive_talk_replay.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {createTalkController,installTalkGestures,savedTalkMode,TALK_KEY,TALK_TAIL_MS,TALK_TAP_MS,TALK_WATCHDOG_MS,TALK_KEY_ID}
  from '../../src/app/static/voice-talk.js';
import {createNativeTransport,installHoldAction,conversationBusy,idleWarning,createIdleWatch,workingIds,anyFresh,taskAwaitsPerson,idleEndedNote,IDLE_STALL_SECONDS}
  from '../../src/app/static/voice-gptlive.js';

const flush=async()=>{for(let i=0;i<30;i++)await Promise.resolve();};

// ---- the controller -------------------------------------------------------------------------------
assert.equal(TALK_KEY,'blam8.liveTalk');assert.equal(TALK_TAIL_MS,400);assert.equal(TALK_TAP_MS,250);assert.equal(TALK_WATCHDOG_MS,120000);
function controllerRig(options={}) {
  let clock=1000;const changes=[];
  const talk=createTalkController({now:()=>clock,onChange:(state,reason)=>changes.push([state,reason]),...options});
  return {talk,changes,at:ms=>{clock=ms;},advance:ms=>{clock+=ms;return clock;},now:()=>clock};
}
// Press, hold, release: open while held, still open for the tail, then closed.
{
  const r=controllerRig();assert.equal(r.talk.state(),'idle');assert.equal(r.talk.talking(),false);
  assert.equal(r.talk.press(1),true);assert.equal(r.talk.state(),'pressed');assert.equal(r.talk.talking(),true);assert.equal(r.talk.held(),true);
  r.advance(3000);assert.equal(r.talk.release(1),'release');assert.equal(r.talk.state(),'tail');assert.equal(r.talk.talking(),true,'the tail keeps the mic open');
  assert.equal(r.talk.held(),false);
  r.advance(399);r.talk.tick(r.now());assert.equal(r.talk.state(),'tail');
  r.advance(1);r.talk.tick(r.now());assert.equal(r.talk.state(),'idle');assert.equal(r.talk.talking(),false);
  assert.deepEqual(r.changes.map(c=>c[0]),['pressed','tail','idle']);
  assert.deepEqual(r.talk.report(),{mode:'hold',presses:1,talk_ms:3400,latches:0,watchdog_stops:0});
}
// Pressing again during the tail carries on without a gap.
{
  const r=controllerRig();r.talk.press(1);r.advance(1000);r.talk.release(1);r.advance(200);
  assert.equal(r.talk.press(2),true);assert.equal(r.talk.state(),'pressed');r.advance(5000);r.talk.tick(r.now());assert.equal(r.talk.state(),'pressed');
  assert.equal(r.talk.report().presses,2);
}
// A tap latches the mic open; the next press closes it (after the tail), however long it is held.
{
  const r=controllerRig();r.talk.press(1);r.advance(250);
  assert.equal(r.talk.release(1),'latch');assert.equal(r.talk.state(),'latched');assert.equal(r.talk.talking(),true);
  r.advance(60000);r.talk.tick(r.now());assert.equal(r.talk.state(),'latched','a latched mic stays open');
  assert.equal(r.talk.press(1),true);assert.equal(r.talk.state(),'latched','still open while the closing press is down');
  r.advance(50);assert.equal(r.talk.release(1),'unlatch');assert.equal(r.talk.state(),'tail');
  r.advance(400);r.talk.tick(r.now());assert.equal(r.talk.state(),'idle');
  assert.equal(r.talk.report().latches,1);
  // A press held for longer than a tap while latched also closes it: release is release.
  const q=controllerRig();q.talk.press(1);q.advance(100);q.talk.release(1);q.talk.press(1);q.advance(2000);assert.equal(q.talk.release(1),'unlatch');
  // Just over a tap is a hold, not a latch.
  const s=controllerRig();s.talk.press(1);s.advance(251);assert.equal(s.talk.release(1),'release');
}
// A tap while the idle warning is showing only keeps the conversation: it does not latch.
{
  const r=controllerRig();r.talk.press(1,{latch:false});r.advance(100);assert.equal(r.talk.release(1),'release');assert.equal(r.talk.state(),'tail');
  assert.equal(r.talk.report().latches,0);
}
// One finger at a time; the wrong pointer cannot release; a stray release does nothing.
{
  const r=controllerRig();assert.equal(r.talk.press('a'),true);assert.equal(r.talk.press('b'),false);assert.equal(r.talk.press('a'),false);
  assert.equal(r.talk.release('b'),null);assert.equal(r.talk.state(),'pressed');
  r.advance(500);assert.equal(r.talk.release('a'),'release');assert.equal(r.talk.release('a'),null);assert.equal(r.talk.report().presses,1);
}
// Cancel (hidden page, lost focus, Escape, mute) closes at once, with no tail, and the finger that is still down is ignored when it lifts.
{
  const r=controllerRig();r.talk.press(1);r.advance(500);assert.equal(r.talk.cancel('blur'),true);
  assert.equal(r.talk.state(),'idle');assert.equal(r.talk.talking(),false);assert.equal(r.talk.release(1),null);
  assert.equal(r.talk.press(2),true,'the next press is taken');
  r.talk.cancel('blur');assert.equal(r.talk.cancel('blur'),false,'nothing to cancel');
  assert.equal(r.changes.at(-1)[1],'blur');
  const q=controllerRig();q.talk.press(1);q.advance(100);q.talk.release(1);q.talk.press(1);q.talk.cancel('hidden');assert.equal(q.talk.state(),'idle','a latched mic is let go of too');
}
// A press that has gone on for 120 s is taken as stuck, and nothing resets the count: there is no way for the controller to be told
// "something was heard" (words say nothing about whether the finger is still down).
{
  const r=controllerRig();r.talk.press(1);
  r.advance(119999);assert.equal(r.talk.tick(r.now()),false);assert.equal(r.talk.state(),'pressed');
  r.advance(1);assert.equal(r.talk.tick(r.now()),true);assert.equal(r.talk.state(),'idle');assert.equal(r.talk.report().watchdog_stops,1);
  assert.equal(r.changes.at(-1)[1],'watchdog');assert.equal(r.talk.release(1),null);
  assert.equal(r.talk.touch,undefined,'no way to reset it');
  // A latched mic is not on the watchdog: it ends on a tap, End or the idle close.
  const l=controllerRig();l.talk.press(1);l.advance(100);l.talk.release(1);l.advance(600000);assert.equal(l.talk.tick(l.now()),false);assert.equal(l.talk.state(),'latched');
}
// Holding: a press and its tail hold the idle clock; a latched mic is open but not held (it is watched through the level, like a hands-free mic).
{
  const r=controllerRig();assert.equal(r.talk.holding(),false,'idle');
  r.talk.press(1);assert.equal(r.talk.holding(),true,'pressed');r.advance(1000);r.talk.release(1);assert.equal(r.talk.holding(),true,'tail');
  r.advance(400);r.talk.tick(r.now());assert.equal(r.talk.holding(),false,'idle again');
  r.talk.press(2);r.advance(100);r.talk.release(2);assert.equal(r.talk.state(),'latched');
  assert.equal(r.talk.talking(),true);assert.equal(r.talk.holding(),false,'latched: open, not held');
  r.talk.press(3);assert.equal(r.talk.state(),'latched');assert.equal(r.talk.holding(),false,'the closing press is a touch, not a hold');
  r.advance(500);r.talk.release(3);assert.equal(r.talk.state(),'tail');assert.equal(r.talk.holding(),true,'its tail');
}
// The report counts the time the mic is open right now too, and is bounded.
{
  const r=controllerRig();r.talk.press(1);r.advance(7000);assert.equal(r.talk.report().talk_ms,7000);
  r.advance(10**9);assert.equal(r.talk.report().talk_ms,86400000);
}
// The saved choice: only "hold" is hold; anything else, or a store that throws, is the usual hands-free.
{
  const store=value=>({getItem:key=>{assert.equal(key,'blam8.liveTalk');return value;}});
  assert.equal(savedTalkMode(store('hold')),'hold');
  for(const other of [null,undefined,'','hands_free','Hold','true','1'])assert.equal(savedTalkMode(store(other)),'hands_free',String(other));
  assert.equal(savedTalkMode({getItem(){throw Error('blocked');}}),'hands_free');
  assert.equal(savedTalkMode(),'hands_free','no localStorage at all');
}

// ---- the gestures ---------------------------------------------------------------------------------
function target() {
  const handlers=new Map();
  return {handlers,addEventListener(type,fn,capture){(handlers.get(type)||handlers.set(type,[]).get(type)).push({fn,capture:Boolean(capture)});},
    removeEventListener(type,fn,capture){const list=handlers.get(type)||[];const i=list.findIndex(h=>h.fn===fn && h.capture===Boolean(capture));if(i>=0)list.splice(i,1);},
    listeners:type=>(handlers.get(type)||[]).length,
    fire(type,event={},{capture=null}={}){for(const h of [...(handlers.get(type)||[])])if(capture===null || h.capture===capture)h.fn(event);}};
}
function gestureRig({enabled=true,ready=true,warning=false}={}) {
  const el=target(),doc=Object.assign(target(),{hidden:false}),win=target();
  const state={enabled,ready,warning,presses:[],releases:[],cancels:[],notReady:0,captured:[]};
  el.setPointerCapture=id=>state.captured.push(id);
  const talk={press:(id,options)=>{state.presses.push([id,options]);return true;},release:id=>state.releases.push(id),cancel:reason=>state.cancels.push(reason)};
  const off=installTalkGestures(el,{talk,enabled:()=>state.enabled,ready:()=>state.ready,notReady:()=>state.notReady++,warning:()=>state.warning,doc,win});
  return {el,doc,win,state,off};
}
const down=(over={})=>({isTrusted:true,button:0,pointerId:7,preventDefault(){this.prevented=true;},...over});
{
  const r=gestureRig();
  r.el.fire('pointerdown',down());
  assert.deepEqual(r.state.presses,[[7,{latch:true}]]);assert.deepEqual(r.state.captured,[7]);
  r.el.fire('pointerup',{pointerId:7});r.el.fire('lostpointercapture',{pointerId:7});
  assert.deepEqual(r.state.releases,[7,7],'both end events are passed on; the controller ignores the second');
  r.el.fire('pointercancel',{pointerId:7});assert.equal(r.state.releases.length,3);
}
// Not a trusted left press, or not live: nothing. A press before the session is ready says so and takes nothing.
{
  const r=gestureRig();
  r.el.fire('pointerdown',down({isTrusted:false}));r.el.fire('pointerdown',down({button:2}));
  r.state.enabled=false;r.el.fire('pointerdown',down());
  assert.equal(r.state.presses.length,0);assert.equal(r.state.notReady,0);
  r.state.enabled=true;r.state.ready=false;r.el.fire('pointerdown',down());
  assert.equal(r.state.presses.length,0);assert.equal(r.state.notReady,1);assert.deepEqual(r.state.captured,[]);
  // a release is passed on even when the screen has stopped being live meanwhile
  r.state.enabled=false;r.el.fire('pointerup',{pointerId:7});assert.deepEqual(r.state.releases,[7]);
}
// A browser without pointer capture, or one that throws, still takes the press.
{
  const r=gestureRig();r.el.setPointerCapture=()=>{throw Error('unsupported');};r.el.fire('pointerdown',down());assert.equal(r.state.presses.length,1);
  const q=gestureRig();delete q.el.setPointerCapture;q.el.fire('pointerdown',down());assert.equal(q.state.presses.length,1);
}
// Keyboard: Space or Enter while Mist is focused, no repeats, Escape lets go, nothing else is taken.
{
  const r=gestureRig();
  for(const key of [' ','Enter']) {
    const press={isTrusted:true,key,repeat:false,preventDefault(){this.prevented=true;}};
    r.el.fire('keydown',press);assert.equal(press.prevented,true,key);
    const again={isTrusted:true,key,repeat:true,preventDefault(){this.prevented=true;}};r.el.fire('keydown',again);
    const up={key,preventDefault(){this.prevented=true;}};r.el.fire('keyup',up);assert.equal(up.prevented,true,key);
  }
  assert.deepEqual(r.state.presses.map(p=>p[0]),[TALK_KEY_ID,TALK_KEY_ID]);assert.deepEqual(r.state.releases,[TALK_KEY_ID,TALK_KEY_ID]);
  const tab={isTrusted:true,key:'Tab',repeat:false,preventDefault(){this.prevented=true;}};r.el.fire('keydown',tab);assert.equal(tab.prevented,undefined);assert.equal(r.state.presses.length,2);
  r.el.fire('keydown',{key:'Escape',preventDefault(){}});assert.deepEqual(r.state.cancels,['escape']);
  r.el.fire('blur');assert.equal(r.state.releases.at(-1),TALK_KEY_ID);
  const synthetic={isTrusted:false,key:' ',repeat:false,preventDefault(){}};r.el.fire('keydown',synthetic);assert.equal(r.state.presses.length,2,'a script cannot press');
  // Hands-free: the keys are left alone (the shared hold gesture owns them there).
  const q=gestureRig({enabled:false});const space={isTrusted:true,key:' ',repeat:false,preventDefault(){this.prevented=true;}};
  q.el.fire('keydown',space);q.el.fire('keyup',{key:' ',preventDefault(){this.prevented=true;}});
  assert.equal(space.prevented,undefined);assert.equal(q.state.presses.length,0);
  q.el.fire('keydown',{key:'Escape',preventDefault(){}});assert.deepEqual(q.state.cancels,[]);
}
// A hidden page, a lost window focus and leaving the page all let go at once.
{
  const r=gestureRig();
  r.win.fire('blur');r.win.fire('pagehide');
  r.doc.hidden=false;r.doc.fire('visibilitychange');assert.deepEqual(r.state.cancels,['blur','hidden']);
  r.doc.hidden=true;r.doc.fire('visibilitychange');assert.deepEqual(r.state.cancels,['blur','hidden','hidden']);
}
// The idle warning is read before the idle clock's own listener clears it: a press or a tap then is not allowed to latch.
{
  const r=gestureRig({warning:true});
  // document capture (the snapshot), then the overlay's capture (the idle clock touches and clears the warning), then Mist
  r.doc.fire('pointerdown',{},{capture:true});r.state.warning=false;r.el.fire('pointerdown',down());
  assert.deepEqual(r.state.presses.at(-1),[7,{latch:false}]);
  r.doc.fire('pointerdown',{},{capture:true});r.el.fire('pointerdown',down({pointerId:8}));
  assert.deepEqual(r.state.presses.at(-1),[8,{latch:true}],'the next press, with no warning, latches normally');
  r.state.warning=true;r.doc.fire('keydown',{},{capture:true});r.state.warning=false;
  r.el.fire('keydown',{isTrusted:true,key:' ',repeat:false,preventDefault(){}});
  assert.deepEqual(r.state.presses.at(-1),[TALK_KEY_ID,{latch:false}]);
  assert.equal(r.doc.listeners('pointerdown'),1);
  assert(r.doc.handlers.get('pointerdown')[0].capture && r.doc.handlers.get('keydown')[0].capture,'read in the capture phase, before the idle clock');
}
// The disposer takes every listener away.
{
  const r=gestureRig();r.off();
  for(const t of [r.el,r.doc,r.win])assert.equal([...t.handlers.values()].reduce((n,l)=>n+l.length,0),0);
  r.el.fire('pointerdown',down());assert.equal(r.state.presses.length,0);
}
// Facade and controller together: a second finger, a tap, and a hard stop, end to end.
{
  const c=controllerRig(),r=gestureRig();
  const talk={press:(id,o)=>c.talk.press(id,o),release:id=>c.talk.release(id),cancel:reason=>c.talk.cancel(reason)};
  const el=target(),doc=Object.assign(target(),{hidden:false}),win=target();
  installTalkGestures(el,{talk,enabled:()=>true,ready:()=>true,doc,win});
  el.fire('pointerdown',down({pointerId:1}));el.fire('pointerdown',down({pointerId:2}));
  assert.equal(c.talk.report().presses,1,'the second finger is ignored');
  c.advance(100);el.fire('pointerup',{pointerId:2});assert.equal(c.talk.state(),'pressed','the second finger lifting changes nothing');
  el.fire('pointerup',{pointerId:1});assert.equal(c.talk.state(),'latched','a quick press and lift is a tap');
  el.fire('pointerdown',down({pointerId:3}));win.fire('blur');assert.equal(c.talk.state(),'idle');
  el.fire('pointerup',{pointerId:3});assert.equal(c.talk.state(),'idle');
}

// ---- the hold gesture is disarmed while talking is held -------------------------------------------
{
  const real={doc:globalThis.document,win:globalThis.window,set:globalThis.setTimeout,clear:globalThis.clearTimeout};
  const timers=new Map();let id=0;
  globalThis.document=target();globalThis.window=target();
  globalThis.setTimeout=fn=>{timers.set(++id,fn);return id;};globalThis.clearTimeout=i=>timers.delete(i);
  try {
    const button=Object.assign(target(),{classList:{add(){},remove(){}},click(){this.clicked=(this.clicked||0)+1;}});
    let fired=0,armed=false;
    installHoldAction(button,()=>fired++,{armed:()=>armed});
    button.fire('pointerdown',{button:0});assert.equal(timers.size,0,'disarmed: no hold begins');
    button.fire('pointerup');
    const click={prevented:false,stopped:false,preventDefault(){this.prevented=true;},stopImmediatePropagation(){this.stopped=true;}};
    button.fire('click',click);assert.equal(click.prevented,false,'a click after a disarmed press is not swallowed');
    const space={key:' ',repeat:false,preventDefault(){}};button.fire('keydown',space);assert.equal(timers.size,0);button.fire('keyup',{key:' ',preventDefault(){}});assert.equal(button.clicked,undefined,'and no click is made up');
    armed=true;button.fire('pointerdown',{button:0});assert.equal(timers.size,1);[...timers.values()][0]();assert.equal(fired,1);
    // the two-argument call (an old caller) is always armed
    timers.clear();const plain=Object.assign(target(),{classList:{add(){},remove(){}}});let plainFired=0;
    installHoldAction(plain,()=>plainFired++);plain.fire('pointerdown',{button:0});[...timers.values()][0]();assert.equal(plainFired,1);
  } finally {globalThis.document=real.doc;globalThis.window=real.win;globalThis.setTimeout=real.set;globalThis.clearTimeout=real.clear;}
}

// The idle wiring inside createVoiceLive (sliced by its markers) run against a REAL transport, so what the clock reads is what the
// screen would read; only the screen's own functions are stubs. Its own fake clock is ticked by the caller, one call a second.
function wiringRig(transport) {
  const native=fs.readFileSync('src/app/static/voice-gptlive.js','utf8').replace(/\r\n/g,'\n');
  const wiring=native.slice(native.indexOf('  // ── idle auto-close wiring'),native.indexOf('  // ── end idle auto-close wiring'));
  let now=0;const calls=[];const timers={set(fn){this.fn=fn;return 3;},clear(){this.fn=null;}};
  const els={status:{textContent:'',hidden:true}};
  const c=vm.createContext({after:0,root:{},document:{hidden:false,addEventListener(){},removeEventListener(){}},activities:new Map(),tasks:new Map(),els,Date:{now:()=>now},
    createIdleWatch:o=>createIdleWatch({...o,now:()=>now,timers,target:null,doc:null}),conversationBusy,workingIds,anyFresh,taskAwaitsPerson,idleWarning,idleEndedNote,IDLE_STALL_SECONDS,
    transport:{started:()=>transport.started(),signals:()=>transport.signals(),noteIdle:s=>{calls.push(['noteIdle',s]);transport.noteIdle(s);}},
    status:text=>{calls.push(['status',text]);els.status.textContent=text;els.status.hidden=false;},pauseConversation:(reason,note)=>calls.push(['pause',reason,note])});
  vm.runInContext(wiring,c);
  return {c,calls,tick:s=>{now=s*1000;timers.fn?.();}};
}

// ---- the transport in hold-to-talk mode -----------------------------------------------------------
function makeTrack() {
  return {_e:true,log:[],stops:0,readyState:'live',muted:false,
    get enabled(){return this._e;},set enabled(v){this._e=v;this.log.push(v);},stop(){this.stops++;this.readyState='ended';}};
}
function transportRig({mode='hold',views=[]}={}) {
  const tracks=[makeTrack()],media={getTracks:()=>tracks,getAudioTracks:()=>tracks};
  const calls=[],added=[],channel={close(){}};
  const peer={iceGatheringState:'complete',addTrack(track){added.push(track.enabled);},createDataChannel:()=>channel,createOffer:async()=>({type:'offer',sdp:'v=offer'}),
    setLocalDescription(offer){this.localDescription=offer;},setRemoteDescription:async()=>{},addEventListener(){},removeEventListener(){},close(){}};
  const audio={muted:false,srcObject:null,play:async()=>{},pause(){}};
  const statuses=[];
  const transport=createNativeTransport({media:async()=>media,peer:()=>peer,audio:()=>audio,
    request:async(action,body)=>{calls.push([action,body]);return action==='session'?{ok:true,json:async()=>({session:'owned',transport:{sdp:'v=answer'}})}:{ok:true};},
    caption(){},status:s=>statuses.push(s),playback(){},input:(view,why)=>views.push([view,why])});
  transport.configureInput({talkMode:mode});
  return {transport,tracks,media,calls,added,peer,channel,statuses,views,
    started:()=>channel.onmessage({data:JSON.stringify({type:'session.started'})}),
    delta:(type='session.input_transcript.delta')=>channel.onmessage({data:JSON.stringify({type,delta:'x',event_id:'e'+Math.random(),start_ms:0,end_ms:1})}),
    closeReport:()=>calls.find(([a,b])=>a==='control'&&b.operation==='close')[1]};
}
// Fake clock and fake intervals: nothing in the transport waits for real time.
const realPerf=Object.getOwnPropertyDescriptor(globalThis,'performance'),realSet=globalThis.setInterval,realClear=globalThis.clearInterval;
let clock=5000;const intervals=new Map();let intervalId=0;
const setClock=()=>Object.defineProperty(globalThis,'performance',{configurable:true,writable:true,value:{now:()=>clock}});
globalThis.setInterval=(fn,ms)=>{intervals.set(++intervalId,{fn,ms});return intervalId;};
globalThis.clearInterval=i=>intervals.delete(i);
const tickInputs=(ms)=>{clock+=ms;for(const {fn,ms:every} of [...intervals.values()])if(every===25)fn();};
setClock();
try {
  // Default configuration: no new timer and nothing about talking in the report.
  {
    intervals.clear();const r=transportRig({mode:'hands_free'});await r.transport.start('hands-free');await flush();r.started();
    assert.deepEqual([...intervals.values()].map(i=>i.ms),[250],'only the existing level sampler');
    assert.equal(r.transport.inputMode(),'hands_free');assert(r.tracks[0].enabled);assert.equal(r.transport.talkPress(1),false,'no controller, no press');
    assert.equal(r.transport.signals().holdMode,false);assert.equal(r.transport.signals().talking,false);
    r.peer.connectionState='connected';r.peer.onconnectionstatechange();assert(r.tracks[0].log.every(v=>v===true),'no refresh toggle in hands-free: the mic is never switched off');
    await r.transport.close('end');assert.equal(r.closeReport().talk,null);
  }
  // Hold: the track is disabled before the peer is given it, the session opens closed, and a press opens it.
  {
    intervals.clear();const r=transportRig();await r.transport.start('hold');await flush();
    assert.deepEqual(r.added,[false],'closed before addTrack: nothing leaks at the start');
    assert.deepEqual([...intervals.values()].map(i=>i.ms).sort((a,b)=>a-b),[25,250]);
    r.started();assert.equal(r.tracks[0].enabled,false);assert.equal(r.transport.inputMuted(),false,'closed is not muted');
    const held=talk=>({mode:'hold',talk,line:'off',below:false,near:0});   // no volume line in hold mode
    assert.deepEqual(r.transport.inputView(),held('idle'));assert.equal(r.transport.signals().holdMode,true);
    assert(r.views.some(([v,why])=>why==='started' && v.mode==='hold'),'the screen is told the conversation started in hold mode');
    // a press opens it at once; the line under Mist, the hint and the idle clock all hear about it
    assert.equal(r.transport.talkPress(1),true);assert.equal(r.tracks[0].enabled,true);
    assert.equal(r.transport.signals().talking,true);assert.equal(r.transport.inputMuted(),false);
    assert.deepEqual(r.views.at(-1)[0],held('pressed'));
    // a release keeps it open for the tail, then closes it
    clock+=3000;assert.equal(r.transport.talkRelease(1),'release');assert.equal(r.tracks[0].enabled,true);assert.equal(r.transport.signals().talking,true);
    tickInputs(399);assert.equal(r.tracks[0].enabled,true);tickInputs(1);assert.equal(r.tracks[0].enabled,false);assert.equal(r.transport.signals().talking,false);
    assert.deepEqual(r.views.at(-1)[0],held('idle'));
    // the person's level is zero while nothing is being sent
    assert.equal(r.transport.signals().user,0);
    // a tap latches it open; another tap closes it after the tail
    r.transport.talkPress(2);clock+=100;assert.equal(r.transport.talkRelease(2),'latch');tickInputs(60000);assert.equal(r.tracks[0].enabled,true);
    assert.deepEqual(r.transport.inputView(),held('latched'));
    assert.equal(r.transport.signals().talking,false,'latched: open, but nobody is holding anything, so the idle clock is not held');
    r.transport.talkPress(3);clock+=80;r.transport.talkRelease(3);tickInputs(400);assert.equal(r.tracks[0].enabled,false);
    // a hard stop (hidden page) closes at once
    r.transport.talkPress(4);assert.equal(r.tracks[0].enabled,true);assert.equal(r.transport.talkCancel('hidden'),true);assert.equal(r.tracks[0].enabled,false);
    // the connection coming up writes a closed mic as open and closed again (Safari 18), once per connect, never while it would be open
    const before=r.tracks[0].log.length;r.peer.connectionState='connected';r.peer.onconnectionstatechange();
    assert.deepEqual(r.tracks[0].log.slice(before),[true,false]);
    r.transport.talkPress(5);const mid=r.tracks[0].log.length;r.peer.onconnectionstatechange();assert.deepEqual(r.tracks[0].log.slice(mid),[true],'an open mic is just written open');
    r.transport.talkCancel('hidden');
    await r.transport.close('end');
    const report=r.closeReport().talk;
    assert.deepEqual(Object.keys(report).sort(),['latches','mode','presses','talk_ms','watchdog_stops']);
    assert.equal(report.mode,'hold');assert.equal(report.presses,5);assert.equal(report.latches,1);assert.equal(report.watchdog_stops,0);assert(report.talk_ms>=3400);
    assert.equal(intervals.size,0,'every timer is cleared with the conversation');
    assert(r.tracks[0].stops>=1);
    assert(!JSON.stringify(report).includes('x'.repeat(5)) && Object.values(report).every(v=>typeof v==='number' || v==='hold'));
  }
  // What the meter reads is a copy that stays on, so the room is still measured while the sent track is closed;
  // but the "you" level the screen shows and the idle clock use is zero whenever nothing is being sent.
  {
    intervals.clear();
    const realAudio={ctx:globalThis.AudioContext,stream:globalThis.MediaStream};
    globalThis.MediaStream=class{constructor(t=[]){this.t=[...t];}getTracks(){return this.t;}getAudioTracks(){return this.t;}};
    globalThis.AudioContext=class{constructor(){this.state='running';}createMediaStreamSource(){return {connect(){},disconnect(){}};}
      createAnalyser(){return {fftSize:0,getFloatTimeDomainData(s){s.fill(.1);},disconnect(){}};}resume(){return Promise.resolve();}close(){return Promise.resolve();}};
    try {
      const r=transportRig();const original=r.tracks[0];
      original.clone=()=>{const c=makeTrack();c.clone=original.clone;return c;};
      await r.transport.start('meter');await flush();r.started();
      assert.equal(original.enabled,false);
      assert.equal(r.transport.signals().user,0,'closed: no "you" level, though the room is loud');
      r.transport.talkPress(1);assert.equal(original.enabled,true);
      assert(r.transport.signals().user>0,'held: the level is the person');
      clock+=3000;r.transport.talkRelease(1);tickInputs(500);
      assert.equal(original.enabled,false);assert.equal(r.transport.signals().user,0);
      await r.transport.close('end');
    } finally {globalThis.AudioContext=realAudio.ctx;globalThis.MediaStream=realAudio.stream;}
  }
  // Mute always wins, and a mute lets go of a latched mic.
  {
    intervals.clear();const r=transportRig();await r.transport.start('mute');await flush();r.started();
    r.transport.talkPress(1);r.transport.setMuted(true);assert.equal(r.tracks[0].enabled,false);assert.equal(r.transport.inputMuted(),true);
    assert.equal(r.transport.signals().talking,false,'the press was ended by the mute');
    assert.equal(r.transport.talkPress(2),false,'a muted conversation takes no press');assert.equal(r.tracks[0].enabled,false);
    r.transport.talkRelease(2);r.transport.setMuted(false);tickInputs(500);assert.equal(r.tracks[0].enabled,false,'unmuting does not open a hold-to-talk mic');
    assert.equal(r.transport.inputView().talk,'idle');
    // latched, then muted through the API
    r.transport.talkPress(3);clock+=50;r.transport.talkRelease(3);r.transport.setMuted(true);r.transport.setMuted(false);assert.equal(r.tracks[0].enabled,false);
    // muted before the session exists, with hold mode: still disabled at the peer
    await r.transport.close('end');
    const m=transportRig();m.transport.setMuted(true);await m.transport.start('premuted');await flush();assert.deepEqual(m.added,[false]);await m.transport.close('end');
  }
  // A press that is never released is ended by the watchdog at 120 s and the screen is told why. Words arriving, a television's
  // or the agent's own answer to it, do not reset it: they say nothing about whether a finger is still down.
  {
    intervals.clear();const r=transportRig();await r.transport.start('stuck');await flush();r.started();
    r.transport.talkPress(1);
    for(let t=0;t<115;t+=5){clock+=5000;r.delta('session.input_transcript.delta');r.delta('session.output_transcript.delta');tickInputs(0);}
    assert.equal(r.transport.signals().talking,true,'115 s: not yet');
    tickInputs(5001);assert.equal(r.transport.signals().talking,false,'120 s, with words arriving all the time');assert.equal(r.tracks[0].enabled,false);
    assert.equal(r.views.at(-1)[1],'watchdog');
    await r.transport.close('end');assert.equal(r.closeReport().talk.watchdog_stops,1);
  }
  // A latched mic does not hold the idle clock. One tap and walking away, with only a television in the room, is watched through the level
  // like a hands-free mic: the warning at 50 s and the close at 60 s. A held press does hold it, and counts from when it lets go.
  {
    intervals.clear();const r=transportRig();await r.transport.start('latched-idle');await flush();r.started();
    r.transport.talkPress(1);clock+=100;assert.equal(r.transport.talkRelease(1),'latch');assert.equal(r.transport.inputView().talk,'latched');
    const w=wiringRig(r.transport);w.c.startIdleWatch(60);
    for(let s=1;s<=49;s++)w.tick(s);
    assert(!w.calls.some(([k])=>k==='status'||k==='pause'),'quiet for 49 s: nothing yet');
    w.tick(50);assert.equal(w.calls.at(-1)[1],'Closing in 10 seconds to save your minutes. Hold Mist to stay.');
    for(let s=51;s<=59;s++)w.tick(s);assert(!w.calls.some(([k])=>k==='pause'));
    w.tick(60);assert.deepEqual(w.calls.slice(-2),[['noteIdle',60],['pause','idle',idleEndedNote]]);
    assert.equal(r.transport.inputView().talk,'latched','the controller itself is untouched: the screen closes the conversation');
    await r.transport.close('idle');
    // a held press holds it for as long as it is held (here 5 minutes), and the count starts again from the release
    const h=transportRig();await h.transport.start('held-idle');await flush();h.started();
    const v=wiringRig(h.transport);v.c.startIdleWatch(60);
    h.transport.talkPress(1);clock+=500;
    for(const s of [30,59,60,120,300])v.tick(s);
    assert(!v.calls.some(([k])=>k==='pause'),'held: the conversation stays open');
    h.transport.talkRelease(1);tickInputs(399);v.tick(301);assert.equal(h.transport.signals().talking,true,'the tail still counts');
    tickInputs(1);v.tick(302);assert.equal(h.transport.signals().talking,false);
    v.tick(351);assert.equal(v.calls.at(-1)[1],'Closing in 10 seconds to save your minutes. Hold Mist to stay.');
    v.tick(361);assert.deepEqual(v.calls.slice(-2),[['noteIdle',60],['pause','idle',idleEndedNote]]);
    await h.transport.close('idle');
  }
  // A press needs a started session on a live connection, not an audible voice. Blocked audio, a guard silence and a connection
  // silence (all states hands-free tolerates, with the mic open) must not stop the mic opening, and "still connecting" is said only
  // when it is true. The gestures here are the real ones, wired to the real transport the way the screen wires them.
  {
    intervals.clear();const r=transportRig();
    const el=target(),doc=Object.assign(target(),{hidden:false}),win=target();let notReady=0;
    installTalkGestures(el,{talk:{press:(id,o)=>r.transport.talkPress(id,o),release:id=>r.transport.talkRelease(id),cancel:x=>r.transport.talkCancel(x)},
      enabled:()=>r.transport.active() && r.transport.inputMode()==='hold',ready:()=>r.transport.inputReady(),notReady:()=>notReady++,doc,win});
    const press=id=>el.fire('pointerdown',{isTrusted:true,button:0,pointerId:id}),lift=id=>{clock+=1000;el.fire('pointerup',{pointerId:id});tickInputs(400);};   // held for a second: a hold, not a tap
    assert.equal(r.transport.inputReady(),false,'no conversation');
    await r.transport.start('input-ready');await flush();
    press(1);assert.equal(notReady,1,'not started: one moment');assert.equal(r.tracks[0].enabled,false);
    r.peer.connectionState='connected';assert.equal(r.transport.inputReady(),false,'connected, but the data channel is not open and nothing has started');
    r.channel.readyState='open';assert.equal(r.transport.inputReady(),false,'connected and open, but the provider session has not started');press(1);assert.equal(notReady,2);
    r.started();assert.equal(r.transport.inputReady(),true);
    r.channel.readyState='closing';assert.equal(r.transport.inputReady(),false,'the data channel is closing');r.channel.readyState='open';assert.equal(r.transport.inputReady(),true);
    assert.equal(r.transport.ready(),false,'the voice is not playing yet');
    press(2);assert.equal(r.tracks[0].enabled,true,'a press opens the mic although the voice is not audible');lift(2);assert.equal(r.tracks[0].enabled,false);
    for(const [reason,seq] of [['guard',7],['connection',null],['user',null]]) {
      r.transport.silence(reason,seq);assert.equal(r.transport.ready(),false,reason+': the speaker is silenced');
      assert.equal(r.transport.inputReady(),true,reason);
      press(3);assert.equal(r.tracks[0].enabled,true,reason+': the mic opens');lift(3);assert.equal(r.tracks[0].enabled,false,reason);
    }
    assert.equal(notReady,2,'never refused for the sake of the speaker');
    r.peer.connectionState='disconnected';assert.equal(r.transport.inputReady(),false,'a dropped connection is a real reason');press(4);assert.equal(notReady,3);
    await r.transport.close('end');assert.equal(r.transport.inputReady(),false);
  }
  // A new conversation starts closed again, whatever the last one left behind; the setting is read at each start.
  {
    intervals.clear();const r=transportRig();await r.transport.start('first');await flush();r.started();r.transport.talkPress(1);await r.transport.close('end');
    r.tracks[0]._e=true;r.transport.configureInput({talkMode:'hold'});
    await r.transport.start('second');await flush();assert.equal(r.tracks[0].enabled,false);assert.equal(r.transport.inputView().talk,'idle');
    r.transport.configureInput({talkMode:'hands_free'});assert.equal(r.transport.inputMode(),'hands_free');
    await r.transport.close('end');
    // anything but "hold" is hands-free
    for(const bad of [undefined,null,'',{},'HOLD','true'])r.transport.configureInput({talkMode:bad}),assert.equal(r.transport.inputMode(),'hands_free');
    r.transport.configureInput();assert.equal(r.transport.inputMode(),'hands_free');
  }
} finally {
  Object.defineProperty(globalThis,'performance',realPerf);globalThis.setInterval=realSet;globalThis.clearInterval=realClear;
}

// ---- the idle clock knows about talking ------------------------------------------------------------
{
  const idle={started:true,eventMoved:false,working:false,agentLevel:0,userLevel:0,muted:false};
  assert.equal(conversationBusy(idle),false);
  assert.equal(conversationBusy({...idle,talking:true}),true,'Mist is held (or was a moment ago); a latched mic is not "talking", see the real-transport case above');
  assert.equal(conversationBusy({...idle,talking:true,muted:true}),true);
  assert.equal(conversationBusy({...idle,talking:false}),false);
  assert.equal(idleWarning(10),'Closing in 10 seconds to save your minutes. Tap the screen to keep going.');
  assert.equal(idleWarning(10,false),idleWarning(10));
  assert.equal(idleWarning(7,true),'Closing in 7 seconds to save your minutes. Hold Mist to stay.');
}
// The wiring inside createVoiceLive, run against stubs: a held Mist holds the clock, and the warning asks for the right gesture.
{
  const native=fs.readFileSync('src/app/static/voice-gptlive.js','utf8').replace(/\r\n/g,'\n');
  const wiring=native.slice(native.indexOf('  // ── idle auto-close wiring'),native.indexOf('  // ── end idle auto-close wiring'));
  assert(wiring.length>500);
  let now=0;const calls=[];const timers={set(fn){this.fn=fn;return 3;},clear(){this.fn=null;}};
  const state={started:true,agent:0,user:0,muted:false,talking:false,holdMode:false};
  const els={status:{textContent:'',hidden:true}};
  const c=vm.createContext({after:0,root:{},document:{hidden:false,addEventListener(){},removeEventListener(){}},activities:new Map(),tasks:new Map(),els,Date:{now:()=>now},
    createIdleWatch:o=>createIdleWatch({...o,now:()=>now,timers,target:null,doc:null}),conversationBusy,workingIds,anyFresh,taskAwaitsPerson,idleWarning,idleEndedNote,IDLE_STALL_SECONDS,
    transport:{started:()=>state.started,signals:()=>({agent:state.agent,user:state.user,muted:state.muted,talking:state.talking,holdMode:state.holdMode}),noteIdle:s=>calls.push(['noteIdle',s])},
    status:text=>{calls.push(['status',text]);els.status.textContent=text;els.status.hidden=false;},pauseConversation:(reason,note)=>calls.push(['pause',reason,note])});
  vm.runInContext(wiring,c);
  const tick=s=>{now=s*1000;timers.fn?.();};
  c.startIdleWatch(60);state.talking=true;
  for(const s of [30,59,60,300])tick(s);
  assert(!calls.some(([k])=>k==='pause'),'a held Mist holds the conversation open');
  state.talking=false;tick(301);tick(351);   // counted from when it let go
  assert.equal(calls.at(-1)[1],'Closing in 9 seconds to save your minutes. Tap the screen to keep going.');
  tick(361);assert.equal(calls.at(-1)[0],'pause');
  // In hold-to-talk the warning says how to stay.
  calls.length=0;state.holdMode=true;c.startIdleWatch(60);tick(361+50);
  assert.equal(calls.at(-1)[1],'Closing in 10 seconds to save your minutes. Hold Mist to stay.');
  c.stopIdleWatch();
}

// ---- announcements and the idle warning ---------------------------------------------------------------
// announce() and paintInput() (sliced from the screen) run against stubs: an announcement never replaces the idle warning, and what
// was not said because of it is said at the next change; the near-miss note is said the first time and then at most once a minute.
{
  const native=fs.readFileSync('src/app/static/voice-gptlive.js','utf8').replace(/\r\n/g,'\n');
  const block=native.slice(native.indexOf('  function announce(text) {'),native.indexOf('  function paintMic() {'));
  assert(block.includes('function paintInput(view, why)') && block.length>500);
  let t=1000;const said=[],timers=[];let warning=false;
  const els={status:{textContent:''}};
  const c=vm.createContext({idleWatch:{warning:()=>warning},status:text=>{said.push(text);els.status.textContent=text;},els,announceText:'',announceTimer:null,
    setTimeout:fn=>{timers.push(fn);return timers.length;},clearTimeout(){},Date:{now:()=>t},root:{},open:true,hintDismissed:false,updateHint(){},paintMic(){},
    lineSaid:false,nearSeen:0,nearNotedAt:0});
  vm.runInContext(block,c);
  const view=(over={})=>({mode:'hands_free',talk:'idle',line:'on',below:false,near:0,...over});
  const NEAR='Something under your line was not sent. If that was you, lower the line in Settings.';
  const CANNOT='The volume line cannot work on this phone right now, so everything is being sent. Tap the screen to try again.';
  c.paintInput(view(),'started');assert.deepEqual(said,['Volume line on — sound under your line is not sent.']);
  // the idle warning is showing: nothing replaces it, and nothing is marked as said
  said.length=0;els.status.textContent='Closing in 9 seconds to save your minutes. Tap the screen to keep going.';warning=true;
  c.paintInput(view({near:1}),'');c.paintInput(view({near:1,line:'unavailable'}),'');c.paintInput(view(),'watchdog');
  assert.deepEqual(said,[],'the warning stays on screen');assert.equal(els.status.textContent.startsWith('Closing in'),true);
  assert.equal(c.nearSeen,0,'the near miss is still owed');assert.equal(c.nearNotedAt,0);assert.equal(c.lineSaid,false,'and so is "cannot work"');
  // the warning goes: the next change says what was owed (once)
  warning=false;c.paintInput(view({near:1,line:'unavailable'}),'');
  assert.deepEqual(said,[CANNOT,NEAR],'both said, in that order');assert.equal(c.nearSeen,1);assert.equal(c.lineSaid,true);
  said.length=0;c.paintInput(view({near:1,line:'unavailable'}),'');assert.deepEqual(said,[],'not again');
  // at most once a minute: a second near miss inside the minute is counted but not said; one after it is said
  t+=30000;c.paintInput(view({near:2,line:'unavailable'}),'');assert.deepEqual(said,[]);assert.equal(c.nearSeen,2);
  t+=31000;c.paintInput(view({near:3,line:'unavailable'}),'');assert.deepEqual(said,[NEAR]);assert.equal(c.nearSeen,3);
  // a watchdog announcement is dropped under the warning too, and not queued
  said.length=0;warning=true;c.paintInput(view({near:3}),'watchdog');assert.deepEqual(said,[]);warning=false;c.paintInput(view({near:3}),'');assert.deepEqual(said,[]);
  c.paintInput(view({near:3}),'watchdog');assert.deepEqual(said,['Released the mic — hold Mist again to talk.']);
}

// ---- the glue that only the real screen can run, pinned in the source ------------------------------
{
  const native=fs.readFileSync('src/app/static/voice-gptlive.js','utf8').replace(/\r\n/g,'\n');
  assert.match(native,/mist\.addEventListener\('click',\(\)=>\{\n\s+if\(!open \|\| paused \|\| !transport\.inputAvailable\(\) \|\| holdMode\(\)\)return;/,'a click never mutes in hold-to-talk');
  assert.match(native,/\},\{armed:\(\)=>!\(holdMode\(\) && open && !paused && transport\.active\(\)\)\}\)\);/,'a long press never ends a live hold-to-talk conversation');
  assert.match(native,/installTalkGestures\(mist,\{[\s\S]*warning:\(\)=>Boolean\(idleWatch\?\.warning\(\)\),\n\s*\}\);/);
  assert.match(native,/if\(open\)return;inputNow=inputSettings\(\);transport\.configureInput\(inputNow\);/,'the setting is read each time the screen opens');
  assert.match(native,/const talkMode=savedTalkMode\(\), line=talkMode==='hold' \? null : savedLiveLine\(\);/,'hold to talk and the line are never used together');
  assert.match(native,/input:\(view,why\)=>paintInput\(view,why\),\n  \}\);/);
  assert.match(native,/<p class="vl-mic-mode" hidden><\/p>/);
  assert.match(native,/ready:\(\)=>transport\.inputReady\(\),\n\s+notReady:/,'the gestures ask whether the connection is up, not whether the voice is audible');
  assert.match(native,/if\(plain && announceText && els\.status\.textContent===announceText\)return;/,'the plain "Listening"/"Speaker enabled" lines do not wipe an announcement that is up');
}

console.log('GPT-Live hold to talk: controller, gestures, transport, idle clock and glue passed.');
