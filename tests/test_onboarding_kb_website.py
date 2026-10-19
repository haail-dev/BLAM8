"""Onboarding "start from your website" flow (src/app/kb_website.py).

Paste a URL → crawl same-site pages → one LLM call proposes labelled notes →
the owner reviews → /knowledge/website/save is the ONLY writer. These pin:
link discovery (same-site, priority-scored, assets skipped), the distill
parse, the propose endpoint's error surface, and the save endpoint's indexed
fields (an unticked checkbox must never misalign a label with the wrong
content).

Run: python -m pytest tests/test_onboarding_kb_website.py -q
"""
import pytest
from fastapi.testclient import TestClient

from app import db, kb_website
from tests.knowledge_review_fixture import review_store
from app import knowledge as kb
from app.auth import TenantCtx, require_tenant
from app.main import app
from app.security import require_csrf


@pytest.fixture
def client():
    app.dependency_overrides[require_tenant] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)   # no `with`: startup events must not run
    app.dependency_overrides.clear()


@pytest.fixture
def store(monkeypatch, review_store):
    saved: list[dict] = []

    async def fake_ingest(tenant_id, source, text, client=None, mailbox_id=None):
        saved.append({"source": source, "content": text})
        return 1

    monkeypatch.setattr(kb, "ingest", fake_ingest)
    monkeypatch.setattr(kb, "is_configured", lambda: True)
    return saved


# ── link discovery ───────────────────────────────────────────────────────────

HOME = """
<a href="/pricing">Prices</a>
<a href="/blog/post-1">Blog</a>
<a href="https://www.example.co.uk/contact-us">Contact</a>
<a href="https://facebook.com/example">FB</a>
<a href="/logo.png">logo</a>
<a href="mailto:hi@example.co.uk">mail</a>
<a href="#top">top</a>
<a href="/pricing/">Prices again</a>
"""


def test_discover_links_same_site_priority_first_no_assets():
    links = kb_website.discover_links("https://example.co.uk/", HOME)
    # priority pages (pricing, contact) lead; www ↔ bare host is the same site;
    # externals, images, mailto, fragments and the /pricing dupe are gone.
    assert links == ["https://example.co.uk/pricing",
                     "https://www.example.co.uk/contact-us",
                     "https://example.co.uk/blog/post-1"]


def test_normalize_url_adds_scheme():
    assert kb_website.normalize_url("example.co.uk") == "https://example.co.uk"
    assert kb_website.normalize_url("  http://example.co.uk ") == "http://example.co.uk"


# ── distill parsing ──────────────────────────────────────────────────────────

class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Msg:
    def __init__(self, text):
        self.content = [_Block(text)]
        self.model = "test-model"
        self.usage = {}


def test_distill_parses_and_drops_empty_entries(monkeypatch):
    async def fake_create(**kwargs):
        return _Msg('{"entries": [{"label": "Opening hours", "content": "Mon-Fri 8-6."},'
                    '{"label": "Junk", "content": "   "}]}')

    monkeypatch.setattr(kb_website.llm, "create", fake_create)
    monkeypatch.setattr(kb_website.agent, "client", lambda: None)
    monkeypatch.setattr(kb_website.usage, "record_anthropic", lambda *a, **k: None)
    import asyncio
    entries = asyncio.run(kb_website.distill("t1", "https://x", [{"url": "u", "text": "t"}]))
    assert entries[0]["label"] == "Opening hours" and entries[0]["content"] == "Mon-Fri 8-6."
    assert entries[0]["provenance"][0]["source"] == "u"


def test_distill_unparseable_reply_is_empty_never_an_error(monkeypatch):
    async def fake_create(**kwargs):
        return _Msg("not json")

    monkeypatch.setattr(kb_website.llm, "create", fake_create)
    monkeypatch.setattr(kb_website.agent, "client", lambda: None)
    monkeypatch.setattr(kb_website.usage, "record_anthropic", lambda *a, **k: None)
    import asyncio
    assert asyncio.run(kb_website.distill("t1", "https://x", [])) == []


