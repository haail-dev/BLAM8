"""Subject boundaries preserve per-note evidence and the owner's admission gate."""
import asyncio
import json
from copy import deepcopy

from app import kb_website, knowledge_coverage
from app.knowledge_note_structure import SUBJECT_NOTES
from tests.test_onboarding_kb_website import _Msg


def test_distillation_keeps_related_conditions_without_recombining_subjects(monkeypatch):
    facts = [
        ('Included services', 'We coordinate requests and provide progress updates.', ['process']),
        ('Service exclusions', 'We do not provide overnight attendance.', ['rules']),
        ('Approval limits', 'Only the named approver may approve costs above £200, unless an emergency agreement applies.', ['rules']),
        ('Opening hours', 'We open Monday to Friday, 9 am to 4 pm, excluding public holidays.', ['hours']),
    ]
    url = 'https://example.test/about'
    pages = [{'url': url, 'text': '\n\n'.join(f[1] for f in facts)}]
    before = deepcopy(pages)

    async def create(**kwargs):
        assert SUBJECT_NOTES in kwargs['system']
        return _Msg(json.dumps({'entries': [dict(label=title, content=text, topics=topics,
            sources=[dict(url=url, excerpt=text, claim=text)]) for title, text, topics in facts], 'questions': []}))

    monkeypatch.setattr(kb_website.llm, 'create', create)
    monkeypatch.setattr(kb_website.agent, 'client', lambda: None)
    monkeypatch.setattr(kb_website.usage, 'record_anthropic', lambda *a, **kw: None)
    result = asyncio.run(kb_website.distill('offline', url, pages))
    assert pages == before
    assert [(r['label'], r['content'], r['topics']) for r in result] == facts
    for r in result:
        assert r['provenance'] == [dict(source=url, excerpt=r['content'], claim=r['content'])]
    assert SUBJECT_NOTES in knowledge_coverage.prompt_block(knowledge_coverage.coverage([]))
    # Broad coverage tags do not combine independently editable rules.
    assert result[1]['topics'] == result[2]['topics'] and result[1]['label'] != result[2]['label']


def test_broad_draft_partitions_into_exact_subjects_before_admission(monkeypatch):
    facts = [('Identity', 'We are a small independent team.'),
             ('Services', 'We maintain shared equipment.'),
             ('Service limits', 'We do not provide overnight support unless separately agreed.')]
    url = 'https://example.test'
    original = ' '.join(text for _, text in facts)
    async def create(**kwargs):
        citation = kwargs['output_config']['format']['schema']['properties']['entries']['items']['properties']['sources']['items']
        assert 'subject' in citation['required']
        return _Msg(json.dumps({'entries': [{'label':'Overview', 'content':original, 'topics':['rules'],
            'sources':[dict(url=url, excerpt=text, claim=text, subject=label) for label,text in facts]}], 'questions':[]}))
    monkeypatch.setattr(kb_website.llm,'create',create)
    monkeypatch.setattr(kb_website.agent,'client',lambda:None)
    monkeypatch.setattr(kb_website.usage,'record_anthropic',lambda *a,**kw:None)
    result=asyncio.run(kb_website.distill('offline',url,[dict(url=url,text=original)]))
    assert [(r['label'],r['content']) for r in result]==facts
    assert 'unless separately agreed' in result[2]['content']
    assert all(len(r['provenance'])==1 and r['provenance'][0]['claim']==r['content'] for r in result)


def test_subject_partition_keeps_ambiguous_or_uncovered_clauses_intact():
    from app.knowledge_note_structure import partition_subjects
    original={'label':'Overview','content':'We maintain equipment. Terms depend on the agreement.',
              'sources':[dict(subject='Services',claim='We maintain equipment.'),
                         dict(subject='Limits',claim='Terms depend on')], 'topics':[]}
    before=deepcopy(original)
    assert partition_subjects(original)==[before] and original==before
    original['sources'][1]['claim']='Terms depend on the agreement.'
    original['sources'].append(dict(subject='Different meaning',claim='We maintain equipment.'))
    assert partition_subjects(original)==[original]


def test_subject_partition_never_splits_mid_sentence_or_price_schedule():
    from app.knowledge_note_structure import partition_subjects
    note={'content':'It costs £40, unless separately agreed.','sources':[
        dict(subject='Price',claim='It costs £40,'),dict(subject='Exception',claim='unless separately agreed.')]}
    assert partition_subjects(note)==[note]
    note={'label':'Prices','content':'First item £40. Additional items £20.', 'sources':[
        dict(subject='Prices',claim='First item £40.'),dict(subject='Prices',claim='Additional items £20.')]}
    assert partition_subjects(note)==[note]
