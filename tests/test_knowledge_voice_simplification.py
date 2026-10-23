"""Approved review/Voice contract at real state, route and SQL boundaries."""
from copy import deepcopy
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from app import db, onboarding, voice_completion, knowledge_fidelity as fidelity
from app import knowledge_review as review, knowledge_guidance as guide
from app import knowledge_source_review as source, knowledge_note_edits as edits
from app.auth import TenantCtx, require_tenant
from app.main import app
from app.security import require_csrf
from tests.test_knowledge_review import context, entry, form
from tests.test_knowledge_source_review import seeded, corrections
from tests.test_knowledge_review_database import review_db, review_case
from tests.test_booking_capacity_database import cluster

VALID={'agent_name':'Office team','tone':'Warm and professional','sign_off':'Kind regards,\nThe team'}


RELATIONSHIP_CHANGES = [
    ('Bookings are available Monday and Friday.', 'Bookings are available Monday to Friday.'),
    ('Deliveries from Camden to Islington.', 'Deliveries to Camden from Islington.'),
    ('Bookings are available Monday and Friday.', 'Bookings are available Monday to Thursday and Friday.'),
    ('Visits in Camden.', 'Visits from Camden.'),
    ('Payment by Friday.', 'Payment on Friday.'),
]


@pytest.mark.parametrize('original,changed', RELATIONSHIP_CHANGES)
def test_relationship_changes_are_not_automatic_source_coverage(original, changed):
    assert not fidelity.represented_fact(original, changed)
    assert not fidelity.represented_fact(changed, original)


@pytest.mark.parametrize('original,changed', RELATIONSHIP_CHANGES)
def test_changed_relationship_blocks_final_knowledge_admission(original, changed):
    ctx,row=context([entry(content=changed)])
    row['payload']['source_units']=fidelity.inventory([{'url':'https://example.test','text':original}])
    fields=form(ctx,row)
    before=deepcopy(ctx)
    view=review.review_view(ctx,row,fields)
    assert view['source_view']['unresolved']==1
    with pytest.raises(HTTPException,match='unaccounted for'):
        review.plan(ctx,row,fields)
    assert ctx==before


@pytest.fixture
def voice_client(monkeypatch):
    saved={}
    writes=[]
    app.dependency_overrides[require_tenant]=lambda:TenantCtx(tenant_id='tenant',client=None)
    app.dependency_overrides[require_csrf]=lambda:None
    monkeypatch.setattr(db,'get_tenant_config',lambda *a,**kw:deepcopy(saved))
    monkeypatch.setattr(db,'get_member',lambda *a,**kw:None)
    def rpc(name,args):
        assert name=='save_onboarding_voice'
        if args['p_expected']!=voice_completion.snapshot(saved):raise RuntimeError('BLAM8_VOICE_STALE')
        writes.append(args['p_values']);saved.update(args['p_values'])
        return SimpleNamespace(execute=lambda:SimpleNamespace(data=True))
    monkeypatch.setattr(db,'db',lambda:SimpleNamespace(rpc=rpc))
    monkeypatch.setattr(db,'get_connection_status',lambda *a,**kw:False)
    from app import setup_held
    monkeypatch.setattr(setup_held,'load',lambda *a,**kw:[])
    monkeypatch.setattr(setup_held,'mark_used',lambda *a,**kw:None)
    yield TestClient(app),saved,writes
    app.dependency_overrides.clear()


@pytest.mark.parametrize('missing',list(VALID))
@pytest.mark.parametrize('value',['',' \n\t '])
def test_invalid_voice_cannot_save_or_finish(voice_client,missing,value):
    client,saved,writes=voice_client
    response=client.post('/onboarding/voice',data={**VALID,missing:value},follow_redirects=False)
    assert response.status_code==422 and voice_completion.REQUIRED[missing] in response.text
    assert writes==[] and saved=={}
    assert client.post('/onboarding/finish',follow_redirects=False).status_code==422
    assert client.get('/onboarding/knowledge',follow_redirects=False).headers['location']=='/onboarding/voice'


