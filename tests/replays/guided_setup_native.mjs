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
 const screenshot=await cdp('Page.captureScreenshot',{format:'png'});fs.writeFileSync(output+'/'+name+'.png',Buffer.from(screenshot.data,'base64'));
 const geometry=await evaluate(`(()=>{
   const rect=s=>document.querySelector(s)?.getBoundingClientRect();
   const panel=rect('#bc-panel'),form=rect('#bc-form'),stream=rect('.guided-stream'),nav=rect('nav[aria-label="Primary"]');
   const messages=document.querySelector('#bc-messages'),tray=document.querySelector('.bc-setup-tray');
   const header=rect('header[data-bc-full-width]'),progress=rect('[data-stepper-total]');
   return {fits:panel.top>=0&&panel.bottom<=innerHeight+1&&panel.left>=0&&panel.right<=innerWidth+1,
     separate:stream.bottom<=form.top+1&&form.bottom<=panel.bottom+1,
     navClear:!nav||nav.height===0||panel.bottom<=nav.top+1,
     singleScroll:getComputedStyle(messages).overflowY==='visible'&&getComputedStyle(tray).overflowY==='visible',
     title:document.querySelector('#bc-title').textContent,
     shell:document.querySelectorAll('[data-onboarding-shell]').length===1&&document.querySelectorAll('main').length===1&&
       document.querySelectorAll('[data-step-name-toggle]').length===7&&document.querySelectorAll('[aria-current="step"]').length===1&&
       !document.querySelector('[data-setup-strip],main.app-sky')&&!!document.querySelector('[data-onboarding-card]')&&
       document.querySelector('[data-onboarding-brand]').textContent==='BLAM8Automate'&&
       !!document.querySelector('header[data-bc-full-width] img[src*="blam8-assistant-glass"]'),
     noOverflow:document.documentElement.scrollWidth<=innerWidth+1,
     chromeVisible:header.top>=-1&&progress.top>=header.bottom-1&&progress.bottom<innerHeight,
     greetingHidden:[...document.querySelectorAll('[data-bc-greeting]')].every(e=>!e.getClientRects().length)};
 })()`);
 assert(geometry.fits&&geometry.separate&&geometry.navClear&&geometry.singleScroll&&geometry.greetingHidden&&geometry.shell&&geometry.noOverflow&&geometry.chromeVisible,JSON.stringify(geometry));
 assert(geometry.title.includes('Setup'));
 const expectedStep=area==='records'||name.includes('records')?5:['charges','knowledge_conflict'].includes(area)?7:6;
 assert((await evaluate('document.querySelector("[data-current-step]").getAttribute("aria-label")')).startsWith('Step '+expectedStep+':'));
 }
