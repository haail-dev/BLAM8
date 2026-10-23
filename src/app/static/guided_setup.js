/* One durable decision, its existing card and its real owning page. */
(function () {
  'use strict';
  var state=null, panel=null, opener=null, busy=false, highlight=null, host=null, status=null;
  var previews=window.Blam8ProposalForms;
  var associating={};
  var revealed=null;
  var resumeButton=null, stream=null, conversation=null, navigation=null, historyButton=null, messageObserver=null, navigating=false;
  function messagesChanged(){
    if(!conversation)return;
    var messages=document.getElementById('bc-messages'), earlier=0;
    messages.querySelectorAll(':scope > [data-bc-role]').forEach(function(item){
      var old=!!item.dataset.bcRole && item.dataset.guideStop!==(state&&state.stop?state.stop.id:'') &&
        item.dataset.guideStop!==(state&&state.stop?state.stop.decision_id:undefined);
      item.classList.toggle('guided-earlier',old);if(old)earlier++;
    });
    if(historyButton)historyButton.hidden=!earlier;
  }
  function arrange(){
    var parent=document.getElementById('bc-panel'), messages=document.getElementById('bc-messages');
    if(!parent||!messages)return;
    if(active()){
      if(!stream){
        stream=el('div',null,'guided-stream');
        conversation=el('section',null,'guided-conversation');
        conversation.setAttribute('aria-label','Setup conversation');
        historyButton=button('Earlier conversation',function(){
          var shown=conversation.classList.toggle('guided-show-history');
          historyButton.setAttribute('aria-expanded',String(shown));
          historyButton.textContent=shown?'Hide earlier conversation':'Earlier conversation';
        });
        historyButton.className='guided-history';historyButton.setAttribute('aria-expanded','false');
        conversation.appendChild(historyButton);
        messageObserver=new MutationObserver(messagesChanged);messageObserver.observe(messages,{childList:true});
        parent.insertBefore(stream,parent.querySelector('#bc-form'));
      }
      stream.appendChild(host);
      stream.appendChild(conversation);conversation.appendChild(messages);
      if(panel){stream.appendChild(panel.error);stream.appendChild(panel.tray);}
      if(!navigation)navigation=el('div',null,'guided-navigation');stream.appendChild(navigation);
      messagesChanged();
      document.getElementById('bc-title').textContent='BLAM8 assistant — Setup';
      document.getElementById('bc-input').placeholder='Answer or ask a question…';
      parent.setAttribute('aria-live','off');
    }else if(stream){
      parent.insertBefore(host,parent.querySelector('#bc-form'));
      parent.insertBefore(messages,parent.querySelector('#bc-form'));
      if(panel){parent.insertBefore(panel.error,parent.querySelector('#bc-form'));parent.insertBefore(panel.tray,parent.querySelector('#bc-form'));}
      stream.remove();stream=null;conversation=null;navigation=null;historyButton=null;
      if(messageObserver){messageObserver.disconnect();messageObserver=null;}
      parent.setAttribute('aria-live','polite');
      document.getElementById('bc-input').placeholder='Ask a question…';
      var main=document.querySelector('main');if(main)main.style.maxHeight='';
    }
  }
  function el(tag,text,cls){var n=document.createElement(tag);if(text)n.textContent=text;if(cls)n.className=cls;return n;}
  function button(text,work,primary){var b=el('button',text,primary?'guided-primary':'');b.type='button';b.disabled=busy;b.addEventListener('click',work);return b;}
  function controlsBusy(on){if(host)(stream||host).querySelectorAll('.guided-question button,.guided-navigation button').forEach(function(b){b.disabled=on;});}
  function note(text){if(status)status.textContent=text;
    var intro=document.querySelector('[data-guided-intro-status]');if(intro&&!active())intro.textContent=text;}
  function csrf(){var m=document.cookie.match(/(?:^|;\s*)bl_csrf=([^;]+)/);return m?decodeURIComponent(m[1]):'';}
  async function request(path,body){var r=await fetch('/app/setup/guide'+path,{cache:'no-store',method:body?'POST':'GET',
    headers:{'Content-Type':'application/json','X-CSRF-Token':csrf()},body:body?JSON.stringify(body):undefined});
    var result=await r.json();if(!r.ok)throw Error(typeof result.detail==='string'?result.detail:'The guide could not be confirmed. Reload its current state.');return result;}
  function active(){return !!(state&&state.guide&&state.guide.status==='active'&&state.guide.intro_ack);}
  function clear(){if(previews)previews.clear();if(highlight)highlight.classList.remove('guided-target');highlight=null;}
  function confirmDiscard(reason){return !previews || !previews.anyDirty() || window.confirm('You have unsaved changes on this page. '+reason+' will discard those edits. Keep this page open to save or review them first, or continue and discard them.');}
  function navigate(){var path=state.stop.href.split('#')[0];if(path===location.pathname||navigating)return;
    if(!confirmDiscard('Opening the next guide page')){note('Your edits are kept here. Open the current guide page when you are ready.');return;}
    var sync=window.Blam8DeviceSync;
    navigating=true;
    Promise.resolve(sync?sync.flush():null).then(function(){location.assign(state.stop.href);}).catch(function(){navigating=false;note('Sync could not be confirmed. Your page is kept.');});}
  function resumeLocation(){
    if(active()&&state.stop.href.split('#')[0]!==location.pathname&&(!previews||!previews.anyDirty()))navigate();
  }
  async function refresh(){var next=await request('/state');state=next;paint();if(panel)panel.paint();return next;}
  async function action(operation,choice){
    if(busy)return;
    busy=true;
    controlsBusy(true);
    var composer=Array.from(document.querySelectorAll('#bc-input,#bc-form button[type="submit"]')).map(function(e){var prior=e.disabled;e.disabled=true;return [e,prior];});
    try{
      var body={revision:state.revision,stop:state.stop?state.stop.id:'intro',operation:operation};if(choice)body.choice=choice;
      state=await request('/action',body);clear();if(panel)await panel.load();
      if(operation==='knowledge_use_updated'||operation==='knowledge_keep_earlier')revealed=null;
      paint();
      if(operation==='pause'&&window.blam8ConciergeClose)window.blam8ConciergeClose();
      if(operation!=='pause')navigate();
    }catch(e){note(e.message+' Your page and text are kept.');}
    finally{busy=false;controlsBusy(false);composer.forEach(function(pair){pair[0].disabled=pair[1];});}
  }
  function ensureHost(){
    if(host)return;
    var parent=document.getElementById('bc-panel');if(!parent)return;
    host=el('section',null,'guided-question');host.setAttribute('aria-label','Current setup decision');
    parent.insertBefore(host,parent.querySelector('#bc-messages'));
  }
  function paint(){
    ensureHost();if(!host)return;host.replaceChildren();
    var hasGuide=!!(state&&state.guide), running=active();
    document.body.classList.toggle('guided-running',running);
    arrange();
    if(hasGuide&&!resumeButton){resumeButton=button('Resume guided setup',function(){action('resume');});
      resumeButton.className='guided-resume';document.body.appendChild(resumeButton);}
    if(resumeButton)resumeButton.hidden=!hasGuide||state.guide.status!=='paused';
    if(!hasGuide)return;
    status=el('p',null,'guided-status');status.setAttribute('role','status');status.setAttribute('aria-live','polite');
    if(!running){
      if(state.guide.status==='paused'){host.appendChild(el('p','Guided setup is paused. Your question and pending suggestions are kept.'));
        host.appendChild(button('Resume guided setup',function(){action('resume');},true));host.appendChild(status);}
      return;
    }
    if(opener&&(!window.Blam8DeviceSync||window.Blam8DeviceSync.isReady()))opener();
    var stop=state.stop;
    var groups={diary:'Diary',records:'Records and workflows',charges:'Charges and payments'};
    var group=(stop.substep||'').split('.')[0];
    host.appendChild(el('p',state.section?'Guided setup · '+state.section.group+' · '+state.section.badge:'Guided setup','guided-progress'));
    if(state.section)host.appendChild(el('p',state.section.label+' · '+state.remaining_sections+' sections to review','guided-section'));
    host.appendChild(el('p',stop.title,'guided-title'));
    var viewPage=button(document.body.classList.contains('guided-page-expanded')?'Return to conversation':'Expand page',function(){
      document.body.classList.toggle('guided-page-expanded');
      viewPage.textContent=document.body.classList.contains('guided-page-expanded')?'Return to conversation':'Expand page';
      fit();if(highlight)highlight.scrollIntoView({block:'start',behavior:'instant'});
    });viewPage.className='guided-page-toggle';host.appendChild(viewPage);
    var decision=state.guide.decisions[stop.decision_id||stop.id];
    var pending=!!state.pending && ['applied','cancelled','partial'].indexOf(decision)===-1;
    if(!pending||stop.kind==='explanation')host.appendChild(el('p',stop.kind==='explanation'?stop.question:decision==='applied'?'The displayed changes were saved. Next walks through the affected controls.':
      decision==='cancelled'?'You left these suggestions. Next moves on without saving them.':pending?'Review your suggested changes, then choose what to save.':stop.question));
    if(stop.kind==='explanation'&&decision)host.appendChild(el('p',decision==='applied'?'The displayed changes were saved.':
      decision==='cancelled'?'You left these suggestions. The page shows its current settings.':
      decision==='partial'?'Some choices were saved and others left. Review their receipts in the setup record.':'','guided-why'));
    (stop.kind==='fact'?state.answers||[]:[]).forEach(function(a){host.appendChild(el('p',
      (a.provenance&&['knowledge_owner_review','about_you_owner_review'].indexOf(a.provenance.kind)!==-1?'Already covered — ':'Your answer — ')+
      (typeof a.value==='boolean'?(a.value?'Yes':'No'):String(a.value)),'guided-why'));});
    (state.knowledge_conflicts||[]).forEach(function(conflict){
      var box=el('section',null,'guided-knowledge-conflict');box.setAttribute('role','status');
      box.appendChild(el('p',conflict.message));
      box.appendChild(el('p','Earlier answer: '+String(conflict.earlier.value==null?'Not applicable':conflict.earlier.value)));
      if(conflict.candidate&&conflict.candidate.state==='answered')box.appendChild(el('p','Updated Knowledge: '+String(conflict.candidate.answer)));
      box.appendChild(el('p','Choose which answer to use, or edit the suggestion. Settings still need explicit acceptance.'));
      if(['answered','not_applicable'].indexOf(conflict.earlier.state)!==-1)box.appendChild(button('Use earlier answer',function(){action('knowledge_keep_earlier',conflict.id);}));
      if(conflict.candidate&&['answered','not_applicable'].indexOf(conflict.candidate.state)!==-1)box.appendChild(button('Use updated answer',function(){action('knowledge_use_updated',conflict.id);}));
      host.appendChild(box);
    });
    if(stop.why&&!pending){var why=el('details',null,'guided-why');why.appendChild(el('summary','Why this matters'));why.appendChild(el('p',stop.why));host.appendChild(why);}
    if(stop.kind==='fact'){
      var direct=el('details'),summary=el('summary','Answer directly');direct.appendChild(summary);
      var field;
      if(stop.answer_kind==='boolean'||stop.answer_kind==='choice'){
        field=el('select');var blank=el('option','Choose an answer');blank.value='';field.appendChild(blank);
        var choices=stop.answer_kind==='boolean'?['true','false']:stop.choices;
        choices.forEach(function(value){var words={true:'Yes',false:'No',owner_arranges:'I arrange and confirm times',
          review_booking_settings:'Review the assistant’s booking rules'};
          var option=el('option',words[value]||value);option.value=value;field.appendChild(option);});
      }else{field=el('textarea');field.rows=2;field.maxLength=2000;}
      field.setAttribute('aria-label',stop.question);field.className='guided-direct-answer';direct.appendChild(field);
      direct.appendChild(button('Use answer',async function(){
        if(busy)return;var value=field.value;if(stop.answer_kind==='boolean'){if(!value){note('Choose Yes or No.');return;}value=value==='true';}
        busy=true;controlsBusy(true);
        try{state=await request('/answer',{revision:state.revision,stop:stop.id,state:'answered',value:value});paint();}
        catch(e){note(e.message+' Your direct answer is kept.');}
        finally{busy=false;controlsBusy(false);}
      }));
      host.appendChild(direct);
    }
    if(stop.kind==='fact'&&(state.scopes||[]).length>1){
      var label=el('label','Answer for '),select=el('select');select.setAttribute('aria-label','Answer scope');
      state.scopes.filter(function(s){return (stop.scopes||[]).indexOf(s.type)!==-1;}).forEach(function(s){
        var option=el('option',s.label);option.value=s.type+':'+s.id;option.selected=s.type===state.guide.scope_type&&s.id===state.guide.scope_id;select.appendChild(option);});
      select.addEventListener('change',async function(){try{state=await request('/action',{revision:state.revision,stop:stop.id,operation:'scope',
        scope_type:select.value.split(':')[0],scope_id:select.value.slice(select.value.indexOf(':')+1)});paint();}catch(e){note(e.message);}});
      label.appendChild(select);host.appendChild(label);
    }
    var actions=el('div',null,'guided-actions');
    var more=el('details',null,'guided-more');more.appendChild(el('summary','More options'));
    var secondary=el('div',null,'guided-actions');more.appendChild(secondary);
    if(stop.kind==='intro'){
      actions.appendChild(button('Start guided setup',function(){action('start');},true));
    }else if(stop.kind==='relevance'||stop.kind==='need'){
      var operation=stop.kind==='need'?'need':'relevance';
      actions.appendChild(button('Needed',function(){action(operation,'needed');},true));
      actions.appendChild(button('Not needed',function(){action(operation,'off');}));
      actions.appendChild(button('Decide later',function(){action(operation,'deferred');}));
    }else if(stop.kind==='review'){
      (state.unresolved||[]).forEach(function(item){actions.appendChild(button('Review '+item.title,function(){action('revisit',item.id);}));});
      actions.appendChild(button('Continue — reviewed',function(){action('review');},true));
      actions.appendChild(button('Defer this section',function(){action('later');}));
    }else if(stop.kind==='finished'){
      actions.appendChild(button('Finish review',function(){action('finish');},true));
    }else{
      actions.appendChild(button('Next',function(){action('next');},!pending));
      if(stop.kind==='decision'||stop.kind==='fact'){
        secondary.appendChild(button('Decide later',function(){action('later');}));
        secondary.appendChild(button('Not needed',function(){action('not_needed');}));
        if(stop.kind==='decision'&&!pending&&decision!=='applied')actions.appendChild(button('Keep current settings',function(){action('keep_current');}));
      }
    }
    if(state.index>0)actions.appendChild(button('Back',function(){action('back');}));
    secondary.appendChild(button('Pause setup',function(){action('pause');}));
    if(state.guide.knowledge_unavailable){
      host.appendChild(el('p','Reviewed Knowledge could not be loaded. Your existing answers are kept.','guided-why'));
      secondary.appendChild(button('Retry Knowledge',function(){action('retry_knowledge');}));
    }
    navigation.replaceChildren(actions,more);host.appendChild(status);
    if(location.pathname!==stop.href.split('#')[0]){
      var link=el('a','Open '+stop.title);link.href=stop.href;link.className='guided-owning-link';
      link.addEventListener('click',function(e){e.preventDefault();navigate();});host.appendChild(link);
      note('The current question is on its owning page. Your position is kept.');
    }
    if(panel)project(panel.data);
    if(stream&&revealed!==stop.id){stream.scrollTop=0;conversation.classList.remove('guided-show-history');}
    fit();
    if(revealed!==stop.id && highlight){revealed=stop.id;requestAnimationFrame(function(){
      if(!document.activeElement||!/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName))highlight.scrollIntoView({block:'start',behavior:'instant'});
    });}
  }
  function matches(card,line){
    if(!active() || !state.stop || ['decision','explanation'].indexOf(state.stop.kind)===-1)return false;
    var identity=card.page+'/'+(line.name==='setting'?line.fields.key:line.name);
    return (state.stop.catalogue||[]).indexOf(identity)!==-1;
  }
  function selection(card){return card.lines.filter(function(line){return matches(card,line)&&(line.status==='proposed'||line.status==='held');}).map(function(l){return l.id;});}
  function editor(){return document.querySelector('[data-guided-editor="'+CSS.escape(state.stop.page||'')+'"]');}
  function target(){
    var stop=state.stop;
    var specific=document.querySelector('[data-guided-decision="'+CSS.escape(stop.focus_catalogue||stop.decision_id||stop.id)+'"]');
    if(specific)return specific;
    if(stop.page==='dependants'&&!document.querySelector('#dep-form .dep-group'))return document.getElementById('add-group')||editor();
    if(stop.anchor==='orientation.diary')return document.querySelector('[data-tour-diary]');
    if(stop.anchor==='orientation.money')return document.querySelector('[data-tour="sheet"]');
    if(stop.anchor&&stop.anchor.indexOf('setting.')===0){var key=stop.anchor.slice(8);
      var name=key==='stay_mode'?'booking_mode':key;return document.querySelector('main [name="'+CSS.escape(name)+'"]')||editor();}
    return editor();
  }
  function reveal(t){
    var p=t;while(p){if(p.tagName==='DETAILS')p.open=true;p=p.parentElement;}
    if(highlight&&highlight!==t)highlight.classList.remove('guided-target');highlight=t;t.classList.add('guided-target');
  }
  function project(data){
    if(!active()||state.stop.href.split('#')[0]!==location.pathname)return;
    clear();var t=target();if(t)reveal(t);else if(state.stop.anchor)note('This control is unavailable here. Use its owning page or defer this decision; it remains unresolved.');
    if(host){var obsoleteRefresh=host.querySelector('[data-guided-refresh]');if(obsoleteRefresh)obsoleteRefresh.remove();}
    if((state.knowledge_conflicts||[]).length){note('Knowledge changed. Resolve the earlier answer before accepting or displaying it as a current proposal.');return;}
    var newCard=(data.cards||[]).find(function(card){return selection(card).length&&(!state.pending||card.id!==state.pending.card_id);});
    if(newCard){
      var key=newCard.id+':'+state.stop.id;
      if(!associating[key]&&!busy){associating[key]=true;
        request('/action',{revision:state.revision,stop:state.stop.id,operation:'associate'}).then(function(next){state=next;paint();})
          .catch(function(e){note(e.message);});}
      return;
    }
    if(['applied','cancelled','partial'].indexOf(state.guide.decisions[state.stop.decision_id||state.stop.id])!==-1)return;
    if(state.pending){var v=data.views[state.pending.page],c=(data.cards||[]).find(function(c){return c.id===state.pending.card_id;});
      var snapshot=v&&v.all&&!v.all.error?v.all.settings_version:null;
      if(c&&(c.revision!==state.pending.revision || snapshot&&snapshot!==state.pending.settings_version)){
        note('The card or saved settings changed. Your suggestion is kept; review the current difference before accepting.');
        if(host&&!host.querySelector('[data-guided-refresh]')){var refresh=button('Review current difference',function(){action('refresh_preview');});refresh.dataset.guidedRefresh='';host.appendChild(refresh);}
        return;
      }
    }
    if(!previews)return;
    var projected=0, pending=0;
    (data.cards||[]).forEach(function(card){card.lines.forEach(function(line){
      if(!matches(card,line)||['proposed','held'].indexOf(line.status)===-1)return;
      pending++;
      var bind=card.id+':'+state.stop.id;
      if(!state.pending&&!associating[bind]&&!busy){associating[bind]=true;
        request('/action',{revision:state.revision,stop:state.stop.id,operation:'associate'}).then(function(next){state=next;paint();})
          .catch(function(e){note(e.message);});}
      if(line.name!=='setting')return;
      var key=line.fields.key, name=key==='stay_mode'?'booking_mode':key;
      var controls=document.querySelectorAll('main [name="'+CSS.escape(name)+'"]');
      var control=Array.from(controls).find(function(e){return e.type!=='hidden';});
      var row=document.querySelector('[data-line-id="'+CSS.escape(line.id)+'"]');
      var edit=function(){if(row){var buttons=Array.from(row.querySelectorAll('button'));var b=buttons.find(function(b){return b.textContent==='Edit';});if(b)b.click();}};
      var value=key==='stay_mode'?(line.fields.value?'stay':'appointment'):line.fields.value;
      if(control&&previews.projectNamed(controls,value,{id:line.id,field:'value',edit:edit,change:function(value,raw){
        if(key==='stay_mode')return;
        note('Checking your edit. It is not saved to settings.');
        panel.guidedDraft={control:control,raw:raw};
        panel.mutate(card,'/changes',{revision:card.revision,changes:[{op:'set',id:line.id,fields:{value:value},
          sources:{value:{kind:'owner_said',reference:'Owner edited the proposed control'}}}]});
      }})){projected++;reveal(control);}
    });});
    if(pending&&!projected)note('Review the existing card beside this editor. Compound or locally edited controls stay unchanged until you Accept.');
    else if(projected)note('Proposed — not saved.');
    if(state.stop.kind==='explanation'&&t)reveal(t);
  }
  function fit(){
    if(!active())return;
    var vp=window.visualViewport, height=vp?vp.height:innerHeight;
    document.documentElement.style.setProperty('--guided-viewport',height+'px');
    var inset=vp?Math.max(0,innerHeight-vp.height-vp.offsetTop):0;
    document.documentElement.style.setProperty('--guided-keyboard-inset',inset+'px');
    document.body.classList.toggle('guided-keyboard',inset>100);
    var p=document.getElementById('bc-panel');if(p)document.documentElement.style.setProperty('--guided-panel',p.getBoundingClientRect().height+'px');
    var main=document.querySelector('main');if(main){main.style.maxHeight=innerWidth<=767&&p?
      Math.max(0,p.getBoundingClientRect().top-main.getBoundingClientRect().top-12)+'px':'';}
  }
  var api=window.Blam8GuidedSetup={ready:null,active:active,exists:function(){return !!(state&&state.guide);},
    currentStop:function(){return active()&&state.stop?state.stop.id:'';},messagesChanged:messagesChanged,pause:function(){return action('pause');},
    attach:function(cards,open){panel=cards;opener=open;Promise.all([api.ready,
      window.Blam8DeviceSync?window.Blam8DeviceSync.ready:Promise.resolve()]).then(function(){paint();cards.paint();resumeLocation();});},
    selection:selection,matches:matches,project:project,
    beforeApply:function(){if((state.knowledge_conflicts||[]).length){note('Resolve the changed Knowledge answer before accepting.');return false;}return confirmDiscard('Accepting this suggestion');},
    changed:async function(){try{await refresh();}catch(e){clear();note(e.message);}},
    turn:async function(question,history){
      if(busy)throw Error('Wait for the current guide action.');busy=true;
      try{var result=await request('/turn',{revision:state.revision,stop:state.stop.id,question:question,path:location.pathname,
        history:history.slice(-12).map(function(t){return {role:t.role,content:String(t.content||'').slice(0,4000)};})});
        state=result;paint();return result;
      }finally{busy=false;controlsBusy(false);}
    }};
  api.ready=(async function(){try{await refresh();}catch(_){state=null;note('The guide could not be loaded. Reload to try again; your saved setup is kept.');}
    document.querySelectorAll('[data-guided-start]').forEach(function(b){b.disabled=false;b.addEventListener('click',function(){
      if(!state){location.reload();return;}
      if(state&&state.guide&&state.guide.intro_ack)action('resume');else action('start');
    });});return state;})();
  window.addEventListener('guided:submit-blocked',function(){note('These are proposed values. Use Accept in the card to save them, or Leave to restore the page.');});
  window.addEventListener('guided:projection-conflict',function(){note('Your restored edits differ from this preview. They are kept; reload the card and review before applying it.');});
  window.addEventListener('resize',fit);if(window.visualViewport)window.visualViewport.addEventListener('resize',fit);
  setInterval(async function(){
    if(!state||!state.guide||document.hidden||busy||panel&&(panel.busy||panel.editing)||
      document.activeElement&&/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName))return;
    try{var next=await request('/state');if(next.revision>state.revision||JSON.stringify(next.knowledge_conflicts)!==JSON.stringify(state.knowledge_conflicts)){state=next;clear();paint();if(panel)panel.load();resumeLocation();}}catch(_){}
  },2500);
  document.addEventListener('keydown',function(e){if(e.key==='Escape'&&active()&&host&&host.contains(e.target))action('pause');});
})();