def test_valid_voice_optional_notes_empty_persists_before_redirect(voice_client):
    client,saved,writes=voice_client
    response=client.post('/onboarding/voice',data={**VALID,'voice_notes':'','voice_expected':json.dumps(voice_completion.snapshot(saved))},follow_redirects=False)
    assert response.status_code==303 and response.headers['location']=='/onboarding/knowledge'
    assert voice_completion.complete(saved) and saved['voice_notes'] is None
    assert saved['voice_profile_source']=='manual' and len(writes)==1
    html=client.get('/onboarding/voice').text
    assert '>Skip<' not in html and 'voice_completion.js' in html and 'voice-required' in html


def test_learned_voice_accept_rejects_missing_required_values(voice_client):
    client,saved,writes=voice_client
    saved.update(voice_learning_status='ready',voice_profile={'tone':'Professional','sign_off':'Regards'})
    response=client.post('/onboarding/voice/accept',follow_redirects=False)
    assert response.status_code==422 and not writes


def test_nine_note_review_paraphrase_boroughs_and_audit_do_not_duplicate():
    row,evidence=seeded()
    row['payload']['source_units']=fidelity.inventory(evidence['pages'])
    ctx={'version':'v1','brief':'','sources':[],'reviews':[row]}
    fields={'version':'v1',**{'action_'+n['id']:'keep' for n in row['payload']['entries']}}
    # Complete the deliberately missing safety facts in the original synthetic fixture.
    for n in row['payload']['entries']:
        if n['label']=='Prices and Fees':n['content']+=' Bank details are not published.'
        if n['label']=='Maintenance Reports':n['content']+=' We do not make a legal determination from an email. Urgent safety guidance must not wait for an approval request.'
        if n['label']=='Gas Safety Guidance':n['content']+=' There is no staffed out-of-hours repair service.'
    area=next(n for n in row['payload']['entries'] if n['label']=='Service Area')
    original=area['content']
    fields['content_'+area['id']]=original.replace('Our service area is','Service area:')
    for n in row['payload']['entries']:
        for m in review.candidates(n,ctx,row['id']):fields['match_'+m['key']]='keep_both'
    notes,payload=review.plan(ctx,row,fields)
    effective=next(n['content'] for n in notes if n['label'].startswith('Service Area'))
    assert all(effective.count(b)==1 for b in ['Camden','Islington','Hackney','Haringey'])
    assert 'Retained source statement' not in effective
    assert guide.current(ctx,row,fields)[1]['kind']=='ready'


def test_layout_whitelist_retains_unique_heading_fact():
    units=fidelity.inventory([{'url':'https://example.test','text':'Home\nCamden\nFee £25 per visit',
        'blocks':[{'start':0,'end':4,'location':'nav'},{'start':5,'end':11,'location':'h1'},
                  {'start':12,'end':29,'location':'h2'}]}])
    assert fidelity.layout_fragment(units[0])
    assert not fidelity.layout_fragment(units[1]) and not fidelity.layout_fragment(units[2])
    ctx,row=context([entry(content='We provide support.')]);row['payload']['source_units']=units
    view=review.review_view(ctx,row,{'version':ctx['version']})
    assert view['source_view']['unresolved']==2


def test_critical_fact_missing_from_owner_confirmed_paraphrase_still_blocks():
    ctx,row=context([entry(content='We cover Camden and Islington.')])
    row['payload']['source_units']=fidelity.inventory([{'url':'u','text':'We cover Camden, Islington, Hackney and Haringey.'}])
    fields=form(ctx,row);unit=row['payload']['source_units'][0]
    fields['source_'+unit['id']]='covered:one'
    with pytest.raises(HTTPException):review.plan(ctx,row,fields)


