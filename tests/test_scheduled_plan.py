import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

from app import scheduled_plan as plan, scheduled_plan_routes as routes
from app.auth import TenantCtx


def item(when='2026-10-10T08:30:00+00:00', freq='daily', **rule):
    return {'next_at':when, 'timezone':'Europe/London', 'recurrence':{'frequency':freq,**rule}}


def test_weekly_and_monthly_preserve_local_time_and_anchor():
    assert plan.next_occurrence(item('2026-10-24T08:30:00+00:00','weekly'))=='2026-10-31T09:30:00+00:00'
    feb=plan.next_occurrence(item('2027-01-31T09:00:00+00:00','monthly',anchor_day=31))
    assert feb=='2027-02-28T09:00:00+00:00'
    assert plan.next_occurrence(item(feb,'monthly',anchor_day=31))=='2027-03-31T08:00:00+00:00'


def test_dst_gap_and_fold():
    with pytest.raises(ValueError,match='daylight'):
        plan.local_time('2027-03-28T01:30','Europe/London')
    assert plan.local_time('2026-10-25T01:30','Europe/London').isoformat()=='2026-10-25T00:30:00+00:00'
    assert plan.next_occurrence(item('2027-03-27T01:30:00+00:00'))=='2027-03-28T01:30:00+00:00'
    assert plan.next_occurrence(item('2027-03-28T01:30:00+00:00',anchor_time='01:30:00'))=='2027-03-29T00:30:00+00:00'


def test_repeat_end_and_custom_interval():
    assert plan.next_occurrence(item(freq='once')) is None
    assert plan.next_occurrence(item(until='2026-10-10')) is None
    assert plan.next_occurrence(item('2026-10-05T08:00:00+00:00','custom',days=[0,2],interval=2))=='2026-10-07T08:00:00+00:00'
    assert plan.next_occurrence(item('2026-10-07T08:00:00+00:00','custom',days=[0,2],interval=2))=='2026-10-19T08:00:00+00:00'


@pytest.mark.parametrize('rule',[{'frequency':'hourly'},{'interval':0},{'frequency':'custom','days':[]},{'frequency':'custom','days':[9]}])
def test_bad_recurrence(rule):
    with pytest.raises(ValueError):plan.recurrence(rule)


def test_window_holidays_and_boundaries():
    cfg=plan.DEFAULTS.copy()
    assert not plan.window(cfg,datetime(2026,10,10,10,tzinfo=timezone.utc))
    assert plan.window(cfg,datetime(2026,10,12,8,tzinfo=timezone.utc))
    assert not plan.window(cfg,datetime(2026,10,12,16,tzinfo=timezone.utc))
    assert not plan.window({**cfg,'holidays':['2026-10-12']},datetime(2026,10,12,10,tzinfo=timezone.utc))


def test_only_current_active_owner_can_run(monkeypatch):
    monkeypatch.setattr(plan.db,'get_tenant',lambda _: {'comp_access':'indefinite'})
    monkeypatch.setattr(plan.db,'get_member',lambda *a:{'role':'staff','active':True})
    with pytest.raises(ValueError,match='permission'):plan.access('t','u')
    monkeypatch.setattr(plan.db,'get_member',lambda *a:{'role':'owner','active':False})
    with pytest.raises(ValueError,match='permission'):plan.access('t','u')
    monkeypatch.setattr(plan.db,'get_member',lambda *a:{'role':'owner','active':True})
    plan.access('t','u')


def test_financial_auto_is_rejected_before_lookup():
    with pytest.raises(ValueError,match='approval'):
        plan.validate_item('t',{'kind':'invoice','mode':'auto'},'u')


def test_contact_stop_cannot_be_bypassed(monkeypatch):
    monkeypatch.setattr(plan.db,'crm_get_contact',lambda *a:{'id':'c','email':'a@example.com','do_not_sequence':True})
    with pytest.raises(ValueError,match='do not contact'):plan.contact('t','c')


def test_dependency_waits_for_correct_invoice(monkeypatch):
    monkeypatch.setattr(plan.db,'get_invoice',lambda *a:None)
    with pytest.raises(ValueError,match='Waiting for payment'):
        plan.dependencies('t',{'dependency':{'kind':'after_payment','invoice_id':'i'}})
    monkeypatch.setattr(plan.db,'get_invoice',lambda *a:{'status':'paid'})
    plan.dependencies('t',{'dependency':{'kind':'after_payment','invoice_id':'i'}})


