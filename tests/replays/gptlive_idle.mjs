// GPT-Live idle close, client half. The timer is a plain helper (createIdleWatch) driven here with a
// fake clock and fake timers; the wiring inside createVoiceLive is sliced out by its own markers and
// run against stubs, the same way tests/replays/gptlive_replay.mjs runs its slices. Run: node tests/replays/gptlive_idle.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {createNativeTransport,createIdleWatch,conversationBusy,taskLive,taskAwaitsPerson,workingIds,anyFresh,speechSeconds,
  idleWarning,idleEndedNote,IDLE_WARN_LEAD,IDLE_LIMIT_CAP,IDLE_STALL_SECONDS,IDLE_SPEECH_CAP_SECONDS}
  from '../../src/app/static/voice-gptlive.js';

const flush=async()=>{for(let i=0;i<30;i++)await Promise.resolve();};

// ---- the lines the person reads -----------------------------------------------------------------
assert.equal(IDLE_WARN_LEAD,10);
assert.equal(idleWarning(10),'Closing in 10 seconds to save your minutes. Tap the screen to keep going.');
assert.equal(idleWarning(3),'Closing in 3 seconds to save your minutes. Tap the screen to keep going.');
assert.equal(idleEndedNote,'Ended after a quiet spell to save your minutes. Hold Mist to continue; results remain in chat.');

// ---- createIdleWatch ----------------------------------------------------------------------------
function watchRig({seconds=60,busy=()=>false,extended=null}={}) {
  let clock=0;
  const log={warns:[],clears:0,closes:[]};
  const timers={set(fn,ms){this.fn=fn;this.ms=ms;this.sets++;return 7;},clear(id){assert.equal(id,7);this.fn=null;this.clears++;},sets:0,clears:0,fn:null,ms:0};
  const listeners=new Map(), docListeners=new Map();
  const target={addEventListener(type,fn,capture){assert.equal(capture,true);listeners.set(type,fn);},removeEventListener(type,fn,capture){assert.equal(listeners.get(type),fn);assert.equal(capture,true);listeners.delete(type);}};
  const doc={hidden:false,
    addEventListener(type,fn,capture){assert.equal(capture,type==='keydown' ? true : undefined,type);docListeners.set(type,fn);},
    removeEventListener(type,fn,capture){assert.equal(docListeners.get(type),fn);assert.equal(capture,type==='keydown' ? true : undefined,type);docListeners.delete(type);}};
  const watch=createIdleWatch({seconds,now:()=>clock,busy,extended,onWarn:left=>log.warns.push(left),onClear:()=>log.clears++,
    onClose:spent=>log.closes.push(spent),target,doc,timers});
  return {watch,log,timers,listeners,docListeners,doc,at:s=>{clock=s*1000;},tick(s){clock=s*1000;watch.tick();}};
}