def test_distill_preserves_caveats_questions_and_only_verified_page_citations(monkeypatch):
    import asyncio,json
    fact="These are fictional test facts. No external dispatch integration is configured."
    async def fake_create(**kwargs):
        return _Msg(json.dumps({'entries':[{'label':'Service information','content':fact,'topics':['process'],
            'sources':[{'url':'https://example.test/info','excerpt':fact},
                       {'url':'https://other.test','excerpt':fact},
                       {'url':'https://example.test/info','excerpt':'No dispatch service is offered.'}]}],
            'questions':[{'topic':'prices','question':'Does the rate vary with request size?'}]}))
    monkeypatch.setattr(kb_website.llm,'create',fake_create)
    monkeypatch.setattr(kb_website.agent,'client',lambda:None)
    monkeypatch.setattr(kb_website.usage,'record_anthropic',lambda *a,**kw:None)
    result=asyncio.run(kb_website.distill('tenant','https://example.test',[{'url':'https://example.test/info','text':fact}]))
    assert result[0]['content']==fact
    assert result[0]['provenance']==[{'source':'https://example.test/info','excerpt':fact}]
    assert result.questions==[{'topic':'prices','question':'Does the rate vary with request size?'}]


@pytest.mark.parametrize('excerpt_is_partial', [False, True])
def test_drafts_retain_complete_cited_process_and_named_channel_scope(monkeypatch, excerpt_is_partial):
    import asyncio, json
    process = 'We acknowledge a report, gather the issue and access availability, then explain the next step.'
    limitation = 'This sample business does not provide diagnosis or emergency dispatch through this website or ReplyDesk.'
    abbreviated = [process.split(', then')[0] + '.', limitation.replace(' or ReplyDesk', '')]
    statements = [process, limitation]
    pages = [{'url':'https://example.invalid/home', 'text':process + '\nAn unrelated opening offer.'},
             {'url':'https://example.invalid/safety', 'text':limitation + '\nFictional sample facts only.'}]
    captured = []
    async def model(**kw):
        captured.append(kw)
        return _Msg(json.dumps({'entries':[
            {'label':label, 'content':short, 'topics':['process'], 'sources':[
                {'url':page['url'], 'excerpt':whole.split(', then')[0] if excerpt_is_partial and i == 0 else whole,
                 'claim':short}]}
            for i, (label, short, whole, page) in enumerate(zip(
                ['Report rules', 'Service limits'], abbreviated, statements, pages))], 'questions':[]}))
    monkeypatch.setattr(kb_website.llm, 'create', model)
    monkeypatch.setattr(kb_website.agent, 'client', lambda: None)
    monkeypatch.setattr(kb_website.usage, 'record_anthropic', lambda *a, **k: None)
    entries = asyncio.run(kb_website.distill('synthetic', 'https://example.invalid', pages))
    assert len(entries) == 2
    for entry, short, whole in zip(entries, abbreviated, statements):
        assert short in entry['content'] and whole in entry['content']
        assert 'unrelated opening offer' not in entry['content']
        assert any(whole == p['excerpt'] for p in entry['provenance'])
    assert 'then explain the next step' in entries[0]['content']
    assert 'or ReplyDesk' in entries[1]['content']
    assert len(captured) == 1  # no provider retry or writer


def test_cited_statement_expansion_does_not_silently_cut_at_note_limit(monkeypatch):
    import asyncio, json
    statement = 'We receive reports ' + 'with supporting information ' * 160 + 'then explain the next step.'
    async def model(**kw):
        return _Msg(json.dumps({'entries':[{'content':'We receive reports.', 'sources':[
            {'url':'https://example.invalid', 'excerpt':'We receive reports', 'claim':'We receive reports.'}]}]}))
    monkeypatch.setattr(kb_website.llm, 'create', model)
    monkeypatch.setattr(kb_website.agent, 'client', lambda: None)
    monkeypatch.setattr(kb_website.usage, 'record_anthropic', lambda *a, **k: None)
    entries = asyncio.run(kb_website.distill('synthetic', 'https://example.invalid', [
        {'url':'https://example.invalid', 'text':statement}]))
    assert entries == [] and 'No source clauses were silently dropped' in entries.failure


