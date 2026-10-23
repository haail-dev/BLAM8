from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from app import knowledge_review as kr,knowledge as kb,concierge_routes as cr
from app.auth import TenantCtx,require_tenant
from app.main import app
from app.security import require_csrf
from tests.knowledge_review_fixture import review_store


def test_saved_page_uses_note_titles_and_authoritative_status(client, review_store, monkeypatch):
    from app import db, onboarding
    row = review_store.stage('tenant', [{'label':'Service hours', 'content':'We close at four.'}])
    row['state'] = 'saved'
    source = 'Service hours · 1234abcd'
    row['payload']['entries'][0]['admitted_source'] = source
    review_store.sources = [{'source':source,'content':'We close at four.','mailbox_id':None,'chunks':1}]
    monkeypatch.setattr(db, 'get_tenant_config', lambda *a, **k: {'agent_name':'Team','tone':'Clear','sign_off':'Thanks'})
    monkeypatch.setattr(onboarding, '_next_step_target', lambda ctx: {'href':'/app/diary','label':'Guided setup'})
    response = client.get('/onboarding/knowledge')
    assert response.status_code == 200
    from bs4 import BeautifulSoup
    page = BeautifulSoup(response.text, 'html.parser')
    assert page.select_one('[data-kb-note] > summary').get_text(' ',strip=True) == 'Service hours View note Close'
    assert source in page.select_one('[data-kb-note] details').get_text()
    assert '1 note saved. No drafts waiting for review.' in page.select_one('[data-knowledge-saved-status]').get_text()
    assert page.select_one('[data-knowledge-saved-status]').get_text().startswith('Knowledge saved 1 note')
    assert row['id'] in page.select_one('[data-knowledge-saved-reviews]')['data-knowledge-saved-reviews']
    assert 'topics have saved notes' not in response.text
    # Starting another batch must not falsely say all reviews are complete.
    review_store.stage('tenant', [{'label':'New fact', 'content':'A new pending note.'}])
    pending = BeautifulSoup(client.get('/onboarding/knowledge').text, 'html.parser')
    assert pending.select_one('[data-knowledge-saved-status]') is None
    assert pending.select_one('[data-knowledge-review]')


def test_automatic_correction_recheck_does_not_rebase_a_concurrent_edit(client, review_store):
    from copy import deepcopy
    row = review_store.stage('tenant', [{'label':'Hours','content':'We close at four.'}])
    before = deepcopy(review_store.rows)
    response = client.post('/onboarding/knowledge/review/'+row['id'],
        headers={'Accept':'application/json'}, data={'operation':'preview','version':'old',
        'correction_recheck_version':'another-device-changed-this'})
    assert response.status_code == 409
    assert 'review changed after your correction' in response.json()['detail']
    assert review_store.rows == before and not review_store.sources


@pytest.fixture
def client(monkeypatch,review_store):
    app.dependency_overrides[require_tenant]=lambda:TenantCtx(tenant_id='tenant',client=None)
    app.dependency_overrides[require_csrf]=lambda:None
    monkeypatch.setattr(kb,'is_configured',lambda:True)
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_preview_and_unresolved_overlap_save_nothing_and_preserve_edits(client,review_store,monkeypatch):
    review_store.brief='We provide a scheduled support service for customers.'
    row=review_store.stage('tenant',[{'label':'What we do','content':review_store.brief}])
    view=client.get('/onboarding/knowledge/review/'+row['id'])
    assert view.status_code==200 and 'Possible overlap: Business brief' in view.text
    version=review_store.load('tenant')['version'];id=row['payload']['entries'][0]['id']
    f={'version':version,'operation':'save','action_'+id:'keep','content_'+id:review_store.brief+' Extra fact.'}
    r=client.post('/onboarding/knowledge/review/'+row['id'],data=f)
    assert r.status_code==409 and 'Extra fact.' in r.text and review_store.sources==[]
    f['operation']='preview'
    r=client.post('/onboarding/knowledge/review/'+row['id'],data=f)
    assert r.status_code==200 and 'Extra fact.' in r.text and review_store.sources==[]
    assert client.get('/onboarding/knowledge/review/missing').status_code==404


