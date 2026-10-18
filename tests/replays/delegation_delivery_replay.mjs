import assert from 'node:assert/strict';
import fs from 'node:fs';
const {createTaskDelivery} = await import('data:text/javascript;base64,' + Buffer.from(fs.readFileSync('src/app/static/task-delivery.js')).toString('base64'));
const deferred = () => { let resolve; const promise = new Promise(r => {resolve = r;}); return {promise, resolve}; };
const flush = async () => { for(let i=0;i<20;i++) await Promise.resolve(); };
function rig() {
  let now = 0, gap = true, played = 0, state = null, rows;
  const task = {id:'t',revision:1,event_id:'e',voice_session:'s',speakable:true,outcome:'Prepared for approval.'};
  rows = [task];
  const ops = [], tts = deferred(), playback = deferred();
  const scheduler = createTaskDelivery({now:()=>now,id:()=> 'delivery',gap:()=>gap,
    read: async()=> rows.map(t=>({...t,delivery:{state}})),
    transition: async(job,op,outcome)=> {
      ops.push([op,outcome]);
      if(!rows.some(t=>t.event_id===job.task.event_id && t.revision===job.task.revision)) return {ok:false};
      if(op==='claim') {if(state) return {ok:false};state='claimed';}
      if(op==='release') {if(state!=='claimed') return {ok:false};state=null;}
      if(op==='start') {if(state!=='claimed') return {ok:false};state='started';}
      if(op==='ack') {if(state!=='started') return {ok:false};state=outcome;}
      return {ok:true,task:rows[0]};
    },
    tts:()=>tts.promise,
    play:async(_,signal)=>{played++;await playback.promise;return !signal.aborted;},
  });
  return {scheduler,tts,playback,ops,task, get played(){return played;},get state(){return state;},
    setGap:v=>{gap=v;},setRows:v=>{rows=v;},setNow:v=>{now=v;},
    async begin(){await scheduler.tick();now=600;const run=scheduler.tick();await flush();return {run};}};
}
// Long TTS crossed by even brief user activity must wait for a fresh gap.
{
  const r=rig(), {run}=await r.begin();r.setNow(700);r.setGap(false);await r.scheduler.tick();
  r.setNow(701);r.setGap(true);r.tts.resolve(new Uint8Array([1,2]));await run;
  assert.equal(r.played,0);assert.equal(r.state,null);
  await r.scheduler.tick();r.setNow(1300);await r.scheduler.tick();assert.equal(r.played,0);
}
// Revision changed, permission revoked, or new session closed during TTS.
for(const replacement of [[],[{id:'t',revision:2,event_id:'new',voice_session:'s'}]]) {
  const r=rig(), {run}=await r.begin();r.setRows(replacement);r.tts.resolve(new Uint8Array([1,2]));await run;assert.equal(r.played,0);
}
// Audio completion ACK comes after playback, and reconnect cannot replay it.
{
  const r=rig(),{run}=await r.begin();r.tts.resolve(new Uint8Array([1,2]));await flush();
  assert.equal(r.played,1);assert.equal(r.state,'started');
  await r.scheduler.tick();assert.equal(r.played,1); // own audio isn't an interruption
  r.playback.resolve();await run;assert.equal(r.state,'completed');
  r.setNow(2000);await r.scheduler.tick();assert.equal(r.played,1);
}
// Barge-in is not task cancellation and never creates an automatic replay.
{
  const r=rig(),{run}=await r.begin();r.tts.resolve(new Uint8Array([1,2]));await flush();
  r.scheduler.interrupt();r.playback.resolve();await run;assert.equal(r.state,'interrupted');
  r.setNow(2000);await r.scheduler.tick();r.setNow(3000);await r.scheduler.tick();assert.equal(r.played,1);
  assert(!r.ops.some(([op])=>op==='cancel'));
}
console.log('delegation delivery replay passed');

// Initial DB read also owns the quiet reservation and is aborted on activity.
{
  const first=deferred();let now=0,gap=true,signal,claims=0;
  const scheduler=createTaskDelivery({now:()=>now,id:()=> 'id',gap:()=>gap,
    read:s=>{signal=s;return first.promise;},transition:async()=>{claims++;return {ok:false};}});
  await scheduler.tick();now=600;const run=scheduler.tick();await flush();
  gap=false;await scheduler.tick();assert(signal.aborted);
  gap=true;now=601;first.resolve([{id:'t',revision:1,event_id:'e',voice_session:'s',speakable:true}]);
  await run;assert.equal(claims,0);
}
// An unresolved fetch/body cannot leave the scheduler busy forever.
{
  const realSet=globalThis.setTimeout,realClear=globalThis.clearTimeout;
  const timers=new Map();let next=0,now=0,signal,reads=0;
  globalThis.setTimeout=fn=>{const id=++next;timers.set(id,fn);return id;};
  globalThis.clearTimeout=id=>timers.delete(id);
  try {
    const scheduler=createTaskDelivery({now:()=>now,id:()=> 'id',gap:()=>true,
      read:s=>{reads++;signal=s;return reads===1 ? new Promise(()=>{}) : Promise.resolve([]);}});
    await scheduler.tick();now=600;const run=scheduler.tick();await flush();
    const timeout=[...timers.values()][0];timeout();await run;assert(signal.aborted);
    now=20000;await scheduler.tick();assert.equal(reads,2);
  } finally {globalThis.setTimeout=realSet;globalThis.clearTimeout=realClear;}
}
