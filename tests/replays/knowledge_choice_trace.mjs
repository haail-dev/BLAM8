import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const source=fs.readFileSync('src/app/static/concierge.js','utf8');
const between=(a,b)=>source.slice(source.indexOf(a),source.indexOf(b,source.indexOf(a)));
class Element {
  constructor(tag='div'){this.tagName=tag.toUpperCase();this.children=[];this.attributes={};this.dataset={};this.listeners={};this.disabled=false;this.textContent='';}
  setAttribute(k,v){this.attributes[k]=String(v);}
  getAttribute(k){return this.attributes[k];}
  appendChild(child){this.children.push(child);return child;}
  set innerHTML(_){this.children=[];}
  addEventListener(name,fn){this.listeners[name]=fn;}
  click(){if(!this.disabled)this.listeners.click?.();}
  querySelectorAll(selector){
    const match=selector.match(/^\[([^\]]+)\]( button)?$/);
    const result=[];
    const visit=(node,inside=false)=>{for(const c of node.children){const matched=Object.hasOwn(c.attributes,match?.[1]);if(match?.[2]?inside&&c.tagName==='BUTTON':matched)result.push(c);visit(c,inside||matched);}};
    visit(this);return result;
  }
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
}
let current=null, queued=[], edits=[], saved='', calls=[];
const msgs=new Element(),input={value:'',focus(){}},sendBtn={};
const guide={available:()=>true,start(){},isCurrent:(hash,batch,version)=>!!current&&hash===current.focus_hash&&(batch===undefined||batch===current.batch_id)&&(version===undefined||version===current.version),
  edit:(hash,id)=>{if(!guide.isCurrent(hash))return false;edits.push(id);return true;},save:()=>assert.fail('No save from choices'),
  async ask(text){calls.push(text);const next=queued.shift();if(next instanceof Error)throw next;if(!next)throw new Error('No fixture');current=next;return next;}};
const noop=()=>{};
const c=vm.createContext({document:{createElement:tag=>new Element(tag),querySelector:()=>null},window:{Blam8KnowledgeGuide:guide},
  msgs,input,sendBtn,log:[],deviceSync:null,ONB_STEP:'knowledge',AUTHED:true,ONB_MODE:true,FIX_LOG_ID:false,HELP_MODE:false,SCENARIO:false,MAX_TURNS:16,sending:false,
  readCurrent:()=>({}),applyFill:noop,appendHeldLine:noop,hideTyping:noop,showTyping:noop,pinToTop:noop,showContinueHint:noop,
  autosize:noop,refreshTitle:noop,touch:noop,clearPin:noop,unpinCaveat:noop,scrollDown:noop,applyQuestionAllowance:noop,
  saveLog:log=>{saved=JSON.stringify(log);},bubbleEl:(role,text)=>{const e=new Element();e.textContent=text;return e;},
  caveatEl:()=>new Element(),greeting:()=>'',appendTrailStrip:noop,setOpen:noop,console});
vm.runInContext(between('var pendingKnowledgeChoice = null;','function appendHeldLine(')+
  between('async function onboardTurn(','// Node builder + appender')+
  between('async function send(options)','// ── wiring')+
  between('function renderAll()','var typingEl = null;')+
  between('function openWith(text)','// Programmatic asks are HELP'),c);