def test_review_preserves_assistant_context_and_json_preview_has_explicit_result(client,review_store):
    from copy import deepcopy
    row=review_store.stage('tenant',[{'label':'Policy','content':'Only confirmed requests proceed.'}])
    before=deepcopy(review_store.rows); path='/onboarding/knowledge/review/'+row['id']
    page=client.get(path)
    assert 'id="bc-panel"' in page.text and 'data-onboarding-step="knowledge"' in page.text
    assert 'id="onboarding-journey"' in page.text
    identity=row['payload']['entries'][0]['id'];version=review_store.load('tenant')['version']
    response=client.post(path,headers={'Accept':'application/json'},data={'operation':'preview','version':version,
        'content_'+identity:'Owner edited this text.','action_'+identity:'keep'})
    assert response.status_code==200 and 'No facts saved' in response.json()['summary']
    assert 'Owner edited this text.' in response.json()['review_html']
    assert 'value="keep" selected' in response.json()['review_html']
    assert [r['payload']['entries'] for r in review_store.rows]==[r['payload']['entries'] for r in before] and review_store.sources==[]


def test_assistant_questions_and_drafts_are_stored_and_linked_before_admission(client,review_store,monkeypatch):
    async def reply(*a,**kw):
        return SimpleNamespace(reply='Does the price vary with size?',form_fill={},ready_to_advance=False,
            notes=[{'label':'Prices','content':'We charge £50.'}],unanswered=[{'topic':'prices','question':'Does the price vary with size?'}],held=[],model='test',usage={})
    monkeypatch.setattr(cr.concierge,'answer_onboarding',reply)
    monkeypatch.setattr(cr.fairuse,'onboarding_allowed',lambda *a,**kw:True)
    monkeypatch.setattr(cr,'_tenant_context',lambda ctx:{})
    monkeypatch.setattr(cr.usage,'record_anthropic',lambda *a,**kw:None)
    from app import setup_held
    monkeypatch.setattr(setup_held,'load',lambda *a,**kw:[])
    r=client.post('/concierge/onboard',json={'step':'knowledge','question':'Help with prices'})
    assert r.status_code==200 and r.json()['review_link']['href'].startswith('/onboarding/knowledge/review/')
    assert review_store.rows[-1]['payload']['questions'][0]['question']=='Does the price vary with size?'
    page=client.get(r.json()['review_link']['href'])
    assert 'Does the price vary with size?' in page.text and review_store.sources==[]


@pytest.mark.parametrize('pending',[True,False])
def test_review_only_assistant_never_invents_replacement_cards(client,review_store,monkeypatch,pending):
    from copy import deepcopy
    if pending:
        row=review_store.stage('tenant',[{'label':'Starting service','content':'The monthly fee is £95.'}])
    before=deepcopy(review_store.rows)
    async def forbidden(*a,**kw):
        pytest.fail('Review corrections must not call an ungrounded prose generator')
    monkeypatch.setattr(cr.concierge,'answer_onboarding',forbidden)
    monkeypatch.setattr(cr.fairuse,'onboarding_allowed',lambda *a,**kw:True)
    r=client.post('/concierge/onboard',json={'step':'knowledge',
        'question':'Give source-faithful replacement cards for review only.',
        'current':{'note':'Unsaved browser edit'},
        'history':[{'role':'assistant','content':'Invented fee 10% and an imaginary First Report card.'}]})
    assert r.status_code==200
    data=r.json()
    assert data['notes']==[] and data['form_fill']=={} and not data['ready_to_advance']
    assert '10%' not in data['reply'] and 'First Report' not in data['reply']
    assert [r['payload']['entries'] for r in review_store.rows]==[r['payload']['entries'] for r in before] and review_store.sources==[]
    if pending:
        assert 'Starting service' in data['reply']
        assert data['review_link']['href'].endswith(row['id'])
    else:
        assert "can't verify" in data['reply']


def test_source_inventory_blocks_admission_and_preserves_corrections(client,review_store,monkeypatch):
    from app.knowledge_fidelity import inventory
    from app import setup_card_knowledge
    row=review_store.stage('tenant',[{'label':'Starting service','content':'The monthly fee is £95.'}],
        origin='Website',source_units=inventory([{'url':'https://example.test','text':'Approval must precede a booking.'}]))
    async def forbidden(*a,**kw):pytest.fail('Unresolved source coverage must fail before embedding')
    monkeypatch.setattr(setup_card_knowledge,'prepare',forbidden)
    id=row['payload']['entries'][0]['id']
    r=client.post('/onboarding/knowledge/review/'+row['id'],data={
        'version':review_store.load('tenant')['version'],'operation':'save',
        'action_'+id:'keep','content_'+id:'The corrected fee is £95.'})
    assert r.status_code==409 and 'corrected fee' in r.text
    unit=row['payload']['source_units'][0]
    assert unit['text']=='Approval must precede a booking.' and review_store.sources==[]
    assert f'select name="source_{unit["id"]}"' in r.text
    assert 'Website evidence and optional details' in r.text
    assert 'Source-to-note review' not in r.text


