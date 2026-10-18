"""Investigation evidence, isolation and real help-loop regression contracts."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from anthropic.types import TextBlock, ToolUseBlock

from app import concierge, db, help_investigation as investigation, knowledge_review, knowledge_source_evidence, setup_cards
from app.auth import TenantCtx


@pytest.fixture
def owner(monkeypatch):
    monkeypatch.setattr(db, 'get_member', lambda tenant, user: None)
    monkeypatch.setattr(db, 'active_modules', lambda *a, **k: {'concierge', 'crm'})
    monkeypatch.setattr(db, 'preview_modules', lambda *a, **k: set())
    return investigation.Investigator(TenantCtx('mine', object(), user_id='owner'))


def check(owner, capability='knowledge.review'):
    return owner.execute('check_help_account', {'capability_id': capability})


@pytest.mark.parametrize('name,args', [
    ('read_help_code', {'evidence_id': '../../.env'}),
    ('read_help_document', {'document_id': 'https://attacker.invalid'}),
    ('check_help_account', {'capability_id': 'money', 'tenant_id': 'other'}),
    ('check_help_account', {'capability_id': 'money', 'user_id': 'owner'}),
    ('read_help_code', {'evidence_id': ['money.accept_online']}),
    ('check_help_account', {'capability_id': 'db.sql'}),
    ('run_sql', {'sql': 'select secrets'}),
])
def test_tool_inputs_cannot_expand_authority(owner, monkeypatch, name, args):
    monkeypatch.setattr(db, 'db', lambda: pytest.fail('No database access for invalid input'))
    assert owner.execute(name, args)['status'] in {'invalid_request', 'not_in_index'}


def test_all_reviewed_code_symbols_exist_and_only_selected_symbols_are_returned(owner, monkeypatch):
    monkeypatch.setenv('RENDER_GIT_COMMIT', 'a' * 40)
    for identity, (filename, symbols) in investigation.EVIDENCE.items():
        result = owner.execute('read_help_code', {'evidence_id': identity})
        assert result['status'] == 'ok', (identity, result)
        assert [r['symbol'] for r in result['symbols']] == list(symbols)
        assert result['release'] == 'a' * 40 and len(result['sha256']) == 64
        assert all(r['line'] > 0 and r['code'] for r in result['symbols'])
    monkeypatch.setenv('RENDER_GIT_COMMIT', 'secret or invalid metadata')
    assert investigation.revision() == 'unknown'


def test_complete_documents_come_from_current_bundle(owner):
    result = owner.execute('read_help_document', {'document_id': 'guide.knowledge.review'})
    assert result['status'] == 'ok' and len(result['content_version']) == 64
    assert '30 days' in result['document']['paragraphs'][0]
    assert result['checked_at']


@pytest.mark.parametrize('identity', ['missing', 'failed', 'inactive', 'staff', 'view_as'])
def test_private_checks_fail_closed_before_fetching_account_data(monkeypatch, identity):
    def member(*a):
        if identity == 'failed':
            raise RuntimeError('secret in provider error')
        if identity == 'inactive':
            return {'role': 'owner', 'active': False}
        return {'role': 'staff', 'scopes': {}} if identity == 'staff' else None
    monkeypatch.setattr(db, 'get_member', member)
    monkeypatch.setattr(db, 'active_modules', lambda *a, **k: pytest.fail('Read before authorization'))
    monkeypatch.setattr(knowledge_review, 'load', lambda *a: pytest.fail('Private read before authorization'))
    inv = investigation.Investigator(TenantCtx('mine', None, user_id=None if identity == 'missing' else 'user',
                                              viewing_as='real-owner' if identity == 'view_as' else None))
    result = check(inv)
    assert result['status'] in {'unknown', 'permission_required'}
    assert not inv.can_configure and 'secret' not in json.dumps(result)


def test_knowledge_checks_bound_tenant_and_report_states_without_content(owner, monkeypatch):
    monkeypatch.setattr(investigation.settings, 'knowledge_source_review_enabled', True)
    calls = []
    def load(tenant):
        assert tenant == 'mine'
        return {'version': 'v1', 'sources': [{'text': 'PRIVATE saved words'}], 'reviews': [
            {'state': 'pending', 'payload': {'source_snapshot': identity, 'entries': ['PRIVATE draft']}}
            for identity in [None, 'fresh', 'expired', 'broken', 'fresh', 'fresh']]}
    def evidence(tenant, identity):
        calls.append((tenant, identity))
        if identity == 'expired': raise setup_cards.Invalid('expired PRIVATE')
        if identity == 'broken': raise RuntimeError('PRIVATE database credentials')
        return {'pages': [{'text': 'PRIVATE source text'}]}
    monkeypatch.setattr(knowledge_review, 'load', load)
    monkeypatch.setattr(knowledge_source_evidence, 'load', evidence)
    result = check(owner)
    assert result['source_corrections_enabled'] is True
    assert result['review_storage'] == 'read_verified' and result['pending_reviews'] == 6
    assert result['remaining_reviews'] == 1 and result['write_migration_readiness'] == 'not_checked'
    assert [r['source_evidence'] for r in result['inspected_reviews']] == ['missing', 'valid', 'missing_expired_or_invalid', 'unknown', 'valid']
    assert all(t == 'mine' for t, _ in calls) and 'PRIVATE' not in json.dumps(result)


def test_empty_reviews_and_disabled_flag_do_not_mean_unsupported(owner, monkeypatch):
    monkeypatch.setattr(investigation.settings, 'knowledge_source_review_enabled', False)
    monkeypatch.setattr(knowledge_review, 'load', lambda t: {'sources': [], 'version': 'v', 'reviews': []})
    result = check(owner)
    assert result['status'] == 'checked' and result['source_corrections_enabled'] is False
    assert result['pending_reviews'] == 0 and result['inspected_reviews'] == []
    monkeypatch.setattr(knowledge_review, 'load', lambda t: (_ for _ in ()).throw(RuntimeError('private')))
    failed = check(owner)
    assert failed['status'] == 'partially_checked' and failed['review_storage'] == 'unknown'
    assert failed['source_corrections_enabled'] is False and failed['source_corrections_scope'] == 'service'


def test_live_state_not_cached_between_turns(owner, monkeypatch):
    monkeypatch.setattr(db, 'active_modules', lambda *a, **k: set())
    assert check(owner, 'crm')['availability'] == 'module_required'
    monkeypatch.setattr(db, 'active_modules', lambda *a, **k: {'crm'})
    assert check(owner, 'crm')['availability'] == 'available_subject_to_configuration'


def test_mailbox_check_does_not_leak_addresses_or_claim_connection_health(owner, monkeypatch):
    def boxes(t, **kw):
        assert t == 'mine' and kw['client'] is owner.ctx.client
        return [{'address': 'private@example.test', 'token': 'PRIVATE'}]
    monkeypatch.setattr(db, 'list_mailboxes', boxes)
    result = check(owner, 'mailboxes')
    assert result['configured_mailbox_count'] == 1 and result['provider_health'] == 'not_checked'
    assert 'PRIVATE' not in json.dumps(result) and 'private@example' not in json.dumps(result)


def message(*blocks):
    return SimpleNamespace(content=list(blocks), model='test', usage=SimpleNamespace(input_tokens=10, output_tokens=5))


def final():
    # Deliberately propose a side effect: investigation must discard it.
    return message(TextBlock(type='text', text=json.dumps({
        'reply': 'I checked the source evidence; nothing was changed.', 'nav': '',
        'no_dependants': True, 'held': [], 'setup_changes': [{'page': 'services'}],
        'recommend': [], 'submit': None})))


def call(name, key, identity, n=1):
    return ToolUseBlock(type='tool_use', id=f'tool-{n}', name=name, input={key: identity})


@pytest.mark.asyncio
async def test_help_investigates_documents_code_account_then_answers_and_meters_every_call(owner, monkeypatch):
    monkeypatch.setattr(concierge, 'client', lambda: None)
    monkeypatch.setattr(knowledge_review, 'load', lambda t: {'sources': [], 'version': 'v', 'reviews': []})
    replies = iter([
        message(call('read_help_document', 'document_id', 'guide.knowledge.review')),
        message(call('read_help_code', 'evidence_id', 'knowledge.correction_gates', 2)),
        message(call('check_help_account', 'capability_id', 'knowledge.review', 3)), final()])
    captured = []
    async def create(**kwargs):
        captured.append(deepcopy(kwargs))
        return next(replies)
    monkeypatch.setattr(concierge.llm, 'create', create)
    result = await concierge.answer_help('Why can I not correct my Knowledge?', tenant_id='mine', investigator=owner)
    assert len(captured) == 4 and captured[-1]['tool_choice'] == {'type': 'none'}
    assert 'source_corrections_enabled' in str(captured[-1]['messages'])
    assert result.usage['input_tokens'] == 40 and len(result.usage.attempts) == 4
    assert len(result.evidence) == 3 and all(r['status'] in {'ok', 'checked'} for r in result.evidence)
    assert result.no_dependants is False and result.held == [] and result.setup_changes is None and result.recommend is None
    assert all(set(r) == {'source_id', 'status', 'release', 'checked_at', 'tool'} for r in result.evidence)


@pytest.mark.asyncio
async def test_excessive_tool_calls_are_not_executed(owner, monkeypatch):
    monkeypatch.setattr(concierge, 'client', lambda: None)
    executed = []
    monkeypatch.setattr(owner, 'execute', lambda *a: executed.append(a) or {'status': 'ok'})
    replies = iter([message(*[call('read_help_code', 'evidence_id', 'people.permissions', n) for n in range(9)]), final()])
    async def create(**kw): return next(replies)
    monkeypatch.setattr(concierge.llm, 'create', create)
    await concierge.answer_help('Check everything', investigator=owner)
    assert len(executed) == investigation.MAX_TOOL_CALLS


@pytest.mark.asyncio
async def test_tool_failure_is_unknown_and_final_answer_still_runs(owner, monkeypatch):
    monkeypatch.setattr(concierge, 'client', lambda: None)
    monkeypatch.setattr(owner, 'execute', lambda *a: (_ for _ in ()).throw(RuntimeError('SECRET')))
    captured = []
    replies = iter([message(call('check_help_account', 'capability_id', 'money')), final()])
    async def create(**kw):
        captured.append(deepcopy(kw)); return next(replies)
    monkeypatch.setattr(concierge.llm, 'create', create)
    result = await concierge.answer_help('Check money', investigator=owner)
    assert result.evidence[0]['status'] == 'unknown'
    assert 'SECRET' not in str(captured) and 'unknown' in str(captured[-1]['messages'])
    assert 'cannot confirm availability' in result.reply


@pytest.mark.asyncio
async def test_failed_storage_cannot_be_presented_as_ready(owner, monkeypatch):
    monkeypatch.setattr(concierge, 'client', lambda: None)
    monkeypatch.setattr(investigation.settings, 'knowledge_source_review_enabled', True)
    monkeypatch.setattr(knowledge_review, 'load', lambda t: (_ for _ in ()).throw(RuntimeError('SECRET')))
    replies = iter([message(call('check_help_account', 'capability_id', 'knowledge.review')),
                    message(TextBlock(type='text', text=json.dumps({'reply': 'Everything is ready!'})))])
    async def create(**kw): return next(replies)
    monkeypatch.setattr(concierge.llm, 'create', create)
    result = await concierge.answer_help('Does this work on my account?', investigator=owner)
    assert 'Everything is ready' not in result.reply
    assert 'enabled at the service level' in result.reply and 'storage could not be verified' in result.reply


@pytest.mark.parametrize('enabled', [True, False])
def test_knowledge_blocker_explanation_never_guesses_expiry_or_note_contents(owner, monkeypatch, enabled):
    monkeypatch.setattr(investigation.settings, 'knowledge_source_review_enabled', enabled)
    monkeypatch.setattr(knowledge_review, 'load', lambda t: {'sources': [], 'reviews': [
        {'state': 'pending', 'payload': {'source_snapshot': 'test'}}]})
    monkeypatch.setattr(knowledge_source_evidence, 'load', lambda *a: (_ for _ in ()).throw(setup_cards.Invalid('unavailable')))
    owner.record('check_help_account', check(owner))
    text = owner.diagnostic_reply()
    assert 'cannot distinguish those causes' in text and 'have not checked the contents' in text
    assert '1 of 1 pending review batches' in text and 'not tested' in text
    assert ('only the service team' in text) is (not enabled)


@pytest.mark.asyncio
async def test_loop_limits_rounds_even_below_total_call_cap(owner, monkeypatch):
    monkeypatch.setattr(concierge, 'client', lambda: None)
    captured = []
    async def create(**kw):
        captured.append(kw['tool_choice'])
        return final() if kw['tool_choice']['type'] == 'none' else message(call('read_help_code', 'evidence_id', 'people.permissions', len(captured)))
    monkeypatch.setattr(concierge.llm, 'create', create)
    result = await concierge.answer_help('Keep investigating', investigator=owner)
    assert len(captured) == 4 and len(result.evidence) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['provider', 'incomplete_json'])
async def test_failed_completion_preserves_receipts_and_never_shows_partial_answer(owner, monkeypatch, failure):
    monkeypatch.setattr(concierge, 'client', lambda: None)
    calls = []
    async def create(**kw):
        calls.append(kw)
        if len(calls) == 1: return message(call('read_help_code', 'evidence_id', 'people.permissions'))
        if failure == 'provider': raise RuntimeError('SECRET')
        return message(TextBlock(type='text', text='{"reply":"Partial unfinished advice'))
    monkeypatch.setattr(concierge.llm, 'create', create)
    result = await concierge.answer_help('Investigate', investigator=owner)
    assert 'Please try again' in result.reply and 'Partial unfinished' not in result.reply
    assert len(result.evidence) == 1 and result.usage['input_tokens'] >= 10


@pytest.mark.parametrize('role', ['owner', 'staff', 'failed', 'view_as'])
def test_real_route_binds_identity_and_gates_private_context_and_side_effects(monkeypatch, role):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.auth import require_tenant
    from app import concierge_routes as routes, security, setup_held
    monkeypatch.setattr(security, '_BUCKETS', {})
    ctx = TenantCtx('mine', None, user_id='user', viewing_as='real-owner' if role == 'view_as' else None)
    app.dependency_overrides[require_tenant] = lambda: ctx
    def member(*a):
        if role == 'failed': raise RuntimeError('no access')
        return {'role': 'staff', 'scopes': {}} if role == 'staff' else None
    monkeypatch.setattr(db, 'get_member', member)
    reads, writes, investigators = [], [], []
    monkeypatch.setattr(routes, '_tenant_context', lambda c: reads.append(c.tenant_id) or {'brief': 'PRIVATE'})
    monkeypatch.setattr(db, 'active_modules', lambda *a, **k: set())
    monkeypatch.setattr(db, 'preview_modules', lambda *a, **k: set())
    monkeypatch.setattr(routes.usage, 'record_anthropic', lambda *a, **k: None)
    monkeypatch.setattr(db, 'mark_dependants_configured', lambda *a, **k: writes.append('stamp'))
    monkeypatch.setattr(setup_held, 'add', lambda *a, **k: writes.append('held') or [])
    def steps(*a):
        assert role == 'owner', 'Restricted help must not read the setup walkthrough for a link'
        return []
    monkeypatch.setattr(routes.setup_handoff, 'load_steps', steps)
    async def answer(*a, investigator=None, tenant=None, **kw):
        investigators.append(investigator)
        if role != 'owner': assert 'PRIVATE' not in str(tenant)
        return concierge.HelpReply('Checked', {'href': '/app/', 'label': 'Today'}, None, None, None, 'test', no_dependants=True)
    monkeypatch.setattr(concierge, 'answer_help', answer)
    client = TestClient(app)
    try:
        response = client.post('/concierge/help', json={'question': 'Check my account'})
        assert response.status_code == 200 and investigators[0].ctx is ctx
        assert reads == (['mine'] if role == 'owner' else [])
        assert writes == (['stamp', 'held'] if role == 'owner' else [])
        if role != 'owner':
            assert client.post('/concierge/help', json={'question': 'Change settings', 'card_mode': True}).status_code == 403
    finally:
        client.close()
        app.dependency_overrides.pop(require_tenant, None)