@pytest.fixture
def execution(monkeypatch):
    i={'id':'i','created_by':'u','kind':'email','payload':{},'mode':'auto'}
    prepared={'to':'a@example.com','subject':'Hello','mailbox_id':'b','sender':'me@example.com','body':'Hello'}
    r={'id':'r','item_id':'i','snapshot':i,'prepared':prepared,'scheduled_at':'2026-10-12T09:00:00Z','email_log_id':'l'}
    monkeypatch.setattr(plan.outreach,'get',lambda table,*a:r if table=='schedule_runs' else i)
    monkeypatch.setattr(plan,'access',lambda *a:None)
    monkeypatch.setattr(plan,'dependencies',lambda *a:None)
    monkeypatch.setattr(plan,'inspect_action',lambda *a:{k:v for k,v in prepared.items() if k!='body'})
    monkeypatch.setattr(plan.db,'effective_config',lambda *a:{'auto_send':True})
    monkeypatch.setattr(plan.db,'get_email_log_entry',lambda *a:{'status':'pending'})
    monkeypatch.setattr(plan.db,'update_email_log_draft',lambda *a:None)
    from app import concierge_booking_claims
    monkeypatch.setattr(concierge_booking_claims,'check',lambda *a:(None,None))
    sender=AsyncMock();monkeypatch.setattr(plan.mailbox,'send_mail',sender)
    updates=[]
    class Update:
        def update(self, fields):updates.append(fields);return self
        def eq(self, *a):return self
        def execute(self):return SimpleNamespace(data=[])
    monkeypatch.setattr(plan.db,'db',lambda:SimpleNamespace(table=lambda *a:Update()))
    return i,r,sender,updates


def test_late_pause_blocks_inbox_approval(monkeypatch,execution):
    monkeypatch.setattr(plan,'rpc',lambda *a,**k:'Scheduled plan is paused')
    with pytest.raises(ValueError,match='paused'):asyncio.run(plan.execute('t','r'))
    execution[2].assert_not_called()


def test_auto_requires_existing_mailbox_trust(monkeypatch,execution):
    monkeypatch.setattr(plan.db,'effective_config',lambda *a:{'auto_send':False})
    with pytest.raises(ValueError,match='approval'):asyncio.run(plan.execute('t','r',auto=True))
    execution[2].assert_not_called()


def test_unknown_provider_outcome_is_held(monkeypatch,execution):
    monkeypatch.setattr(plan,'rpc',lambda name,**k:'claimed' if name=='schedule_claim' else True)
    execution[2].side_effect=TimeoutError('provider timed out')
    with pytest.raises(ValueError,match='Outcome uncertain'):asyncio.run(plan.execute('t','r'))
    assert execution[3][-1]['status']=='unknown'
    execution[2].assert_awaited_once()


def test_approved_body_and_expected_identity_reach_provider(monkeypatch,execution):
    monkeypatch.setattr(plan,'rpc',lambda name,**k:'claimed' if name=='schedule_claim' else True)
    assert asyncio.run(plan.execute('t','r',body='Edited by owner'))=='sent'
    assert execution[2].call_args.args[3]=='Edited by owner'
    assert execution[2].call_args.kwargs['expected_sender']=='me@example.com'


def test_cross_tenant_reference_is_checked_at_creation(monkeypatch):
    monkeypatch.setattr(plan.db,'crm_get_contact',lambda cid,tid:None)
    with pytest.raises(ValueError,match='this business'):
        plan.validate_item('tenant-a',{'title':'Test','kind':'email','run_at':'2026-10-12T10:00',
            'payload':{'contact_id':'other-tenant-contact'}},'u')


def test_schedule_form_requires_csrf():
    app=FastAPI();app.include_router(routes.router)
    app.dependency_overrides[routes.owner]=lambda:TenantCtx(tenant_id='t',user_id='u',client=None)
    response=TestClient(app).post('/app/plan/settings',data={'enabled':'on'},follow_redirects=False)
    assert response.status_code==403


def test_calendar_renders_escaped_titles_and_draft_defaults():
    env=Environment(loader=ChoiceLoader([DictLoader({'app/base.html':'{% block content %}{% endblock %}'}),FileSystemLoader('src/app/templates')]),autoescape=True)
    env.globals['csrf_token']=lambda request:'token'
    today=datetime(2026,10,12).date()
    html=env.get_template('app/scheduled_plan.html').render(request=SimpleNamespace(),setting={'enabled':False,'config':plan.DEFAULTS},
        days=[{'date':today,'entries':[]}],start=today,span=7,today=today,
        items=[{'id':'i','title':'<script>bad()</script>','status':'active','next_at':None}],review=[],summaries=[],
        kinds=plan.KINDS,contacts=[],identities=[],invoices=[],quotes=[],campaigns=[],msg='',err='')
    assert 'Plan paused' in html and 'Draft for approval' in html
    assert '&lt;script&gt;bad()&lt;/script&gt;' in html
    assert 'name="enabled" checked' not in html
    assert 'data-plan-day="2026-10-12"' in html


def test_scheduled_history_is_not_customer_authority():
    from app.conversation_direction import direction
    assert direction({'kind':'scheduled_plan'})=='outbound'


