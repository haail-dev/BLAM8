import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
// Fake canvas geometry exercises the real renderer and lifecycle without browser or media access.
const source=readFileSync('src/app/static/assistant-glass-logo.js','utf8');
function run(count=2, reduced=false, fail=false){
 const noop=()=>{};
 const ctx=new Proxy({createLinearGradient:()=>({addColorStop:noop}),createRadialGradient:()=>({addColorStop:noop}),isPointInPath:(_path,x,y)=>x>94&&x<334&&y>39&&y<348,getImageData:(x,y,w,h)=>({data:new Uint8ClampedArray(w*h*4)}),createImageData:(w,h)=>({data:new Uint8ClampedArray(w*h*4)})},{get:(target,key)=>target[key]??noop});
 const roots=Array.from({length:count},()=>({isConnected:true,still:{hidden:false},children:[],querySelector(){return this.still;},querySelectorAll(){return this.children;},appendChild(el){this.children.push(el);}}));
 let observer,listener,frame,frames=0,cancelled=0;
 const motion={matches:reduced,addEventListener(_,fn){listener=fn;}};
 const document={hidden:false,querySelectorAll:()=>roots,createElement:()=>({hidden:false,getContext(){if(fail)throw Error('canvas unavailable');return ctx;},setAttribute:noop,remove(){}}),addEventListener:noop};
 const sandbox={Math,document,navigator:{},matchMedia:()=>motion,window:{requestIdleCallback:fn=>fn()},requestIdleCallback:fn=>fn(),performance:{now:()=>0},requestAnimationFrame:fn=>{frame=fn;return ++frames;},cancelAnimationFrame:()=>cancelled++,IntersectionObserver:class{constructor(fn){observer=fn;}observe(){}},Path2D:class{addPath(){}rect(){}moveTo(){}lineTo(){}arc(){}closePath(){}},DOMMatrix:class{},console:{warn:noop}};
 vm.runInNewContext(source,sandbox);
 return {roots,motion,get frames(){return frames;},get cancelled(){return cancelled;},change(){listener();},observe(entries){observer(entries);},tick(){frame(16);}};
}
const r=run();
assert.equal(r.roots.every(root=>root.children.length===1&&root.still.hidden),true);
r.observe([{target:r.roots[0],isIntersecting:true},{target:r.roots[1],isIntersecting:false}]);
const before=r.frames;r.tick();assert.ok(r.frames>before,'header keeps frames running while launcher is hidden');
r.motion.matches=true;r.change();assert.ok(r.cancelled);assert.ok(r.roots.every(root=>!root.still.hidden&&root.children[0].hidden));
r.motion.matches=false;r.change();assert.ok(r.roots.every(root=>root.still.hidden&&!root.children[0].hidden));
assert.ok(run(1).roots[0].still.hidden,'header works without launcher');
assert.ok(run(2,true).roots.every(root=>!root.still.hidden&&!root.children.length));
assert.ok(run(2,false,true).roots.every(root=>!root.still.hidden));
console.log('shared glass output lifecycle passed');
