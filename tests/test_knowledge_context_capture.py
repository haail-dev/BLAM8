import asyncio
from copy import deepcopy
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader
from app import kb_website as web, knowledge_collection as collection, knowledge_review as review
from tests.test_knowledge_review import context, entry, form


def test_shared_business_vocabulary_does_not_block_admission():
    note = entry(content='We manage residential property for owners in North London.')
    ctx, row = context([note], brief='Our two-person residential property agency helps owners in North London with repairs.')
    assert review.candidates(note, ctx, row['id']) == []
    assert len(review.plan(ctx, row, form(ctx, row))[0]) == 1


@pytest.mark.parametrize('left,right', [
    ('We open Monday to Friday at 9 am and close at 4 pm.', 'We open Monday to Friday at 9 am and close at 5 pm.'),
    ('We accept requests without prior approval.', 'We accept requests with prior approval.'),
])
def test_fact_difference_has_both_exact_quotes_and_requires_review(left, right):
    ctx, row = context([entry(content=left)], brief=right)
    match = review.candidates(row['payload']['entries'][0], ctx, row['id'])[0]
    assert match['comparisons'] == [{'type': 'difference', 'left': left, 'right': right}]
    assert match['comparison_title'] == 'Check a possible fact difference'
    with pytest.raises(Exception):
        review.plan(ctx, row, form(ctx, row))


def test_repeated_source_display_is_unique_but_citations_remain_intact():
    text = 'We provide ongoing support only after written confirmation.'
    one, two = entry(content=text), entry('two', 'Service scope', text)
    two['provenance'] = [{'source':'https://example.test/scope','excerpt':text,'claim':text},
                         {'source':'https://example.test/scope','excerpt':'written confirmation','claim':text}]
    ctx, row = context([one, two]); before = deepcopy(ctx)
    view = review.review_view(ctx, row)
    match = view['entries'][0]['matches'][0]
    assert len(match['provenance']) == 2 and len(match['source_labels']) == 1
    env = Environment(loader=FileSystemLoader(Path(__file__).parents[1]/'src/app/templates'), autoescape=True)
    html = env.get_template('_knowledge_review.html').render(review=view, csrf_token=lambda _: 'csrf', request={})
    assert '“'+text+'”' in html and 'This note · Prices' in html
    assert html.count('Source: https://example.test/scope') == 1
    assert 'complete semantic coverage' not in html and ctx == before


def test_context_is_saved_tenant_data_not_chat_secrets_or_pending_facts():
    ctx, _ = context([entry(content='Unconfirmed suggestion')], sources=[
        {'source':'Access','content':'Confirmed delivery access','mailbox_id':None},
        {'source':'Private','content':'Mailbox private fact','mailbox_id':'other'}], brief='Owner confirmed brief')
    result = collection.context_for({'goals':'Reduce missed enquiries','api_key':'SECRET','chat':'Unconfirmed chat'},ctx)
    assert 'Owner confirmed brief' in str(result) and 'Confirmed delivery access' in str(result)
    assert result['open_questions'] == ['Does the price vary by quantity?']
    assert all(value not in str(result) for value in ['SECRET','Unconfirmed','Mailbox private'])


@pytest.mark.parametrize('subject', ['Ceramic firing', 'Translation delivery', 'Equipment calibration', 'Membership access'])
def test_link_ranking_uses_owner_vocabulary_and_anchor_text(subject):
    html = f'<a href="/pricing">Prices</a><a href="/details">{subject}</a>'
    hints = {'open_questions':[subject], 'saved_context':[]}
    assert web.discover_links('https://example.test/',html,hints)[0].endswith('/details')
    assert web.discover_links('https://example.test/',html)[0].endswith('/pricing')


def test_nested_context_page_discovered_with_attempt_budget_and_local_hints(monkeypatch):
    calls=[]
    pages={'/':'<p>Public facts.</p><a href="/index">Index</a><a href="/other">Other</a>',
           '/index':'<a href="/details">Calibration tolerances</a>',
           '/details':'<p>Calibration tolerances are agreed in writing.</p>', '/other':'<p>Other facts.</p>'}
    async def fetch(client,url):
        from urllib.parse import urlparse
        calls.append(url)
        return url,pages[urlparse(url).path].encode(),'text/html'
    monkeypatch.setattr(web,'_fetch_raw',fetch);monkeypatch.setattr(web,'MAX_PAGES',3)
    _, captured=asyncio.run(web.crawl('https://example.test/',context={'open_questions':['Calibration tolerances']}))
    assert calls == ['https://example.test/','https://example.test/index','https://example.test/details']
    assert any(p['status']=='page_limit_omitted' and p['url'].endswith('/other') for p in captured.manifest)
    assert 'agreed in writing' in captured[-1]['text']


def test_owner_context_is_distinct_from_website_evidence():
    prompt=web._build_prompt('https://example.test', [{'url':'https://example.test','text':'Website fact.'}], 'Name',None,
        {'saved_context':[{'label':'Hours','content':'Owner corrected 4 pm'}],'open_questions':[]})
    assert 'reference only, not website evidence' in prompt
    assert prompt.index('Owner corrected 4 pm') < prompt.index('=== PAGE:')
    assert 'Do not copy these facts into website notes' in prompt


