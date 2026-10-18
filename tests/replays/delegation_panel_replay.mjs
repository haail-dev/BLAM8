import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const panel=fs.readFileSync('src/app/static/concierge_panel.js','utf8').replace(/\r\n/g,'\n');
function extract(name) {
  const start=panel.indexOf('    function '+name+'(');assert(start>=0);
  const end=panel.indexOf('\n    }\n',start);assert(end>=0);
  return panel.slice(start,end+6);
}
const attrs=new Map([['data-gone',''],['data-proposal','old']]);
const nodes={'[data-t-icon]':{},'[data-t-out]':{},'[data-t-reads]':{}};
const row={style:{display:'none'},setAttribute:(k,v)=>attrs.set(k,String(v)),getAttribute:k=>attrs.get(k),
  removeAttribute:k=>attrs.delete(k),hasAttribute:k=>attrs.has(k),querySelector:s=>nodes[s],
  classList:{toggle(){}},closest:()=>null};
const c={taskRows:{task:row},TASK_ICON:{},TASK_TONE:{},paintTaskHead(){},fadeAway(){throw Error('New revision must stay visible');}};
vm.createContext(c);
for(const name of ['verifiedReadLines','paintTaskRow','applyTask'])vm.runInContext(extract(name),c);
c.applyTask({id:'task',revision:2,row_version:10,status:'queued',proposal_id:null});
assert.equal(attrs.has('data-proposal'),false);assert.equal(attrs.has('data-gone'),false);assert.equal(row.style.display,'');
assert.equal(c.applyTask({id:'task',revision:1,row_version:100,status:'waiting',proposal_id:'old'}),false);
assert.equal(c.applyTask({id:'task',revision:2,row_version:9,status:'waiting',proposal_id:'old'}),false);
c.applyTask({id:'task',revision:2,row_version:11,status:'waiting',proposal_id:'new',outcome:'Prepared for Monday'});
assert.equal(attrs.get('data-proposal'),'new');assert.match(nodes['[data-t-out]'].textContent,/Monday/);
c.applyTask({id:'task',revision:2,row_version:12,status:'working',proposal_id:'new',outcome:'Executing'});
assert.equal(attrs.get('data-status'),'working');
// An unchanged poll result is NOT movement: reporting it refreshed the waiting/done lists, which
// poll tasks again - a request loop of ~3/s per open Concierge (10-14 Oct flood).
assert.equal(c.applyTask({id:'task',revision:2,row_version:12,status:'working',proposal_id:'new',outcome:'Executing'}),false);
assert(!c.verifiedReadLines([{tool:'lookup',result:{tenant_id:'private-id',price:'£45'}}]).includes('private-id'));
// Exercise the actual panel active-card callback inside the actual voice gap.
const callback=panel.slice(panel.indexOf('          approvalWaiting: function () {')+27,panel.indexOf('          taskRead: async function'));
const callbackBody=callback.slice(callback.indexOf('{'),callback.lastIndexOf('}')+1);
const voice=fs.readFileSync('src/app/static/voice-live.js','utf8').replace(/\r\n/g,'\n');
const block=voice.slice(voice.indexOf('  const delivery = createTaskDelivery({'),voice.indexOf('\n\n  // Audio out'));
const gap={open:true,document:{visibilityState:'visible'},phase:'listening',turnBusy:()=>false,turnInFlight:null,
  userSpeaking:false,turns:{hasPending:()=>false},heldWords:null,liveSources:[],micMutedUntil:0,
  voiceStage:{proposal:null,draft:null},pendingConfirmations:0,Date,
  createTaskDelivery:d=>{gap.captured=d;return{};},deps:{}};
vm.createContext(gap);
vm.runInContext('function approvalWaiting() '+callbackBody,gap);
gap.deps.approvalWaiting=gap.approvalWaiting;
gap.deps.confirmationBusy=()=>gap.pendingConfirmations>0;
vm.runInContext(block,gap);
assert(gap.captured.gap());
gap.voiceStage.proposal={proposal_id:'Alice'};assert(!gap.captured.gap());
gap.voiceStage.proposal=null;gap.voiceStage.draft={_draft:{pending:()=>true}};assert(!gap.captured.gap());
gap.voiceStage.draft=null;gap.pendingConfirmations=1;assert(!gap.captured.gap());
gap.pendingConfirmations=0;
for(const [key,value] of [['userSpeaking',true],['heldWords',{}],['turnInFlight',{}],['liveSources',[{}]]]) {
  const old=gap[key];gap[key]=value;assert(!gap.captured.gap());gap[key]=old;
}
gap.document.visibilityState='hidden';assert(!gap.captured.gap());
console.log('delegation panel and voice gap replay passed');