// Quiet from the start: nothing before 50 s, one warning at 50 s (not repeated each second), the close at 60 s with the seconds idle.
{
  const r=watchRig();r.watch.start();
  assert.equal(r.timers.ms,1000);assert(r.watch.running());assert.equal(r.watch.warning(),false);
  for(const s of [1,30,49])r.tick(s);
  assert.deepEqual(r.log.warns,[]);assert.deepEqual(r.log.closes,[]);assert.equal(r.watch.warning(),false);
  r.tick(50);r.tick(55);r.tick(59);
  assert.deepEqual(r.log.warns,[10]);assert.deepEqual(r.log.closes,[]);assert.equal(r.watch.warning(),true);
  r.tick(60);
  assert.deepEqual(r.log.closes,[60]);assert(!r.watch.running());assert.equal(r.timers.clears,1);assert.equal(r.watch.warning(),false);
  r.tick(61);r.tick(120);assert.deepEqual(r.log.closes,[60]); // closes once, then is finished
}
// A tap on the screen, any key on the page, and coming back to the page reset the clock and clear the warning.
for(const via of ['pointerdown','keydown','visibilitychange']) {
  const r=watchRig();r.watch.start();
  r.tick(55);assert.deepEqual(r.log.warns,[5]);assert.equal(r.watch.warning(),true);
  const fire=via==='pointerdown' ? r.listeners : r.docListeners;
  if(via==='visibilitychange'){r.doc.hidden=true;fire.get(via)();assert.equal(r.log.clears,0,'hiding the page is not presence');r.doc.hidden=false;}
  fire.get(via)();
  assert.equal(r.log.clears,1,via);assert.equal(r.watch.warning(),false,via);   // reset at 55 s
  r.tick(100);assert.deepEqual(r.log.warns,[5],via);  // 45 s since the reset: no new warning
  r.tick(105);assert.deepEqual(r.log.warns,[5,10],via);
  r.tick(114);assert.deepEqual(r.log.closes,[],via);
  r.tick(115);assert.deepEqual(r.log.closes,[60],via);
}
// Where the listeners go: taps on the overlay, keys anywhere on the page (the overlay is modal), page visibility.
{
  const r=watchRig();r.watch.start();
  assert.deepEqual([...r.listeners.keys()],['pointerdown']);
  assert.deepEqual([...r.docListeners.keys()].sort(),['keydown','visibilitychange']);
}
// Something working, a business turn or an event, holds the clock back; it counts from when that ends.
{
  let working=true;const r=watchRig({busy:()=>working});r.watch.start();
  for(const s of [10,50,300])r.tick(s);
  assert.deepEqual(r.log.closes,[]);assert.deepEqual(r.log.warns,[]);
  working=false;r.tick(301);r.tick(350);assert.deepEqual(r.log.warns,[10]); // 50 s after the last busy reading
  r.tick(359);assert.deepEqual(r.log.closes,[]);r.tick(360);assert.deepEqual(r.log.closes,[60]);
}
// Several busy sources: any one holds it, and every source is read every second (they may keep their own state).
{
  const reads={a:0,b:0};let a=false,b=false;
  const r=watchRig({busy:[()=>{reads.a++;return a;},()=>{reads.b++;return b;}]});r.watch.start();
  r.tick(10);assert.deepEqual(reads,{a:1,b:1});
  b=true;r.tick(100);assert.deepEqual(reads,{a:2,b:2});assert.deepEqual(r.log.warns,[]);
  b=false;a=true;r.tick(200);assert.deepEqual(r.log.closes,[]);assert.deepEqual(reads,{a:3,b:3});
  a=false;r.tick(259);assert.deepEqual(r.log.closes,[]);r.tick(260);assert.deepEqual(r.log.closes,[60]);
}
// Busy during the warning cancels it.
{
  let working=false;const r=watchRig({busy:()=>working});r.watch.start();
  r.tick(52);assert.deepEqual(r.log.warns,[8]);
  working=true;r.tick(53);assert.equal(r.log.clears,1);assert.equal(r.watch.warning(),false);
  working=false;r.tick(100);assert.deepEqual(r.log.warns,[8]);  // 47 s since the last busy reading
  r.tick(103);assert.deepEqual(r.log.warns,[8,10]);
}
// Zero (or nonsense) is off: no timer, no listeners, never closes.
for(const seconds of [0,-5,NaN,null,'60']) {
  const r=watchRig({seconds});r.watch.start();
  assert.equal(r.timers.sets,0,String(seconds));assert.equal(r.listeners.size+r.docListeners.size,0);assert(!r.watch.running());
  r.tick(10000);assert.deepEqual(r.log.closes,[]);assert.deepEqual(r.log.warns,[]);
}
// Start twice is one timer; stop removes the timer and every listener, and a stopped watch is silent.
{
  const r=watchRig();r.watch.start();r.watch.start();
  assert.equal(r.timers.sets,1);
  r.tick(55);r.watch.stop();r.watch.stop();
  assert.equal(r.timers.clears,1);assert.equal(r.listeners.size,0);assert.equal(r.docListeners.size,0);
  r.tick(300);assert.deepEqual(r.log.closes,[]);
  r.watch.touch();assert.equal(r.log.clears,0); // touching a stopped watch does nothing
  r.watch.hold(100);r.watch.start();r.tick(360);assert.deepEqual(r.log.closes,[60],'a hold made while stopped is ignored');
}
// A late tick (a suspended page) reports the real time idle.
{
  const r=watchRig();r.watch.start();r.tick(5);r.tick(125);
  assert.deepEqual(r.log.closes,[125]);
}
// A clock that steps backwards restarts the count from there; it does not stall the close.
{
  const r=watchRig();r.at(100);r.watch.start();r.tick(130);assert.deepEqual(r.log.warns,[]);
  r.tick(10);assert.deepEqual(r.log.closes,[]);
  r.tick(69);assert.deepEqual(r.log.closes,[]);r.tick(70);assert.deepEqual(r.log.closes,[60]);
}
// A short setting still only warns in its second half; a one-second setting just closes.
{
  const r=watchRig({seconds:4});r.watch.start();r.tick(1);assert.deepEqual(r.log.warns,[]);r.tick(2);assert.deepEqual(r.log.warns,[2]);
  const q=watchRig({seconds:1});q.watch.start();q.tick(1);assert.deepEqual(q.log.warns,[]);assert.deepEqual(q.log.closes,[1]);
}
// The default timer is a real 1 s interval that can be cleared (no fake given).
{
  const closes=[];const w=createIdleWatch({seconds:60,onClose:s=>closes.push(s)});
  w.start();assert(w.running());w.stop();assert(!w.running());assert.deepEqual(closes,[]);
}
// A card waiting for the owner doubles the time allowed (bounded by the cap); the warning still comes ten seconds before the close.
{
  const r=watchRig({extended:()=>true});r.watch.start();
  r.tick(109);assert.deepEqual(r.log.warns,[]);r.tick(110);assert.deepEqual(r.log.warns,[10]);
  r.tick(119);assert.deepEqual(r.log.closes,[]);r.tick(120);assert.deepEqual(r.log.closes,[120]);
}
{
  // It appears part-way through: the count starts again from then (the card is activity), with the longer allowance.
  let card=false;const r=watchRig({extended:()=>card});r.watch.start();
  r.tick(40);card=true;r.tick(41);assert.deepEqual(r.log.warns,[]);
  r.tick(150);assert.deepEqual(r.log.warns,[]);r.tick(151);assert.deepEqual(r.log.warns,[10]);
  r.tick(160);assert.deepEqual(r.log.closes,[]);r.tick(161);assert.deepEqual(r.log.closes,[120]);
}
{
  // It goes away (answered or dismissed) while the longer allowance is running: the count starts again with the normal one.
  let card=true;const r=watchRig({extended:()=>card});r.watch.start();
  r.tick(100);assert.deepEqual(r.log.warns,[]);        // 100 s idle against 120 s: the warning would come at 110
  card=false;r.tick(101);assert.equal(r.log.clears,0); // nothing was showing; the count restarts at 101 s
  r.tick(150);assert.deepEqual(r.log.warns,[]);        // 49 s since
  r.tick(151);assert.deepEqual(r.log.warns,[10]);
  r.tick(160);assert.deepEqual(r.log.closes,[]);r.tick(161);assert.deepEqual(r.log.closes,[60]);
}
{
  // ... and a warning that was showing for the longer allowance is cleared.
  let card=true;const r=watchRig({extended:()=>card});r.watch.start();
  r.tick(115);assert.deepEqual(r.log.warns,[5]);assert.equal(r.watch.warning(),true);
  card=false;r.tick(116);assert.equal(r.log.clears,1);assert.equal(r.watch.warning(),false);
}
{
  const r=watchRig({seconds:1000,extended:()=>true});r.watch.start();
  assert.equal(IDLE_LIMIT_CAP,1800);
  r.tick(1789);assert.deepEqual(r.log.warns,[]);r.tick(1790);assert.deepEqual(r.log.warns,[10]);
  r.tick(1800);assert.deepEqual(r.log.closes,[1800]);
}
// Speech the voice has not finished saying holds the clock: hold(seconds) is a backlog that builds, up to a cap.
{
  const r=watchRig();r.watch.start();
  r.watch.hold(30);r.tick(10);r.tick(29);assert.deepEqual(r.log.warns,[]);
  r.tick(88);assert.deepEqual(r.log.closes,[]);r.tick(89);assert.deepEqual(r.log.closes,[60]); // counted from when the speech ended (29 s)
}
{
  const r=watchRig();r.watch.start();
  r.watch.hold(30);r.watch.hold(30);            // a backlog: 60 s of speech to get through
  r.tick(59);r.tick(118);assert.deepEqual(r.log.closes,[]);
  r.tick(119);assert.deepEqual(r.log.closes,[60]);   // counted from the last held reading (59 s)
}
{
  const r=watchRig();r.watch.start();
  r.watch.hold(10);r.at(50);r.watch.hold(10);   // the second starts from now: the first had run out
  r.tick(59);r.tick(118);assert.deepEqual(r.log.closes,[]);r.tick(119);assert.deepEqual(r.log.closes,[60]);
}
{
  const r=watchRig();r.watch.start();r.watch.hold(100000);assert.equal(IDLE_SPEECH_CAP_SECONDS,120);
  r.tick(119);r.tick(120);assert.deepEqual(r.log.closes,[]);
  r.tick(178);assert.deepEqual(r.log.closes,[]);r.tick(179);assert.deepEqual(r.log.closes,[60]);   // 120 s of speech at most, then the usual count
  const q=watchRig();q.watch.start();for(const bad of [0,-3,NaN,undefined,'9'])q.watch.hold(bad);q.tick(5);q.tick(60);
  assert.deepEqual(q.log.closes,[60],'nothing to hold');
  const n=watchRig();n.watch.start();n.watch.hold(NaN);n.watch.hold(30);n.tick(29);n.tick(88);
  assert.deepEqual(n.log.closes,[],'a bad value does not spoil the holds after it');n.tick(89);assert.deepEqual(n.log.closes,[60]);
}
{
  // What was queued up does not carry over a stop and restart.
  const r=watchRig();r.watch.start();r.watch.hold(100);r.watch.stop();r.watch.start();
  r.tick(60);assert.deepEqual(r.log.closes,[60]);
}