def test_repeated_passage_pairs_do_not_multiply_within_notes():
    from app.knowledge_comparison import comparison
    statement = 'We confirm every request before agreeing a date.'
    assert comparison((statement+' ')*5, (statement+' ')*3) == [
        {'type':'repeated','left':statement,'right':statement}]


def test_linked_offsite_redirect_is_not_website_evidence(monkeypatch):
    async def fetch(client, url):
        if url.endswith('/jump'):
            return 'https://other.test/', b'<p>Foreign facts.</p>', 'text/html'
        return url,b'<p>Our facts.</p><a href="/jump">Details</a>','text/html'
    monkeypatch.setattr(web, '_fetch_raw', fetch)
    _, pages = asyncio.run(web.crawl('https://example.test/'))
    assert len(pages) == 1 and 'Foreign' not in str(list(pages))
    assert pages.manifest[-1]['status'] == 'offsite_redirect'


def test_slow_link_is_bounded_and_recorded_without_losing_home(monkeypatch):
    async def fetch(client, url):
        if url.endswith('/slow'):
            await asyncio.sleep(1)
        return url,b'<p>Our facts.</p><a href="/slow">More facts</a>','text/html'
    monkeypatch.setattr(web, '_fetch_raw', fetch)
    monkeypatch.setattr(web, 'FETCH_TIMEOUT', .01)
    _, pages = asyncio.run(web.crawl('https://example.test/'))
    assert len(pages) == 1 and pages.manifest[-1]['status'] == 'failed'


def test_same_title_still_offers_merge_without_an_overlap_warning():
    ctx,row=context([entry(label='Access',content='Bring a reference number.')],
                    sources=[{'source':'Access','content':'Use the side entrance.','mailbox_id':None}])
    view=review.review_view(ctx,row)
    assert view['entries'][0]['matches']==[]
    target=view['entries'][0]['merge_options'][0]
    fields=form(ctx,row);fields['action_one']='merge_saved:'+target['id']
    notes,_=review.plan(ctx,row,fields)
    assert 'Use the side entrance.' in notes[0]['content'] and 'Bring a reference number.' in notes[0]['content']


@pytest.mark.parametrize('width,theme', [(390,'dark'),(1280,'light')])
def test_native_quoted_comparison_layout(tmp_path, width, theme):
    import subprocess
    from bs4 import BeautifulSoup
    from tests.test_knowledge_guidance_browser import chromium_binary
    root = Path(__file__).resolve().parents[1]
    url = 'https://example.test/'+'long-source-path/'*15
    one = entry(content='Our office closes at 5 pm on weekdays.')
    two = entry('two','Office hours','Our office closes at 4 pm on weekdays.')
    two['provenance']=[{'source':url,'excerpt':'4 pm'},{'source':url,'excerpt':'weekdays'}]
    ctx,row=context([one,two])
    env=Environment(loader=FileSystemLoader(root/'src/app/templates'),autoescape=True)
    html=env.get_template('_knowledge_review.html').render(review=review.review_view(ctx,row),csrf_token=lambda _: 'csrf',request={})
    card=str(BeautifulSoup(html,'html.parser').select_one('[data-duplicate-candidate]'))
    css=(root/'src/app/static/tw.css').read_text(encoding='utf-8')
    page=tmp_path/'comparison.html'
    page.write_text('<!doctype html><html data-theme="'+theme+'"><meta charset="utf-8"><style>'+css+
        '</style><body class="bg-slate-100 text-slate-700" style="margin:0;width:'+str(width)+'px;padding:12px;box-sizing:border-box">'+card+
        '<script>const card=document.querySelector("[data-duplicate-candidate]");'
        'const quotes=card.querySelectorAll("blockquote");'
        'document.body.dataset.result=(quotes.length===2&&Array.from(quotes).every(q=>q.textContent.startsWith("“")&&q.getBoundingClientRect().height>0)&&card.scrollWidth<=card.clientWidth+1)?"PASS":"FAIL";'
        '</script></body></html>',encoding='utf-8')
    result=subprocess.run([chromium_binary(),'--headless=new','--no-sandbox','--disable-gpu','--no-first-run',
        '--disable-background-networking','--disable-component-update','--disable-sync','--no-proxy-server',
        '--host-resolver-rules=MAP * ~NOTFOUND','--user-data-dir='+str(tmp_path/'profile'),
        '--window-size='+str(max(width,500))+',900','--force-device-scale-factor=1',
        '--screenshot='+str(tmp_path/'comparison.png'),'--dump-dom',page.as_uri()],
        capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
    assert result.returncode==0 and 'data-result="PASS"' in result.stdout


def test_malformed_link_does_not_abort_valid_discovery():
    assert web.discover_links('https://example.test/', '<a href="http://[broken">Bad</a><a href="/details">Details</a>') == ['https://example.test/details']
