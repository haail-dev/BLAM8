import asyncio
import base64
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import dashboard, mailbox, tenant_outreach as out, tenant_outreach_routes as routes
from app.auth import TenantCtx, require_onboarded, require_subscription
from app.main import app
from app.security import require_csrf
from test_admin_outreach import Query
from test_crm_sequences import _stub_common


@pytest.mark.parametrize('change',[
 {'daily_cap':0},{'daily_cap':True},{'min_gap_seconds':1},{'timezone':'Moon/Base'},
 {'working_days':[]},{'working_days':[True]},{'holidays':['bad']},{'mode':'unrestricted'},
 {'start_time':'18:00'},{'end_time':'25:00'},{'reply_to':['bad']},
])
def test_config_rejects_unsafe_or_unknown_settings(change):
    with pytest.raises(ValueError): out.validate_config(change)


def test_draft_default_and_timezone_windows():
    cfg=out.validate_config({})
    assert cfg['mode']=='draft'
    assert out.in_window(cfg,datetime(2026,10,12,8,tzinfo=timezone.utc))
    assert not out.in_window(cfg,datetime(2026,10,26,8,tzinfo=timezone.utc))
    assert not out.in_window(cfg,datetime(2026,10,10,12,tzinfo=timezone.utc))
    assert not out.in_window(cfg | {'holidays':['2026-10-12']},datetime(2026,10,12,12,tzinfo=timezone.utc))


@pytest.fixture
def target():
    return {'name':'A Person','email':'contact@example.com','basis':'corporate','company_number':'12345678',
            'evidence':'Company register checked','email_source':'https://example.com/contact',
            'email_verified':'true','soft_match':'false'}


@pytest.mark.parametrize('change',[
 {'email':'a@example.com\nBcc:other@example.com'}, {'email':'a@example.com,b@example.com'},
 {'basis':'assumed'},{'company_number':''},{'evidence':''},{'email_source':''},
 {'email_verified':'yes'}, {'soft_match':'no'},
])
def test_recipient_requires_recorded_evidence(target,change):
    with pytest.raises(ValueError): out.validate_target(target | change)


def test_permission_can_include_noncorporate_recipient(target):
    row=out.validate_target(target | {'basis':'permission','company_number':'','evidence':'Requested product updates, 10 Oct 2026'})
    assert row['email_verified'] is True and row['soft_match'] is False


class Fake:
    def __init__(self):
        self.tables={}
        self.claim='claimed'
        self.calls=[]
    def table(self,name):return Query(self,name)
    def rpc(self,name,params):
        self.calls.append((name,params))
        if name=='outreach_claim':
            if self.claim=='claimed': self.tables['outreach_messages'][0]['status']='sending'
            data=self.claim
        elif name=='outreach_complete':
            self.tables['outreach_messages'][0]['status']='sent';data=None
        else:raise AssertionError(name)
        return SimpleNamespace(execute=lambda:SimpleNamespace(data=data))


@pytest.fixture
def fake(monkeypatch):
    f=Fake();monkeypatch.setattr(out.db,'db',lambda:f)
    return f


def test_import_validates_all_before_writing_and_never_resets_existing(fake,monkeypatch,target):
    fake.tables['outreach_lists']=[{'id':'list','tenant_id':'t'}]
    created=[]
    monkeypatch.setattr(out.db,'crm_get_or_create_contact',lambda *a,**k:created.append(a) or {'id':'contact'})
    with pytest.raises(ValueError):out.import_targets('t','list',[target,target | {'email':'invalid'}])
    assert created==[]
    fake.tables['outreach_targets']=[{'id':'old','tenant_id':'t','list_id':'list','email':target['email'],'email_verified':False}]
    assert out.import_targets('t','list',[target])==0
    assert fake.tables['outreach_targets'][0]['email_verified'] is False
    assert created==[]


def test_import_cannot_reference_another_business_list(fake,target):
    fake.tables['outreach_lists']=[{'id':'list','tenant_id':'other'}]
    with pytest.raises(ValueError,match='this business'):out.import_targets('t','list',[target])


