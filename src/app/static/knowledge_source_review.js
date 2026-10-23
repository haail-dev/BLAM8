/* Source corrections never admit Knowledge. The existing review form does that. */
(function () {
  "use strict";
  var busy = false, submitting = false, preview = null, locked = [], selected = null;
  function forms() { return Array.from(document.querySelectorAll('[data-knowledge-review]')); }
  function targetForm() {
    var eligible = forms().filter(function(f){return f.dataset.reviewClosed!=='1' && f.querySelector('[data-source-corrections]');});
    return eligible.find(function(f){return f.dataset.knowledgeReview===selected;}) || (eligible.length===1 ? eligible[0] : null);
  }
  function say(form, text) { form.querySelector('[data-source-status]').textContent = text; }
  function node(tag, text) { var el = document.createElement(tag); el.textContent = text; return el; }
  function snapshot(form) {
    var edits = {};
    form.querySelectorAll('[data-draft]').forEach(function (section) {
      var id = section.dataset.draft;
      edits[id] = {label: form.elements['label_' + id].value, content: form.elements['content_' + id].value};
    });
    var local = {version:form.elements.version.value};
    Object.keys(edits).forEach(function(id){local['label_'+id]=edits[id].label;local['content_'+id]=edits[id].content;});
    form.querySelectorAll('input,textarea,select').forEach(function(el){
      if(el.name && el.name!=='csrf_token' && el.name!=='operation' &&
          !((el.type==='checkbox' || el.type==='radio') && !el.checked))local[el.name]=el.value;
    });
    var orderedEdits={}, orderedLocal={};
    Object.keys(edits).sort().forEach(function(id){orderedEdits[id]=edits[id];});
    Object.keys(local).sort().forEach(function(name){orderedLocal[name]=local[name];});
    return {version: form.elements.version.value, edits: orderedEdits, form:orderedLocal};
  }
  function lock(on) {
    busy = on;
    if (on) {
      locked = Array.from(document.querySelectorAll('[data-knowledge-review] input,[data-knowledge-review] textarea,[data-knowledge-review] select,[data-knowledge-review] button,#kb-note button,form[action="/onboarding/knowledge/upload"] button,#site-go,[data-onb-continue],[data-leave-knowledge]'));
      locked.forEach(function (el) { el.dataset.sourceWasDisabled = el.disabled ? '1' : '0'; el.disabled = true; });
    } else {
      locked.forEach(function (el) { el.disabled = el.dataset.sourceWasDisabled === '1'; delete el.dataset.sourceWasDisabled; });
      locked = [];
    }
    window.dispatchEvent(new Event('blam8:onboarding-readiness'));
  }
  async function request(form, suffix, body) {
    var response = await (window.Blam8DeviceSync ? window.Blam8DeviceSync.request : fetch)(form.action + '/corrections' + suffix, {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': form.elements.csrf_token.value},
      body: JSON.stringify(body)
    });
    var data;
    try { data = await response.json(); }
    catch (_) { throw new Error('The source review service returned an unreadable response. Your edits remain on the page; check the current note before retrying.'); }
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'The operation could not be confirmed. Reload before retrying.');
    return data;
  }
  function clear(form) { preview = null; form.querySelector('[data-source-preview]').replaceChildren(); }
  async function ask(form, question, scope) {
    if (!form) throw new Error('Choose the pending batch with Use this batch with Assistant first.');
    if (busy || submitting || window.blam8KnowledgeImportBusy) throw new Error('Wait for the current review operation.');
    if (!question.trim()) throw new Error('Say what to check against the source.');
    clear(form);
    var base = snapshot(form), box = form.querySelector('[data-source-preview]');
    lock(true); say(form, 'Checking the retained source and these exact drafts…');
    try {
      var data = await request(form, '', Object.assign({question: question}, base, scope || {}));
      preview = {key:form.dataset.knowledgeReview, base: base, review: data.review, question: question, scope: scope || {}};
      data.changes.forEach(function (change) {
        var detail = node('details', ''); detail.open = true;
        detail.appendChild(node('summary', change.label));
        detail.appendChild(node('p', 'Original: ' + change.before));
        detail.appendChild(node('p', 'Proposed: ' + change.after));
        var evidence=node('details','');evidence.appendChild(node('summary','Why this changes · source details'));
        (change.owner_replacements || []).forEach(function(p){detail.appendChild(node('p','Owner-provided replacement: '+p.before+' → '+p.after+'. This new wording is supplied by you, not verified by the website.'));});
        (change.removals || []).forEach(function(p){detail.appendChild(node('blockquote','Remove this exact paragraph: '+p.quote));});
        (change.source_choices || []).forEach(function(choice){
          if(choice.action==='excluded')detail.appendChild(node('blockquote','Exclude this source fact: '+choice.text));
          evidence.appendChild(node('p','Source passage: '+choice.text));
          evidence.appendChild(node('p','Source: '+choice.sources.join(', ')));
          detail.appendChild(node('p',choice.action==='superseded'?'Superseded by your correction: keep this source passage in the audit, outside active Knowledge.':choice.action==='excluded'?'Removed factual claim: this will be excluded from all active notes if you Apply and then Save. Review the full claim below; this is not merely a formatting change.':'Retained in: '+choice.destinations.join(', ')));
          if(choice.reason)detail.appendChild(node('p','Your exact reason: '+choice.reason));
        });
        change.passages.forEach(function (p) { evidence.appendChild(node('blockquote', p.quote)); evidence.appendChild(node('p', p.url)); });
        (change.citations || []).forEach(function(p){evidence.appendChild(node('p','Citation for existing wording: '+p.claim));evidence.appendChild(node('blockquote',p.quote));evidence.appendChild(node('p',p.url));});
        detail.appendChild(evidence);
        box.appendChild(detail);
      });
      data.overlaps.forEach(function (group) { box.appendChild(node('p', 'Possible overlap; kept separate: ' + group.map(function (n) { return n.label; }).join(', '))); });
      data.questions.forEach(function (q) { box.appendChild(node('p', 'Needs your answer: ' + q)); });
      var diagnostics=node('details','');diagnostics.appendChild(node('summary','Review details'));diagnostics.appendChild(node('p',data.summary));box.appendChild(diagnostics);
      if (data.changes.length) { var apply = node('button', data.changes.some(function(c){return (c.removals || []).length;}) ? 'Apply listed wording and source choices to draft' : 'Apply corrections to drafts'); apply.type = 'button'; apply.className = 'kr-apply bg-brand hover:bg-blue-700'; apply.dataset.sourceApply = '1'; box.appendChild(apply); }
      if(data.changes.length){
        var disclosure=form.querySelector('[data-source-corrections]');if(disclosure)disclosure.open=true;
        if(window.blam8ConciergeClose)window.blam8ConciergeClose();
        box.tabIndex=-1;
        if(box.scrollIntoView)box.scrollIntoView({behavior:'smooth',block:'center'});
        if(box.focus)box.focus({preventScroll:true});
      }
      say(form, data.presentation_summary || data.summary);
      return {reply: (data.presentation_summary || data.summary)+(data.changes.length?' Check the before and after wording, then Apply. I’ll recheck overlaps automatically; you still review the updated note before Save.':''), correction_preview_ids: data.changes.map(function(change){return change.id;}), form_fill: {}, ready_to_advance: false, notes: [], held_for: []};
    } catch (err) { say(form, err.message); throw err; }
    finally { lock(false); }
  }
  async function apply(form) {
    form = form || targetForm();
    if(!form)return;
    if (busy || submitting || window.blam8KnowledgeImportBusy) {
      say(form,'Another review operation is still active. Wait for it to finish; reload after preserving edits if it remains unconfirmed.'); return;
    }
    if(!preview || preview.key!==form.dataset.knowledgeReview){
      say(form,'There is no current correction preview for this batch. Ask to review the correction again. Nothing changed.'); return;
    }
    if (JSON.stringify(snapshot(form)) !== JSON.stringify(preview.base)) {
      clear(form); say(form, 'The draft text changed. Review it again before applying.'); return;
    }
    var body = Object.assign({review: preview.review, owner_request: preview.question}, preview.base, preview.scope);
    lock(true);
    var applyControl = form.querySelector('[data-source-apply]');
    if (applyControl) { applyControl.textContent = 'Applying corrections…'; applyControl.setAttribute('aria-busy', 'true'); }
    try {
      var data = await request(form, '/apply', body);
      // Preserve the stale form token until the authoritative recheck succeeds.
      if (data.version) form.dataset.syncVersion = data.version;
      data.entries.forEach(function (entry) {
        form.elements['label_' + entry.id].value = entry.label;
        form.elements['content_' + entry.id].value = entry.content;
      });
      Object.keys(data.patches || {}).forEach(function(name){
        var field=form.elements[name];
        if(!field){field=document.createElement('input');field.type='hidden';field.name=name;form.appendChild(field);}
        if(field.type==='checkbox')field.checked=data.patches[name]===field.value;
        else field.value=data.patches[name];
        if(field.dispatchEvent)field.dispatchEvent(new Event('change',{bubbles:true}));
      });
      form.dataset.reviewDirty = '1';
      clear(form); say(form, data.summary);
      // Rechecking is part of Apply, not another task for the owner. This does
      // not approve the changed note or admit any facts to saved Knowledge.
      lock(false);
      var workspace=window.Blam8KnowledgeReview;
      if(workspace && workspace.check){
        var updated=await workspace.check(form,data.version);
        if(updated){
          var first=data.entries[0];
          if(first)workspace.focusNote(updated,first.id);
          var status=updated.querySelector('[data-review-status]');
          if(status)status.textContent='Correction applied and checked. Review the updated note, then Save when you’ve finished.';
          if(first){var note=updated.querySelector('[data-draft="'+first.id+'"]');if(note && note.scrollIntoView)note.scrollIntoView({behavior:'smooth',block:'center'});}
        }else say(form,'Correction applied, but its recheck could not be confirmed. Use Check changes and overlaps before saving.');
      }
    } catch (err) { clear(form); say(form, err.message + ' Reload to check the outcome before retrying.'); }
    finally { lock(false); }
  }
  document.addEventListener('click', function (event) {
    var selectButton = event.target.closest('[data-source-select]');
    if (selectButton) { var selectedForm=selectButton.closest('form'); selected=selectedForm.dataset.knowledgeReview; say(selectedForm,'Assistant will review this pending batch. Your local edits are included; nothing saved.'); }
    var askButton = event.target.closest('[data-source-ask]'), applyButton = event.target.closest('[data-source-apply]');
    if (askButton) { var form = askButton.closest('form'); ask(form, form.querySelector('[data-source-question]').value).catch(function (err) { say(form, err.message); }); }
    if (applyButton) apply(applyButton.closest('form'));
    var next = event.target.closest('[data-onb-continue],[data-leave-knowledge]');
    if (!next || !document.querySelector('[data-knowledge-source-mode]')) return;
    var note = document.getElementById('kb-note'), later = document.querySelector('[data-knowledge-later]');
    var message = later.querySelector('[data-knowledge-navigation-status]');
    if (busy || submitting || window.blam8KnowledgeImportBusy || note.elements.content.value.trim() || (forms().length && !next.hasAttribute('data-leave-knowledge'))) {
      event.preventDefault(); event.stopImmediatePropagation(); later.hidden = false;
      message.textContent = busy || submitting || window.blam8KnowledgeImportBusy ? 'Wait for the current review operation.' : note.elements.content.value.trim() ? 'Your pasted note is unsaved. Choose Review note, or clear the box before continuing.' : 'Review the drafts or choose to leave them for later.';
      return;
    }
    if (next.hasAttribute('data-leave-knowledge')) {
      var choice = node('input', ''); choice.type = 'hidden'; choice.name = 'knowledge_draft_decision'; choice.value = 'keep'; note.appendChild(choice);
    }
  }, true);
  document.addEventListener('submit', function (event) {
    if (!event.target.matches('[data-knowledge-review],#kb-note,form[action="/onboarding/knowledge/upload"]')) return;
    if (event.defaultPrevented) return;
    if (busy || submitting || window.blam8KnowledgeImportBusy) { event.preventDefault(); event.stopImmediatePropagation(); return; }
    if (event.submitter && event.submitter.value === 'preview') return;
    submitting = true;
  }, true);
  window.addEventListener('pageshow', function () { submitting = false; });
  window.Blam8KnowledgeSourceReview = {
    working: function () { return !!(busy || submitting); },
    available: function () { return !!targetForm(); },
    ask: function (question, reviewForm, scope) {
      if(reviewForm){if(!forms().includes(reviewForm))throw new Error('That review batch is no longer available.');selected=reviewForm.dataset.knowledgeReview;}
      return ask(reviewForm || targetForm(), question, scope);
    }
  };
})();