def test_inbox_and_voice_approval_reenter_plan_gate(monkeypatch):
    from app import dashboard
    ctx=SimpleNamespace(tenant_id='t',client=None)
    entry={'id':'log','kind':'scheduled_plan','mailbox_id':'box'}
    monkeypatch.setattr(dashboard,'require_can',lambda *a:None)
    monkeypatch.setattr(dashboard,'_require_their_mail',lambda *a:None)
    monkeypatch.setattr(dashboard._guards,'require_admin',lambda *a:None)
    monkeypatch.setattr(dashboard.db,'get_email_log_entry',lambda *a,**k:entry)
    send=AsyncMock(return_value='sent');monkeypatch.setattr(plan,'send_draft',send)
    assert asyncio.run(dashboard.send_approved(ctx,'log','Reviewed',concierge=True))=='sent'
    send.assert_awaited_once_with('t',entry,'Reviewed',auto=False)


def test_two_sweeps_prepare_one_draft_and_never_send_draft_mode(monkeypatch):
    import copy
    from test_admin_outreach import Query
    class Q(Query):
        def lte(self,key,value):
            self.filters.append(lambda r:r.get(key) is not None and r[key]<=value);return self
        def lt(self,key,value):
            self.filters.append(lambda r:r.get(key) is not None and r[key]<value);return self
    t='t';due='2026-10-12T09:00:00+00:00'
    i={'id':'i','tenant_id':t,'next_at':due,'timezone':'UTC','recurrence':{'frequency':'once'},'revision':1,
       'kind':'email','title':'Hello','payload':{'brief':'Say hello'},'mode':'draft','status':'active','created_by':'u','catch_up':'run_late'}
    fake=SimpleNamespace(tables={'schedule_settings':[{'tenant_id':t,'enabled':True,'config':{**plan.DEFAULTS,'timezone':'UTC'}}],
                                'schedule_items':[i],'schedule_runs':[]})
    fake.table=lambda name:Q(fake,name)
    monkeypatch.setattr(plan.db,'db',lambda:fake)
    monkeypatch.setattr(plan,'now',lambda:datetime(2026,10,12,10,tzinfo=timezone.utc))
    monkeypatch.setattr(plan,'access',lambda *a:None)
    monkeypatch.setattr(plan,'dependencies',lambda *a:None)
    monkeypatch.setattr(plan,'inspect_action',lambda *a:{'to':'a@example.com','subject':'Hello','mailbox_id':'b','sender':'me@example.com'})
    monkeypatch.setattr(plan.db,'effective_config',lambda *a:{'auto_send':True})
    monkeypatch.setattr(plan.usage,'record_anthropic',lambda *a,**k:None)
    composer=AsyncMock(return_value=SimpleNamespace(text='Hello there',model='fake',usage={}))
    monkeypatch.setattr(plan.agent,'compose_outbound',composer)
    sender=AsyncMock();monkeypatch.setattr(plan.mailbox,'send_mail',sender)
    def rpc(name,**kwargs):
        if name=='schedule_reserve':
            assert not fake.tables['schedule_runs']
            r={'id':'r','tenant_id':t,'item_id':'i','scheduled_at':due,'snapshot':copy.deepcopy(i),
               'status':'preparing','token':kwargs['token'],'lease_until':'2026-10-12T10:05:00+00:00'}
            i['next_at']=None;fake.tables['schedule_runs'].append(r);return [copy.deepcopy(r)]
        if name=='schedule_prepare':
            fake.tables['schedule_runs'][0].update(status='ready',prepared=kwargs['data'],email_log_id='l');return True
        raise AssertionError(name)
    monkeypatch.setattr(plan,'rpc',rpc)
    asyncio.run(plan.run());asyncio.run(plan.run())
    assert len(fake.tables['schedule_runs'])==1
    assert fake.tables['schedule_runs'][0]['status']=='ready'
    composer.assert_awaited_once();sender.assert_not_called()


@pytest.mark.parametrize('kind',['invoice','quote'])
def test_document_draft_uses_existing_create_action(monkeypatch,kind):
    from app import concierge_actions
    target={'id':'c','email':'a@example.com'}
    monkeypatch.setattr(plan,'contact',lambda *a:target)
    monkeypatch.setattr(plan.db,'crm_contact_by_email',lambda *a:target)
    seen=[]
    def stage(action_kind,tid,client,action,member):
        seen.append(action)
        return 'Create draft',{'contact':action['contact'],'amount':25},['Draft only'],None
    monkeypatch.setattr(concierge_actions,'_run_stager',stage)
    prepared=plan.inspect_action('t',{'kind':kind,'title':'Agreed work','payload':{
        'document_action':'create','contact_id':'c','description':'Agreed work','amount':'25.00'}})
    assert prepared['action']['kind']==kind+'_create'
    assert seen[0]['contact']=='a@example.com' and prepared['body'] is None