// ---- what counts as busy ------------------------------------------------------------------------
{
  const idle={started:true,eventMoved:false,working:false,agentLevel:0,userLevel:0,muted:false};
  assert.equal(conversationBusy(idle),false);
  assert.equal(conversationBusy({...idle,started:false}),true,'mid-start');
  assert.equal(conversationBusy({...idle,eventMoved:true}),true,'a new server event');
  assert.equal(conversationBusy({...idle,working:true}),true,'something is working');
  assert.equal(conversationBusy({...idle,agentLevel:.2}),true,'the voice is audible');
  assert.equal(conversationBusy({...idle,agentLevel:.01}),false,'room tone is not speech');
  assert.equal(conversationBusy({...idle,userLevel:.2}),true,'the person is talking and the words have not arrived yet');
  assert.equal(conversationBusy({...idle,userLevel:.2,muted:true}),false,'a muted microphone is idle');
  assert.equal(conversationBusy({...idle,userLevel:.01}),false);
  assert(taskLive({status:'working'}));assert(!taskLive({status:'waiting'}));
  for(const row of [{status:'queued'},{status:'working'},{proposal_state:'executing',status:'done'}])assert(taskLive(row),JSON.stringify(row));
  for(const row of [{status:'waiting',result_kind:'waiting'},{status:'done',result_kind:'lookup_answer'},{status:'working',stale:'stalled'},
      {status:'working',stale:'expired'},{status:'failed'},{status:'done',result_kind:'action_completed'}])assert(!taskLive(row),JSON.stringify(row));
}
// What is working: running turns and live tasks, by id.
{
  const activities=new Map([['a',{state:'running'}],['b',{state:'finished'}]]);
  const tasks=new Map([['t1',{status:'working'}],['t2',{status:'waiting',result_kind:'waiting'}],['t3',{status:'queued'}]]);
  assert.deepEqual(workingIds(activities,tasks),['turn:a','task:t1','task:t3']);
  assert.deepEqual(workingIds(new Map(),new Map()),[]);
}
// A row that has been "working" for longer than the stall threshold no longer holds the conversation.
{
  assert.equal(IDLE_STALL_SECONDS,300);
  const seen=new Map();
  assert.equal(anyFresh(['turn:a'],seen,1000),true);
  assert.equal(anyFresh(['turn:a'],seen,1000+299000),true);
  assert.equal(anyFresh(['turn:a'],seen,1000+300000),false);
  assert.equal(anyFresh(['turn:a','task:b'],seen,1000+300000),true,'a newer row is fresh');
  assert.equal(anyFresh(['turn:a'],seen,1000+301000),false);
  assert.equal(anyFresh([],seen,1000+302000),false);assert.equal(seen.size,0,'rows that finished are forgotten');
  assert.equal(anyFresh(['turn:a'],seen,1000+303000),true,'the same id coming back is new work');
}
// A card, draft or result waiting for the owner.
{
  for(const row of [{status:'waiting',result_kind:'waiting'},{result_kind:'needs_input',status:'working'},{status:'waiting'},{priority:0,status:'working'}])
    assert(taskAwaitsPerson(row),JSON.stringify(row));
  for(const row of [{status:'working'},{status:'queued'},{status:'done',result_kind:'lookup_answer'},{status:'waiting',stale:'expired'},
      {status:'waiting',result_kind:'waiting',stale:'stalled'},{status:'failed',result_kind:'failed'},{status:'done',result_kind:'action_completed'}])
    assert(!taskAwaitsPerson(row),JSON.stringify(row));
}
// About fifteen characters of text per second of speaking.
{
  assert.equal(speechSeconds('x'.repeat(150)),10);assert.equal(speechSeconds(''),0);
  for(const bad of [undefined,null,42,{}])assert.equal(speechSeconds(bad),0);
}

