"""Source choices stay local, relevant and reversible across multi-page review."""
from copy import deepcopy
from app import knowledge_guidance as guide, knowledge_fidelity, knowledge_review
from tests.test_knowledge_review import context, entry
from tests.test_knowledge_guidance import act
from tests.test_knowledge_source_routes import source_client
from tests.test_knowledge_source_review import seeded
from app import knowledge_source_review as source, knowledge_source_evidence as evidence, setup_cards
import pytest


def multirole():
    notes=[entry('overview','Business overview','We answer general enquiries about the demonstration.'),
           entry('maintenance','Maintenance reports','We collect maintenance reports and explain the next step.'),
           entry('prices','Service prices','Our service prices and payment terms are illustrative.')]
    # Home has cross-cutting material; citations/page ownership alone are insufficient.
    for note in notes:note['provenance']=[{'source':'https://example.test/','excerpt':note['content']}]
    ctx,row=context(notes)
    row['payload']['source_units']=knowledge_fidelity.inventory([
        {'url':'https://example.test/','text':'Fictional test facts only.\nMaintenance reports must include useful details before we explain the next step.\nHome'},
        {'url':'https://example.test/prices','text':'Service prices include illustrative payment terms for this demonstration.\nContact us'}])
    return ctx,row,{'version':ctx['version']}


def test_first_keep_does_not_absorb_other_notes_or_navigation():
    ctx,row,form=multirole();before=deepcopy(row)
    response=act(ctx,row,form,'keep')
    assert response['focus']['kind']=='note' and response['focus']['id']=='maintenance'
    assert not any(k.startswith('source_') for k in form)
    kept=act(ctx,row,form,'keep')
    assert kept['focus']['kind']=='note' and kept['focus']['id']=='prices'
    assert not any(k.startswith('source_') for k in form)
    assert row==before


def test_unrelated_page_with_only_one_cited_note_does_not_become_its_destination():
    ctx,row,form=multirole()
    row['payload']['entries'][1]['provenance']=[{'source':'https://example.test/policies'}]
    unit=next(u for u in row['payload']['source_units'] if 'must include' in u['text'])
    assert [n['id'] for n in guide.source_targets(unit,row['payload']['entries'])]==['maintenance']
    unit={'text':'Independent unrelated weather information is published separately.','sources':['https://example.test/']}
    assert guide.source_targets(unit,row['payload']['entries'])==[]


def test_large_support_group_requires_individual_passage_review():
    ctx,row,form=multirole()
    row['payload']['source_units']=knowledge_fidelity.inventory([{'url':'https://example.test/',
        'text':'\n'.join(f'Maintenance reports must include detail {i} before staff explain the next step.' for i in range(5))}])
    form.update(action_overview='keep',action_maintenance='keep')
    shown=guide.present(ctx,row,form)
    assert shown['focus']['kind']=='note' and shown['focus']['id']=='prices'
    act(ctx,row,form,'keep')
    shown=guide.present(ctx,row,form)
    if shown['focus']['kind']=='overlap':act(ctx,row,form,'keep_both');shown=guide.present(ctx,row,form)
    assert shown['focus']['kind']=='source_batch' and shown['focus']['remaining']==5
    assert 'Keep the original website wording' not in [a['label'] for a in shown['actions']]
    assert len(shown['reply'])<1000 and not any(k.startswith('source_') for k in form)


def test_undo_unsaved_spill_restores_coverage_without_rewriting_or_excluding_facts():
    ctx,row,form=multirole();before=deepcopy(row)
    form['action_overview']='keep'
    form.update({'source_'+u['id']:'note:overview' for u in row['payload']['source_units'] if u['kind']!='qualification'})
    form['source_default']='note:overview'
    form['content_overview']='Owner edited the short overview.'
    response=act(ctx,row,form,'undo_support',target='overview')
    assert all(v=='' for k,v in response['patches'].items() if k.startswith('source_'))
    assert form['content_overview']=='Owner edited the short overview.'
    assert form['action_overview']=='keep' and 'exclude' not in form.values()
    assert guide.view(ctx,row,form)['source_view']['unresolved']>0
    assert 'Fictional test facts only.' in response['shared_caveats']
    assert row==before


def test_ambiguous_wording_remains_unresolved_instead_of_auto_excluded():
    ctx,row,form=multirole()
    form.update({'action_'+n['id']:'discard' for n in row['payload']['entries']})
    # No kept target: admission's existing source/discard rules remain authoritative.
    _,item=guide.current(ctx,row,form)
    assert item['kind'] in {'question','blocked','ready'}
    assert not any(k.startswith('source_') for k in form)


def test_overlap_explanation_is_bounded_and_binds_the_full_text():
    a=entry('a','Same label','A repeated statement with enough words to identify shared exact wording.\n'+'Long retained source text. '*120)
    b=entry('b','Same label',a['content'])
    ctx,row=context([a,b]);form={'version':ctx['version'],'action_a':'keep','action_b':'keep'}
    shown=guide.present(ctx,row,form)
    assert shown['focus']['kind']=='overlap' and len(shown['reply'])<1800
    form['content_a']=a['content']+' Material late qualifier.'
    assert guide.present(ctx,row,form)['focus_hash']!=shown['focus_hash']
    assert len(row['payload']['entries'][0]['content'])>2500


