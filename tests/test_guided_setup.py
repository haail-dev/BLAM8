"""Guide caller -> real proposal -> HTTP -> disposable SQL, no model network."""
from copy import deepcopy
import json
from types import SimpleNamespace
import pytest
from app import guided_setup, guided_setup_routes as routes, concierge, concierge_routes, db, llm, usage
from app import setup_catalogue, setup_card_routes, onboarding_state
from app.auth import TenantCtx
from tests.test_agnostic_onboarding import cluster, database, journey, REAL_MANAGER


def seed_reviewed_voice(j):
    for field in ('agent_name','tone','sign_off'):
        j.execute('alter table tenant_config add column if not exists '+field+' text')
    j.execute('update tenant_config set agent_name=%s,tone=%s,sign_off=%s where tenant_id=%s',
              'Office team','Professional','Regards',j.tenant)


@pytest.fixture
def guide_journey(journey, monkeypatch):
    j=journey
    j.execute((__import__('pathlib').Path(__file__).resolve().parents[1]/'db/migrations/20261023_guided_setup.sql').read_text())
    monkeypatch.setattr(routes,'require_manager',lambda ctx:None)
    monkeypatch.setattr(db,'get_tenant',lambda *a,**k:{'name':'Example business'})
    monkeypatch.setattr(db,'active_modules',lambda *a,**k:{'scheduling','crm'})
    monkeypatch.setattr(db,'list_mailboxes',lambda *a,**k:[])
    monkeypatch.setattr(db,'list_knowledge_sources',lambda *a,**k:[])
    monkeypatch.setattr(db,'knowledge_source_texts',lambda *a,**k:[])
    monkeypatch.setattr(db,'crm_list_field_definitions',lambda *a,**k:[])
    monkeypatch.setattr(usage,'record_anthropic',lambda *a,**k:None)
    monkeypatch.setattr(usage,'record_provider',lambda *a,**k:None,raising=False)
    # Permission is tested separately; SQL and card validation stay real.
    j.ctx=TenantCtx(j.tenant,j.rpc,'owner')
    def action(op,choice=None,expected=200,**extra):
        g=j.state().get('guide') or {}
        body=dict(revision=j.state()['revision'],stop=g.get('current','intro'),operation=op,**extra)
        if choice:body['choice']=choice
        response=j.http.post('/app/setup/guide/action',json=body)
        assert response.status_code==expected,response.text
        return response
    def move(stop):
        # Fixture setup only: seed a supported stop through the real metadata
        # RPC. Navigation semantics are separately driven through HTTP below.
        context=onboarding_state.load(j.ctx);state=deepcopy(context['state'])
        state['capabilities']={k:'needed' for k in guided_setup.contract.CAPABILITIES}
        assert stop in {s['id'] for s in guided_setup.steps({**context,'state':state})}
        state['guide']['current']=stop
        onboarding_state.save(j.ctx,context['state'],state)
    def diary(op,expected=200,**extra):
        g=j.state().get('guide') or {}
        response=j.http.post('/app/setup/guide/diary',json=dict(revision=j.state()['revision'],stop=g['current'],operation=op,**extra))
        assert response.status_code==expected,response.text
        return response
    j.guide_action=action;j.move=move;j.diary=diary
    action('start')
    return j


def model(monkeypatch, changes, *, facts=None, reply='Please clarify this choice.'):
    seen=[]
    answer=concierge.answer_help
    async def diagnosed(*args,**kwargs):
        try:return await answer(*args,**kwargs)
        except Exception as exc:
            seen.append({'synthetic_error':repr(exc)})
            raise
    monkeypatch.setattr(concierge,'answer_help',diagnosed)
    async def create(**kwargs):
        seen.append(kwargs)
        payload=dict(reply=reply,nav='',submit=None,recommend=[],held=[],no_dependants=False,
                     setup_changes=changes,guide_answers=facts or [])
        return SimpleNamespace(content=[SimpleNamespace(type='text',text=json.dumps(payload))],
            usage=SimpleNamespace(input_tokens=11,output_tokens=8),model='synthetic')
    monkeypatch.setattr(llm,'create',create)
    monkeypatch.setattr(concierge,'client',lambda:None)
    return seen


def change(page,revision,patches):
    return [dict(page=page,revision=revision,changes=[dict(op=p.get('op','append'),id=p.get('id',''),
        name=p.get('name','setting'),held=None,fields=[dict(field=k,value=v,source='owner_said',reference='Owner supplied this answer')
        for k,v in p['fields'].items()]) for p in patches])]


def turn(j,text,expected=200):
    description=j.http.get('/app/setup/guide/state').json()
    response=j.http.post('/app/setup/guide/turn',json=dict(revision=j.state()['revision'],
        stop=j.state()['guide']['current'],question=text,path=description['stop']['href'].split('#')[0],history=[]))
    assert response.status_code==expected,response.text
    return response


def test_registry_accounts_for_entire_native_catalogue():
    actual=guided_setup.coverage()
    expected={e['id'] for e in setup_catalogue.entries() if e['page'] in guided_setup.ROUTES}
    assert {e['identity'] for e in actual}==expected
    assert len(actual)==len(expected)
    assert all(e['adapter'] in ('card','native_editor') and e['href'].startswith('/app/') for e in actual)


def test_grouped_questions_keep_controls_with_their_owning_subsection():
    rows=guided_setup.registry()
    assert set(rows['diary_settings/working_days']['catalogue'])=={
        'diary_settings/'+k for k in ('working_days','open_time','close_time','timezone')}
    late_cancel=next(s for s in rows.values() if 'diary_settings/late_cancel_hours' in s['catalogue'])
    assert late_cancel['substep']=='charges.terms'
    assert 'diary_settings/credits_spend' not in late_cancel['catalogue']