# ── propose endpoint ─────────────────────────────────────────────────────────

def _tenant_env(monkeypatch):
    monkeypatch.setattr(db, "get_tenant",
                        lambda tid, client=None: {"name": "Example Ltd", "industry": "trades"})


def test_website_proposes_entries_as_json(client, store, monkeypatch):
    _tenant_env(monkeypatch)

    async def fake_build(tenant_id, url, business_name=None, industry=None):
        assert (tenant_id, business_name, industry) == ("t1", "Example Ltd", "trades")
        return {"site": "https://example.co.uk", "pages": ["https://example.co.uk"],
                "entries": [{"label": "Prices", "content": "£45 + VAT."}]}

    monkeypatch.setattr(kb_website, "build", fake_build)
    r = client.post("/onboarding/knowledge/website", data={"url": "example.co.uk"})
    assert r.status_code == 200
    assert r.json()["entries"] == [{"label": "Prices", "content": "£45 + VAT."}]
    assert store == []          # proposing must not ingest anything


def test_website_bad_url_is_a_400_with_the_reason(client, store, monkeypatch):
    _tenant_env(monkeypatch)

    async def fake_build(*a, **k):
        raise ValueError("Only http(s) URLs are supported.")

    monkeypatch.setattr(kb_website, "build", fake_build)
    r = client.post("/onboarding/knowledge/website", data={"url": "ftp://x"})
    assert r.status_code == 400
    assert "http(s)" in r.json()["error"]


def test_website_no_facts_is_a_422(client, store, monkeypatch):
    _tenant_env(monkeypatch)

    async def fake_build(*a, **k):
        return {"site": "https://example.co.uk", "pages": [], "entries": []}

    monkeypatch.setattr(kb_website, "build", fake_build)
    r = client.post("/onboarding/knowledge/website", data={"url": "example.co.uk"})
    assert r.status_code == 422


def test_incomplete_website_drafts_are_reported_without_staging(client,store,monkeypatch,review_store):
    _tenant_env(monkeypatch)
    async def build(*a,**kw):
        return {'entries':[], 'distillation_error':'Draft output limit reached; no drafts staged.'}
    monkeypatch.setattr(kb_website,'build',build)
    response=client.post('/onboarding/knowledge/website',data={'url':'example.co.uk'})
    assert response.status_code==422 and 'output limit' in response.json()['error']
    assert review_store.rows==[] and store==[]


def test_website_unconfigured_kb_is_a_400(client, store, monkeypatch):
    monkeypatch.setattr(kb, "is_configured", lambda: False)
    r = client.post("/onboarding/knowledge/website", data={"url": "example.co.uk"})
    assert r.status_code == 400
    assert "VOYAGE_API_KEY" in r.json()["error"]


# ── save endpoint (the only writer) ──────────────────────────────────────────

def test_save_stages_only_ticked_notes_with_aligned_fields(client, store, review_store):
    r = client.post("/onboarding/knowledge/website/save", data={
        "count": "3",
        # inc_0 absent = unticked; its label/content must NOT bleed into others
        "label_0": "Marketing fluff", "content_0": "We are passionate.",
        "inc_1": "1", "label_1": "Opening hours", "content_1": "Mon-Fri 8-6.",
        "inc_2": "1", "label_2": "", "content_2": "£45 + VAT per pallet.",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert "/knowledge/review/" in r.headers["location"]
    assert store == []
    entries = review_store.rows[-1]["payload"]["entries"]
    assert [e["label"] for e in entries] == ["Opening hours", "Website note 3"]
    assert entries[0]["content"] == "Mon-Fri 8-6."


def test_save_nothing_ticked_redirects_with_an_error(client, store):
    r = client.post("/onboarding/knowledge/website/save",
                    data={"count": "1", "label_0": "x", "content_0": "y"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "err=" in r.headers["location"]
    assert store == []
