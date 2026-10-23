"""Owner replacements through the actual correction contract and HTTP callers."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from app import knowledge_source_review as review, knowledge_source_evidence as evidence
from app import knowledge_fidelity, knowledge_review as admission, setup_cards
from tests.test_knowledge_source_review import seeded
from tests.test_knowledge_source_routes import source_client, url

BEFORE = 'Our office is open Monday to Friday, 9 am to 5 pm UK time, excluding public holidays in England.'
REST = 'Routine messages are reviewed on the next working day. Email contact@example.test. Include your property address.'
REQUEST = 'I am the owner confirming a correction: we now close at 4 pm, not 5 pm. Other contact details remain unchanged.'
answer = review.answer


def owner_case(row=None, source=None):
    if row is None: row, source = seeded()
    row['payload']['entries'] = [dict(row['payload']['entries'][0], label='Hours and Contact',
        content=BEFORE+' '+REST, provenance=[{'source':'https://example.test/hours','excerpt':BEFORE}])]
    row['payload']['source_units'] = knowledge_fidelity.inventory([{'url':'https://example.test/hours','text':BEFORE+' '+REST}])
    card = review.draft_card(row, source)
    card['knowledge_owner_request'] = REQUEST
    card['knowledge_source_units'] = row['payload']['source_units']
    line = card['lines'][0]
    raw = {'revision':card['revision'], 'changes':[{'id':line['id'],
        'content_hash':evidence.digest(line['fields']['content']), 'insertions':[], 'citations':[],
        'owner_replacements':[{'before':'5 pm','after':'4 pm'}]}], 'overlaps':[], 'questions':[]}
    return row, source, card, raw


def test_exact_replacement_preserves_unrelated_facts_and_distinct_owner_audit():
    row, source, card, raw = owner_case()
    original = deepcopy(row)
    preview = review.validate(card, source, raw)
    assert preview['changes'][0]['after'] == (BEFORE+' '+REST).replace('5 pm','4 pm')
    assert 'citation changes only' not in preview['summary']
    payload = review.corrected_payload(row, source, raw, card=card, owner_request=REQUEST, reviewer='owner')
    assert row == original and payload['draft_revision']==2
    entry = payload['entries'][0]
    assert ''.join(p['text'] for p in entry['content_segments']) == entry['content']
    corrected = next(p for p in entry['content_segments'] if p['text']=='4 pm')
    assert corrected['kind']=='owner_said' and corrected['reference']==REQUEST
    assert not any(p.get('excerpt')=='4 pm' for p in entry['provenance'])
    assert payload['draft_review_choices']['source_'+row['payload']['source_units'][0]['id']]=='exclude'
    assert payload['draft_review_choices']['action_'+entry['id']]==''
    assert payload['source_review_history'][0]['changes'][0]['source_choices'][0]['action']=='superseded'
    revised = dict(row, payload=payload)
    with pytest.raises(setup_cards.Stale):review.validate(review.draft_card(revised,source), source, raw)


@pytest.mark.parametrize('case',['invented','ambiguous','hypothetical','denied','no_op','effective_no_op','duplicate','overlap','mixed','foreign'])
def test_unverified_replacements_fail_closed(case):
    row, source, card, raw = owner_case()
    change = raw['changes'][0]
    if case=='invented':change['owner_replacements'][0]['after']='3 pm'
    elif case=='ambiguous':card['knowledge_owner_request']='Do we now close at 4 pm?'
    elif case=='hypothetical':card['knowledge_owner_request']='Perhaps update the time to 4 pm.'
    elif case=='denied':card['knowledge_owner_request']='Do not change to 4 pm.'
    elif case=='no_op':change['owner_replacements'][0]['after']='5 pm'
    elif case=='effective_no_op':change['owner_replacements'][0]['after']='5 PM'
    elif case=='duplicate':change['owner_replacements'] *= 2
    elif case=='overlap':change['owner_replacements'].append({'before':'to 5 pm','after':'4 pm'})
    elif case=='mixed':change['removals']=['paragraph:0']
    elif case=='foreign':change['id']='another-note'
    with pytest.raises((setup_cards.Invalid, setup_cards.Stale)):review.validate(card,source,raw)


def test_owner_replacement_preview_apply_route_keeps_final_admission_separate(source_client, monkeypatch):
    client, row, source, ctx, calls, _ = source_client
    row, source, card, raw = owner_case(row, source)
    async def provider(**kwargs):
        sent = json.loads(kwargs['messages'][0]['content'])
        assert sent['owner_request']==REQUEST
        assert 'owner_replacements' in kwargs['output_config']['format']['schema']['properties']['changes']['items']['properties']
        return SimpleNamespace(model='offline',usage={},content=[SimpleNamespace(type='text',text=json.dumps(raw))])
    from app import llm, usage, concierge
    monkeypatch.setattr(review,'answer',answer)
    monkeypatch.setattr(llm,'create',provider)
    monkeypatch.setattr(usage,'record_anthropic',lambda *a,**k:None)
    monkeypatch.setattr(concierge,'client',lambda:SimpleNamespace(with_options=lambda **k:None))
    preview = client.post(url(row),json={'version':'v1','question':REQUEST})
    assert preview.status_code==200, preview.text
    assert calls==[] and row['payload']['entries'][0]['content'].startswith(BEFORE)
    applied = client.post(url(row)+'/apply',json={'version':'v1','review':preview.json()['review'],'owner_request':REQUEST})
    assert applied.status_code==200, applied.text
    assert [name for name,_ in calls]==['revise_knowledge_review_sources']
    payload = calls[0][1]['p_payload']
    assert payload['entries'][0]['content'].startswith(BEFORE.replace('5 pm','4 pm'))
    assert payload['source_review_history'][0]['reviewer']=='owner'
    assert row['state']=='pending' and 'Nothing saved to Knowledge' in applied.json()['summary']
