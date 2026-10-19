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
    shown=guide.present(ctx,row,form)
    reply=guide.apply(ctx,row,form,shown['focus_hash'],choice(intent,**kw))
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
    assert supported['focus']['kind']=='passage' and supported['focus']['target']==''
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
        assert kw['output_config']['format']['schema']==guide.SCHEMA
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
    reason='It\'s only a page heading, and the actual prices and payment terms are already in the notes.'
    async def model(**kw):
        return SimpleNamespace(content=[SimpleNamespace(type='text',text=json.dumps(choice('exclude',reason=reason)))],model='fake',usage={})
    monkeypatch.setattr(llm,'create',model)
    monkeypatch.setattr(concierge,'client',lambda:SimpleNamespace(with_options=lambda **kw:None))
    monkeypatch.setattr(usage,'record_anthropic',lambda *a,**kw:None)
    question='Leave that heading out. It’s only a page heading, and the actual prices and payment terms are already in the notes.'
    result=await guide.turn('tenant',ctx,row,form,question,shown['focus_hash'])
    assert result['patches']['source_'+shown['focus']['id']]=='exclude'
    assert result['patches']['source_reason_'+shown['focus']['id']]=='It’s only a page heading, and the actual prices and payment terms are already in the notes.'


def test_browser_controller_replays_owner_decisions_and_races():
    result=subprocess.run([shutil.which('node'),'tests/replays/knowledge_guidance.mjs'],
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
