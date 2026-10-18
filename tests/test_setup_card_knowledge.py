import asyncio
from copy import deepcopy
import pytest
from app import setup_cards as cards, setup_card_knowledge as notes, db
from tests.test_setup_cards import start, append
from tests.test_setup_card_store import Client


def proposal(label='Hours', content='We open at 9.', **kw):
    return start(append('note', label=label, content=content, **kw), page='knowledge')


def apply(card, sources=None):
    return cards.accept(card, card['revision'], 'accept', [r['id'] for r in card['lines']],
        {'sources': sources or [{'label': 'Hours', 'chunks': []}]})


def test_note_prose_sources_and_receipt_preserve_exact_facts():
    text = 'We open at 9.\n\nA | B costs £0. {{reference}}'
    card = proposal(content=text)
    original = deepcopy(card)
    accepted, patch = apply(card, [{'label': 'Hours', 'chunks': [{'id': 'old', 'content': 'We open at 8.'}]}])
    assert patch == {'notes': [{'label': 'Hours', 'content': text}]}
    assert card == original and accepted['lines'][0]['status'] == 'applied'
    assert accepted['lines'][0]['sources'] == card['lines'][0]['sources']
    assert accepted['last_action']['changes'][0]['before']['stored_chunks'] == ['We open at 8.']


def test_legacy_note_boundary_preserves_content_after_first_separator():
    from app import setup_rows
    row = setup_rows.parse('knowledge', 'note', 'Hours | A | B\n\nC')
    assert row['fields'] == {'label': 'Hours', 'content': 'A | B\n\nC'}


@pytest.mark.parametrize('fields', [{}, {'label': 'X', 'content': ''},
    {'label': 'X'*81, 'content': 'X'}, {'label': 'X', 'content': 'X'*4001},
    {'label': 'X', 'content': 3}, {'label': 'X', 'content': 'X', 'mailbox_id': 'other'}])
def test_notes_reject_missing_overlong_and_scoped_fields(fields):
    with pytest.raises(ValueError): notes.parse(fields)


def test_snapshot_scopes_each_source_and_does_not_treat_failure_as_empty():
    client = Client(response=[])
    assert notes.snapshot('owner', proposal(), client) == {'sources': [{'label': 'Hours', 'chunks': []}]}
    assert notes.snapshot('owner', proposal(label=' Hours '), Client(response=[])) == {'sources': [{'label': 'Hours', 'chunks': []}]}
    for call in [('eq', ('tenant_id', 'owner'), {}), ('eq', ('source', 'Hours'), {}),
                 ('is_', ('mailbox_id', 'null'), {}), ('limit', (7,), {})]:
        assert call in client.calls
    with pytest.raises(RuntimeError): notes.snapshot('owner', proposal(), Client(response=None))
    with pytest.raises(cards.Invalid, match='imported'):
        notes.snapshot('owner', proposal(), Client(response=[{}]*7))


def test_embedding_failure_and_incomplete_vectors_never_prepare_a_write(monkeypatch):
    monkeypatch.setattr(notes.kb, 'is_configured', lambda: True)
    patch = {'notes': [{'label': 'Hours', 'content': 'x'*4000}]}
    async def embed(texts): return [[0.1]*1024 for _ in texts], 12
    monkeypatch.setattr(notes.embeddings, 'embed_documents', embed)
    result, tokens = asyncio.run(notes.prepare(patch))
    assert tokens == 12 and len(result['notes'][0]['chunks']) > 1
    assert 'chunks' not in patch['notes'][0]
    for vectors in [[], [[0.1]], [[float('nan')]*1024]*4]:
        async def bad(texts): return vectors, 1
        monkeypatch.setattr(notes.embeddings, 'embed_documents', bad)
        with pytest.raises(RuntimeError): asyncio.run(notes.prepare(patch))


def test_ordinary_source_replacement_uses_one_verified_rpc():
    client = Client(response=1)
    rows = [{'content': 'Text'}]
    db.replace_knowledge_source('owner', 'Hours', rows, client=client, mailbox_id='mailbox')
    assert [c[0] for c in client.calls] == ['rpc', 'execute']
    assert client.calls[0][1] == ('replace_knowledge_source_atomic', {
        'p_tenant': 'owner', 'p_source': 'Hours', 'p_mailbox': 'mailbox', 'p_rows': rows})
    with pytest.raises(RuntimeError):
        db.replace_knowledge_source('owner', 'Hours', rows, client=Client(response=None))
