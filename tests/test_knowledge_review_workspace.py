from copy import deepcopy
from app import knowledge_review as kr, knowledge_source_review as source, knowledge_source_evidence as evidence, setup_cards
from tests.test_knowledge_review import context, entry, form
from tests.test_knowledge_source_review import seeded
import pytest


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
    with pytest.raises(Exception):kr.plan(ctx,row,f)


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
    with pytest.raises(setup_cards.Invalid,match='already recorded'):source.validate(next_card,saved,next_raw)
    with pytest.raises(setup_cards.Stale):source.validate(next_card,saved,raw)


def test_citation_cannot_create_replacement_wording_or_manufacture_a_missing_footer_quote():
    row,saved=seeded();card,raw=citation_review(row,saved)
    raw['changes'][0]['citations'][0]['claim']='Invented replacement clause.'
    with pytest.raises(setup_cards.Invalid,match='existing note'):source.validate(card,saved,raw)
    card,raw=citation_review(row,saved);raw['changes'][0]['citations'][0]['reference']='missing-footer/0/0/5'
    with pytest.raises(setup_cards.Invalid,match='retained'):source.validate(card,saved,raw)
