/* Concierge prepares the existing review form. Only its normal Save admits notes. */
(function () {
  'use strict';
  var selected = null, focus = null, busy = false, greeted = null, sourceWorking = function(){return false;};
  function forms() { return Array.from(document.querySelectorAll('[data-knowledge-review]')).filter(function(f){return f.dataset.reviewClosed!=='1';}); }
  function form() {
    var all = forms();
    return all.find(function(f){return f.dataset.knowledgeReview===selected;}) || (all.length===1 ? all[0] : null);
  }
  function values(f) {
    var result = {};
    new FormData(f).forEach(function(value, key){if(typeof value==='string' && key!=='csrf_token' && key!=='operation')result[key]=value;});
    return result;
  }
  function ready() { window.dispatchEvent(new Event('blam8:onboarding-readiness')); }
  async function ask(question) {
    var f = form();
    if(!f) throw new Error('Choose the batch of notes you want to review with the Assistant.');
    if(busy || window.blam8KnowledgeImportBusy || sourceWorking())throw new Error('Please wait for the current review to finish.');
    var base=values(f), signature=JSON.stringify(base), key=f.dataset.knowledgeReview;
    busy=true; window.blam8KnowledgeImportBusy=true; ready();
    try {
      var response=await fetch(f.action+'/guide', {method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':f.elements.csrf_token.value},
        body:JSON.stringify({form:base,question:question,focus_hash:focus && focus.key===key ? focus.hash : ''})});
      var data;
      try { data=await response.json(); }
      catch (_) { throw new Error('The review service returned an unreadable response. Your choices remain on the page; ask to show the current note to recover.'); }
      if(!response.ok)throw new Error(typeof data.detail==='string' ? data.detail : 'The review could not be checked. Nothing has been saved.');
      if(form()!==f || JSON.stringify(values(f))!==signature)throw new Error('The review changed while I was checking. Your edits remain; ask to see the current note.');
      if(data.correction_requested) {
        // Release this read-only turn before the existing source correction tool.
        busy=false; window.blam8KnowledgeImportBusy=false; ready();
        focus=null;
        return await window.Blam8KnowledgeGuide.sourceAsk(question);
      }
      Object.keys(data.patches || {}).forEach(function(name){
        var field=f.elements[name];
        if(!field && (name.indexOf('guided_unknown_')===0 || name.indexOf('guided_expand_')===0)){field=document.createElement('input');field.type='hidden';field.name=name;f.appendChild(field);}
        if(!field)throw new Error('The review controls changed. Check changes and overlaps before trying again.');
        if(field.type==='checkbox')field.checked=data.patches[name]===field.value;
        else field.value=data.patches[name];
        field.dispatchEvent(new Event('change',{bubbles:true}));
      });
      focus={key:key,hash:data.focus_hash,form:f,signature:JSON.stringify(values(f)),saveReady:!!data.save_ready};
      if(!question.trim() && data.shared_caveats && data.shared_caveats.length) {
        data.reply='We will review the actual notes together. '+data.shared_caveats[0]+' All '+data.shared_caveats.length+' source caveats stay attached to retained notes; you can inspect the full list with the notes.\n\n'+data.reply;
      }
      data.guide_actions=data.actions || [];
      data.guide_save=!!data.save_ready;
      var item=data.focus || {}, section=item.kind==='note' ? f.querySelector('[data-draft="'+item.id+'"]') : null;
      var disclosure=section && (section.tagName==='DETAILS' ? section : section.querySelector('[data-note-details]'));
      if(disclosure)disclosure.open=true;
      return data;
    } catch (err) {
      if(question.trim() && !['', 'show the current note', 'start reviewing the notes', 'show the next decision'].includes(question.trim().toLowerCase()) &&
          form()===f && JSON.stringify(values(f))===signature) {
        busy=false; window.blam8KnowledgeImportBusy=false; ready(); focus=null;
        try {
          var recovered=await ask('Show the current note');
          recovered.reply=(err.message || 'The review could not be checked.')+'\n\n'+recovered.reply;
          recovered.recovered_after_error=true;
          return recovered;
        } catch (_) { /* Keep the original error and local choices visible. */ }
      }
      throw err;
    } finally { busy=false; window.blam8KnowledgeImportBusy=false; ready(); }
  }
  function maybeStart() {
    var f=form();
    if(!f || busy || window.blam8KnowledgeImportBusy || !window.blam8KnowledgeGuideStart)return;
    var key=f.dataset.knowledgeReview;
    if(greeted===key)return;
    greeted=key;
    window.blam8KnowledgeGuideStart().then(function(ok){if(!ok)greeted=null;});
  }
  window.Blam8KnowledgeGuide={ask:ask,working:function(){return busy;},available:function(){return !!form();},
    isCurrent:function(hash){var f=form();return !!(f && focus && focus.form===f && focus.hash===hash && focus.key===f.dataset.knowledgeReview && focus.signature===JSON.stringify(values(f)));},
    edit:function(hash,id){
      var f=form();
      if(!f || !id || !window.Blam8KnowledgeGuide.isCurrent(hash))return false;
      var section=Array.from(f.querySelectorAll('[data-draft]')).find(function(node){return node.dataset.draft===id;});
      var field=f.elements['content_'+id];
      if(!section || !field)return false;
      var disclosure=section.tagName==='DETAILS' ? section : section.querySelector('[data-note-details]');
      if(disclosure)disclosure.open=true;
      if(window.blam8ConciergeClose)window.blam8ConciergeClose();
      if(section.scrollIntoView)section.scrollIntoView({behavior:'smooth',block:'center'});
      field.focus();
      return true;
    },
    save:function(){var f=form();if(!f || busy || !focus || !focus.saveReady || !window.Blam8KnowledgeGuide.isCurrent(focus.hash))return;
      var button=f.querySelector('button[name="operation"][value="save"]');if(button && !button.disabled)f.requestSubmit(button);},
    start:maybeStart};
  document.addEventListener('click',function(e){var button=e.target.closest('[data-source-select]');if(button){selected=button.closest('form').dataset.knowledgeReview;focus=null;greeted=null;maybeStart();}});
  document.addEventListener('blam8:site-read',maybeStart);
  window.addEventListener('blam8:onboarding-readiness',maybeStart);
  document.addEventListener('DOMContentLoaded',function(){
    if(window.Blam8KnowledgeSourceReview){
      window.Blam8KnowledgeGuide.sourceAsk=window.Blam8KnowledgeSourceReview.ask;
      sourceWorking=window.Blam8KnowledgeSourceReview.working;
      window.Blam8KnowledgeSourceReview.working=function(){return busy || sourceWorking();};
    }
    maybeStart();
  });
})();
