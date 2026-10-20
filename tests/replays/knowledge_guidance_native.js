/* Native DOM/FormData integration. No network, model, database or admission calls. */
(async function () {
  'use strict';
  var output=document.getElementById('native-result');
  function assert(value,message){if(!value)throw new Error(message);}
  try {
    var fixture=window.nativeFixture, replies=fixture.responses.slice(), requests=[];
    var f=document.querySelector('[data-knowledge-review]'), box=f.elements[fixture.matchName];
    var initialValue=box.value, changes=[], submits=0;
    assert(box instanceof HTMLInputElement && box.type==='checkbox','actual checkbox required');
    assert(!box.checked && !new FormData(f).has(box.name),'unchecked boxes must be omitted natively');
    box.addEventListener('change',function(){changes.push(box.checked);});
    f.addEventListener('submit',function(event){event.preventDefault();submits++;});
    window.fetch=async function(url,options){
      requests.push(JSON.parse(options.body));
      assert(replies.length>0,'unexpected request');
      return {ok:true,json:async function(){return replies.shift();}};
    };
    var guide=window.Blam8KnowledgeGuide;
    await guide.ask('');
    await guide.ask('Keep this note');
    var next=await guide.ask('Keep both notes');
    assert(box.checked,'keep_both patch must check overlap control');
    assert(box.value===initialValue,'configured submission value must be preserved');
    assert(changes[0]===true,'change listener must observe checked state');
    assert(new FormData(f).get(box.name)==='keep_both','native FormData must retain acknowledgment');
    assert(next.focus.id==='two','guide must advance to second note');
    await guide.ask('Keep this note');
    assert(requests[3].form[box.name]==='keep_both','next request must serialize overlap acknowledgment');
    assert(requests[3].focus_hash===next.focus_hash,'next request must carry matching advanced focus');
    assert(f.elements.action_one.value==='keep' && f.elements.action_two.value==='keep','select patches must persist');

    // Exercise typed empty/nonmatching patches without inventing a new backend action.
    var response=function(patches,hash){return {patches:patches,focus_hash:hash,focus:{kind:'note',id:'two'},reply:'Local patch fixture.'};};
    var patch={};patch[box.name]='';patch.content_two='Owner edited fee wording.';
    replies.push(response(patch,'cleared'));
    await guide.ask('Show the current note');
    assert(!box.checked && !new FormData(f).has(box.name),'empty patch must uncheck and omit field');
    assert(box.value===initialValue && changes[1]===false,'clearing preserves value before change event');
    assert(f.elements.content_two.value==='Owner edited fee wording.','textarea patch must retain text behavior');
    patch={};patch[box.name]='unrecognized';replies.push(response(patch,'unmatched'));
    await guide.ask('Show the current note');
    assert(!box.checked && box.value==='keep_both','nonmatching patch must not redefine checkbox value');
    patch={};patch[box.name]='keep_both';replies.push(response(patch,'checked-again'));
    await guide.ask('Show the current note');
    assert(box.checked,'matching patch must recheck');

    // The existing signature guard must see an in-flight manual checkbox edit.
    var resolveReply;
    window.fetch=function(){return new Promise(function(resolve){resolveReply=resolve;});};
    var pending=guide.ask('Keep this note');
    box.checked=false;box.dispatchEvent(new Event('change',{bubbles:true}));
    resolveReply({ok:true,json:async function(){return response({action_two:'discard'},'stale');}});
    var rejected=false;
    try{await pending;}catch(error){rejected=/review changed/i.test(error.message);}
    assert(rejected && f.elements.action_two.value==='keep','manual edit must reject stale response patches');
    assert(!guide.isCurrent('checked-again'),'manual edit must invalidate prior focus');
    assert(submits===0,'guide must never submit Save implicitly');
    output.dataset.status='PASS';
    output.textContent=JSON.stringify({requests:requests});
  } catch (error) {output.textContent='FAIL: '+error.message;}
})();