// ---- the transport hands over the setting and reports an idle close -----------------------------
function transportRig(created) {
  const tracks=[{enabled:true,stop(){}}];
  const media={getTracks:()=>tracks,getAudioTracks:()=>tracks};
  const channel={close(){}};
  const peer={iceGatheringState:'complete',addTrack(){},createDataChannel:()=>channel,createOffer:async()=>({type:'offer',sdp:'v=offer'}),
    setLocalDescription(offer){this.localDescription=offer;},setRemoteDescription:async()=>{},addEventListener(){},removeEventListener(){},close(){}};
  const audio={muted:false,srcObject:null,play:async()=>{},pause(){}};
  const calls=[],started=[],ended=[];
  const transport=createNativeTransport({media:async()=>media,peer:()=>peer,audio:()=>audio,
    request:async(action,body)=>{calls.push([action,body]);return action==='session'?{ok:true,json:async()=>created}:{ok:true};},
    caption(){},status(){},playback(){},sessionStarted:(...args)=>started.push(args),sessionEnded:id=>ended.push(id)});
  return {transport,channel,calls,started,ended};
}
const sessionStarted=r=>r.channel.onmessage({data:JSON.stringify({type:'session.started'})});
for(const [given,expected] of [[45,45],[60,60],[0,0],[undefined,0],['60',0],[-3,0],[1.5,0],[999999,1800]]) {
  const r=transportRig({session:'owned',transport:{sdp:'v=answer'},idle_close_seconds:given});
  await r.transport.start('voice');await flush();sessionStarted(r);
  assert.equal(r.started.length,1,String(given));assert.equal(r.started[0][3],expected,String(given));
  await r.transport.close('end');
}
{
  const r=transportRig({session:'owned',transport:{sdp:'v=answer'},idle_close_seconds:60});
  await r.transport.start('voice');await flush();sessionStarted(r);
  r.transport.noteIdle(61);
  await r.transport.close('idle');
  const body=r.calls.find(([a,b])=>a==='control'&&b.operation==='close')[1];
  assert.equal(body.close_reason,'idle');assert.equal(body.idle_seconds,61);assert.equal(body.session,'owned');
  assert.deepEqual(r.ended,['voice']);
}
// The count rides only on an idle close, whatever was noted, and never goes out of range.
for(const [note,reason,has] of [[40,'end',false],[40,'hold_stop',false],[999999,'idle',86400],[-4,'idle',0],[NaN,'idle',0]]) {
  const r=transportRig({session:'owned',transport:{sdp:'v=answer'}});
  await r.transport.start('voice');await flush();sessionStarted(r);
  r.transport.noteIdle(note);await r.transport.close(reason);
  const body=r.calls.find(([a,b])=>a==='control'&&b.operation==='close')[1];
  if(has===false)assert(!('idle_seconds' in body),reason);else assert.equal(body.idle_seconds,has,String(note));
}
{
  const r=transportRig({session:'owned',transport:{sdp:'v=answer'}});
  r.transport.noteIdle(30);await r.transport.start('voice');await flush();sessionStarted(r);
  await r.transport.close('idle'); // noted before any owner existed: nothing to report
  assert(!('idle_seconds' in r.calls.find(([a,b])=>a==='control'&&b.operation==='close')[1]));
}

