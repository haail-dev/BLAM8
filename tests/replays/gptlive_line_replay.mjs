// GPT-Live volume line, no real microphone, provider or audio. The gate is a pure state machine driven with
// a fake clock; the transport is driven the way the screen drives it, with a fake analyser whose level the
// replay sets. Run: node tests/replays/gptlive_line_replay.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {createLineGate,savedLiveLine,LINE_KEY,LINE_HOLD_MS,LINE_CLOSE_RATIO,LINE_ZERO_MS,LINE_WARMUP_MS,LINE_GAP_MS,LINE_PRESS_OPEN_MS,LINE_RETURN_OPEN_MS,
  LINE_NEAR_RATIO,LINE_NEAR_MS,LINE_FAIL_OPEN_REASONS,LINE_BROKEN_REASONS,LINE_BROKEN_MS,LINE_BELOW_MS}
  from '../../src/app/static/voice-line.js';
import {createNativeTransport,captureConstraints} from '../../src/app/static/voice-gptlive.js';

const flush=async()=>{for(let i=0;i<30;i++)await Promise.resolve();};

// ---- the starting constants ------------------------------------------------------------------------
assert.equal(LINE_KEY,'blam8.liveMicLine');
assert.deepEqual([LINE_HOLD_MS,LINE_CLOSE_RATIO,LINE_ZERO_MS,LINE_WARMUP_MS,LINE_GAP_MS,LINE_PRESS_OPEN_MS,LINE_RETURN_OPEN_MS,LINE_NEAR_RATIO,LINE_NEAR_MS,LINE_BROKEN_MS,LINE_BELOW_MS],
  [900,.6,500,1000,75,3000,1000,.5,400,1500,300]);
assert.deepEqual([...LINE_FAIL_OPEN_REASONS].sort(),['analyser','context','gap','hidden','no_clone','press','track','visible','warmup','zero']);
assert(LINE_BROKEN_REASONS.every(r=>LINE_FAIL_OPEN_REASONS.includes(r)));
assert.deepEqual([...LINE_BROKEN_REASONS].sort(),['analyser','context','no_clone','track','zero'],'a forced open window is not "cannot work"');

// ---- the saved line --------------------------------------------------------------------------------
{
  const store=value=>({getItem:key=>{assert.equal(key,'blam8.liveMicLine');return value;}});
  assert.equal(savedLiveLine(store('0.12')),.12);assert.equal(savedLiveLine(store(' 0.2 ')),.2);assert.equal(savedLiveLine(store('0.000001')),.000001);
  for(const bad of [null,undefined,'','0','1','1.0','1.5','-0.2','abc','NaN','Infinity'])assert.equal(savedLiveLine(store(bad)),null,String(bad));
  assert.equal(savedLiveLine({getItem(){throw Error('blocked');}}),null);assert.equal(savedLiveLine(),null,'no localStorage');
}

// ---- the gate --------------------------------------------------------------------------------------
const ok={contextState:'running',hidden:false,tapAvailable:true,tapLive:true,started:true};
function rig(line=.1,options={}) {
  const gate=createLineGate({line,...options});let at=0;
  const step=(peak,ms=25,extra={})=>{at+=ms;return gate.step({peak,...ok,...extra},at);};
  const hold=(peak,ms,extra={})=>{let last;for(let t=0;t<ms;t+=25)last=step(peak,25,extra);return last;};
  return {gate,step,hold,now:()=>at,jump:ms=>{at+=ms;}};
}
// Settled: past warm-up with real sound, so the rules under test are the only ones in play.
function settled(line=.1,options={}) {const r=rig(line,options);r.hold(.3,1100);assert.equal(r.gate.reason(),'open');return r;}