def test_module_limited_start_does_not_send_owner_to_an_unavailable_diary(guide_journey):
    context=onboarding_state.load(guide_journey.ctx)
    for modules in ({'crm'},{'tms'},{'fsm'},set()):
        context['modules']=modules
        sequence=guided_setup.steps(context)
        assert not any(s['href']=='/app/diary' for s in sequence)
        assert not any(s.get('page','').startswith('crm_') for s in sequence if 'crm' not in modules)


def test_start_reuses_actual_saved_knowledge_and_about_you_without_applying(journey,monkeypatch):
    from app import knowledge_review as kr
    j=journey
    monkeypatch.setattr(routes,'require_manager',lambda ctx:None)
    text='How often should inspections occur?'
    sources=[dict(source='Owner facts',content='Inspect monthly.',mailbox_id=None)]
    evidence=kr.fingerprint([(s['source'],s['mailbox_id'],s['content']) for s in sources])
    saved=dict(version='review-v1',sources=sources,reviews=[dict(id='review-1',state='saved',payload=dict(
        entries=[],questions=[dict(topic='cadence',question=text)],topic_reviews={'cadence':dict(
            state='partial',evidence=evidence,questions=[dict(id=kr.fingerprint('cadence',text),
                question=text,status='answered',answer='Monthly')])}))])
    monkeypatch.setattr(kr,'load',lambda tid:deepcopy(saved))
    j.execute('alter table tenant_config add column if not exists business_brief text')
    j.execute('update tenant_config set business_brief=%s where tenant_id=%s','We inspect equipment.',j.tenant)
    r=j.http.post('/app/setup/guide/action',json=dict(revision=j.state()['revision'],stop='intro',operation='start'))
    assert r.status_code==200,r.text
    facts=list(j.state()['answers'].values())
    assert next(a for a in facts if a['question']=='activity.purpose')['value']=='We inspect equipment.'
    known=next(a for a in facts if a['question'].startswith('knowledge.'))
    assert known['value']=='Monthly' and known['provenance']['kind']=='knowledge_owner_review'
    assert known['question'] in j.state()['routed_questions']
    context=onboarding_state.load(__import__('app.auth',fromlist=['TenantCtx']).TenantCtx(j.tenant,j.rpc,'owner'))
    context['state']['capabilities']['time_occupancy']='needed'
    assert any(s.get('question_id')==known['question'] for s in guided_setup.steps(context))
    assert j.row('scheduling_config',j.tenant) is None
    assert j.row('invoice_settings',j.tenant) is None
    before=j.state()
    r=j.http.post('/app/setup/guide/action',json=dict(revision=before['revision'],stop='intro',operation='start'))
    assert r.status_code==400 and j.state()==before
    # Exercise the real Knowledge adapter, not only a synthetic candidate list.
    saved['sources'][0]['content']='Inspection intervals depend on the agreement.'
    saved['version']='review-v2'
    r=j.http.post('/app/setup/guide/action',json=dict(revision=j.state()['revision'],
        stop=j.state()['guide']['current'],operation='retry_knowledge'))
    assert r.status_code==200,r.text
    stale=next(a for a in j.state()['answers'].values() if a['question']==known['question'])
    assert stale['state']=='unknown' and stale['value'] is None
    assert stale['history'][-1]['value']=='Monthly'


def test_explanations_preserve_one_proposal_and_resume_the_exact_control(guide_journey,monkeypatch):
    j=guide_journey;j.move('diary_settings/min_notice_hours')
    model(monkeypatch,change('diary_settings',0,[dict(fields={'key':'min_notice_hours','value':24}),dict(fields={'key':'horizon_days','value':60})]))
    turn(j,'24 hours notice, 60 days ahead.')
    proposal=deepcopy(j.state()['guide']['pending']['diary_settings/min_notice_hours'])
    j.guide_action('next')
    d=j.http.get('/app/setup/guide/state').json()
    assert d['stop']['id']=='explain.diary_settings/min_notice_hours'
    assert d['pending']['proposal']==proposal['proposal']
    j.guide_action('next');j.guide_action('pause');j.guide_action('resume')
    d=j.http.get('/app/setup/guide/state').json()
    assert d['stop']['id']=='explain.diary_settings/horizon_days'
    assert d['stop']['anchor']=='setting.horizon_days'
    assert j.row('scheduling_config',j.tenant) is None
    accept_current(j,'diary_settings')
    assert j.state()['guide']['decisions']['diary_settings/min_notice_hours']=='applied'
    assert j.state()['guide']['current']=='explain.diary_settings/horizon_days'


def test_exact_prior_setting_answer_prepares_one_card_without_reasking_or_saving(guide_journey):
    j=guide_journey
    j.answer('setting.invoice_settings.payment_terms','After completion')
    stop=next(s['id'] for s in guided_setup.registry().values() if 'invoice_settings/payment_terms' in s['catalogue'])
    j.move(stop);j.guide_action('resume')
    pending=deepcopy(j.state()['guide']['pending'][stop])
    card=j.row('tenant_config',j.tenant)['setup_cards']['invoice_settings']
    assert len(card['lines'])==1 and card['lines'][0]['fields']=={'key':'payment_terms','value':'After completion'}
    assert j.row('invoice_settings',j.tenant) is None
    j.guide_action('pause');j.guide_action('resume')
    assert j.state()['guide']['pending'][stop]==pending
    assert j.row('tenant_config',j.tenant)['setup_cards']['invoice_settings']==card


def test_knowledge_retry_is_visible_without_losing_position_or_owner_answers(guide_journey,monkeypatch):
    from app import knowledge_review
    j=guide_journey;j.move('question.activity.purpose')
    j.answer('activity.purpose','Inspect equipment')
    before=deepcopy(j.state()['answers'])
    def unavailable(tid):raise routes.HTTPException(503,'synthetic source failure')
    monkeypatch.setattr(knowledge_review,'load',unavailable)
    j.guide_action('retry_knowledge')
    assert j.state()['guide']['knowledge_unavailable'] is True
    assert j.state()['guide']['current']=='question.activity.purpose'
    assert j.state()['answers']==before
    monkeypatch.setattr(knowledge_review,'load',lambda tid:dict(sources=[],reviews=[],metadata=[],version='empty'))
    j.guide_action('retry_knowledge')
    assert j.state()['guide']['knowledge_unavailable'] is False and j.state()['answers']==before


