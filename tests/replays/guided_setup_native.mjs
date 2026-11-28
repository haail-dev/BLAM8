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
async function click(selector, toggleSummary=false){const p=await evaluate(`(()=>{const e=document.querySelector(${JSON.stringify(selector)});for(let p=e.parentElement;p;p=p.parentElement)if(p.tagName==='DETAILS'&&!(${toggleSummary}&&e.tagName==='SUMMARY'&&p===e.parentElement))p.open=true;e.scrollIntoView({block:'nearest'});const r=e.getBoundingClientRect();const x=r.x+r.width/2,y=r.y+r.height/2;if(!e.contains(document.elementFromPoint(x,y))&&document.elementFromPoint(x,y)!==e)throw Error('Control covered: '+${JSON.stringify(selector)}+' by '+document.elementFromPoint(x,y)?.outerHTML.slice(0,200));return {x,y};})()`);
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
     chromeVisible:header.top>=-1&&!!progress,
     greetingHidden:[...document.querySelectorAll('[data-bc-greeting]')].every(e=>!e.getClientRects().length)};
 })()`);
 assert(geometry.fits&&geometry.separate&&geometry.navClear&&geometry.singleScroll&&geometry.greetingHidden&&geometry.shell&&geometry.noOverflow&&geometry.chromeVisible,JSON.stringify(geometry));
 assert(geometry.title.includes('Setup'));
 const expectedStep=['records','contact_fields','pipeline','held_pipeline'].includes(area)||name.includes('records')?5:['charges','knowledge_conflict'].includes(area)?7:6;
 assert((await evaluate('document.querySelector("[data-current-step]").getAttribute("aria-label")')).startsWith('Step '+expectedStep+':'));
 }
try{
 await cdp('Page.enable');await cdp('Emulation.setDeviceMetricsOverride',{width:+widthText,height:+heightText,deviceScaleFactor:1,mobile:false});
 await cdp('Page.bringToFront');await cdp('Emulation.setFocusEmulationEnabled',{enabled:true});
 if(area.startsWith('handoff')){
   await until('document.querySelector("[data-onb-continue]") && window.Blam8DeviceSync?.isReady() && window.Blam8OnboardingJourney?.read(0).ready');
   const chrome=await evaluate(`(()=>{const header=document.querySelector('header[data-bc-full-width]');return {brand:header.querySelector('[data-onboarding-brand]').textContent,height:header.getBoundingClientRect().height,background:getComputedStyle(document.body).backgroundColor,steps:document.querySelectorAll('[data-step-name-toggle]').length};})()`);
   if(area==='handoff_assistant'){
     if(await evaluate('document.querySelector("#bc-panel").classList.contains("hidden")'))await click('#bc-bubble');
     await until('document.querySelector("#bc-cta button")?.textContent.includes("Records")');
     await click('#bc-cta button');
   }else{
     if(await evaluate('!document.querySelector("#bc-panel").classList.contains("hidden")'))await click('#bc-close');
     await click('[data-onb-continue]');
   }
   // Records comes first. An owner who is not ready leaves each section for later.
   await until('location.pathname==="/app/diary/dependants" && window.Blam8GuidedSetup?.currentStop()==="relevance.records.relationships"');
   assert.equal(await evaluate('window.initialTranscriptVisibility'),'hidden');
   assert.deepEqual(await evaluate(`(()=>{const header=document.querySelector('header[data-bc-full-width]');return {brand:header.querySelector('[data-onboarding-brand]').textContent,height:header.getBoundingClientRect().height,background:getComputedStyle(document.body).backgroundColor,steps:document.querySelectorAll('[data-step-name-toggle]').length};})()`),chrome);
   assert(await evaluate(`!document.querySelector('[data-guided-intro]')`));
   const layout = await evaluate(`({
     first: window.firstSetupLayout,
     width: document.querySelector('[data-onboarding-column]').getBoundingClientRect().width,
     padding: parseFloat(getComputedStyle(document.body).paddingRight)
   })`);
   assert.equal(layout.first.padding, layout.padding, 'Records dock space must be reserved before the widget loads');
   assert(Math.abs(layout.first.width-layout.width)<=1, JSON.stringify(layout));
   assert(+widthText>=768 ? layout.padding>300 : layout.padding===0, JSON.stringify(layout));
   await shot('00-records-handoff');
   for(const [stop,next] of [['relevance.records.relationships','/app/crm/fields'],['relevance.records.information','/app/crm/settings'],
                             ['relevance.records.progress','/app/diary/settings']]){
     await until('window.Blam8GuidedSetup?.currentStop()==='+JSON.stringify(stop)+' && window.Blam8DeviceSync?.isReady()');
     if(stop==='relevance.records.progress'){
       assert(await evaluate(`(()=>{
         const card=document.querySelector('[data-onboarding-card]'), title=card.querySelector('h1');
         const help=title.parentElement.querySelector('a[href="/help"]');
         if(!help || title.textContent.trim()!=='CRM' || card.querySelector('a[href="/app/crm/fields"]'))return false;
         const heading=title.getBoundingClientRect(), link=help.getBoundingClientRect();
         return link.left>=heading.right && link.top<heading.bottom && link.bottom>heading.top;
       })()`),'CRM heading and Help share a row, without a Fields detour');
     }
     if(stop==='relevance.records.relationships' && area==='handoff'){
       await click('[data-record-type-choice] [value=deferred]');
     }else {
       const selector = '.guided-navigation button';
       await until(`Array.from(document.querySelectorAll('${selector}')).some(b=>b.textContent==='Decide later')`);
       await evaluate(`Array.from(document.querySelectorAll('${selector}')).find(b=>b.textContent==='Decide later').dataset.chatLater='1'`);
       await click('[data-chat-later]');
     }
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
   if (+widthText<768) {
     assert(await evaluate(`getComputedStyle(document.querySelector('#bc-resize-handle')).display!=='none'`));
     const before=await sheet(),grip=await evaluate(`(()=>{const r=document.querySelector('#bc-title').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);
     await cdp('Input.dispatchMouseEvent',{type:'mousePressed',button:'left',buttons:1,clickCount:1,...grip});
     for(const step of [50,100,150])await cdp('Input.dispatchMouseEvent',{type:'mouseMoved',button:'left',buttons:1,x:grip.x,y:grip.y-step});
     await cdp('Input.dispatchMouseEvent',{type:'mouseReleased',button:'left',buttons:0,clickCount:1,x:grip.x,y:grip.y-150});
     const resized=await sheet();assert(resized>=before+140,JSON.stringify({before,resized}));
     assert(await evaluate(`(()=>{const m=document.querySelector('main').getBoundingClientRect(),p=document.querySelector('#bc-panel').getBoundingClientRect();return m.bottom<=p.top+1;})()`));
   } else {
     assert(await evaluate(`document.querySelector('main').getBoundingClientRect().right <= document.querySelector('#bc-panel').getBoundingClientRect().left + 1`));
   }
   const dragged=await sheet();
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
   await after(()=>button('One-off'));
   await after(()=>button('The customer confirms'));
   await until('document.querySelector(".guided-title")?.textContent==="Your hours"');
   await click('.guided-question details:has(.guided-hours) > summary');
   const contactMonday='input[aria-label="Contact Monday opening"]';
   const contactVisible=()=>evaluate('document.querySelector('+JSON.stringify(contactMonday)+').getClientRects().length>0');
   const sameHours='.guided-question label:has(+ .guided-hours) input[type=checkbox]';
   assert.equal(await contactVisible(),false);
   await click(sameHours);assert.equal(await contactVisible(),true);
   await evaluate('document.querySelector('+JSON.stringify(contactMonday)+').value="10:30"');
   await click(sameHours);assert.equal(await contactVisible(),false);
   await click(sameHours);assert.equal(await evaluate('document.querySelector('+JSON.stringify(contactMonday)+').value'),'10:30');
   await click(sameHours);
   for(const day of [0,1,2,3,4])await click(`.guided-hours-row:nth-child(${day+1}) input[type=checkbox]`);
   await after(()=>button('Use these hours'));
   await until('document.querySelector(".guided-diary-list")?.innerText.includes("Ready to set up")');
   assert(await evaluate('document.querySelector(".guided-question").innerText.includes("Hours: Monday to Friday, 09:00 to 17:00.")'));
   await shot('01-diary-profile');
   await button('That’s right');
   await until('document.querySelector(".guided-question")?.textContent.includes("Already covered — We inspect equipment.")');
   await shot('01-reused-answer');
   const direct='.guided-answer-field .guided-direct-answer';
   assert(await evaluate(`document.querySelector('${direct}').checkVisibility() && !document.querySelector('${direct}').closest('details')`));
   if(area==='handoff_assistant'){
     await type(direct,'I inspect equipment on site.');
     await button('Save and continue');
   }else{
     await button('Next');
   }
   await until('Blam8GuidedSetup.currentStop()==="question.activity.authority" && document.querySelector(".guided-answer-field select")');
   await click(direct);await key('ArrowDown','ArrowDown',40);await key('Enter','Enter',13);
   await until('document.querySelector(".guided-navigation .guided-primary")?.textContent==="Save and continue"');
   await button('Save and continue');
   await until('Blam8GuidedSetup.currentStop()!=="question.activity.authority"');
   for(let n=0;n<12&&await evaluate('Blam8GuidedSetup.currentStop()!=="diary_settings/stay_mode"');n++){
     const before=await evaluate('Blam8GuidedSetup.currentStop()');await button('Next');
     await until('Blam8GuidedSetup.currentStop()!=='+JSON.stringify(before));
   }
   await until('Blam8GuidedSetup.currentStop()==="diary_settings/stay_mode"');
   await until(`(() => {
     const mode=document.querySelector('#booking-mode select');
     const saved=mode.querySelector('option[selected]').textContent.split(' (')[0];
     const bounds=mode.getBoundingClientRect(), main=document.querySelector('main').getBoundingClientRect();
     const bar=document.querySelector('.guided-navigation'), toggle=bar.querySelector('.guided-more > summary').getBoundingClientRect();
     return mode.closest('details').open && bounds.top>=main.top && bounds.bottom<=Math.min(main.bottom,innerHeight) &&
       mode.closest('.guided-target') && bar.scrollTop===0 && toggle.top>=bar.getBoundingClientRect().top && toggle.bottom<=bar.getBoundingClientRect().bottom+1 &&
       document.querySelector('.guided-question').innerText.includes('Current booking mode: '+saved) &&
       bar.innerText.includes('Keep “'+saved+'”');
   })()`);
   await click('.guided-more > summary',true);
   assert(await evaluate('document.querySelector(".guided-more[open]").innerText.includes("Decide later") && document.querySelector(".guided-more[open]").innerText.includes("Not needed")'));
   await click('.guided-more > summary',true);
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
 assert(await evaluate('document.querySelector(".guided-question").innerText.trim().length>30'));
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
   await until('document.querySelector("[name=booking_mode][data-proposed]")?.value==="appointment" && document.querySelector(".bc-setup-tray")?.textContent.includes("Accept changes")');
   await until('document.querySelector(".guided-conversation")?.tagName==="SECTION" && [...document.querySelectorAll("[data-bc-role=assistant]")].some(e=>e.getClientRects().length&&e.textContent.includes("suggestion"))');
   await shot('02-mode-proposed');
   await until('document.querySelector(".guided-composer-next")?.getAttribute("aria-disabled")==="true"');
   assert(!await evaluate('document.querySelector(".guided-next-notice")?.checkVisibility()'));
   await button('Next');
   await until('document.querySelector("#bc-form .guided-next-notice")?.innerText.includes("choose Leave")');
   assert.equal(await evaluate('Blam8GuidedSetup.currentStop()'),'diary_settings/stay_mode');
   await shot('03-mode-awaiting-review');
   await evaluate("window.nativeBeforeAccept='old-document'");await button('Accept changes');
   await until('window.nativeBeforeAccept!=="old-document" && window.Blam8DeviceSync?.isReady() && document.querySelector("[name=booking_mode]")?.value==="appointment" && !document.querySelector("[data-proposed]") && document.querySelector(".guided-question")?.innerText.includes("were saved")');
   await shot('04-mode-saved');
   await cdp('Page.navigate',{url:`http://127.0.0.1:${appPort}/app/setup/guide`});
   await until('location.pathname==="/app/diary/settings" && window.Blam8DeviceSync?.isReady() && window.Blam8GuidedSetup?.currentStop()==="diary_settings/stay_mode"');
   assert(await evaluate('!document.querySelector(".guided-question").textContent.includes("Your answer — false")'));
   await shot('05-exact-resume');
   await until('document.querySelector(".guided-composer-next")?.getAttribute("aria-disabled")==="false" && !document.querySelector(".guided-next-notice")');
   await button('Next');await until('document.querySelector(".guided-progress")?.textContent.includes("Section 2 of 3")');
   console.log('Knowledge handover, diary profile, booking mode, visible options and pending review passed');
 }else if(area==='pipeline'||area==='held_pipeline'){
   if(area==='pipeline'){
     await type('#bc-input',"Yes. I'd like to track each enquiry as New enquiry, Quote sent, Booked or Declined.");
     await key('Enter','Enter',13);
   }else{
     await until('document.querySelector(".bc-setup-tray").innerText.includes("Review current difference")');
     assert(!await evaluate('document.querySelector(".bc-setup-tray").innerText.includes("Accept changes")'));
     const rejected=await evaluate(`(async()=>{
       const id=document.querySelector('.bc-setup-tray [data-line-id]').dataset.lineId;
       const response=await Blam8DeviceSync.request('/concierge/setup-cards/crm_settings/preview',{
         method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({revision:1,ids:[id]})});
       return {status:response.status,conflict:Blam8DeviceSync.hasConflict()};
     })()`);
     assert.deepEqual(rejected,{status:409,conflict:false});
     await button('Review current difference');
   }
   await until('Blam8GuidedSetup.currentStop()==="crm_settings/stage" && document.querySelector(".bc-setup-tray").innerText.includes("Accept changes")');
   assert(await evaluate(`(() => {
     const row=document.querySelector('.bc-setup-tray [data-line-id]');
     const group=row.parentElement, accept=group.lastElementChild;
     return accept.textContent==='Accept changes' && [...group.querySelectorAll('[data-line-id]')].every(line =>
       (line.compareDocumentPosition(accept) & Node.DOCUMENT_POSITION_FOLLOWING) &&
       ['Edit','Leave'].every(text => [...line.querySelectorAll('button')].some(button => button.textContent===text)));
   })()`));
   const summary=await evaluate('document.querySelector("[data-pending-stages]").innerText');
   for(const label of ['New enquiry','Quote sent','Booked','Declined','Pending review'])assert(summary.includes(label));
   assert(await evaluate('document.querySelectorAll("[data-stage-row]").length===0'));
   await button('Accept changes');
   await until('document.querySelectorAll("[data-stage-row]").length===4 && document.querySelector("[data-pending-stages]").hidden');
   assert.deepEqual((await evaluate('Array.from(document.querySelectorAll("[data-stage-row] [name=name]"),el=>el.value)')).sort(),['Booked','Declined','New enquiry','Quote sent']);
   await button('Next');
   if(area==='pipeline'){
     await until('Blam8GuidedSetup.currentStop()==="crm_settings/quiet_days"');
     await until('document.querySelector(".guided-context")?.innerText.includes("flags a contact")');
   }else{
     await until('Blam8GuidedSetup.currentStop()!=="crm_settings/stage"');
   }
 }else if(area==='contact_fields'){
   await type('#bc-input',"Yes. I'd like to keep a phone number. It's optional and only I should see it.");
   await key('Enter','Enter',13);
   await until('Blam8GuidedSetup.currentStop()==="crm_fields/contact_field" && document.querySelector(".bc-setup-tray").innerText.includes("Accept changes")');
   const summary = '[data-pending-contact-fields]';
   assert(await evaluate(`document.querySelector('${summary}').innerText.includes('Phone number') && document.querySelector('${summary}').innerText.includes('Hidden from other staff')`));
   assert(!await evaluate('document.querySelector(".guided-navigation").innerText.includes("Not needed") && document.querySelector(".guided-navigation").innerText.includes("Needed")'));
   const card = '.bc-setup-line';
   const changes = await evaluate(`document.querySelector('${card} details').textContent`);
   assert(changes.includes('When missing: Never ask') && changes.includes('Requirement: Optional'), changes);
   assert(changes.includes('Staff visibility: Private-detail access required'), changes);
   assert(!/Recall days|valid months|Not set|passive|durability|Reminder|Choices:/.test(changes), changes);
   const defaults = await evaluate(`Array.from(document.querySelectorAll('${card} details')).find(el=>el.querySelector('summary').textContent==='Defaults used')?.textContent`);
   assert(defaults && defaults.includes('Format: Text') && defaults.includes('Expiry: Does not expire'), defaults);
   assert.equal(await evaluate(`getComputedStyle(document.querySelector('${card}')).borderTopWidth`),'0px');
   await until('document.querySelector(".bc-setup-applied")?.getClientRects().length===0 && !document.querySelector(".guided-history").hidden');
   await click('.guided-history');
   await click('.bc-setup-applied summary',true);
   await until('document.querySelector(".bc-setup-applied[open]")?.innerText.includes("Pending review: Phone number")');
   await click('.guided-history');
   await shot('02-contact-proposed');
   const peerTab=await cdp('Target.createTarget',{url:`http://127.0.0.1:${appPort}/app/crm/fields`});
   const peer=(await cdp('Target.attachToTarget',{targetId:peerTab.targetId,flatten:true})).sessionId;
   await until('window.Blam8DeviceSync?.isReady() && document.querySelector("[data-pending-contact-fields]:not([hidden])")',peer);
   // Record even a short-lived warning on the old document before the refresh.
   const watchWarnings=`new MutationObserver(()=>{if(document.querySelector('[data-setup-reload]'))
     sessionStorage.setItem('testSawReloadWarning','yes');}).observe(document.body,{subtree:true,childList:true});true`;
   await evaluate(watchWarnings,peer);
   await cdp('Page.bringToFront');
   await evaluate(watchWarnings);
   await evaluate('window.beforeContactSave=true');
   await button('Accept changes');
   await until('!window.beforeContactSave && document.querySelector(".guided-question")?.innerText.includes("were saved") && document.querySelector("[data-pending-contact-fields]").hidden');
   assert.equal(await evaluate('sessionStorage.getItem("testSawReloadWarning")'),null);
   assert(!await evaluate('!!document.querySelector("[data-setup-reload]")'));
   await until('sessionStorage.getItem("testSawReloadWarning")==="yes" || [...document.querySelectorAll("input[name=label]")].some(e=>e.value==="Phone number")',peer);
   await cdp('Target.closeTarget',{targetId:peerTab.targetId});
   const fieldsLayout = await evaluate(`Array.from(document.querySelectorAll('.add-field')).map(form=>{
     const panel=form.closest('.field-panel'), card=form.closest('[data-onboarding-card]');
     const left=form.getBoundingClientRect().left-card.getBoundingClientRect().left;
     const hint=form.querySelector('[name=extraction_hint]').getBoundingClientRect();
     return {entity:form.elements.entity.value, left, hint:hint.width, form:form.clientWidth,
       fits:panel.scrollWidth<=panel.clientWidth+1};
   })`);
   assert.deepEqual(fieldsLayout.map(row=>row.entity),['contact','company','deal']);
   assert(fieldsLayout.every(row=>row.left<=28 && row.hint>=row.form-2 && row.fits),JSON.stringify(fieldsLayout));
   const visibility=await evaluate(`(()=>{
     const form=document.querySelector('[data-record-entity=contact] .add-field');
     const {field_type,durability,options,recall_days,valid_months}=form.elements;
     function visible(control){return !control.disabled && !!control.getClientRects().length;}
     function choose(control,value){control.value=value;control.dispatchEvent(new Event('change',{bubbles:true}));}
     const textOnly=!visible(options)&&!visible(durability)&&!visible(recall_days);
     choose(field_type,'dropdown');const dropdown=visible(options)&&!visible(durability);
     choose(field_type,'date');const date=visible(durability)&&!visible(options)&&!visible(recall_days);
     choose(durability,'expires');const expires=visible(recall_days)&&visible(valid_months);
     choose(durability,'once');choose(field_type,'text');
     return {textOnly,dropdown,date,expires};
   })()`);
   assert.deepEqual(visibility,{textOnly:true,dropdown:true,date:true,expires:true});
   await evaluate(`document.querySelector('.add-field input[value=company]').closest('.field-panel').scrollIntoView({block:'start'})`);
   await shot('05-companies-and-deals');
   await button('Next');
   await until('Blam8GuidedSetup.currentStop()==="need.company_fields" && document.querySelector(".guided-target")?.dataset.recordEntity==="company"');
   assert.equal(await evaluate('location.pathname'),'/app/crm/fields');
   await click('#bc-close');await until('!document.body.classList.contains("guided-running") && document.querySelector("#bc-panel").classList.contains("hidden")');
   assert.equal(await evaluate('!!document.querySelector(".guided-resume")'),false);
   // Closing the dock animates the page width. The help link moves with its header.
   await evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))).then(()=>Promise.all(document.getAnimations().filter(a=>a.transitionProperty==="padding-right").map(a=>a.finished.catch(()=>{}))))');
   await click('main a[href="/help"]');
   await until('!document.querySelector("#bc-panel").classList.contains("hidden")');
   await button('Resume guided setup');await until('document.body.classList.contains("guided-running")');
   await button('Needed');await until('Blam8GuidedSetup.currentStop()==="editor.company_fields"');
   await button('Next');await until('Blam8GuidedSetup.currentStop()==="need.deal_fields" && document.querySelector(".guided-target")?.dataset.recordEntity==="deal"');
   await button('Not needed');await until('window.Blam8GuidedSetup?.currentStop()==="relevance.records.progress" && location.pathname==="/app/crm/settings"');
 }else if(area==='knowledge_conflict'){
   await until('document.querySelector(".guided-knowledge-conflict")?.innerText.includes("Within 14 days")');
   assert(await evaluate('document.querySelector(".guided-knowledge-conflict").innerText.includes("Before work begins") && !document.querySelector("[name=payment_terms][data-proposed]")'));
   await shot('02-knowledge-conflict');
   await button('Use updated answer');
   await until('!document.querySelector(".guided-knowledge-conflict") && document.querySelector("[name=payment_terms][data-proposed]")?.value==="Before work begins"');
   await until('(()=>{const r=document.querySelector("[name=payment_terms]").getBoundingClientRect();return r.top>=0&&r.bottom<=document.querySelector("#bc-panel").getBoundingClientRect().top})()');
   assert(await evaluate('!document.querySelector("[data-guided-refresh]")'));
   await shot('03-resolved-proposal');
   await button('Accept changes');
   await until('document.querySelector(".guided-question")?.innerText.includes("were saved") && document.querySelector("[name=payment_terms]")?.value==="Before work begins"');
   await shot('04-resolved-saved');
 }else if(area==='hours'){
   const original=await evaluate('Array.from(Blam8ProposalForms.data(document.querySelector("#scheduling")).getAll("working_days"))');
   await type('#bc-input','Bookings are available on Mondays and Wednesdays only.');await key('Enter','Enter',13);
   await until('document.querySelectorAll("[name=working_days][data-proposed]").length===7 && document.querySelector(".bc-setup-tray")?.textContent.includes("Accept changes")');
   assert.deepEqual(await evaluate('[...document.querySelectorAll("[name=working_days]:checked")].map(e=>e.value)'),['mon','wed']);
   assert.deepEqual(await evaluate('Array.from(Blam8ProposalForms.data(document.querySelector("#scheduling")).getAll("working_days"))'),original);
   assert(await evaluate('[...document.querySelectorAll("[name=working_days]")].every(e=>e.disabled)'));
   await shot('02-exact-weekdays');
   await button('Accept changes');
   // The saved receipt and the native form refresh arrive independently.
   // Wait for both; keep the exact UI and durable SQL assertions below.
   await until('document.querySelector(".guided-question")?.innerText.includes("were saved") && document.querySelectorAll("[data-proposed]").length===0 && [...document.querySelectorAll("[name=working_days]:checked")].map(e=>e.value).join(",")==="mon,wed"');
   assert.deepEqual(await evaluate('[...document.querySelectorAll("[name=working_days]:checked")].map(e=>e.value)'),['mon','wed']);
   await shot('04-weekdays-saved');
 }else if(area!=='diary'){
   if(area==='records'){
     const saveVisible="document.querySelector('#dep-form [type=submit]').getClientRects().length > 0";
     async function checkSave(expected) { await until('('+saveVisible+') === '+expected); }
     const editForm=async(selector,value)=>evaluate(`(()=>{const field=document.querySelector(${JSON.stringify(selector)});field.value=${JSON.stringify(value)};field.dispatchEvent(new Event('input',{bubbles:true}));})()`);
     await checkSave(false);
     await evaluate("document.querySelector('#add-ladder').click()");
     await checkSave(false);
     const widths=await evaluate(`(()=>{
       const form=document.querySelector('#dep-form');
       const label=form.querySelector('[name=l_label]'),notify=form.querySelector('[name=l_notify]');
       return [form.clientWidth,label.offsetWidth,notify.offsetWidth,form.scrollWidth];
     })()`);
     assert(widths[1]>=widths[0]-36 && widths[2]>=widths[0]-36 && widths[3]<=widths[0]+1,JSON.stringify(widths));
     await shot('01-progress-editor');
     await editForm('[name=l_label]','Booking status');await editForm('[name=l_levels]','Enquiry, Confirmed');
     await checkSave(true);await editForm('[name=l_label]','');await checkSave(false);
     await evaluate("document.querySelector('.remove-ladder').click();document.querySelector('#add-group').click()");
     await checkSave(false);await editForm('[name=g_label]','Item');await editForm('[name=f_label]','Reference');
     await checkSave(true);
     await shot('01-record-type-editor');
     await evaluate("document.querySelector('.remove-group').click()");
     await checkSave(false);
   }
   await type('#bc-input',area==='records'?'Track Items with an optional text Reference, remembered once. It never blocks a booking.':
      'No VAT, no business details printed; payment terms On completion, quotes valid 30 days. Prices exclude VAT.');
   await key('Enter','Enter',13);await until('document.querySelector(".bc-setup-tray")?.innerText.includes("Accept changes")');
   await shot('02-proposed');
   if(area==='records'){
     assert(await evaluate('!document.querySelector(".bc-setup-tray .bc-setup-problem") && document.querySelector("#add-group.guided-target")'));
     const cards=await(await fetch(`http://127.0.0.1:${appPort}/concierge/setup-cards`)).json();
     const card=cards.cards.find(c=>c.page==='dependants'),line=card.lines.find(l=>l.name==='field');
     const selector='[data-line-id="'+line.id+'"]';
     await evaluate(`document.querySelector(${JSON.stringify(selector)}).querySelectorAll('button').forEach(b=>{if(b.textContent==='Edit')b.dataset.nativeTarget=''})`);
     await evaluate('document.querySelector("[data-native-target]").scrollIntoView({block:"center"})');
     await click('[data-native-target]');await type('[data-field=label]','Owner reference');await button('Keep changes');
     await until('document.querySelector(".bc-setup-tray").innerText.includes("Owner reference")');
     await until('document.querySelector("[data-pending-rows~=group]")?.innerText.includes("Owner reference")');
   }else{
     await type('[name=payment_terms]','After completion');await key('Tab','Tab',9);
     await until('document.querySelector(".bc-setup-tray").innerText.includes("After completion")');
   }
   await shot('03-edited');
   await evaluate("window.nativeBeforeAccept='old-document'");await button('Accept changes');
   await until('window.nativeBeforeAccept!=="old-document" && window.Blam8DeviceSync?.isReady() && document.querySelector(".guided-question")?.innerText.includes("were saved")');
   await shot('04-saved');
   assert(await evaluate('document.documentElement.scrollWidth<=innerWidth+1 && document.getElementById("bc-input").getBoundingClientRect().bottom<=innerHeight+1'));
   if(area==='records'){
     const saveRow = "document.querySelector('#dep-form [type=submit]')";
     await until(saveRow+'.getClientRects().length===0');
     await evaluate("document.querySelector('.remove-group').click()");
     await until(saveRow+'.getClientRects().length===1');
   }
   console.log('Native '+area+' question, existing card edit, exact Accept and saved receipt passed');
 }else{
 const composerNext='document.querySelector("#bc-form .guided-composer-next")?.checkVisibility()';
 const composerSend='document.querySelector("#bc-send").checkVisibility()';
 await until(composerNext+' && !'+composerSend);
 assert(!await evaluate('[...document.querySelectorAll(".guided-navigation button")].some(b=>b.textContent==="Next")'));
 await type('#bc-input','A draft');
 assert(await evaluate(composerSend+' && !'+composerNext));
 await key('a','KeyA',65,2);await key('Backspace','Backspace',8);
 assert(await evaluate(composerNext+' && !'+composerSend));
 await type('#bc-input','  ');await key('Enter','Enter',13);
 assert(await evaluate(composerNext+' && Blam8GuidedSetup.currentStop()==="diary_settings/min_notice_hours"'));
 await type('#bc-input','We need 24 hours notice and allow bookings 60 days ahead.');await click('#bc-send');
 await until(composerNext+' && !'+composerSend);
 await until('document.querySelector("[name=horizon_days]")?.value==="60" && document.querySelectorAll("[data-proposed]").length===2 && document.querySelector(".bc-setup-tray")?.textContent.includes("Accept changes")');
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
 assert(await evaluate(`![...document.querySelectorAll('.guided-navigation button')].some(b=>b.textContent==='Back')`));
 await click('#bc-close');await until('!document.body.classList.contains("guided-running")');
 await click('#bc-bubble');await button('Resume guided setup');await until('document.body.classList.contains("guided-running") && document.querySelectorAll("[data-proposed]").length===2');
 const trusted=await evaluate('window.trustedEvents.length>=7 && window.trustedEvents.every(Boolean)');assert(trusted);
 await evaluate("window.nativeBeforeAccept='old-document'");
 await button('Accept changes');
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
