from copy import deepcopy

import pytest
from fastapi import HTTPException
from jinja2 import Environment, FileSystemLoader
from pathlib import Path

from app import knowledge_fidelity as fidelity, knowledge_review as kr
from tests.test_knowledge_review import context, entry, form


def source_review():
    ctx, row = context([entry(content='We charge £95 per item each month.')])
    row['payload']['source_units'] = fidelity.inventory([
        {'url':'https://example.test/prices', 'text':
         'These are fictional sample facts.\nWe charge £95 per item each month.\n'
         'Urgent safety guidance must not wait for an approval request.\n'
         'We do not make a legal determination from an email.\n'
         'There is no emergency-dispatch integration.'}])
    return ctx, row


def test_inventory_keeps_exact_read_statements_and_all_duplicate_sources():
    pages=[{'url':'https://example.test/a','text':'Fictional sample facts.\nOnly if agreed.\nA costs £95.'},
           {'url':'https://example.test/b','text':'Fictional sample facts.\nKeep this separate.'}]
    units=fidelity.inventory(pages)
    assert len(units)==4
    assert units[0]['kind']=='qualification' and units[1]['kind']=='constraint'
    assert units[0]['sources']==[p['url'] for p in pages]
    assert [u['text'] for u in units]==['Fictional sample facts.','Only if agreed.','A costs £95.','Keep this separate.']


def test_citations_and_topic_coverage_do_not_hide_omitted_constraints():
    ctx,row=source_review(); f=form(ctx,row)
    row['payload']['entries'][0]['topics']=list(kr.QUESTIONS)
    before=deepcopy(ctx)
    view=kr.review_view(ctx,row)
    assert view['coverage']['pending_count']==8
    assert view['source_view']['unresolved']==3
    assert any('legal determination' in u['text'] and u['status']=='unresolved' for u in view['source_view']['units'])
    with pytest.raises(HTTPException,match='unaccounted'):kr.plan(ctx,row,f)
    assert ctx==before


def test_explicit_mapping_retains_exact_words_and_qualifications_without_new_cards():
    ctx,row=source_review(); f=form(ctx,row);f['source_default']='note:one'
    notes,payload=kr.plan(ctx,row,f)
    assert len(notes)==1 and notes[0]['label'].startswith('Prices · one')
    assert 'fictional sample facts' in notes[0]['source_context']
    assert 'We do not make a legal determination from an email.' in notes[0]['content']
    assert 'Urgent safety guidance must not wait for an approval request.' in notes[0]['content']
    assert 'no emergency-dispatch integration' in notes[0]['content']
    assert 'no emergency service' not in notes[0]['content']
    assert payload['entries'][0]['id']=='one'
    assert all(u['status']!='unresolved' for u in payload['source_decisions'])
    assert payload['source_decisions'][0]['status']=='retained_context'
    assert any(p.get('excerpt')=='We do not make a legal determination from an email.'
               for p in payload['entries'][0]['combined_provenance'])


def test_exclusions_need_reasons_and_cannot_strip_source_qualifications():
    ctx,row=source_review(); f=form(ctx,row);f['source_default']='note:one'
    unit=row['payload']['source_units'][2];key='source_'+unit['id']
    f[key]='exclude'
    with pytest.raises(HTTPException,match='Explain'):kr.plan(ctx,row,f)
    f['source_reason_'+unit['id']]='Reviewer rejects this statement as obsolete.'
    _,payload=kr.plan(ctx,row,f)
    outcome=next(u for u in payload['source_decisions'] if u['id']==unit['id'])
    assert outcome['status']=='excluded' and outcome['reason']==f['source_reason_'+unit['id']]
    f['source_'+row['payload']['source_units'][0]['id']]='exclude'
    with pytest.raises(HTTPException,match='qualifications'):kr.plan(ctx,row,f)


def test_saved_merge_carries_both_sources_qualifications_into_rechunking():
    ctx,row=source_review();source={'source':'Prices','mailbox_id':None,'content':row['payload']['entries'][0]['content']}
    old_context='Source qualification (https://old.example.test): Provisional facts.'
    ctx['sources']=[source]
    ctx['reviews'].insert(0,{'id':'old','state':'saved','payload':{'entries':[{
        'admitted_source':'Prices','mailbox_id':None,'admitted_content_hash':kr.fingerprint(source['content']),
        'source_context':old_context,'provenance':[{'source':'https://old.example.test'}]}]}})
    target=next(m for m in kr.candidates(row['payload']['entries'][0],ctx,row['id']) if m['kind']=='saved')
    f=form(ctx,row);f['action_one']='merge_saved:'+target['id'];f['source_default']='note:one'
    notes,payload=kr.plan(ctx,row,f)
    assert old_context in notes[0]['source_context'] and 'fictional sample facts' in notes[0]['source_context']
    assert payload['entries'][0]['source_context']==notes[0]['source_context']


def test_editing_away_literal_fact_invalidates_coverage_and_discard_cannot_be_targeted():
    ctx,row=source_review(); f=form(ctx,row)
    f['content_one']='We offer a service.'
    assert kr.review_view(ctx,row,f)['source_view']['unresolved']==4
    row['payload']['entries'].append(entry('two','Hours','Our office opens at nine.'))
    f['action_two']='keep';f['action_one']='discard';f['source_default']='note:one'
    with pytest.raises(HTTPException,match='unaccounted'):kr.plan(ctx,row,f)
    f['action_two']='discard'
    notes,payload=kr.plan(ctx,row,f)
    assert notes==[] and all(u['status']=='discarded_with_drafts' for u in payload['source_decisions'])


@pytest.mark.asyncio
async def test_every_embedded_chunk_carries_source_qualification(monkeypatch):
    from app import setup_card_knowledge as store
    ctx,row=source_review();f=form(ctx,row);f['source_default']='note:one'
    f['content_one']='A long factual description. '*250
    notes,_=kr.plan(ctx,row,f);seen=[]
    async def embed(texts):
        seen.extend(texts)
        return [[0.1]*1024 for _ in texts], 12
    monkeypatch.setattr(store.kb,'is_configured',lambda:True)
    monkeypatch.setattr(store.embeddings,'embed_documents',embed)
    prepared,_=await store.prepare({'notes':notes})
    chunks=prepared['notes'][0]['chunks']
    assert len(chunks)>1 and seen==[c['content'] for c in chunks]
    assert all(c['content'].startswith('Source qualification (https://example.test/prices): These are fictional sample facts.') for c in chunks)


def test_source_review_is_unselected_and_escapes_source_markup():
    ctx,row=source_review()
    row['payload']['source_units']+=fidelity.inventory([{'url':'https://example.test/x','text':'<script>bad()</script>'}])
    env=Environment(loader=FileSystemLoader(Path(__file__).parents[1]/'src/app/templates'),autoescape=True)
    html=env.get_template('_knowledge_review.html').render(review=kr.review_view(ctx,row),csrf_token=lambda r:'token',request={})
    assert 'Source-to-note review' in html and '4 unresolved' in html
    assert 'data-source-qualifications' in html and '&lt;script&gt;' in html
    import re
    source_selects=re.findall(r'<select name="source_[^"]*".*?</select>',html,re.S)
    assert source_selects and '<script>' not in html
    assert all(not re.search(r'<option[^>]* selected',select) for select in source_selects)
