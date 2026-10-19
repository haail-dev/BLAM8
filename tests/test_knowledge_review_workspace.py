from copy import deepcopy
from app import knowledge_review as kr, knowledge_source_review as source, knowledge_source_evidence as evidence, setup_cards
from tests.test_knowledge_review import context, entry, form
from tests.test_knowledge_source_review import seeded
import pytest
from fastapi import HTTPException


def test_partial_sentence_overlap_after_long_unrelated_prefix_has_one_pair_and_exact_evidence():
    sentence = 'We do not promise a date, price or attendance before an authorised person confirms it.'
    one = entry('one','First policy','Alpha unique wording. '*130+sentence)
    two = entry('two','Other policy','Different independent content. '*130+sentence)
    ctx,row=context([one,two]);before=deepcopy(ctx)
    a=kr.candidates(one,ctx,row['id']);b=kr.candidates(two,ctx,row['id'])
    assert a[0]['exact_passages']==[sentence] and a[0]['key']==b[0]['key']
    view=kr.review_view(ctx,row)
    assert view['summary']['overlaps']==1
    assert sum(len(n['matches']) for n in view['entries'])==1
    f=form(ctx,row);f['match_'+a[0]['key']]='keep_both'
    assert len(kr.plan(ctx,row,f)[0])==2 and ctx==before
    f['content_two']=two['content']+' Changed wording.'
    with pytest.raises(HTTPException) as failure:kr.plan(ctx,row,f)
    assert failure.value.status_code==409


def test_known_topic_does_not_create_default_questions_or_approve_coverage():
    ctx,row=context([entry(content='Our fees vary only as stated in the signed agreement.')])
    row['payload']['questions']=[]
    topic=next(t for t in kr.topic_view(ctx)['topics'] if t['key']=='prices')
    assert topic['questions']==[] and topic['state']=='unknown'
    assert kr.plan(ctx,row,form(ctx,row))[1]['topic_reviews']['prices']['state']=='unknown'


def test_signatures_distinguish_noop_content_evidence_and_other_context_changes():
    ctx,row=context([entry()]);start=kr.review_view(ctx,row)['signatures']
    assert kr.review_view(deepcopy(ctx),deepcopy(row))['signatures']==start
    ctx['version']='other-context';after=kr.review_view(ctx,row)['signatures']
    assert after['content']==start['content'] and after['evidence']==start['evidence'] and after['context']!=start['context']
    row['payload']['source_units']=[{'id':'u','text':'A source fact.','kind':'statement','sources':[]}]
    assert kr.review_view(ctx,row)['signatures']['evidence']!=start['evidence']
    row['payload']['entries'][0]['content']+=' An owner edit.'
    assert kr.review_view(ctx,row)['signatures']['content']!=start['content']


def test_website_saved_state_uses_actual_saved_notes_separately_from_pending():
    ctx,row=context([entry()]);row['payload']['origin']='Website: https://example.invalid'
    assert kr.website_state(ctx)['pending_notes']==1
    row['state']='saved';row['payload']['entries'][0]['admitted_source']='Saved source'
    assert kr.website_state(ctx)['state']=='idle'
    ctx['sources']=[{'source':'Saved source','content':'A fact.'}]
    assert kr.website_state(ctx)=={'state':'saved','pending_notes':0,'saved_notes':1,'review_href':None}


def citation_review(row,saved):
    card=source.draft_card(row,saved);line=card['lines'][0]
    raw={'revision':card['revision'],'changes':[{'id':line['id'],'content_hash':evidence.digest(line['fields']['content']),
        'insertions':[], 'citations':[{'claim':line['fields']['content'], 'reference':evidence.reference(saved,0,'Fictional test profile only.')}]}],
        'overlaps':[],'questions':[]}
    return card,raw


def test_citation_only_proposal_preserves_wording_and_records_owner_scoped_audit():
    row,saved=seeded();before=deepcopy(row);card,raw=citation_review(row,saved)
    preview=source.validate(card,saved,raw)
    assert preview['changes'][0]['before']==preview['changes'][0]['after']
    payload=source.corrected_payload(row,saved,raw,owner_request='Check this qualification citation.',reviewer='owner')
    assert [n['content'] for n in payload['entries']]==[n['content'] for n in row['payload']['entries']]
    assert payload['entries'][0]['provenance'][-1]['excerpt']=='Fictional test profile only.'
    audit=payload['source_review_history'][0]
    assert audit['reviewer']=='owner' and audit['revision']==2 and audit['owner_request'] and audit['at']
    assert row==before
    next_row={**row,'payload':payload};next_card,next_raw=citation_review(next_row,saved)
    repeated=source.validate(next_card,saved,next_raw)
    assert repeated['changes']==[] and 'already-recorded' in repeated['summary']
    with pytest.raises(setup_cards.Invalid,match='no corrections'):source.apply(next_card,saved,next_raw,turn='repeat')
    with pytest.raises(setup_cards.Stale):source.validate(next_card,saved,raw)


def test_citation_cannot_create_replacement_wording_or_manufacture_a_missing_footer_quote():
    row,saved=seeded();card,raw=citation_review(row,saved)
    raw['changes'][0]['citations'][0]['claim']='Invented replacement clause.'
    with pytest.raises(setup_cards.Invalid,match='existing note'):source.validate(card,saved,raw)
    card,raw=citation_review(row,saved);raw['changes'][0]['citations'][0]['reference']='missing-footer/0/0/5'
    with pytest.raises(setup_cards.Invalid,match='retained'):source.validate(card,saved,raw)


def test_requested_missing_intake_and_contact_constraints_reach_actual_drafts_without_admission():
    from app import knowledge_fidelity
    from uuid import uuid4
    intake='We acknowledge the report, gather details, then explain the next step.'
    contact='Do not claim that another party or emergency service has been contacted unless that action is confirmed.'
    pages=[{'url':'https://example.invalid/home','text':'Fictional sample facts.\n'+intake},
           {'url':'https://example.invalid/hours','text':contact}]
    saved=evidence.build(pages)
    row={'id':str(uuid4()),'state':'pending','payload':{'source_snapshot':saved['id'],'draft_revision':1,
        'source_units':knowledge_fidelity.inventory(pages),'entries':[
            entry('intake','Intake','We acknowledge reports and gather details.'),entry('hours','Office','We review reports in office hours.')],
        'questions':[],'origin':'Website'}}
    card=source.draft_card(row,saved);raw={'revision':1,'changes':[], 'overlaps':[],'questions':[]}
    for line,page,quote in zip(card['lines'],(0,1),(intake,contact)):
        raw['changes'].append({'id':line['id'],'content_hash':evidence.digest(line['fields']['content']),
            'insertions':[{'position':'after','reference':evidence.reference(saved,page,quote)}]})
    before=deepcopy(row);payload=source.corrected_payload(row,saved,raw,owner_request='Restore both source constraints.',reviewer='owner')
    assert row==before and row['state']=='pending'
    assert intake in payload['entries'][0]['content'] and contact in payload['entries'][1]['content']
    ctx={'version':'fresh','brief':'','sources':[],'reviews':[{**row,'payload':payload}]}
    notes,admitted=kr.plan(ctx,ctx['reviews'][0],{'version':'fresh','action_intake':'keep','action_hours':'keep'})
    assert all('Fictional sample facts.' in n['source_context'] for n in notes)
    assert all(u['status']!='unresolved' for u in admitted['source_decisions'])
    assert ctx['sources']==[]  # constructing final decisions still does not write Knowledge
