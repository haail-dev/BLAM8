"""Native page/card/guide/sync integration over real HTTP and disposable SQL."""
import json
from pathlib import Path
import os
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from uuid import uuid4
import pytest
from fastapi import Request, Response
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from app import dashboard, db, device_sync as sync
from app.auth import TenantCtx, require_onboarded, require_tenant
from app.main import app
from tests.test_guided_setup import cluster,database,journey,guide_journey,model,change,seed_reviewed_voice
from tests.test_device_sync import Store
from tests.test_knowledge_guidance_browser import chromium_binary,devtools_port

ROOT=Path(__file__).resolve().parents[1]


def test_grouped_setup_nudge_enters_the_same_guide():
    result=subprocess.run([shutil.which('node'),str(ROOT/'tests/replays/guided_setup_entry.mjs')],
        cwd=ROOT,capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr


@pytest.mark.parametrize('width,height,theme',[(390,844,'light'),(1280,900,'dark'),(320,640,'dark'),(768,900,'light')])
def test_real_diary_projections_edits_accept_and_device_restore(guide_journey,monkeypatch,tmp_path,width,height,theme):
    run_browser(guide_journey,monkeypatch,tmp_path,width,height,theme,'diary')


@pytest.mark.parametrize('area,width,height,theme',[('records',390,844,'dark'),('charges',1280,900,'light')])
def test_records_and_charges_real_native_decision_journeys(guide_journey,monkeypatch,tmp_path,area,width,height,theme):
    run_browser(guide_journey,monkeypatch,tmp_path,width,height,theme,area)


@pytest.mark.parametrize('entry',['handoff','handoff_assistant'])
def test_knowledge_handover_to_real_diary_without_intake_redirect(guide_journey,monkeypatch,tmp_path,entry):
    run_browser(guide_journey,monkeypatch,tmp_path,390,844,'light',entry)


def test_weekday_collection_projects_exact_members_without_serialising_proposals(guide_journey,monkeypatch,tmp_path):
    run_browser(guide_journey,monkeypatch,tmp_path,390,844,'light','hours')


def test_changed_knowledge_requires_visible_resolution_before_native_accept(guide_journey,monkeypatch,tmp_path):
    run_browser(guide_journey,monkeypatch,tmp_path,390,844,'light','knowledge_conflict')


def run_browser(j,monkeypatch,tmp_path,width,height,theme,area):
    if area.startswith('handoff'):
        from app import onboarding, onboarding_state
        from copy import deepcopy
        seed_reviewed_voice(j)
        from app import knowledge_review
        saved_facts=[dict(source='Reviewed facts',content='Confirmed business facts.',mailbox_id=None,chunks=1)]
        monkeypatch.setattr(knowledge_review,'load',lambda tid:dict(sources=saved_facts,reviews=[],version='saved-facts'))
        monkeypatch.setattr(db,'list_knowledge_sources',lambda *a,**k:saved_facts)
        context=onboarding_state.load(j.ctx);state=deepcopy(context['state']);state.pop('guide')
        onboarding_state.save(j.ctx,context['state'],state)
        j.execute('alter table tenant_config add column if not exists business_brief text')
        j.execute('update tenant_config set business_brief=%s where tenant_id=%s','We inspect equipment.',j.tenant)
        monkeypatch.setattr(db,'set_onboarded',lambda *a,**k:None)
        monkeypatch.setattr(db,'upsert_tenant_config',lambda *a,**k:None)
        for name in ('list_bookings','list_email_log','list_branches','list_pallet_types'):
            monkeypatch.setattr(db,name,lambda *a,**k:[])
        monkeypatch.setattr(db,'module_active',lambda tid,module,**kw:module in ('scheduling','crm'))
        monkeypatch.setattr(db,'is_enterprise',lambda *a,**k:False)
        monkeypatch.setattr(db,'get_member',lambda *a,**k:{'role':'owner'})
        # Exercise the actual incomplete-intake condition. Only the guided
        # orientation may render the Diary before its later intake step.
        monkeypatch.setattr(dashboard,'_intake_configured',lambda ctx:False)
        original_step=onboarding._step_ctx
        monkeypatch.setattr(onboarding,'_step_ctx',lambda *a,**kw:original_step(*a,**kw)|{'concierge_on':False})
        path='/onboarding/knowledge';view=onboarding.knowledge_get
        proposed=change('diary_settings',0,[dict(fields={'key':'stay_mode','value':False})])
    elif area=='hours':
        j.move('diary_settings/working_days');path='/app/diary/settings';view=dashboard.diary_settings
        proposed=change('diary_settings',0,[dict(fields={'key':'working_days','value':['mon','wed']})])
    elif area=='diary':
        j.move('diary_settings/min_notice_hours')
        path='/app/diary/settings';view=dashboard.diary_settings
        proposed=change('diary_settings',0,[dict(fields={'key':'min_notice_hours','value':24}),dict(fields={'key':'horizon_days','value':60})])
    elif area=='records':
        j.move('dependants/field');path='/app/diary/dependants';view=dashboard.diary_dependants
        proposed=change('dependants',0,[dict(name='group',fields={'label':'Item','plural':'Items','cardinality':'many'}),
            dict(name='field',fields={'kind':'Item','label':'Reference','type':'text','required':False,'durability':'once','gate':False,'hint':''})])
    else:
        from app import guided_setup
        j.move(next(s['id'] for s in guided_setup.registry().values() if s['page']=='invoice_settings' and len(s['catalogue'])>3))
        path='/app/invoices/settings';view=dashboard.invoice_settings_view
        choices={'show_vat':False,'vat_rate':0,'prices_include_vat':False,'show_business_details':False,
            'show_payment_terms':True,'payment_terms':'On completion','default_quote_valid_days':30}
        proposed=change('invoice_settings',0,[dict(fields={'key':k,'value':v}) for k,v in choices.items()])
        if area=='knowledge_conflict':
            from copy import deepcopy
            from app import onboarding_state
            j.execute('insert into invoice_settings(tenant_id,payment_terms) values(%s,%s)',j.tenant,'Existing saved terms')
            q=dict(id='knowledge.payment-question',canonical='setting.invoice_settings.payment_terms',state='answered',
                   answer='Within 14 days',evidence_revision='v1',source_review='review')
            monkeypatch.setattr(onboarding_state,'knowledge_candidates',lambda _: [deepcopy(q)])
            j.guide_action('retry_knowledge')
            q.update(answer='Before work begins',evidence_revision='v2')
    j.execute((ROOT/'db/migrations/20261004_setup_forms.sql').read_text())
    j.execute((ROOT/'db/migrations/20261020_device_sync.sql').read_text())
    user=str(uuid4());ctx=TenantCtx(j.tenant,None,user)
    async def auth(request:Request,response:Response):
        request.state.tenant_id=j.tenant;request.state.user_email='owner@example.test'
        return ctx
    app.dependency_overrides[require_onboarded]=auth
    app.dependency_overrides[require_tenant]=auth
    monkeypatch.setattr(sync,'require_tenant',auth)
    monkeypatch.setattr(sync.settings,'device_sync_enabled',True)
    class Combined(Store):
        def rpc(self,name,args):
            if name in ('save_onboarding_state','change_setup_card'):return j.rpc.rpc(name,args)
            return super().rpc(name,args)
    monkeypatch.setattr(db,'db',lambda:Combined(((j.rpc.driver,j.rpc.dsn),j.tenant,user)))
    monkeypatch.setattr(db,'calendar_connected',lambda *a,**k:False)
    monkeypatch.setattr(db,'list_mailboxes',lambda *a,**k:[dict(tenant_id=j.tenant,address='owner@example.test')])
    monkeypatch.setattr(db,'setup_has_booking_history',lambda *a,**k:False)
    model(monkeypatch,proposed)
    original_ctx=dashboard._ctx
    monkeypatch.setattr(dashboard,'_ctx',lambda active,ctx,**kw:original_ctx(active,ctx,**kw)|{
        'guided_setup_meta':j.state().get('guide'),'scheduling_on':True,'concierge_on':True,
        'device_sync_enabled':lambda:True})
    def render(handler):
        def page(request:Request):
            request.state.tenant_id=j.tenant;request.state.user_email='owner@example.test'
            response=handler(request,ctx=ctx)
            response.body=response.body.replace(b'<html lang="en"',('<html data-theme="'+theme+'" lang="en"').encode())
            response.headers['content-length']=str(len(response.body))
            response.set_cookie('bl_csrf','test-csrf')
            return response
        return page
    views={path:view}
    if area.startswith('handoff'):views.update({'/app/diary/dependants':dashboard.diary_dependants,
        '/app/crm/fields':dashboard.crm_fields_page,'/app/crm/settings':dashboard.crm_settings_page,
        '/app/diary':dashboard.diary_view,'/app/diary/settings':dashboard.diary_settings})
    page_routes=[APIRoute(url,render(handler)) for url,handler in views.items()]
    for route in page_routes:app.router.routes.insert(0,route)
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):self.dispatch()
        def do_POST(self):self.dispatch()
        def log_message(self,*a):pass
        def dispatch(self):
            body=self.rfile.read(int(self.headers.get('content-length',0)))
            c=TestClient(app)
            try:result=c.request(self.command,self.path,content=body,headers=dict(self.headers),follow_redirects=False)
            finally:c.close()
            if self.path.startswith(('/device-sync/','/app/setup/guide/','/concierge/','/onboarding/')):
                try:incoming=json.loads(body) if body else {};outgoing=result.json()
                except ValueError:incoming={};outgoing={}
                requests.append(dict(path=self.path,method=self.command,status=result.status_code,
                    revision=incoming.get('revision'),result_revision=outgoing.get('revision'),operation=incoming.get('operation'),
                    patch_keys=sorted((incoming.get('patch') or {}).keys()),
                    channel=self.headers.get('X-Blam8-Channel'),workspace_revision=self.headers.get('X-Blam8-Revision')))
            self.send_response(result.status_code)
            for key,value in result.headers.multi_items():
                if key.lower() not in ('connection','transfer-encoding'):self.send_header(key,value)
            self.end_headers()
            try:self.wfile.write(result.content)
            except (BrokenPipeError,ConnectionAbortedError):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    profile=tmp_path/'profile'
    command=[chromium_binary(),'--headless=new','--no-sandbox','--disable-gpu','--disable-background-networking',
        '--disable-component-update','--disable-sync','--no-first-run','--no-proxy-server','--remote-debugging-port=0',
        '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1','--user-data-dir='+str(profile),
        f'http://127.0.0.1:{server.server_port}'+path]
    try:
        with (tmp_path/'chrome.log').open('w') as log:
            browser=subprocess.Popen(command,stdout=log,stderr=log)
            try:
                port=devtools_port(profile)
                with (tmp_path/'native.log').open('w') as native_log:
                    run=subprocess.run([shutil.which('node'),str(ROOT/'tests/replays/guided_setup_native.mjs'),str(port),str(server.server_port),
                        str(width),str(height),str(tmp_path),theme,area],stdout=native_log,stderr=subprocess.STDOUT,text=True,timeout=90)
                assert run.returncode==0,(tmp_path/'native.log').read_text(errors='replace')+'\n'+json.dumps(requests[-35:],indent=2)
            finally:
                if os.name=='nt':
                    subprocess.run(['taskkill','/PID',str(browser.pid),'/T','/F'],stdout=log,stderr=log,timeout=5)
                browser.kill();browser.wait(timeout=10)
        if area.startswith('handoff'):
            assert len([r for r in requests if r['operation']=='start'])==1
            assert len([r for r in requests if r['operation']=='resume'])==1
            from app import intake
            saved=j.row('scheduling_config',j.tenant)
            assert not intake.stay_mode(saved)
            assert j.state()['guide']['intro_ack'] is True
            assert {c:j.state()['capabilities'].get(c) for c in ('related_records','record_information','lifecycle')}=={
                'related_records':'deferred','record_information':'deferred','lifecycle':'deferred'}
            assert j.state()['guide']['decisions']['diary_settings/stay_mode']=='applied'
            assert next(a for a in j.state()['answers'].values() if a['question']=='activity.purpose')['value']=='We inspect equipment.'
        elif area=='hours':
            saved=j.row('scheduling_config',j.tenant)
            assert saved['working_days']=='mon,wed'
        elif area=='diary':
            saved=j.row('scheduling_config',j.tenant)
            assert saved['min_notice_hours']==24 and saved['horizon_days']==45
            assert j.state()['guide']['decisions']['diary_settings/min_notice_hours']=='applied'
        elif area=='records':
            saved=j.query('select to_jsonb(g) from requirement_groups g where tenant_id=%s',j.tenant,all=True)
            assert len(saved)==1 and saved[0][0]['fields'][0]['label']=='Owner reference'
        elif area=='knowledge_conflict':
            saved=j.row('invoice_settings',j.tenant)
            assert saved['payment_terms']=='Before work begins'
        else:
            saved=j.row('invoice_settings',j.tenant)
            assert saved['payment_terms']=='After completion' and saved['show_vat'] is False
        (tmp_path/'durable-state.json').write_text(json.dumps(dict(state=j.state(),settings=saved),indent=2),encoding='utf-8')
    finally:
        (tmp_path/'request-trace.json').write_text(json.dumps(requests,indent=2),encoding='utf-8')
        server.shutdown();server.server_close();worker.join(timeout=5)
        for route in page_routes:app.router.routes.remove(route)