@pytest.mark.parametrize('pair',[['missing','foreign'],['same','same'],['same'],[None,'same']])
def test_invalid_overlap_annotations_retain_verified_proposals(pair):
    row,evidence=seeded();card=source.draft_card(row,evidence);raw=corrections(card,evidence)
    pair=[card['lines'][0]['id'] if value=='same' else value for value in pair]
    raw['overlaps']=[pair]
    result=source.validate(card,evidence,raw)
    assert len(result['changes'])==9 and result['overlaps']==[]
    assert 'invalid overlap' in result['summary'] and row['payload']['draft_revision']==1


def test_multi_answer_resolves_all_supported_items_without_partial_invalid_application():
    ctx,row=context([entry(content='We provide support.')])
    row['payload']['source_units']=fidelity.inventory([{'url':'u','text':'First unresolved fact.\nSecond unresolved fact.'}])
    fields=form(ctx,row)
    decisions=[dict(kind='source',id=u['id'],action='exclude',answer='',reason='The owner does not use this policy.',target='') for u in row['payload']['source_units']]
    result=guide.apply_many(ctx,row,fields,decisions)
    assert len([k for k in result['patches'] if k.startswith('source_reason_')])==2
    assert guide.current(ctx,row,{**fields,**result['patches']})[1]['kind']=='ready'
    before=deepcopy(fields)
    with pytest.raises(HTTPException):guide.apply_many(ctx,row,fields,decisions+[dict(decisions[0],id='foreign')])
    assert fields==before


@pytest.mark.parametrize('count', [0, 31])
def test_multi_answer_bound_is_enforced_before_pending_choices_are_read(monkeypatch, count):
    ctx,row=context([entry(content='We provide support.')]);fields=form(ctx,row)
    before=deepcopy((ctx,fields))
    monkeypatch.setattr(guide,'pending_decisions',lambda *a:pytest.fail('Invalid batch reached decision processing'))
    with pytest.raises(HTTPException) as error:
        guide.apply_many(ctx,row,fields,[{}]*count)
    assert error.value.status_code==422 and (ctx,fields)==before


def test_current_revision_review_survives_unrelated_edit_only():
    ctx,row=context([entry(),entry('two',content='Other useful information.')])
    first,second=row['payload']['entries']
    row['payload']['draft_review_choices']={'action_one':'keep','action_two':'keep',
        'reviewed_one':review.fingerprint(first['label'],first['content']),
        'reviewed_two':review.fingerprint(second['label'],second['content'])}
    fields=review.review_form(row,{'content_two':'Changed second note.'})
    assert fields['action_one']=='keep' and fields['action_two']==''


def test_reread_uses_saved_owner_replacement_and_preserves_other_facts():
    old=entry(content='Hours 9am-5pm. Contact the office by email.')
    old['provenance']=[{'source':'https://example.test/contact'}]
    saved={'state':'saved','payload':{'entries':[old],'source_review_history':[{'owner_request':'Correct hours to 9am-4pm.',
        'changes':[{'id':'one','owner_replacements':[{'before':'9am-5pm','after':'9am-4pm'}]}]}]}}
    payload={'entries':[deepcopy(old)],'source_units':fidelity.inventory([{'url':'https://example.test/contact','text':old['content']}])}
    review.preserve_owner_corrections(payload,{'reviews':[saved]})
    text=payload['entries'][0]['content']
    assert '9am-4pm' in text and '9am-5pm' not in text and 'Contact the office by email.' in text
    assert payload['draft_review_choices'] and payload['owner_authority']


