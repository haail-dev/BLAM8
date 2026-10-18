"""Bounded immutable website evidence for revisioned Knowledge proposals.

Source text is untrusted data. A valid reference proves attribution, not entailment.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4

from app import setup_cards

MAX_PAGES = 8
MAX_TEXT = 60_000
RETENTION_DAYS = 30


def digest(value):
    return sha256(value.encode('utf-8')).hexdigest()


def build(pages):
    if not isinstance(pages, list) or not 1 <= len(pages) <= MAX_PAGES:
        raise setup_cards.Invalid('Supply between one and eight source pages.')
    result = []
    for page in pages:
        if not isinstance(page, dict) or set(page) != {'url', 'text'}:
            raise setup_cards.Invalid('Supply a source URL and its text.')
        url, text = page['url'], page['text']
        if not isinstance(url, str) or not url.startswith(('https://', 'http://')) or len(url) > 2048:
            raise setup_cards.Invalid('Invalid source URL.')
        if not isinstance(text, str) or not text.strip() or len(text) > 12_000:
            raise setup_cards.Invalid('Source text is missing or too large.')
        result.append({'url': url, 'text': text, 'hash': digest(text)})
    if sum(len(p['text']) for p in result) > MAX_TEXT:
        raise setup_cards.Invalid('Source text exceeds the review limit.')
    return {'id': str(uuid4()), 'pages': result,
            'expires_at': (datetime.now(timezone.utc) + timedelta(days=RETENTION_DAYS)).isoformat()}


def reference(evidence, page, quote):
    if type(page) is not int or not 0 <= page < len(evidence['pages']) or not isinstance(quote, str) or not quote:
        raise setup_cards.Invalid('Supply an exact source passage.')
    start = evidence['pages'][page]['text'].find(quote)
    if start < 0:
        raise setup_cards.Invalid('The quoted passage does not occur in the retained source.')
    return f"{evidence['id']}/{page}/{start}/{start + len(quote)}"


def resolve(evidence, ref):
    try:
        identity, page, start, end = ref.split('/')
        page, start, end = int(page), int(start), int(end)
        if identity != evidence['id'] or not 0 <= page < len(evidence['pages']):
            raise ValueError()
        source = evidence['pages'][page]
        if not 0 <= start < end <= len(source['text']) or digest(source['text']) != source['hash']:
            raise ValueError()
        if datetime.fromisoformat(evidence['expires_at']) <= datetime.now(timezone.utc):
            raise ValueError()
        return {'url': source['url'], 'quote': source['text'][start:end]}
    except (ValueError, TypeError, KeyError, AttributeError):
        raise setup_cards.Invalid('Source evidence is missing, expired or does not match.') from None


def load(tenant_id, identity):
    from app import db
    rows = (db.db().table('knowledge_review_sources').select('evidence')
            .eq('tenant_id', tenant_id).eq('id', identity).limit(1).execute().data)
    if not isinstance(rows, list) or len(rows) != 1:
        raise setup_cards.Invalid('Retained website evidence is unavailable. Review these notes manually.')
    evidence = rows[0]['evidence']
    if evidence.get('id') != identity or not evidence.get('pages'):
        raise setup_cards.Invalid('Retained source identity does not match.')
    for i, page in enumerate(evidence['pages']):
        resolve(evidence, reference(evidence, i, page['text']))
    return evidence
