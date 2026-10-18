// Run the actual proposal transport and card functions with offline deferred IO.
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const source = fs.readFileSync('src/app/static/concierge_panel.js', 'utf8');
const helpers = source.slice(source.indexOf('    var UNKNOWN_PROPOSAL'), source.indexOf('    function proposalCard(p)'));
const voice = source.slice(source.indexOf('    async function voiceExecuteProposal'), source.indexOf('    // A bare "yes" sends'));
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.handlers = {}; this.style = {}; this.isConnected = true; this.classList = {remove(){}, add(){}}; }
  appendChild(child) { this.children.push(child); child.parentNode = this; return child; }
  setAttribute(k,v) { this.attrs[k]=v; }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener(k,f) { this.handlers[k]=f; }
  remove() { if(this.parentNode) this.parentNode.children = this.parentNode.children.filter(x=>x!==this); }
  async click() { if(!this.disabled) await this.handlers.click?.(); }
}
let calls=[], timers=[], response, allowBulkCancel=true, confirmations=[];
const ctx = vm.createContext({
  confirm:message=>{confirmations.push(message);return allowBulkCancel;},
  AbortController, Promise, document: {createElement:tag=>new Element(tag)},
  csrfToken:()=> 'offline', KIND_LABELS:{}, THUMB_UP:'confirm', SKIP_BIN:'cancel', DECLINED_LINE:'Declined.',
  voiceStage:{}, loadWaiting(){}, loadDone(){}, scrollDown(){}, fadeAway(){}, settledByTap(){}, addLinks(){},
  setTimeout(fn,ms) { const timer={fn,ms,active:true}; timers.push(timer); return timer; },
  clearTimeout(t) { if(t) t.active=false; },
  fetch: async (url,options) => { calls.push({url, options}); return response(url,options); }
});
const dates=source.slice(source.indexOf('    function waitingDate'),source.indexOf('    function waitingCounts'));
vm.runInContext(dates + helpers + voice, ctx);
const result=(state='completed')=>({proposal_id:'p1',state,ok:state==='completed',summary:state==='completed'?'Saved.':'Check the record.'});
const json=data=>({ok:true,json:async()=>data});
const card=()=>ctx.proposalCardNode({proposal_id:'p1',kind:'draft_reply',summary:'Send the draft',draft_text:'edited'});
const buttons=c=>c.children[0].children[1].children;
// A response lost AFTER effect: both click and spoken paths read the same saved receipt.
response=url=>{ if(url.endsWith('/execute')) throw Error('lost response'); return json(result()); };
let c=card(); await buttons(c)[0].click();
assert.equal(c.attrs['data-state'],'sent'); assert.equal(calls.filter(x=>x.url.endsWith('/execute')).length,1);
calls=[]; let spoken=await ctx.voiceExecuteProposal({proposal_id:'p1'},card());
assert.equal(spoken.state,'completed'); assert.equal(calls.filter(x=>x.url.endsWith('/execute')).length,1);
// Malformed JSON, HTTP failure, and invalid receipt reconcile; unavailable status stays locked.
for(const fault of ['json','http','shape']) {
  calls=[]; response=url=>url.endsWith('/execute') ?
    fault==='json'?{ok:true,json:async()=>{throw Error('bad JSON');}}:fault==='http'?{ok:false}:json({ok:true}) : json(result('unknown'));
  c=card(); await buttons(c)[0].click();
  assert.equal(c.attrs['data-state'],'unknown'); assert.equal(buttons(c)[0].hidden,true);
  assert.equal(buttons(c)[1].hidden,true); assert.equal(c._cz.settled,true);
  assert.equal(buttons(c)[2].textContent,'Check result');
  assert.equal(calls.length,2);
}
// Pending is the only authority to reopen the SAME card; terminal success resists old polls.
c._cz.applyOutcome(result('pending')); assert.equal(c._cz.settled,false);
assert.equal(buttons(c)[0].hidden,false); assert.equal(buttons(c)[0].disabled,false);
assert.equal(c.children.find(x=>x.tag==='textarea').disabled,false);
c._cz.applyOutcome(result()); c._cz.applyOutcome(result('unknown'));
assert.equal(c.attrs['data-state'],'sent');
// A never-resolving POST or JSON body has a bounded deadline and only GET recovery.
for(const body of [false,true]) {
  calls=[]; timers=[];
  response=url=>url.endsWith('/execute') ? body?{ok:true,json:()=>new Promise(()=>{})}:new Promise(()=>{}) : json(result('unknown'));
  const pending=ctx.voiceExecuteProposal({proposal_id:'p1'},card());
  await Promise.resolve(); await Promise.resolve();
  const deadline=timers.find(t=>t.active&&t.ms===10000); assert(deadline); deadline.fn();
  assert.equal((await pending).state,'unknown');
  assert.equal(calls.filter(x=>x.url.endsWith('/execute')).length,1);
  assert.equal(calls.filter(x=>x.url.includes('/status/')).length,1);
  assert(calls[0].options.signal.aborted);
}
// Recovery survives reload and contains no new confirmation or resurface route.
c=ctx.proposalCardNode({...result('unknown'),type:'recovery'});
assert.equal(buttons(c)[0].hidden,true); assert.equal(buttons(c)[2].textContent,'Check result');
response=()=>json(result()); calls=[]; await buttons(c)[2].click();
assert.equal(c.attrs['data-state'],'sent'); assert(calls[0].url.endsWith('/reconcile'));
timers=[]; c=card(); c._cz.applyOutcome(result('executing'));
response=()=>json(result('executing'));
for(let i=0;i<5;i++) { const t=timers.find(x=>x.active&&x.ms===2000); if(!t) break; t.active=false; await t.fn(); }
assert.equal(timers.filter(x=>x.ms===2000).length,3);
assert(source.includes("w.type !== 'recovery' && !isNaN(t)"));
assert(source.includes("if (w.type === 'recovery') { if (!focusOpenCard(w.proposal_id)) { addCard(proposalCardNode(w)"));
console.log('proposal recovery replay: passed');