@pytest.mark.parametrize('removed',[False,True])
def test_changed_or_removed_knowledge_is_unresolved_before_proposal_review_and_model(guide_journey,monkeypatch,removed):
    j=guide_journey
    q=dict(id='knowledge.payment-question',canonical='setting.invoice_settings.payment_terms',state='answered',
           answer='Within 14 days',evidence_revision='v1',source_review='review')
    candidates=[q]
    monkeypatch.setattr(onboarding_state,'knowledge_candidates',lambda _:deepcopy(candidates))
    j.guide_action('retry_knowledge')
    j.answer('setting.invoice_settings.default_quote_valid_days',30)
    key=onboarding_state.key_for(q['canonical'],'organisation','organisation')
    q.update(answer='Before work begins',evidence_revision='v2')
    if removed:candidates.clear()
    j.guide_action('retry_knowledge')
    answer=j.state()['answers'][key]
    assert answer['state']=='unknown' and answer['value'] is None
    assert answer['history'][-1]['value']=='Within 14 days'
    stop=next(s['id'] for s in guided_setup.registry().values() if 'invoice_settings/payment_terms' in s['catalogue'])
    j.move(stop);j.guide_action('resume')
    assert not (j.row('tenant_config',j.tenant).get('setup_cards') or {}).get('invoice_settings')
    d=j.http.get('/app/setup/guide/state').json()
    assert not any(a['question']==q['canonical'] for a in d['answers'])
    assert d['knowledge_conflicts'][0]['earlier']['value']=='Within 14 days'
    context=routes.context_for(j.ctx)
    model_answer=next(a for a in guided_setup.model_data(context,d['stop'])['answers'] if a['question']==q['canonical'])
    assert model_answer['state']=='unknown' and model_answer['value'] is None
    turn(j,'Use the terms you already know',expected=400)
    j.move('review.charges.terms');j.guide_action('review',expected=400)
    assert stop in [s['id'] for s in j.http.get('/app/setup/guide/state').json()['unresolved']]
    j.move(stop)
    j.guide_action('knowledge_keep_earlier' if removed else 'knowledge_use_updated',q['id'])
    card=j.row('tenant_config',j.tenant)['setup_cards']['invoice_settings']
    terms=next(l for l in card['lines'] if l['fields']['key']=='payment_terms')
    assert terms['fields']['value']==('Within 14 days' if removed else 'Before work begins')
    assert j.row('invoice_settings',j.tenant) is None


def test_existing_knowledge_card_requires_resolution_before_accept_or_refresh(guide_journey,monkeypatch):
    j=guide_journey
    j.execute('insert into invoice_settings(tenant_id,payment_terms) values(%s,%s)',j.tenant,'Existing saved terms')
    q=dict(id='knowledge.payment-question',canonical='setting.invoice_settings.payment_terms',state='answered',
           answer='Within 14 days',evidence_revision='v1',source_review='review')
    monkeypatch.setattr(onboarding_state,'knowledge_candidates',lambda _: [deepcopy(q)])
    j.guide_action('retry_knowledge')
    stop=next(s['id'] for s in guided_setup.registry().values() if 'invoice_settings/payment_terms' in s['catalogue'])
    j.move(stop);j.guide_action('resume')
    card=j.row('tenant_config',j.tenant)['setup_cards']['invoice_settings'];ids=[l['id'] for l in card['lines']]
    q.update(answer='Before work begins',evidence_revision='v2')
    before=j.state()
    assert j.http.get('/app/setup/guide/state').json()['knowledge_conflicts']
    assert j.state()==before  # Read-only freshness is not a hidden metadata write.
    preview_response=j.http.post('/concierge/setup-cards/invoice_settings/preview',json=dict(revision=card['revision'],ids=ids))
    assert preview_response.status_code==200,preview_response.text
    preview=preview_response.json()
    r=j.http.post('/concierge/setup-cards/invoice_settings/accept',json=dict(revision=card['revision'],ids=ids,settings_version=preview['settings_version']))
    assert r.status_code==409 and 'Knowledge changed' in r.text
    j.guide_action('refresh_preview',expected=400)
    assert j.row('invoice_settings',j.tenant)['payment_terms']=='Existing saved terms'
    j.guide_action('knowledge_use_updated',q['id'])
    changed=j.row('tenant_config',j.tenant)['setup_cards']['invoice_settings']
    assert changed['lines'][0]['id']==card['lines'][0]['id'] and changed['lines'][0]['fields']['value']=='Before work begins'
    assert not j.http.get('/app/setup/guide/state').json()['knowledge_conflicts']
    accept_current(j,'invoice_settings')
    assert j.row('invoice_settings',j.tenant)['payment_terms']=='Before work begins'
    q.update(answer='Another source change',evidence_revision='v3')
    j.guide_action('retry_knowledge')
    assert not j.http.get('/app/setup/guide/state').json()['knowledge_conflicts']
    assert j.row('invoice_settings',j.tenant)['payment_terms']=='Before work begins'


def test_independent_owner_answer_survives_knowledge_refresh(guide_journey,monkeypatch):
    j=guide_journey
    q=dict(id='knowledge.payment-question',canonical='setting.invoice_settings.payment_terms',state='answered',
           answer='Within 14 days',evidence_revision='v1',source_review='review')
    monkeypatch.setattr(onboarding_state,'knowledge_candidates',lambda _: [deepcopy(q)])
    j.guide_action('retry_knowledge');j.answer(q['canonical'],'On completion')
    q.update(answer='Before work begins',evidence_revision='v2')
    j.guide_action('retry_knowledge')
    answer=j.state()['answers'][onboarding_state.key_for(q['canonical'],'organisation','organisation')]
    assert answer['value']=='On completion' and answer['state']=='answered'


