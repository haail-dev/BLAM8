from copy import deepcopy
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from app import db, knowledge_review, knowledge_source_routes as routes, knowledge_source_review as review, knowledge_source_evidence, onboarding, knowledge as kb
from app.main import app
from app.auth import TenantCtx, require_tenant
from app.security import require_csrf, require_csrf_header
from tests.test_knowledge_source_review import seeded, corrections


@pytest.fixture
def source_client(monkeypatch):
    from app import security
    # Independent clients/tests must not inherit another test's per-IP budget.
    # The actual limiter remains enabled within each test.
    monkeypatch.setattr(security, '_BUCKETS', {})
    row, evidence = seeded(); context = {'version':'v1','reviews':[row], 'sources':[], 'brief':''}
    calls = []
    ctx = TenantCtx(tenant_id='tenant', user_id='owner', client=None)
    app.dependency_overrides[require_tenant] = lambda: ctx
    app.dependency_overrides[require_csrf_header] = lambda: None
    app.dependency_overrides[require_csrf] = lambda: None
    monkeypatch.setattr(routes.settings, 'knowledge_source_review_enabled', True)
    monkeypatch.setattr(db, 'get_member', lambda *a, **k: None)
    monkeypatch.setattr(routes.people, 'can_do', lambda *a: True)
    monkeypatch.setattr(knowledge_review, 'load', lambda *a: deepcopy(context))
    monkeypatch.setattr(knowledge_source_evidence, 'load', lambda tenant, identity: deepcopy(evidence))
    monkeypatch.setattr(routes.fairuse, 'onboarding_allowed', lambda *a, **k: True)
    def rpc(name, body):
        calls.append((name, body))
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=True))
    monkeypatch.setattr(db, 'db', lambda: SimpleNamespace(rpc=rpc))
    async def answer(tenant, question, card, source):
        return review.validate(card, source, corrections(card, source))
    monkeypatch.setattr(review, 'answer', answer)
    yield TestClient(app), row, evidence, context, calls, ctx
    app.dependency_overrides.clear()


def url(row): return '/onboarding/knowledge/review/'+row['id']+'/corrections'


def test_preview_is_read_only_and_apply_uses_draft_rpc_not_admission(source_client):
    client, row, evidence, context, calls, _ = source_client
    response = client.post(url(row), json={'version':'v1','question':'Retain qualifications.'})
    assert response.status_code == 200 and calls == []
    applied = client.post(url(row)+'/apply', json={'version':'v1','review':response.json()['review']})
    assert applied.status_code == 200 and len(applied.json()['entries']) == 9
    assert [name for name, body in calls] == ['revise_knowledge_review_sources']
    assert row['payload']['draft_revision'] == 1


@pytest.mark.parametrize('case,expected', [('flag',404),('permission',403),('viewing',403),('stale',409),('saved',409),('missing',404)])
def test_gates_reject_before_model_or_mutation(source_client, monkeypatch, case, expected):
    client, row, evidence, context, calls, ctx = source_client
    async def forbidden(*a): pytest.fail('Model must not be called')
    monkeypatch.setattr(review, 'answer', forbidden)
    if case == 'flag': monkeypatch.setattr(routes.settings, 'knowledge_source_review_enabled', False)
    elif case == 'permission': monkeypatch.setattr(routes.people, 'can_do', lambda *a: False)
    elif case == 'viewing': ctx.viewing_as = 'other'
    elif case == 'stale': context['version'] = 'v2'
    elif case == 'saved': row['state'] = 'saved'
    else: context['reviews'] = []
    assert client.post(url(row), json={'version':'v1','question':'Review'}).status_code == expected
    assert calls == []


def test_knowledge_guide_provider_credit_error_is_actionable_and_keeps_local_form(source_client,monkeypatch):
    from app import knowledge_guidance
    client,row,evidence,context,calls,ctx=source_client
    class BadRequestError(Exception): pass
    async def unavailable(*args): raise BadRequestError('Your credit balance is too low to access the API.')
    monkeypatch.setattr(knowledge_guidance,'turn',unavailable)
    response=client.post('/onboarding/knowledge/review/'+row['id']+'/guide',json={
        'form':{'version':'v1','action_'+row['payload']['entries'][0]['id']:'keep'},
        'question':'Leave out that page heading.','focus_hash':'focus'})
    assert response.status_code==503
    assert 'API credit' in response.json()['detail'] and 'manually' in response.json()['detail']
    assert 'credit balance' not in response.json()['detail']
    assert calls==[]


