/* Native forms can display a proposal without acquiring an unsaved setting.
 * All serialization consumers use the pre-projection values. Native submission
 * is blocked while a proposal owns any control in that form. No autosave path.
 */
(function () {
  'use strict';
  var owned = new Map(), baselines = new WeakMap(), quiet = false;
  function value(el) { return {value:el.value, checked:el.checked, disabled:el.disabled,
    readOnly:el.readOnly, description:el.getAttribute('aria-describedby')}; }
  function put(el, v) { el.value=v.value; if ('checked' in v) el.checked=v.checked; }
  function ordinary(form, work) {
    var shown=[];
    var previousQuiet=quiet;
    quiet=true;
    try {
      owned.forEach(function (p, el) { if(el.form===form){shown.push([el,value(el)]);put(el,p.original);el.disabled=p.original.disabled;} });
      return work();
    } finally { shown.forEach(function(pair){put(pair[0],pair[1]);pair[0].disabled=pair[1].disabled;});quiet=previousQuiet; }
  }
  function data(form) { return ordinary(form,function(){return new FormData(form);}); }
  function signature(form) { return Array.from(data(form)).filter(function(p){return !/^(_sync_|csrf_token)/.test(p[0]);})
    .map(function(p){return p[0]+'='+String(p[1]);}).join('&'); }
  function scan() { document.querySelectorAll('main form[method="post"]').forEach(function(f){if(!baselines.has(f))baselines.set(f,signature(f));}); }
  function dirty(form) { return baselines.has(form) && signature(form)!==baselines.get(form); }
  function anyDirty() { return Array.from(document.querySelectorAll('main form[method="post"]')).some(dirty); }
  function clear() {
    quiet=true;
    owned.forEach(function(p,el){put(el,p.original);el.disabled=p.original.disabled;el.readOnly=p.original.readOnly;
      if(p.original.description===null)el.removeAttribute('aria-describedby');else el.setAttribute('aria-describedby',p.original.description);
      el.removeAttribute('data-proposed');if(p.note)p.note.remove();});
    owned.clear();
    document.querySelectorAll('main form').forEach(function(f){f.dispatchEvent(new Event('change',{bubbles:true}));});
    quiet=false;
  }
  function project(el, proposed, info) {
    if (!el || !el.form || el.type==='hidden' || el.type==='file') return false;
    if (dirty(el.form) || owned.has(el)) return false;
    var original=value(el), note=document.createElement('span');
    note.id='proposal-'+info.id+'-'+info.field+'-'+owned.size; note.className='guided-proposed-note';
    note.textContent='Proposed — not saved. ';note.setAttribute('role','note');
    var edit=document.createElement('button');edit.type='button';edit.textContent='Edit suggestion';
    edit.addEventListener('click',info.edit);note.appendChild(edit);
    el.insertAdjacentElement('afterend',note);
    el.setAttribute('aria-describedby',((original.description||'')+' '+note.id).trim());
    el.setAttribute('data-proposed','');
    if(el.type==='checkbox')el.checked=!!proposed;
    else el.value=proposed==null?'':Array.isArray(proposed)?proposed.join('\n'):String(proposed);
    var item={original:original,note:note,info:info};owned.set(el,item);
    // Number/text values have native, single-control meanings. Compound choices
    // and selects are reviewed in the existing typed card editor instead.
    if(el.tagName==='SELECT' || el.type==='checkbox' || el.type==='radio')el.disabled=true;
    else {
      el.addEventListener('change',function(){
        if(quiet || owned.get(el)!==item)return;
        var raw=el.value, next=(el.type==='number' && raw!=='')?Number(raw):raw;
        info.change(next, raw);
      },{once:true});
    }
    return true;
  }
  function projectNamed(controls, proposed, info) {
    controls=Array.from(controls).filter(function(e){return e.type!=='hidden';});
    if(!controls.length)return false;
    if(controls.length===1)return project(controls[0],proposed,info);
    // A checkbox collection represents a set, not a truthy scalar. Reject
    // ambiguous duplicate names rather than painting a misleading preview.
    if(!controls.every(function(e){return e.type==='checkbox'&&e.form===controls[0].form;}))return false;
    var choices=Array.isArray(proposed)?proposed:typeof proposed==='string'?proposed.split(',').map(function(v){return v.trim();}):null;
    if(!choices||choices.some(function(v){return !controls.some(function(e){return e.value===String(v);});})||dirty(controls[0].form))return false;
    var done=controls.every(function(e){return project(e,choices.map(String).indexOf(e.value)!==-1,info);});
    if(done){
      var first=owned.get(controls[0]).note;
      controls.slice(1).forEach(function(e){var p=owned.get(e);p.note.remove();p.note=null;
        e.setAttribute('aria-describedby',((p.original.description||'')+' '+first.id).trim());});
      var label=controls[0].closest('label'),group=label&&label.parentElement;
      if(group&&controls.every(function(e){return group.contains(e);}))group.insertAdjacentElement('afterend',first);
    }
    return done;
  }
  function restoreField(form,name,values) {
    var projected=Array.from(owned.entries()).filter(function(p){return p[0].form===form && p[0].name===name;});
    if(!projected.length)return false;
    var originals=projected.filter(function(p){return !/^(checkbox|radio)$/.test(p[0].type)||p[1].original.checked;}).map(function(p){return p[1].original.value;});
    if(JSON.stringify(originals)===JSON.stringify(values))return true;
    // A follower restored a real local draft, not an approved proposal. Drop the
    // projection and retain those edits; the coordinator asks for fresh review.
    clear();window.dispatchEvent(new CustomEvent('guided:projection-conflict'));
    return false;
  }
  document.addEventListener('submit',function(e){
    if(!Array.from(owned.keys()).some(function(el){return el.form===e.target;}))return;
    e.preventDefault();e.stopImmediatePropagation();
    window.dispatchEvent(new CustomEvent('guided:submit-blocked'));
  },true);
  document.addEventListener('dirty-save:reset',function(e){if(e.target.tagName==='FORM')baselines.set(e.target,signature(e.target));},true);
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',scan);else scan();
  window.Blam8ProposalForms={data:data,ordinary:ordinary,project:project,projectNamed:projectNamed,clear:clear,restoreField:restoreField,
    dirty:dirty,anyDirty:anyDirty,isQuiet:function(){return quiet;},owns:function(el){return owned.has(el);}};
})();