// No line, or a nonsense one: always open and never reports a line.
for(const line of [null,undefined,0,1,-.2,'0.2',NaN,2]) {
  const gate=createLineGate({line});let at=0;
  const step=peak=>gate.step({peak,...ok},at+=25);
  assert.deepEqual(step(.001),{open:true,reason:'off'});for(let i=0;i<200;i++)assert.equal(step(.0001).open,true);
  assert.equal(gate.report().line,null,String(line));assert.equal(gate.isOpen(),true);
}
assert.deepEqual(createLineGate().report(),{line:null,closed_ms:0,opens:0,near_misses:0,fail_open:{},agc_requested_off:false});
// Open at the start: warm-up holds it open until the session has started and a second has passed.
{
  const r=rig();
  assert.deepEqual(r.step(.001,25,{started:false}),{open:true,reason:'warmup'});r.hold(.001,5000,{started:false});assert.equal(r.gate.isOpen(),true,'not started: no gate, however quiet');
  assert.deepEqual(r.step(.001),{open:true,reason:'warmup'});          // the session starts now
  r.hold(.001,975);assert.equal(r.gate.isOpen(),true,'a second of warm-up, counted from the start');assert.equal(r.gate.reason(),'warmup');
  assert.equal(r.step(.001).reason,'open','warm-up over (and quiet is not yet long enough to shut it)');
}
// Closes only after the level has stayed under the close threshold for the whole hold, and opens at once on the line.
{
  const r=settled();   // line .1: close below .06
  r.hold(.059,900);assert.equal(r.gate.isOpen(),true,'under the threshold for less than the hold');   // the first quiet reading starts the count
  assert.equal(r.step(.059).open,false);assert.equal(r.gate.reason(),'closed','900 ms under the threshold shuts it');
  assert.equal(r.step(.099).open,false,'just under the line is not enough to open');
  assert.deepEqual(r.step(.1),{open:true,reason:'open'});assert.equal(r.gate.report().opens,1);
  assert.equal(r.step(.0001).open,true,'and quiet again does not shut it until the hold has run');
}
// Hysteresis: between the close threshold and the line, an open gate stays open and a closed one stays closed.
{
  const r=settled();r.hold(.07,5000);assert.equal(r.gate.isOpen(),true,'in the band: never closes');
  r.hold(.059,1000);assert.equal(r.gate.isOpen(),false);r.hold(.099,2000);assert.equal(r.gate.isOpen(),false,'in the band: never opens');
  r.step(.1);assert.equal(r.gate.isOpen(),true);
}
// A burst inside the hold window starts the hold again; a level that dips into the band does too.
{
  const r=settled();r.hold(.02,800);r.step(.5);r.hold(.02,800);assert.equal(r.gate.isOpen(),true);
  r.hold(.02,125);assert.equal(r.gate.isOpen(),false);
  const q=settled();q.hold(.02,800);q.step(.07);q.hold(.02,800);assert.equal(q.gate.isOpen(),true,'the band resets the hold');
}
// Each reason that makes the line unable to work holds the gate open at once, is counted once per time it starts,
// and leaves a fresh hold before the gate can shut again.
const unable=[
  ['no_clone',{tapAvailable:false}],['track',{tapLive:false}],['context',{contextState:'suspended'}],['context',{contextState:'interrupted'}],['context',{contextState:'closed'}],
  ['analyser',{contextState:null}],['analyser',{peak:null}],['hidden',{hidden:true}],
];
for(const [reason,extra] of unable) {
  const r=settled();r.hold(.02,1000);assert.equal(r.gate.isOpen(),false,reason);        // shut
  const first=r.step('peak' in extra?extra.peak:.02,25,'peak' in extra?{}:extra);
  assert.deepEqual(first,{open:true,reason},reason);
  assert.equal(r.gate.report().opens,0,'a forced open is not a reopen');
  r.hold('peak' in extra?extra.peak:.02,2000,'peak' in extra?{}:extra);
  assert.equal(r.gate.report().fail_open[reason],reason==='hidden'?1:r.gate.report().fail_open[reason]);
  // the cause goes away: it stays open for a fresh hold, then shuts again
  const wasHidden=reason==='hidden';
  if(wasHidden){assert.equal(r.step(.02).reason,'visible');r.hold(.02,975);assert.equal(r.gate.isOpen(),true);}
  r.hold(.02,800);assert.equal(r.gate.isOpen(),true,reason+': a fresh hold');
  r.hold(.02,200);assert.equal(r.gate.isOpen(),false,reason+': and then it works again');
  assert.equal(r.gate.report().fail_open[reason]>=1,true,reason);
}
// A failure is counted when it starts, not on every tick of it.
{
  const r=settled();r.hold(.02,1000);r.hold(.02,2000,{contextState:'suspended'});
  assert.equal(r.gate.report().fail_open.context,1);
  r.step(.02);r.hold(.02,1000,{contextState:'suspended'});assert.equal(r.gate.report().fail_open.context,2);
}
// A page that was hidden holds it open for a second after it comes back, then the usual hold.
{
  const r=settled();r.hold(.02,1000);r.hold(.02,5000,{hidden:true});assert.equal(r.gate.report().fail_open.hidden,1);
  assert.deepEqual(r.step(.02),{open:true,reason:'visible'});assert.equal(r.gate.report().fail_open.visible,1);
  r.hold(.02,975);assert.equal(r.gate.isOpen(),true);
  r.hold(.02,1000);assert.equal(r.gate.isOpen(),false,'the usual hold counts from when the second ended');
}
// Throttled timers: a tick later than 75 ms cannot be trusted.
{
  const r=settled();r.hold(.02,1000);
  assert.equal(r.step(.02,75).reason,'closed','75 ms is on time');
  assert.deepEqual(r.step(.02,76),{open:true,reason:'gap'});
  assert.equal(r.gate.report().fail_open.gap,1);
}
// A press on Mist holds it open for three seconds; a second press extends it, a shorter one does not shorten it.
{
  const r=settled();r.hold(.02,1000);assert.equal(r.gate.isOpen(),false);
  r.gate.forceOpen(3000,r.now());assert.deepEqual(r.step(.02),{open:true,reason:'press'});
  r.hold(.02,2950);assert.equal(r.gate.isOpen(),true);
  r.gate.forceOpen(1000,r.now());r.hold(.02,1500);assert.equal(r.gate.isOpen(),true,'a later, shorter hold is still after the first');
  r.hold(.02,4000);assert.equal(r.gate.isOpen(),false);assert.equal(r.gate.report().fail_open.press,1);
  assert.equal(r.gate.report().fail_open.hidden,undefined,'reasons that never happened are not reported');
}
// Dead silence is unknown, never quiet: an exact zero for half a second opens the gate and keeps it open.
{
  const r=settled();r.hold(.02,1000);assert.equal(r.gate.isOpen(),false);
  r.hold(0,500);assert.equal(r.gate.isOpen(),false,'a short zero is just quiet');   // the first zero reading starts the count
  assert.deepEqual(r.step(0),{open:true,reason:'zero'});
  r.hold(0,10000);assert.equal(r.gate.isOpen(),true,'an analyser that reads zero for ever can never shut it');
  assert.equal(r.gate.report().fail_open.zero,1);
  r.step(.02);r.hold(.02,800);assert.equal(r.gate.isOpen(),true);r.hold(.02,200);assert.equal(r.gate.isOpen(),false);
  // a zero that is broken up by sound never adds up
  const q=settled();q.hold(.02,1000);for(let i=0;i<10;i++){q.hold(0,400);q.step(.02);}assert.equal(q.gate.isOpen(),false);
}
// Near misses: sound close to the line that was not sent, once it has lasted, once until it leaves the band.
{
  const r=settled();r.hold(.02,1000);assert.equal(r.gate.isOpen(),false);
  r.hold(.07,400);assert.equal(r.gate.nearMisses(),0);r.step(.07);assert.equal(r.gate.nearMisses(),1,'sustained for 400 ms');   // the first reading in the band starts the count
  r.hold(.07,5000);assert.equal(r.gate.nearMisses(),1,'once while it lasts');
  r.hold(.02,100);r.hold(.07,425);assert.equal(r.gate.nearMisses(),2,'it left the band and came back');
  r.step(.1);assert.equal(r.gate.isOpen(),true);r.hold(.02,1000);r.hold(.055,425);assert.equal(r.gate.nearMisses(),3,'half the line counts');
  r.hold(.02,1000);r.hold(.049,2000);
  assert.equal(r.gate.nearMisses(),3,'under half the line is just the room');
  // sound that is being sent is not a near miss
  const q=settled();q.hold(.07,3000);assert.equal(q.gate.nearMisses(),0);
}
// The report: counts, milliseconds and reason names only.
{
  const r=settled(.25);r.hold(.02,1000);r.hold(.02,2000);r.step(.5);
  const report=r.gate.report({agcRequestedOff:true});
  assert.deepEqual(Object.keys(report).sort(),['agc_requested_off','closed_ms','fail_open','line','near_misses','opens']);
  assert.equal(report.line,.25);assert.equal(report.opens,1);assert.equal(report.agc_requested_off,true);
  assert(report.closed_ms>=2000 && report.closed_ms<=2200,'about the 2 s it was shut: '+report.closed_ms);
  assert.deepEqual(report.fail_open,{warmup:1});
  assert.equal(r.gate.report().agc_requested_off,false);assert.equal(r.gate.report({agcRequestedOff:'yes'}).agc_requested_off,true);
  assert.equal(r.gate.closedFor(r.now()),0,'open now');
  const s=settled();s.hold(.02,1000);assert.equal(s.gate.closedFor(s.now()),75,'shut for the 75 ms since it closed');
  s.hold(.02,400);assert.equal(s.gate.closedFor(s.now()),475);
  // a long gap does not add a long time: closed_ms grows by at most 100 ms a tick
  const t=settled();t.hold(.02,1000);const before=t.gate.report().closed_ms;t.step(.02,5000);assert(t.gate.report().closed_ms-before<=100);
}

