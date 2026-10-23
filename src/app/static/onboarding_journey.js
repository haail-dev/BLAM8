// The model may suggest fields. Only application state decides action availability.
(function () {
  function availability(state) {
    if (!state.action) return {ready:false, reason:'No Continue action is available on this page.'};
    if (state.busy) return {ready:false, blocker:'busy', reason:'Wait for the current operation to finish.'};
    if (state.pendingCards) return {ready:false, reason:'Save or clear the pending notes card before continuing.'};
    if (state.step === 'knowledge') {
      if (state.savedKnowledge === false) return {ready:false, blocker:'knowledge_required', reason:'Review and save your Knowledge before continuing.'};
      if (state.unsavedNote) return {ready:false, reason:'Your pasted note is unsaved. Review it or clear it before continuing.'};
      if (state.pendingReviews) return {ready:false, blocker:'pending_reviews', reason:'Review and save every pending draft before continuing.'};
    }
    if (!state.valid) return {ready:false, reason:'Complete the required form fields before continuing.'};
    return {ready:true, reason:''};
  }
  function descriptor() {
    var el=document.getElementById('onboarding-journey');
    try { return el ? JSON.parse(el.textContent) : null; } catch(e) { return null; }
  }
  function read(pendingCards) {
    var journey=descriptor(), action=Array.from(document.querySelectorAll('[data-onb-continue]'))
      .find(function(el){return el.offsetParent !== null;});
    var form=action && action.form;
    // Inspect validity without reporting errors or dispatching invalid events.
    var valid=!form || action.formNoValidate || Array.from(form.elements).every(function(el){return !el.willValidate || el.validity.valid;});
    var note=document.querySelector('#kb-note [name="content"]');
    var sourceReview=window.Blam8KnowledgeSourceReview;
    var voice=journey && journey.current==='voice' && window.Blam8VoiceCompletion ? window.Blam8VoiceCompletion.read() : null;
    if(voice && (!voice.valid || voice.saving))return {journey:journey,action:action,ready:false,reason:voice.reason,blocker:voice.saving?'busy':'required'};
    var state=availability({step:journey && journey.current, action:!!action,
      busy:!!(action && action.disabled && (!journey || journey.current !== 'knowledge') || window.blam8KnowledgeImportBusy || sourceReview && sourceReview.working()),
      pendingCards:pendingCards || 0, valid:valid, unsavedNote:!!(note && note.value.trim()),
      savedKnowledge:!!document.querySelector('[data-knowledge-saved-status]'),
      pendingReviews:document.querySelectorAll('[data-knowledge-review]').length});
    return {journey:journey, action:action, ready:state.ready, reason:state.reason, blocker:state.blocker};
  }
  window.Blam8OnboardingJourney={availability:availability, descriptor:descriptor, read:read};
})();
