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