try{
 await cdp('Page.enable');await cdp('Emulation.setDeviceMetricsOverride',{width:+widthText,height:+heightText,deviceScaleFactor:1,mobile:false});
 await cdp('Page.bringToFront');await cdp('Emulation.setFocusEmulationEnabled',{enabled:true});
 if(area.startsWith('handoff')){
   await until('document.querySelector("[data-onb-continue]") && window.Blam8DeviceSync?.isReady() && window.Blam8OnboardingJourney?.read(0).ready');
   const chrome=await evaluate(`(()=>{const header=document.querySelector('header[data-bc-full-width]');return {brand:header.querySelector('[data-onboarding-brand]').textContent,height:header.getBoundingClientRect().height,background:getComputedStyle(document.body).backgroundColor,steps:document.querySelectorAll('[data-step-name-toggle]').length};})()`);
   if(await evaluate('!document.querySelector("#bc-panel").classList.contains("hidden")'))await click('#bc-close');
   await click('[data-onb-continue]');
   await until('location.pathname==="/app/setup/guide" && document.querySelector("[data-guided-start]:not(:disabled)") && window.Blam8GuidedSetup && window.Blam8DeviceSync?.isReady()');
   assert.deepEqual(await evaluate(`(()=>{const header=document.querySelector('header[data-bc-full-width]');return {brand:header.querySelector('[data-onboarding-brand]').textContent,height:header.getBoundingClientRect().height,background:getComputedStyle(document.body).backgroundColor,steps:document.querySelectorAll('[data-step-name-toggle]').length};})()`),chrome);
   const intro=await cdp('Page.captureScreenshot',{format:'png'});fs.writeFileSync(output+'/00-introduction.png',Buffer.from(intro.data,'base64'));
   assert(await evaluate(`!document.querySelector('[data-guided-intro] a[href="/app/setup"]')`));
   await until('document.querySelector("[data-guided-entry]:not(:disabled)") && document.querySelector("#bc-title").textContent.includes("Setup")');
   assert(await evaluate(`document.querySelector('[data-guided-entry]').textContent===document.querySelector('[data-guided-start]').textContent && !document.querySelector('#bc-cta').textContent.includes('Diary →')`));
   if(area==='handoff_assistant'){
     if(await evaluate('document.querySelector("#bc-panel").classList.contains("hidden")'))await click('#bc-bubble');
     await click('[data-guided-entry]');
   }else await click('[data-guided-start]');
   // Records comes first. An owner who is not ready leaves each section for later.
   await until('location.pathname==="/app/diary/dependants" && window.Blam8GuidedSetup?.currentStop()==="relevance.records.relationships"');
   await shot('00-records-handoff');
   for(const [stop,next] of [['relevance.records.relationships','/app/crm/fields'],['relevance.records.information','/app/crm/settings'],
                             ['relevance.records.progress','/app/diary/settings']]){
     await until('window.Blam8GuidedSetup?.currentStop()==='+JSON.stringify(stop)+' && window.Blam8DeviceSync?.isReady()');
     await button('Decide later');
     await until('location.pathname==='+JSON.stringify(next)+' && document.querySelector(".guided-title")');
   }
   // Diary then starts by asking which diaries the business keeps, on the real settings page.
   await until('location.pathname==="/app/diary/settings" && document.querySelector(".guided-title")?.textContent==="Your diaries"');
   await shot('00-diary-handoff');
   await click('#bc-close');
   await until('!document.body.classList.contains("guided-running")');
   await cdp('Page.navigate',{url:`http://127.0.0.1:${appPort}/app/setup/guide`});
   await until(`document.querySelector('[data-guided-start]:not(:disabled)')?.textContent==='Resume guided setup' && document.querySelector('[data-guided-entry]:not(:disabled)')?.textContent==='Resume guided setup'`);
   if(area==='handoff_assistant'){
     if(await evaluate('document.querySelector("#bc-panel").classList.contains("hidden")'))await click('#bc-bubble');
     await click('[data-guided-entry]');
   }else await click('[data-guided-start]');
   await until('location.pathname==="/app/diary/settings" && document.querySelector(".guided-title")?.textContent==="Your diaries"');
   // Discovery runs on the real Diary settings page, under its own heading.
   assert.equal(await evaluate(`document.querySelector('[data-onboarding-heading]').textContent`),'Diary settings');
   assert(!await evaluate(`document.querySelector('.guided-context')`));
   // The owner can drag the assistant taller; the guide keeps that size on its next page.
   const sheet=()=>evaluate(`Math.round(document.querySelector('#bc-panel').getBoundingClientRect().height)`);
   assert(await evaluate(`getComputedStyle(document.querySelector('#bc-resize-handle')).display!=='none'`));
   const before=await sheet(),grip=await evaluate(`(()=>{const r=document.querySelector('#bc-title').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);
   await cdp('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',buttons:1,clickCount:1,...grip});
   for(const step of [50,100,150])await cdp('Input.dispatchMouseEvent',{type:'mouseMoved',button:'left',buttons:1,x:grip.x,y:grip.y-step});
   await cdp('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',buttons:0,clickCount:1,x:grip.x,y:grip.y-150});
   const dragged=await sheet();assert(dragged>=before+140,JSON.stringify({before,dragged}));
   assert(await evaluate(`(()=>{const m=document.querySelector('main').getBoundingClientRect(),p=document.querySelector('#bc-panel').getBoundingClientRect();return m.bottom<=p.top+1;})()`));
   await cdp('Page.reload');
   await until('window.Blam8DeviceSync?.isReady() && document.querySelector(".guided-title")?.textContent==="Your diaries"');
   assert(Math.abs(await sheet()-dragged)<=2,JSON.stringify({dragged,next:await sheet()}));
   // Answers first: name a diary, describe it with the controls, give hours, confirm.
   await type('.guided-question textarea','Inspections');await button('Continue');
   await until('document.querySelector(".guided-title")?.textContent==="Inspections"');
   const answerText=async(text)=>{await type('.guided-choices input:not([type=checkbox])',text);await button('Use answer');};
   const stopNow=()=>evaluate('Blam8GuidedSetup.currentStop()');
   const after=async(work)=>{const before=await stopNow();await work();await until('Blam8GuidedSetup.currentStop()!=='+JSON.stringify(before));};
   await after(()=>answerText('Boiler check at 4 Elm Road'));
   await shot('01-diary-question');
   await after(()=>button('Customers ask for it'));
   await after(async()=>{await click('.guided-choices input[value=people]');await button('Use answer');});
   await after(()=>answerText('2'));
   await after(()=>button('A set time'));
   await after(()=>answerText('60'));
   await after(()=>button('No'));
   await after(()=>button('The customer confirms'));
   await until('document.querySelector(".guided-title")?.textContent==="Your hours"');
   for(const day of [0,1,2,3,4])await click(`.guided-hours-row:nth-child(${day+1}) input[type=checkbox]`);
   await after(()=>button('Use these hours'));
   await until('document.querySelector(".guided-diary-list")?.innerText.includes("Ready to set up")');
   assert(await evaluate('document.querySelector(".guided-question").innerText.includes("Hours: Monday to Friday, 09:00 to 17:00.")'));
   await shot('01-diary-profile');
   await button('That’s right');
   await until('document.querySelector(".guided-question")?.textContent.includes("Already covered — We inspect equipment.")');
   await shot('01-reused-answer');
   for(let n=0;n<12&&await evaluate('Blam8GuidedSetup.currentStop()!=="diary_settings/stay_mode"');n++){
     const before=await evaluate('Blam8GuidedSetup.currentStop()');await button('Next');
     await until('Blam8GuidedSetup.currentStop()!=='+JSON.stringify(before));
   }
   await until('Blam8GuidedSetup.currentStop()==="diary_settings/stay_mode"');
 }
 await until('window.Blam8DeviceSync?.isReady() && document.querySelector(".guided-question .guided-title")');
 // Restore the real cross-page transcript, including settled and unsettled
 // Knowledge cards. Earlier tests started empty and missed orphaned controls.
 const priorReview=[
   {role:'assistant',content:'Service exclusions: We do not offer short stays. Does this look right?',
    knowledge_card:{batch_id:'review',version:'v1',focus_hash:'exclusions',focus:{kind:'note',id:'exclusions'},
      actions:[{action:'keep',label:'Keep note'},{action:'discard',label:'Leave out'}],selected:'keep',state:'prepared'}},
   {role:'assistant',content:'Service area: Please provide a postcode. Does this look right?',
    knowledge_card:{batch_id:'review',version:'v1',focus_hash:'area',focus:{kind:'note',id:'area'},
      actions:[{action:'keep',label:'Keep note'},{action:'discard',label:'Leave out'}]}},
   {role:'user',content:'Your choice: Service area — Reviewed to keep (review choice; not final Save).'},
   {role:'assistant',content:'Knowledge review finished.',trail:[{label:'Reviewed source'}],links:[{label:'Previous settings',href:'/app/crm/settings'}]}
 ];
 await evaluate('Blam8DeviceSync.transcript('+JSON.stringify(priorReview)+')');
 await cdp('Page.reload');
 await until('window.Blam8DeviceSync?.isReady() && document.querySelector(".guided-question .guided-title") && document.querySelector(".guided-history:not([hidden])")');
 assert(await evaluate('document.querySelector(".guided-context")?.textContent.length>30'));
 assert(await evaluate('!document.querySelector("#bc-messages").innerText.includes("Service exclusions") && !document.querySelector("#bc-messages").innerText.includes("Previous settings")'));
 await button('Earlier conversation');
 assert(await evaluate('document.querySelector("#bc-messages").innerText.includes("Service exclusions") && document.querySelector("#bc-messages").innerText.includes("Your choice: Keep note.")'));
 assert(await evaluate('!document.querySelector("#bc-messages [data-knowledge-guide-actions]") && !document.querySelector("#bc-messages").textContent.includes("Chosen during review") && !document.querySelector("#bc-messages").textContent.includes("review choice; not final Save")'));
 assert.deepEqual(await evaluate('Blam8DeviceSync.state().payload.messages'),priorReview);
 await shot('00-history-context');
 await button('Hide earlier conversation');
 await evaluate(`localStorage.setItem('blam8-theme',${JSON.stringify(theme)});document.documentElement.dataset.theme=${JSON.stringify(theme)};true`);
 await evaluate("window.trustedEvents=[];document.addEventListener('click',e=>trustedEvents.push(e.isTrusted));true");
 await shot('01-question');
 if(area.startsWith('handoff')){
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
   console.log('Actual Knowledge Continue, one-time handover, diary discovery and profile, answer reuse, proposed mode, explanation and Accept passed');
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
 await click('#bc-close');await until('!document.body.classList.contains("guided-running")');
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
 // A new section on the same native page updates display state only.
 const beforeProgress=await evaluate('location.href');
 await evaluate(`document.dispatchEvent(new CustomEvent('onboarding:progress',{detail:{current:'charges',groups:[{key:'diary',done:true},{key:'records',done:false},{key:'charges',done:false}]}}))`);
 assert(await evaluate(`document.querySelector('[data-current-step]').getAttribute('aria-label')==='Step 7: Charges and payments, current step' && document.querySelector('[data-tuning-step="diary"] .onboarding-step-toggle').getAttribute('aria-label').endsWith(', completed') && !document.querySelector('[data-tuning-step="records"] .onboarding-step-toggle').getAttribute('aria-label').includes('completed')`));
 await click('[data-tuning-step="diary"] [data-step-name-toggle]');
 assert(await evaluate(`document.querySelector('[data-tuning-step="diary"] [data-step-name-toggle]').getAttribute('aria-expanded')==='true' && document.querySelectorAll('[data-current-step]').length===1`));
 assert.equal(await evaluate('location.href'),beforeProgress);
}catch(e){try{await shot('failure');fs.writeFileSync(output+'/failure-geometry.json',JSON.stringify(await evaluate('({main:document.querySelector("main")?.getBoundingClientRect().toJSON(),panel:document.getElementById("bc-panel")?.getBoundingClientRect().toJSON(),height:innerHeight,width:innerWidth})'),null,2));}catch(_){}throw e;}finally{socket.close();}