def test_checkpoint_sql_is_durable_scoped_and_stale_safe(review_case):
    (driver,dsn),tenant,identity,version=review_case
    from pathlib import Path
    sql=(Path(__file__).resolve().parents[1]/'db/migrations/20261023_knowledge_review_choices.sql').read_text()
    with driver.connect(dsn,autocommit=True) as c:
        c.execute(sql);c.execute(sql)
        c.execute('set role service_role')
        choices=json.dumps({'action_one':'keep','match_pair':'keep_both'})
        assert c.execute('select checkpoint_knowledge_review(%s,%s,%s,%s::jsonb)',(tenant,identity,version,choices)).fetchone()[0]
        stored=c.execute('select knowledge_review_context(%s)',(tenant,)).fetchone()[0]
        assert stored['reviews'][0]['payload']['draft_review_choices']['match_pair']=='keep_both'
        assert not stored['sources']
        with pytest.raises(Exception,match='BLAM8_KNOWLEDGE_REVIEW_STALE'):
            c.execute("select checkpoint_knowledge_review(%s,%s,%s,'{}')",(tenant,identity,version))
        assert not c.execute("select checkpoint_knowledge_review(%s,%s,%s,'{}')",(tenant,uuid4(),stored['version'])).fetchone()[0]
        c.execute('set role authenticated')
        with pytest.raises(driver.errors.InsufficientPrivilege):
            c.execute("select checkpoint_knowledge_review(%s,%s,%s,'{}')",(tenant,identity,stored['version']))


def test_checkpoint_rollback_and_reapply_preserve_choices(review_case):
    from pathlib import Path
    (driver,dsn),tenant,identity,version=review_case
    root=Path(__file__).resolve().parents[1]/'db/migrations'
    with driver.connect(dsn,autocommit=True) as c:
        c.execute((root/'20261023_knowledge_review_choices.sql').read_text())
        c.execute("select checkpoint_knowledge_review(%s,%s,%s,'{\"action_one\":\"keep\"}')",(tenant,identity,version))
        c.execute((root/'20261023_knowledge_review_choices.rollback.sql').read_text())
        assert c.execute('select payload from knowledge_reviews where id=%s',(identity,)).fetchone()[0]['draft_review_choices']['action_one']=='keep'
        c.execute((root/'20261023_knowledge_review_choices.sql').read_text())


def test_setup_question_receipt_survives_unrelated_knowledge_edit():
    from app import onboarding_state
    sources=[{'source':'Payment terms','content':'Invoices are due within 14 days.','mailbox_id':None}]
    question={'id':'payment-q','question':'When are invoices payable?','answer':'Within 14 days','status':'answered'}
    payload={'entries':[],'questions':[{'topic':'payment','question':question['question']}],
        'topic_reviews':{'payment':{'state':'partial','scoped_evidence':review.topic_evidence('payment',sources),'questions':[question]}}}
    ctx={'version':'v1','sources':sources,'reviews':[{'id':'saved','state':'saved','payload':payload}]}
    first=onboarding_state.knowledge_candidates(ctx)[0]
    ctx['version']='v2';ctx['sources'].append({'source':'Hours','content':'Open Monday to Friday.','mailbox_id':None})
    second=onboarding_state.knowledge_candidates(ctx)[0]
    assert first['state']==second['state']=='answered' and first['evidence_revision']==second['evidence_revision']
    ctx['sources'][0]['content']='Invoices are payable within 30 days.'
    assert onboarding_state.knowledge_candidates(ctx)[0]['evidence_revision']!=first['evidence_revision']


def test_invalid_correction_note_id_keeps_other_verified_changes():
    row,evidence=seeded();card=source.draft_card(row,evidence);raw=corrections(card,evidence)
    raw['changes'][0]['id']='foreign-tenant-note'
    result=source.validate(card,evidence,raw)
    assert len(result['changes'])==8 and 'unavailable or repeated note IDs' in result['summary']


def test_voice_stale_snapshot_and_save_failure_do_not_advance(voice_client,monkeypatch):
    client,saved,writes=voice_client
    original=json.dumps(voice_completion.snapshot(saved))
    saved.update(tone='Changed on another device')
    result=client.post('/onboarding/voice',data={**VALID,'voice_expected':original},follow_redirects=False)
    assert result.status_code==409 and not writes
    assert saved['tone']=='Changed on another device'
    result=client.post('/onboarding/voice',data=VALID,follow_redirects=False)
    assert result.status_code==409 and not writes


