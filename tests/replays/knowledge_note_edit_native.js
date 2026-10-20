(async()=>{
  const out=document.getElementById('result'), f=window.fixture;
  const pause=()=>new Promise(r=>setTimeout(r,0));
  const until=async(check)=>{for(let n=0;n<100;n++){if(check())return;await pause();}throw Error('Timed out: '+check);};
  const assert=(value,message)=>{if(!value)throw Error(message);};
  try{
    let form=document.querySelector('form'),checkbox=form.elements[f.matchName];
    assert(checkbox.checked && checkbox.value==='keep_both','Fixture must start with a real approved overlap');
    assert(form.elements.action_one.value==='keep','Fixture must start with prior keep');
    const local=form.elements.content_two.value;
    await Blam8KnowledgeSourceReview.ask(f.request,form);
    assert(form.elements.content_one.value.includes('previous reference'),'Preview must not change wording');
    const preview=form.querySelector('[data-source-preview]'), apply=form.querySelector('[data-source-apply]');
    assert(form.querySelector('[data-source-corrections]').open,'Assistant must open its preview disclosure');
    assert(preview.textContent.includes('Your exact reason: '+f.request),'Source exclusion reason must be visible before Apply');
    assert(preview.getClientRects().length && apply.getClientRects().length,'Before/after and Apply must actually be visible');
    assert(document.activeElement===preview && window.closedOverlay===true,'Preview focus must be reachable outside the mobile assistant overlay');
    window.nativeEditReady=true;
    await new Promise(resolve=>document.addEventListener('click',function clicked(event){if(event.target.closest('[data-source-apply]')){assert(event.isTrusted,'Apply must use trusted native input');document.removeEventListener('click',clicked);resolve();}}));
    await until(()=>!Blam8KnowledgeSourceReview.working());
    assert(!checkbox.checked && checkbox.value==='keep_both','Apply must clear checked state without rewriting submission value');
    assert(new FormData(form).get(f.matchName)===null,'Cleared checkbox must not submit');
    assert(form.elements.action_one.value==='','Edit must reopen the note decision');
    assert(form.elements.content_two.value===local,'Unrelated local text must survive Apply');
    form.requestSubmit(form.querySelector('button[value=preview]'));
    await until(()=>document.querySelector('form').elements.version.value==='v2');
    form=document.querySelector('form');
    assert(form.elements.content_two.value===local,'Check must preserve unrelated local text');
    await Blam8KnowledgeGuide.ask('Show the current note',form);
    await Blam8KnowledgeGuide.ask('Keep this note',form);
    assert(form.elements.action_one.value==='keep','Explicit current-note confirmation must still work');
    checkbox=form.querySelector('input[type=checkbox]');
    assert(!checkbox.checked && checkbox.value==='keep_both','Fresh overlap still requires deliberate recheck');
    checkbox.click();
    assert(checkbox.checked && new FormData(form).get(checkbox.name)==='keep_both','Native recheck must submit configured keep_both value');
    out.dataset.status='PASS';out.textContent=JSON.stringify({requests:window.requests,form:Object.fromEntries(new FormData(form))});
  }catch(error){out.dataset.status='FAIL';out.textContent=error.stack;}
})();