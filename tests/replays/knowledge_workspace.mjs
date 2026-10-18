// Execute the shipped refresh/preview/lifecycle controller without live services.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const events={}, requests=[], tokens=new Map();
function el(){return {dataset:{},hidden:false,disabled:false,textContent:'',value:'',listeners:{},
  addEventListener(n,f){this.listeners[n]=f;},appendChild(){},prepend(){},focus(){},querySelectorAll(){return [];}};}
let active;
function form(content='c1', evidence='e1', context='v1'){
  const f=el(),status=el();
  f.dataset={knowledgeReview:'id',contentVersion:content,evidenceVersion:evidence,contextVersion:context};
  f.action='/onboarding/knowledge/review/id';f.status=status;f.edits='Original';f.matches=()=>true;
  f.querySelector=s=>s==='[data-review-status]'?status:null;
  f.replaceWith=n=>{active=n;};return f;
}
active=form();
const holder={appendChild(n){active=n;}};
const heading=el(), description=el(), link=el(), lifecycle=el();
lifecycle.dataset.state='idle';lifecycle.querySelector=s=>s==='[data-site-heading]'?heading:s==='[data-site-description]'?description:link;
const go=el(), input=el(), status=el(),box=el();input.value='https://example.invalid';box.dataset={scope:'tenant'};
const document={getElementById:id=>({'site-go':go,'site-url':input,'site-lifecycle':lifecycle,'site-status':status,'site-box':box,'knowledge-pending-reviews':holder,'knowledge-review-csrf':{value:'csrf'}}[id]),
  querySelectorAll:()=>[active],querySelector:()=>active,
  addEventListener(n,f){(events[n]??=[]).push(f);},createElement:el,dispatchEvent(){}};
class Parser{parseFromString(s){return {querySelectorAll:()=>tokens.get(s)||[],getElementById:()=>null};}}
class FD{constructor(){this.values={};}set(k,v){this.values[k]=v;}append(k,v){this.values[k]=v;}}
const window={location:{reload(){throw Error('Unexpected reload');}},addEventListener(){},dispatchEvent(){}};
const fetch=(url,options)=>new Promise(resolve=>requests.push({url,options,resolve}));
vm.runInNewContext(fs.readFileSync('src/app/static/knowledge_onboarding_review.js','utf8'),{document,window,fetch,DOMParser:Parser,FormData:FD,Event:class{},CustomEvent:class{},sessionStorage:{getItem(){return null;},setItem(){}}});
function incoming(name,f){tokens.set(name,[f]);return {review_html:name};}
const original=active;
window.Blam8KnowledgeReview.replace(incoming('same',form()));
assert.equal(active,original);assert.match(active.status.textContent,/no new evidence/);
active.dataset.reviewDirty='1';active.edits='Unsaved owner text';
window.Blam8KnowledgeReview.replace(incoming('changed',form('c2')));
assert.equal(active,original);assert.equal(active.edits,'Unsaved owner text');assert.match(active.status.textContent,/local edits are preserved/);
const checked=form('c2','e1','v2');checked.edits='Unsaved owner text';
const submit={target:active,submitter:{value:'preview'},preventDefault(){this.prevented=true;}};
const pending=events.submit[0](submit);assert(submit.prevented);assert.equal(requests.length,1);assert(window.blam8KnowledgeImportBusy);
requests[0].resolve({ok:true,json:async()=>({...incoming('checked',checked),summary:'Check complete: no new findings. No facts saved.'})});await pending;
assert.equal(active,checked);assert.equal(active.edits,'Unsaved owner text');assert.match(active.status.textContent,/No facts saved/);assert(!window.blam8KnowledgeImportBusy);
const beforeRead=active;
const read=go.listeners.click();assert.equal(heading.textContent,'Reading your website');
const duplicate=go.listeners.click();await duplicate;assert.equal(requests.length,2);
requests[1].resolve({ok:true,json:async()=>({...incoming('read',form('c2','e1','v2')),pages:['url'],site_state:{state:'pending',pending_notes:10,review_href:'#review-id'}})});
await read;assert.equal(active,beforeRead);assert.equal(lifecycle.dataset.state,'pending');assert.equal(go.textContent,'Read again');assert.equal(link.textContent,'Review 10 drafts');assert.match(description.textContent,/Nothing.*saved/);
const failed=go.listeners.click();requests[2].resolve({ok:false,json:async()=>({error:'Synthetic fetch failure'})});await failed;
assert.equal(lifecycle.dataset.state,'error');assert.equal(go.textContent,'Try again');assert.equal(active,beforeRead);
assert.equal(link.textContent,'Review 10 drafts');assert.equal(link.hidden,false);
input.listeners.input();assert.equal(lifecycle.dataset.state,'idle');
const uncertain=go.listeners.click();requests[3].resolve({ok:true,json:async()=>{throw Error('Synthetic lost response');}});await uncertain;
assert.equal(lifecycle.dataset.state,'unconfirmed');assert.equal(go.textContent,'Check stored drafts');assert.equal(requests.length,4);
const reconcile=go.listeners.click();assert.equal(requests[4].url,'/onboarding/knowledge/reviews');
requests[4].resolve({ok:true,json:async()=>({...incoming('reconciled',form('c2','e1','v2')),site_state:{state:'pending',pending_notes:10}})});await reconcile;
assert.equal(lifecycle.dataset.state,'pending');assert.equal(active,beforeRead);assert.equal(requests.length,5);
console.log('Workspace no-op/dirty refresh, nonmutating preview and website lifecycle replay passed');