// ---- the transport ---------------------------------------------------------------------------------
class FakeStream {constructor(tracks=[]){this.t=[...tracks];}getTracks(){return this.t;}getAudioTracks(){return this.t;}}
function makeTrack(extra={}) {
  return {_e:true,log:[],stops:0,readyState:'live',muted:false,
    get enabled(){return this._e;},set enabled(v){this._e=v;this.log.push(v);},stop(){this.stops++;this.readyState='ended';},...extra};
}
let level=.3,contextState='running',analyserThrows=false;
const realCtx=globalThis.AudioContext,realStream=globalThis.MediaStream,realPerf=Object.getOwnPropertyDescriptor(globalThis,'performance'),realSet=globalThis.setInterval,realClear=globalThis.clearInterval;
let clock=1000;const intervals=new Map();let intervalId=0,contexts=[];
function installFakes() {
  globalThis.MediaStream=FakeStream;
  globalThis.AudioContext=class{
    constructor(){this.state=contextState;contexts.push(this);}
    createMediaStreamSource(){return {connect(){},disconnect(){}};}
    createAnalyser(){return {fftSize:0,getFloatTimeDomainData(samples){if(analyserThrows)throw Error('analyser failed');samples.fill(0);samples[3]=level;},disconnect(){}};}
    resume(){this.resumes=(this.resumes||0)+1;return Promise.resolve();}close(){return Promise.resolve();}
  };
  Object.defineProperty(globalThis,'performance',{configurable:true,writable:true,value:{now:()=>clock}});
  globalThis.setInterval=(fn,ms)=>{intervals.set(++intervalId,{fn,ms});return intervalId;};
  globalThis.clearInterval=i=>intervals.delete(i);
}
function removeFakes() {
  globalThis.AudioContext=realCtx;globalThis.MediaStream=realStream;Object.defineProperty(globalThis,'performance',realPerf);globalThis.setInterval=realSet;globalThis.clearInterval=realClear;
}
const tick=(ms=25)=>{clock+=ms;for(const {fn,ms:every} of [...intervals.values()])if(every===25)fn();};
const run=(ms)=>{for(let t=0;t<ms;t+=25)tick(25);};
function transportRig({cloneable=true,views=[]}={}) {
  const original=makeTrack();
  if(cloneable)original.clone=()=>{const c=makeTrack();c.clone=original.clone;return c;};
  const tracks=[original],media={getTracks:()=>tracks,getAudioTracks:()=>tracks};
  const calls=[],added=[],channel={close(){}};
  const peer={iceGatheringState:'complete',addTrack(track){added.push(track.enabled);},createDataChannel:()=>channel,createOffer:async()=>({type:'offer',sdp:'v=offer'}),
    setLocalDescription(offer){this.localDescription=offer;},setRemoteDescription:async()=>{},addEventListener(){},removeEventListener(){},close(){}};
  const audio={muted:false,srcObject:null,play:async()=>{},pause(){}};
  const transport=createNativeTransport({media:async()=>media,peer:()=>peer,audio:()=>audio,
    request:async(action,body)=>{calls.push([action,body]);return action==='session'?{ok:true,json:async()=>({session:'owned',transport:{sdp:'v=answer'}})}:{ok:true};},
    caption(){},status(){},playback(){},input:(view,why)=>views.push([view,why])});
  return {transport,original,tracks,added,peer,views,calls,
    started:()=>channel.onmessage({data:JSON.stringify({type:'session.started'})}),
    report:()=>calls.find(([a,b])=>a==='control'&&b.operation==='close')[1]};
}
const LINE=.1;
installFakes();
try {
  // Not set: no gate, no timer beyond the level sampler, nothing about a line in the report, whatever the room does.
  {
    intervals.clear();level=.001;const r=transportRig();r.transport.configureInput({talkMode:'hands_free'});
    await r.transport.start('off');await flush();r.started();
    assert.deepEqual([...intervals.values()].map(i=>i.ms),[250]);run(5000);assert.equal(r.original.enabled,true);
    assert.equal(r.transport.inputView().line,'off');await r.transport.close('end');assert.equal(r.report().line,null);
  }
  // configureInput: only a number strictly between 0 and 1 is a line, hold to talk never has one, and AGC only goes off for a line.
  {
    const r=transportRig();
    for(const [config,expected] of [[{line:.1,agcOff:true},{line:.1,agcOff:true}],[{line:.1},{line:.1,agcOff:false}],[{line:0,agcOff:true},{line:null,agcOff:false}],
        [{line:1,agcOff:true},{line:null,agcOff:false}],[{line:'0.1',agcOff:true},{line:null,agcOff:false}],[{line:NaN,agcOff:true},{line:null,agcOff:false}],
        [{line:-.1},{line:null,agcOff:false}],[{talkMode:'hold',line:.1,agcOff:true},{line:null,agcOff:false}],[{},{line:null,agcOff:false}]]) {
      r.transport.configureInput(config);intervals.clear();
      // read back through a session: the gate exists only when a line was accepted
      const probe=transportRig();probe.transport.configureInput(config);
      await probe.transport.start('probe');await flush();probe.started();
      assert.equal(probe.transport.inputView().line,expected.line===null?'off':'on',JSON.stringify(config));
      await probe.transport.close('end');
      assert.equal(probe.report().line===null,expected.line===null,JSON.stringify(config));
      if(expected.line!==null)assert.equal(probe.report().line.agc_requested_off,expected.agcOff,JSON.stringify(config));
    }
  }
  // A line: open through warm-up, shut when the room is under it, open at once on sound, mute always wins.
  {
    intervals.clear();contexts=[];level=.3;const views=[],r=transportRig({views});r.transport.configureInput({line:LINE,agcOff:true});
    await r.transport.start('line');await flush();
    assert.deepEqual(r.added,[true],'the sent track starts open: nothing is shut before the room has been measured');
    assert.deepEqual([...intervals.values()].map(i=>i.ms).sort((a,b)=>a-b),[25,250]);
    r.started();assert(views.some(([v,why])=>why==='started' && v.line==='on'));
    run(1200);assert.equal(r.original.enabled,true,'sound over the line');assert.equal(r.transport.inputView().line,'on');
    level=.02;run(850);assert.equal(r.original.enabled,true,'quiet, but not for the whole hold');
    run(100);assert.equal(r.original.enabled,false,'shut after the hold');assert.equal(r.transport.inputMuted(),false,'a closed line is not a muted mic');
    assert.equal(r.transport.signals().user,0,'the "you" level is zero while nothing is sent');
    assert.equal(r.transport.signals().muted,false);
    run(250);assert.equal(r.transport.inputView().below,false,'not yet: shut for less than 300 ms');
    run(100);assert.equal(r.transport.inputView().below,true,'the orb dims once it has been shut a moment');
    level=.3;run(25);assert.equal(r.original.enabled,true,'opens on the first tick over the line');assert.equal(r.transport.inputView().below,false);
    // mute wins at every transition
    level=.02;run(1100);assert.equal(r.original.enabled,false);
    r.transport.setMuted(true);assert.equal(r.original.enabled,false);level=.3;run(500);assert.equal(r.original.enabled,false,'loud room, muted: still shut');
    assert.equal(r.transport.inputMuted(),true);
    r.transport.setMuted(false);run(25);assert.equal(r.original.enabled,true,'unmuted over the line: open on the next reading');
    level=.02;run(1100);assert.equal(r.original.enabled,false);r.transport.setMuted(true);r.transport.setMuted(false);run(25);
    assert.equal(r.original.enabled,false,'unmuting into a quiet room leaves the line shut');
    // the gate is not stepped while muted
    r.transport.setMuted(true);run(2000);r.transport.setMuted(false);   // nothing is stepped while muted: coming back is one late reading, which fails open
    // the meter reads a copy, so the room is still measured while the sent track is shut: the line can open again
    level=.5;run(50);assert.equal(r.original.enabled,true);
    await r.transport.close('end');
    const report=r.report().line;
    assert.equal(report.line,LINE);assert.equal(report.agc_requested_off,true);assert(report.opens>=1 && report.closed_ms>=500,JSON.stringify(report));
    assert(report.fail_open.gap>=1,'the readings after a mute are late: '+JSON.stringify(report));
    assert.deepEqual(Object.keys(report).sort(),['agc_requested_off','closed_ms','fail_open','line','near_misses','opens']);
    assert.equal(report.fail_open.warmup,1);assert.equal(intervals.size,0,'every timer is cleared');
    assert(Object.values(report.fail_open).every(n=>Number.isInteger(n)));
  }
  // A press on Mist holds it open for three seconds; the screen is told, the room reading does not change that.
  {
    intervals.clear();level=.02;const views=[],r=transportRig({views});r.transport.configureInput({line:LINE});
    await r.transport.start('press');await flush();r.started();run(2500);assert.equal(r.original.enabled,false);
    r.transport.noteMist();assert.equal(r.original.enabled,true,'at once, not on the next tick');
    run(2950);assert.equal(r.original.enabled,true);run(900);assert.equal(r.original.enabled,true,'3 s of forced open, then the hold');run(100);assert.equal(r.original.enabled,false);
    await r.transport.close('end');assert.equal(r.report().line.fail_open.press,1);
    // with no line set a press does nothing and costs nothing
    const q=transportRig();q.transport.configureInput({});await q.transport.start('nopress');await flush();q.transport.noteMist();await q.transport.close('end');
  }
  // Something close to the line that was not sent: counted, and the screen is told (the screen limits how often it says so).
  {
    intervals.clear();level=.02;const views=[],r=transportRig({views});r.transport.configureInput({line:LINE});
    await r.transport.start('near');await flush();r.started();run(2500);assert.equal(r.original.enabled,false);
    level=.07;run(500);assert.equal(r.transport.inputView().near,1);assert.equal(r.original.enabled,false,'close to the line is still not sent');
    assert.equal(views.at(-1)[0].near,1);
    level=.02;run(100);level=.07;run(500);assert.equal(r.transport.inputView().near,2);
    await r.transport.close('end');assert.equal(r.report().line.near_misses,2);
  }
  // This phone cannot measure the room: no copy of the track, a suspended context, an analyser that throws or reads zero. All of them
  // send everything, count why, and after a moment the screen says the line cannot work.
  for(const cause of ['no_clone','context','analyser','zero']) {
    intervals.clear();contexts=[];level=.3;contextState='running';analyserThrows=false;
    const views=[],r=transportRig({views,cloneable:cause!=='no_clone'});r.transport.configureInput({line:LINE});
    await r.transport.start(cause);await flush();r.started();run(1200);
    if(cause==='context')contexts[0].state='suspended';
    if(cause==='analyser')analyserThrows=true;
    if(cause==='zero')level=0;
    level=cause==='zero'?0:.02;run(2500);
    assert.equal(r.original.enabled,true,cause+': everything is sent');
    assert.equal(r.transport.inputView().line,'unavailable',cause);assert.equal(r.transport.inputView().below,false,cause);
    assert(views.some(([v])=>v.line==='unavailable'),'the screen is told: '+cause);
    // it comes back when the cause goes (not for a missing copy: that is for the whole conversation)
    if(cause!=='no_clone'){
      if(cause==='context')contexts[0].state='running';
      if(cause==='analyser')analyserThrows=false;
      level=.02;run(1100);
      assert.equal(r.transport.inputView().line,'on',cause+': working again');assert.equal(r.original.enabled,false,cause+': and shutting the room out again');
    }
    await r.transport.close('end');
    const report=r.report().line;assert(report.fail_open[cause]>=1,cause+' '+JSON.stringify(report));
  }
  analyserThrows=false;contextState='running';
  // A moment of not being able to measure (a context waking up) is not yet "cannot work": the screen waits 1.5 s before saying so.
  {
    intervals.clear();contexts=[];level=.3;contextState='running';const r=transportRig();r.transport.configureInput({line:LINE});
    await r.transport.start('blip');await flush();r.started();run(1200);
    contexts[0].state='suspended';const seen=new Set();for(let t=0;t<1400;t+=25){tick(25);seen.add(r.transport.inputView().line);}
    contexts[0].state='running';run(100);for(let t=0;t<200;t+=25){tick(25);seen.add(r.transport.inputView().line);}
    assert.deepEqual([...seen],['on'],'1.4 s of a suspended context never reached the screen');
    contexts[0].state='suspended';run(1600);assert.equal(r.transport.inputView().line,'unavailable','but 1.6 s did');
    await r.transport.close('end');
  }
  // A tap resumes a suspended analysis. With a line set the room is measured whether or not the orb is being drawn, so it resumes then too;
  // with no line the visual flag still decides, as it always did.
  {
    intervals.clear();contexts=[];level=.3;contextState='running';const r=transportRig();r.transport.configureInput({line:LINE});
    await r.transport.start('resume');await flush();r.started();run(1200);
    r.transport.setVisualActive(false);contexts[0].state='suspended';contexts[0].resumes=0;
    r.transport.resumeAnalysis();assert.equal(contexts[0].resumes,1,'line set, visual off: resumed');
    await r.transport.close('end');
    intervals.clear();contexts=[];const q=transportRig();q.transport.configureInput({});
    await q.transport.start('resume-plain');await flush();q.started();
    q.transport.setVisualActive(false);contexts[0].state='suspended';contexts[0].resumes=0;
    q.transport.resumeAnalysis();assert.equal(contexts[0].resumes,0,'no line, visual off: left alone, as before');
    q.transport.setVisualActive(true);q.transport.resumeAnalysis();assert.equal(contexts[0].resumes,1,'no line, visual on: resumed');
    await q.transport.close('end');
  }
  // Nothing is gated before the provider session has started, however quiet the room.
  {
    intervals.clear();contexts=[];level=.02;const r=transportRig();r.transport.configureInput({line:LINE});
    await r.transport.start('early');await flush();run(5000);assert.equal(r.original.enabled,true,'not started: nothing shut');
    r.started();run(950);assert.equal(r.original.enabled,true,'warm-up counts from the start');run(1000);assert.equal(r.original.enabled,false);
    await r.transport.close('end');
  }
  // A copy that is not live, or muted by the system, is "cannot measure" too.
  {
    intervals.clear();contexts=[];level=.02;const r=transportRig();r.transport.configureInput({line:LINE});
    const created=[];const original=r.original;original.clone=()=>{const c=makeTrack();created.push(c);return c;};
    await r.transport.start('track');await flush();r.started();run(3000);assert.equal(r.original.enabled,false,'working');
    created[0].muted=true;run(25);assert.equal(r.original.enabled,true,'the system muted the copy');run(1500);assert.equal(r.transport.inputView().line,'unavailable');
    created[0].muted=false;created[0].readyState='ended';run(25);assert.equal(r.original.enabled,true);
    await r.transport.close('end');assert(r.report().line.fail_open.track>=1);
  }
  // Hold to talk and the line are never together: the line is ignored.
  {
    intervals.clear();level=.02;const r=transportRig();r.transport.configureInput({talkMode:'hold',line:LINE,agcOff:true});
    await r.transport.start('hold');await flush();r.started();run(2000);
    assert.equal(r.original.enabled,false,'closed by hold to talk');level=.5;run(500);assert.equal(r.original.enabled,false,'loud room: still closed');
    assert.equal(r.transport.inputView().line,'off');await r.transport.close('end');assert.equal(r.report().line,null);assert.equal(r.report().talk.mode,'hold');
  }
  // The page hidden mid-conversation holds it open.
  {
    intervals.clear();level=.02;const r=transportRig();r.transport.configureInput({line:LINE});
    await r.transport.start('hidden');await flush();r.started();run(2500);assert.equal(r.original.enabled,false);
    globalThis.document={hidden:true};run(100);assert.equal(r.original.enabled,true,'a hidden page is never gated');
    globalThis.document={hidden:false};run(25);assert.equal(r.original.enabled,true,'and a second of grace after');run(1100);assert.equal(r.original.enabled,true,'then the usual hold');run(900);assert.equal(r.original.enabled,false);
    delete globalThis.document;
    await r.transport.close('end');assert(r.report().line.fail_open.hidden>=1 && r.report().line.fail_open.visible>=1);
  }
} finally {removeFakes();delete globalThis.document;}