@pytest.mark.parametrize('resolution',['edit','earlier','leave_then_updated'])
def test_knowledge_conflict_cannot_be_cleared_by_editing_another_setting(guide_journey,monkeypatch,resolution):
    j=guide_journey
    j.execute('insert into invoice_settings(tenant_id,payment_terms) values(%s,%s)',j.tenant,'Existing saved terms')
    q=dict(id='knowledge.payment-question',canonical='setting.invoice_settings.payment_terms',state='answered',
           answer='Within 14 days',evidence_revision='v1',source_review='review')
    monkeypatch.setattr(onboarding_state,'knowledge_candidates',lambda _: [deepcopy(q)])
    j.guide_action('retry_knowledge');j.answer('setting.invoice_settings.default_quote_valid_days',30)
    stop=next(s['id'] for s in guided_setup.registry().values() if 'invoice_settings/payment_terms' in s['catalogue'])
    j.move(stop);j.guide_action('resume')
    card=j.row('tenant_config',j.tenant)['setup_cards']['invoice_settings']
    terms=next(l for l in card['lines'] if l['fields']['key']=='payment_terms')
    other=next(l for l in card['lines'] if l['fields']['key']=='default_quote_valid_days')
    q.update(answer='Before work begins',evidence_revision='v2')
    def edit(line,value):
        return j.http.post('/concierge/setup-cards/invoice_settings/changes',json=dict(revision=card['revision'],changes=[
            dict(op='set',id=line['id'],fields={'value':value},sources={'value':dict(kind='owner_said',reference='Owner edited this value')})]))
    blocked=edit(other,45)
    assert blocked.status_code==422 and 'Resolve the changed Knowledge' in blocked.text
    assert j.row('tenant_config',j.tenant)['setup_cards']['invoice_settings']==card
    expected='Within 14 days'
    if resolution=='edit':
        expected='On completion'
        result=edit(terms,expected);assert result.status_code==200,result.text
    elif resolution=='earlier':
        j.guide_action('knowledge_keep_earlier',q['id'])
    else:
        result=j.http.post('/concierge/setup-cards/invoice_settings/changes',json=dict(revision=card['revision'],changes=[
            dict(op='remove',id=l['id']) for l in card['lines']]))
        assert result.status_code==200,result.text
        expected='Before work begins';j.guide_action('knowledge_use_updated',q['id'])
    assert not j.http.get('/app/setup/guide/state').json()['knowledge_conflicts']
    assert j.row('invoice_settings',j.tenant)['payment_terms']=='Existing saved terms'
    accept_current(j,'invoice_settings')
    assert j.row('invoice_settings',j.tenant)['payment_terms']==expected


def test_unavailable_knowledge_never_certifies_an_imported_answer(guide_journey,monkeypatch):
    from app import knowledge_review
    j=guide_journey
    q=dict(id='knowledge.payment-question',canonical='setting.invoice_settings.payment_terms',state='answered',
           answer='Within 14 days',evidence_revision='v1',source_review='review')
    monkeypatch.setattr(onboarding_state,'knowledge_candidates',lambda _: [deepcopy(q)])
    j.guide_action('retry_knowledge')
    def unavailable(tid):raise routes.HTTPException(503,'synthetic Knowledge failure')
    monkeypatch.setattr(knowledge_review,'load',unavailable)
    stop=next(s['id'] for s in guided_setup.registry().values() if 'invoice_settings/payment_terms' in s['catalogue'])
    j.move(stop);j.guide_action('resume')
    d=j.http.get('/app/setup/guide/state').json()
    assert d['guide']['knowledge_unavailable'] and d['knowledge_conflicts'][0]['candidate'] is None
    j.guide_action('knowledge_use_updated',q['id'],expected=400)
    assert not (j.row('tenant_config',j.tenant).get('setup_cards') or {}).get('invoice_settings')


def test_start_opens_records_then_asks_which_diaries_and_get_never_mutates(guide_journey):
    j=guide_journey
    before=j.state()
    for _ in range(2):
        d=j.http.get('/app/setup/guide/state').json()
        assert d['stop']['substep']=='records.relationships' and d['section']['group']=='Records and workflows'
        assert d['stop']['href'].split('#')[0]=='/app/diary/dependants'
    assert j.state()==before
    # Records first; Diary then asks which diaries before any setting, and the real diary comes last.
    ids=[s['id'] for s in guided_setup.steps({**onboarding_state.load(j.ctx),'state':j.state()})]
    assert ids.index('review.records.progress')<ids.index('diary.discover.diaries')<ids.index('diary.profile')<ids.index('orientation.diary')
    assert 'relevance.diary.activities' not in ids
    j.move('diary.discover.diaries')
    d=j.http.get('/app/setup/guide/state').json()
    assert d['stop']['id']=='diary.discover.diaries' and d['stop']['href']=='/app/diary/settings'
    assert d['discovery']['diaries']==[] and d['discovery']['limit']==8
    j.guide_action('next',expected=400)
    assert j.row('scheduling_config',j.tenant) is None


