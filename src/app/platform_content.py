"""Versioned local product articles and an offline-only index refresh contract.

Current runtime help reads this release's bundle. Legacy vector rows cannot
override it. Preparing a refresh never embeds, deletes, activates or writes.
"""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from app import product_inventory, product_guides

FORMAT = 1
SCOPE = 'platform-help'


def digest(value):
    return sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def articles():
    from app.platform_public import resolve_data
    rows = json.loads(Path(__file__).with_name('platform_articles.json').read_text(encoding='utf-8'))
    rows = resolve_data(rows)
    rows.extend({'id':'guide.'+r['id'],'kind':'guide','title':r['title'],
                 'paragraphs':[product_guides.GUIDES[r['id']]],'capability':r['id']}
                for r in product_inventory.entries())
    public_guides = {'packages':'packages-and-credits','progress':'progress-and-prerequisites',
                     'scheduling':'configure-booking-rules','product.help':'how-product-help-works',
                     'people':'configure-people-permissions'}
    titles={r['id']:r['title'] for r in product_inventory.entries()}
    rows.extend({'id':'help.'+slug,'kind':'help','title':titles[identity],
                 'paragraphs':[product_guides.GUIDES[identity]],'slug':slug,
                 'related':[['Capabilities and limitations','/capabilities']]}
                for identity,slug in public_guides.items())
    ids = [r['id'] for r in rows]
    if len(ids)!=len(set(ids)): raise ValueError('Product topic IDs must be unique.')
    if any(not r['paragraphs'] or any(not isinstance(p,str) or not p.strip() for p in r['paragraphs']) for r in rows):
        raise ValueError('Product topics require complete paragraphs.')
    return rows


def seed_sources():
    # Preserve the old named-source API, including its price-free KB contract.
    return {r['title']:'\n\n'.join(r['paragraphs']) for r in articles() if r['kind'] in {'kb','guide'}}


def public_topics():
    return [{'slug':r['slug'],'question':r['title'],'answer':r['paragraphs'],'related':r.get('related',[])}
            for r in articles() if r['kind']=='help']


def bundle(definition_version=None):
    if definition_version is None:
        from app.platform_capabilities import catalogue
        definition_version = catalogue()['definition_version']
    result = {'format':FORMAT,'scope':SCOPE,'definition_version':definition_version,'articles':articles()}
    return {**result,'version':digest(result)}


def manifest(pack):
    raw = {k:v for k,v in pack.items() if k!='version'}
    if pack.get('format')!=FORMAT or pack.get('scope')!=SCOPE or pack.get('version')!=digest(raw):
        raise ValueError('Incomplete or changed product bundle.')
    ids=[a['id'] for a in pack['articles']]
    if len(ids)!=len(set(ids)):raise ValueError('Duplicate product topic IDs.')
    return {'format':FORMAT,'scope':SCOPE,'version':pack['version'],
            'articles':{a['id']:digest(a) for a in pack['articles']}}


def refresh_plan(current=None, desired=None, *, tenant_id):
    from app.db import PLATFORM_TENANT_ID
    if tenant_id!=PLATFORM_TENANT_ID:
        raise ValueError('Product refresh is limited to the reserved platform namespace.')
    desired = bundle() if desired is None else desired
    target = manifest(desired)
    if current is not None and (current.get('scope')!=SCOPE or current.get('format')!=FORMAT
                                or current.get('tenant_id')!=tenant_id or not isinstance(current.get('articles'),dict)):
        raise ValueError('Unmanaged or foreign index requires separate inspection; no cleanup is authorised.')
    old=(current or {}).get('articles',{})
    new=target['articles']
    return {'tenant_id':tenant_id,'scope':SCOPE,'expected_version':(current or {}).get('version'),
        'desired_manifest':{**target,'tenant_id':tenant_id},
        'added':sorted(set(new)-set(old)), 'changed':sorted(k for k in set(new)&set(old) if new[k]!=old[k]),
        'retired':sorted(set(old)-set(new)), 'inspection_required':current is None,
        'publication':'offline-only; no embeddings, database changes or activation performed'}


def verify_complete(pack, receipt):
    """A partial/old receipt never counts as an activated complete version."""
    expected=manifest(pack)
    from app.db import PLATFORM_TENANT_ID
    return receipt.get('tenant_id')==PLATFORM_TENANT_ID and all(
        receipt.get(k)==expected[k] for k in ('format','scope','version','articles'))


def relevant(query, pack, limit=6):
    import re
    words={w[:5] for w in re.findall(r'[a-z]{3,}',str(query)[:4000].lower())}
    # No tenant/user data is a content source. Return whole authored paragraphs.
    ranked=sorted(pack['articles'],key=lambda a:(
        -sum(w in (a['title']+' '+' '.join(a['paragraphs'])).lower() for w in words),a['id']))
    return deepcopy(ranked[:limit])
