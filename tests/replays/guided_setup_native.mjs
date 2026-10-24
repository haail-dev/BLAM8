// Trusted native input; only disposable loopback HTTP and DevTools endpoints.
import assert from 'node:assert/strict';
import fs from 'node:fs';
const [debugPort,appPort,widthText,heightText,output,theme,area]=process.argv.slice(2);
for(const p of [debugPort,appPort])assert(+p>0&&+p<65536);
const targets=await(await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
const target=targets.find(t=>t.type==='page');
const address=new URL(target.webSocketDebuggerUrl);assert.equal(address.hostname,'127.0.0.1');
const socket=new WebSocket(address);await new Promise((r,j)=>{socket.onopen=r;socket.onerror=j;});
let sequence=0;const pending=new Map();
socket.onmessage=e=>{const m=JSON.parse(e.data),p=pending.get(m.id);if(p){pending.delete(m.id);clearTimeout(p.t);m.error?p.j(Error(JSON.stringify(m.error))):p.r(m.result);}};
function cdp(method,params={},session){return new Promise((r,j)=>{const id=++sequence,t=setTimeout(()=>j(Error('CDP timeout '+method)),8000);pending.set(id,{r,j,t});socket.send(JSON.stringify({id,method,params,...(session?{sessionId:session}:{})}));});}
async function evaluate(expression,session){const r=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true},session);assert(!r.exceptionDetails,JSON.stringify(r.exceptionDetails));return r.result.value;}
async function until(expression,session){const end=Date.now()+14000;while(Date.now()<end){if(await evaluate(expression,session))return;await new Promise(r=>setTimeout(r,50));}
 throw Error('Timed out '+expression+' '+await evaluate('document.body.innerText.slice(-6500)',session));}
