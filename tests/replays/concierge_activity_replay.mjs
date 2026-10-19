import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync('src/app/static/concierge_panel.js','utf8');
class Node {
  constructor(text='',isText=false){this._text=text;this.isText=isText;this.nodes=[];this.attrs={};}
  get children(){return this.nodes.filter(n=>!n.isText);}
  get childElementCount(){return this.children.length;}
  get textContent(){return this._text+this.nodes.map(n=>n.textContent).join('');}
  set textContent(value){this._text=value;this.nodes=[];}
  appendChild(node){this.nodes.push(node);return node;}
  setAttribute(k,v){this.attrs[k]=v;}
  removeAttribute(k){delete this.attrs[k];}
  hasAttribute(k){return k in this.attrs;}
}
const ctx=vm.createContext({document:{createElement:()=>new Node(),createTextNode:t=>new Node(t,true)},scrollDown(){}});
vm.runInContext(source.slice(source.indexOf('    function trailPill('),source.indexOf('    // One delegated handler covers')),ctx);
const strip={row:new Node()},knowledge={hint:'Checking knowledge…',label:'Knowledge checked',detail:'Business availability'};
const first=ctx.trailPill(strip,knowledge,true);
assert.equal(strip.row.textContent,'Checking knowledge');
assert.ok(first.hasAttribute('data-cz-tool-active') && first.hasAttribute('data-cz-pill'));
ctx.settlePill(first,knowledge);
const second=ctx.trailPill(strip,{hint:'Checking tasks...',label:'Tasks checked'},true);
assert.equal(strip.row.textContent,'Knowledge checked, Checking tasks');
assert.ok(!first.hasAttribute('data-cz-tool-active') && second.hasAttribute('data-cz-tool-active'));
ctx.settlePill(second,{label:'Tasks checked'});ctx.finishTrail(strip);ctx.finishTrail(strip);
assert.equal(strip.row.textContent,'Knowledge checked, Tasks checked.');
assert.ok(!second.hasAttribute('data-cz-tool-active') && !second.hasAttribute('data-cz-pill'));
const saved={row:new Node()};ctx.trailPill(saved,knowledge,false);ctx.finishTrail(saved);
assert.equal(saved.row.textContent,'Knowledge checked.');
// Polling follows connected task rows even when the mobile sidebar is outside #cz-root.
const requests=[],taskRows={};
const taskCtx=vm.createContext({taskRows,TASK_POLL:{queued:1,working:1,waiting:1},fetch:url=>{requests.push(url);return Promise.resolve({ok:true,json:async()=>({tasks:[]})});},applyTask:()=>false,loadWaiting(){},loadDone(){}});
vm.runInContext(source.slice(source.indexOf('    function pollTasks()'),source.indexOf('    setInterval(function () { if (document.visibilityState',source.indexOf('    function pollTasks()'))),taskCtx);
function task(status,batch,connected=true){return {isConnected:connected,getAttribute:()=>status,closest:()=>({getAttribute:()=>batch})};}
taskRows.one=task('working','batch-a');taskRows.two=task('queued','batch-a');taskRows.old=task('working','old',false);
assert.equal(taskCtx.anyTaskRunning(),true);taskCtx.pollTasks();
assert.deepEqual(requests,['/concierge/business/tasks?batch=batch-a']);
taskRows.one=task('done','batch-a');taskRows.two=task('waiting','batch-a');
assert.equal(taskCtx.anyTaskRunning(),false);
console.log('tool activity lifecycle and sidebar task polling passed');

// A reply arriving before the sidebar import is adopted when its host attaches.
const pending=[{id:'prepared-card'},{id:'restored-tasks'}],adopted=[];
const attachCtx=vm.createContext({sideHost:null,messages:{querySelectorAll:()=>pending},addCard:n=>adopted.push(n.id)});
const hostBody=source.match(/host: function \(h\) \{([\s\S]*?)\n        \},/)[1];
vm.runInContext('function attach(h) {'+hostBody+'}',attachCtx);
attachCtx.attach({active:()=>false});assert.deepEqual(adopted,[]);
attachCtx.attach({active:()=>true});assert.deepEqual(adopted,['prepared-card','restored-tasks']);
