/* One durable decision, its existing card and its real owning page. */
(function () {
  'use strict';
  var state=null, panel=null, opener=null, busy=false, highlight=null, host=null, status=null;
  var previews=window.Blam8ProposalForms;
  var associating={};
  var revealed=null;
  var sectionContext={
    'diary.activities':'Choose which activities the assistant can help arrange, and which you will arrange yourself.',
    'diary.time':'Set the times and capacity available for bookings. These rules apply only when booking is enabled.',
    'diary.conditions':'Choose what customers must provide before a booking can go ahead, such as answers or documents.',
    'records.relationships':'Keep separate records for people, places or items linked to a customer. Give each type a name that fits your business.',
    'records.information':'Choose the details to keep on each record, and when the assistant should ask for or confirm them.',
    'records.progress':'Choose whether to track stages for customers or their related records, and what should happen when a stage changes.',
    'charges.responsibility':'Choose who is responsible for charges and which business details appear on quotes and invoices.',
    'charges.terms':'Review your charges, payment deadlines and any deposit rules.',
    'charges.methods':'Choose how customers can pay and which payment instructions to include.'
  };
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
      // The step's question and answers stay pinned under the thread; More
      // options sits just below them, revealed by scrolling the pinned bar.
      if(!navigation)navigation=el('div',null,'guided-navigation');parent.insertBefore(navigation,parent.querySelector('#bc-form'));
      messagesChanged();
      document.getElementById('bc-title').textContent='BLAM8 assistant — Setup';
      document.getElementById('bc-input').placeholder='Answer or ask a question…';
      parent.setAttribute('aria-live','off');
    }else if(stream){
      parent.insertBefore(host,parent.querySelector('#bc-form'));
      parent.insertBefore(messages,parent.querySelector('#bc-form'));
      if(panel){parent.insertBefore(panel.error,parent.querySelector('#bc-form'));parent.insertBefore(panel.tray,parent.querySelector('#bc-form'));}
      stream.remove();if(navigation)navigation.remove();stream=null;conversation=null;navigation=null;historyButton=null;
      if(messageObserver){messageObserver.disconnect();messageObserver=null;}
      parent.setAttribute('aria-live','polite');
      document.getElementById('bc-input').placeholder='Ask a question…';
      var main=document.querySelector('main');if(main)main.style.maxHeight='';
    }
  }
  function el(tag,text,cls){var n=document.createElement(tag);if(text)n.textContent=text;if(cls)n.className=cls;return n;}
  function button(text,work,primary){var b=el('button',text,primary?'guided-primary':'');b.type='button';b.disabled=busy;b.addEventListener('click',work);return b;}
  function controlsBusy(on){if(host)document.querySelectorAll('.guided-question button,.guided-navigation button').forEach(function(b){b.disabled=on;});
    document.querySelectorAll('[data-guided-start],[data-guided-entry]').forEach(function(b){b.disabled=on;});}
  function note(text){if(status)status.textContent=text;
    var intro=document.querySelector('[data-guided-intro-status]');if(intro&&!active())intro.textContent=text;}
  function csrf(){var m=document.cookie.match(/(?:^|;\s*)bl_csrf=([^;]+)/);return m?decodeURIComponent(m[1]):'';}
  async function request(path,body){var r=await fetch('/app/setup/guide'+path,{cache:'no-store',method:body?'POST':'GET',
    headers:{'Content-Type':'application/json','X-CSRF-Token':csrf()},body:body?JSON.stringify(body):undefined});
    var result=await r.json();if(!r.ok){var failed=Error(typeof result.detail==='string'?result.detail:'The guide could not be confirmed. Reload its current state.');
      failed.status=r.status;throw failed;}return result;}
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
    if(state&&state.stop&&state.stop.substep&&state.progress){
      document.dispatchEvent(new CustomEvent('onboarding:progress',{detail:{
        current:state.stop.substep.split('.')[0],groups:state.progress}}));
    }
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
    var sectionNumber=state.section?{A:1,B:2,C:3}[state.section.badge]:null;
    host.appendChild(el('p',state.section?state.section.group+(sectionNumber?' · Section '+sectionNumber+' of 3':''):'Guided setup','guided-progress'));
    if(state.section&&state.section.label!==stop.title)host.appendChild(el('p',state.section.label,'guided-section'));
    host.appendChild(el('p',stop.title,'guided-title'));
    if(sectionContext[stop.substep]&&DISCOVERY.indexOf(stop.kind)===-1)host.appendChild(el('p',sectionContext[stop.substep],'guided-context'));
    var viewPage=button(document.body.classList.contains('guided-page-expanded')?'Return to conversation':'Expand page',function(){
      document.body.classList.toggle('guided-page-expanded');sheet(null);
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
    }else if(DISCOVERY.indexOf(stop.kind)!==-1){
      discovery(stop,actions,secondary);
    }else{
      actions.appendChild(button('Next',function(){action('next');},!pending));
      if(stop.kind==='decision'||stop.kind==='fact'){
        secondary.appendChild(button('Decide later',function(){action('later');}));
        secondary.appendChild(button('Not needed',function(){action('not_needed');}));
        if(stop.kind==='decision'&&!pending&&decision!=='applied')actions.appendChild(button('Keep current settings',function(){action('keep_current');}));
      }
    }
    if(state.index>0)actions.appendChild(button('Back',function(){action('back');}));
    if(state.guide.knowledge_unavailable){
      host.appendChild(el('p','Reviewed Knowledge could not be loaded. Your existing answers are kept.','guided-why'));
      secondary.appendChild(button('Retry Knowledge',function(){action('retry_knowledge');}));
    }
    var pinned=[actions];if(stop.kind==='relevance'||stop.kind==='need')pinned.unshift(el('p',stop.question,'guided-ask'));
    if(secondary.childElementCount)pinned.push(more);
    navigation.replaceChildren.apply(navigation,pinned);host.appendChild(status);pin();
    if(location.pathname!==stop.href.split('#')[0]){
      var link=el('a','Open '+stop.title);link.href=stop.href;link.className='guided-owning-link';
      link.addEventListener('click',function(e){e.preventDefault();navigate();});host.appendChild(link);
      note('The current question is on its owning page. Your position is kept.');
    }
    if(panel)project(panel.data);
    if(stream&&revealed!==stop.id){
      stream.scrollTop=0;conversation.classList.remove('guided-show-history');
      historyButton.setAttribute('aria-expanded','false');historyButton.textContent='Earlier conversation';
    }
    fit();
    if(revealed!==stop.id){revealed=stop.id;if(highlight)requestAnimationFrame(function(){
      if(highlight&&(!document.activeElement||!/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)))highlight.scrollIntoView({block:'start',behavior:'instant'});
    });}
  }
  // ── Diary discovery: answers first, settings after ─────────────────────────
  var DISCOVERY=['diaries','diary_fact','hours','profile'], suggesting=null;
  // What the owner typed or ticked survives a repaint (suggestions arrive while they type).
  var draft={stop:null,text:'',picked:{}};
  var DAYS=[['mon','Monday'],['tue','Tuesday'],['wed','Wednesday'],['thu','Thursday'],['fri','Friday'],['sat','Saturday'],['sun','Sunday']];
  var WORDS={'diary.starter':{customers:'Customers ask for it',we_arrange:'We arrange it',both:'Customers ask, or we arrange it',
      from_records:'It follows a date we already keep',internal:'Just us, no customer'},
    'diary.occupies':{people:'Someone’s time',places:'A place',equipment:'Equipment',nothing:'Nothing — it’s a date or a to-do'},
    'diary.time_shape':{set_time:'A set time',window:'An arrival window',days:'Over one or more days',due_by:'Done by a date'},
    'diary.repeats':{no:'No',weekly:'Weekly',fortnightly:'Fortnightly',monthly:'Monthly',six_weekly:'Every six weeks',
      quarterly:'Quarterly',depends:'It depends on something'},
    'diary.agreement':{no:'No',customer:'The customer confirms',someone_else:'Someone else'}};
  var SUPPORT={ready:'Ready to set up',from_records:'Shown from your records',noted:'Noted for later',
    unavailable:'Not available on this account',undecided:'Not decided yet'};
  async function diaryAction(operation,extra){
    if(busy)return;busy=true;controlsBusy(true);
    var send=function(){return request('/diary',Object.assign({revision:state.revision,stop:state.stop.id,operation:operation},extra||{}));};
    try{
      // Suggestions save in the background; a stale revision reloads once and resends the same choice.
      try{state=await send();}catch(e){if(e.status!==409)throw e;state=await request('/state');state=await send();}
      clear();paint();navigate();
    }catch(e){note(e.message+' Your answers so far are kept.');}
    finally{busy=false;controlsBusy(false);}
  }
  function answerWith(value,status){return diaryAction('answer',{state:status||'answered',value:value});}
  function hoursText(value){
    if(!value||!value.working)return '';
    return DAYS.filter(function(d){return value.working[d[0]];}).map(function(d){return d[1]+' '+value.working[d[0]].join('–');}).join(', ');
  }
  function shown(question,value){
    if(value==null)return '';
    if(question==='diary.hours')return hoursText(value);
    if(question==='diary.date_source'){var source=(state.discovery.date_sources||[]).find(function(d){return d.id===value;});return source?source.label:String(value);}
    if(Array.isArray(value))return value.map(function(v){return (WORDS[question]||{})[v]||v;}).join(', ');
    if(typeof value==='object'&&value.amount)return value.amount+' '+value.unit;
    return (WORDS[question]||{})[value]||String(value);
  }
  function unsure(secondary,extra){
    secondary.appendChild(button('Not sure',function(){answerWith(null,'unknown');}));
    secondary.appendChild(button('Decide later',function(){answerWith(null,'deferred');}));
    secondary.appendChild(button(extra||'Doesn’t apply',function(){answerWith(null,'not_applicable');}));
  }
  function prefillBox(stop){
    var prefill=state.discovery.prefill;if(!prefill)return;
    var box=el('section',null,'guided-prefill');box.setAttribute('role','status');
    var source={conversation:'what you said',suggestion:'the suggestion you chose',context:'your setup'}[prefill.source]||'your setup';
    if(prefill.value!=null){
      box.appendChild(el('p',(prefill.change?'You gave a different answer earlier. Change it to: ':'From '+source+': ')+shown(stop.question_id,prefill.value)+'. Is that right?'));
      box.appendChild(button(prefill.change?'Use the new answer':'Yes, that’s right',function(){diaryAction('accept_prefill');},true));
      box.appendChild(button(prefill.change?'Keep the earlier answer':'Change it',function(){diaryAction('dismiss_prefill');}));
    }else if(prefill.words){box.appendChild(el('p','From your notes: “'+prefill.words+'”. Set the times below to confirm them.'));}
    host.appendChild(box);
  }
  function currentAnswer(stop){
    var a=state.discovery.answer;if(!a)return;
    host.appendChild(el('p','Your answer — '+(a.state==='answered'?shown(stop.question_id,a.value):
      {unknown:'Not sure',deferred:'Decide later',not_applicable:'Doesn’t apply'}[a.state]),'guided-why'));
  }
  function choices(stop,actions){
    var words=WORDS[stop.question_id]||{}, list=el('div',null,'guided-choices');
    if(stop.answer_kind==='choice'){
      stop.choices.forEach(function(value){list.appendChild(button(words[value]||value,function(){answerWith(value);}));});
    }else if(stop.answer_kind==='multi'){
      var boxes=stop.choices.map(function(value){var label=el('label'),box=el('input');box.type='checkbox';box.value=value;
        label.appendChild(box);label.appendChild(document.createTextNode(' '+(words[value]||value)));list.appendChild(label);return box;});
      actions.appendChild(button('Use answer',function(){var picked=boxes.filter(function(b){return b.checked;}).map(function(b){return b.value;});
        if(!picked.length){note('Choose at least one.');return;}answerWith(picked);},true));
    }else if(stop.answer_kind==='reference'){
      (state.discovery.date_sources||[]).forEach(function(d){list.appendChild(button(d.label,function(){answerWith(d.id);}));});
    }else{
      var field=el('input'),unit=null;field.className='guided-direct-answer';field.setAttribute('aria-label',stop.question);
      if(stop.answer_kind==='count'||stop.answer_kind==='length'){field.type='number';field.min='1';field.max='999';field.step='1';}
      else{field.type='text';field.maxLength=200;}
      list.appendChild(field);
      if(stop.answer_kind==='length'){unit=el('select');unit.setAttribute('aria-label','Unit');
        ['minutes','hours','days'].forEach(function(u){var o=el('option',u);o.value=u;unit.appendChild(o);});list.appendChild(unit);}
      actions.appendChild(button('Use answer',function(){
        var raw=field.value.trim();if(!raw){note('Type an answer, or choose Not sure.');return;}
        if(stop.answer_kind==='count')return answerWith(parseInt(raw,10));
        if(stop.answer_kind==='length')return answerWith({amount:parseInt(raw,10),unit:unit.value});
        answerWith(raw);},true));
    }
    host.appendChild(list);
  }
  function weekRows(prefix,value){
    var rows=el('div',null,'guided-hours'),inputs={};
    DAYS.forEach(function(d){
      var row=el('div',null,'guided-hours-row'),label=el('label'),on=el('input'),open=el('input'),close=el('input');
      on.type='checkbox';open.type=close.type='time';open.step=close.step='900';
      var span=value&&value[d[0]];on.checked=!!span;open.value=span?span[0]:'09:00';close.value=span?span[1]:'17:00';
      open.setAttribute('aria-label',prefix+' '+d[1]+' opening');close.setAttribute('aria-label',prefix+' '+d[1]+' closing');
      label.appendChild(on);label.appendChild(document.createTextNode(' '+d[1]));
      row.appendChild(label);row.appendChild(open);row.appendChild(el('span','to'));row.appendChild(close);rows.appendChild(row);
      inputs[d[0]]=[on,open,close];
    });
    return {node:rows,read:function(){var week={};Object.keys(inputs).forEach(function(k){var i=inputs[k];if(i[0].checked)week[k]=[i[1].value,i[2].value];});return week;}};
  }
  function hours(stop,actions,secondary){
    var a=state.discovery.answer, prefill=state.discovery.prefill;
    var start=(a&&a.state==='answered'&&a.value)||(prefill&&prefill.value)||null;
    var working=weekRows('Working',start&&start.working);host.appendChild(el('p','When you’re working','guided-section'));host.appendChild(working.node);
    var bank=el('label'),bankBox=el('input');bankBox.type='checkbox';bankBox.checked=!!(start&&start.bank_holidays_closed);
    bank.appendChild(bankBox);bank.appendChild(document.createTextNode(' We’re closed on bank holidays'));host.appendChild(bank);
    var same=el('label'),sameBox=el('input');sameBox.type='checkbox';sameBox.checked=!start||start.contact_same!==false;
    same.appendChild(sameBox);same.appendChild(document.createTextNode(' People can reach us at the same times'));host.appendChild(same);
    var contact=weekRows('Contact',start&&start.contact);contact.node.hidden=sameBox.checked;host.appendChild(contact.node);
    sameBox.addEventListener('change',function(){contact.node.hidden=sameBox.checked;});
    actions.appendChild(button('Use these hours',function(){
      var value={working:working.read(),bank_holidays_closed:bankBox.checked,contact_same:sameBox.checked};
      if(!sameBox.checked)value.contact=contact.read();
      if(!Object.keys(value.working).length){note('Tick at least one working day, or choose We don’t keep set hours.');return;}
      answerWith(value);},true));
    unsure(secondary,'We don’t keep set hours');
  }
  function profile(actions,secondary){
    var p=state.discovery.profile,list=el('ul',null,'guided-diary-list');
    p.diaries.forEach(function(d){
      var item=el('li');item.appendChild(el('strong',d.label));item.appendChild(el('span',' — '+SUPPORT[d.support],'guided-badge guided-badge-'+d.support));
      item.appendChild(el('p',d.line.charAt(0).toUpperCase()+d.line.slice(1)+'.'));
      d.reasons.forEach(function(r){item.appendChild(el('p',r,'guided-why'));});
      var row=el('div',null,'guided-actions');
      row.appendChild(button('Edit',function(){diaryAction('edit',{diary:d.id});}));
      row.appendChild(button('Remove',function(){if(window.confirm('Remove '+d.label+'? Its answers are kept in the history.'))diaryAction('remove',{diary:d.id});}));
      item.appendChild(row);list.appendChild(item);
    });
    if(!p.diaries.length)list.appendChild(el('li','No diaries. That’s fine — Diary settings will be skipped.'));
    host.appendChild(list);host.appendChild(el('p',p.hours,'guided-why'));
    actions.appendChild(button('That’s right',function(){diaryAction('confirm');},true));
    actions.appendChild(button('Add a diary',function(){diaryAction('add_more');}));
    secondary.appendChild(button('Decide later',function(){diaryAction('later');}));
  }
  function diaries(actions,secondary){
    var d=state.discovery,existing=el('ul',null,'guided-diary-list');
    d.diaries.forEach(function(item){var li=el('li',item.label);
      li.appendChild(button('Remove',function(){diaryAction('remove',{diary:item.id});}));existing.appendChild(li);});
    if(d.diaries.length){host.appendChild(el('p','Your diaries','guided-section'));host.appendChild(existing);}
    if(draft.stop!==state.stop.id)draft={stop:state.stop.id,text:'',picked:{}};
    var boxes=(d.suggestions||[]).map(function(s){
      var label=el('label',null,'guided-suggestion'),box=el('input');box.type='checkbox';box.value=s.id;
      box.checked=!!draft.picked[s.label];box.addEventListener('change',function(){draft.picked[s.label]=box.checked;});
      label.appendChild(box);label.appendChild(el('strong',' '+s.label));label.appendChild(el('span',' — '+s.reason,'guided-why'));
      host.appendChild(label);return box;});
    (d.proposed_labels||[]).forEach(function(p){host.appendChild(button('Add “'+p.label+'”',function(){diaryAction('accept_label',{label:p.label});}));});
    var other=el('textarea');other.rows=2;other.maxLength=400;other.className='guided-direct-answer';
    other.placeholder='Something else — one per line';other.setAttribute('aria-label','Other diaries, one per line');host.appendChild(other);
    other.value=draft.text;other.addEventListener('input',function(){draft.text=other.value;});
    actions.appendChild(button('Continue',function(){
      var labels=other.value.split(/\n|,/).map(function(s){return s.trim();}).filter(Boolean);
      var picked=boxes.filter(function(b){return b.checked;}).map(function(b){return b.value;});
      if(!labels.length&&!picked.length&&!d.diaries.length){note('Choose or name at least one diary, or choose Not sure.');return;}
      diaryAction('choose',{suggestions:picked,labels:labels});},true));
    if(!d.diaries.length){
      secondary.appendChild(button('Not sure',function(){diaryAction('choose',{state:'unknown'});}));
      secondary.appendChild(button('None of these apply',function(){diaryAction('choose',{state:'not_applicable'});}));
    }
    if(state.guide.diary&&state.guide.diary.suggestions_source!=='model'&&suggesting!==state.stop.id&&!busy){
      suggesting=state.stop.id;
      request('/diary/suggest',{revision:state.revision,stop:state.stop.id,operation:'suggest'})
        .then(function(next){if(next.revision>=state.revision){state=next;paint();}}).catch(function(){});
    }
  }
  function discovery(stop,actions,secondary){
    if(!state.discovery)return;
    if(stop.kind==='diaries')return diaries(actions,secondary);
    if(stop.kind==='profile')return profile(actions,secondary);
    prefillBox(stop);currentAnswer(stop);
    if(stop.kind==='hours')return hours(stop,actions,secondary);
    choices(stop,actions);unsure(secondary);
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
  // The pinned bar shows the question and buttons; anything after them (More
  // options) stays below its fold until the bar itself is scrolled.
  function pin(){
    if(!navigation)return;navigation.style.maxHeight='';navigation.scrollTop=0;
    var actions=navigation.querySelector(':scope > .guided-actions');if(!actions||!navigation.querySelector(':scope > .guided-more'))return;
    var box=navigation.getBoundingClientRect(),end=actions.getBoundingClientRect().bottom;
    navigation.style.maxHeight=Math.ceil(end-box.top+parseFloat(getComputedStyle(navigation).paddingBottom))+'px';
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
    pin();
  }
  function startGuide(){
    if(!state){location.reload();return;}
    return action(state.guide&&state.guide.intro_ack?'resume':'start');
  }
  // A dragged assistant height is kept as a share of the viewport for the
  // session, so the next guide page opens at the size the owner chose.
  var SHEET_KEY='blam8-guided-sheet';
  function sheet(height){
    var ratio=null;
    try{if(height===undefined)ratio=parseFloat(sessionStorage.getItem(SHEET_KEY))||null;
      else if(height===null)sessionStorage.removeItem(SHEET_KEY);
      else{var vp=window.visualViewport;ratio=height/(vp?vp.height:innerHeight);sessionStorage.setItem(SHEET_KEY,String(ratio));}}catch(_){}
    if(ratio)document.documentElement.style.setProperty('--guided-sheet-height','calc(var(--guided-viewport,100dvh) * '+ratio+')');
    else document.documentElement.style.removeProperty('--guided-sheet-height');
    fit();
  }
  sheet();
  var api=window.Blam8GuidedSetup={ready:null,start:startGuide,sheet:sheet,active:active,exists:function(){return !!(state&&state.guide);},
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
    document.querySelectorAll('[data-guided-start]').forEach(function(b){b.disabled=false;b.addEventListener('click',startGuide);});return state;})();
  window.addEventListener('guided:submit-blocked',function(){note('These are proposed values. Use Accept in the card to save them, or Leave to restore the page.');});
  window.addEventListener('guided:projection-conflict',function(){note('Your restored edits differ from this preview. They are kept; reload the card and review before applying it.');});
  window.addEventListener('resize',fit);if(window.visualViewport)window.visualViewport.addEventListener('resize',fit);
  setInterval(async function(){
    if(!state||!state.guide||document.hidden||busy||panel&&(panel.busy||panel.editing)||
      document.activeElement&&/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName))return;
    try{var next=await request('/state');if(next.revision>state.revision||JSON.stringify(next.knowledge_conflicts)!==JSON.stringify(state.knowledge_conflicts)){state=next;clear();paint();if(panel)panel.load();resumeLocation();}}catch(_){}
  },2500);
})();
