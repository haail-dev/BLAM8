// Server-rendered escaped fragments; refresh is never a correction or approval.
(function () {
  'use strict';
  var go=document.getElementById('site-go'), input=document.getElementById('site-url');
  var lifecycle=document.getElementById('site-lifecycle'), busy=false;
  var knownSite={pending_notes:lifecycle ? Number(lifecycle.dataset.pendingNotes || 0) : 0,review_href:lifecycle && lifecycle.dataset.reviewHref};
  var navigation=[];
  function reading(on){
    busy=on;window.blam8KnowledgeImportBusy=on;
    if(on){
      navigation=Array.from(document.querySelectorAll('[data-onb-continue],[data-leave-knowledge]')).map(function(el){
        var previous={el:el,disabled:el.disabled,text:el.textContent};
        el.disabled=true;el.textContent='Reading website… Please wait';return previous;
      });
    }else{
      navigation.forEach(function(previous){previous.el.disabled=previous.disabled;previous.el.textContent=previous.text;});navigation=[];
    }
    ready();
  }
  function ready(){window.dispatchEvent(new Event('blam8:onboarding-readiness'));}
  function restoring(){return window.Blam8DeviceSync && window.Blam8DeviceSync.isRestoring();}
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
    button.addEventListener('click',function(){old.replaceWith(next);syncNotes(next);compactShell();notice(next,'Stored version loaded by your choice. Review its decisions before saving.');ready();});
    detail.appendChild(button);old.prepend(detail);
  }
  function compactShell(){
    var holder=document.getElementById('knowledge-pending-reviews'),manual=document.getElementById('knowledge-manual-controls');
    if(!holder || !manual)return;
    var heading=document.querySelector('[data-onboarding-heading]'),helper=document.querySelector('[data-onboarding-helper]');
    if(!heading || !Array.from(holder.querySelectorAll('[data-knowledge-review]')).some(function(form){return form.dataset.reviewClosed!=='1';}))return;
    var savedStatus=document.querySelector('[data-knowledge-saved-status]');if(savedStatus)savedStatus.remove();
    heading.textContent='Review Knowledge';if(helper)helper.hidden=true;
    holder.after(manual); // Move the existing controls, preserving unsaved inputs.
    var coverage=document.getElementById('knowledge-coverage-shell');
    if(coverage){
      if(coverage.dataset.compactCoverage!=='1'){
        var panel=document.createElement('details'),summary=document.createElement('summary');summary.textContent='Knowledge coverage';panel.className='mb-3 text-xs text-slate-600';panel.appendChild(summary);
        while(coverage.firstChild)panel.appendChild(coverage.firstChild);
        coverage.appendChild(panel);coverage.dataset.compactCoverage='1';
      }
      manual.after(coverage);
    }
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
          var openNote=Array.from(old.querySelectorAll('[data-draft]')).find(function(section){var detail=section.querySelector('[data-note-details]');return detail && detail.open;});
          var opened=Array.from(old.querySelectorAll('details[open]')).map(function(d){return d.dataset.draft || d.dataset.sourceUnit;}).filter(Boolean);
          next.querySelectorAll('details').forEach(function(d){if(opened.includes(d.dataset.draft || d.dataset.sourceUnit))d.open=true;});
          old.replaceWith(next);if(openNote)focusNote(next,openNote.dataset.draft);
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
        var wasClosed=f.dataset.reviewClosed==='1';
        f.dataset.reviewClosed='1';notice(f,'This batch is no longer pending in stored reviews. Local text is preserved; reload after copying any unsaved edits.');
        f.querySelectorAll('button:not([data-review-dismiss])').forEach(function(b){b.disabled=true;});
        if(!wasClosed){
          var dismiss=document.createElement('button');dismiss.type='button';dismiss.dataset.reviewDismiss='1';
          dismiss.textContent='Dismiss this local copy after preserving my unsaved text';
          dismiss.addEventListener('click',function(){f.remove();ready();});f.appendChild(dismiss);
        }
      }});
    }
    var coverage=parser.parseFromString(data.coverage_html || '', 'text/html').getElementById('knowledge-review-coverage');
    var current=document.getElementById('knowledge-review-coverage');
    if(current && coverage)current.replaceWith(coverage);
    if(data.site_state)present(data.site_state);
    document.querySelectorAll('[data-knowledge-review]').forEach(syncNotes);compactShell();
    ready();
  }
  function present(state){
    if(!lifecycle)return;
    lifecycle.hidden=state.state==='saved' && !!document.querySelector('[data-knowledge-saved-status]');
    if(state.pending_notes!==undefined){knownSite.pending_notes=state.pending_notes;knownSite.review_href=state.review_href;}
    lifecycle.dataset.state=state.state;
    var title={idle:'Start from your website',reading:'Reading your website',pending:'Website drafts ready to review',saved:'Website notes saved',error:'Website read failed',unconfirmed:'Website read outcome unconfirmed'};
    lifecycle.querySelector('[data-site-heading]').textContent=title[state.state];
    lifecycle.querySelector('[data-site-description]').textContent=state.message || (state.state==='pending' ? state.pending_notes+' drafts pending. Nothing from these drafts is saved to Knowledge.' : state.state==='saved' ? state.saved_notes+' website notes saved to Knowledge.'+(state.clarification_notes?' '+state.clarification_notes+' separate clarification notes saved.':'') : 'Read key pages and prepare notes for your review.');
    var link=lifecycle.querySelector('[data-site-review]');
    link.hidden=!knownSite.pending_notes;link.style.display=knownSite.pending_notes?'':'none';link.textContent='Review '+knownSite.pending_notes+' drafts';link.href=knownSite.review_href || '#knowledge-pending-reviews';
    if(go){go.disabled=state.state==='reading';go.textContent=state.state==='reading'?'Reading…':state.state==='unconfirmed'?'Check stored drafts':state.state==='error'?'Try again':state.state==='idle'?'Read my site':'Read again';}
  }
  function noteSections(form){return Array.from(form.querySelectorAll('[data-draft]'));}
  function noteTool(section,tool){
    section.querySelectorAll('[data-note-tool]').forEach(function(button){button.setAttribute('aria-expanded',button.dataset.noteTool===tool?'true':'false');});
    section.querySelectorAll('[data-note-panel]').forEach(function(panel){panel.hidden=panel.dataset.notePanel!==tool;});
    var editing=tool==='edit';
    section.querySelectorAll('[data-note-preview],[data-note-label-preview]').forEach(function(preview){preview.hidden=editing;});
    var button=section.querySelector('[data-note-tool="edit"]');if(button)button.textContent=editing?'Done editing':'Edit';
    if(editing){var field=section.querySelector('textarea[name^="content_"]');growEditor(field);}
  }
  function growEditor(field){
    if(!field || field.closest('[hidden]'))return;
    field.style.height='auto';field.style.height=Math.min(field.scrollHeight+2,window.innerHeight*.6)+'px';
  }
  function focusNote(form,id,tool){
    if(!form || !id)return false;
    var found=false;
    noteSections(form).forEach(function(section){var detail=section.querySelector('[data-note-details]');if(detail){detail.open=section.dataset.draft===id;if(detail.open){found=true;if(tool)noteTool(section,tool);}}});
    return found;
  }
  function syncNotes(form){
    if(!form.dataset.reviewEnhanced){noteSections(form).forEach(function(section){if(section.querySelector('[data-note-status]'))noteTool(section,null);});form.dataset.reviewEnhanced='1';}
    var remaining=form.querySelector('[data-review-remaining]');if(remaining)remaining.textContent=noteSections(form).filter(function(section){return !form.elements['action_'+section.dataset.draft].value;}).length;
    var label=form.querySelector('[data-review-remaining-label]');if(label && remaining)label.textContent=Number(remaining.textContent)===1?'note remaining':'notes remaining';
    noteSections(form).forEach(function(section){
      var overlap=section.querySelector('[data-overlap-status]');if(overlap){var checks=Array.from(section.querySelectorAll('[data-duplicate-candidate] input[type="checkbox"]'));overlap.textContent=checks.length && checks.every(function(c){return c.checked;})?'Overlap reviewed':'Overlap to check';}
      var id=section.dataset.draft,status=section.querySelector('[data-note-status]');
      if(!status)return;
      var action=form.elements['action_'+id],later=form.elements['review_later_'+id];
      var value=action && action.value || '';
      if(value && later)later.value='';
      status.textContent=value==='keep'?'Reviewed · not saved':value?'Decision selected · not saved':later && later.value?'For later · pending':'Pending';
      status.style.color=value==='keep'?'#047857':'';
      var preview=section.querySelector('[data-note-preview]'),content=form.elements['content_'+id];
      if(preview && content)preview.textContent=content.value;
      var title=section.querySelector('[data-note-label-preview]'),heading=form.elements['label_'+id];
      if(title && heading)title.textContent=heading.value;
      if(content)growEditor(content);
      section.querySelectorAll('[data-note-reviewed],[data-note-later]').forEach(function(button){button.setAttribute('aria-pressed',button.hasAttribute('data-note-reviewed')?String(value==='keep'):String(!value && !!(later && later.value)));});
    });
  }
  document.addEventListener('toggle',function(event){
    var detail=event.target;
    if(!detail.matches || !detail.matches('[data-note-details]') || !detail.open)return;
    var section=detail.closest('[data-draft]'),form=detail.closest('[data-knowledge-review]');
    if(section && form)focusNote(form,section.dataset.draft);
  },true);
  function browseNext(form,section,deferred){
    var matches=Array.from(section.querySelectorAll('[data-duplicate-candidate] input[type="checkbox"]'));
    if(!deferred && matches.some(function(input){return !input.checked;})){noteTool(section,'more');return;}
    var all=noteSections(form),index=all.indexOf(section);
    var ordered=all.slice(index+1).concat(all.slice(0,index));
    var next=ordered.find(function(candidate){var id=candidate.dataset.draft,later=form.elements['review_later_'+id];return !form.elements['action_'+id].value && !(later && later.value);});
    if(next)focusNote(form,next.dataset.draft);
  }
  document.addEventListener('click',async function(event){
    var tool=event.target.closest('[data-note-tool]');
    if(tool){var section=tool.closest('[data-draft]');noteTool(section,tool.getAttribute('aria-expanded')==='true'?null:tool.dataset.noteTool);if(tool.dataset.noteTool==='edit' && tool.getAttribute('aria-expanded')==='true'){var heading=section.querySelector('input[name^="label_"]');if(heading)heading.focus();}return;}
    var button=event.target.closest('[data-note-reviewed],[data-note-later]');
    if(!button)return;
    var form=button.closest('[data-knowledge-review]'),section=button.closest('[data-draft]');
    if(!form || !section || form.dataset.reviewClosed==='1' || busy || window.blam8KnowledgeImportBusy || window.Blam8KnowledgeSourceReview && window.Blam8KnowledgeSourceReview.working())return;
    var id=section.dataset.draft,action=form.elements['action_'+id];
    if(!action)return;
    var later=form.elements['review_later_'+id];
    if(!later){later=document.createElement('input');later.type='hidden';later.name='review_later_'+id;form.appendChild(later);}
    var deferred=button.hasAttribute('data-note-later');
    action.value=deferred?'':'keep';later.value=deferred?'1':'';
    form.dataset.reviewDirty='1';
    action.dispatchEvent(new Event('change',{bubbles:true}));syncNotes(form);
    notice(form,deferred?'Left the last note pending for later. It has not been discarded; decide every draft before saving.':'Marked the last note reviewed to keep. Source and overlap decisions still apply; nothing saved.');
    var guide=window.Blam8KnowledgeGuide;
    if(guide && guide.available(form) && !guide.working()){
      button.disabled=true;
      try{
        var data=await guide.ask('Show the current note',form);
        if(window.blam8KnowledgeReviewChanged)window.blam8KnowledgeReviewChanged(data.reply || 'Review choice recorded. Save when you have finished.');
        var focus=data.focus || {};
        var next=focus.kind==='note'||focus.kind==='support'?focus.id:focus.note_id || focus.target;
        if(deferred && next===id){
          // A deferred note remains unresolved to the planner. Browsing another
          // draft does not pretend to approve or skip that pending decision.
          browseNext(form,section,true);
        }else if(next)focusNote(form,next,focus.kind==='overlap'?'more':focus.kind==='support'||focus.kind==='passage'?'source':null);
        if(!deferred && focus.kind!=='note' && focus.kind!=='ready')notice(form,'Note reviewed to keep. '+(focus.kind==='overlap'?'Review its possible overlap under More options.':'Source decisions are still pending; review Source and check changes before saving.'));
      }catch(error){notice(form,error.message || 'Your local choice remains. Show the current note with the Assistant to continue.');}
      finally{button.disabled=false;}
    }else browseNext(form,section,deferred);
    ready();
  });
  document.querySelectorAll('[data-knowledge-review]').forEach(syncNotes);compactShell();
  document.addEventListener('change',function(event){var form=event.target.closest('[data-knowledge-review]');if(form)syncNotes(form);});
  document.addEventListener('input',function(event){
    // Hydration replays input/change for native controls. It is not an owner
    // edit: the stored review stamps still bind decisions to their exact text.
    if(restoring())return;
    var form=event.target.closest('[data-knowledge-review]');if(!form)return;
    var name=event.target.name || '';
    if(name.startsWith('content_') || name.startsWith('label_')){
      var id=name.slice(name.indexOf('_')+1),action=form.elements['action_'+id],stamp=form.elements['reviewed_'+id];
      if(action && action.value){action.value='';if(stamp)stamp.value='';notice(form,'This note changed. Review its new wording before Save. Other note decisions remain.');}
    }
    syncNotes(form);
  });
  window.Blam8KnowledgeReview={replace:replace,signature:signature,focusNote:focusNote,syncNotes:syncNotes,noteTool:noteTool,check:checkReview};
  document.addEventListener('submit',async function(event){
    var form=event.target, button=event.submitter;
    if(!form.matches('[data-knowledge-review]') || !button || button.value!=='preview')return;
    if(event.defaultPrevented)return;
    event.preventDefault();
    await checkReview(form);
  },true);
  async function checkReview(form,expectedVersion){
    if(busy || window.blam8KnowledgeImportBusy || window.Blam8KnowledgeSourceReview && window.Blam8KnowledgeSourceReview.working())return null;
    var body=new FormData(form);body.set('operation','preview');
    if(expectedVersion)body.set('correction_recheck_version',expectedVersion);
    var controls=Array.from(form.querySelectorAll('input,textarea,select,button'));
    var disabled=controls.map(function(el){return el.disabled;});
    busy=true;window.blam8KnowledgeImportBusy=true;ready();
    controls.forEach(function(el){el.disabled=true;});notice(form,'Checking changes and overlaps… No facts are being saved.');
    try{
      var response=await (window.Blam8DeviceSync ? window.Blam8DeviceSync.request : fetch)(form.action,{method:'POST',headers:{Accept:'application/json'},body:body});
      var data=await response.json();if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:'The check failed. Your edits remain.');
      replace(data,form.dataset.knowledgeReview);
      var updated=document.querySelector('[data-knowledge-review="'+form.dataset.knowledgeReview+'"]');notice(updated,data.summary);
      return updated;
    }catch(e){notice(form,e.message || 'Check failed. Your local edits remain; no facts saved.');return null;}
    finally{controls.forEach(function(el,i){el.disabled=disabled[i];});busy=false;window.blam8KnowledgeImportBusy=false;ready();}
  }
  window.addEventListener('blam8:knowledge-review',async function(){
    try{var r=await fetch('/onboarding/knowledge/reviews',{cache:'no-store'});if(!r.ok)throw new Error();replace(await r.json());}
    catch(e){document.querySelectorAll('[data-knowledge-review]').forEach(function(f){notice(f,'Review refresh could not be confirmed. Your local edits remain; reload only after preserving them.');});}
  });
  document.addEventListener('input',function(e){if(restoring())return;var f=e.target.closest('[data-knowledge-review]');if(f)f.dataset.reviewDirty='1';ready();});
  document.addEventListener('change',function(e){if(restoring())return;var f=e.target.closest('[data-knowledge-review]');if(f)f.dataset.reviewDirty='1';ready();});
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
    reading(true);
    present({state:'reading',message:'Reading key pages and preparing pending drafts. Existing edits are preserved.'});ready();
    say('Reading website… Please wait before continuing. No facts are being saved.');
    var ok=false,received=false;
    try{
      var fd=new FormData();fd.append('url',input.value.trim());fd.append('csrf_token',document.getElementById('knowledge-review-csrf').value);
      var response=await (window.Blam8DeviceSync ? window.Blam8DeviceSync.request : fetch)('/onboarding/knowledge/website',{method:'POST',body:fd});
      var data=await response.json();received=true;
      if(!response.ok)throw new Error(data.error || data.detail || 'Could not read that site.');
      replace(data);ok=true;say('Read '+data.pages.length+' pages. Drafts are pending. Review capture limits and source gaps; no facts saved.');
    }catch(e){
      present({state:received?'error':'unconfirmed',message:received?e.message+' No new drafts were confirmed. Existing drafts remain.':'The response was not confirmed. Reload to check stored drafts before starting another read; preserve local edits first.'});
      say('Your existing review edits remain on this page.');
    }finally{reading(false);announce(ok);}
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
