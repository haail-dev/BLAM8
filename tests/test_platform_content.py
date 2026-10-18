from copy import deepcopy
import pytest
from app import platform_content as content, platform_help, platform_kb_content, help_center, db, concierge


def test_canonical_articles_feed_compatibility_exports_and_current_lookup():
    pack=content.bundle()
    assert len({a['id'] for a in pack['articles']})==len(pack['articles'])
    assert {'packages-and-credits','progress-and-prerequisites'} <= {t['slug'] for t in help_center.TOPICS}
    assert len([a for a in pack['articles'] if a['kind']=='kb'])==31
    assert platform_kb_content.SOURCES==content.seed_sources()
    assert all('£' not in text for text in platform_kb_content.SOURCES.values())
    answer=platform_help.lookup('packages')
    assert answer['content_version']==pack['version']
    assert all(a['id'] in {p['id'] for p in pack['articles']} for a in answer['articles'])


def test_refresh_reconciles_stable_ids_without_writes_or_foreign_tenant_cleanup():
    old=content.bundle(); current=content.refresh_plan(tenant_id=db.PLATFORM_TENANT_ID,desired=old)['desired_manifest']
    changed=deepcopy(old)
    changed['articles'][0]['title']='Renamed heading, same topic'
    retired=changed['articles'].pop(1)['id']
    changed['articles'].append({'id':'guide.new','kind':'guide','title':'New','paragraphs':['A reviewed article.']})
    changed['version']=content.digest({k:v for k,v in changed.items() if k!='version'})
    plan=content.refresh_plan(current,changed,tenant_id=db.PLATFORM_TENANT_ID)
    assert plan['changed']==[old['articles'][0]['id']]
    assert plan['retired']==[retired] and plan['added']==['guide.new']
    assert current['version']==old['version'] and 'offline-only' in plan['publication']
    with pytest.raises(ValueError,match='reserved'):content.refresh_plan(current,changed,tenant_id='customer')
    with pytest.raises(ValueError,match='foreign'):content.refresh_plan({**current,'tenant_id':'customer'},changed,tenant_id=db.PLATFORM_TENANT_ID)
    with pytest.raises(ValueError,match='Unmanaged'):content.refresh_plan({**current,'scope':'tenant-knowledge'},changed,tenant_id=db.PLATFORM_TENANT_ID)


def test_partial_old_foreign_or_tampered_receipt_never_verifies():
    pack=content.bundle(); receipt=content.refresh_plan(tenant_id=db.PLATFORM_TENANT_ID,desired=pack)['desired_manifest']
    assert content.verify_complete(pack,receipt)
    for bad in [{**receipt,'articles':{}},{**receipt,'version':'old'},{**receipt,'tenant_id':'customer'}]:
        assert not content.verify_complete(pack,bad)
    pack['articles'][0]['paragraphs']=['Unreviewed edit']
    with pytest.raises(ValueError,match='changed'):content.manifest(pack)


@pytest.mark.asyncio
async def test_legacy_seeding_entry_point_only_prepares_and_rejects_extra_sources(monkeypatch):
    async def forbidden(*a,**k):pytest.fail('Preparation cannot embed or ingest')
    monkeypatch.setattr(concierge.kb,'ingest',forbidden)
    plan=await concierge.seed_platform_kb()
    assert plan['inspection_required'] and plan['added']
    with pytest.raises(ValueError,match='stable IDs'):
        await concierge.seed_platform_kb({'Unreviewed':'A new source'})