@pytest.mark.parametrize('admitted',[0,1])
def test_discarded_notes_do_not_consume_facts_held_for_later(client,review_store,monkeypatch,admitted):
    from app import setup_held
    row=review_store.stage('tenant',[{'label':'Hours','content':'We open at nine.'}])
    used=[]
    async def admit(*a,**kw):return admitted
    monkeypatch.setattr(kr,'admit',admit)
    monkeypatch.setattr(setup_held,'mark_used',lambda *a,**kw:used.append(a))
    r=client.post('/onboarding/knowledge/review/'+row['id'],data={'operation':'save'},follow_redirects=False)
    assert r.status_code==303 and len(used)==admitted


def test_many_long_draft_titles_do_not_break_the_next_chat_turn(client,review_store,monkeypatch):
    review_store.stage('tenant',[{'label':str(i)+'x'*157,'content':'An actual stored fact.'} for i in range(30)])
    monkeypatch.setattr(cr.fairuse,'onboarding_allowed',lambda *a,**kw:True)
    first=client.post('/concierge/onboard',json={'step':'knowledge','question':'Review my drafts'})
    assert first.status_code==200 and len(first.json()['reply'])<=cr.MAX_TURN_CHARS
    assert '22 more draft titles' in first.json()['reply']
    followup=client.post('/concierge/onboard',json={'step':'knowledge','question':'Show that review',
        'history':[{'role':'assistant','content':first.json()['reply']}]})
    assert followup.status_code==200 and len(review_store.rows)==1 and review_store.sources==[]


def test_overlap_acknowledged_through_the_guide_is_honoured_by_the_native_save(client,review_store,monkeypatch):
    from app import knowledge_source_routes as routes,security,setup_card_knowledge,setup_held,usage
    from app.security import require_csrf_header
    from tests.test_knowledge_review import PARAGRAPHS
    monkeypatch.setattr(security,'_BUCKETS',{})
    monkeypatch.setattr(routes.settings,'knowledge_source_review_enabled',True)
    monkeypatch.setattr(routes.db,'get_member',lambda *a,**k:None)
    monkeypatch.setattr(routes.people,'can_do',lambda *a:True)
    app.dependency_overrides[require_csrf_header]=lambda:None
    review_store.brief=PARAGRAPHS[0]
    row=review_store.stage('tenant',[{'label':'Services','content':'\n\n'.join(PARAGRAPHS)}])
    path='/onboarding/knowledge/review/'+row['id'];id=row['payload']['entries'][0]['id']
    # The guide reads the form with FormData, so textarea newlines travel as LF.
    guide={'version':review_store.load('tenant')['version'],'label_'+id:'Services','content_'+id:'\n\n'.join(PARAGRAPHS),'action_'+id:'keep'}
    shown=client.post(path+'/guide',json={'form':guide,'question':'','focus_hash':''})
    assert shown.status_code==200 and shown.json()['focus']['kind']=='overlap'
    guide['version']=shown.json()['checkpoint']['version']
    acknowledged=client.post(path+'/guide',json={'form':guide,'question':'Keep both notes','focus_hash':shown.json()['focus_hash']})
    assert acknowledged.status_code==200
    guide['version']=acknowledged.json()['checkpoint']['version']
    (match_name,)=acknowledged.json()['patches']
    assert match_name.startswith('match_') and acknowledged.json()['patches'][match_name]=='keep_both'
    calls=[]
    async def prepare(patch):
        return {'notes':[{**v,'chunks':[{'content':v['content'],'embedding':'[0.1]'}]} for v in patch['notes']]},1
    monkeypatch.setattr(setup_card_knowledge,'prepare',prepare)
    monkeypatch.setattr(usage,'record_voyage',lambda *a,**kw:None)
    monkeypatch.setattr(setup_held,'mark_used',lambda *a,**kw:None)
    class Client:
        def rpc(self,name,payload):calls.append((name,payload));return SimpleNamespace(execute=lambda:SimpleNamespace(data=True))
    monkeypatch.setattr(kr.db,'db',lambda:Client())
    # Save reviewed decisions is a native form submission: the browser sends textarea newlines as CRLF.
    native={**guide,**acknowledged.json()['patches'],'operation':'save','content_'+id:'\r\n\r\n'.join(PARAGRAPHS)}
    saved=client.post(path,data=native,follow_redirects=False)
    assert saved.status_code==303,saved.text[:400]
    (name,rpc),=calls
    assert name=='admit_knowledge_review' and rpc['p_payload']['entries'][0]['matches'][0]['key']==match_name.removeprefix('match_')
    assert '\r' not in rpc['p_notes'][0]['content']