def test_notice_horizon_edit_next_accept_and_resume_real_sql(guide_journey,monkeypatch,tmp_path):
    j=guide_journey;j.move('diary_settings/min_notice_hours')
    metered=[]
    monkeypatch.setattr(usage,'record_anthropic',lambda *args,**kwargs:metered.append((args,kwargs)))
    seen=model(monkeypatch,change('diary_settings',0,[dict(fields={'key':'min_notice_hours','value':24}),dict(fields={'key':'horizon_days','value':60})]))
    result=turn(j,'We need 24 hours notice and allow bookings 60 days ahead.').json()
    assert len(metered)==1 and metered[0][1]=={'operation':'concierge_help','module':'concierge'}
    assert metered[0][0][0]==j.tenant and metered[0][0][2].input_tokens==11 and metered[0][0][2].output_tokens==8
    assert j.row('scheduling_config',j.tenant) is None
    assert len(j.state()['answers'])==2 and len(result['pending']['ids'])==2
    assert 'Booking notice and horizon' not in str(seen[0]['messages'])
    assert 'CURRENT CATALOGUE' in str(seen[0]['system'])
    card=j.row('tenant_config',j.tenant)['setup_cards']['diary_settings']
    line=next(l for l in card['lines'] if l['fields']['key']=='horizon_days')
    response=j.http.post('/concierge/setup-cards/diary_settings/changes',json=dict(revision=card['revision'],changes=[
        dict(op='set',id=line['id'],fields={'value':45},sources={'value':{'kind':'owner_said','reference':'Owner edited the proposed control'}})]))
    assert response.status_code==200,response.text
    card=response.json()['card']
    answer=j.state()['answers'][onboarding_state.key_for('setting.diary_settings.horizon_days','organisation','organisation')]
    assert answer['value']==45 and answer['history'][-1]['value']==60
    assert answer['provenance']['reference']==line['id']
    assert j.row('scheduling_config',j.tenant) is None
    stop=j.state()['guide']['current'];j.guide_action('next');j.guide_action('back')
    assert j.state()['guide']['current']==stop and j.row('scheduling_config',j.tenant) is None
    ids=[l['id'] for l in card['lines'] if l['status']=='proposed']
    preview=j.http.post('/concierge/setup-cards/diary_settings/preview',json=dict(revision=card['revision'],ids=ids)).json()
    accepted=j.http.post('/concierge/setup-cards/diary_settings/accept',json=dict(revision=card['revision'],ids=ids,settings_version=preview['settings_version']))
    assert accepted.status_code==200,accepted.text
    saved=j.row('scheduling_config',j.tenant)
    assert saved['min_notice_hours']==24 and saved['horizon_days']==45
    assert j.state()['guide']['decisions'][stop]=='applied'
    after=j.state();assert j.http.get('/app/setup/guide/state').json()['guide']['current']==stop
    assert j.state()==after
    # Existing actual Help caller context now sees the corrected exact value.
    context=concierge_routes._tenant_context(j.ctx)
    assert next(a for a in context['onboarding_answers']['answers'] if a['question']=='setting.diary_settings.horizon_days')['value']==45
    (tmp_path/'diary-receipts.json').write_text(json.dumps(dict(state=after,settings=saved),indent=2),encoding='utf-8')
    assert len(metered)==1  # Edits, guide navigation and Accept emit no duplicate usage event.


def test_stale_saved_settings_need_explicit_new_difference(guide_journey,monkeypatch):
    j=guide_journey;j.move('diary_settings/min_notice_hours')
    model(monkeypatch,change('diary_settings',0,[dict(fields={'key':'min_notice_hours','value':0}),dict(fields={'key':'horizon_days','value':60})]))
    turn(j,'No notice, and 60 days ahead.')
    card=j.row('tenant_config',j.tenant)['setup_cards']['diary_settings'];ids=[l['id'] for l in card['lines']]
    j.execute('insert into scheduling_config(tenant_id,min_notice_hours,horizon_days) values(%s,12,30)',j.tenant)
    preview=j.http.post('/concierge/setup-cards/diary_settings/preview',json=dict(revision=card['revision'],ids=ids)).json()
    refused=j.http.post('/concierge/setup-cards/diary_settings/accept',json=dict(revision=card['revision'],ids=ids,settings_version=preview['settings_version']))
    assert refused.status_code==409,refused.text
    assert j.row('scheduling_config',j.tenant)['min_notice_hours']==12
    j.guide_action('refresh_preview')
    accepted=j.http.post('/concierge/setup-cards/diary_settings/accept',json=dict(revision=card['revision'],ids=ids,settings_version=preview['settings_version']))
    assert accepted.status_code==200,accepted.text
    assert j.row('scheduling_config',j.tenant)['min_notice_hours']==0


def test_off_journey_finishes_without_settings_or_readiness(guide_journey):
    j=guide_journey
    for _ in range(40):
        d=j.http.get('/app/setup/guide/state').json();kind=d['stop']['kind']
        if kind=='finished':break
        if kind=='diaries':j.diary('choose',state='not_applicable')
        elif kind=='hours':j.diary('answer',state='not_applicable')
        elif kind=='profile':j.diary('confirm')
        else:j.guide_action('relevance','off') if kind=='relevance' else j.guide_action('next')
    assert kind=='finished'
    j.guide_action('finish')
    assert j.state()['guide']['status']=='finished'
    assert j.row('scheduling_config',j.tenant) is None and j.row('invoice_settings',j.tenant) is None
    assert all(r['state']=='off' for r in j.state()['reviews'].values())


def test_factual_answer_uses_actual_model_transport_schema_and_preserves_false(guide_journey,monkeypatch):
    j=guide_journey;j.move('question.records.relationships')
    seen=model(monkeypatch,[],facts=[dict(question='records.relationships',state='answered',value=False)])
    turn(j,'No, we do not track separate named subjects.')
    answer=next(a for a in j.state()['answers'].values() if a['question']=='records.relationships')
    assert answer['value'] is False
    assert 'guide_answers' in seen[0]['output_config']['format']['schema']['required']
    assert j.row('scheduling_config',j.tenant) is None


