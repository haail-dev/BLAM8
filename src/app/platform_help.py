"""Local, read-only product lookup. Never embeds, indexes or searches tenant facts."""
import json
import re
from app import platform_capabilities as product, product_guides, platform_content

TOOL = {'name':'get_product_help',
        'description':'Explain platform features and settings using current product sources. For all features set all=true; the complete index always includes restricted/disabled states. This tool never reads business records or executes work.',
        'input_schema':{'type':'object','properties':{'query':{'type':'string'},'all':{'type':'boolean'}}}}


def runtime_context(tenant_id, client, member):
    from app import db
    return product.Context(active=frozenset(db.active_modules(tenant_id,client=client)),
        preview=frozenset(db.preview_modules(tenant_id,client=client)),member=member)


def lookup(query='', context=None, *, all_features=False):
    result = product.catalogue(context)
    pack = platform_content.bundle(result['definition_version'])
    words = {w[:5] for w in re.findall(r'[a-z]{3,}',str(query)[:4000].lower())}
    ranked = sorted(result['capabilities'], key=lambda row:(
        -sum(w in (row['title']+' '+product_guides.GUIDES[row['id']]).lower() for w in words),row['id']))
    complete = all_features or bool(re.search(r'\b(all|every|complete)\b.*\b(feature|capabilit|setting)',str(query),re.I))
    chosen = ranked if complete else ranked[:6]
    return {'definition_version':result['definition_version'],'content_version':pack['version'],
        'articles':platform_content.relevant(query,pack),
        'complete_index':[{'id':r['id'],'title':r['title'],'availability':r['availability'],
                           'classification':r['status']} for r in result['capabilities']],
        'guides':[{'id':r['id'],'title':r['title'],'availability':r['availability'],
                  'instructions':product_guides.GUIDES[r['id']], 'limits':r['limits'],
                  'plans':r['plans']} for r in chosen],
        'setup':result['setup'] if complete else [],
        'reads':result['reads'], 'actions':result['actions'],
        'can_propose_settings':result['can_propose_settings'],
        'execution':'Read-only discovery. No settings, records or sends were changed.'}


def prompt(query='', context=None):
    return json.dumps(lookup(query,context),ensure_ascii=False,sort_keys=True)
