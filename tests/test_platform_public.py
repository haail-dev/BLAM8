from copy import deepcopy
import json
from pathlib import Path
import re
from fastapi.testclient import TestClient
from app import plans, platform_public, platform_content, platform_facts
from app.main import app


def test_changed_catalogue_prices_reach_fact_sheet_articles_and_public_pages(monkeypatch):
    catalogue=deepcopy(plans.CATALOG)
    for p in catalogue:
        if p.get('public'):p['price']={'email':'£47','starter':'£107','concierge':'£259'}[p['key']]
    monkeypatch.setattr(plans,'CATALOG',catalogue)
    monkeypatch.setattr(plans,'EXTRA_MAILBOX',{**plans.EXTRA_MAILBOX,'price':'£58'})
    # No lifespan/startup; the existing suite stubs database and provider clients.
    client=TestClient(app)
    for path in ['/','/capabilities','/help/how-much-does-blam8-cost','/vs/tradify','/llms.txt']:
        response=client.get(path)
        assert response.status_code==200,response.text
        for price in ('£47','£107','£259'): assert price in response.text,(path,price)
        assert '£249' not in response.text
    assert '£58' in client.get('/help/how-much-does-blam8-cost').text
    landing=client.get('/').text
    assert '"lowPrice": "47"' in landing and '"highPrice": "259"' in landing
    assert '£21.40' in client.get('/partners').text  # Canonical Pro price × current commission.
    assert '£259' in platform_facts.sheet() and '£249' not in platform_facts.sheet()
    assert '£107' in str(platform_content.public_topics())


def test_new_help_visible_text_and_json_ld_share_the_same_authored_paragraphs():
    client=TestClient(app)
    for slug in ('packages-and-credits','progress-and-prerequisites','configure-booking-rules'):
        html=client.get('/help/'+slug).text
        blocks=re.findall(r'<script type="application/ld\+json">(.*?)</script>',html,re.S)
        data=json.loads(blocks[0])
        graph=data.get('@graph',[data])
        faq=next(n for n in graph if n.get('@type')=='FAQPage')
        text=faq['mainEntity'][0]['acceptedAnswer']['text']
        # Shared guide paragraphs contain no markup/escaped quotes in these topics.
        assert text in html
    caps=client.get('/capabilities').text
    assert 'data-product-capability="knowledge.review"' in caps
    assert 'Knowledge admission review' in caps and 'restricted' in caps


def test_maintained_public_templates_do_not_copy_plan_amounts():
    root=Path(__file__).resolve().parents[1]
    paths = list((root/'src/app/templates').glob('*.html'))
    assert paths, 'Public template inventory must not be empty'
    for path in paths:
        assert not re.search(r'(?:£|&pound;)(39|99|249)(?![\d.])',path.read_text(encoding='utf-8')),path.name
    assert '£' not in platform_facts.PRODUCT_OVERVIEW
    assert '2026-06' not in (root/'README.md').read_text(encoding='utf-8').split('## Architecture')[0]


def test_public_values_are_resolved_from_each_named_owner(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings,'trial_days',19)
    assert platform_public.resolve('{{product:trial.days}}')=='19'
    assert '19-day' in plans.pricing_blurb()
    assert platform_public.value('price.email')==next(p['price'] for p in plans.CATALOG if p['key']=='email')
