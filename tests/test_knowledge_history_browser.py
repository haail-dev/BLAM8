"""Native DOM proof for compact Knowledge history and safe restored actions."""
from html import unescape
from pathlib import Path
import re
import subprocess
import pytest
from tests.test_knowledge_guidance_browser import chromium_binary
ROOT=Path(__file__).resolve().parents[1]

@pytest.mark.parametrize('width',[390,940])
def test_knowledge_history_is_collapsed_and_current_message_visible(tmp_path,width):
    source=(ROOT/'src/app/static/concierge.js').read_text(encoding='utf-8')
    render=source[source.index('  function renderAll()'):source.index('  var typingEl = null;')]
    css=(ROOT/'src/app/static/tw.css').read_text(encoding='utf-8')
    script='''
    const msgs=document.getElementById('messages');
    const ONB_STEP='knowledge', currentKnowledgeFormCard=null;
    const oldCard={id:'old',batch_id:'batch',version:'v1',focus_hash:'old',
      actions:[{action:'keep',label:'Keep note'}],selected:'keep',state:'prepared'};
    const log=[{role:'user',content:'Keep our original request.'},
      {role:'user',content:'Your choice: Original note — Reviewed to keep (review choice; not final Save).'},
      {role:'user',content:'Your choice: Other note — Leave for later (still pending; not discarded).'},
      {role:'assistant',content:'Stale correction: nothing changed yet.',knowledge_card:oldCard},
      {role:'assistant',content:'Stale correction: nothing changed yet.',knowledge_card:{...oldCard}},
      {role:'assistant',content:'Unconfirmed choice: please retry.',knowledge_card:{id:'failed',selected:'discard'}},
      {role:'assistant',content:'Review this current note.',knowledge_card:{id:'current'}}];
    const before=JSON.stringify(log);
    const clearPin=()=>{},refreshTitle=()=>{},pinToTop=()=>{},scrollDown=()=>{},appendTrailStrip=()=>{},appendNavLink=()=>{};
    const caveatEl=()=>document.createElement('p'), greeting=()=>'Review your notes.';
    const knowledgeBatchSaved=()=>false,knowledgeCardCurrent=c=>c.id==='current';
    const bubbleEl=(role,text)=>{const el=document.createElement('p');el.textContent=text;return el};
    const knowledgeCardNode=()=>{const b=document.createElement('button');b.textContent='Keep note';return b};
    RENDER
    renderAll();
    const failures=[],check=(v,m)=>{if(!v)failures.push(m)};
    let history=msgs.querySelector('details');
    check(history&&!history.open,'history collapsed');
    check(history.textContent.includes('original request')&&history.textContent.includes('Stale correction'),'history retained');
    check(!history.querySelector('button'),'no historical actions');
    check(msgs.querySelectorAll('button').length===1,'one active card');
    check(msgs.lastElementChild.textContent==='Keep note','current controls visible');
    check(!document.body.innerText.includes('Stale correction'),'old instructions hidden');
    check(document.body.innerText.includes('Review this current note'),'current text visible');
    history.open=true;check(document.body.innerText.includes('Stale correction'),'history can be read');
    check(history.textContent.split('Stale correction: nothing changed yet.').length===2,'repeated prompt shown once');
    check(history.textContent.split('Your choice: Keep note').length===2,'confirmed owner choice shown once');
    check(history.textContent.includes('Your choice: Keep note.'),'plain decision label');
    check(history.textContent.includes('Your choice: Original note — Reviewed to keep.'),'old form receipt displayed plainly');
    check(history.textContent.includes('Your choice: Other note — Leave for later.'),'old deferred receipt displayed plainly');
    check(!history.textContent.includes('review choice; not final Save')&&!history.textContent.includes('still pending; not discarded'),'state metadata hidden');
    check(!history.textContent.includes('Your choice: Leave'),'unconfirmed selection is not an owner decision');
    const status=document.createElement('p');status.dataset.knowledgeSavedStatus='';status.textContent='Knowledge saved';document.body.appendChild(status);
    renderAll();history=msgs.querySelector('details');
    check(!history.open&&!msgs.querySelector('button'),'saved history collapsed without controls');
    history.open=true;check(history.textContent.includes('Your choice: Keep note'),'decision survives saved-state render');
    check(msgs.lastElementChild.textContent==='Knowledge saved','saved receipt current');
    check(JSON.stringify(log)===before,'transcript unchanged');
    check(document.documentElement.scrollWidth<=WIDTH,'no overflow');
    const result=document.createElement('pre');result.id='result';result.dataset.status=failures.length?'FAIL':'PASS';result.textContent=JSON.stringify(failures);document.body.appendChild(result);
    '''.replace('RENDER',render).replace('WIDTH',str(width))
    page=tmp_path/'page.html';page.write_text('<!doctype html><meta charset="utf-8"><style>'+css+'</style><body style="margin:0;width:'+str(width)+'px"><div id="messages"></div><script>'+script+'</script>',encoding='utf-8')
    wrapper=tmp_path/'frame.html'
    wrapper.write_text('<style>body{margin:0}iframe{border:0}</style><iframe id="frame" width="'+str(width)+'" height="900" src="page.html"></iframe><script>const timer=setInterval(()=>{const r=document.getElementById("frame").contentDocument.getElementById("result");if(r){document.body.appendChild(r.cloneNode(true));clearInterval(timer);}},20);</script>',encoding='utf-8')
    result=subprocess.run([chromium_binary(),'--headless=new','--no-sandbox','--disable-gpu','--no-first-run','--disable-background-networking','--host-resolver-rules=MAP * ~NOTFOUND','--user-data-dir='+str(tmp_path/'profile'),'--window-size='+str(max(width,500))+',1000','--allow-file-access-from-files','--virtual-time-budget=2000','--dump-dom',wrapper.as_uri()],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
    report=re.search(r'<pre id="result"[^>]*>(.*?)</pre>',result.stdout,re.S)
    assert result.returncode==0 and report and 'data-status="PASS"' in report.group(0),unescape(report.group(0)) if report else result.stderr[-1500:]