async function click(selector){const p=await evaluate(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});for(let p=e.parentElement;p;p=p.parentElement)if(p.tagName==='DETAILS')p.open=true;e.scrollIntoView({block:'nearest'});const r=e.getBoundingClientRect();const x=r.x+r.width/2,y=r.y+r.height/2;if(!e.contains(document.elementFromPoint(x,y))&&document.elementFromPoint(x,y)!==e)throw Error('Control covered: '+${JSON.stringify(selector)}+' by '+document.elementFromPoint(x,y)?.outerHTML.slice(0,200));return {x,y};})()`);
 for(const type of ['mousePressed','mouseReleased'])await cdp('Input.dispatchMouseEvent',{type,button:'left',clickCount:1,...p});}
async function button(text){await until(`[...document.querySelectorAll('button')].some(e=>e.textContent===${JSON.stringify(text)}&&!e.disabled&&e.getClientRects().length)`);await evaluate(`(()=>{const e=[...document.querySelectorAll('button')].find(e=>e.textContent===${JSON.stringify(text)}&&!e.disabled&&e.getClientRects().length);e.dataset.nativeTarget='';})()`);await click('[data-native-target]');await evaluate("document.querySelector('[data-native-target]')?.removeAttribute('data-native-target')");}
async function key(key,code,vk,modifiers=0){for(const type of ['keyDown','keyUp'])await cdp('Input.dispatchKeyEvent',{type,key,code,windowsVirtualKeyCode:vk,nativeVirtualKeyCode:vk,modifiers,...(key==='Enter'&&type==='keyDown'?{text:'\r',unmodifiedText:'\r'}:{})});}
async function type(selector,text){await until(`document.querySelector(${JSON.stringify(selector)})&&!document.querySelector(${JSON.stringify(selector)}).disabled`);await click(selector);await key('a','KeyA',65,2);await cdp('Input.insertText',{text});}
async function shot(name){
 const geometry=await evaluate(`(()=>{
   const rect=s=>document.querySelector(s)?.getBoundingClientRect();
   const panel=rect('#bc-panel'),form=rect('#bc-form'),stream=rect('.guided-stream'),nav=rect('nav[aria-label="Primary"]');
   const messages=document.querySelector('#bc-messages'),tray=document.querySelector('.bc-setup-tray');
   return {fits:panel.top>=0&&panel.bottom<=innerHeight+1&&panel.left>=0&&panel.right<=innerWidth+1,
     separate:stream.bottom<=form.top+1&&form.bottom<=panel.bottom+1,
     navClear:!nav||nav.height===0||panel.bottom<=nav.top+1,
     singleScroll:getComputedStyle(messages).overflowY==='visible'&&getComputedStyle(tray).overflowY==='visible',
     title:document.querySelector('#bc-title').textContent,
     greetingHidden:[...document.querySelectorAll('[data-bc-greeting]')].every(e=>!e.getClientRects().length)};
 })()`);
 assert(geometry.fits&&geometry.separate&&geometry.navClear&&geometry.singleScroll&&geometry.greetingHidden,JSON.stringify(geometry));
 assert(geometry.title.includes('Setup'));
 const r=await cdp('Page.captureScreenshot',{format:'png'});fs.writeFileSync(output+'/'+name+'.png',Buffer.from(r.data,'base64'));}
try{
 await cdp('Page.enable');await cdp('Emulation.setDeviceMetricsOverride',{width:+widthText,height:+heightText,deviceScaleFactor:1,mobile:false});
 await cdp('Page.bringToFront');await cdp('Emulation.setFocusEmulationEnabled',{enabled:true});
 if(area==='handoff'){
   await until('document.querySelector("[data-onb-continue]") && window.Blam8DeviceSync?.isReady() && window.Blam8OnboardingJourney?.read(0).ready');
   if(await evaluate('!document.querySelector("#bc-panel").classList.contains("hidden")'))await click('#bc-close');
   await click('[data-onb-continue]');
   await until('location.pathname==="/app/setup/guide" && document.querySelector("[data-guided-start]:not(:disabled)") && window.Blam8GuidedSetup && window.Blam8DeviceSync?.isReady()');
   await click('[data-guided-start]');
   await until('location.pathname==="/app/diary" && document.querySelector(".guided-title")?.textContent==="Your diary"');
   await shot('00-diary-handoff');
   await button('Next');
   await until('location.pathname==="/app/diary/settings" && document.querySelector(".guided-title")');
   await button('Needed');
   await until('document.querySelector(".guided-question")?.textContent.includes("Already covered — We inspect equipment.")');
   await shot('01-reused-answer');
   for(let n=0;n<12&&await evaluate('Blam8GuidedSetup.currentStop()!=="diary_settings/stay_mode"');n++){
     const before=await evaluate('Blam8GuidedSetup.currentStop()');await button('Next');
     await until('Blam8GuidedSetup.currentStop()!=='+JSON.stringify(before));
   }
   await until('Blam8GuidedSetup.currentStop()==="diary_settings/stay_mode"');
 }
 await until('window.Blam8DeviceSync?.isReady() && document.querySelector(".guided-question .guided-title")');
 await evaluate(`localStorage.setItem('blam8-theme',${JSON.stringify(theme)});document.documentElement.dataset.theme=${JSON.stringify(theme)};true`);
 await evaluate("window.trustedEvents=[];document.addEventListener('click',e=>trustedEvents.push(e.isTrusted));true");
 await shot('01-question');
 if(area==='handoff'){
   await type('#bc-input','Use appointment times, not arrival-to-departure bookings.');await key('Enter','Enter',13);
   await until('document.querySelector("[name=booking_mode][data-proposed]")?.value==="appointment" && document.querySelector(".bc-setup-tray")?.textContent.includes("Accept displayed changes")');
   await until('document.querySelector(".guided-conversation")?.tagName==="SECTION" && [...document.querySelectorAll("[data-bc-role=assistant]")].some(e=>e.getClientRects().length&&e.textContent.includes("suggestion"))');
   await shot('02-mode-proposed');
   await button('Next');await until('Blam8GuidedSetup.currentStop()==="explain.diary_settings/stay_mode"');
   await shot('03-mode-explained');
   await evaluate("window.nativeBeforeAccept='old-document'");await button('Accept displayed changes');
   await until('window.nativeBeforeAccept!=="old-document" && window.Blam8DeviceSync?.isReady() && document.querySelector("[name=booking_mode]")?.value==="appointment" && !document.querySelector("[data-proposed]") && document.querySelector(".guided-question")?.innerText.includes("were saved")');
   await shot('04-mode-saved');
   await cdp('Page.navigate',{url:`http://127.0.0.1:${appPort}/app/setup/guide`});
   await until('location.pathname==="/app/diary/settings" && window.Blam8DeviceSync?.isReady() && window.Blam8GuidedSetup?.currentStop()==="explain.diary_settings/stay_mode"');
   assert(await evaluate('!document.querySelector(".guided-question").textContent.includes("Your answer — false")'));
   await shot('05-exact-resume');
   console.log('Actual Knowledge Continue, one-time handover, incomplete-intake Diary orientation, answer reuse, proposed mode, explanation and Accept passed');
 }else if(area==='knowledge_conflict'){
   await until('document.querySelector(".guided-knowledge-conflict")?.innerText.includes("Within 14 days")');
   assert(await evaluate('document.querySelector(".guided-knowledge-conflict").innerText.includes("Before work begins") && !document.querySelector("[name=payment_terms][data-proposed]")'));
   await shot('02-knowledge-conflict');
   await button('Use updated answer');
   await until('!document.querySelector(".guided-knowledge-conflict") && document.querySelector("[name=payment_terms][data-proposed]")?.value==="Before work begins"');
   await until('(()=>{const r=document.querySelector("[name=payment_terms]").getBoundingClientRect();return r.top>=0&&r.bottom<=document.querySelector("#bc-panel").getBoundingClientRect().top})()');
   assert(await evaluate('!document.querySelector("[data-guided-refresh]")'));
   await shot('03-resolved-proposal');
   await button('Accept displayed changes');
   await until('document.querySelector(".guided-question")?.innerText.includes("were saved") && document.querySelector("[name=payment_terms]")?.value==="Before work begins"');
   await shot('04-resolved-saved');
 }else if(area==='hours'){
   const original=await evaluate('Array.from(Blam8ProposalForms.data(document.querySelector("#scheduling")).getAll("working_days"))');
   await type('#bc-input','Bookings are available on Mondays and Wednesdays only.');await key('Enter','Enter',13);
   await until('document.querySelectorAll("[name=working_days][data-proposed]").length===7 && document.querySelector(".bc-setup-tray")?.textContent.includes("Accept displayed changes")');
   assert.deepEqual(await evaluate('[...document.querySelectorAll("[name=working_days]:checked")].map(e=>e.value)'),['mon','wed']);
   assert.deepEqual(await evaluate('Array.from(Blam8ProposalForms.data(document.querySelector("#scheduling")).getAll("working_days"))'),original);
   assert(await evaluate('[...document.querySelectorAll("[name=working_days]")].every(e=>e.disabled)'));
   await shot('02-exact-weekdays');
   await button('Accept displayed changes');
   // The saved receipt and the native form refresh arrive independently.
   // Wait for both; keep the exact UI and durable SQL assertions below.
   await until('document.querySelector(".guided-question")?.innerText.includes("were saved") && document.querySelectorAll("[data-proposed]").length===0 && [...document.querySelectorAll("[name=working_days]:checked")].map(e=>e.value).join(",")==="mon,wed"');
   assert.deepEqual(await evaluate('[...document.querySelectorAll("[name=working_days]:checked")].map(e=>e.value)'),['mon','wed']);
   await shot('04-weekdays-saved');
 }else if(area!=='diary'){
   await type('#bc-input',area==='records'?'Track Items with an optional text Reference, remembered once. It never blocks a booking.':
      'No VAT, no business details printed; payment terms On completion, quotes valid 30 days. Prices exclude VAT.');
   await key('Enter','Enter',13);await until('document.querySelector(".bc-setup-tray")?.innerText.includes("Accept displayed changes")');
   await shot('02-proposed');
   if(area==='records'){
     assert(await evaluate('!document.querySelector(".bc-setup-tray .bc-setup-problem") && document.querySelector("#add-group.guided-target")'));
     const cards=await(await fetch(`http://127.0.0.1:${appPort}/concierge/setup-cards`)).json();
     const card=cards.cards.find(c=>c.page==='dependants'),line=card.lines.find(l=>l.name==='field');
     const selector='[data-line-id="'+line.id+'"]';
     await evaluate(`document.querySelector(${JSON.stringify(selector)}).querySelectorAll('button').forEach(b=>{if(b.textContent==='Edit')b.dataset.nativeTarget=''})`);
     await click('[data-native-target]');await type('[data-field=label]','Owner reference');await button('Keep changes');
     await until('document.querySelector(".bc-setup-tray").innerText.includes("Owner reference")');
   }else{
     await type('[name=payment_terms]','After completion');await key('Tab','Tab',9);
     await until('document.querySelector(".bc-setup-tray").innerText.includes("After completion")');
   }
   await shot('03-edited');
   await evaluate("window.nativeBeforeAccept='old-document'");await button('Accept displayed changes');
   await until('window.nativeBeforeAccept!=="old-document" && window.Blam8DeviceSync?.isReady() && document.querySelector(".guided-question")?.innerText.includes("were saved")');
   await shot('04-saved');
   assert(await evaluate('document.documentElement.scrollWidth<=innerWidth+1 && document.getElementById("bc-input").getBoundingClientRect().bottom<=innerHeight+1'));
   console.log('Native '+area+' question, existing card edit, exact Accept and saved receipt passed');
 }else{
 await type('#bc-input','We need 24 hours notice and allow bookings 60 days ahead.');await key('Enter','Enter',13);
 await until('document.querySelector("[name=horizon_days]")?.value==="60" && document.querySelectorAll("[data-proposed]").length===2 && document.querySelector(".bc-setup-tray")?.textContent.includes("Accept displayed changes")');
 assert(await evaluate('document.querySelector("[name=min_notice_hours]").value==="24"'));
 await shot('02-proposed');
 if(+widthText===390){
   await button('Expand page');
   assert(await evaluate('document.body.classList.contains("guided-page-expanded")'));
   await shot('02-expanded-page');
   await button('Return to conversation');
   await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:420,deviceScaleFactor:1,mobile:false});
   await until('innerHeight===420 && getComputedStyle(document.documentElement).getPropertyValue("--guided-viewport")==="420px"');
   await evaluate('new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))');
   await shot('02-reduced-viewport');
   assert(await evaluate('document.querySelector("main").getBoundingClientRect().bottom<=document.querySelector("#bc-panel").getBoundingClientRect().top+1'));
   await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:+heightText,deviceScaleFactor:1,mobile:false});
   await until('getComputedStyle(document.documentElement).getPropertyValue("--guided-viewport")==='+JSON.stringify(heightText+'px'));
 }
 assert.equal(await evaluate('document.documentElement.dataset.theme'),theme);
 if(theme==='dark')assert(await evaluate('parseInt(getComputedStyle(document.querySelector(".bc-setup-tray")).backgroundColor.match(/\\d+/)[0],10)<80'));
 // The real sync checkpoint contains pre-projection native values, never 24/60.
 await evaluate('Blam8DeviceSync.flush()');
 const snapshot=await evaluate('Blam8DeviceSync.state().payload.form');
 assert(!Object.values(snapshot||{}).some(f=>f.values?.horizon_days?.[0]==='60'));
 assert(await evaluate('document.querySelector("#scheduling [data-save-row]").hidden'));
 const other=await cdp('Target.createTarget',{url:`http://127.0.0.1:${appPort}/app/diary/settings`});
 const follower=(await cdp('Target.attachToTarget',{targetId:other.targetId,flatten:true})).sessionId;
 await cdp('Page.enable',{},follower);
 await until('window.Blam8DeviceSync?.isReady() && document.querySelector("[name=horizon_days]")?.value==="60"',follower);
 assert(await evaluate('Blam8ProposalForms.data(document.querySelector("#scheduling")).get("horizon_days")!=="60"',follower));
 await cdp('Page.bringToFront');
 // Native Enter on the real proposed input must not submit its owning form.
 await click('[name=horizon_days]');await key('Enter','Enter',13);
 assert(await evaluate('location.pathname==="/app/diary/settings" && document.querySelector("[name=horizon_days]").value==="60"'));
 await type('[name=horizon_days]','45');await key('Tab','Tab',9);
 await until('document.querySelector("[data-line-id]") && document.querySelector("[name=horizon_days]").value==="45" && document.querySelector(".bc-setup-tray").innerText.includes("45")');
 await shot('03-edited');
 // Real follower restore retains unrelated local work and never restores a
 // projected value as an approved native value.
 const originalBuffer=await evaluate('document.querySelector("[name=buffer_minutes]").value');
 await type('[name=buffer_minutes]','10');await key('Tab','Tab',9);await evaluate('Blam8DeviceSync.flush()');
 await cdp('Page.reload',{},follower);
 await until('window.Blam8DeviceSync?.isReady() && document.querySelector("[name=buffer_minutes]")?.value==="10"',follower);
 assert(await evaluate('document.querySelector("[name=horizon_days]").value!=="60" && Blam8ProposalForms.data(document.querySelector("#scheduling")).get("buffer_minutes")==="10"',follower));
 await type('[name=buffer_minutes]',originalBuffer);await key('Tab','Tab',9);await evaluate('Blam8DeviceSync.flush()');
 await cdp('Page.reload',{},follower);
 await until('window.Blam8DeviceSync?.isReady() && document.querySelector("[name=horizon_days]")?.value==="45"',follower);
 await button('Next');await until('document.querySelector(".guided-title") && !document.querySelector(".guided-title").textContent.includes("Booking notice and horizon")');
 await button('Back');await until('document.querySelector(".guided-title").textContent.includes("Booking notice and horizon")');
 await click('.guided-more > summary');await button('Pause setup');await until('!document.body.classList.contains("guided-running")');
 await button('Resume guided setup');await until('document.body.classList.contains("guided-running") && document.querySelectorAll("[data-proposed]").length===2');
 const trusted=await evaluate('window.trustedEvents.length>=7 && window.trustedEvents.every(Boolean)');assert(trusted);
 await evaluate("window.nativeBeforeAccept='old-document'");
 await button('Accept displayed changes');
 await until('window.nativeBeforeAccept!=="old-document" && window.Blam8DeviceSync?.isReady() && document.querySelector(".guided-question")?.innerText.includes("were saved")');
 assert(await evaluate('document.querySelector("[name=horizon_days]").value==="45" && document.querySelector("[name=min_notice_hours]").value==="24"'));
 await shot('04-saved');
 await evaluate('Blam8DeviceSync.refresh()',follower);
 await until('window.Blam8DeviceSync?.isReady() && document.querySelector(".guided-question")?.innerText.includes("were saved")',follower);
 assert(await evaluate('document.querySelector("[name=horizon_days]").value==="45" && document.querySelectorAll("[data-proposed]").length===0',follower));
 await cdp('Target.closeTarget',{targetId:other.targetId});
 const measured=await evaluate('({overflow:document.documentElement.scrollWidth>innerWidth+1,composer:document.getElementById("bc-input").getBoundingClientRect().toJSON(),viewport:{width:innerWidth,height:innerHeight}})');
 measured.trusted=trusted;
 assert.equal(measured.overflow,false);assert.equal(measured.trusted,true);
 assert(measured.composer.bottom<=+heightText+1);
 fs.writeFileSync(output+'/native-measurements.json',JSON.stringify(measured,null,2));
 console.log('Trusted answer, projection, native submit guard, direct edit, Next/Back, pause/resume and exact Accept passed');
 }
}catch(e){try{await shot('failure');fs.writeFileSync(output+'/failure-geometry.json',JSON.stringify(await evaluate('({main:document.querySelector("main")?.getBoundingClientRect().toJSON(),panel:document.getElementById("bc-panel")?.getBoundingClientRect().toJSON(),height:innerHeight,width:innerWidth})'),null,2));}catch(_){}throw e;}finally{socket.close();}