// ---- the wiring inside createVoiceLive ----------------------------------------------------------
const native=fs.readFileSync('src/app/static/voice-gptlive.js','utf8').replace(/\r\n/g,'\n');
const start=native.indexOf('  // ── idle auto-close wiring'), end=native.indexOf('  // ── end idle auto-close wiring');
assert(start>0 && end>start,'wiring markers present');
const wiring=native.slice(start,end);
function wiringRig() {
  let clock=0;const calls=[];
  const timers={set(fn){this.fn=fn;return 3;},clear(){this.fn=null;}};
  const state={started:true,agent:0,user:0,muted:false};
  const els={status:{textContent:'',hidden:true}};
  const c=vm.createContext({
    after:0,root:{tag:'root'},document:{hidden:false,addEventListener(){},removeEventListener(){}},activities:new Map(),tasks:new Map(),els,Date:{now:()=>clock},
    createIdleWatch:options=>createIdleWatch({...options,now:()=>clock,timers,target:null,doc:null}),conversationBusy,workingIds,anyFresh,taskAwaitsPerson,
    idleWarning,idleEndedNote,IDLE_STALL_SECONDS,
    transport:{started:()=>state.started,signals:()=>({agent:state.agent,user:state.user,muted:state.muted}),noteIdle:seconds=>calls.push(['noteIdle',seconds])},
    status:text=>{calls.push(['status',text]);els.status.textContent=text.replace(/^Listening — GPT-Live$/,'');els.status.hidden=!els.status.textContent;},
    pauseConversation:(reason,note)=>calls.push(['pause',reason,note]),
  });
  vm.runInContext(wiring,c);
  return {c,calls,timers,state,els,tick(s){clock=s*1000;timers.fn?.();}};
}
{
  // Quiet and muted (a muted mic only means no speech arrives): warns at 50 s, ends paused as "idle" at 60 s.
  const r=wiringRig();r.c.startIdleWatch(60);
  r.tick(40);assert.deepEqual(r.calls,[]);
  r.tick(50);assert.deepEqual(r.calls.at(-1),['status','Closing in 10 seconds to save your minutes. Tap the screen to keep going.']);
  r.tick(60);
  assert.deepEqual(r.calls.slice(-2),[['noteIdle',60],['pause','idle','Ended after a quiet spell to save your minutes. Hold Mist to continue; results remain in chat.']]);
  assert.equal(r.timers.fn,null);
}
{
  // A server event while the warning shows (poll moved `after`) clears it and restarts the count.
  const r=wiringRig();r.c.startIdleWatch(60);
  r.tick(52);assert.match(r.els.status.textContent,/^Closing in 8 seconds/);
  r.c.after=4;r.tick(53);
  assert.equal(r.els.status.textContent,'');assert.deepEqual(r.calls.at(-1),['status','Listening — GPT-Live']); // there was no line before the warning
  r.tick(112);assert(!r.calls.some(([k])=>k==='pause'));r.tick(113);assert.deepEqual(r.calls.at(-1),['pause','idle',idleEndedNote]);
}
{
  // The line that was showing before the warning comes back when the warning is cleared.
  const r=wiringRig();r.els.status.textContent='Audio blocked by browser. Tap Retry audio.';r.els.status.hidden=false;
  r.c.startIdleWatch(60);r.tick(55);assert.match(r.els.status.textContent,/^Closing in 5 seconds/);
  r.c.after=1;r.tick(56);
  assert.equal(r.els.status.textContent,'Audio blocked by browser. Tap Retry audio.');assert.equal(r.els.status.hidden,false);
}
{
  // Another message on the status line is not wiped by clearing a warning that is no longer showing.
  const r=wiringRig();r.c.startIdleWatch(60);
  r.tick(55);r.els.status.textContent='Result ready in chat';r.c.after=2;r.tick(56);
  assert.equal(r.els.status.textContent,'Result ready in chat');
}
{
  // Working turn, task, audible voice, speech not yet transcribed and a not-yet-started session each hold the close back.
  for(const hold of ['turn','task','voice','talking','starting']) {
    const r=wiringRig();r.c.startIdleWatch(60);
    if(hold==='turn')r.c.activities.set('t',{state:'running'});
    if(hold==='task')r.c.tasks.set('k',{status:'working'});
    if(hold==='voice')r.state.agent=.4;
    if(hold==='talking')r.state.user=.4;
    if(hold==='starting')r.state.started=false;
    for(const s of [30,59,60,200,299])r.tick(s);
    assert(!r.calls.some(([k])=>k==='pause'),hold);
  }
  // A muted microphone's level does not hold it; the same reading unmuted does.
  const r=wiringRig();r.c.startIdleWatch(60);r.state.user=.4;r.state.muted=true;
  r.tick(59);r.tick(60);assert.deepEqual(r.calls.at(-1),['pause','idle',idleEndedNote]);
}
{
  // A turn or task that never reports finishing stops holding after the stall threshold; the usual count then runs.
  for(const kind of ['turn','task']) {
    const r=wiringRig();r.c.startIdleWatch(60);
    if(kind==='turn')r.c.activities.set('t',{state:'running'});else r.c.tasks.set('k',{status:'queued'});
    for(let s=1;s<=299;s++)r.tick(s);
    assert(!r.calls.some(([k])=>k==='pause'),kind);
    r.tick(300);r.tick(358);assert(!r.calls.some(([k])=>k==='pause'),kind);
    r.tick(360);assert.deepEqual(r.calls.at(-1),['pause','idle',idleEndedNote],kind);
  }
}
{
  // A card waiting for the owner doubles the time allowed; the warning follows the longer limit.
  const r=wiringRig();r.c.startIdleWatch(60);
  r.c.tasks.set('card',{status:'waiting',result_kind:'waiting'});
  r.tick(1);r.tick(60);r.tick(109);assert(!r.calls.some(([k])=>k==='status'||k==='pause'));
  r.tick(111);assert.deepEqual(r.calls.at(-1),['status','Closing in 10 seconds to save your minutes. Tap the screen to keep going.']);
  r.tick(120);assert(!r.calls.some(([k])=>k==='pause'));r.tick(121);assert.deepEqual(r.calls.at(-1),['pause','idle',idleEndedNote]);
  assert.deepEqual(r.calls.at(-2),['noteIdle',120]);
}
{
  // Off, and stopped, do nothing; a restart replaces the earlier watch.
  const r=wiringRig();r.c.startIdleWatch(0);assert(!r.timers.fn);r.tick(500);assert.deepEqual(r.calls,[]);
  r.c.startIdleWatch(60);assert(r.timers.fn);r.c.stopIdleWatch();assert.equal(r.timers.fn,null);r.tick(500);assert.deepEqual(r.calls,[]);
  r.c.stopIdleWatch();
}

