import json
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from app import setup_forms, knowledge_forms, dashboard, knowledge
from tests.test_crm_fields import client, Env


@pytest.mark.parametrize('page', ['crm_fields','crm_settings','booking_details','dependants'])
@pytest.mark.parametrize('raw', ['', '{}', '[]', '{bad', 'null'])
def test_missing_and_malformed_versions_fail_closed(page,raw):
    with pytest.raises(HTTPException) as exc:
        setup_forms.parse(raw,page)
    assert exc.value.status_code==409


@pytest.mark.parametrize('page,extra', [('booking_details',{'config':[],'definitions':[]}),
    ('dependants',{'config':{},'definitions':[],'groups':[],'instances':[{}]}),
    ('crm_fields',{'definitions':[{'id':'x'}]}),('crm_settings',{'stages':[]})])
def test_incomplete_matching_page_versions_are_refused(page,extra):
    with pytest.raises(HTTPException): setup_forms.parse(json.dumps({'page':page,**extra}),page)


@pytest.mark.parametrize('path,data', [('/app/crm/fields',{'entity':'contact','label':'Reference'}),
    ('/app/crm/fields/field',{'label':'Reference'}),('/app/crm/fields/field/delete',{}),
    ('/app/crm/settings',{'quiet_days':'4'}),('/app/crm/settings/stages',{'name':'Open'}),
    ('/app/crm/settings/stages/stage',{'name':'Open'}),('/app/crm/settings/stages/stage/delete',{}),
    ('/app/diary/intake',{}),('/app/diary/dependants',{})])
def test_every_mutating_form_requires_its_original_version(client,monkeypatch,path,data):
    Env(monkeypatch)
    monkeypatch.setattr(setup_forms,'save',lambda *a: pytest.fail('Unversioned form wrote'))
    assert client.post(path,data=data).status_code==409


@pytest.mark.parametrize('message,status',[('BLAM8_SETTINGS_STALE',409),('lost response',503)])
def test_save_conflict_and_uncertainty_never_retry(message,status):
    calls=[]
    class RPC:
        def rpc(self,name,args):
            calls.append((name,args)); return self
        def execute(self): raise RuntimeError(message)
    with pytest.raises(HTTPException) as exc:
        setup_forms.save(SimpleNamespace(tenant_id='tenant',client=RPC()),'crm_fields',{},[])
    assert exc.value.status_code==status and len(calls)==1


def test_intake_batches_promoted_fields_and_keeps_existing_key():
    before={'config':{},'definitions':[{'id':'one','entity':'contact','key':'old_key','label':'Old'}]}
    fields=[{'label':'Renamed','required':True,'remember':'confirm','crm_key':'old_key'},
            {'label':'Reference','required':True,'remember':'once'}]
    ops,config,problems=setup_forms.intake_plan(before,fields,False)
    assert not problems and len(ops)==1 and ops[0]['fields']['key']=='reference'
    assert config['intake_fields'][0]['crm_key']=='old_key'
    assert config['intake_fields'][1]['crm_key']=='reference'
    assert len(before['definitions'])==1


def test_leaving_stays_requires_explicit_scope_cleanup():
    with pytest.raises(HTTPException,match='Stays-only'):
        setup_forms.intake_plan({'config':{'booking_mode':'stay'},'definitions':[]},
            [{'label':'Reference','required':True,'services':['Stays']}],False)


def test_dependants_plan_retains_ids_normalises_levels_and_clears_removed_ladders():
    before={'groups':[{'id':'g'}], 'definitions':[{'id':'d','entity':'contact','key':'old','ladder':{}}],
            'instances':[{'id':'i','group_id':'g','data':{'level':'stage one','retained':'yes'}}]}
    groups=[{'id':'g','label':'Asset','fields':[{'key':'level','label':'Level','type':'dropdown',
             'options':['Stage one','Stage two'],'ladder':{'notify':'off','gates':[]}}]}]
    ops=setup_forms.groups_plan(before,groups,[])
    assert ops[0]['op']=='update' and ops[0]['id']=='g'
    assert next(o for o in ops if o['table']=='crm_field_definitions')['fields']=={'ladder':None}
    assert next(o for o in ops if o['table']=='requirement_instances')['fields']['data']=={'level':'Stage one','retained':'yes'}


@pytest.mark.parametrize('raw',['','{}','[{}]','null','[{"source":"Hours","version":"bad"}]'])
def test_knowledge_requires_a_valid_version(raw):
    with pytest.raises(HTTPException): knowledge_forms.version(raw,'Hours',None)


def test_knowledge_versions_are_scope_specific_and_edits_require_an_existing_source():
    raw=json.dumps([{'source':'Hours','mailbox_id':'mailbox','version':'a'*32}])
    assert knowledge_forms.version(raw,'Hours','mailbox')=='a'*32
    assert knowledge_forms.version(raw,'Hours',None)==knowledge_forms.EMPTY_VERSION
    with pytest.raises(HTTPException): knowledge_forms.version(raw,'Hours',None,existing=True)


@pytest.mark.asyncio
async def test_checked_ingest_submits_one_atomic_rename(monkeypatch):
    async def embed(chunks): return [[1.0] for _ in chunks],0
    monkeypatch.setattr(knowledge.embeddings,'embed_documents',embed)
    monkeypatch.setattr(knowledge.usage,'record_voyage',lambda *a,**k: None)
    monkeypatch.setattr(knowledge.db,'replace_knowledge_source',lambda *a,**k: pytest.fail('Unchecked replacement'))
    writes=[]
    monkeypatch.setattr(knowledge_forms,'save',lambda *a,**k: writes.append(a))
    assert await knowledge.ingest('tenant','New','A fact',expected_version='a'*32,original_source='Old')==1
    assert writes[0][:5]==('tenant','Old','New',None,'a'*32)
    assert writes[0][5][0]['source']=='New'


def test_preserved_multiday_span_keeps_both_dates_visible_in_appointment_diary():
    from datetime import datetime,timezone
    from app import scheduling
    from tests.booking_fixtures import carried_config
    cfg=carried_config({'booking_mode':'appointment','horizon_days':7})
    booking={'id':'booking','booking_date':'2026-10-05','window':'day','start_block':36,
             'length_blocks':120,'status':'confirmed','quantity':1}
    days=scheduling.diary_days(cfg,[booking],datetime(2026,10,5,tzinfo=timezone.utc))
    assert '05 Oct' in booking['time_label'] and '06 Oct' in booking['time_label']
    assert sum(any(b['id']=='booking' for b in d['bookings']) for d in days)==2
