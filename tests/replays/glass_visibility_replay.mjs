// Exercise the actual renderer's speech and visibility lifecycle without a GPU.
import assert from 'node:assert/strict';
import {createGlassCore} from '../../src/app/static/glass-core.js';

const frames=new Map(), listeners=new Map();
let frameId=0, now=0, observer, reads=0;
globalThis.requestAnimationFrame=fn=>{frames.set(++frameId,fn);return frameId;};
globalThis.cancelAnimationFrame=id=>frames.delete(id);
const reduced={matches:false,addEventListener(_,fn){this.change=fn;},removeEventListener(){}};
globalThis.matchMedia=()=>reduced;
globalThis.ResizeObserver=class {observe(){} disconnect(){}};
globalThis.IntersectionObserver=class {
  constructor(fn){this.callback=fn;observer=this;}
  observe(target){this.target=target;}
  disconnect(){this.disconnected=true;}
};
const gl=new Proxy({}, {get:(_,key)=>{
  if(key==='getShaderParameter' || key==='getProgramParameter')return ()=>true;
  if(key==='getExtension')return ()=>null;
  if(/^create/.test(key))return ()=>({});
  return ()=>{};
}});
const element=()=>({
  dataset:{},classList:{add(){},remove(){}},prepend(){},append(){},remove(){},
  addEventListener(){},removeEventListener(){},setAttribute(){},querySelector(){return null;},
  getContext(){return gl;},
});
globalThis.document={
  hidden:false,createElement:element,
  addEventListener:(name,fn)=>listeners.set(name,fn),
  removeEventListener:(name)=>listeners.delete(name),
};
function advance(seconds){
  for(let i=0;i<seconds*30;i++){
    now+=1000/30+.01;
    const pending=[...frames.values()];frames.clear();
    for(const fn of pending)fn(now);
  }
}
const orb=element(), stage=element(), activity=[];
let signals={ready:true,inputActive:true,muted:false,analysisAvailable:true,user:.5,agent:0,agentPlaying:false};
const glass=createGlassCore(orb,()=>{reads++;return signals;},on=>activity.push(on),{visibilityTarget:stage});
assert.equal(glass.snapshot().failed,false);
assert.equal(observer.target,stage,'the caller may keep audio sampling tied to the whole voice surface');
observer.callback([{isIntersecting:true}]);advance(2);
assert.equal(glass.snapshot().mode,'user');
assert.equal(glass.snapshot().ribbon,1);
assert(glass.snapshot().inputEnvelope>.4);
const before=reads;
// The orb may be off-screen; the observed stage is still intersecting.
observer.callback([{isIntersecting:true}]);advance(2);
assert(reads>before+30,'speech sampling must continue on the adjacent page');
signals={...signals,user:0,inputActive:false,muted:true};advance(2);
assert.equal(glass.snapshot().userActive,false);
assert.equal(glass.snapshot().ribbon,0);
signals={...signals,thinking:true};advance(2);
assert.equal(glass.snapshot().mode,'thinking');
assert.equal(glass.snapshot().ribbon,1);
signals={...signals,thinking:false,agentPlaying:true,agent:.6};advance(2);
assert.equal(glass.snapshot().mode,'agent');
assert.equal(glass.snapshot().ribbon,0);
assert(glass.snapshot().agentEnvelope>.5);
document.hidden=true;listeners.get('visibilitychange')();
let stopped=reads;advance(2);assert.equal(reads,stopped);assert.equal(activity.at(-1),false);
document.hidden=false;listeners.get('visibilitychange')();advance(1);
assert(reads>stopped);
reduced.matches=true;reduced.change();
stopped=reads;advance(2);assert.equal(reads,stopped);assert.equal(glass.snapshot().phase,'reduced-b');
reduced.matches=false;reduced.change();advance(1);
observer.callback([{isIntersecting:false}]);
stopped=reads;advance(2);assert.equal(reads,stopped);
glass.destroy();advance(1);assert.equal(reads,stopped);assert(observer.disconnected);
assert.equal(frames.size,0);
// Existing callers keep the default orb-based visibility contract.
const legacy=createGlassCore(orb,()=>signals);
assert.equal(observer.target,orb);legacy.destroy();
console.log('Glass speech, adjacent-page sampling, hidden/reduced-motion and disposal passed.');
