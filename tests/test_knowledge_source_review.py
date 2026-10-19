"""Synthetic regression contracts adapted from the eight retained-source commits."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import uuid4
import pytest
from app import knowledge_source_review as review, knowledge_source_evidence as evidence, setup_cards, knowledge_review as admission


def fixture():
    return json.loads((Path(__file__).parent/'fixtures/knowledge_review/nine_cards.json').read_text(encoding='utf-8'))


def seeded():
    data = fixture()
    saved = evidence.build(data['pages'])
    row = {'id': str(uuid4()), 'state': 'pending', 'payload': {
        'origin': 'Website: https://example.invalid', 'source_snapshot': saved['id'],
        'draft_revision': 1, 'entries': [dict(e, id=str(uuid4()), mailbox_id=None,
            provenance=[{'source': 'https://example.invalid'}]) for e in data['entries']],
        'source_units': [], 'questions': []}}
    return row, saved


def corrections(card, saved):
    raw = {'revision': card['revision'], 'changes': [], 'overlaps': [], 'questions': []}
    for line in card['lines']:
        additions = [{'position': 'before', 'reference': evidence.reference(saved, 0, 'Fictional test profile only.')}]
        if line['fields']['label'] == 'Maintenance Reports':
            additions += [{'position': 'after', 'reference': evidence.reference(saved, 5, text)} for text in (
                'We do not make a legal determination from an email.',
                'Urgent safety guidance must not wait for an approval request.')]
        raw['changes'].append({'id': line['id'], 'content_hash': evidence.digest(line['fields']['content']), 'insertions': additions})
    raw['overlaps'] = [[l['id'] for l in card['lines'] if l['fields']['label'] in ('Prices and Fees', 'Payment Terms')]]
    return raw


def test_review_preserves_all_nine_ids_titles_order_and_original_facts_without_saving():
    row, saved = seeded(); before = deepcopy(row)
    card = review.draft_card(row, saved); raw = corrections(card, saved)
    result = review.validate(card, saved, raw)
    assert row == before and len(result['changes']) == 9
    payload = review.corrected_payload(row, saved, raw)
    assert row == before and payload['draft_revision'] == 2
    for old, new in zip(row['payload']['entries'], payload['entries']):
        assert old['id'] == new['id'] and old['label'] == new['label']
        assert old['content'] in new['content']
        assert 'Fictional test profile only.' in new['content']
    assert payload['source_units'] == row['payload']['source_units']


@pytest.mark.parametrize('tamper', ['revision','hash','unknown_id','extra_fact','foreign_evidence','merge','duplicate','questions','fragment','prose'])
def test_invented_stale_or_unstructured_corrections_are_rejected(tamper):
    row, saved = seeded(); card = review.draft_card(row, saved); raw = corrections(card, saved)
    if tamper == 'revision': raw['revision'] += 1
    elif tamper == 'hash': raw['changes'][0]['content_hash'] = 'bad'
    elif tamper == 'unknown_id': raw['changes'][0]['id'] = 'missing'
    elif tamper == 'extra_fact': raw['changes'][0]['content'] = '10% of rent'
    elif tamper == 'foreign_evidence': raw['changes'][0]['insertions'][0]['reference'] = 'foreign/0/0/1'
    elif tamper == 'merge': raw['changes'][0]['op'] = 'merge'
    elif tamper == 'duplicate': raw['changes'].append(deepcopy(raw['changes'][0]))
    elif tamper == 'questions': raw['questions'] = ['Missing?']*4
    elif tamper == 'fragment': raw['changes'][0]['insertions'][0]['reference'] = evidence.reference(saved, 0, 'Fictional')
    else: raw = {'reply': 'I fixed it and changed your fee to 10%.', 'notes': []}
    with pytest.raises(setup_cards.Invalid): review.validate(card, saved, raw)


def test_mixed_owner_and_source_authorship_survives_repeated_corrections_and_admission_edits():
    row, saved = seeded(); identity = row['payload']['entries'][0]['id']
    edits = {identity: {'content': 'Owner-supplied terms remain authoritative.'}}
    card = review.draft_card(row, saved, edits)
    payload = review.corrected_payload(row, saved, corrections(card, saved), edits)
    entry = payload['entries'][0]
    assert [s['text'] for s in entry['content_segments'] if s['kind'] == 'owner_said'] == [edits[identity]['content']]
    next_row = {**row, 'payload': payload}; next_card = review.draft_card(next_row, saved)
    raw = {'revision': 2, 'changes': [{'id': identity, 'content_hash': evidence.digest(entry['content']),
        'insertions': [{'position': 'after', 'reference': evidence.reference(saved, 0, 'We do not promise attendance or completion dates.')}]}], 'overlaps': [], 'questions': []}
    again = review.corrected_payload(next_row, saved, raw)
    assert again['entries'][0]['content_segments'][:2] == entry['content_segments']
    changed = admission.edited_entries(next_row, {'content_'+identity: 'Owner replaced this text.'})[0]
    assert changed['content_segments'] == [{'kind':'owner_said','reference':'Owner edited this Knowledge draft','text':'Owner replaced this text.'}]
    tampered = deepcopy(next_row); tampered['payload']['entries'][0]['content'] += ' Untracked.'
    with pytest.raises(setup_cards.Invalid, match='provenance'):
        review.content_provenance(review.draft_card(tampered, saved))


def test_source_reference_identity_hash_expiry_and_bounds():
    row, saved = seeded(); ref = evidence.reference(saved, 0, 'Fictional test profile only.')
    assert evidence.resolve(saved, ref)['quote'] == 'Fictional test profile only.'
    for changed in [dict(saved, expires_at=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat()),
                    dict(saved, id=str(uuid4()))]:
        with pytest.raises(setup_cards.Invalid): evidence.resolve(changed, ref)
    changed = deepcopy(saved); changed['pages'][0]['text'] += ' Forged.'
    with pytest.raises(setup_cards.Invalid): evidence.resolve(changed, ref)
    with pytest.raises(setup_cards.Invalid): evidence.build([{'url':'https://example.invalid','text':'x'*12001}])


def test_existing_passage_is_a_noop_without_blocking_an_independent_source_addition():
    row,saved=seeded(); card=review.draft_card(row,saved); line=card['lines'][0]
    existing='We manage properties and coordinate maintenance reports.'
    existing_ref=evidence.reference(saved,0,existing)
    new_quote='We do not make a legal determination from an email.'
    page=next(i for i,p in enumerate(saved['pages']) if new_quote in p['text'])
    new_ref=evidence.reference(saved,page,new_quote)
    raw={'revision':card['revision'],'changes':[{'id':line['id'],'content_hash':evidence.digest(line['fields']['content']),
        'insertions':[{'position':'after','reference':existing_ref},
                      {'position':'after','reference':new_ref}]}], 'overlaps':[],'questions':[]}
    checked=review.validate(card,saved,raw)
    assert len(checked['changes'])==1
    assert [p['quote'] for p in checked['changes'][0]['passages']]==[new_quote]
    assert 'unchanged or already-recorded item' in checked['summary']


def test_business_type_does_not_change_source_contract():
    row, saved = seeded(); card = review.draft_card(row, saved)
    for kind in ['property management', 'music lessons', 'repairs']:
        assert review.context(dict(card, industry=kind), saved) == review.context(card, saved)


def test_real_model_boundary_uses_retained_passages_and_rejects_prose(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from app import concierge, llm, usage
    row, saved = seeded(); card = review.draft_card(row, saved); captured = []
    monkeypatch.setattr(concierge, 'client', lambda: SimpleNamespace(with_options=lambda **kw: None))
    monkeypatch.setattr(usage, 'record_anthropic', lambda *a, **kw: None)
    async def create(**kw):
        captured.append(kw)
        return SimpleNamespace(model='fake', usage={}, content=[SimpleNamespace(type='text',text=json.dumps(corrections(card,saved)))])
    monkeypatch.setattr(llm, 'create', create)
    result = asyncio.run(review.answer('tenant','Check exact source caveats.',card,saved))
    assert len(result['changes']) == 9
    assert captured[0]['system'] == review.SYSTEM and 'untrusted' in captured[0]['system']
    assert captured[0]['output_config']['format']['schema'] == review.SCHEMA
    data = json.loads(captured[0]['messages'][0]['content'])
    assert data['drafts_and_evidence'] == review.context(card,saved)


def test_final_admission_still_requires_overlap_and_source_coverage_decisions():
    from fastapi import HTTPException
    row, saved = seeded(); card = review.draft_card(row, saved)
    row['payload'] = review.corrected_payload(row, saved, corrections(card, saved))
    context = {'version':'current','brief':row['payload']['entries'][0]['content'], 'sources':[], 'reviews':[row]}
    form = {'version':'current', **{'action_'+e['id']:'keep' for e in row['payload']['entries']}}
    with pytest.raises(HTTPException) as failure: admission.plan(context, row, form)
    assert failure.value.status_code == 409


def test_merges_preserve_mixed_and_legacy_authorship_through_saved_review():
    row, saved = seeded()
    row['payload']['entries'] = row['payload']['entries'][:2]
    target, incoming = row['payload']['entries']
    target['content'] = 'Owner supplied specific wording.'
    target['content_segments'] = [{'kind': 'owner_said', 'text': target['content'], 'reference': 'Owner edit'}]
    incoming['content'] = 'Older draft with unrecorded authorship.'
    context = {'version': 'current', 'brief': '', 'sources': [], 'reviews': [row]}
    form = {'version': 'current', 'action_'+target['id']: 'keep', 'action_'+incoming['id']: 'merge_pending:'+target['id']}
    notes, payload = admission.plan(context, row, form)
    segments = payload['entries'][0]['combined_content_segments']
    assert [s['kind'] for s in segments] == ['owner_said', 'unattributed']
    assert [s['text'] for s in segments] == [target['content'], incoming['content']]
    source = {'source': notes[0]['label'], 'content': notes[0]['content'], 'mailbox_id': None}
    payload['entries'][0]['admitted_content_hash'] = admission.fingerprint(source['content'])
    context['sources'] = [source]
    context['reviews'] = [dict(row, state='saved', payload=payload)]
    assert admission.saved_wording_segments(context, source) == segments
    next_row, _ = seeded(); next_row['payload']['entries'] = next_row['payload']['entries'][:1]
    entry = next_row['payload']['entries'][0]; entry['label'] = source['source']
    entry['content'] = 'A new owner clarification.'
    entry['content_segments'] = [{'kind': 'owner_said', 'text': entry['content'], 'reference': 'Owner edit'}]
    context['reviews'].append(next_row)
    candidate = next(m for m in admission.candidates(entry, context, next_row['id']) if m['kind'] == 'saved')
    merged = admission.effective_entries(context, next_row, [entry], {'action_'+entry['id']: 'merge_saved:'+candidate['id']})[0]
    assert merged['content_segments'] == segments + entry['content_segments']
    changed = dict(source, content='Changed outside the recorded review')
    assert admission.saved_wording_segments(context, changed)[0]['kind'] == 'unattributed'


def test_final_source_coverage_keeps_owner_authorship_for_original_text():
    from tests.test_knowledge_fidelity import source_review
    from tests.test_knowledge_review import form
    context, row = source_review()
    entry = row['payload']['entries'][0]
    entry['content_segments'] = [{'kind':'owner_said', 'reference':'Owner edit', 'text':entry['content']}]
    fields = form(context,row); fields['source_default'] = 'note:'+entry['id']
    notes, payload = admission.plan(context,row,fields)
    parts = payload['entries'][0]['combined_content_segments']
    assert parts[0] == entry['content_segments'][0]
    assert parts[1]['kind'] == 'owner_site'
    assert 'We do not make a legal determination from an email.' in parts[1]['text']
    preview = admission.review_view(context,row,fields)
    assert preview['entries'][0]['content_segments'] == parts