// Actual waiting-list click distinguishes current approvals from historical uncertainty.
const waiting=source.slice(source.indexOf('    function waitingCounts'),source.indexOf('    // The typed thread'));
const resurface=source.slice(source.indexOf('    function resurface(w, btn)'),source.indexOf('    function dismissWaiting'));
const opened=[], notices=[];let resurfaceFail=false;
Object.assign(ctx,{waitingItems:[],dotOpen:false,waitingOpen:false,voiceLiveInstance:null,messages:{appendChild:node=>opened.push(node)},
  noteShown(){},focusOpenCard:()=>false,discovery:null,addCard:(node)=>opened.push(node),sideHost:null,refused:{},sideChanged(){},proposalCard:p=>opened.push(ctx.proposalCardNode(p)),bubble:(role,text)=>notices.push(text),
  postJson:async(url,body)=>{calls.push({url,body});if(resurfaceFail)throw Error('offline');return {ok:true,proposal:{proposal_id:body.proposal_id,summary:'Review current draft',kind:'invoice_reminder_draft'}};}});
vm.runInContext(waiting+resurface,ctx);
const legacy={...result('unknown'),proposal_id:'legacy',type:'recovery',created_at:'2026-07-26T09:00:00Z',action_summary:'Earlier invoice reminder',link:{href:'/app/invoices/fixture',label:'Open invoice'}};
ctx.waitingItems=[legacy];calls=[];let list=ctx.waitingRow(true);
assert.match(list.children[0].textContent,/Unverified earlier results/);
let go=list.children[1].children[0];assert.match(go.textContent,/Result unverified: Earlier invoice reminder/);assert.match(go.textContent,/2026/);
go.handlers.click({stopPropagation(){}});assert.equal(calls.length,0);assert.equal(opened.at(-1).attrs['data-state'],'unknown');
const pending={proposal_id:'current',summary:'Current invoice reminder',created_at:'2026-10-12T12:00:00Z'};
ctx.waitingItems=[pending];list=ctx.waitingRow(true);go=list.children[1].children[0];assert.match(go.textContent,/Awaiting approval/);
go.handlers.click({stopPropagation(){}});for(let i=0;i<5;i++)await Promise.resolve();
assert.equal(calls.at(-1).url,'/concierge/business/resurface');assert.equal(calls.at(-1).body.proposal_id,'current');
assert.equal(opened.at(-1)._cz.settled,false);assert(!calls.some(call=>call.url.endsWith('/execute')));
resurfaceFail=true;go.handlers.click({stopPropagation(){}});for(let i=0;i<5;i++)await Promise.resolve();
assert.match(notices.at(-1),/Could not load the approval card.*No action was submitted/);
assert(!notices.at(-1).includes('Work may have happened'));assert(!calls.some(call=>call.url.endsWith('/execute')));
console.log('Actual historical recovery/current approval clicks and pre-submit inspection failure passed.');


