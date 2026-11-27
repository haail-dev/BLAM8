import assert from 'node:assert/strict';import fs from 'node:fs';import vm from 'node:vm';
const events={},sections=[];let calls=[],guidance=null;const choices=[];
const node=(dataset={})=>({dataset,attributes:{},value:'',hidden:false,disabled:false,textContent:'',style:{},scrollHeight:100,closest(){return null;},setAttribute(k,v){this.attributes[k]=v;},getAttribute(k){return this.attributes[k];},hasAttribute(k){return Object.hasOwn(this.attributes,k);}});
function form(key,ids){const f=node({knowledgeReview:key});f.elements={};f.notice=node();f.querySelector=s=>s==='[data-review-status]'?f.notice:null;f.querySelectorAll=s=>s==='[data-draft]'?f.sections:[];f.sections=ids.map(id=>{const section=node({draft:id}),detail=node(),status=node(),preview=node();detail.open=id===ids[0];section.detail=detail;section.matches=[];section.form=f;
section.tools=['edit','source','more'].map(tool=>node({noteTool:tool}));section.panels=['edit','source','more'].map(tool=>node({notePanel:tool}));section.actions=['reviewed','later'].map(kind=>{const b=node();b.attributes['data-note-'+kind]='';b.closest=s=>s==='[data-draft]'?section:f;return b;});section.querySelector=s=>s==='[data-note-details]'?detail:s==='[data-note-status]'?status:s==='[data-note-preview]'?preview:null;
section.querySelectorAll=s=>s==='[data-note-tool]'?section.tools:s==='[data-note-panel]'?section.panels:s.includes('checkbox')?section.matches:s.includes('data-note-reviewed')?section.actions:[];
section.closest=s=>s==='[data-knowledge-review]'?f:null;
f.elements['action_'+id]=node();f.elements['label_'+id]=node();f.elements['label_'+id].value='Heading '+id;f.elements['content_'+id]=node();f.elements['content_'+id].value='Exact owner text '+id;f.elements['action_'+id].dispatchEvent=()=>{};sections.push(section);return section;});f.appendChild=el=>{f.elements[el.name]=el;};return f;}
const first=form('first',['a','b']),second=form('second',['c']);
const document={getElementById(){return null;},querySelectorAll:()=>[first,second],addEventListener(name,fn){(events[name]??=[]).push(fn);},createElement:()=>node()};
const window={innerHeight:844,addEventListener(){},dispatchEvent(){},get Blam8KnowledgeGuide(){return guidance;},blam8KnowledgeReviewChanged(data,choice){choices.push(choice);}};
vm.runInNewContext(fs.readFileSync('src/app/static/knowledge_onboarding_review.js','utf8'),{document,window,Event:class{},console});
async function click(section,kind){const button=section.actions[kind==='reviewed'?0:1];for(const fn of events.click||[])await fn({target:{closest:s=>s==='[data-note-tool]'?null:button}});}
assert(first.sections[0].panels.every(p=>p.hidden));
await click(first.sections[0],'reviewed');assert.equal(first.elements.action_a.value,'keep');assert(!first.sections[0].detail.open && first.sections[1].detail.open);assert.match(first.sections[0].querySelector('[data-note-status]').textContent,/Reviewed/);
await click(first.sections[1],'later');assert.equal(first.elements.action_b.value,'');assert.equal(first.elements.review_later_b.value,'1');assert.equal(first.elements.content_b.value,'Exact owner text b');assert.match(first.notice.textContent,/not been discarded/);
// Visible unresolved overlap keeps the reviewed note open at More options.
first.sections[1].detail.open=true;first.sections[1].matches=[{checked:false}];await click(first.sections[1],'reviewed');assert(first.sections[1].detail.open);assert.equal(first.sections[1].panels[2].hidden,false);
// Reconciliation must receive the originating second batch, never whichever guide was selected.
guidance={available:()=>true,working:()=>false,ask:async(q,f)=>{calls.push([q,f.dataset.knowledgeReview]);return {focus:{kind:'note',id:'c'}};}};
await click(second.sections[0],'reviewed');assert.deepEqual(calls,[['Show the current note','second']]);assert.deepEqual(JSON.parse(JSON.stringify(choices)),[{batch_id:'second',action:'keep',label:'Heading c'}]);assert.equal(second.elements.action_c.value,'keep');
// Tool disclosure preserves fields and source/merge panels.
const tools=second.sections[0].tools;for(const tool of tools){tool.closest=()=>second.sections[0];tool.attributes['aria-expanded']='false';for(const fn of events.click)await fn({target:{closest:s=>s==='[data-note-tool]'?tool:null}});assert.equal(tool.attributes['aria-expanded'],'true');}
assert.equal(second.elements.content_c.value,'Exact owner text c');
console.log('Manual Reviewed/later, local browsing, unresolved overlap, exact batch and edit/source/more controls verified.');
// Real empty-to-draft shell function moves existing nodes rather than rebuilding owner inputs.
const controller=fs.readFileSync('src/app/static/knowledge_onboarding_review.js','utf8');
const compact=controller.slice(controller.indexOf('function compactShell(){'),controller.indexOf('function replace(data, checkedId)'));
let pending=false;const order=[];
function shellNode(kind){const n={kind,dataset:{},nodes:[],appendChild(el){if(el.parent){el.parent.nodes.splice(el.parent.nodes.indexOf(el),1);}el.parent=this;this.nodes.push(el);},after(el){order.splice(order.indexOf(el),1);order.splice(order.indexOf(this)+1,0,el);}};Object.defineProperty(n,'firstChild',{get(){return this.nodes[0];}});return n;}
const holder=shellNode('holder'),manual=shellNode('manual'),coverage=shellNode('coverage'),evidence=shellNode('evidence');coverage.appendChild(evidence);order.push(coverage,manual,holder);manual.unsavedText='Owner edits';holder.querySelectorAll=()=>pending?[{dataset:{}}]:[];
const heading={textContent:'Give it your facts'},helper={hidden:false},savedStatus={removed:false,remove(){this.removed=true;}};const shell=vm.createContext({document:{getElementById:id=>({'knowledge-pending-reviews':holder,'knowledge-manual-controls':manual,'knowledge-coverage-shell':coverage}[id]),querySelector:s=>s==='[data-onboarding-heading]'?heading:s==='[data-onboarding-helper]'?helper:s==='[data-knowledge-saved-status]'&&!savedStatus.removed?savedStatus:null,createElement:s=>shellNode(s)}});
vm.runInContext(compact,shell);shell.compactShell();assert.equal(heading.textContent,'Give it your facts');assert(!savedStatus.removed);pending=true;shell.compactShell();assert(savedStatus.removed);assert.deepEqual(order.map(n=>n.kind),['holder','manual','coverage']);assert.equal(heading.textContent,'Review Knowledge');assert(helper.hidden);assert.equal(manual.unsavedText,'Owner edits');assert.equal(coverage.firstChild,evidence);shell.compactShell();assert.equal(coverage.nodes.length,1);
console.log('Dynamic draft arrival compacts the real shell while preserving manual fields and evidence nodes.');
