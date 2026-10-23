from copy import deepcopy
from types import SimpleNamespace
import json
from pathlib import Path
import shutil
import subprocess
import pytest
from fastapi import HTTPException
from app import knowledge_guidance as guide, knowledge_fidelity, knowledge_review
from tests.test_knowledge_review import context, entry
from tests.test_knowledge_source_routes import source_client


def fixture():
    ctx,row=context([entry(content='We charge £50 per service. Payment is due by agreement after the first visit.')],brief='')
    row['payload']['source_units']=knowledge_fidelity.inventory([{'url':'https://example.test/prices',
        'text':'Fictional test facts only.\nPayment is due within 14 days.\nReturn to home.'}])
    return ctx,row,{'version':ctx['version']}


def choice(intent, **kw):
    return {**dict(intent=intent,reason='',answer='',target='',explanation=''),**kw}


def act(ctx,row,form,intent,**kw):
    # This helper represents explicit deterministic review actions, not model approval.
    shown=guide.present(ctx,row,form)
    reply=guide.apply(ctx,row,form,shown['focus_hash'],choice(intent,**kw), confirmed_note_decision=True)
    form.update(reply.get('patches',{}))
    return reply


def test_guided_review_reaches_existing_admission_without_touching_stored_drafts():
    ctx,row,form=fixture(); original=deepcopy(ctx)
    first=guide.present(ctx,row,form)
    assert first['focus']['kind']=='note' and row['payload']['entries'][0]['content'] in first['reply']
    assert 'literal in note' not in first['reply'] and not form.get('action_one')
    kept=act(ctx,row,form,'keep')
    assert form['action_one']=='keep' and kept['focus']['kind']=='support'
    supported=act(ctx,row,form,'retain_support')
    payment = next(u for u in row['payload']['source_units'] if '14 days' in u['text'])
    navigation = next(u for u in row['payload']['source_units'] if u['text']=='Return to home.')
    assert form['source_'+payment['id']]=='note:one'
    assert not form.get('source_'+navigation['id'])
    assert supported['focus']['kind']=='source_batch' and supported['focus']['remaining']==1
    supported=act(ctx,row,form,'expand_sources')
    supported=act(ctx,row,form,'exclude',reason='This is a navigation link, not wording I want in my note.')
    assert 'Fictional test facts only.' in supported['shared_caveats']
    for _ in range(40):
        if supported['focus']['kind']=='ready':break
        assert supported['focus']['kind']=='question'
        supported=act(ctx,row,form,'unknown')
    assert supported['save_ready'] and 'nothing' in supported['reply'].lower()
    notes,payload=knowledge_review.plan(ctx,row,form)
    assert 'Payment is due within 14 days.' in notes[0]['content']
    assert 'Fictional test facts only.' in notes[0]['source_context']
    assert all(q['status']=='unknown' for t in payload['topic_reviews'].values() for q in t['questions'])
    assert ctx==original  # No persistence/LLM admission from any guide turn.


def test_actual_passage_review_needs_owner_reason_and_preserves_negative_limits():
    ctx,row,form=fixture()
    act(ctx,row,form,'keep'); opened=act(ctx,row,form,'details')
    assert opened['focus']['kind']=='passage'
    empty=act(ctx,row,form,'exclude')
    assert not empty['patches'] and empty['focus']['id']==opened['focus']['id']
    act(ctx,row,form,'exclude',reason='I do not want this sample payment term used.')
    assert any(k.startswith('source_reason_') for k in form)
    assert not any(k.startswith('source_') and v=='exclude' for k,v in form.items() if row['payload']['source_units'][0]['id'] in k)


def test_stale_focus_saved_reviews_and_foreign_targets_fail_closed():
    ctx,row,form=fixture(); focus=guide.present(ctx,row,form)['focus_hash']
    form['content_one']='Owner edited after reading.'
    with pytest.raises(HTTPException) as exc:
        guide.apply(ctx,row,form,focus,choice('keep'))
    assert exc.value.status_code==409
    act(ctx,row,form,'keep'); act(ctx,row,form,'details')
    if guide.present(ctx,row,form)['focus']['kind']=='source_batch':act(ctx,row,form,'expand_sources')
    with pytest.raises(HTTPException):act(ctx,row,form,'retain',target='other-tenant-note')
    row['state']='saved'
    with pytest.raises(HTTPException):guide.present(ctx,row,form)