def test_voice_sql_required_contract_concurrent_stale_saves_and_unrelated_values(review_case):
    from pathlib import Path
    from concurrent.futures import ThreadPoolExecutor
    (driver,dsn),tenant,identity,version=review_case
    root=Path(__file__).resolve().parents[1]/'db/migrations'
    with driver.connect(dsn,autocommit=True) as c:
        for field in voice_completion.SNAPSHOT_FIELDS:
            if field=='business_brief':continue
            c.execute('alter table tenant_config add column if not exists '+field+(' jsonb' if field=='voice_profile' else ' text'))
        c.execute('alter table tenant_config add column if not exists examples text')
        c.execute('alter table tenant_config add column if not exists unrelated text')
        c.execute("update tenant_config set unrelated='Preserve this',examples='Owner examples stay private' where tenant_id=%s",(tenant,))
        c.execute((root/'20261023_onboarding_voice.sql').read_text());c.execute((root/'20261023_onboarding_voice.sql').read_text())
    expected=voice_completion.snapshot({'business_brief':'Original brief'})
    def save_one(values,role='service_role'):
        with driver.connect(dsn,autocommit=True) as c:
            c.execute('set role '+role)
            return c.execute('select save_onboarding_voice(%s,%s::jsonb,%s::jsonb,%s::text[])',
                (tenant,json.dumps(expected),json.dumps(values),list(voice_completion.REQUIRED))).fetchone()[0]
    with pytest.raises(Exception,match='BLAM8_VOICE_INVALID'):save_one({**VALID,'tone':'  '})
    def attempt(_):
        try:return save_one({**VALID,'examples':'Generated optional sample'})
        except Exception:return False
    with ThreadPoolExecutor(max_workers=2) as pool:assert sum(pool.map(attempt,range(2)))==1
    with driver.connect(dsn,autocommit=True) as c:
        row=c.execute('select tone,unrelated,business_brief,examples from tenant_config where tenant_id=%s',(tenant,)).fetchone()
        assert row==('Warm and professional','Preserve this','Original brief','Owner examples stay private')
        c.execute((root/'20261023_onboarding_voice.rollback.sql').read_text())
        assert c.execute('select tone from tenant_config where tenant_id=%s',(tenant,)).fetchone()[0]==VALID['tone']
        c.execute((root/'20261023_onboarding_voice.sql').read_text())
    with pytest.raises(driver.errors.InsufficientPrivilege):save_one(VALID,'authenticated')


def test_applied_correction_replaces_checkpoint_body_and_invalidates_only_its_approval():
    from tests.test_knowledge_owner_corrections import owner_case,REQUEST
    row,evidence,card,raw=owner_case()
    n=row['payload']['entries'][0]
    row['payload']['draft_review_choices']={'content_'+n['id']:n['content'],'action_'+n['id']:'keep',
        'reviewed_'+n['id']:review.fingerprint(n['label'],n['content'])}
    payload=source.corrected_payload(row,evidence,raw,card=card,owner_request=REQUEST)
    fields=review.review_form({'payload':payload})
    assert '4 pm' in fields['content_'+n['id']] and '5 pm' not in fields['content_'+n['id']]
    assert fields['action_'+n['id']]=='' and fields['reviewed_'+n['id']]==''


@pytest.mark.parametrize('fill,ready',[({'agent_name':'Office team'},False),(VALID,True)])
def test_voice_assistant_namedtuple_fill_uses_same_required_contract(voice_client,monkeypatch,fill,ready):
    from app import concierge_routes as routes,concierge,setup_held
    client,saved,writes=voice_client
    monkeypatch.setattr(db,'get_tenant',lambda *a,**kw:{})
    monkeypatch.setattr(routes,'_tenant_context',lambda ctx:{})
    monkeypatch.setattr(routes.fairuse,'onboarding_allowed',lambda *a,**kw:True)
    monkeypatch.setattr(routes.usage,'record_anthropic',lambda *a,**kw:None)
    monkeypatch.setattr(setup_held,'add',lambda *a,**kw:[])
    async def answer(*a,**kw):return concierge.OnboardingReply('The form is filled.',fill,True,{},'fake')
    monkeypatch.setattr(concierge,'answer_onboarding',answer)
    result=client.post('/concierge/onboard',json={'step':'voice','question':'Use these choices','current':{}})
    assert result.status_code==200 and result.json()['ready_to_advance'] is ready
    assert result.json()['form_fill']==fill and not writes
    assert 'Review the suggested values' in result.json()['reply']


