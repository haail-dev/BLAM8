// Server-rendered escaped fragments; refresh is never a correction or approval.
(function () {
  'use strict';
  var go=document.getElementById('site-go'), input=document.getElementById('site-url');
  var lifecycle=document.getElementById('site-lifecycle'), busy=false;
  function ready(){window.dispatchEvent(new Event('blam8:onboarding-readiness'));}
  function notice(form,text){if(!form)return;var el=form.querySelector('[data-review-status]');if(el)el.textContent=text;}
  function signature(form){return ['contentVersion','evidenceVersion','contextVersion'].map(function(k){return form.dataset[k] || '';}).join('/');}
  function showStored(old,next){
    var previous=old.querySelector('[data-review-incoming]');if(previous)previous.remove();
    var detail=document.createElement('details');detail.dataset.reviewIncoming='1';detail.open=true;
    var title=document.createElement('summary');title.textContent='Stored version changed — compare before reconciling local edits';detail.appendChild(title);
    next.querySelectorAll('[data-draft]').forEach(function(d){
      var id=d.dataset.draft,p=document.createElement('p');
      p.textContent=next.elements['label_'+id].value+': '+next.elements['content_'+id].value;detail.appendChild(p);
    });
    var button=document.createElement('button');button.type='button';button.textContent='Use stored version and replace my local review edits';
    button.addEventListener('click',function(){old.replaceWith(next);notice(next,'Stored version loaded by your choice. Review its decisions before saving.');ready();});
    detail.appendChild(button);old.prepend(detail);
  }
  function replace(data, checkedId) {
    var parser=new DOMParser(), incoming=parser.parseFromString(data.review_html || '', 'text/html');
    var holder=document.getElementById('knowledge-pending-reviews');
    var oldForms=Array.from(document.querySelectorAll('[data-knowledge-review]'));
    incoming.querySelectorAll('[data-knowledge-review]').forEach(function(next){
      var old=oldForms.find(function(f){return f.dataset.knowledgeReview===next.dataset.knowledgeReview;});
      if(old){
        var changed=signature(old)!==signature(next);
        if(checkedId===old.dataset.knowledgeReview){
          var opened=Array.from(old.querySelectorAll('details[open]')).map(function(d){return d.dataset.draft || d.dataset.sourceUnit;}).filter(Boolean);
          next.querySelectorAll('details').forEach(function(d){if(opened.includes(d.dataset.draft || d.dataset.sourceUnit))d.open=true;});
          old.replaceWith(next);
        }
        else if(changed && old.dataset.reviewDirty!=='1'){
          old.replaceWith(next);notice(next,'Review updated from the stored drafts. Nothing saved to Knowledge.');
        }else{
          notice(old,changed?'Stored review or evidence changed. Your local edits are preserved; check changes and overlaps before saving.':'Review refreshed: no new evidence or draft changes. Your edits are preserved.');
          if(changed)showStored(old,next);
        }
      }else if(holder){holder.appendChild(next);}
    });
    if(!checkedId){
      // Nodes appended/replaced above leave the detached document, so derive
      // presence from the original response too, not the moved node inventory.
      var responseDoc=parser.parseFromString(data.review_html || '', 'text/html');
      var incomingIds=Array.from(responseDoc.querySelectorAll('[data-knowledge-review]')).map(function(f){return f.dataset.knowledgeReview;});
      oldForms.forEach(function(f){if(!incomingIds.includes(f.dataset.knowledgeReview)){
        f.dataset.reviewClosed='1';notice(f,'This batch is no longer pending in stored reviews. Local text is preserved; reload after copying any unsaved edits.');
        f.querySelectorAll('button').forEach(function(b){b.disabled=true;});
      }});
    }
    var coverage=parser.parseFromString(data.coverage_html || '', 'text/html').getElementById('knowledge-review-coverage');
    var current=document.getElementById('knowledge-review-coverage');
    if(current && coverage)current.replaceWith(coverage);
    if(data.site_state)present(data.site_state);
    ready();
  }
  function present(state){
    if(!lifecycle)return;
    lifecycle.dataset.state=state.state;
    var title={idle:'Start from your website',reading:'Reading your website',pending:'Website drafts ready to review',saved:'Website notes saved',error:'Website read failed',unconfirmed:'Website read outcome unconfirmed'};
    lifecycle.querySelector('[data-site-heading]').textContent=title[state.state];
    lifecycle.querySelector('[data-site-description]').textContent=state.message || (state.state==='pending' ? state.pending_notes+' drafts pending. Nothing from these drafts is saved to Knowledge.' : state.state==='saved' ? state.saved_notes+' notes saved to Knowledge.' : 'Read key pages and prepare notes for your review.');
    var link=lifecycle.querySelector('[data-site-review]');
    link.hidden=!state.pending_notes;link.textContent='Review '+(state.pending_notes || 0)+' drafts';link.href=state.review_href || '#knowledge-pending-reviews';
    if(go){go.disabled=state.state==='reading';go.textContent=state.state==='reading'?'Reading…':state.state==='unconfirmed'?'Check stored drafts':state.state==='error'?'Try again':state.state==='idle'?'Read my site':'Read again';}
  }
  window.Blam8KnowledgeReview={replace:replace,signature:signature};
  document.addEventListener('submit',async function(event){
    var form=event.target, button=event.submitter;
    if(!form.matches('[data-knowledge-review]') || !button || button.value!=='preview')return;
    if(event.defaultPrevented)return;
    event.preventDefault();
    if(busy || window.blam8KnowledgeImportBusy || window.Blam8KnowledgeSourceReview && window.Blam8KnowledgeSourceReview.working())return;
    var body=new FormData(form);body.set('operation','preview');
    var controls=Array.from(form.querySelectorAll('input,textarea,select,button'));
    var disabled=controls.map(function(el){return el.disabled;});
    busy=true;window.blam8KnowledgeImportBusy=true;ready();
    controls.forEach(function(el){el.disabled=true;});notice(form,'Checking changes and overlaps… No facts are being saved.');
    try{
      var response=await fetch(form.action,{method:'POST',headers:{Accept:'application/json'},body:body});
      var data=await response.json();if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:'The check failed. Your edits remain.');
      replace(data,form.dataset.knowledgeReview);
      var updated=document.querySelector('[data-knowledge-review="'+form.dataset.knowledgeReview+'"]');notice(updated,data.summary);
    }catch(e){notice(form,e.message || 'Check failed. Your local edits remain; no facts saved.');}
    finally{controls.forEach(function(el,i){el.disabled=disabled[i];});busy=false;window.blam8KnowledgeImportBusy=false;ready();}
  },true);
  window.addEventListener('blam8:knowledge-review',async function(){
    try{var r=await fetch('/onboarding/knowledge/reviews',{cache:'no-store'});if(!r.ok)throw new Error();replace(await r.json());}
    catch(e){document.querySelectorAll('[data-knowledge-review]').forEach(function(f){notice(f,'Review refresh could not be confirmed. Your local edits remain; reload only after preserving them.');});}
  });
  document.addEventListener('input',function(e){var f=e.target.closest('[data-knowledge-review]');if(f)f.dataset.reviewDirty='1';ready();});
  document.addEventListener('change',function(e){var f=e.target.closest('[data-knowledge-review]');if(f)f.dataset.reviewDirty='1';ready();});
  if(!go || !input)return;
  var status=document.getElementById('site-status'), box=document.getElementById('site-box');
  function say(text){status.hidden=false;status.textContent=text;}
  function announce(ok){window.blam8SiteRead={done:true,ok:ok};document.dispatchEvent(new CustomEvent('blam8:site-read',{detail:{ok:ok}}));ready();}
  async function read(){
    if(lifecycle && lifecycle.dataset.state==='unconfirmed'){
      try{var stored=await fetch('/onboarding/knowledge/reviews',{cache:'no-store'});if(!stored.ok)throw new Error();replace(await stored.json());say('Stored reviews checked. Compare any changed version before starting another read.');}
      catch(e){say('Stored drafts could not be checked. Your local edits remain; do not repeat the read yet.');}return;
    }
    if(busy || window.blam8KnowledgeImportBusy || (window.Blam8KnowledgeSourceReview && window.Blam8KnowledgeSourceReview.working()))return;
    if(!input.value.trim()){say('Paste your website address first.');return;}
    busy=true;window.blam8KnowledgeImportBusy=true;
    present({state:'reading',message:'Reading key pages and preparing pending drafts. Existing edits are preserved.'});ready();
    var ok=false,received=false;
    try{
      var fd=new FormData();fd.append('url',input.value.trim());fd.append('csrf_token',document.getElementById('knowledge-review-csrf').value);
      var response=await fetch('/onboarding/knowledge/website',{method:'POST',body:fd});
      var data=await response.json();received=true;
      if(!response.ok)throw new Error(data.error || data.detail || 'Could not read that site.');
      replace(data);ok=true;say('Read '+data.pages.length+' pages. Drafts are pending. Review capture limits and source gaps; no facts saved.');
    }catch(e){
      present({state:received?'error':'unconfirmed',message:received?e.message+' No new drafts were confirmed. Existing drafts remain.':'The response was not confirmed. Reload to check stored drafts before starting another read; preserve local edits first.'});
      say('Your existing review edits remain on this page.');
    }finally{busy=false;window.blam8KnowledgeImportBusy=false;announce(ok);}
  }
  go.addEventListener('click',read);
  input.addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();read();}});
  input.addEventListener('input',function(){if(!busy && lifecycle.dataset.state!=='unconfirmed')present({state:'idle',message:'Address changed. Read deliberately when ready; existing pending drafts remain below.'});});
  var again=document.getElementById('site-again');
  if(again)again.addEventListener('click',function(){box.hidden=false;document.getElementById('site-done').hidden=true;input.focus();});
  if(box.dataset.autoSite==='1' && input.value.trim() && (!lifecycle || lifecycle.dataset.state==='idle')){
    var stamp='blam8_site_autoread:'+box.dataset.scope+':'+input.value.trim(),attempted=false;
    try{attempted=sessionStorage.getItem(stamp)==='1';sessionStorage.setItem(stamp,'1');}catch(e){}
    if(attempted){present({state:'error',message:'A previous read was attempted in this tab. No completed website drafts are confirmed by this page. Review stored drafts before deliberately trying again.'});announce(false);}else read();
  }
})();