def test_empty_model_change_is_a_read_only_noop_not_a_false_limit_error():
    row,saved=seeded();card=source.draft_card(row,saved);line=card['lines'][0]
    raw={'revision':1,'changes':[{'id':line['id'],'content_hash':evidence.digest(line['fields']['content']),
        'insertions':[],'citations':[]}],'overlaps':[],'questions':['Which local source choice should be undone?']}
    checked=source.validate(card,saved,raw)
    assert checked['changes']==[] and checked['questions']==raw['questions']
    assert 'No new draft corrections' in checked['summary'] and 'twelve' not in checked['summary']
    with pytest.raises(setup_cards.Invalid,match='no corrections'):
        source.corrected_payload(row,saved,raw)


def test_redundant_citation_does_not_block_an_independent_valid_correction():
    row,saved=seeded();quote='Fictional test profile only.';ref=evidence.reference(saved,0,quote)
    first,second=row['payload']['entries'][:2]
    first['provenance'].append({'source':saved['pages'][0]['url'],'excerpt':quote,'claim':first['content']})
    card=source.draft_card(row,saved)
    def change(n):return {'id':n['id'],'content_hash':evidence.digest(n['content']),
        'insertions':[],'citations':[{'claim':n['content'],'reference':ref}]}
    raw={'revision':1,'changes':[change(first),change(second)],'overlaps':[],'questions':[]}
    checked=source.validate(card,saved,raw)
    assert [c['id'] for c in checked['changes']]==[second['id']]
    assert 'citation changes only' in checked['summary'] and 'already-recorded' in checked['summary']
    bad=deepcopy(raw);bad['changes'][0]['citations'][0]['reference']='foreign-snapshot/0/1/2'
    with pytest.raises(setup_cards.Invalid):source.validate(card,saved,bad)


def test_correction_reads_the_same_local_retention_state_without_persisting_it(source_client,monkeypatch):
    client,row,saved,ctx,calls,_=source_client
    first=row['payload']['entries'][0]
    row['payload']['source_units']=knowledge_fidelity.inventory([{'url':p['url'],'text':p['text']} for p in saved['pages']])
    unit=next(u for u in row['payload']['source_units'] if u['kind']!='qualification' and u['text'] not in first['content'])
    local={'version':'v1','action_'+first['id']:'keep','source_'+unit['id']:'note:'+first['id'],
           'content_'+first['id']:first['content'],'label_'+first['id']:first['label']}
    seen=[]
    async def capture(tenant,question,card,source_snapshot):
        seen.append(source.context(card,source_snapshot))
        return {'changes':[],'review':{},'overlaps':[],'questions':[],'summary':'No new corrections.'}
    monkeypatch.setattr(source,'answer',capture)
    response=client.post('/onboarding/knowledge/review/'+row['id']+'/corrections',json={
        'version':'v1','question':'Review this local choice.','edits':{first['id']:{'content':first['content'],'label':first['label']}},'form':local})
    assert response.status_code==200 and calls==[]
    effective=next(n for n in seen[0]['local_review']['notes'] if n['id']==first['id'])
    assert effective['effective_content']==first['content']  # Evidence choices do not append source text.
    assert seen[0]['lines'][0]['content']==first['content'] and unit['text'] not in first['content']
    assert seen[0]['local_review']['source_choices']['source_'+unit['id']]=='note:'+first['id']


@pytest.mark.parametrize('case',['version','conflicting_text','merge'])
def test_inconsistent_local_review_stops_before_model_or_writer(source_client,monkeypatch,case):
    client,row,saved,ctx,calls,_=source_client;note=row['payload']['entries'][0]
    async def forbidden(*a):pytest.fail('Invalid local review must be rejected before the model')
    monkeypatch.setattr(source,'answer',forbidden)
    local={'version':'v1'}
    if case=='version':local['version']='v0'
    elif case=='conflicting_text':local['content_'+note['id']]='Different local body.'
    else:local['action_'+note['id']]='merge_pending:'+row['payload']['entries'][1]['id']
    response=client.post('/onboarding/knowledge/review/'+row['id']+'/corrections',json={
        'version':'v1','question':'Correct this.','form':local})
    assert response.status_code in {409,422} and calls==[]


def test_noop_apply_never_reaches_the_draft_rpc(source_client):
    client,row,saved,ctx,calls,_=source_client;note=row['payload']['entries'][0]
    raw={'revision':1,'changes':[{'id':note['id'],'content_hash':evidence.digest(note['content']),
        'insertions':[],'citations':[]}],'overlaps':[],'questions':[]}
    response=client.post('/onboarding/knowledge/review/'+row['id']+'/corrections/apply',json={'version':'v1','review':raw})
    assert response.status_code==422 and 'no corrections' in response.json()['detail']
    assert calls==[]