def test_malicious_scope_route_and_other_question_cannot_mutate(guide_journey,monkeypatch):
    j=guide_journey;j.move('diary_settings/min_notice_hours');before=j.state()
    j.guide_action('scope',scope_type='record_group',scope_id=j.other,expected=400)
    response=j.http.post('/app/setup/guide/action',json=dict(revision=before['revision'],stop='../other',operation='next',selector='body'))
    assert response.status_code==422
    model(monkeypatch,change('diary_settings',0,[dict(fields={'key':'unregistered_selector','value':50})]))
    turn(j,'Please explain notice.',expected=422)
    assert j.state()==before and j.row('scheduling_config',j.tenant) is None


def test_later_area_answer_is_held_once_without_advancing_or_applying(guide_journey,monkeypatch):
    j=guide_journey;j.move('question.activity.purpose');stop=j.state()['guide']['current']
    model(monkeypatch,change('invoice_settings',0,[dict(fields={'key':'payment_terms','value':'Within seven days'})]))
    result=turn(j,'For later, our payment terms are within seven days.').json()
    assert 'Kept that suggestion' in result['reply'] and j.state()['guide']['current']==stop
    card=j.row('tenant_config',j.tenant)['setup_cards']['invoice_settings']
    assert len(card['lines'])==1 and card['lines'][0]['status']=='held'
    model(monkeypatch,change('invoice_settings',card['revision'],[dict(fields={'key':'payment_terms','value':'Within seven days'})]))
    turn(j,'Our payment terms are within seven days.')
    assert len(j.row('tenant_config',j.tenant)['setup_cards']['invoice_settings']['lines'])==1
    assert len(j.state()['answers'])==1 and j.row('invoice_settings',j.tenant) is None


def test_pending_blocks_review_and_pause_later_preserve_card(guide_journey,monkeypatch):
    j=guide_journey;j.move('diary_settings/min_notice_hours')
    model(monkeypatch,change('diary_settings',0,[dict(fields={'key':'min_notice_hours','value':24})]))
    turn(j,'24 hours notice.')
    proposal=deepcopy(j.state()['guide']['pending'])
    j.guide_action('pause');assert j.state()['guide']['status']=='paused'
    j.guide_action('resume');assert j.state()['guide']['pending']==proposal
    j.guide_action('later');j.move('review.diary.time')
    j.guide_action('review',expected=400);j.guide_action('later')
    assert j.state()['reviews']['diary.time']['state']=='deferred'
    assert j.row('scheduling_config',j.tenant) is None


def accept_current(j,page,ids=None):
    card=j.row('tenant_config',j.tenant)['setup_cards'][page]
    ids=ids or [l['id'] for l in card['lines'] if l['status'] in ('proposed','held')]
    preview=j.http.post('/concierge/setup-cards/'+page+'/preview',json=dict(revision=card['revision'],ids=ids))
    assert preview.status_code==200,preview.text
    response=j.http.post('/concierge/setup-cards/'+page+'/accept',json=dict(revision=card['revision'],ids=ids,
        settings_version=preview.json()['settings_version']))
    assert response.status_code==200,response.text
    return response.json()['card']


def test_records_kind_and_first_detail_use_one_native_dependency_acceptance(guide_journey,monkeypatch,tmp_path):
    j=guide_journey;j.move('dependants/field')
    model(monkeypatch,change('dependants',0,[dict(name='group',fields={'label':'Item','plural':'Items','cardinality':'many'}),
        dict(name='field',fields={'kind':'Item','label':'Reference','type':'text','required':False,'durability':'once','gate':False,'hint':''})]))
    turn(j,'Track Items with an optional text Reference, remembered once. It never blocks a booking.')
    assert not j.row('scheduling_config',j.tenant)
    card=accept_current(j,'dependants')
    groups=j.query('select to_jsonb(g) from requirement_groups g where tenant_id=%s',j.tenant,all=True)
    assert len(groups)==1 and groups[0][0]['label']=='Item'
    assert groups[0][0]['fields'][0]['label']=='Reference'
    assert groups[0][0]['fields'][0]['required'] is False
    assert j.state()['guide']['decisions']['dependants/field']=='applied'
    (tmp_path/'records-receipts.json').write_text(json.dumps(dict(card=card,state=j.state(),groups=groups),indent=2),encoding='utf-8')


def test_records_kind_without_stated_plural_is_proposed_and_saved_with_derived_plural(guide_journey,monkeypatch):
    j=guide_journey;j.move('dependants/field')
    model(monkeypatch,change('dependants',0,[dict(name='group',fields={'label':'Property','cardinality':'many'}),
        dict(name='field',fields={'kind':'Property','label':'Address','type':'text','required':False,'durability':'once','gate':False,'hint':''})]))
    turn(j,'Call it Property. A landlord can have more than one. Keep the address for each.')
    accept_current(j,'dependants')
    groups=j.query('select to_jsonb(g) from requirement_groups g where tenant_id=%s',j.tenant,all=True)
    assert [(g[0]['label'],g[0]['label_plural'],g[0]['cardinality']) for g in groups]==[('Property','Properties','many')]


def test_records_kind_with_assumed_plural_is_still_rejected_without_storing(guide_journey,monkeypatch):
    j=guide_journey;j.move('dependants/field')
    proposal=change('dependants',0,[dict(name='group',fields={'label':'Property','plural':'Properties','cardinality':'many'})])
    plural=next(f for f in proposal[0]['changes'][0]['fields'] if f['field']=='plural')
    plural.update(source='assumed',reference='Standard English plural of Property')
    model(monkeypatch,proposal)
    response=turn(j,'Call it Property. A landlord can have more than one.',expected=422)
    assert 'assumed rather than supplied' in response.text
    assert 'dependants' not in (j.row('tenant_config',j.tenant).get('setup_cards') or {})