def test_sync_exact_stops_and_structured_bounces_without_domain_guessing(fake,monkeypatch):
    fake.tables['outreach_targets']=[{'id':'a','tenant_id':'t','email':'one@example.com'},
                                     {'id':'b','tenant_id':'t','email':'two@example.com'}]
    fake.tables['email_log']=[
        {'id':'1','tenant_id':'t','kind':'inbound','from_address':'one@example.com','body_in':'Please stop.'},
        {'id':'2','tenant_id':'t','kind':'inbound','from_address':'someone@example.com','body_in':'stop'},
        {'id':'3','tenant_id':'t','kind':'inbound','from_address':'two@example.com','body_in':'Thanks\nOn Monday you wrote:\nReply stop'},
        {'id':'4','tenant_id':'other','kind':'inbound','from_address':'two@example.com','body_in':'stop'},
        {'id':'5','tenant_id':'t','kind':'inbound','from_address':'mailer-daemon@provider.com',
         'body_in':'Final-Recipient: rfc822; two@example.com\nAction: failed'},
    ]
    calls=[];monkeypatch.setattr(out,'suppress',lambda *a:calls.append(a))
    out.sync('t')
    assert calls==[('t','one@example.com','Stop requested'),('t','two@example.com','Delivery report: bounced')]


@pytest.fixture
def send(fake,monkeypatch):
    fake.tables['outreach_messages']=[{'id':'msg','tenant_id':'t','email_log_id':'log','campaign_id':'campaign',
        'email':'contact@example.com','mailbox_id':'box','sender_address':'sender@example.com','status':'draft'}]
    monkeypatch.setattr(out,'sync',lambda tid:None)
    monkeypatch.setattr(out,'identity',lambda *a:{'id':'box','reply':'sender@example.com','sender':'sender@example.com'})
    monkeypatch.setattr(out.db,'effective_config',lambda *a:{'auto_send':False})
    monkeypatch.setattr(out,'outcome',lambda *a:None)
    deliver=AsyncMock();monkeypatch.setattr(out.mailbox,'send_mail',deliver)
    return {'id':'log','tenant_id':'t','kind':'tenant_outreach','subject':'Hello','draft_reply':'Draft'},deliver


def test_approval_cannot_remove_stop_line_or_headers(send,fake):
    entry,deliver=send
    assert asyncio.run(out.send_draft('t',entry,'Edited message'))=='sent'
    args,kw=deliver.call_args
    assert out.STOP_LINE in args[3]
    assert kw['outreach_headers']['List-Unsubscribe']=='<mailto:sender@example.com?subject=stop>'
    assert kw['mailbox_id']=='box' and kw['expected_sender']=='sender@example.com'
    assert fake.tables['outreach_messages'][0]['status']=='sent'


@pytest.mark.parametrize('reason',['Campaign is paused','Outside working window','Recipient must not be contacted','Shared outreach cap reached'])
def test_late_send_gate_holds_manual_approval(send,fake,reason):
    entry,deliver=send;fake.claim=reason
    with pytest.raises(HTTPException) as err:asyncio.run(out.send_draft('t',entry,'Edited message'))
    assert err.value.detail==reason
    deliver.assert_not_called()


def test_mailbox_auto_permission_cannot_be_bypassed(send,fake):
    entry,deliver=send
    with pytest.raises(HTTPException,match='requires draft approval'):asyncio.run(out.send_draft('t',entry,'Draft',auto=True))
    deliver.assert_not_called();assert fake.calls==[]


def test_ambiguous_provider_result_is_held_without_retry(send,fake):
    entry,deliver=send;deliver.side_effect=TimeoutError()
    with pytest.raises(HTTPException,match='uncertain'):asyncio.run(out.send_draft('t',entry,'Draft'))
    assert fake.tables['outreach_messages'][0]['status']=='unknown'
    assert not any(n=='outreach_complete' for n,p in fake.calls)


def test_inbox_and_voice_approval_use_outreach_boundary(monkeypatch):
    ctx=SimpleNamespace(tenant_id='t',client=None)
    entry={'id':'log','kind':'tenant_outreach','mailbox_id':'box'}
    monkeypatch.setattr(dashboard,'require_can',lambda *a:None)
    monkeypatch.setattr(dashboard,'_require_their_mail',lambda *a:None)
    monkeypatch.setattr(dashboard.db,'get_email_log_entry',lambda *a,**k:entry)
    send=AsyncMock(return_value='sent');monkeypatch.setattr(out,'send_draft',send)
    assert asyncio.run(dashboard.send_approved(ctx,'log','Reviewed',concierge=True))=='sent'
    send.assert_awaited_once_with('t',entry,'Reviewed',auto=False)


