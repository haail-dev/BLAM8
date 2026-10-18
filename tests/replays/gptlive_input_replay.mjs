// GPT-Live microphone ownership, no real capture, provider or audio destination. The sent track is written
// in ONE place (applyInput) and a mute always wins; the meter reads a copy of the track, never the sent one.
// Run: node tests/replays/gptlive_input_replay.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {createNativeTransport} from '../../src/app/static/voice-gptlive.js';

const flush=async()=>{for(let i=0;i<30;i++)await Promise.resolve();};
const source=fs.readFileSync('src/app/static/voice-gptlive.js','utf8').replace(/\r\n/g,'\n');

// A track that counts every write to `enabled`, so "written every time, from one place" can be seen.
function makeTrack(extra={}) {
  const t={_enabled:true,writes:0,stops:0,readyState:'live',muted:false,
    get enabled(){return this._enabled;},set enabled(value){this._enabled=value;this.writes++;},
    stop(){this.stops++;this.readyState='ended';},...extra};
  return t;
}
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
function rig({tracks=[makeTrack()],onAddTrack,holdMedia=false}={}) {
  const media={getTracks:()=>tracks,getAudioTracks:()=>tracks};
  const calls=[],added=[],gate=deferred();
  const channel={close(){}};
  const peer={iceGatheringState:'complete',addTrack(track){added.push({track,enabled:track.enabled});onAddTrack?.(track);},createDataChannel:()=>channel,
    createOffer:async()=>({type:'offer',sdp:'v=offer'}),setLocalDescription(offer){this.localDescription=offer;},setRemoteDescription:async()=>{},
    addEventListener(){},removeEventListener(){},close(){}};
  const audio={muted:false,srcObject:null,play:async()=>{},pause(){}};
  const transport=createNativeTransport({media:()=>holdMedia?gate.promise:Promise.resolve(media),peer:()=>peer,audio:()=>audio,
    request:async(action,body)=>{calls.push([action,body]);return action==='session'?{ok:true,json:async()=>({session:'owned',transport:{sdp:'v=answer'}})}:{ok:true};},
    caption(){},status(){},playback(){}});
  return {transport,tracks,media,channel,peer,audio,calls,added,release:()=>gate.resolve(media)};
}

// ---- one writer -----------------------------------------------------------------------------------
// `enabled` on the sent track is assigned in exactly one place, applyInput, and nowhere else in the module.
{
  const writes=[...source.matchAll(/\btrack\.enabled=/g)];
  assert.equal(writes.length,1,'the sent track is written in one place');
  const at=writes[0].index, open=source.lastIndexOf('function applyInput(',at), close=source.indexOf('\n  }\n',at);
  assert(open>0 && close>at,'that one write is inside applyInput');
  assert(!source.slice(open,at).includes('\n  function '),'...and applyInput is where it starts');
  // Nothing else assigns to .enabled on a track of the media stream (a copy made for the meter is a different variable).
  assert.equal([...source.matchAll(/\.enabled\s*=[^=]/g)].filter(m=>source.slice(m.index-4,m.index)!=='copy').length,1);
}

// ---- mute wins, and a closed mic is not a muted mic ----------------------------------------------
{
  const r=rig();await r.transport.start('s');await flush();
  assert.equal(r.transport.inputMuted(),false);
  r.transport.setMuted(true);assert(r.tracks.every(t=>!t.enabled));assert.equal(r.transport.inputMuted(),true);
  assert.equal(r.transport.signals().muted,true,'an explicit mute is what the orb colours');
  r.transport.setMuted(false);assert(r.tracks.every(t=>t.enabled));assert.equal(r.transport.inputMuted(),false);
  // The flag is written every time it is applied, not only when it changes.
  const before=r.tracks[0].writes;r.transport.setMuted(false);r.transport.setMuted(false);assert.equal(r.tracks[0].writes,before+2);
  // A track that is disabled for some other reason is not "muted": the label, the orb and aria-pressed must not claim it is.
  r.tracks[0].enabled=false;
  assert.equal(r.transport.inputMuted(),false,'disabled but not muted by the person');
  assert.equal(r.transport.signals().muted,false);
  await r.transport.close('end');
}
// Muting before the tracks exist, and while they are being acquired, still opens the session muted: the track is
// disabled before it is handed to the peer connection.
for(const phase of ['before-start','during-capture']) {
  const r=rig({holdMedia:true});
  if(phase==='before-start')r.transport.setMuted(true);
  const start=r.transport.start('muted');
  if(phase==='during-capture'){await flush();r.transport.setMuted(true);}
  r.release();await start;await flush();
  assert(r.tracks.every(t=>!t.enabled),phase);
  assert(r.added.length===1 && r.added[0].enabled===false,phase+': disabled before addTrack');
  assert.equal(r.transport.inputMuted(),true,phase);
  r.transport.setMuted(false);assert(r.tracks.every(t=>t.enabled),phase);
  await r.transport.close('end');
}
// With nobody to write to, a mute is just remembered.
{
  const t=createNativeTransport({media:async()=>({}),peer:()=>({}),audio:()=>({}),request:async()=>({ok:true}),caption(){},status(){},playback(){}});
  t.setMuted(true);t.setMuted(false);assert.equal(t.inputMuted(),false);assert.equal(t.signals().muted,false);
}