def test_csrf_is_enforced(source_client):
    client, row, *_ = source_client
    app.dependency_overrides.pop(require_csrf_header)
    assert client.post(url(row), json={'version':'v1','question':'Review'}).status_code == 403


@pytest.mark.parametrize('enabled',[True,False])
def test_continue_does_not_stage_unsaved_note_and_requires_explicit_pending_choice(source_client, monkeypatch,enabled):
    client, row, evidence, context, calls, _ = source_client
    monkeypatch.setattr(db, 'list_knowledge_sources', lambda *a, **k: [])
    monkeypatch.setattr(kb, 'is_configured', lambda: True)
    monkeypatch.setattr(routes.settings,'knowledge_source_review_enabled',enabled)
    monkeypatch.setattr(knowledge_review, 'stage', lambda *a, **k: pytest.fail('Continue must not stage text'))
    from fastapi.responses import RedirectResponse
    monkeypatch.setattr(onboarding, '_finish', lambda ctx: RedirectResponse('/finished', status_code=303))
    r = client.post('/onboarding/knowledge/text', data={'then':'continue','content':'Unsaved owner fact','label':'Owner note'}, follow_redirects=False)
    assert r.status_code == 200 and 'Unsaved owner fact' in r.text and 'unsaved' in r.text
    r = client.post('/onboarding/knowledge/text', data={'then':'continue'}, follow_redirects=False)
    assert r.status_code == 303 and 'pending' in r.headers['location']
    r = client.post('/onboarding/knowledge/text', data={'then':'continue','knowledge_draft_decision':'keep'}, follow_redirects=False)
    assert r.headers['location'] == '/finished' and not calls


def test_capture_stages_snapshot_and_drafts_atomically(monkeypatch):
    row, evidence = seeded(); captured = []
    def rpc(name, body):
        captured.append((name, body))
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=body['p_review']))
    monkeypatch.setattr(db, 'db', lambda: SimpleNamespace(rpc=rpc))
    result = knowledge_review.stage('tenant', row['payload']['entries'], evidence_pages=[{k:p[k] for k in ('url','text')} for p in evidence['pages']])
    assert captured[0][0] == 'capture_knowledge_review_sources'
    assert result['payload']['source_snapshot'] == captured[0][1]['p_evidence']['id']
    assert result['state'] == 'pending'


def test_website_import_retains_raw_source_without_returning_it_to_browser(source_client, monkeypatch):
    client, row, evidence, context, calls, _ = source_client
    seen = []
    monkeypatch.setattr(kb, 'is_configured', lambda: True)
    monkeypatch.setattr(db, 'get_tenant', lambda *a, **k: {})
    pages = [{k:p[k] for k in ('url','text')} for p in evidence['pages']]
    async def build(*a, **kw):
        return {'site':'https://example.invalid','pages':['https://example.invalid'],
            'entries':row['payload']['entries'], 'evidence_pages':pages, 'source_units':[]}
    monkeypatch.setattr(onboarding.kb_website, 'build', build)
    monkeypatch.setattr(knowledge_review, 'stage', lambda *a, **kw: seen.append(kw) or row)
    monkeypatch.setattr(onboarding, '_knowledge_review_fragments', lambda *a: {'review_html':'review','coverage_html':'coverage'})
    result = client.post('/onboarding/knowledge/website', data={'url':'https://example.invalid'})
    assert result.status_code == 200 and 'evidence_pages' not in result.json()
    assert seen[0]['evidence_pages'] == pages


@pytest.mark.parametrize('operation', ['save', 'discard'])
@pytest.mark.parametrize('enabled', [True, False])
def test_source_draft_final_write_requires_permission_even_after_flag_disabled(source_client, monkeypatch, operation, enabled):
    client, row, evidence, context, calls, _ = source_client
    monkeypatch.setattr(routes.settings, 'knowledge_source_review_enabled', enabled)
    monkeypatch.setattr(routes.people, 'can_do', lambda *a: False)
    result = client.post('/onboarding/knowledge/review/'+row['id'],data={'version':'v1','operation':operation})
    assert result.status_code == 403 and calls == []
