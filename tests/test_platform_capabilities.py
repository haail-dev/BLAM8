from copy import deepcopy
from app import platform_capabilities as product, concierge_data, concierge_actions


def states(context=None):
    return {r['id']:r for r in product.catalogue(context)['capabilities']}


def test_unknown_public_preview_and_disabled_are_not_execution_authority():
    assert states()['crm']['availability']=='context_required'
    preview=product.Context(preview=frozenset({'crm','concierge'}))
    assert states(preview)['crm']['availability']=='preview_only'
    assert states(preview)['knowledge.review']['availability']=='restricted'
    assert states(preview)['outreach']['availability']=='restricted'
    assert product.catalogue(preview)['actions']==[]
    assert not any(r['can_execute'] for r in states(preview).values())
    assert product.availability({'id':'knowledge.review','status':'disabled'},preview)=='disabled'


def test_catalogue_reuses_schemas_tools_and_action_permissions():
    ctx=product.Context(active=frozenset({'crm','scheduling','concierge'}))
    result=product.catalogue(ctx)
    assert {r['id'] for r in result['reads']}=={t['name'] for t in concierge_data.tools_for(True)}
    assert {r['id'] for r in result['actions']}==set(concierge_actions.allowed_kinds(True,True))
    assert all(a['confirmation_required'] and not a['execute_here'] for a in result['actions'])
    assert all({'controls','where_on_page','card_can_set'} <= set(r) for r in result['setup'])
    altered=deepcopy(result);altered['setup'][0]['controls'].clear()
    assert product.catalogue(ctx)==result


def test_closed_doors_and_inactive_member_never_gain_discovery_access():
    staff={'role':'staff','active':True,'scopes':{'pages':['today'],'can':[]}}
    ctx=product.Context(active=frozenset({'crm','scheduling','concierge'}),member=staff)
    result=product.catalogue(ctx)
    assert states(ctx)['money']['availability']=='permission_required'
    assert not result['can_propose_settings']
    assert not any(r['id']=='list_invoices' for r in result['reads'])
    assert result['actions']==[]
    inactive=product.Context(active=ctx.active,member={'role':'owner','active':False})
    assert states(inactive)['crm']['availability']=='permission_required'


def test_discovery_has_no_business_type_input_and_uses_public_plan_entitlements():
    result=product.catalogue(product.Context())
    assert states()['crm']['plans']==['starter','concierge']
    assert states()['owner.assistant']['plans']==['concierge']
    assert len(result['definition_version'])==64
    assert set(product.Context.__dataclass_fields__)=={'active','preview','member'}