def test_overlap_is_shown_and_never_silently_merged_or_approved():
    note=entry(); ctx,row=context([note],sources=[{'source':'Existing prices','content':note['content'],'mailbox_id':None}])
    form={'version':ctx['version']}; response=act(ctx,row,form,'keep')
    assert response['focus']['kind']=='overlap' and 'Existing prices' in response['reply']
    assert not any(k.startswith('match_') for k in form)
    response=act(ctx,row,form,'keep_both')
    assert any(k.startswith('match_') and v=='keep_both' for k,v in form.items())
    assert form['action_one']=='keep'


def test_owner_can_reopen_a_named_note_without_changing_its_wording():
    ctx,row,form=fixture(); original=row['payload']['entries'][0]['content']; act(ctx,row,form,'keep')
    response=act(ctx,row,form,'revisit',target='one')
    assert response['focus']['kind']=='note' and form['action_one']==''
    assert row['payload']['entries'][0]['content']==original


@pytest.mark.asyncio
async def test_show_and_explicit_buttons_do_not_spend_model_calls(monkeypatch):
    ctx,row,form=fixture()
    from app import llm
    monkeypatch.setattr(llm,'create',lambda **kw:pytest.fail('No model call for Show or explicit button'))
    shown=await guide.turn('tenant',ctx,row,form,'','')
    result=await guide.turn('tenant',ctx,row,form,'Keep this note',shown['focus_hash'])
    assert result['patches']=={'action_one':'keep'}


@pytest.mark.asyncio
async def test_natural_answer_cannot_invent_an_owner_reason(monkeypatch):
    ctx,row,form=fixture(); act(ctx,row,form,'keep'); shown=act(ctx,row,form,'details')
    from app import llm, usage, concierge
    async def model(**kw):
        assert kw['output_config']['format']['schema']['properties']['reason']['enum']==['', 'leave it out']
        return SimpleNamespace(content=[SimpleNamespace(type='text',text=json.dumps(choice('exclude',reason='invented policy')))],model='fake',usage={})
    monkeypatch.setattr(llm,'create',model)
    monkeypatch.setattr(concierge,'client',lambda:SimpleNamespace(with_options=lambda **kw:None))
    monkeypatch.setattr(usage,'record_anthropic',lambda *a,**kw:None)
    with pytest.raises(HTTPException) as exc:
        await guide.turn('tenant',ctx,row,form,'leave it out',shown['focus_hash'])
    assert exc.value.status_code==422


@pytest.mark.asyncio
async def test_natural_exclusion_accepts_typographic_apostrophe_without_rewriting_owner_words(monkeypatch):
    ctx,row,form=fixture(); act(ctx,row,form,'keep'); shown=act(ctx,row,form,'details')
    from app import llm, usage, concierge
    question='Leave that heading out. It’s only a page heading, and the actual prices and payment terms are already in the notes.'
    async def model(**kw):
        # Provider-constrained output returns the owner's original typography.
        reason=kw['output_config']['format']['schema']['properties']['reason']['enum'][1]
        return SimpleNamespace(content=[SimpleNamespace(type='text',text=json.dumps(choice('exclude',reason=reason)))],model='fake',usage={})
    monkeypatch.setattr(llm,'create',model)
    monkeypatch.setattr(concierge,'client',lambda:SimpleNamespace(with_options=lambda **kw:None))
    monkeypatch.setattr(usage,'record_anthropic',lambda *a,**kw:None)
    result=await guide.turn('tenant',ctx,row,form,question,shown['focus_hash'])
    assert result['patches']['source_'+shown['focus']['id']]=='exclude'
    assert result['patches']['source_reason_'+shown['focus']['id']]==question