@pytest.mark.parametrize('viewing',[False,True])
def test_voice_service_save_requires_settings_permission_outside_view_as(voice_client,monkeypatch,viewing):
    client,saved,writes=voice_client
    app.dependency_overrides[require_tenant]=lambda:TenantCtx('tenant',None,'owner','owner' if viewing else None)
    if not viewing:monkeypatch.setattr(db,'get_member',lambda *a,**kw:{'role':'accountant'})
    result=client.post('/onboarding/voice',data={**VALID,'voice_expected':json.dumps(voice_completion.snapshot(saved))},follow_redirects=False)
    assert result.status_code==403 and not writes


@pytest.mark.parametrize('fact',['We cover Camden, Islington, Hackney and Haringey.',
    'The fee is £95 per property per month.', 'Visits last 30 minutes.', 'This rate applies from 1 November.'])
def test_cleanup_never_excludes_unique_critical_fact_as_deduplication(fact):
    row,evidence=seeded();note=row['payload']['entries'][0]
    note['content']='We provide a useful service.\n\n'+fact
    row['payload']['entries']=[note]
    row['payload']['source_units']=fidelity.inventory([{'url':'https://example.test','text':fact}])
    ctx={'version':'v1','reviews':[row],'sources':[],'brief':''}
    card=edits.scope(source.draft_card(row,evidence),ctx,row,{'version':'v1'},note['id'],
        'Remove repeated wording but keep all boroughs, prices, units and dates.')
    raw={'revision':1,'changes':[{'id':note['id'],'content_hash':source.knowledge_source_evidence.digest(note['content']),
        'insertions':[],'citations':[],'removals':[card['knowledge_edit_scope']['paragraphs'][-1]['reference']]}],
        'overlaps':[],'questions':[],'edit_scope':edits.binding(card)}
    with pytest.raises(Exception,match='critical source fact'):source.validate(card,evidence,raw)
    assert note['content'].endswith(fact)


def test_reread_correction_uses_source_claim_even_when_model_changes_note_title():
    old=entry(content='Office hours 9am-5pm. Contact by email.')
    old['provenance']=[{'source':'https://example.test/contact'}]
    history={'owner_request':'Correct hours to 9am-4pm.','changes':[{'id':'one','before':old['content'],
        'owner_replacements':[{'before':'9am-5pm','after':'9am-4pm'}]}]}
    incoming=deepcopy(old);incoming['label']='A different model title'
    payload={'entries':[incoming],'source_units':fidelity.inventory([{'url':'https://example.test/contact','text':old['content']}])}
    review.preserve_owner_corrections(payload,{'reviews':[{'state':'saved','payload':{'entries':[old],'source_review_history':[history]}}]})
    assert '9am-5pm' not in payload['entries'][0]['content'] and 'Contact by email.' in payload['entries'][0]['content']


def test_learned_prefill_is_visible_but_not_reported_as_saved_completion(voice_client):
    from html import unescape
    import re
    client,saved,writes=voice_client
    saved.update(voice_learning_status='ready',voice_profile={'sign_off_name':'Office team','tone':'Professional','sign_off':'Regards'})
    response=client.get('/onboarding/voice')
    descriptor=re.search(r'<script id="onboarding-journey"[^>]*>(.*?)</script>',response.text,re.S)
    assert descriptor and json.loads(unescape(descriptor.group(1)))['saved_completion']['voice'] is False
    assert 'value="Office team"' in response.text and not writes
    assert 'examples' not in voice_completion.snapshot(saved)