@pytest.mark.parametrize('sent',[False,True])
def test_existing_sequence_advances_only_after_outreach_sent(monkeypatch,sent):
    from app import crm_sequences as seq
    _stub_common(monkeypatch)
    monkeypatch.setattr(seq.db,'crm_get_sequence',lambda *a:{'id':'seq','status':'active','outreach_managed':True})
    monkeypatch.setattr(seq.db,'crm_list_steps',lambda *a:[{'action_type':'send_email','exit_on_reply':True},
                                                           {'action_type':'send_email','delay_days':3}])
    advance=[];reschedule=[]
    monkeypatch.setattr(seq.db,'crm_advance_enrollment',lambda *a,**k:advance.append(k))
    monkeypatch.setattr(seq.db,'crm_reschedule_enrollment',lambda *a,**k:reschedule.append(a))
    monkeypatch.setattr(out,'process_step',AsyncMock(return_value=sent))
    result=asyncio.run(seq._process_one({'id':'e','tenant_id':'t','sequence_id':'seq','contact_id':'c',
        'enrolled_at':'2026-10-01T00:00:00Z','current_step':0}))
    assert result==('advanced' if sent else 'outreach_held')
    assert bool(advance)==sent
    assert bool(reschedule)!=sent


def test_stop_on_reply_cannot_be_disabled_on_managed_sequence(monkeypatch):
    from app import crm_sequences as seq
    _stub_common(monkeypatch)
    monkeypatch.setattr(seq.db,'crm_get_sequence',lambda *a:{'id':'seq','status':'active','outreach_managed':True})
    monkeypatch.setattr(seq.db,'crm_list_steps',lambda *a:[{'action_type':'send_email','exit_on_reply':False}])
    monkeypatch.setattr(seq.db,'crm_contact_replied_since',lambda *a:True)
    exits=[];monkeypatch.setattr(seq.db,'crm_exit_enrollment',lambda *a,**k:exits.append(k))
    assert asyncio.run(seq._process_one({'id':'e','tenant_id':'t','sequence_id':'seq','contact_id':'c'}))=='replied'
    assert exits==[{'reason':'replied'}]


@pytest.mark.parametrize('provider',['google','microsoft'])
def test_real_mime_contains_standard_unsubscribe_and_reply_headers(monkeypatch,provider):
    monkeypatch.setattr(mailbox,'_access',AsyncMock(return_value=('token',provider,{'address':'sender@example.com'})))
    captured=[]
    monkeypatch.setattr(mailbox.gmail,'_send_raw',AsyncMock(side_effect=lambda access,msg:captured.append(msg.as_bytes())))
    class Client:
        def __init__(self,**kw):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
        async def post(self,url,**kw):
            assert kw['headers']['Content-Type']=='text/plain'
            captured.append(base64.b64decode(kw['content']))
            return SimpleNamespace(raise_for_status=lambda:None)
    monkeypatch.setattr(mailbox.httpx,'AsyncClient',Client)
    asyncio.run(mailbox.send_mail('t','to@example.com','Subject','Body',mailbox_id='box',expected_sender='sender@example.com',
        outreach_headers={'Reply-To':'sender@example.com','List-Unsubscribe':'<mailto:sender@example.com?subject=stop>'}))
    msg=BytesParser(policy=policy.default).parsebytes(captured[0])
    assert msg['List-Unsubscribe']=='<mailto:sender@example.com?subject=stop>'
    assert msg['Reply-To']=='sender@example.com'
    assert msg['From']=='sender@example.com' and msg['To']=='to@example.com'


def test_changed_sender_refuses_before_provider(monkeypatch):
    monkeypatch.setattr(mailbox,'_access',AsyncMock(return_value=('token','google',{'address':'different@example.com'})))
    deliver=AsyncMock();monkeypatch.setattr(mailbox.gmail,'_send_raw',deliver)
    with pytest.raises(mailbox.MailNotSentBeforeRequest):
        asyncio.run(mailbox.send_mail('t','to@example.com','Subject','Body',expected_sender='sender@example.com'))
    deliver.assert_not_called()


@pytest.fixture
def client(monkeypatch):
    ctx=TenantCtx(tenant_id='t',user_id='u',client=None)
    app.dependency_overrides[require_onboarded]=lambda:ctx
    app.dependency_overrides[require_subscription]=lambda:ctx
    monkeypatch.setattr(routes.guards,'require_admin',lambda *a:None)
    monkeypatch.setattr(routes.db,'visible_modules',lambda *a,**k:{'crm'})
    monkeypatch.setattr(routes.db,'module_active',lambda *a,**k:True)
    try:yield TestClient(app)
    finally:app.dependency_overrides.clear()


def test_route_requires_csrf(client):
    assert client.post('/app/outreach/settings',data={'enabled':'on','daily_cap':'100'}).status_code==403


def test_management_route_denies_staff(client,monkeypatch):
    def denied(*a):raise HTTPException(403,'Only managers')
    monkeypatch.setattr(routes.guards,'require_admin',denied)
    assert client.get('/app/outreach').status_code==403