def test_charges_explicit_required_batch_saves_empty_false_values_without_payment_enablement(guide_journey,monkeypatch,tmp_path):
    j=guide_journey
    stop=next(s for s in guided_setup.registry().values() if s['page']=='invoice_settings' and len(s['catalogue'])>3)
    j.move(stop['id'])
    choices={'show_vat':False,'vat_rate':0,'prices_include_vat':False,'show_business_details':False,
             'show_payment_terms':False,'payment_terms':'','default_quote_valid_days':30}
    model(monkeypatch,change('invoice_settings',0,[dict(fields={'key':k,'value':v}) for k,v in choices.items()]))
    turn(j,'No VAT, no business details or payment terms printed; terms empty, quotes valid 30 days. Prices exclude VAT.')
    assert j.row('invoice_settings',j.tenant) is None
    accept_current(j,'invoice_settings')
    saved=j.row('invoice_settings',j.tenant)
    assert saved['payment_terms']=='' and saved['show_payment_terms'] is False and saved['vat_rate']==0
    assert not saved.get('stripe_account_id') and not saved.get('bank_account_number')
    assert j.row('scheduling_config',j.tenant) is None
    (tmp_path/'charges-receipts.json').write_text(json.dumps(dict(state=j.state(),settings=saved),indent=2),encoding='utf-8')


def test_partial_accept_then_leave_retains_saved_line_and_receipt(guide_journey,monkeypatch):
    j=guide_journey;j.move('diary_settings/min_notice_hours')
    model(monkeypatch,change('diary_settings',0,[dict(fields={'key':'min_notice_hours','value':24}),dict(fields={'key':'horizon_days','value':60})]))
    turn(j,'24 hours and 60 days.')
    card=j.row('tenant_config',j.tenant)['setup_cards']['diary_settings']
    line=next(l for l in card['lines'] if l['fields']['key']=='min_notice_hours')
    card=accept_current(j,'diary_settings',[line['id']])
    remaining=next(l for l in card['lines'] if l['status']=='proposed')
    response=j.http.post('/concierge/setup-cards/diary_settings/changes',json=dict(revision=card['revision'],changes=[dict(op='remove',id=remaining['id'])]))
    assert response.status_code==200,response.text
    proposal=j.state()['proposals'][j.state()['guide']['pending']['diary_settings/min_notice_hours']['proposal']]
    assert proposal['status']=='partial' and set(proposal['line_receipts'].values())=={'applied','left'}
    assert j.row('scheduling_config',j.tenant)['min_notice_hours']==24


def test_permission_and_view_as_are_enforced_for_guide_reads_and_writes(journey,monkeypatch):
    j=journey
    monkeypatch.setattr(routes,'require_manager',REAL_MANAGER)
    monkeypatch.setattr(db,'get_member',lambda *a,**kw:{'role':'accountant'})
    assert j.http.get('/app/setup/guide/state').status_code==403
    assert j.http.post('/app/setup/guide/action',json=dict(revision=0,stop='intro',operation='start')).status_code==403
    assert j.state()['revision']==0


def test_guide_migration_replay_rollback_preserves_history_and_service_only_grant(guide_journey,monkeypatch):
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    j=guide_journey;j.move('diary_settings/min_notice_hours')
    model(monkeypatch,change('diary_settings',0,[dict(fields={'key':'min_notice_hours','value':24})]))
    turn(j,'24 hours notice.')
    card=j.row('tenant_config',j.tenant)['setup_cards']['diary_settings'];line=card['lines'][0]
    edited=j.http.post('/concierge/setup-cards/diary_settings/changes',json=dict(revision=card['revision'],changes=[
        dict(op='set',id=line['id'],fields={'value':25},sources={'value':{'kind':'owner_said','reference':'Owner corrected notice'}})]))
    assert edited.status_code==200,edited.text
    accept_current(j,'diary_settings')
    before=j.state();cards=j.row('tenant_config',j.tenant)['setup_cards'];settings=j.row('scheduling_config',j.tenant)
    assert next(iter(before['answers'].values()))['history']
    signature="'save_onboarding_state(uuid,bigint,jsonb,jsonb)'"
    acl=j.query('select proacl from pg_proc where oid='+signature+'::regprocedure')[0]
    assert j.query('select has_function_privilege(\'anon\','+signature+',\'execute\')')[0] is False
    assert j.query('select has_function_privilege(\'service_role\','+signature+',\'execute\')')[0] is True
    j.execute((root/'db/migrations/20261023_guided_setup.rollback.sql').read_text())
    assert "'dependants'" not in j.query('select pg_get_functiondef('+signature+'::regprocedure)')[0]
    assert j.state()==before
    assert j.row('tenant_config',j.tenant)['setup_cards']==cards and j.row('scheduling_config',j.tenant)==settings
    assert j.query('select proacl from pg_proc where oid='+signature+'::regprocedure')[0]==acl
    assert j.query('select has_function_privilege(\'authenticated\','+signature+',\'execute\')')[0] is False
    j.execute((root/'db/migrations/20261023_guided_setup.sql').read_text())
    j.execute((root/'db/migrations/20261023_guided_setup.sql').read_text())
    assert "'dependants'" in j.query('select pg_get_functiondef('+signature+'::regprocedure)')[0]
    assert j.state()==before
    assert j.row('tenant_config',j.tenant)['setup_cards']==cards and j.row('scheduling_config',j.tenant)==settings
    assert j.query('select proacl from pg_proc where oid='+signature+'::regprocedure')[0]==acl


def test_next_past_absent_required_answer_cannot_mark_section_reviewed_or_finish(guide_journey):
    j=guide_journey;j.move('question.activity.purpose')
    j.guide_action('next')
    assert not j.state()['answers']
    j.move('review.diary.activities');j.guide_action('review',expected=400)
    assert 'diary.activities' not in j.state()['reviews']
    d=j.http.get('/app/setup/guide/state').json()
    assert any(s['id']=='question.activity.purpose' for s in d['unresolved'])
    j.guide_action('revisit','question.activity.purpose')
    assert j.state()['guide']['current']=='question.activity.purpose'
    j.move('finished');j.guide_action('finish',expected=400)
    j.move('review.diary.activities');j.guide_action('later')
    assert j.state()['reviews']['diary.activities']['state']=='deferred'