const actions={note:[['keep','Keep this note'],['discard','Leave this note out']],overlap:[['keep_both','Keep both notes'],['discard','Leave this draft out']],passage:[['retain','Keep wording']],support:[['retain_support','Keep original wording'],['details','Show differences']],question:[['unknown','Leave unknown']]};
const data=(kind,id,extra={})=>({reply:'Synthetic '+kind+' '+id,batch_id:'batch',version:'v1',focus_hash:kind+id,focus:{kind,id},guide_actions:(actions[kind]||[]).map(([action,label])=>({action,label,message:label})),patches:{},...extra});
const receipt=(shown,action,state='prepared')=>({batch_id:shown.batch_id,version:shown.version,focus_hash:shown.focus_hash,kind:shown.focus.kind,id:shown.focus.id,action,state});
const cards=()=>msgs.querySelectorAll('[data-knowledge-guide-actions]');
const buttons=card=>card.querySelectorAll('[data-knowledge-guide-choice]');
const chosen=(card,action)=>{for(const b of buttons(card)){assert.equal(b.getAttribute('aria-pressed'),String(b.getAttribute('data-knowledge-guide-choice')===action));assert(b.disabled);assert(b.className.includes('bg-transparent'));assert(b.className.includes(b.getAttribute('data-knowledge-guide-choice')===action?'text-emerald-700':'text-slate-400'));}};
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function show(d){queued.push(d);await c.onboardTurn('Show the current note',[]);return cards().at(-1);}
const first=data('note','one');const card=await show(first);
assert(buttons(card).every(b=>!b.disabled));
// Layout contract: the row is centred, Edit sits between the two note decisions, nothing is pushed to an edge.
const choiceOrder=group=>buttons(group).map(b=>b.getAttribute('data-knowledge-guide-choice'));
const centred=group=>{assert(group.className.includes('justify-center'));assert(buttons(group).every(b=>!b.className.includes('ml-auto')));assert(group.querySelector('[data-knowledge-guide-status]').className.includes('text-center'));};
assert.deepEqual(choiceOrder(card),['keep','edit','discard']);centred(card);
queued.push(data('overlap','match',{review_decision:receipt(first,'keep'),patches:{action_one:'keep'}}));
buttons(card)[0].click();await tick();chosen(card,'keep');
assert.equal(calls.at(-1),'Keep this note');assert(!c.log.some(m=>m.role==='user')); // silent button
const overlap=cards().at(-1), overlapData=current;
assert.deepEqual(choiceOrder(overlap),['keep_both','discard']);centred(overlap); // Edit belongs to notes only
queued.push(data('note','two',{review_decision:receipt(overlapData,'keep_both'),patches:{match_match:'keep_both'}}));
buttons(overlap)[0].click();await tick();chosen(overlap,'keep_both');chosen(card,'keep');
// A model proposal cannot paint approval; only the following exact typed action confirms it.
let second=cards().at(-1), secondData=current;
queued.push(data('note','two',{patches:{},save_ready:false}));
c.input.value='keep that one';await c.send();
assert(buttons(second).every(b=>b.getAttribute('aria-pressed')==='false'));
assert(c.log.filter(m=>m.knowledge_card?.focus.id==='two').every(m=>!m.knowledge_card.selected));
second=cards().at(-1);secondData=current;
queued.push(data('passage','source',{review_decision:receipt(secondData,'keep'),patches:{action_two:'keep'}}));
c.input.value='Keep this note';await c.send();chosen(second,'keep');
const passage=cards().at(-1),passageData=current;
queued.push(data('support','third',{review_decision:receipt(passageData,'retain'),patches:{source_source:'note:two'}}));
c.openWith('keep that wording');await tick();chosen(passage,'retain');
const support=cards().at(-1),supportData=current;
queued.push(data('question','q',{review_decision:receipt(supportData,'retain_support'),patches:{source_s:'note:third'}}));
c.openWith('Keep all that original wording');await tick();chosen(support,'retain_support');
const question=cards().at(-1),questionData=current;
queued.push(data('note','four',{review_decision:receipt(questionData,'unknown'),patches:{question_q:'unknown'}}));
c.input.value='leave it unknown';await c.send();chosen(question,'unknown');
// Error, successful read-only recovery and ambiguous response never select a choice.
const pending=cards().at(-1),pendingData=current;
queued.push(new Error('Offline failure'));c.input.value='keep it';await c.send();assert(buttons(pending).every(b=>b.getAttribute('aria-pressed')==='false'));
queued.push(data('note','four',{recovered_after_error:true}));c.input.value='keep it';await c.send();assert(buttons(pending).every(b=>b.getAttribute('aria-pressed')==='false'));
queued.push(data('note','four'));c.input.value='Maybe, what do you think?';await c.send();assert(buttons(cards().at(-1)).every(b=>b.getAttribute('aria-pressed')==='false'));
// Batch/version identity prevents a foreign same-hash receipt from painting this card.
const latest=cards().at(-1);
queued.push(data('note','four',{review_decision:{...receipt(pendingData,'discard'),batch_id:'other'}}));c.input.value='leave it';await c.send();assert(buttons(latest).every(b=>b.getAttribute('aria-pressed')==='false'));
queued.push(data('note','four',{review_decision:{...receipt(pendingData,'discard'),version:'v2'}}));c.input.value='leave it';await c.send();assert(buttons(latest).every(b=>b.getAttribute('aria-pressed')==='false'));
// Opening the editor acknowledges editing, without applying/saving or selecting keep.
const editing=cards().at(-1);buttons(editing).find(b=>b.getAttribute('data-knowledge-guide-choice')==='edit').click();chosen(editing,'edit');assert.equal(edits.at(-1),'four');assert.match(editing.querySelector('[data-knowledge-guide-status]').textContent,/Editing.*not saved/);
assert(!buttons(editing).find(b=>b.getAttribute('data-knowledge-guide-choice')==='keep').className.includes('emerald'));
assert.equal(c.log.find(m=>m.knowledge_card?.state==='editing').knowledge_card.selected,'edit');
// Restore the actual bounded transcript metadata with no executable stale actions.
c.log=JSON.parse(saved);current=null;c.renderAll();
chosen(cards()[0],'keep');chosen(cards()[1],'keep_both');assert(buttons(cards().at(-1)).every(b=>b.disabled));
const before=calls.length;buttons(cards()[0])[0].click();assert.equal(calls.length,before);
assert(!saved.includes('Owner text'));assert.equal(c.sending,false);
console.log('Knowledge card trace: click, typed/transcript, all choices, failures, identity, edit and restored history verified.');

// A server-confirmed saved review collapses old unsaved instructions, without changing its transcript.
const originalLog=JSON.stringify(c.log), savedMarker=new Element(), savedStatus=new Element();
savedMarker.setAttribute('data-knowledge-saved-reviews',JSON.stringify(['batch']));
savedStatus.textContent='Knowledge saved. 9 notes saved. No drafts waiting for review.';
c.document.querySelector=selector=>selector==='[data-knowledge-saved-reviews]'?savedMarker:selector==='[data-knowledge-saved-status]'?savedStatus:null;
c.renderAll();
assert.equal(JSON.stringify(c.log),originalLog);
assert.equal(cards().length,0);
assert(msgs.children.some(el=>el.tagName==='DETAILS'&&el.children[0].textContent.includes('completed')));
assert.equal(msgs.children.at(-1).textContent,savedStatus.textContent);
c.log.push({role:'assistant',content:'A later answer about your business.'});c.renderAll();
assert.equal(msgs.children.at(-1).textContent,'A later answer about your business.');c.log.pop();
// Another pending batch must retain its review controls; a saved ID is not blanket approval.
savedMarker.setAttribute('data-knowledge-saved-reviews',JSON.stringify(['another-batch']));
c.document.querySelector=selector=>selector==='[data-knowledge-saved-reviews]'?savedMarker:null;
c.renderAll();assert(cards().length>0);