OWNER_EXCLUSIONS = [
    'That sentence is already in the note, so please leave out the extra copy.',
    'I want to keep the note as it is. The sentence about keeping the landlord and resident '
    'informed is already included, so adding it again would repeat the same information.',
]


def test_reason_schema_bounds_and_observed_paraphrases():
    assert guide._turn_schema('')==guide.SCHEMA
    assert guide._turn_schema('x'*1000)['properties']['reason']['enum']==['','x'*1000]
    assert guide._turn_schema('x'*1001)==guide.SCHEMA
    paraphrases=[
        'That sentence is already in the note, so the extra copy should be left out.',
        'adding it again would repeat the same information already included in the note',
    ]
    for question,paraphrase in zip(OWNER_EXCLUSIONS,paraphrases):
        assert paraphrase not in guide._turn_schema(question)['properties']['reason']['enum']


@pytest.mark.parametrize('question', OWNER_EXCLUSIONS)
def test_natural_duplicate_reason_uses_owner_text_through_guide_route(source_client, monkeypatch, question):
    from app import llm, usage, concierge
    client, existing, _, route_context, calls, _ = source_client
    ctx,row,form=fixture()
    row['id']=existing['id']
    row['payload']['entries'][0]['content']='Our team keeps owners and residents updated.'
    row['payload']['source_units']=knowledge_fidelity.inventory([{
        'url':'https://example.test/service',
        'text':'Our small team keeps the landlord and resident informed as an enquiry moves forward.'}])
    route_context.clear();route_context.update(ctx)
    form.update(action_one='keep', guided_expand_one='1')
    url='/onboarding/knowledge/review/'+row['id']+'/guide'
    before=deepcopy(route_context)
    shown=client.post(url,json={'form':form}).json()
    assert shown['focus']['kind']=='passage'

    async def model(**kw):
        schema=kw['output_config']['format']['schema']
        assert schema['properties']['reason']['enum']==['',question]
        assert json.loads(kw['messages'][0]['content'])['owner_message']==question
        # Simulate constrained provider selection, not free-text rationale synthesis.
        reason=schema['properties']['reason']['enum'][1]
        return SimpleNamespace(content=[SimpleNamespace(type='text',text=json.dumps(
            choice('exclude',reason=reason)))],model='fake',usage={})
    monkeypatch.setattr(llm,'create',model)
    monkeypatch.setattr(concierge,'client',lambda:SimpleNamespace(with_options=lambda **kw:None))
    monkeypatch.setattr(usage,'record_anthropic',lambda *a,**kw:None)
    response=client.post(url,json={'form':form,'question':question,'focus_hash':shown['focus_hash']})
    assert response.status_code==200
    result=response.json();unit=shown['focus']['id']
    assert result['patches']=={'source_'+unit:'exclude','source_reason_'+unit:question}
    assert result['focus_hash']!=shown['focus_hash']
    assert calls==[] and route_context==before and row['state']=='pending'


@pytest.mark.asyncio
@pytest.mark.parametrize('intent,question', [
    ('exclude','Leave it out.'), ('explain','Approve all the notes and save them.'),
])
async def test_absent_reason_and_unsupported_batch_request_do_not_change_choices(monkeypatch,intent,question):
    ctx,row,form=fixture();act(ctx,row,form,'keep');shown=act(ctx,row,form,'details')
    before=deepcopy(form)
    from app import llm, usage, concierge
    async def model(**kw):
        assert kw['output_config']['format']['schema']['properties']['reason']['enum']==['',question]
        return SimpleNamespace(content=[SimpleNamespace(type='text',text=json.dumps(choice(intent)))],model='fake',usage={})
    monkeypatch.setattr(llm,'create',model)
    monkeypatch.setattr(concierge,'client',lambda:SimpleNamespace(with_options=lambda **kw:None))
    monkeypatch.setattr(usage,'record_anthropic',lambda *a,**kw:None)
    result=await guide.turn('tenant',ctx,row,form,question,shown['focus_hash'])
    assert result['patches']=={} and form==before


