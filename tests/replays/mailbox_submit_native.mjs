// Trusted native input; CDP and fixture HTTP endpoints are loopback-only.
import assert from 'node:assert/strict';
const [debugPort, appPort, mode] = process.argv.slice(2).map(Number);
for (const port of [debugPort, appPort]) assert(Number.isInteger(port) && port > 0 && port < 65536);
assert([0, 1, 2].includes(mode));
const targets = await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
const target = targets.find(t => t.type === 'page');
const address = new URL(target.webSocketDebuggerUrl);
assert.equal(address.hostname, '127.0.0.1');
assert.equal(address.port, String(debugPort));
assert(address.pathname.startsWith('/devtools/page/'));
const socket = new WebSocket(address);
await new Promise((resolve, reject) => {socket.onopen=resolve;socket.onerror=reject;});
let sequence=0;
const pending=new Map();
socket.onmessage=event=>{const result=JSON.parse(event.data);const request=pending.get(result.id);if(request){pending.delete(result.id);clearTimeout(request.timeout);result.error?request.reject(result.error):request.resolve(result.result);}};
function cdp(method, params={}) {
  return new Promise((resolve,reject)=>{const id=++sequence;const timeout=setTimeout(()=>reject(Error('CDP timeout: '+method)),5000);pending.set(id,{resolve,reject,timeout});socket.send(JSON.stringify({id,method,params}));});
}
async function evaluate(expression) {
  const result=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});
  assert(!result.exceptionDetails,JSON.stringify(result.exceptionDetails));
  return result.result.value;
}
async function until(expression) {
  const deadline=Date.now()+8000;
  while(Date.now()<deadline){if(await evaluate(expression))return;await new Promise(r=>setTimeout(r,50));}
  throw Error('Native condition timed out: '+expression+'; state='+await evaluate('JSON.stringify({syncSubmitting:document.querySelector("form")?.dataset.syncSubmitting,body:document.body.textContent})'));
}
async function clickLater() {
  const point=await evaluate('(()=>{const r=document.querySelector("button[value=later]").getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()');
  await cdp('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,...point});
  await cdp('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,...point});
}
try {
  await until('!!window.Blam8DeviceSync && Blam8DeviceSync.isReady()');
  await evaluate('window.trustedClicks=[];document.addEventListener("click",e=>trustedClicks.push(e.isTrusted));true');
  await clickLater();
  if(mode===1){
    await until('window.cancelledOnce===true');
    const state=await (await fetch(`http://127.0.0.1:${appPort}/submission-state`)).json();
    assert.equal(state.choices.length,0);assert.equal(state.submitted.length,0);
    await until('!document.querySelector("form").dataset.syncSubmitting');
    assert(await evaluate('trustedClicks.every(Boolean) && trustedClicks.length===1'));
    await clickLater();
  }
  if(mode===2){
    // invalidReported: native validation has blocked the first attempt, so its async submit chain is finished.
    // (invalidatedOnce alone is set at the submit event, before that chain reaches requestSubmit.)
    await until('window.invalidReported===true && document.querySelector("[name=validation]").value==="" && !document.querySelector("form").dataset.syncSubmitting');
    const state=await (await fetch(`http://127.0.0.1:${appPort}/submission-state`)).json();
    assert.equal(state.choices.length,0);assert.equal(state.submitted.length,0);
    assert(await evaluate('trustedClicks.every(Boolean) && trustedClicks.length===1'));
    await evaluate('const field=document.querySelector("[name=validation]");field.value="ready";field.dispatchEvent(new Event("input",{bubbles:true}));true');
    await clickLater();
  }
  await until('document.body.textContent.includes("Decided later")');
  await cdp('Page.reload');
  await until('!!window.Blam8DeviceSync && Blam8DeviceSync.isReady() && document.body.textContent.includes("Decided later")');
  console.log('Trusted native Decide later submission and reload passed');
} finally {socket.close();}
