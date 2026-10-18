"""Company-wide note proposals and preparation before atomic acceptance."""
from copy import deepcopy
import math
from app import knowledge as kb, embeddings


def parse(fields):
    if not isinstance(fields, dict) or set(fields) - {'label', 'content'}:
        raise ValueError('Supply a note label and content.')
    for key, limit in [('label', 80), ('content', 4000)]:
        value = fields.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(f'Supply {key} within {limit} characters.')
    return {k: fields[k].strip() for k in ('label', 'content')}


def snapshot(tenant, card, client):
    from app import db
    from app.setup_cards import PENDING, Invalid
    labels = sorted({r['fields']['label'].strip() for r in card['lines']
                     if r['status'] in PENDING and isinstance(r['fields'].get('label'), str)})
    sources = []
    for label in labels:
        rows = ((client or db.db()).table('knowledge_chunks').select('id,content')
                .eq('tenant_id', tenant).eq('source', label).is_('mailbox_id', 'null')
                .order('id').limit(kb.EDITABLE_MAX_CHUNKS + 1).execute().data)
        if not isinstance(rows, list):
            raise RuntimeError('Knowledge could not be verified.')
        if len(rows) > kb.EDITABLE_MAX_CHUNKS:
            raise Invalid('Review this imported source on the Knowledge page before replacing it.')
        sources.append({'label': label, 'chunks': rows})
    return {'sources': sources}


def patch(card, ids, snapshot):
    from app.setup_cards import PENDING, Invalid
    if not ids or len(set(ids)) != len(ids):
        raise Invalid('Choose distinct pending notes.')
    selected = [r for r in card['lines'] if r['id'] in ids and r['status'] in PENDING]
    if len(selected) != len(ids) or len(selected) > 20:
        raise Invalid('Choose up to twenty pending notes.')
    sources = {s['label']: s['chunks'] for s in snapshot['sources']}
    notes, changes = [], []
    for line in selected:
        try:
            fields = parse(line['fields'])
        except ValueError as exc:
            raise Invalid(str(exc)) from None
        if fields['label'] not in sources:
            raise Invalid('Reload the note before accepting it.')
        before = [c['content'] for c in sources[fields['label']]]
        notes.append(fields)
        changes.append({'id': line['id'], 'name': 'note', 'fields': fields,
                        'before': {'label': fields['label'], 'stored_chunks': before}, 'after': deepcopy(fields),
                        'notes': ['Company-wide knowledge. Acceptance replaces the complete note under this exact label.']})
    return {'notes': notes}, changes


async def prepare(patch):
    """No writes until every embedding is present and valid."""
    if not kb.is_configured():
        raise RuntimeError('Knowledge ingestion is unavailable.')
    result = deepcopy(patch)
    parts = [kb.chunk_text(n['content']) for n in result['notes']]
    # Source qualifications travel with EVERY independently retrieved chunk.
    parts = [[n['source_context'] + '\n\n' + text for text in group] if n.get('source_context') else group
             for n, group in zip(result['notes'], parts)]
    texts = [text for group in parts for text in group]
    vectors, tokens = await embeddings.embed_documents(texts)
    if len(vectors) != len(texts) or any(len(v) != 1024 or any(
            type(x) not in (int, float) or not math.isfinite(x) for x in v) for v in vectors):
        raise RuntimeError('Knowledge embeddings could not be verified.')
    offset = 0
    for note, group in zip(result['notes'], parts):
        note['chunks'] = [{'content': text, 'embedding': kb._vec_literal(vectors[offset + i])}
                          for i, text in enumerate(group)]
        offset += len(group)
    return result, tokens