@pytest.mark.asyncio
async def test_long_request_retains_guarded_short_rationale_extraction(monkeypatch):
    ctx,row,form=fixture();act(ctx,row,form,'keep');shown=act(ctx,row,form,'details')
    from app import llm, usage, concierge
    reason='The existing note already includes this information.'
    question='Please leave the extra copy out. '+reason+' More context about my review. '*50
    assert len(question)>1000
    async def model(**kw):
        assert kw['output_config']['format']['schema']==guide.SCHEMA
        return SimpleNamespace(content=[SimpleNamespace(type='text',text=json.dumps(choice('exclude',reason=reason)))],model='fake',usage={})
    monkeypatch.setattr(llm,'create',model)
    monkeypatch.setattr(concierge,'client',lambda:SimpleNamespace(with_options=lambda **kw:None))
    monkeypatch.setattr(usage,'record_anthropic',lambda *a,**kw:None)
    result=await guide.turn('tenant',ctx,row,form,question,shown['focus_hash'])
    assert result['patches']['source_reason_'+shown['focus']['id']]==reason


@pytest.mark.asyncio
async def test_long_request_preserves_typographic_apostrophe_in_owner_excerpt(monkeypatch):
    ctx,row,form=fixture();act(ctx,row,form,'keep');shown=act(ctx,row,form,'details')
    from app import llm, usage, concierge
    reason="It's only a page heading, and the terms are already in the notes."
    owner_reason=reason.replace("'",'’')
    question='Leave the heading out. '+owner_reason+' More context about this review. '*50
    async def model(**kw):
        assert kw['output_config']['format']['schema']==guide.SCHEMA
        return SimpleNamespace(content=[SimpleNamespace(type='text',text=json.dumps(choice('exclude',reason=reason)))],model='fake',usage={})
    monkeypatch.setattr(llm,'create',model)
    monkeypatch.setattr(concierge,'client',lambda:SimpleNamespace(with_options=lambda **kw:None))
    monkeypatch.setattr(usage,'record_anthropic',lambda *a,**kw:None)
    result=await guide.turn('tenant',ctx,row,form,question,shown['focus_hash'])
    assert result['patches']['source_reason_'+shown['focus']['id']]==owner_reason


@pytest.mark.asyncio
async def test_answer_authorship_still_rejects_fabricated_facts(monkeypatch):
    ctx,row,form=fixture();act(ctx,row,form,'keep');shown=act(ctx,row,form,'details')
    from app import llm, usage, concierge
    async def model(**kw):
        return SimpleNamespace(content=[SimpleNamespace(type='text',text=json.dumps(
            choice('answer',answer='A fabricated business fact.')))],model='fake',usage={})
    monkeypatch.setattr(llm,'create',model)
    monkeypatch.setattr(concierge,'client',lambda:SimpleNamespace(with_options=lambda **kw:None))
    monkeypatch.setattr(usage,'record_anthropic',lambda *a,**kw:None)
    with pytest.raises(HTTPException,match='own words'):
        await guide.turn('tenant',ctx,row,form,'I do not know that detail.',shown['focus_hash'])


