import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const helper=fs.readFileSync('src/app/static/onboarding_journey.js','utf8');
const widget=fs.readFileSync('src/app/static/concierge.js','utf8');
const a=widget.indexOf('function showContinueHint()'),b=widget.indexOf('// ── open / close',a);
assert(a>0 && b>a);
let action, step='interview', pending=0, note='', clicks=0, busy=false, saved=true, approved=true;
const cta={children:[],innerHTML:'',appendChild(el){this.children.push(el);},classList:{remove(){}}};
const doc={
  getElementById:()=>({textContent:JSON.stringify({current:step,next:{label:'Voice'}})}),
  querySelectorAll(selector){return selector==='[data-onb-continue]'?(action?[action]:[]):Array(pending).fill({});},
  querySelector(selector){return selector==='[data-knowledge-sample][data-approved="yes"]'?approved:selector.includes('content')?{value:note}:selector==='[data-knowledge-saved-status]'&&saved?{textContent:'Knowledge saved'}:null;},
  createElement(){return {className:'',textContent:'',children:[],listeners:{},setAttribute(name,value){this[name]=value;},appendChild(el){this.children.push(el);},addEventListener(name,fn){this.listeners[name]=fn;}};}
};
const window={Blam8KnowledgeSourceReview:{working:()=>busy}};
const context=vm.createContext({window,document:doc,Array,JSON,PENDING_KB_CARDS:[],cta,ADVANCE_WANTED:false,
  pageContinueBtn:()=>action,flash(){},setTimeout(){}});
vm.runInContext(helper+'\n'+widget.slice(a,b),context);
const state=window.Blam8OnboardingJourney;
function makeAction(valid=true){return {offsetParent:{},form:{elements:[{willValidate:true,validity:{valid}}]},
  disabled:false,textContent:'Continue',click(){clicks++;}};}
for(const current of ['interview','mailbox','voice','knowledge']){
  step=current;action=makeAction();assert.equal(state.read(0).ready,true);
  context.showContinueHint();assert.equal(clicks,0,'offering actions cannot navigate');
}
step='interview';action=makeAction(false);assert.equal(state.read(0).ready,false);
action=makeAction();assert.equal(state.read(0).ready,true,'review pause and model confidence are not readiness inputs');
cta.children=[];context.showContinueHint();const button=cta.children.find(el=>el.type==='button');assert(button);
action=makeAction();button.listeners.click();assert.equal(clicks,1,'click resolves the current action');
action=null;button.listeners.click();assert.equal(clicks,1,'no detached-action fallback');
action=makeAction();action.disabled=true;button.listeners.click();assert.equal(clicks,1);
action=makeAction();step='knowledge';pending=1;assert.equal(state.read(0).ready,false);
pending=0;note='Unsaved note';assert.equal(state.read(0).ready,false);
note='';busy=true;assert.equal(state.read(0).ready,false);
assert.equal(state.read(0).blocker,'busy');
cta.children=[];context.showContinueHint();
assert.equal(cta.children[0].className,'bc-notice bc-busy');
assert.equal(cta.children[0].role,'status');
assert.match(cta.children[0].children[0].textContent,/Wait for the current operation/);
busy=false;assert.equal(state.read(1).ready,false);
assert.equal(state.read(0).ready,true);
cta.children=[];context.showContinueHint();
assert(!cta.children.some(el=>el.className.includes('bc-busy')),'busy marker removed when operation ends');
assert(!cta.children.some(el=>el.children.some(child=>child.className==='bc-notice-text')),'a ready Continue stands alone, with no hint line');
note='Unsaved';cta.children=[];context.showContinueHint();
assert(!cta.children.some(el=>el.className.includes('bc-busy')),'a review decision is not active work');
assert.equal(cta.children[0].className,'bc-notice');
assert.match(cta.children[0].children[0].textContent,/unsaved/i);
note='';step='interview';action=makeAction(false);cta.children=[];context.showContinueHint();
assert.equal(cta.children[0].className,'bc-notice');
assert.match(cta.children[0].children[0].textContent,/Complete the required form fields/);
console.log('Journey readiness and explicit-click replay passed');

step="knowledge";action=makeAction();saved=false;assert.equal(state.read(0).ready,false);assert.equal(state.read(0).blocker,"knowledge_required");

action.disabled=true;assert.equal(state.read(0).blocker,"knowledge_required");
assert.match(state.read(0).reason,/add and save your first note/i);
pending=1;assert.equal(state.read(0).blocker,'pending_reviews');
context.showContinueHint();assert.equal(action.hidden,true);
action.offsetParent=null;saved=true;pending=0;action.disabled=false;context.showContinueHint();assert.equal(action.hidden,false);
approved=false;context.showContinueHint();assert.equal(action.hidden,true);assert.equal(state.read(0).blocker,'sample_required');
approved=true;note='Unsaved edit';context.showContinueHint();assert.equal(action.hidden,true);
note='';context.showContinueHint();assert.equal(action.hidden,false);

// Knowledge always opens against the current page, including a conversation resumed from Voice.
const opening=widget.slice(widget.indexOf('  function knowledgeOpening()'),widget.indexOf('  var ONB_GREETINGS'));
const greeting=widget.match(/  function greeting\(\) \{[^\n]+/)[0];
const markers=new Set();
const entry=vm.createContext({document:{querySelector:s=>markers.has(s)},ONB_SITE:'',
  ONB_STEP:'knowledge',FIX_LOG_ID:null,SCENARIO:false,GREETING:'Earlier Voice greeting'});
vm.runInContext(opening+greeting,entry);
assert.match(entry.greeting(),/what do you offer/i);
entry.ONB_SITE='example.test';assert.match(entry.greeting(),/website/i);
markers.add('[data-onb-held-notes]');assert.match(entry.greeting(),/mentioned.*earlier/i);
markers.add('[data-knowledge-saved-status]');assert.match(entry.greeting(),/notes are saved/i);
markers.add('[data-knowledge-review]');assert.match(entry.greeting(),/draft notes are ready/i);
