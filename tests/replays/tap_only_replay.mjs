// The card's own button sends confirm_via "tap"; a spoken yes sends "words". The server
// refuses a delete card without the tap (tests/test_concierge_tap_only_delete.py), and the
// client must leave the card waiting when it does. Runs the real transport and card code
// from src/app/static/concierge_panel.js with offline IO.
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
let calls=[], response;
const ctx = vm.createContext({
  confirm:()=>true, AbortController, Promise, document: {createElement:tag=>new Element(tag)},
  csrfToken:()=> 'offline', KIND_LABELS:{}, THUMB_UP:'confirm', SKIP_BIN:'cancel', DECLINED_LINE:'Declined.',
  voiceStage:{}, loadWaiting(){}, loadDone(){}, scrollDown(){}, fadeAway(){}, settledByTap(){}, addLinks(){},
  setTimeout(fn,ms) { return {fn,ms,active:true}; }, clearTimeout(t) { if(t) t.active=false; },
  fetch: async (url,options) => { calls.push({url, body: options.body ? JSON.parse(options.body) : null}); return response(url); }
});
vm.runInContext(source.slice(source.indexOf('    function waitingDate'), source.indexOf('    function waitingCounts')) + helpers + voice, ctx);
const json=data=>({ok:true,json:async()=>data});
const receipt=(state,summary)=>({proposal_id:'p1',state,ok:state==='completed',summary});
const buttons=c=>c.children[0].children[1].children;
const executes=()=>calls.filter(x=>x.url.endsWith('/execute'));

for (const kind of ['invoice_delete_draft', 'quote_delete_draft', 'invoice_edit_draft', 'bulk']) {
  const make=()=>ctx.proposalCardNode({proposal_id:'p1',kind,summary:'A change',items:kind==='bulk'?[{kind:'invoice_edit_draft',ok:true,summary:'x',details:[]}]:undefined});

  // 1. the card's Confirm button: the tap marker, and nothing else about it changes
  calls=[]; response=url=>json(receipt('completed','Done.'));
  let c=make(); await buttons(c)[0].click();
  assert.equal(executes().length,1);
  assert.equal(executes()[0].body.confirm_via,'tap',kind+': the button must send the tap marker');
  assert.equal(executes()[0].body.proposal_id,'p1');
  assert.ok('draft_text' in executes()[0].body && 'selected' in executes()[0].body);

  // 2. a spoken yes: the words marker, never the tap
  calls=[]; response=url=>json(receipt('pending',"That is a delete: tap Confirm on the card."));
  c=make();
  const spoken=await ctx.voiceExecuteProposal({proposal_id:'p1'},c);
  assert.equal(executes().length,1);
  assert.equal(executes()[0].body.confirm_via,'words',kind+': a spoken yes must say it is words');
  assert.notEqual(executes()[0].body.confirm_via,'tap');

  // 3. the server's refusal leaves the card waiting and its buttons usable
  c._cz.lock();
  c._cz.applyOutcome(spoken);
  assert.equal(c.attrs['data-state'],'pending');
  assert.equal(c._cz.settled,false);
  assert.equal(buttons(c)[0].disabled,false); assert.equal(buttons(c)[1].disabled,false);
  assert.notEqual(buttons(c)[0].hidden,true);
}
console.log('Tap marker from the button, words marker from a spoken yes, card left waiting: passed.');
