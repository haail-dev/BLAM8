// Local file fixture only; trusted input and its debugging endpoint stay isolated.
import assert from 'node:assert/strict';
const [debugPort,width]=process.argv.slice(2).map(Number);
assert(Number.isInteger(debugPort)&&debugPort>0&&debugPort<65536);
assert([390,1440].includes(width));
const targets=await(await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
const target=targets.find(t=>t.type==='page' && new URL(t.url).protocol==='file:' && new URL(t.url).pathname.endsWith('/edit.html'));
assert(target,'Only the disposable local edit fixture can be controlled');
const address=new URL(target.webSocketDebuggerUrl);
assert.equal(address.hostname,'127.0.0.1');assert.equal(address.port,String(debugPort));assert(address.pathname.startsWith('/devtools/page/'));
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
  while(Date.now()<deadline){try{if(await evaluate(expression))return;}catch(error){if(!/Execution context was destroyed|Cannot find context|Cannot find default execution context/.test(JSON.stringify(error)))throw error;}await new Promise(r=>setTimeout(r,50));}
  throw Error('Native condition timed out: '+expression+'; state='+await evaluate('JSON.stringify({syncSubmitting:document.querySelector("form")?.dataset.syncSubmitting,body:document.body.textContent})'));
}

try{
  await cdp('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:false});
  await evaluate('window.nativeBeforeReload=true');
  await cdp('Page.reload');
  await until('window.nativeEditReady===true && window.nativeBeforeReload!==true');
  assert.equal(await evaluate('innerWidth'),width);
  const point=await evaluate('(()=>{const b=document.querySelector("[data-source-apply]");b.scrollIntoView({block:"center"});const r=b.getBoundingClientRect();const x=r.x+r.width/2,y=r.y+r.height/2;if(document.elementFromPoint(x,y)!==b)throw Error("Apply is obscured");return {x,y};})()');
  await cdp('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',clickCount:1,...point});
  await cdp('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',clickCount:1,...point});
  await until('document.getElementById("result").dataset.status');
  assert.equal(await evaluate('document.getElementById("result").dataset.status'),'PASS',await evaluate('document.getElementById("result").textContent'));
  console.log(await evaluate('document.getElementById("result").textContent'));
}finally{socket.close();}
