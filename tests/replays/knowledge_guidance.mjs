import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';

const events={}, windowEvents={}, requests=[];
const fields={version:{name:'version',value:'v1'},csrf_token:{name:'csrf_token',value:'secret'},
  action_one:{name:'action_one',value:''},content_one:{name:'content_one',value:'Owner text.'},
  source_unit:{name:'source_unit',value:''}};
for(const field of Object.values(fields))field.dispatchEvent=()=>{};
let submitted=0;
let focused=0,scrolled=0,closed=0;
const disclosure={open:false};
const section={dataset:{draft:'one'},scrollIntoView(){scrolled++;},querySelector(){return disclosure;}};
fields.content_one.focus=()=>{focused++;};
const form={dataset:{knowledgeReview:'batch'},elements:fields,action:'/review/batch',
  appendChild(el){fields[el.name]=el;},querySelector(selector){return selector.includes('operation') ? {disabled:false} : selector.startsWith('[data-draft=') ? section : null;},
  querySelectorAll(){return [section];},
  requestSubmit(){submitted++;}};
let activeForm=form, availableForms=null;
const document={querySelectorAll(){return availableForms || [activeForm];},
  addEventListener(name,cb){events[name]=cb;},createElement(){return {dispatchEvent(){}};}};
let sourceReply={reply:'Correction preview; not saved.'};
let correctionScope=null;
const window={addEventListener(name,cb){windowEvents[name]=cb;},dispatchEvent(){},
  blam8ConciergeClose(){closed++;},
  Blam8KnowledgeSourceReview:{working(){return false;},ask:async(question,form,scope)=>{correctionScope=scope;return sourceReply;}}};
class FormData {constructor(f){this.form=f;}forEach(cb){for(const field of Object.values(this.form.elements))cb(field.value,field.name);}}
const fetch=(url,options)=>new Promise(resolve=>requests.push({url,options,resolve}));
const context={document,window,FormData,fetch,Event:class{},console};
vm.runInNewContext(fs.readFileSync('src/app/static/knowledge_guidance.js','utf8'),context);
events.DOMContentLoaded();
const guide=window.Blam8KnowledgeGuide;
const finish=(index,data)=>requests[index].resolve({ok:true,json:async()=>data});
const result=(patches={},hash='h1')=>({reply:'Review this actual note.',patches,focus_hash:hash,focus:{kind:'note',id:'one'},actions:[],shared_caveats:[]});

assert.equal(disclosure.open,false); // Pending cards start collapsed.
const first=guide.ask('');
assert(window.blam8KnowledgeImportBusy && window.Blam8KnowledgeSourceReview.working());
await assert.rejects(guide.ask('second'),/wait/);
assert.equal(requests.length,1);
assert.equal(JSON.parse(requests[0].options.body).form.csrf_token,undefined);
assert.equal(requests[0].options.headers['X-CSRF-Token'],'secret');
finish(0,result());await first;
assert(disclosure.open); // The note under active review opens automatically.
assert(guide.isCurrent('h1'));
assert(!window.blam8KnowledgeImportBusy);

const keep=guide.ask('Keep this note');finish(1,result({action_one:'keep'},'h2'));await keep;
assert.equal(fields.action_one.value,'keep');
assert(!guide.isCurrent('h1') && guide.isCurrent('h2'));
assert.equal(guide.edit('stale','one'),false);
assert.equal(guide.edit('h2','one'),true);
assert(disclosure.open);assert.equal(focused,1);assert.equal(scrolled,1);assert.equal(closed,1);

const racing=guide.ask('Keep the wording');fields.content_one.value='Manual edit during request.';
finish(2,result({source_unit:'note:one'},'h3'));
await assert.rejects(racing,/changed/);
assert.equal(fields.source_unit.value,'');
assert(!guide.isCurrent('h2'));

const refresh=guide.ask('Show the current note');finish(3,result({},'h4'));await refresh;
const unknown=guide.ask('Leave this unknown');finish(4,result({guided_unknown_question:'1'},'h5'));await unknown;
assert.equal(fields.guided_unknown_question.value,'1');
assert(guide.isCurrent('h5'));
assert.equal(submitted,0); // Chat choices never invoke Save.
guide.save();assert.equal(submitted,0); // Not offered until the planner is ready.
const final=guide.ask('Show the current note');finish(5,{...result({},'ready'),save_ready:true});await final;
guide.save();assert.equal(submitted,1); // Only separate explicit Save action.

