// Exercise the actual controller with synthetic DOM fields and deferred requests.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const events = {}, requests = [], controls = [];
function element() {
  return {dataset:{},disabled:false,value:'',children:[],textContent:'',hidden:false,
    appendChild(el) { this.children.push(el); return el; }, replaceChildren(...els) { this.children=els; }};
}
const status=element(), box=element(), question=element(), later=element(), navigation=element();
later.querySelector=()=>navigation;
const form = element(); form.action='https://offline.invalid/onboarding/knowledge/review/review-id';
form.elements={version:{value:'v1'},csrf_token:{value:'csrf'},label_note:{value:'Exact title'},content_note:{value:'Owner original.'},action_note:{value:'keep'}};
form.querySelector=s=>s==='[data-source-preview]'?box:s==='[data-source-status]'?status:s==='[data-source-question]'?question:element();
form.querySelectorAll=()=>[{dataset:{draft:'note'}}];
form.matches=()=>true;
controls.push(...Object.values(form.elements)); controls.forEach(e=>Object.assign(e,{disabled:false,dataset:{}}));
const note=element(); note.elements={content:{value:''}};
const document = {addEventListener:(name,fn)=>events[name]=fn, createElement:element,
  querySelectorAll:s=>s==='[data-knowledge-review]'?[form]:controls,
  querySelector:s=>s==='[data-knowledge-later]'?later:element(), getElementById:()=>note};
const window={addEventListener(){},dispatchEvent(){}};
const fetch=(url,options)=>new Promise(resolve=>requests.push({url,options,resolve}));
vm.runInNewContext(fs.readFileSync('src/app/static/knowledge_source_review.js','utf8'),{document,window,fetch,console,Event:class{constructor(type){this.type=type;}}});
function event(target) { return {target,prevented:false,stopped:false,preventDefault(){this.prevented=true;},stopImmediatePropagation(){this.stopped=true;}}; }
function target(kind) { return {closest:s=>s.includes(kind)?targetButton:null}; }
const targetButton={closest:()=>form,hasAttribute:()=>false};
const preview={summary:'Review ready; nothing saved.',changes:[{label:'Exact title',before:'Owner original.',after:'Source caveat. Owner original.',passages:[{quote:'Source caveat.',url:'https://example.invalid'}]}],overlaps:[],questions:[],review:{revision:1}};
function finish(index,data,ok=true) { requests[index].resolve({ok,json:async()=>data}); }
const flush=()=>new Promise(resolve=>setImmediate(resolve));

assert(window.Blam8KnowledgeSourceReview.available());
const first=window.Blam8KnowledgeSourceReview.ask('Retain the caveat.');
assert.equal(requests.length,1);
await assert.rejects(window.Blam8KnowledgeSourceReview.ask('Double click'),/Wait/);
const saveWhileBusy=event(form); events.submit(saveWhileBusy); assert(saveWhileBusy.prevented);
assert(controls.every(c=>c.disabled));
const uploadForm = {matches:selector=>selector.includes('form[action="/onboarding/knowledge/upload"]')};
const uploadWhileBusy=event(uploadForm);events.submit(uploadWhileBusy);assert(uploadWhileBusy.prevented);
finish(0,preview); await first;
assert(controls.every(c=>!c.disabled));
assert.equal(form.elements.content_note.value,'Owner original.');
assert.equal(requests[0].options.headers['X-CSRF-Token'],'csrf');
form.elements.content_note.value='Owner changed after preview.';
events.click(event(target('[data-source-apply]'))); await flush();
assert.equal(requests.length,1); assert.match(status.textContent,/changed/);

form.elements.content_note.value='Owner original.';
const second=window.Blam8KnowledgeSourceReview.ask('Retain the caveat.'); finish(1,preview); await second;
events.click(event(target('[data-source-apply]'))); await flush();
assert.equal(requests.length,3); assert(requests[2].url.endsWith('/corrections/apply'));
const duplicateSave=event(form);events.submit(duplicateSave);assert(duplicateSave.prevented);
finish(2,{entries:[{id:'note',label:'Exact title',content:'Source caveat. Owner original.'}],summary:'Drafts changed; check overlaps.'}); await flush();
assert.equal(form.elements.content_note.value,'Source caveat. Owner original.');
assert.equal(form.elements.action_note.value,'keep');
assert.equal(form.elements.version.value,'v1'); // Final save requires a fresh preview.
assert.equal(requests.length,3);

note.elements.content.value='Unsaved pasted fact';
const next=event(target('[data-onb-continue]'));events.click(next);
assert(next.prevented && next.stopped);assert.match(navigation.textContent,/unsaved/);
assert.equal(note.elements.content.value,'Unsaved pasted fact');
note.elements.content.value='';
const pending=event(target('[data-onb-continue]'));events.click(pending);assert(pending.prevented);
window.blam8KnowledgeImportBusy=true;
await assert.rejects(window.Blam8KnowledgeSourceReview.ask('During website read'),/Wait/);
console.log('Source-review serialization, stale-edit and navigation replay passed');