def test_expired_subscription_blocks_new_guide_reads_answers_and_positions(journey,monkeypatch):
    from app import auth,trial
    from app.config import settings
    j=journey
    monkeypatch.setattr(settings,'require_subscription_gate',True)
    monkeypatch.setattr(auth,'_subscription_tenant',lambda ctx:{'id':j.tenant,'subscription_status':'canceled'})
    monkeypatch.setattr(trial,'is_active',lambda row:False)
    for path in ('/app/setup/guide','/app/setup/guide/state'):
        r=j.http.get(path,follow_redirects=False)
        assert r.status_code==303 and r.headers['location']=='/billing/plans'
    for path in ('/action','/answer','/turn'):
        r=j.http.post('/app/setup/guide'+path,json={},follow_redirects=False)
        assert r.status_code==303 and r.headers['location']=='/billing/plans'
    assert j.state()['revision']==0


def test_service_edits_keep_different_activity_answers_isolated(guide_journey,monkeypatch):
    j=guide_journey
    services=[dict(name='Visit A',minutes=30,price=10,timing='time'),dict(name='Visit B',minutes=60,price=20,timing='time')]
    j.execute('insert into scheduling_config(tenant_id,service_types) values(%s,%s)',j.tenant,json.dumps(services))
    j.action('activity',{'service_name':'Visit B'})
    b=next(iter(j.state()['scopes']))
    key=j.answer('service.price',20,scope_type='activity',scope_id=b)
    old=deepcopy(j.state()['answers'][key]);j.move('diary_settings/service')
    seen=model(monkeypatch,change('diary_settings',0,[dict(name='service',fields={'name':'Visit A','minutes':45,'price':15})]))
    response=turn(j,'Visit A takes 45 minutes and costs 15.')
    assert not response.json().get('fallback'),seen[-1].get('synthetic_error') if seen else 'Model not reached'
    a=next(k for k,s in j.state()['scopes'].items() if s['service_name']=='Visit A')
    assert a!=b and j.state()['answers'][key]==old
    assert j.state()['answers'][onboarding_state.key_for('service.duration','activity',a)]['value']==45
    assert str(j.state()['answers'][onboarding_state.key_for('service.price','activity',a)]['value'])=='15'
    accept_current(j,'diary_settings')
    saved=j.row('scheduling_config',j.tenant)['service_types']
    assert next(s for s in saved if s['name']=='Visit A')['minutes']==45
    assert next(s for s in saved if s['name']=='Visit B')['price']==20
    assert j.state()['answers'][key]==old


def test_model_failure_keeps_question_and_typed_false_fallback_needs_no_provider(guide_journey,monkeypatch):
    j=guide_journey;j.move('question.records.relationships');before=j.state()
    async def unavailable(**kwargs):raise RuntimeError('synthetic unavailable')
    monkeypatch.setattr(llm,'create',unavailable)
    response=turn(j,'No separate subjects.')
    assert response.json()['fallback'] is True and j.state()==before
    response=j.http.post('/app/setup/guide/answer',json=dict(revision=before['revision'],stop=before['guide']['current'],state='answered',value=False))
    assert response.status_code==200,response.text
    key=onboarding_state.key_for('records.relationships','organisation','organisation')
    assert j.state()['answers'][key]['value'] is False
    assert j.row('scheduling_config',j.tenant) is None


def test_knowledge_continue_creates_intro_once_and_resumes_after_ack(journey,monkeypatch):
    from app import onboarding
    from app.auth import require_tenant
    from app.main import app
    j=journey;ctx=TenantCtx(j.tenant,j.rpc,'owner')
    app.dependency_overrides[require_tenant]=lambda:ctx
    monkeypatch.setattr(routes,'require_manager',lambda ctx:None)
    onboarded=[]
    monkeypatch.setattr(db,'set_onboarded',lambda *a,**k:onboarded.append(True))
    monkeypatch.setattr(db,'upsert_tenant_config',lambda *a,**k:None)
    monkeypatch.setattr(db,'active_modules',lambda *a,**k:{'scheduling','crm'})
    from app import knowledge_review
    monkeypatch.setattr(knowledge_review,'load',lambda tid:dict(sources=[dict(source='Reviewed facts',content='Confirmed business facts.',mailbox_id=None)],reviews=[],version='saved-facts'))
    before=j.state()
    refused=j.http.post('/onboarding/knowledge/text',data={'content':'','then':'continue'},follow_redirects=False)
    assert refused.status_code==422 and 'Complete Voice' in refused.text
    assert not onboarded and j.state()==before and not j.state().get('guide')
    seed_reviewed_voice(j)
    r=j.http.post('/onboarding/knowledge/text',data={'content':'','then':'continue'},follow_redirects=False)
    assert r.status_code==303 and r.headers['location']=='/app/setup/guide'
    assert onboarded==[True] and j.state()['guide']['current']=='intro' and not j.state()['guide']['intro_ack']
    before=j.state();r=j.http.get('/app/setup/guide');assert r.status_code==200
    assert j.state()==before
    r=j.http.post('/app/setup/guide/action',json=dict(revision=before['revision'],stop='intro',operation='start'))
    assert r.status_code==200 and j.state()['guide']['current']=='relevance.records.relationships'
    after=j.state();assert onboarding._prepare_guided_setup(ctx)=='/app/diary/dependants'
    assert j.state()==after

def test_guided_stream_scrolls_without_scrollbar_chrome():
    css=(__import__('pathlib').Path(__file__).resolve().parents[1]/'src/app/static/guided_setup.css').read_text(encoding='utf-8')
    stream=css.split('.guided-stream{')[1].split('}')[0]
    assert 'overflow-y:auto' in stream and 'scrollbar-width:none' in stream
    assert '.guided-stream::-webkit-scrollbar{display:none}' in css