// ---- what the microphone is asked for --------------------------------------------------------------
assert.deepEqual(captureConstraints(),{audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true}});
assert.deepEqual(captureConstraints(false),captureConstraints());
assert.deepEqual(captureConstraints(true),{audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:false}});

// ---- the glue the real screen runs, pinned in the source ------------------------------------------
{
  const native=fs.readFileSync('src/app/static/voice-gptlive.js','utf8').replace(/\r\n/g,'\n');
  assert.match(native,/getUserMedia\(captureConstraints\(inputNow\.agcOff\)\)/,'the screen asks for the constraints the line needs');
  assert.match(native,/root\.addEventListener\('pointerdown',event=>\{if\(event\.isTrusted && event\.button===0 && open && !paused\)transport\.resumeAnalysis\(\);\}\);\n\s+mist\.addEventListener\('pointerdown',event=>\{if\(event\.isTrusted && event\.button===0 && open && !paused\)transport\.noteMist\(\);\}\);/,'any trusted tap on the screen resumes the analysis; only a press on Mist is "using Mist"');
  assert.match(native,/\(event\.key===' ' \|\| event\.key==='Enter'\) && open && !paused\)\{transport\.resumeAnalysis\(\);transport\.noteMist\(\);\}\}\);/);
  assert.match(native,/o\.gate=!o\.talk && inputConfig\.line \? createLineGate\(\{line:inputConfig\.line\}\) : null;/);
  assert.match(native,/if\(o\.talk \|\| o\.gate\)\{o\.inputTimer = setInterval\(\(\) => tickInput\(o\), 25\)/,'no timer unless a control needs one');
  assert.match(native,/line:o\.gate \? o\.gate\.report\(\{agcRequestedOff:inputConfig\.agcOff\}\) : null/);
  assert.match(native,/if\(o\.gate && !microphoneMuted\)\{/,'the line does not run while muted');
  assert.match(native,/const sending = o => o\.talk \? o\.talk\.talking\(\) : o\.gate \? o\.gate\.isOpen\(\) : true;/);
}

console.log('GPT-Live volume line: gate rules, fail-open reasons, counters, transport, report and glue passed.');
