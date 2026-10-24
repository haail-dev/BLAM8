"""Bounded owner context for collection, never additional website evidence.

Only saved, tenant-scoped facts and review questions enter this projection.
Raw chat, credentials, customer records and suggested settings do not.
"""
import re
from html.parser import HTMLParser
from urllib.parse import unquote, urlparse


def context_for(config, review):
    facts = []
    for label, value in [('Business brief', review.get('brief') or config.get('business_brief')),
                         ('Owner goals', config.get('goals'))]:
        if isinstance(value, str) and value.strip():
            facts.append({'label': label, 'content': value.strip()[:2000]})
    for note in review.get('sources', []):
        # A business-wide capture must not import another mailbox's private facts.
        if note.get('mailbox_id') is None:
            facts.append({'label': str(note.get('source', 'Saved note'))[:160],
                          'content': str(note.get('content', '')).split('\n\nSource evidence:')[0][:2000]})
    questions = []
    from app.knowledge_review import topic_view
    explicit = {q.get('question') for row in review.get('reviews', [])
                for q in row.get('payload', {}).get('questions', [])}
    for topic in topic_view(review)['topics']:
        for question in topic['questions']:
            if question['status'] == 'unknown' and question['question'] in explicit:
                questions.append(question['question'][:300])
    # Explicit budgets; these hints never claim that omitted context was examined.
    return {'saved_context': facts[:12], 'open_questions': list(dict.fromkeys(questions))[:20],
            'omitted_notes': max(0, len(facts)-12), 'omitted_questions': max(0, len(set(questions))-20)}


def terms(text):
    framing = set('a an the and or of to for we our us you your is are be it its this that with in on at from by as have has will can do does what when where how which'.split())
    return {word for word in re.findall(r'[^\W_]+', text.casefold()) if len(word) > 2 and word not in framing}


class Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items, self.href, self.label = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.href = dict(attrs).get('href')
            self.label = []

    def handle_data(self, text):
        if self.href is not None:
            self.label.append(text)

    def handle_endtag(self, tag):
        if tag == 'a' and self.href is not None:
            self.items.append((self.href, ' '.join(self.label)))
            self.href = None


def relevance(url, label, context):
    """Rank public links with local hints; no context is sent in HTTP requests."""
    words = terms(unquote(urlparse(url).path) + ' ' + label)
    questions = terms(' '.join(context.get('open_questions', [])))
    known = terms(' '.join(n['label'] + ' ' + n['content'] for n in context.get('saved_context', [])))
    return 3 * len(words & questions) + len(words & known)