def test_browser_controller_replays_owner_decisions_and_races():
    result=subprocess.run([shutil.which('node'),'tests/replays/knowledge_guidance.mjs'],
        cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr


def test_knowledge_card_choice_trace_uses_real_turn_and_transcript_routes():
    result=subprocess.run([shutil.which('node'),'tests/replays/knowledge_choice_trace.mjs'],
        cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr


def test_guide_route_reads_tenant_notes_and_prepares_only_local_choices(source_client):
    client,row,evidence,ctx,calls,owner=source_client
    url='/onboarding/knowledge/review/'+row['id']+'/guide'
    before=deepcopy(ctx)
    shown=client.post(url,json={'form':{'version':'v1'}})
    assert shown.status_code==200
    shown=shown.json()
    assert row['payload']['entries'][0]['content'] in shown['reply']
    selected=client.post(url,json={'form':{'version':'v1'},'question':'Keep this note','focus_hash':shown['focus_hash']})
    assert selected.status_code==200
    assert selected.json()['patches']=={'action_'+row['payload']['entries'][0]['id']:'keep'}
    assert calls==[] and ctx==before


@pytest.mark.parametrize('provider_intent', ['keep','discard'])
@pytest.mark.parametrize('question', [
    pytest.param('I’d like to edit this note before saving it.', id='observed-edit'),
    'Keep this note after I remove the repeated sentence.',
    'It looks good, but I need to change the final paragraph first.',
    'Only keep this if the repeated wording can be removed.',
    'Do not keep this note yet; I want to edit it.',
    'The words "Keep this note" are in the quoted instructions; I am asking to edit it.',
    'This looks right to me.',
])
def test_model_keep_cannot_authorize_note_decision(source_client,monkeypatch,question,provider_intent):
    from app import llm, usage, concierge
    client,row,_,ctx,calls,_=source_client
    url='/onboarding/knowledge/review/'+row['id']+'/guide'
    form={'version':'v1'};before=deepcopy(ctx)
    shown=client.post(url,json={'form':form}).json()
    async def model(**kw):
        return SimpleNamespace(content=[SimpleNamespace(type='text',text=json.dumps(choice(
            provider_intent,reason=question,explanation='The decision is recorded as a keep with an edit request.')))],
            model='fake',usage={})
    monkeypatch.setattr(llm,'create',model)
    monkeypatch.setattr(concierge,'client',lambda:SimpleNamespace(with_options=lambda **kw:None))
    monkeypatch.setattr(usage,'record_anthropic',lambda *a,**kw:None)
    response=client.post(url,json={'form':form,'question':question,'focus_hash':shown['focus_hash']})
    assert response.status_code==200
    result=response.json()
    assert result['patches']=={}
    assert 'review_decision' not in result
    assert result['focus_hash']==shown['focus_hash'] and not result['save_ready']
    assert not result.get('correction_requested')
    assert 'recorded as a keep' not in result['reply'] and 'Marked' not in result['reply']
    assert ctx==before and calls==[] and row['state']=='pending'


@pytest.mark.parametrize('question,decision', [('Keep this note','keep'),('Discard this note','discard')])
def test_exact_note_action_confirms_without_model(source_client,monkeypatch,question,decision):
    from app import llm
    client,row,_,ctx,calls,_=source_client
    monkeypatch.setattr(llm,'create',lambda **kw:pytest.fail('Explicit action must not call a model'))
    url='/onboarding/knowledge/review/'+row['id']+'/guide';form={'version':'v1'}
    shown=client.post(url,json={'form':form}).json()
    response=client.post(url,json={'form':form,'question':question,'focus_hash':shown['focus_hash']})
    assert response.status_code==200
    assert response.json()['patches']=={'action_'+shown['focus']['id']:decision}
    assert calls==[] and row['state']=='pending'


@pytest.mark.parametrize('previous', ['','keep','discard'])
@pytest.mark.parametrize('provider_intent', ['edit','keep','discard'])
def test_edit_interpretation_preserves_prior_decisions(source_client,monkeypatch,previous,provider_intent):
    from app import llm, usage, concierge
    client,row,_,ctx,calls,_=source_client
    first=row['payload']['entries'][0]['id'];form={'version':'v1','action_'+first:previous}
    before=deepcopy(ctx);url='/onboarding/knowledge/review/'+row['id']+'/guide'
    shown=client.post(url,json={'form':form}).json()
    async def model(**kw):
        return SimpleNamespace(content=[SimpleNamespace(type='text',text=json.dumps(choice(
            provider_intent,explanation='Kept and edited the earlier note.')))],model='fake',usage={})
    monkeypatch.setattr(llm,'create',model)
    monkeypatch.setattr(concierge,'client',lambda:SimpleNamespace(with_options=lambda **kw:None))
    monkeypatch.setattr(usage,'record_anthropic',lambda *a,**kw:None)
    response=client.post(url,json={'form':form,'question':'I want to edit the earlier note before deciding.',
                                  'focus_hash':shown['focus_hash']})
    assert response.status_code==200
    result=response.json()
    assert result['patches']=={} and result['focus_hash']==shown['focus_hash']
    assert not result['save_ready'] and not result.get('correction_requested')
    if 'review_decision' in result:
        assert provider_intent=='edit'
        assert result['review_decision']['action']=='edit' and result['review_decision']['state']=='editing'
    assert form['action_'+first]==previous and ctx==before and calls==[]


def test_explicit_confirmation_still_requires_current_focus(source_client,monkeypatch):
    from app import llm
    client,row,_,ctx,calls,_=source_client
    monkeypatch.setattr(llm,'create',lambda **kw:pytest.fail('Stale confirmation must not call a model'))
    url='/onboarding/knowledge/review/'+row['id']+'/guide';form={'version':'v1'}
    shown=client.post(url,json={'form':form}).json()
    form['content_'+shown['focus']['id']]='Owner changed the note after reading it.'
    response=client.post(url,json={'form':form,'question':'Keep this note','focus_hash':shown['focus_hash']})
    assert response.status_code==409 and calls==[]


def test_preview_preserves_guide_progress_without_exposing_more_audit_labels(source_client):
    client,row,evidence,ctx,calls,owner=source_client
    response=client.post('/onboarding/knowledge/review/'+row['id'],data={
        'version':'v1','operation':'preview','guided_unknown_q':'1','guided_expand_note':'1'},
        headers={'accept':'application/json'})
    assert response.status_code==200
    html=response.json()['review_html']
    assert 'name="guided_unknown_q" value="1"' in html
    assert 'name="guided_expand_note" value="1"' in html
    assert calls==[]


@pytest.mark.parametrize('case,expected',[('flag',404),('permission',403),('viewing',403),('stale',409),('saved',409),('missing',404),('csrf',403)])
def test_guide_reuses_tenant_permission_csrf_and_version_gates(source_client,monkeypatch,case,expected):
    from app import knowledge_source_routes as routes
    from app.main import app
    from app.security import require_csrf_header
    client,row,evidence,ctx,calls,owner=source_client
    if case=='flag':monkeypatch.setattr(routes.settings,'knowledge_source_review_enabled',False)
    elif case=='permission':monkeypatch.setattr(routes.people,'can_do',lambda *a:False)
    elif case=='viewing':owner.viewing_as='another-owner'
    elif case=='stale':ctx['version']='v2'
    elif case=='saved':row['state']='saved'
    elif case=='missing':ctx['reviews']=[]
    else:app.dependency_overrides.pop(require_csrf_header)
    response=client.post('/onboarding/knowledge/review/'+row['id']+'/guide',json={'form':{'version':'v1'}})
    assert response.status_code==expected and calls==[]


@pytest.mark.parametrize('intent', ['keep', 'discard', 'edit'])
def test_decision_receipt_identifies_submitted_note_before_advancing(intent):
    ctx,row,form=fixture(); shown=guide.present(ctx,row,form)
    result=act(ctx,row,form,intent)
    assert result['review_decision']==dict(batch_id=row['id'],version=ctx['version'],
        focus_hash=shown['focus_hash'],kind='note',id='one',action=intent,
        state='editing' if intent=='edit' else 'prepared')
    assert result.get('edit_requested',False)==(intent=='edit')
    if intent in {'keep','discard'}:assert 'last note, Prices' in result['reply']
    if intent=='edit':assert result['patches']=={} and form=={'version':ctx['version']}
    else:assert result['focus_hash']!=shown['focus_hash']


def test_overlap_receipt_does_not_use_the_next_note_identity():
    note=entry();ctx,row=context([note],sources=[{'source':'Existing prices','content':note['content'],'mailbox_id':None}])
    form={'version':ctx['version']};shown=act(ctx,row,form,'keep')
    result=act(ctx,row,form,'keep_both')
    assert result['review_decision']['focus_hash']==shown['focus_hash']
    assert result['review_decision']['action']=='keep_both'
    assert result['review_decision']['kind']=='overlap'
    assert result['focus_hash']!=shown['focus_hash']


@pytest.mark.parametrize('intent',['show','explain','exclude','keep_both'])
def test_nondecisions_and_inapplicable_intents_have_no_choice_receipt(intent):
    ctx,row,form=fixture();result=act(ctx,row,form,intent)
    assert not result['patches'] and 'review_decision' not in result


@pytest.mark.asyncio
@pytest.mark.parametrize('question', ['edit it','change this','Edit this note'])
async def test_explicit_editor_requests_do_not_call_a_model_or_approve_notes(monkeypatch,question):
    from app import llm
    monkeypatch.setattr(llm,'create',lambda **kw:pytest.fail('Editor shortcut needs no model'))
    ctx,row,form=fixture();shown=guide.present(ctx,row,form)
    result=await guide.turn('tenant',ctx,row,form,question,shown['focus_hash'])
    assert result['edit_requested'] and result['review_decision']['state']=='editing'
    assert result['patches']=={}


@pytest.mark.asyncio
@pytest.mark.parametrize('question,intent', [('keep that one','keep'),('leave that one out','discard'),('I want to edit it','edit')])
async def test_model_interpretations_only_trace_editing_not_note_approval(monkeypatch,question,intent):
    from app import llm,usage,concierge
    async def model(**kwargs):
        return SimpleNamespace(content=[SimpleNamespace(type='text',text=json.dumps(choice(intent)))],model='fake',usage={})
    monkeypatch.setattr(llm,'create',model)
    monkeypatch.setattr(concierge,'client',lambda:SimpleNamespace(with_options=lambda **kw:None))
    monkeypatch.setattr(usage,'record_anthropic',lambda *a,**kw:None)
    ctx,row,form=fixture();shown=guide.present(ctx,row,form)
    result=await guide.turn('tenant',ctx,row,form,question,shown['focus_hash'])
    assert result['patches']=={} and result['focus_hash']==shown['focus_hash'] and not result['save_ready']
    if intent=='edit':
        assert result['edit_requested']
        assert result['review_decision']['action']=='edit' and result['review_decision']['state']=='editing'
        assert result['review_decision']['focus_hash']==shown['focus_hash']
    else:
        assert 'review_decision' not in result


def test_unconfirmed_model_choice_has_no_history_until_exact_confirmation(source_client,monkeypatch):
    from app import llm,usage,concierge
    client,row,_,ctx,calls,_=source_client
    before=deepcopy(ctx);url='/onboarding/knowledge/review/'+row['id']+'/guide';form={'version':'v1'}
    shown=client.post(url,json={'form':form}).json();provider_calls=[]
    async def model(**kwargs):
        provider_calls.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type='text',text=json.dumps(choice(
            'keep',explanation='Kept while editing.')))],model='fake',usage={})
    monkeypatch.setattr(llm,'create',model)
    monkeypatch.setattr(concierge,'client',lambda:SimpleNamespace(with_options=lambda **kw:None))
    monkeypatch.setattr(usage,'record_anthropic',lambda *a,**kw:None)
    proposed=client.post(url,json={'form':form,'question':'I want to edit this note before keeping it.',
                                  'focus_hash':shown['focus_hash']}).json()
    assert proposed['patches']=={} and 'review_decision' not in proposed and not proposed['save_ready']
    assert proposed['focus_hash']==shown['focus_hash']
    confirmed=client.post(url,json={'form':form,'question':'Keep this note',
                                   'focus_hash':proposed['focus_hash']})
    assert confirmed.status_code==200
    accepted=confirmed.json()
    assert accepted['patches']=={'action_'+shown['focus']['id']:'keep'}
    assert accepted['review_decision']==dict(batch_id=row['id'],version=ctx['version'],
        focus_hash=shown['focus_hash'],kind='note',id=shown['focus']['id'],action='keep',state='prepared')
    assert len(provider_calls)==1 and ctx==before and calls==[] and row['state']=='pending'