// Two separately identified proposals: Bin must cancel only the clicked card.
{
  calls=[];
  const states=new Map([['first-independent','pending'],['second-independent','pending']]);
  response=(url,options)=>{
    assert(url.endsWith('/cancel'),'Bin must never execute or send');
    const body=JSON.parse(options.body);states.set(body.proposal_id,'cancelled');
    return json({proposal_id:body.proposal_id,state:'cancelled',ok:false,summary:'Cancelled.'});
  };
  const first=ctx.proposalCardNode({proposal_id:'first-independent',kind:'draft_reply',summary:'First draft',draft_text:'First text'});
  const second=ctx.proposalCardNode({proposal_id:'second-independent',kind:'draft_reply',summary:'Second draft',draft_text:'Second text'});
  await buttons(first)[1].click();
  assert.equal(calls.length,1);assert.equal(JSON.parse(calls[0].options.body).proposal_id,'first-independent');
  assert.equal(states.get('first-independent'),'cancelled');assert.equal(states.get('second-independent'),'pending');
  assert.equal(second._cz.settled,false);assert.equal(buttons(second)[0].disabled,undefined);
  assert.equal(second.children.find(node=>node.tag==='textarea').value,'Second text');
}
// Existing bulk contract: one group Bin ignores Confirm-only checkbox selection.
{
  calls=[];
  const bulk=ctx.proposalCardNode({proposal_id:'one-bulk-group',kind:'bulk',summary:'Two prepared drafts',items:[
    {ok:true,kind:'draft_reply',summary:'First draft',draft_text:'First text'},
    {ok:true,kind:'draft_reply',summary:'Second draft',draft_text:'Second text'}]});
  const checkboxes=bulk.children.find(node=>node.className==='space-y-1.5').children.map(label=>label.children[0]);
  checkboxes[0].checked=false;checkboxes[1].checked=true;
  assert.match(buttons(bulk)[1].attrs['aria-label'],/entire group.*2 prepared items/);
  allowBulkCancel=false;await buttons(bulk)[1].click();
  assert.equal(calls.length,0);assert.equal(bulk._cz.settled,false);
  assert.equal(checkboxes[0].checked,false);assert.equal(checkboxes[1].checked,true);
  assert.equal(buttons(bulk)[1].disabled,undefined);
  assert.match(confirmations.at(-1),/Cancel all 2 prepared items.*Tick boxes only/);
  allowBulkCancel=true;
  await buttons(bulk)[1].click();
  assert.equal(calls.length,1);const body=JSON.parse(calls[0].options.body);
  assert.equal(body.proposal_id,'one-bulk-group');assert.equal(body.selected,undefined);
  assert.equal(bulk._cz.settled,true);
}
console.log('Independent proposal Bin isolation passed; explicit whole-group bulk cancellation scope guard passed.');


// Repeated taps while cancellation is pending cannot dispatch twice.
{
  calls=[];let finishCancel;
  response=(url,options)=>new Promise(resolve=>{finishCancel=()=>resolve(json({proposal_id:'slow-cancel',state:'cancelled',ok:false,summary:'Cancelled.'}));});
  const slow=ctx.proposalCardNode({proposal_id:'slow-cancel',kind:'draft_reply',summary:'Independent slow draft',draft_text:'Keep'});
  const firstClick=buttons(slow)[1].click();
  await buttons(slow)[1].click();
  assert.equal(calls.length,1);finishCancel();await firstClick;
  assert.equal(calls.length,1);
}
console.log('Pending cancellation duplicate tap suppression passed.');
