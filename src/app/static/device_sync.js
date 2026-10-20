/* Authenticated workspaces. Events accelerate reads; durable polling recovers
 * missed events, sleeping phones, restarts and multiple web processes. */
(function () {
  'use strict';
  if (window.Blam8DeviceSync) return;
  var states = {}, queue = Promise.resolve(), adapter = null, applying = false;
  var initialized = false, dirty = false, conflict = false, timer, polling = false;
  var submissions = new WeakSet();
  var here = location.pathname, flow = here.indexOf('/onboarding/') === 0;
  var tuningPaths = ['/app/diary/settings','/app/diary/intake','/app/diary/dependants','/app/crm/settings','/app/invoices/settings'];
  var notice;
  function uuid() { return crypto.randomUUID(); }
  function csrf() { var m = document.cookie.match(/(?:^|;\s*)bl_csrf=([^;]+)/); return m ? decodeURIComponent(m[1]) : ''; }
  function warn(message) {
    if (!notice) { notice = document.createElement('div'); notice.setAttribute('role','status'); notice.className='fixed top-14 left-4 right-4 z-50 rounded-xl border border-amber-300 bg-amber-50 p-3 text-sm text-slate-800'; document.body.appendChild(notice); }
    notice.textContent=message;
  }
  function channel() { return flow ? 'flow' : 'assistant'; }
  function fields(form) {
    var values={};
    Array.from(form.elements).forEach(function(el){
      if (!el.name || /^(csrf_token|_sync_.*|version|operation)$/.test(el.name) || /^(file|password|submit|button)$/.test(el.type)) return;
      if (!values[el.name]) values[el.name]=[];
      if ((el.type==='checkbox' || el.type==='radio') && !el.checked) return;
      if (el.tagName==='SELECT' && el.multiple) Array.from(el.selectedOptions).forEach(function(o){values[el.name].push(o.value);});
      else values[el.name].push(el.value);
    });
    return values;
  }
  function capture() {
    var forms={};
    document.querySelectorAll('main form').forEach(function(f,i){
      if (f.method.toLowerCase()!=='post') return;
      var action=new URL(f.action,location.href).pathname;
      forms[action+':'+i]={action:action,version:f.elements.version ? f.elements.version.value : '',values:fields(f)};
    });
    return forms;
  }
  function restore(forms) {
    applying=true;
    try {
      document.querySelectorAll('main form').forEach(function(f,i){
        var action=new URL(f.action,location.href).pathname, saved=forms[action+':'+i];
        if (!saved || saved.action!==action || saved.version!==(f.elements.version ? f.elements.version.value : '')) return;
        Object.keys(saved.values || {}).forEach(function(name){
          var values=saved.values[name];
          if (!Array.isArray(values) || /^(csrf_token|_sync_.*|version|operation)$/.test(name)) return;
          var els=Array.from(f.elements).filter(function(el){return el.name===name;});
          if (!els.length && /^guided_(unknown|expand)_/.test(name)) {
            var hidden=document.createElement('input');hidden.type='hidden';hidden.name=name;f.appendChild(hidden);els=[hidden];
          }
          els.forEach(function(el,index){
            if (/^(file|password|submit|button)$/.test(el.type)) return;
            if (el.type==='checkbox' || el.type==='radio') el.checked=values.indexOf(el.value)!==-1;
            else if (el.multiple) Array.from(el.options).forEach(function(o){o.selected=values.indexOf(o.value)!==-1;});
            else el.value=values[Math.min(index,values.length-1)] || '';
            el.dispatchEvent(new Event('input',{bubbles:true}));
            el.dispatchEvent(new Event('change',{bubbles:true}));
          });
        });
      });
    } finally { applying=false; }
  }
  function apply(name,state) {
    // A read started while idle may finish during a guide turn. Keep its form
    // snapshot intact; the next poll can apply this revision after the turn.
    if ((adapter && adapter.busy()) || (name==='flow' && window.blam8KnowledgeImportBusy)) return;
    var old=states[name];
    if (old && old.revision>=state.revision) return;
    states[name]=state;
    var data=state.payload || {};
    if (name==='flow' && flow) {
      if (dirty) { conflict=true; warn('This setup changed on another device. Your edits are still here. Reload to use the shared version before continuing.'); return; }
      if (data.path && data.path!==here && !conflict) { location.replace(data.path); return; }
      if(old && data.refresh && data.refresh!==old.payload.refresh){location.reload();return;}
      restore(data.form || {});
      if (window.Blam8KnowledgeGuide && data.guide) window.Blam8KnowledgeGuide.restore(data.guide);
    }
    if (adapter && name===channel() && !adapter.busy()) adapter.set(data.messages || []);
  }
  async function read(name) {
    var r=await fetch('/device-sync/'+name,{cache:'no-store',credentials:'same-origin'});
    if (!r.ok) throw Error('Shared session unavailable');
    return r.json();
  }
  async function refresh() {
    if (polling || !initialized || conflict || (adapter && adapter.busy())) return;
    polling=true;
    try {
      var names=flow ? ['flow'] : ['assistant'];
      var result=await Promise.all(names.map(read));
      names.forEach(function(name,i){apply(name,result[i]);});
    } catch (_) { /* The next visible read recovers; writes always fail closed. */ }
    finally { polling=false; }
  }
  function serial(work) { var next=queue.catch(function(){}).then(work);queue=next;return next; }
  async function writeNow(name,patch) {
    if (conflict) throw Error('Reload the shared session before continuing.');
    var body={operation:uuid(),revision:states[name].revision,patch:patch};
    var options={method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf()},body:JSON.stringify(body)};
    var r;
    try { r=await fetch('/device-sync/'+name,options); }
    catch (_) { r=await fetch('/device-sync/'+name,options); } // same operation; never a second write
    if (!r.ok) {
      if (r.status===409) { conflict=true; warn('This work changed on another device. Your edits are still here. Reload before continuing.'); }
      else warn('Your changes have not synced yet. Keep this page open and reconnect before continuing.');
      throw Error('Shared session could not be updated.');
    }
    states[name]=await r.json();
    return states[name];
  }
  function write(name,patch) { return serial(async function(){await api.ready;return writeNow(name,patch);}); }
  function changed(e) {
    if (!flow || applying || !e.target.closest('main form') || /^_sync_/.test(e.target.name || '')) return;
    dirty=true; clearTimeout(timer);
    timer=setTimeout(function(){api.flush().catch(function(){});},350);
  }
  async function flushNow() {
    if (dirty) {
      var snapshot=capture();
      await writeNow('flow',{form:snapshot});
      // A second edit during the write must also be saved.
      dirty=JSON.stringify(snapshot)!==JSON.stringify(capture());
      if (dirty) await flushNow();
    }
  }
  var api=window.Blam8DeviceSync={
    ready:null,
    isReady:function(){return initialized && !conflict;},
    isFlow:function(){return flow;},
    state:function(){return states[channel()] || {payload:{},revision:0};},
    register:function(a){adapter=a;api.ready.then(function(){a.set(api.state().payload.messages || []);});},
    transcript:function(messages){return write(channel(),{messages:JSON.parse(JSON.stringify(messages.slice(-80)))});},
    clear:function(){return write(channel(),{messages:[]});},
    flush:function(){clearTimeout(timer);return serial(async function(){await api.ready;await flushNow();});},
    refresh:refresh,
    request:async function(url,options){
      await api.flush();
      if (conflict) throw Error('Reload the shared session before continuing.');
      var path=new URL(url,location.href).pathname;
      var name=path.indexOf('/onboarding/')===0 || path==='/concierge/onboard' ? 'flow' : flow && (path==='/concierge/help' || path.indexOf('/concierge/setup-cards/')===0) ? 'flow' : 'assistant';
      if (!states[name]) states[name]=await read(name);
      if(name==='flow') await serial(async function(){
        if(JSON.stringify(capture())!==JSON.stringify(states.flow.payload.form || {})){dirty=true;await flushNow();}
      });
      options=Object.assign({},options,{headers:Object.assign({},options.headers,{
        'X-CSRF-Token':csrf(),'X-Blam8-Operation':options.syncOperation || uuid(),
        'X-Blam8-Revision':String(states[name].revision),'X-Blam8-Channel':name,
        'X-Blam8-Automatic':api.automatic ? '1' : '0'})});
      var result=await fetch(url,options);
      if (result.headers.get('X-Blam8-Revision')) states[name]=await read(name);
      if (result.status===409 && !api.automatic) { conflict=true; warn('This work changed or is running on another device. Reload before continuing.'); }
      return result;
    }
  };
  api.ready=(async function(){
    if (document.readyState==='loading') await new Promise(function(resolve){document.addEventListener('DOMContentLoaded',resolve,{once:true});});
    flow=flow || (tuningPaths.indexOf(here)!==-1 && !!document.querySelector('[data-setup-strip]'));
    var name=channel(), state=await read(name);
    states[name]=state;
    if (flow && !state.payload.path) {
      try { state=await writeNow('flow',{path:here}); }
      catch (_) {
        for(var retry=0;retry<10;retry++){
          state=await read('flow');if(state.payload.path)break;
          await new Promise(function(resolve){setTimeout(resolve,100);});
        }
        if(!state.payload.path)throw Error('Session unavailable');
        conflict=false;if(notice)notice.remove();notice=null;
      }
    }
    // Navigation within a free app never subscribes to forced setup routing.
    initialized=true;
    apply(name,Object.assign({},state,{revision:state.revision}));
    if (flow) {
      var data=state.payload;
      if (data.path && data.path!==here) location.replace(data.path);
      else { restore(data.form || {}); if(window.Blam8KnowledgeGuide && data.guide)window.Blam8KnowledgeGuide.restore(data.guide); }
    }
    return state;
  })();
  api.ready.catch(function(){warn('Shared session unavailable. Reconnect before continuing on this device.');});
  document.addEventListener('input',changed);
  document.addEventListener('change',changed);
  document.addEventListener('submit',function(e){
    var f=e.target;
    if (!flow || f.dataset.syncSubmitting==='1' || f.id==='bc-form' || f.method.toLowerCase()!=='post') return;
    e.preventDefault();e.stopImmediatePropagation();
    if (submissions.has(f)) return;
    submissions.add(f);
    var submitter=e.submitter;
    api.flush().then(function(){
      // Native submission still owns its reentrancy guard during microtasks.
      // Resume in a later task, after that original submit dispatch finishes.
      return new Promise(function(resolve){setTimeout(resolve,0);});
    }).then(function(){
      ['operation','revision','channel'].forEach(function(key){
        var el=f.elements['_sync_'+key];if(!el){el=document.createElement('input');el.type='hidden';el.name='_sync_'+key;f.appendChild(el);}
        el.value=key==='operation' ? uuid() : key==='revision' ? states.flow.revision : 'flow';
      });
      f.dataset.syncSubmitting='1';
      try { f.requestSubmit(submitter); }
      finally { delete f.dataset.syncSubmitting; }
    }).catch(function(){warn('This action has not been submitted. Reconnect or reload the shared session first.');})
      .finally(function(){submissions.delete(f);});
  },true);
  document.addEventListener('click',function(e){
    var a=e.target.closest('a[href]');
    if (!flow || !a || e.defaultPrevented || e.ctrlKey || e.metaKey || e.shiftKey || a.target==='_blank') return;
    var target=new URL(a.href,location.href);
    if (target.origin!==location.origin || (target.pathname.indexOf('/onboarding/')!==0 && !a.hasAttribute('data-setup-next'))) return;
    e.preventDefault();
    api.flush().then(function(){return write('flow',{path:target.pathname,form:{},guide:{}});}).then(function(){location.assign(target.href);}).catch(function(){});
  });
  document.addEventListener('visibilitychange',function(){if(document.visibilityState==='visible')refresh();else api.flush().catch(function(){});});
  window.addEventListener('online',refresh);
  window.addEventListener('pageshow',refresh);
  window.addEventListener('blam8:device-sync',refresh);
  // The app shell already owns its stream. The wizard needs its own listener.
  if(here.indexOf('/onboarding/')===0 && typeof EventSource!=='undefined'){
    var stream=new EventSource('/app/stream');
    stream.addEventListener('device_sync',refresh);
    stream.addEventListener('hello',refresh);
  }
  setInterval(function(){if(document.visibilityState==='visible')refresh();},2000);
})();
