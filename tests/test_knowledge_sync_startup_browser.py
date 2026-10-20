"""Native full assistant startup and conflict liveness; isolated fixture transport."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import threading
from types import SimpleNamespace

import pytest
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

from app import knowledge_guidance as guide, knowledge_review, onboarding, knowledge_source_evidence, knowledge_fidelity
from tests.test_knowledge_review import context, entry
from tests.test_knowledge_guidance_browser import chromium_binary

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('mode', ['resume', 'guide_conflict', 'transcript_conflict'])
def test_native_review_startup_and_terminal_conflict(tmp_path, mode):
    notes = [entry(str(i), 'Note '+str(i), 'Distinct wording '+str(i)+'.') for i in range(1, 6)]
    ctx, row = context(notes, brief=notes[0]['content'])
    row['payload']['questions'] = []
    source=knowledge_source_evidence.build([{'url':'https://example.test/','text':'Contact our team'}])
    row['payload'].update(source_snapshot=source['id'],source_units=knowledge_fidelity.inventory(source['pages']))
    choices = {'version': ctx['version'], **{'action_'+str(i): 'keep' for i in range(1, 5)}}
    for note in knowledge_review.review_view(ctx,row,choices)['entries']:
        for match in note['matches']: choices['match_'+match['key']]='keep_both'
    unit=row['payload']['source_units'][0]
    choices.update({'source_'+unit['id']:'exclude','source_reason_'+unit['id']:'This is an invitation to contact us, not a service fact.'})
    stale = guide.present(ctx, row, {'version': ctx['version']})
    resumed = guide.present(ctx, row, choices)
    assert stale['focus']['id'] == '1' and resumed['focus']['id'] == '5'
    env = Environment(loader=ChoiceLoader([
        DictLoader({'_theme_head.html': ''}),
        FileSystemLoader(ROOT / 'src/app/templates')]), autoescape=True)
    request = SimpleNamespace(state=SimpleNamespace(user_email='owner@example.test', tenant_id='offline'),
                              url=SimpleNamespace(path='/onboarding/knowledge'))
    page = env.get_template('onboarding/knowledge.html').render(
        **{**onboarding._step_ctx('knowledge'),'knowledge_source_review_enabled':True}, request=request, configured=True, asset_rev='offline',
        csrf_token=lambda request: 'offline', device_sync_enabled=lambda: True,
        pending_reviews=[knowledge_review.review_view(ctx, row, {'version': ctx['version']})],
        onboarding_identity=lambda request: {'mailbox':'owner@example.test','same':True},
        site_state={'state':'pending','pending_notes':5})
    fixture = {'mode':mode, 'version':ctx['version'], 'choices':choices, 'stale':stale, 'resumed':resumed}
    bootstrap = r'''
    const fixture=FIXTURE, requests=[];let state,armed=false,guideCalls=0,initializing=true;
    const nativeFetch=fetch;
    const response=(body,status=200)=>Promise.resolve(new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json'}}));
    window.EventSource=undefined;
    window.fetch=async function(url,options={}){
      url=new URL(url,location.href).pathname;
      if(url==='/device-sync/flow'){
        if(!state){
          const forms={};document.querySelectorAll('main form').forEach((f,i)=>{
            if(f.method.toLowerCase()!=='post')return;
            const values={};Array.from(f.elements).forEach(el=>{
              if(!el.name || /^(csrf_token|_sync_.*|version|operation)$/.test(el.name) || /^(file|password|submit|button)$/.test(el.type))return;
              if(!values[el.name])values[el.name]=[];
              if(!['checkbox','radio'].includes(el.type)||el.checked)values[el.name].push(el.value);
            });
            if(f.dataset.knowledgeReview)for(const [name,value] of Object.entries(fixture.choices))if(name!=='version')values[name]=[value];
            const action=new URL(f.action,location.href).pathname;
            forms[action+':'+i]={action,version:f.elements.version?.value||'',values};
          });
          const form=document.querySelector('[data-knowledge-review]');
          state={revision:1,payload:{path:location.pathname,form:forms,messages:[],
            guide:{review:form.dataset.knowledgeReview,version:fixture.version,data:fixture.stale}}};
          // Multiple readiness events while the initial GET is genuinely pending.
          await new Promise(resolve=>{setTimeout(()=>window.dispatchEvent(new Event('blam8:onboarding-readiness')),0);setTimeout(resolve,20);});
        }
        if(options.method==='POST'){
          const body=JSON.parse(options.body);requests.push({url,body});
          if(armed && fixture.mode==='transcript_conflict' && body.patch.messages)return response({detail:'Stale revision'},409);
          state={revision:state.revision+1,payload:{...state.payload,...body.patch}};
        }
        return response(state);
      }
      if(url.endsWith('/guide')){
        guideCalls++;const body=JSON.parse(options.body);requests.push({url,body});
        if(!Blam8DeviceSync.isReady() || Blam8DeviceSync.isRestoring())throw Error('Guide started before initialization completed');
        for(let n=1;n<=4;n++)if(body.form['action_'+n]!=='keep')throw Error('Guide saw partially restored choices');
        for(const [name,value] of Object.entries(fixture.choices))if(body.form[name]!==value)throw Error('Missing restored source/overlap choice: '+name);
        if(armed && fixture.mode==='guide_conflict')return response({detail:'Stale review'},409);
        return response({...fixture.resumed,patches:{}});
      }
      throw Error('Unexpected fixture request: '+url);
    };
    const pause=()=>new Promise(resolve=>setTimeout(resolve,25));
    const until=async check=>{for(let i=0;i<80;i++){if(check())return;await pause();}throw Error('Condition timed out: '+check);};
    const assert=(value,message)=>{if(!value)throw Error(message);};
    window.addEventListener('DOMContentLoaded',async()=>{
      try{
        await Blam8DeviceSync.ready;initializing=false;
        await until(()=>document.getElementById('bc-messages').textContent.includes('Note 5 of 5'));
        assert(guideCalls===1,'Startup must rederive current note exactly once');
        const actions=Array.from(document.querySelectorAll('[data-knowledge-guide-actions]')).at(-1);
        assert(JSON.stringify(Array.from(actions.querySelectorAll('button')).map(button=>button.textContent))===JSON.stringify(['Keep note','Leave out','Edit']),'Note actions must stay concise');
        assert(fixture.resumed.actions[0].message==='Keep this note' && fixture.resumed.actions[1].message==='Discard this note','Display labels must not change approval protocol');
        for(let n=1;n<=4;n++)assert(document.querySelector('[name=action_'+n+']').value==='keep','Restored decision lost');
        assert(document.querySelector('[name=action_5]').value==='','Current note must remain unapproved');
        // A genuinely newer normal poll hydrates every native field again.
        state={revision:state.revision+1,payload:{...state.payload,guide:{...state.payload.guide,data:fixture.resumed}}};
        await Blam8DeviceSync.refresh();window.dispatchEvent(new Event('blam8:onboarding-readiness'));await pause();
        assert(guideCalls===1,'Normal poll/readiness restarted review');
        const review=document.querySelector('[data-knowledge-review]'), serialized=Object.fromEntries(new FormData(review));
        for(const [name,value] of Object.entries(fixture.choices))assert(serialized[name]===value,'Later hydration lost '+name);
        const overlap=review.querySelector('[name^=match_]');
        assert(overlap.checked && overlap.value==='keep_both' && new FormData(review).get(overlap.name)==='keep_both','Native overlap value lost');
        assert(Blam8KnowledgeGuide.isCurrent(fixture.resumed.focus_hash,review.dataset.knowledgeReview,fixture.version),'Later hydration lost current focus');
        if(fixture.mode!=='resume'){
          armed=true;document.getElementById('bc-input').value='Please show the current note.';
          document.getElementById('bc-form').requestSubmit();
          await until(()=>!Blam8DeviceSync.isReady());
          // This timer cannot fire if resolved-ready callbacks starve the event loop.
          await pause();await pause();
          for(let i=0;i<5;i++)window.dispatchEvent(new Event('blam8:onboarding-readiness'));
          await pause();
          // Closing/reopening invokes Concierge's sibling automatic-start gate.
          document.getElementById('bc-close').click();document.getElementById('bc-bubble').click();await pause();
          assert(guideCalls===(fixture.mode==='guide_conflict'?2:1),'Conflict replayed guide request');
          assert(document.body.textContent.includes('changed') || document.body.textContent.includes('Reload'),'Conflict warning lost');
          assert(document.querySelector('[name=action_5]').value==='','Conflict inferred approval');
        }
        await nativeFetch('/browser-result',{method:'POST',body:JSON.stringify({status:'PASS',requests,guideCalls})});
      }catch(error){await nativeFetch('/browser-result',{method:'POST',body:JSON.stringify({status:'FAIL',error:String(error),requests,guideCalls})});}
    });
    '''.replace('FIXTURE', json.dumps(fixture))
    page = page.replace('<head>', '<head><script>'+bootstrap+'</script>')
    # Keep the full real application controllers; prevent unrelated asset/network activity.
    allowed = {'device_sync.js','knowledge_guidance.js','knowledge_source_review.js',
               'knowledge_onboarding_review.js','onboarding_journey.js','concierge.js'}
    def inline(match):
        name = match.group(1)
        return '<script defer src="/fixture/'+name+'"></script>' if name in allowed else ''
    page = re.sub(r'<script\b[^>]*src="/static/([^?"/]+)[^"]*"[^>]*></script>', inline, page)
    page = re.sub(r'<link\b[^>]*>', '', page)
    reports=[];completed=threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_args): pass
        def do_GET(self):
            if self.path=='/onboarding/knowledge': body=page.encode()
            elif self.path.startswith('/fixture/') and self.path[9:] in allowed:
                body=(ROOT/'src/app/static'/self.path[9:]).read_bytes()
            else: self.send_error(404);return
            self.send_response(200);self.end_headers();self.wfile.write(body)
        def do_POST(self):
            assert self.path=='/browser-result'
            reports.append(json.loads(self.rfile.read(int(self.headers['content-length']))));completed.set()
            self.send_response(200);self.end_headers()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    command=[chromium_binary(),'--headless=new','--no-sandbox','--disable-gpu',
             '--disable-background-networking','--disable-component-update','--disable-sync',
             '--no-first-run','--no-proxy-server','--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
             '--user-data-dir='+str(tmp_path/'profile'),f'http://127.0.0.1:{server.server_port}/onboarding/knowledge']
    try:
        with (tmp_path/'browser.log').open('w') as output:
            browser=subprocess.Popen(command,stdout=output,stderr=output)
            try: assert completed.wait(8), 'Browser event loop did not complete: '+(tmp_path/'browser.log').read_text(errors='replace')[-500:]
            finally:
                if os.name=='nt':subprocess.run(['taskkill','/PID',str(browser.pid),'/T','/F'],stdout=output,stderr=output,timeout=10)
                else:browser.terminate()
                browser.wait(timeout=10)
    finally:server.shutdown();server.server_close();worker.join(timeout=5)
    assert reports[0]['status']=='PASS', reports[0]
    # Replay the actual native startup form through the real planner, not just queued transport.
    first_request=next(item['body'] for item in reports[0]['requests'] if item['url'].endswith('/guide'))
    _, current=guide.current(ctx,row,first_request['form'])
    assert current['kind']=='note' and current['id']=='5'
    assert reports[0]['guideCalls']==(2 if mode=='guide_conflict' else 1)