const outage=guide.ask('Leave that heading out because it is only navigation.');
requests[6].resolve({ok:false,json:async()=>({detail:'The Knowledge review AI service has no available API credit. Your review choices remain on this page.'})});
await new Promise(resolve=>setImmediate(resolve));
assert.equal(requests.length,8); // One read-only recovery; no retry of the failed choice.
assert.equal(JSON.parse(requests[7].options.body).question,'Show the current note');
assert.equal(JSON.parse(requests[7].options.body).focus_hash,'');
finish(7,result({},'recovered'));
const recovered=await outage;
assert(recovered.recovered_after_error);
assert.match(recovered.reply,/API credit/);
assert.match(recovered.reply,/Review this actual note/);
assert.equal(fields.action_one.value,'keep');
assert(guide.isCurrent('recovered'));

const malformed=guide.ask('Leave it out because it is only navigation.');
requests[8].resolve({ok:false,json:async()=>{throw new SyntaxError('Unexpected token I in JSON');}});
await new Promise(resolve=>setImmediate(resolve));
assert.equal(requests.length,10);
finish(9,result({},'after-malformed'));
const malformedRecovery=await malformed;
assert(malformedRecovery.recovered_after_error);
assert.doesNotMatch(malformedRecovery.reply,/Unexpected token/);
assert.match(malformedRecovery.reply,/unreadable response/);

const correction=guide.ask('Please correct the source');finish(10,{correction_requested:true});
assert.match((await correction).reply,/preview/);
assert(!guide.isCurrent('h5'));
assert(!window.blam8KnowledgeImportBusy);

const replacement=guide.ask('Show the current note');activeForm={...form};
finish(11,result({action_one:'discard'}));
await assert.rejects(replacement,/changed/);
assert.equal(fields.action_one.value,'keep');
console.log('Knowledge guidance: form choices, stale actions, concurrent edits, correction routing and explicit save verified.');

// Conversational editor requests open the exact submitted note, without approval.
activeForm=form;
let index=requests.length;
let loading=guide.ask('Show the current note');finish(index,result({},'edit-hash'));await loading;
assert(guide.isCurrent('edit-hash','batch','v1'));
assert(!guide.isCurrent('edit-hash','other-batch','v1'));
assert(!guide.isCurrent('edit-hash','batch','old-version'));
index=requests.length;
const beforeFocus=focused;
loading=guide.ask('edit it');
finish(index,{...result({},'edit-hash'),edit_requested:true,
  review_decision:{batch_id:'batch',version:'v1',focus_hash:'edit-hash',kind:'note',id:'one',action:'edit',state:'editing'}});
const editing=await loading;
assert.equal(focused,beforeFocus+1);assert.equal(editing.review_decision.state,'editing');
assert.equal(fields.action_one.value,'keep'); // Existing choice remains unchanged.
// A correction to another named note must never select this note's Edit action.
const correctionFocus={batch_id:'batch',version:'v1',focus_hash:'edit-hash',kind:'note',id:'one',action:'edit',state:'preview'};
sourceReply={reply:'Other note correction preview.',correction_preview_ids:['other']};
index=requests.length;loading=guide.ask('Correct the other note');
finish(index,{correction_requested:true,correction_focus:correctionFocus,correction_note:{note_id:'one',focus_hash:'edit-hash'}});
assert.equal((await loading).review_decision,undefined);
assert.equal(correctionScope.note_id,'one');assert.equal(correctionScope.focus_hash,'edit-hash');
assert(guide.isCurrent('edit-hash','batch','v1')); // Read-only/no-op correction does not invalidate a valid focus.
index=requests.length;loading=guide.ask('Show the current note');finish(index,result({},'edit-hash'));await loading;
sourceReply={reply:'This note correction preview.',correction_preview_ids:['one']};
index=requests.length;loading=guide.ask('Add the sourced wording to this note');
finish(index,{correction_requested:true,correction_focus:correctionFocus});
assert.equal((await loading).review_decision,correctionFocus);
assert.equal(submitted,1); // Preview/editor did not submit another Save.

// An explicit form selects the submitted batch even when several batches exist.
const otherForm={...form,dataset:{knowledgeReview:'other-batch'},action:'/review/other-batch'};
availableForms=[form,otherForm];
assert(guide.available(otherForm));
index=requests.length;loading=guide.ask('Show the current note',otherForm);
assert.equal(requests[index].url,'/review/other-batch/guide');
await assert.rejects(guide.ask('Show the current note',form),/wait/);
finish(index,result({},'other-hash'));await loading;
assert(guide.isCurrent('other-hash','other-batch','v1'));
assert(!guide.isCurrent('other-hash','batch','v1'));