// The pause an idle close uses is the one a hold on Mist uses, now with a reason and a line of its own.
{
  const pauseSource='function pauseConversation('+native.split('  function pauseConversation(')[1].split('  async function restart()')[0];
  const run=(...args)=>{
    const calls=[],button={hidden:false};
    const c=vm.createContext({open:true,paused:false,workingTurn:'t',terminalTurn:false,activities:new Map([['t',{state:'running'}]]),
      root:{dataset:{},querySelector:()=>button},transport:{close:reason=>calls.push(['close',reason])},
      status:text=>calls.push(['status',text]),paintReady:()=>calls.push(['ready']),paintFeed:()=>calls.push(['feed'])});
    vm.runInContext(pauseSource,c);c.pauseConversation(...args);
    return {c,calls,button};
  };
  let r=run('idle',idleEndedNote);
  assert.deepEqual(r.calls,[['close','idle'],['status',idleEndedNote],['ready'],['feed']]);
  assert.equal(r.c.paused,true);assert.equal(r.c.root.dataset.paused,'true');assert.equal(r.button.hidden,true);assert.equal(r.c.activities.size,0);
  assert.equal(r.c.workingTurn,null);assert.equal(r.c.terminalTurn,true);
  r=run();   // the hold on Mist: unchanged
  assert.deepEqual(r.calls.slice(0,2),[['close','hold_stop'],['status','Conversation stopped. Hold Mist to reconnect; results remain in chat.']]);
  r=run('idle',idleEndedNote);r.c.paused=true;r.calls.length=0;r.c.pauseConversation('idle',idleEndedNote);
  assert.deepEqual(r.calls,[],'already paused: nothing more happens');
}

// The pieces outside the slice are connected: the setting arrives with the session, speech touches the
// clock and the voice's words hold it, a paused screen is what an idle close leaves, and every way a
// session ends stops the watch.
assert.match(native,/sessionStarted:\(id,snapshot,context,idleLimit\)=>\{[^\n]*startIdleWatch\(idleLimit\)/);
assert.match(native,/sessionEnded:id=>\{stopIdleWatch\(\);endAllowanceSession\(id\);\}/);
assert.match(native,/caption:message=>\{\n\s*idleWatch\?\.touch\(\);/);
assert.match(native,/captionIds\.add\(message\.event_id\);[^\n]*\n\s*if\(message\.type==='session\.output_transcript\.delta'\)idleWatch\?\.hold\(speechSeconds\(message\.delta\)\);/);
assert.match(native,/function pauseConversation\(reason='hold_stop',\s*note='Conversation stopped\. Hold Mist to reconnect; results remain in chat\.'\)/);
assert.match(native,/transport\.close\(reason\);/);
console.log('Idle close: warning line, clock resets, busy readings, longer limit for a waiting card, speech hold, transport report and wiring passed.');
