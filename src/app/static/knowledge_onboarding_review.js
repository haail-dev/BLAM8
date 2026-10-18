// Only server-rendered, escaped review fragments are admitted to the page.
(function () {
  var go=document.getElementById('site-go'), input=document.getElementById('site-url');
  function replace(data) {
    var parser=new DOMParser();
    var coverage=parser.parseFromString(data.coverage_html,'text/html').getElementById('knowledge-review-coverage');
    var current=document.getElementById('knowledge-review-coverage');
    if(current && coverage) current.replaceWith(coverage);
    var holder=document.getElementById('knowledge-pending-reviews');
    if(holder) {
      var incoming=parser.parseFromString(data.review_html,'text/html');
      var existing=Array.from(holder.querySelectorAll('[data-knowledge-review]'));
      incoming.querySelectorAll('[data-knowledge-review]').forEach(function(next){
        var old=existing.find(function(form){return form.dataset.knowledgeReview===next.dataset.knowledgeReview;});
        if(old){
          // Keep edits and the old version: the server requires a fresh overlap
          // preview before admitting against changed knowledge or new drafts.
          if(!old.querySelector('[data-review-changed]')){
            var notice=document.createElement('p');notice.dataset.reviewChanged='1';
            notice.className='text-sm text-amber-800';
            notice.textContent='New review evidence arrived. Your edits are preserved; check changes and overlaps before saving.';
            old.prepend(notice);
          }
          next.replaceWith(old);
        }
      });
      holder.replaceChildren(...incoming.body.childNodes);
    }
  }
  window.addEventListener('blam8:knowledge-review',async function(){
    try { var r=await fetch('/onboarding/knowledge/reviews',{cache:'no-store'}); if(r.ok) replace(await r.json()); }
    catch(e) { /* Stored proposals remain available on reload. */ }
  });
  if(!go || !input) return;
  var status=document.getElementById('site-status'), box=document.getElementById('site-box');
  function say(text){status.hidden=false;status.textContent=text;}
  function announce(ok){window.blam8SiteRead={done:true,ok:ok};document.dispatchEvent(new CustomEvent('blam8:site-read',{detail:{ok:ok}}));}
  async function read(){
    if(window.blam8KnowledgeImportBusy || (window.Blam8KnowledgeSourceReview && window.Blam8KnowledgeSourceReview.working())) return;
    if(!input.value.trim()){say('Paste your website address first.');return;}
    window.blam8KnowledgeImportBusy=true;
    go.disabled=true;go.textContent='Reading…';say('Reading your website and preparing drafts for review…');
    var ok=false;
    try{
      var fd=new FormData();fd.append('url',input.value.trim());fd.append('csrf_token',document.getElementById('knowledge-review-csrf').value);
      var response=await fetch('/onboarding/knowledge/website',{method:'POST',body:fd});
      var data=await response.json();if(!response.ok)throw new Error(data.error || data.detail || 'Could not read that site.');
      replace(data);ok=true;
      say('Read '+data.pages.length+' pages. Drafts are pending; review evidence, unanswered details and overlaps below. Nothing has been saved to Knowledge.');
    }catch(e){say((e.message || 'Could not read that site.')+' Your existing drafts are unchanged.');}
    finally{window.blam8KnowledgeImportBusy=false;go.disabled=false;go.textContent='Read my site';announce(ok);}
  }
  go.addEventListener('click',read);
  input.addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();read();}});
  var again=document.getElementById('site-again');
  if(again)again.addEventListener('click',function(){box.hidden=false;document.getElementById('site-done').hidden=true;input.focus();});
  if(box.dataset.autoSite==='1' && input.value.trim()){
    var stamp='blam8_site_autoread:'+box.dataset.scope,done=false;
    try{done=sessionStorage.getItem(stamp)==='1';sessionStorage.setItem(stamp,'1');}catch(e){}
    if(done)announce(false);else read();
  }
})();