def test_preview_module_cannot_enable_outreach(client,monkeypatch):
    app.dependency_overrides[require_csrf]=lambda:None
    monkeypatch.setattr(routes.db,'module_active',lambda *a,**k:False)
    assert client.post('/app/outreach/settings',data={'enabled':'on','daily_cap':'100'}).status_code==403


def test_prepare_reuses_one_inbox_draft_and_honours_mailbox_draft_mode(fake,monkeypatch):
    cfg=out.validate_config({'mode':'auto'})
    c={'id':'campaign','tenant_id':'t','sequence_id':'seq','list_id':'list','mailbox_id':'box','enabled':True,'config':cfg}
    fake.tables.update(outreach_campaigns=[c],outreach_settings=[{'tenant_id':'t','enabled':True}],
        outreach_targets=[{'id':'target','tenant_id':'t','list_id':'list','contact_id':'contact','email':'recipient@example.com'}])
    monkeypatch.setattr(out,'sync',lambda *a:None)
    monkeypatch.setattr(out,'ready_target',lambda *a:True)
    monkeypatch.setattr(out,'identity',lambda *a:{'id':'box','sender':'sender@example.com','reply':'sender@example.com'})
    monkeypatch.setattr(out.db,'effective_config',lambda *a:{'auto_send':False})
    monkeypatch.setattr(out.usage,'record_anthropic',lambda *a,**k:None)
    composer=AsyncMock(return_value=SimpleNamespace(text='An introduction.',model='fake',usage={}))
    monkeypatch.setattr(out.agent,'compose_outbound',composer)
    sender=AsyncMock();monkeypatch.setattr(out.mailbox,'send_mail',sender)
    def rpc(name,p):
        if name=='outreach_begin_step':
            data=fake.tables.setdefault('outreach_messages',[])
            if not data:
                data.append({'id':'m','tenant_id':'t','status':'preparing','work_token':p['p_token'],'campaign_id':'campaign'})
        elif name=='outreach_finish_draft':
            assert out.STOP_LINE in p['p_body']
            fake.tables['outreach_messages'][0].update(status='draft',email_log_id='log')
            data='log'
        else:raise AssertionError(name)
        return SimpleNamespace(execute=lambda:SimpleNamespace(data=data))
    monkeypatch.setattr(fake,'rpc',rpc)
    args=({'id':'enr','tenant_id':'t','current_step':0},{'id':'seq'},
          {'action_type':'send_email','exit_on_reply':True,'template_subject':'Hello','template_body':'Introduce us'},
          {},{'id':'contact'})
    assert asyncio.run(out.process_step(*args)) is False
    assert asyncio.run(out.process_step(*args)) is False
    composer.assert_awaited_once();sender.assert_not_called()
    assert c['last_result']=='Awaiting draft approval'


def test_campaign_forms_render_and_escape_recipient_evidence():
    from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader
    env=Environment(loader=ChoiceLoader([DictLoader({'app/base.html':'{% block content %}{% endblock %}'}),
                                         FileSystemLoader('src/app/templates')]),autoescape=True)
    env.globals['csrf_token']=lambda request:'test-token'
    html=env.get_template('app/outreach.html').render(request=SimpleNamespace(query_params={}),
        outreach_settings={'enabled':False,'daily_cap':100},
        lists=[{'id':'list','name':'<script>unsafe</script>','targets':[]}],
        campaigns=[],sequences=[],identities=[{'id':'box','address':'sender@example.com'}],
        defaults=out.DEFAULTS,contacts=[],msg='',err='')
    assert '&lt;script&gt;unsafe&lt;/script&gt;' in html and '<script>unsafe</script>' not in html
    assert 'action="/app/outreach/settings"' in html
    assert 'multipart/form-data' in html and 'name="soft_match"' not in html
    assert 'name="identity_confirmed"' in html and 'Draft for approval' in html
    assert html.count('name="csrf_token"')>=5


def test_outreach_history_is_outbound_not_customer_authorisation():
    from app.conversation_direction import direction,replayable_history
    row={'kind':'tenant_outreach','body_in':'[outreach sequence]','draft_reply':'An introduction.'}
    assert direction(row)=='outbound'
    assert replayable_history([row])[0]['body_in']==''


def test_gmail_preserves_machine_delivery_status_for_exact_bounce_matching():
    from app.gmail import _collect_bodies
    report=b'Final-Recipient: rfc822; recipient@example.com\nAction: failed\nStatus: 5.1.1'
    acc={'plain':[],'html':[]}
    _collect_bodies({'mimeType':'message/delivery-status','body':{'data':base64.urlsafe_b64encode(report).decode()}},acc)
    assert 'Final-Recipient: rfc822; recipient@example.com' in acc['plain'][0]
