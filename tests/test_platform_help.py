import json
from types import SimpleNamespace
import pytest
from app import platform_help, platform_capabilities as product, product_inventory, concierge, concierge_data, db


@pytest.mark.parametrize('question', [
    'Can it draft and email a quote using my approved prices, and does approving that email also mean the customer has authorised the work?',
    'Can it handle replies, booking requests, quotes and inventory for my business?',
    'And what happens when that expires?',
])
def test_single_pass_context_cannot_drop_core_guides_on_multi_part_or_followup_questions(question):
    result=json.loads(platform_help.prompt(question))
    assert {g['id'] for g in result['guides']}=={r['id'] for r in product_inventory.entries()}
    assert all(g['availability']=='context_required' for g in result['guides'])
    assert result['setup']==[] and result['reads']==[] and result['actions']==[]
    assert not result['can_propose_settings']
    # Keep this a compact guide catalogue, not an unbounded documentation dump.
    assert sum(len(g['instructions']) for g in result['guides']) < 14000
    assert len(result['articles'])<=6


def test_signed_in_single_pass_coverage_preserves_permissions():
    context=product.Context(active=frozenset(),member={'role':'staff','pages':[],'actions':{}})
    result=json.loads(platform_help.prompt('What can I do?',context))
    assert {g['id']:g['availability'] for g in result['guides']} == {
        r['id']:r['availability'] for r in product.catalogue(context)['capabilities']}
    assert result['actions']==[]


@pytest.mark.asyncio
@pytest.mark.parametrize('surface',['public','scenario','help'])
async def test_real_single_pass_callers_send_quote_and_retention_evidence(monkeypatch,surface):
    from app import llm
    captured=[]
    monkeypatch.setattr(concierge,'client',lambda:None)
    async def fake(**kw):
        captured.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type='text',text=json.dumps({
            'reply':'Example reply.','offer_signup':False,'suggested_industry':'','on_topic':True,
            'nav':'','submit':None,'recommend':None}))],model='offline',usage=None)
    monkeypatch.setattr(llm,'create',fake)
    question='What about accepting it, or when the evidence expires?'
    if surface=='public':await concierge.answer(question)
    elif surface=='scenario':await concierge.scenario_answer(question)
    else:await concierge.answer_help(question)
    prompt=str(captured[0]['system'])
    assert 'record acceptance received by phone or email' in prompt
    assert 'does not delete approved Knowledge' in prompt
    assert 'remain manually reviewable' in prompt
    assert 'KNOWLEDGE_SOURCE_REVIEW_ENABLED' not in prompt
    if surface=='public':
        assert captured[0]['log_mode']=='meta'
        assert 'tools' not in captured[0]  # Product evidence adds no public account access.


def test_all_features_enumerates_every_family_and_actual_setup_definitions():
    result=platform_help.lookup('Show every feature',all_features=True)
    assert {r['id'] for r in result['complete_index']}=={r['id'] for r in product_inventory.entries()}
    assert len(result['guides'])==len(result['complete_index'])
    assert result['setup'] and result['actions']==[]
    assert 'quantity' in next(g['instructions'] for g in result['guides'] if g['id']=='packages')
    assert 'prerequisites' in next(g['instructions'] for g in result['guides'] if g['id']=='progress')
    assert 'Read-only' in result['execution']


@pytest.mark.asyncio
async def test_product_tool_uses_only_authenticated_scope_and_never_searches_tenant_knowledge(monkeypatch):
    client=object(); calls=[]
    def active(tenant,**kw):
        assert tenant=='mine' and kw['client'] is client
        calls.append(tenant);return {'concierge','crm'}
    monkeypatch.setattr(db,'active_modules',active)
    monkeypatch.setattr(db,'preview_modules',lambda tenant,**kw:set())
    async def forbidden(*a,**k):pytest.fail('Product help must not use tenant knowledge or embeddings')
    monkeypatch.setattr(concierge_data.kb,'retrieve',forbidden)
    result=await concierge_data.execute('mine',client,'get_product_help',
        {'query':'packages','tenant_id':'someone-else','all':True},full_qa=True)
    assert len(result['complete_index'])==20 and calls==['mine']
    assert next(r for r in result['complete_index'] if r['id']=='knowledge.review')['availability']=='restricted'
    assert not any('someone-else' in str(r) for r in result['guides'])


@pytest.mark.asyncio
async def test_help_uses_local_sources_without_embeddings_or_industry_recipes(monkeypatch):
    from app import llm, settings_guide
    captured=[]
    async def forbidden(*a,**k):pytest.fail('No external knowledge lookup')
    monkeypatch.setattr(concierge.kb,'retrieve',forbidden)
    monkeypatch.setattr(settings_guide,'guide_for',lambda *a,**k:pytest.fail('No industry recipes'))
    monkeypatch.setattr(concierge.platform_facts,'sheet',lambda:'Approved product facts')
    monkeypatch.setattr(concierge,'client',lambda:None)
    async def fake(**kw):
        captured.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type='text',text=json.dumps({'reply':'Open Packages to review the balance.','nav':'','submit':None,'recommend':None}))],model='offline',usage=None)
    monkeypatch.setattr(llm,'create',fake)
    for industry in ['appointments','equipment rental']:
        result=await concierge.answer_help('Explain packages',tenant={'industry':industry})
        assert 'Packages' in result.reply
    # Tenant vocabulary is context; the stable rules/catalogue are invariant.
    assert captured[0]['system'][0]==captured[1]['system'][0]
    assert 'packages' in str(captured[0]['system'])
    assert 'No durable proposal context' in str(captured[0]['system'])
    assert '<settings_guide>' not in str(captured[0]['system'])


@pytest.mark.parametrize('voice',[False,True])
def test_typed_and_voice_share_product_discovery_and_confirmation_boundary(voice):
    prompt=concierge.build_business_prompt(voice_mode=voice)
    assert 'get_product_help' in prompt and 'complete_index' in prompt
    assert 'Discovery never authorises a write' in prompt
    assert 'get_product_help' in concierge_data.TOOL_NAMES
