import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync('src/app/static/concierge_panel.js','utf8');
const load='function load() {'+source.split('  function load() {')[1].split('  function reloadPanel()')[0];
const deadline='function openingDeadline(promise, onTimeout) {'+source.split('  function openingDeadline(promise, onTimeout) {')[1].split('  function csrfToken()')[0];
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
for(const stalled of ['headers','body']) {
 const gate=deferred(),timers=new Map(),nodes=[],events=[];let id=0,mounts=0,aborted=false;
 const body=stalled==='body'?gate.promise:Promise.resolve('<div></div>');
 const context=vm.createContext({api:null,loading:null,performance:{now:()=>123},window:{},
  AbortController,setTimeout:fn=>{timers.set(++id,fn);return id;},clearTimeout:i=>timers.delete(i),
  slot:{textContent:'',innerHTML:'',querySelector:()=>({}),appendChild:n=>nodes.push(n)},
  document:{createElement:()=>({addEventListener(){},appendChild:n=>nodes.push(n)})},isOpen:()=>true,open(){},timing:name=>events.push(name),
  mount(){mounts++;return {show(){}};},
  fetch:(url,opts)=>{opts.signal.addEventListener('abort',()=>aborted=true);return stalled==='headers'?gate.promise:Promise.resolve({ok:true,text:()=>body});},
 });
 vm.runInContext(deadline+load,context);
 const first=context.load();assert.equal(context.load(),first);
 for(let i=0;i<10;i++)await Promise.resolve();
 [...timers.values()][0]();assert.equal(await first,null);assert(aborted);assert.equal(mounts,0);
 assert.equal(context.loading,null);assert(nodes.some(n=>n.textContent==='Try again'));
 gate.resolve(stalled==='headers'?{ok:true,text:()=>body}:'<div>late</div>');
 for(let i=0;i<10;i++)await Promise.resolve();
 assert.equal(mounts,0);assert.equal(context.slot.innerHTML,'');
 assert(events.includes('panel.open-error'));
}
console.log('Panel header/body deadlines, coalescing, retry availability and late-response isolation passed.');
