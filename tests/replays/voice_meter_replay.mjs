// Read-only production meter ownership, no real capture/provider or audio destination.
import assert from 'node:assert/strict';
import {createVoiceMeter} from '../../src/app/static/voice-meter.js';
let context,disconnections=0,resumes=0,closes=0,connections=0,failAnalyser=false;
const stream=level=>({level,tracks:[{enabled:true,muted:false,readyState:'live'}],getAudioTracks(){return this.tracks;}});
class Context {
 constructor(){context=this;this.state='running';this.destination={};}
 createMediaStreamSource(stream){this.last=stream;return {connect:node=>{assert.notEqual(node,this.destination);connections++;},disconnect(){disconnections++;}};}
 createAnalyser(){if(failAnalyser)throw Error('unsupported analyser');const stream=this.last;return {getFloatTimeDomainData(samples){samples.fill(stream.level);},disconnect(){disconnections++;}};}
 resume(){resumes++;this.state='running';return Promise.resolve();}
 close(){closes++;this.state='closed';return Promise.resolve();}
}
globalThis.AudioContext=Context;
const input=stream(.03),output=stream(.06),audio={paused:false,muted:false,volume:1,readyState:4};
const meter=createVoiceMeter();meter.input(input);meter.output(output);
let value=meter.sample(audio,false,true);assert(value.agent>value.user && value.user>0 && value.wave[0]>0);
assert.equal(meter.sample(audio,false,false).agent,0);
for(const field of ['paused','muted']){audio[field]=true;assert.equal(meter.sample(audio,false,true).agent,0);audio[field]=false;}
value=meter.sample(audio,true,true);assert.equal(value.user,0);assert(value.agent>0);assert(value.wave.every(x=>x===0));
input.tracks[0].enabled=false;assert.equal(meter.sample(audio,false,true).user,0);assert(meter.sample(audio,false,true).agent>0);
context.state='suspended';meter.setActive(false);meter.setActive(true);meter.input(input);meter.output(output);assert.equal(resumes,0);
meter.resume();assert.equal(resumes,1);
// With the visual paused nothing resumes a suspended context, unless forced (the volume line's measurement does not depend on the orb).
meter.setActive(false);context.state='suspended';meter.resume();assert.equal(resumes,1,'visual paused: not resumed');
meter.resume(true);assert.equal(resumes,2,'forced: resumed');assert.equal(context.state,'running');meter.resume(true);assert.equal(resumes,2,'a running context is left alone');
meter.setActive(true);
meter.destroy();meter.destroy();assert.equal(closes,1);assert.equal(meter.sample(audio,false,true).agent,0);assert.equal(meter.sample(audio,false,true).user,0);
const before=disconnections;failAnalyser=true;const unsupported=createVoiceMeter();unsupported.input(input);assert.equal(disconnections,before+1);unsupported.destroy();
globalThis.AudioContext=class{constructor(){throw Error('no audio analysis');}};
const absent=createVoiceMeter();assert.equal(absent.sample(audio,false,true).analysisAvailable,false);absent.destroy();
// The peak of the input window (what the volume line compares with its threshold) and the context state: read-only, null when unreadable.
{
  let state='running',window=[0,.02,-.3,.1],throwing=false;
  globalThis.AudioContext=class{
    constructor(){this.state=state;}
    createMediaStreamSource(){return {connect(){},disconnect(){}};}
    createAnalyser(){return {getFloatTimeDomainData(samples){if(throwing)throw Error('analyser failed');samples.fill(0);window.forEach((value,i)=>{samples[i]=value;});},disconnect(){}};}
    resume(){return Promise.resolve();}close(){return Promise.resolve();}
    set(next){this.state=next;}
  };
  const meter=createVoiceMeter();
  assert.equal(meter.peakLevel(),null,'no input yet');assert.equal(meter.sampleLevel(),null);
  assert.equal(meter.contextState(),'running');
  meter.input(stream(.5));
  assert.equal(meter.peakLevel(),Math.fround(.3),'the largest absolute sample, negative values included');
  window=[0,0,0,0];assert.equal(meter.peakLevel(),0,'exactly zero is a reading, not a failure');
  window=[.9,-.95,.2,0];assert.equal(meter.peakLevel(),Math.fround(.95));
  // the visual flag does not stop the measurement
  meter.setActive(false);assert.equal(meter.peakLevel(),Math.fround(.95));meter.setActive(true);
  throwing=true;assert.equal(meter.peakLevel(),null,'an analyser that throws is unreadable');throwing=false;
  assert.equal(meter.peakLevel(),Math.fround(.95));
  // a context that is not running cannot be read, and says what state it is in
  const instances=[];const Base=globalThis.AudioContext;
  globalThis.AudioContext=class extends Base{constructor(){super();instances.push(this);}};
  const meter2=createVoiceMeter();meter2.input(stream(.5));
  for(const next of ['suspended','interrupted','closed']){instances[0].set(next);assert.equal(meter2.contextState(),next);assert.equal(meter2.peakLevel(),null,next);}
  instances[0].set('running');assert.equal(meter2.peakLevel(),Math.fround(.95));
  meter2.destroy();assert.equal(meter2.peakLevel(),null,'destroyed');assert.equal(meter2.contextState(),null,'and no context');
  meter.destroy();
  // no AudioContext at all
  globalThis.AudioContext=class{constructor(){throw Error('no audio analysis');}};
  const none=createVoiceMeter();assert.equal(none.contextState(),null);assert.equal(none.peakLevel(),null);none.input(stream(.5));assert.equal(none.peakLevel(),null);none.destroy();
}
assert.equal(connections,4);console.log('Owned meter gates, independent input/output, explicit resume only, partial setup cleanup, unsupported fallback and the peak read passed.');
