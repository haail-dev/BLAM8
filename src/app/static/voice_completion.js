/* The server supplies the shared required-field contract. Placeholders are never values. */
(function () {
  'use strict';
  var contract=document.getElementById('voice-required'), form=document.getElementById('voice-form');
  if(!contract || !form)return;
  var required=JSON.parse(contract.textContent), saving=false, failure='';
  function read(){
    var missing=Object.keys(required).filter(function(key){var field=form.elements[key];return !field || !field.value.trim();});
    return {valid:!missing.length, saving:saving, reason:saving?'Saving Voice…':missing.length?'Choose '+missing.map(function(key){return required[key];}).join(', ')+'.':failure || 'Ready to save. Extra voice notes are optional.'};
  }
  function refresh(){
    var state=read(), status=document.getElementById('voice-completion-status');
    document.querySelectorAll('[data-onb-continue]').forEach(function(button){button.disabled=!state.valid || saving;button.setAttribute('aria-describedby','voice-completion-status');});
    if(status)status.textContent=state.reason;
    window.dispatchEvent(new Event('blam8:onboarding-readiness'));
  }
  window.Blam8VoiceCompletion={read:read};
  form.addEventListener('input',function(){failure='';refresh();});
  document.addEventListener('submit',async function(event){
    var target=event.target;
    if(target!==form && !target.matches('[data-voice-accept]'))return;
    event.preventDefault();
    if(!read().valid || saving){refresh();return;}
    var body=new FormData(target);
    saving=true;failure='';refresh();
    try{
      var response=await (window.Blam8DeviceSync ? window.Blam8DeviceSync.request : fetch)(target.action,{method:'POST',body:body});
      if(!response.ok || !response.redirected || new URL(response.url).pathname!=='/onboarding/knowledge')throw new Error('Voice could not be saved. Your entries are still here. Try Continue again.');
      window.location.assign(response.url);
    }catch(error){failure=error.message || 'Voice could not be saved. Your entries are still here.';saving=false;refresh();}
  });
  refresh();
})();