// ---- the meter reads a copy of the track ---------------------------------------------------------------
// A fake MediaStream and AudioContext: what the meter is handed is what the replay can see.
class FakeStream {constructor(tracks=[]){this.tracks=[...tracks];}getTracks(){return this.tracks;}getAudioTracks(){return this.tracks;}}
function withAudio(run) {
  const real={ctx:globalThis.AudioContext,stream:globalThis.MediaStream};
  const sources=[];
  globalThis.MediaStream=FakeStream;
  globalThis.AudioContext=class{
    constructor(){this.state='running';}
    createMediaStreamSource(stream){sources.push(stream);return {connect(){},disconnect(){}};}
    createAnalyser(){return {fftSize:0,getFloatTimeDomainData(samples){samples.fill(.1);},disconnect(){}};}
    resume(){return Promise.resolve();}close(){return Promise.resolve();}
  };
  return Promise.resolve(run(sources)).finally(()=>{globalThis.AudioContext=real.ctx;globalThis.MediaStream=real.stream;});
}
function cloneable(clones) {
  const make=()=>makeTrack({clone(){const copy=make();copy.enabled=false;copy.writes=0;clones.push(copy);return copy;}});
  return make();
}
await withAudio(async sources=>{
  // A copy is made per track, switched on explicitly (the browser may hand back a disabled one), fed to the meter
  // in place of the sent track, and stopped with the conversation.
  {
    const clones=[],r=rig({tracks:[cloneable(clones)]});
    await r.transport.start('copy');await flush();
    assert.equal(clones.length,1);assert.equal(clones[0].enabled,true,'switched on explicitly');assert(clones[0].writes>=1);
    assert.equal(sources.length,1,'the meter listens to one stream');
    assert(sources[0] instanceof FakeStream && sources[0].getAudioTracks()[0]===clones[0],'...and it is the copy, not the sent track');
    assert.notEqual(sources[0],r.media);
    // Closing the sent track for any reason leaves the copy alone: the room is still measured.
    r.transport.setMuted(true);assert.equal(r.tracks[0].enabled,false);assert.equal(clones[0].enabled,true);
    assert.equal(clones[0].stops,0);
    await r.transport.close('end');
    assert(r.tracks[0].stops>=1 && clones[0].stops>=1,'the microphone is released only when the copy is stopped too');
  }
  // A failed start releases the copy as well as the original.
  {
    const clones=[],r=rig({tracks:[cloneable(clones)]});
    r.peer.createOffer=async()=>{throw Error('no offer');};
    await r.transport.start('fails');await flush();
    assert.equal(clones.length,1);assert(clones[0].stops>=1 && r.tracks[0].stops>=1);
  }
  // A close that arrives while the microphone is still being acquired makes no copy at all.
  {
    const clones=[],r=rig({tracks:[cloneable(clones)],holdMedia:true});
    const start=r.transport.start('early');await flush();
    const close=r.transport.close('end');r.release();await start;await close;
    assert.equal(clones.length,0);assert(r.tracks[0].stops>=1);
  }
  // No clone() on the track, or a clone that throws: the sent track is what the meter reads, and nothing leaks.
  {
    sources.length=0;const r=rig();
    await r.transport.start('plain');await flush();
    assert.equal(sources.length,1);assert.equal(sources[0],r.media,'falls back to the sent track');
    await r.transport.close('end');
  }
  {
    sources.length=0;const made=[];
    const good=()=>makeTrack({clone(){const c=makeTrack();made.push(c);return c;}});
    const bad=makeTrack({clone(){throw Error('cannot clone');}});
    const r=rig({tracks:[good(),bad]});
    await r.transport.start('throws');await flush();
    assert.equal(sources.length,1);assert.equal(sources[0],r.media);
    assert(made.length===1 && made[0].stops>=1,'a copy made before the failure is stopped');
    assert(r.tracks.every(t=>t.enabled));await r.transport.close('end');
  }
  // No MediaStream constructor (an old browser): no copy either.
  {
    sources.length=0;const clones=[];globalThis.MediaStream=undefined;
    const r=rig({tracks:[cloneable(clones)]});
    await r.transport.start('old');await flush();
    assert.equal(clones.length,0);assert.equal(sources[0],r.media);await r.transport.close('end');
    globalThis.MediaStream=FakeStream;
  }
});

console.log('GPT-Live microphone ownership: one writer, mute wins, a closed mic is not a muted mic, the meter reads a copy passed.');
