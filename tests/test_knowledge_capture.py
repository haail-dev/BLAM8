"""Synthetic whole-page evidence and budget regressions; no network or model."""
import asyncio
import json
from app import knowledge_capture as capture, knowledge_fidelity, kb_website


def test_nonmain_qualifiers_and_rules_survive_without_joining_headings():
    html = '''<head><title>Ignored</title></head><header><nav><a href="/">Home</a></nav>
    <p>Only confirmed requests proceed.</p></header><main><h1>Service</h1>
    <p>We acknowledge the report, then explain the next step.</p></main>
    <aside><p>Do not claim contact unless confirmed.</p></aside>
    <footer><p>Fictional test content. No real records or contact details.</p></footer>'''
    result = capture.extract(html)
    assert 'Ignored' not in result['text']
    assert 'Service\nWe acknowledge' in result['text']
    units = knowledge_fidelity.inventory([{'url': 'https://example.invalid', **result}])
    assert next(u for u in units if u['text'].startswith('Fictional'))['spans'][0]['locations'] == ['footer']
    assert next(u for u in units if 'claim contact' in u['text'])['kind'] == 'constraint'
    for unit in units:
        for span in unit['spans']:
            assert knowledge_fidelity.normal(result['text'][span['start']:span['end']]) == unit['text']


def test_budget_never_silently_cuts_a_qualifying_sentence():
    result = capture.extract('<p>One complete fact. Only if approval is confirmed.</p><p>Later fact.</p>')
    limited = capture.bounded(result, 28)
    assert limited['text'] == 'One complete fact.'
    assert limited['omitted_chars'] == len(result['text']) - len(limited['text'])
    assert capture.bounded(result, 0)['text'] == ''
    assert limited['available_hash'] == capture.bounded(result, 500)['available_hash']


def test_crawl_accounts_for_failure_short_page_and_every_budget(monkeypatch):
    home = '<main><p>Home fact.</p></main>' + ''.join(f'<a href="/{p}">{p}</a>' for p in ('policy', 'contact', 'about', 'hours', 'pricing'))
    async def fetch(client, url):
        if url.endswith('/policy'): raise ValueError('Synthetic failure')
        body = home if url == 'https://example.invalid/' else '<footer><p>Fictional test facts.</p></footer>'
        return url, body.encode(), 'text/html'
    monkeypatch.setattr(kb_website, '_fetch_raw', fetch)
    monkeypatch.setattr(kb_website, 'MAX_PAGES', 4)
    monkeypatch.setattr(kb_website, 'TOTAL_CHARS', 80)
    _, pages = asyncio.run(kb_website.crawl('https://example.invalid/'))
    assert any(p['status'] == 'failed' for p in pages.manifest)
    assert any(p['status'] == 'page_limit_omitted' for p in pages.manifest)
    assert any(p.get('omitted_chars', 0) for p in pages.manifest)
    assert sum(len(p['text']) for p in pages) <= 80
    assert any('Fictional test facts.' in p['text'] for p in pages)


def test_multiple_exact_spans_keep_each_claim_and_reject_joined_quotes(monkeypatch):
    from types import SimpleNamespace
    content = 'We accept reports. We do not promise attendance.'
    raw = {'entries': [{'label': 'Service rules', 'content': content, 'topics': ['process'], 'sources': [
        {'url': 'https://example.invalid/a', 'excerpt': 'We accept reports.', 'claim': 'We accept reports.'},
        {'url': 'https://example.invalid/b', 'excerpt': 'We do not promise attendance.', 'claim': 'We do not promise attendance.'},
        {'url': 'https://example.invalid/a', 'excerpt': 'We accept reports. We do not promise attendance.', 'claim': content},
        {'url': 'https://example.invalid/a', 'excerpt': 'We accept reports.', 'claim': 'Invented claim.'}]}], 'questions': []}
    async def model(**kw):
        return SimpleNamespace(content=[SimpleNamespace(type='text', text=json.dumps(raw))], model='fake', usage={})
    monkeypatch.setattr(kb_website.llm, 'create', model)
    monkeypatch.setattr(kb_website.agent, 'client', lambda: None)
    monkeypatch.setattr(kb_website.usage, 'record_anthropic', lambda *a, **k: None)
    pages = [{'url': 'https://example.invalid/a', 'text': 'We accept reports. An intervening fact. We do not promise attendance.'},
             {'url': 'https://example.invalid/b', 'text': 'We do not promise attendance.'}]
    entries = asyncio.run(kb_website.distill('tenant', 'https://example.invalid', pages))
    assert len(entries[0]['provenance']) == 2
    assert {p['claim'] for p in entries[0]['provenance']} == {'We accept reports.', 'We do not promise attendance.'}
