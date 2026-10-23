"""Native shipped Voice and Assistant controls, with only HTTP responses stubbed."""
from html import unescape
import json
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader
from app import onboarding, voice_completion
from tests.test_knowledge_guidance_browser import chromium_binary

ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('theme',['light','dark'])
@pytest.mark.parametrize('width',[390,1440])
def test_voice_both_continues_trimmed_validation_visible_fill_and_save_failure(tmp_path,theme,width):
    env=Environment(loader=FileSystemLoader(ROOT/'src/app/templates'),autoescape=True)
    request=SimpleNamespace(state=SimpleNamespace(tenant_id='offline',user_email='owner@example.test'),url=SimpleNamespace(path='/onboarding/voice'))
    html=env.get_template('onboarding/voice.html').render(**onboarding._step_ctx('voice'),
        request=request,asset_rev='offline',cfg={},profile=None,learning_status='manual',
        voice_required=voice_completion.REQUIRED,csrf_token=lambda request:'offline',device_sync_enabled=lambda:False,
        onboarding_identity=lambda request:{'mailbox':None,'same':False})
    html=re.sub(r'<script\b[^>]*\bsrc=[^>]*>.*?</script>','',html,flags=re.S)
    html=re.sub(r'<link\b[^>]*>','',html)
    html=html.replace('<head>','<head><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; script-src \'unsafe-inline\'">'
        +'<style>'+(ROOT/'src/app/static/tw.css').read_text()+'</style>')
    prelude='''
    document.documentElement.dataset.theme=THEME;
    sessionStorage.clear();
    var posts=0;
    window.fetch=async function(url,options){
      if(String(url).includes('/concierge/onboard'))return {ok:true,json:async()=>({reply:'Review your suggested values.',
        form_fill:{agent_name:'Office team',tone:'Warm and professional',sign_off:'Kind regards, The team'},ready_to_advance:true})};
      posts++;return {ok:false,status:503,json:async()=>({detail:'Synthetic save failure'})};
    };
    '''.replace('THEME',json.dumps(theme))
    scripts='\n'.join((ROOT/'src/app/static'/name).read_text(encoding='utf-8') for name in ['onboarding_journey.js','concierge.js','voice_completion.js'])
    assertions=r'''
    document.addEventListener('DOMContentLoaded',async function(){
      const failures=[];const check=(v,m)=>{if(!v)failures.push(m)};
      const wait=()=>new Promise(r=>setTimeout(r,50));
      try{
        await wait();document.getElementById('bc-bubble').click();
        const form=document.getElementById('voice-form'),page=form.querySelector('[data-onb-continue]');
        const panel=()=>document.querySelector('#bc-cta button');
        check(page.disabled && panel() && panel().disabled,'Empty: both Continue controls disabled');
        ['agent_name','tone','sign_off'].forEach(k=>{form.elements[k].value=' \n\t ';form.elements[k].dispatchEvent(new Event('input',{bubbles:true}));});
        check(page.disabled && panel().disabled,'Whitespace: both Continue controls disabled');
        form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));await wait();
        check(posts===0,'Whitespace cannot reach save transport');
        const input=document.getElementById('bc-input');input.value='Use Office team, warm and professional, Kind regards, The team.';
        document.getElementById('bc-form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));
        for(let i=0;i<30 && !form.elements.agent_name.value.includes('Office');i++)await wait();
        check(form.elements.agent_name.value==='Office team' && form.elements.sign_off.value.includes('The team'),'Assistant fill visible');
        check(!page.disabled && panel() && !panel().disabled,'Valid required fields: both Continue controls enabled');
        check(form.elements.voice_notes.value==='','Extra voice notes optional');
        panel().click();await wait();
        check(posts===1,'Assistant Continue uses one real form save');
        check(form.elements.agent_name.value==='Office team' && !page.disabled,'Failed save preserves edits and permits retry');
        check(document.getElementById('voice-completion-status').textContent.includes('could not be saved'),'Failure explanation visible');
        check(!Array.from(document.querySelectorAll('a,button')).some(e=>e.textContent.trim()==='Skip'),'No Voice Skip');
        check(document.documentElement.scrollWidth<=innerWidth+1,'No horizontal overflow');
        document.getElementById('bc-close').click();form.elements.tone.focus();
        check(getComputedStyle(form.elements.tone).outlineStyle!=='none' || getComputedStyle(form.elements.tone).boxShadow!=='none','Keyboard focus visible');
      }catch(e){failures.push(e.stack || String(e));}
      const out=document.getElementById('voice-result');out.textContent=JSON.stringify({failures,width:innerWidth});
    });
    '''
    page=tmp_path/'content.html'
    page.write_text(html.replace('</body>','<pre id="voice-result">PENDING</pre><script>'+prelude+scripts+assertions+'</script></body>'),encoding='utf-8')
    frame=tmp_path/'frame.html'
    frame.write_text('<!doctype html><style>body{margin:0}iframe{display:block;border:0}</style>'+f'<iframe width="{width}" height="1100" src="content.html"></iframe><script>setInterval(function(){{var r=document.querySelector("iframe").contentDocument.getElementById("voice-result");if(r && r.textContent!=="PENDING" && !document.getElementById("voice-result"))document.body.appendChild(r.cloneNode(true));}},50)</script>',encoding='utf-8')
    result=subprocess.run([chromium_binary(),'--headless=new','--no-sandbox','--disable-gpu',
        '--allow-file-access-from-files','--disable-background-networking','--disable-component-update','--disable-sync',
        '--disable-default-apps','--no-first-run','--no-default-browser-check','--no-proxy-server',
        '--host-resolver-rules=MAP * ~NOTFOUND','--user-data-dir='+str(tmp_path/'profile'),
        '--virtual-time-budget=5000','--dump-dom',frame.as_uri()],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
    report=re.search(r'<pre id="voice-result"[^>]*>(.*?)</pre>',result.stdout,re.S)
    assert result.returncode==0 and report,result.stderr[-2000:]+result.stdout[-1000:]
    measured=json.loads(unescape(report.group(1)))
    assert measured['width']==width,measured
    assert not measured['failures'],measured
