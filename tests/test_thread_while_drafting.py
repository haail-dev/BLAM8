"""The inbound must remain readable before a draft or CRM contact exists."""
import asyncio
import json
from pathlib import Path
import re
import shutil
import subprocess

import httpx
import pytest
from fastapi.testclient import TestClient

from app import dashboard, events, inbox_labels, pipeline
from app.auth import TenantCtx, require_onboarded
from app.main import app


ADDRESS = "holly@example.test"
BODY = "Hello, could I book a visit?\nMy details are below."


@pytest.fixture
def client(monkeypatch):
    events._drafting.clear()
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    for name, result in {
        "list_email_log": [], "active_modules": set(), "get_tenant": {"name": "Test business"},
        "is_enterprise": False, "get_tenant_config": {}, "list_documents": [],
        "crm_contact_by_email": None, "bookings_for_contact": [],
    }.items():
        monkeypatch.setattr(dashboard.db, name, lambda *a, _result=result, **k: _result)
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: False)
    monkeypatch.setattr(dashboard, "_sends_with", lambda *a: None)
    yield TestClient(app)
    app.dependency_overrides.clear()
    events._drafting.clear()


def row(**kw):
    return dict({"id": "e1", "from_address": ADDRESS, "from_name": "Holly Marsh",
                 "subject": "A visit", "body_in": BODY, "draft_reply": None,
                 "source_message_id": "m1", "created_at": "2026-09-27T16:52:00+00:00",
                 "status": "pending", "knowledge_gap": None, "draft_sources": None,
                 "draft_reasoning": None, "action_summary": None}, **kw)


def set_rows(monkeypatch, rows):
    monkeypatch.setattr(dashboard.db, "get_conversation", lambda *a, **k: [dict(r) for r in rows])


def stage_body(html):
    return re.search(r'id="stage-bubble-body"[^>]*>(.*?)</div>', html, re.S).group(1)


@pytest.mark.parametrize("endpoint", ["", "/fragment"])
@pytest.mark.parametrize("known", [False, True])
@pytest.mark.parametrize("crm_name, expected", [
    (None, "Holly Marsh"), ("Holly at CRM", "Holly at CRM"), ("  ", "Holly Marsh"),
])
@pytest.mark.parametrize("state", ["drafting", "drafted", "gap_hold", "document_hold", "sent", "skipped"])
def test_every_thread_state_keeps_the_customer_words_and_name(
        client, monkeypatch, endpoint, known, crm_name, expected, state):
    current = row()
    if state == "drafting":
        # The open-before-fetch registry entry has no preview; the row does.
        events.begin_drafting("t1", "m1", {"from_address": ADDRESS, "body": None})
    elif state in ("drafted", "sent"):
        current["draft_reply"] = "Thank you for your message."
        if state == "sent":
            current["status"] = "sent"
    elif state == "gap_hold":
        current["knowledge_gap"] = "Opening hours"
    elif state == "document_hold":
        current["action_summary"] = pipeline.DOC_HOLD_CONFIRM
    else:
        current.update(status="skipped", category="fyi",
                       category_reason="Sender is on your ignore list")
    previous = row(id="older", source_message_id="old", status="sent", body_in="Earlier question",
                   draft_reply="Earlier reply")
    set_rows(monkeypatch, ([previous] if known else []) + [current])
    monkeypatch.setattr(dashboard.db, "crm_contact_by_email",
                        lambda *a, **k: {"id": "c1", "name": crm_name} if crm_name is not None else None)
    response = client.get("/app/conversations/view" + endpoint, params={"contact": ADDRESS})
    assert response.status_code == 200
    html = response.text
    assert BODY == stage_body(html)
    assert "A visit" in html and expected in html
    thread_html = html[html.index('<div id="thread-stage"'):]
    assert "None" not in thread_html and ">null<" not in thread_html
    if not endpoint:
        assert f">{expected}</a>" in html and f"{expected} · BLAM8" in html
    if state == "drafting":
        assert "Agent writing reply" in html
        assert 'class="stage-bubble glass-card min-w-0' in html  # visible without a tap
        assert BODY in html[:html.index('id="stage-ghost-live"')]
        assert html.count(BODY) == 2  # stored history + stage; no provisional duplicate


def test_document_release_recovers_the_exact_stored_message(client, monkeypatch):
    set_rows(monkeypatch, [row(), row(id="e2", source_message_id="m2", body_in="A newer message")])
    events.begin_drafting("t1", "held:e1", {"from_address": ADDRESS, "body": "", "subject": ""})
    html = client.get("/app/conversations/view/fragment", params={"contact": ADDRESS}).text
    assert stage_body(html) == BODY
    assert 'data-draft-key="held:e1"' not in html


def test_new_message_never_borrows_previous_rounds_words(client, monkeypatch):
    set_rows(monkeypatch, [row(status="sent", draft_reply="An old reply")])
    events.begin_drafting("t1", "different", {"from_address": ADDRESS, "body": None})
    html = client.get("/app/conversations/view/fragment", params={"contact": ADDRESS}).text
    assert stage_body(html) == "Message text is not available yet."
    assert "None" not in html


@pytest.mark.parametrize("body", [None, "", "   "])
def test_absent_body_and_reply_never_make_a_null_or_empty_bubble(client, monkeypatch, body):
    set_rows(monkeypatch, [row(body_in=body, from_name=None, subject=None, status="skipped")])
    html = client.get("/app/conversations/view", params={"contact": ADDRESS}).text
    assert stage_body(html) == "No message text was included."
    assert f">{ADDRESS}</a>" in html
    assert "None" not in html


def test_pre_insert_preview_names_the_sender_and_escapes_the_message(client, monkeypatch):
    set_rows(monkeypatch, [])
    events.begin_drafting("t1", "m1", {"from_address": ADDRESS, "from_name": "Holly Marsh",
                                       "subject": "A visit", "body": "Hello <script>bad()</script>"})
    html = client.get("/app/conversations/view", params={"contact": ADDRESS}).text
    assert ">Holly Marsh</a>" in html
    assert stage_body(html) == "Hello &lt;script&gt;bad()&lt;/script&gt;"
    assert "None" not in html


@pytest.mark.parametrize("created", [True, False])
def test_ignored_mail_keeps_an_identified_skipped_record_without_drafting(monkeypatch, caplog, created):
    calls, traces = [], []
    monkeypatch.setattr(pipeline, "_inbox_rules", lambda *a: {})
    monkeypatch.setattr(pipeline.db, "is_ignored", lambda *a: True)
    monkeypatch.setattr(pipeline.db, "insert_email_log",
                        lambda *a, **k: (calls.append((a, k)), ("e1", created))[1])
    monkeypatch.setattr(pipeline.trace, "emit", lambda *a, **k: traces.append((a, k)))

    def never(*a, **k):
        pytest.fail("Ignored mail must stop before drafting, retrieval, CRM or sending")

    monkeypatch.setattr(pipeline.agent, "draft_reply", never)
    monkeypatch.setattr(pipeline.kb, "retrieve", never)
    monkeypatch.setattr(pipeline.crm, "ingest_email_log_row", never)
    monkeypatch.setattr(pipeline.mailbox, "send_reply", never)
    monkeypatch.setattr(pipeline.events, "begin_drafting", never)
    email = {"from_address": ADDRESS, "from_name": "Holly Marsh", "subject": "A visit",
             "body": BODY, "thread_id": "thread-1"}
    with caplog.at_level("INFO", logger="app.pipeline"):
        assert asyncio.run(pipeline.draft_for_email(
            "t1", email, source_message_id="m1", mailbox_id="mb1")) is None
    assert len(calls) == 1
    args, kw = calls[0]
    assert args == ("t1", ADDRESS, "A visit", BODY, "")
    assert kw == dict(status="skipped", source_message_id="m1", thread_id="thread-1",
                      mailbox_id="mb1", from_name="Holly Marsh", channel="email",
                      category="fyi", category_reason="Sender is on your ignore list")
    assert len(traces) == int(created)  # deduplication result is respected
    assert "sender is on the ignore list" in caplog.text


def test_ignored_logging_failure_still_does_not_draft(monkeypatch, caplog):
    monkeypatch.setattr(pipeline, "_inbox_rules", lambda *a: {})
    monkeypatch.setattr(pipeline.db, "is_ignored", lambda *a: True)

    def fail(*a, **k):
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(pipeline.db, "insert_email_log", fail)
    assert asyncio.run(pipeline.draft_for_email("t1", {"from_address": ADDRESS, "body": BODY})) is None
    assert "failed to log ignored inbound" in caplog.text


def test_ignored_sender_page_opens_the_record_without_unignoring(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "list_ignored", lambda *a, **k: [
        {"address": ADDRESS, "reason": "", "created_at": "2026-09-27"}])
    html = client.get("/app/ignored").text
    assert 'href="/app/conversations/view?contact=holly%40example.test"' in html
    assert 'action="/app/ignored/remove"' in html


def test_first_live_hello_repaints_body_and_masthead(client, monkeypatch):
    """Execute live.js against the real server fragment, without a browser/network."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to exercise live.js")
    set_rows(monkeypatch, [row()])
    events.begin_drafting("t1", "m1", {"from_address": ADDRESS, "body": None})
    fragment = client.get("/app/conversations/view/fragment", params={"contact": ADDRESS}).text
    script = r"""
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const listeners = {}, timers = [];
let fetches = 0, initialised = 0;
const heading = {textContent: input.address, href: '/old'};
const thread = {
  dataset: {contact: input.address, fragmentUrl: '/fragment'}, innerHTML: '',
  contains: () => false,
  querySelector: () => ({dataset: {
    contactName: /data-contact-name="([^"]*)"/.exec(thread.innerHTML)[1],
    contactHref: /data-contact-href="([^"]*)"/.exec(thread.innerHTML)[1]
  }})
};
const doc = {
  activeElement: null, title: input.address,
  getElementById: id => ({'thread-live': thread, 'thread-msgs': {}, 'thread-typing': {}}[id] || null),
  querySelector: sel => sel === '[data-thread-contact-name]' ? heading : null
};
vm.runInNewContext(input.js, {
  document: doc, window: {blam8ThreadInit: () => initialised++},
  EventSource: function () {this.addEventListener = (name, fn) => listeners[name] = fn;},
  setTimeout: fn => {timers.push(fn); return timers.length;}, clearTimeout: () => {},
  fetch: async url => {assert.equal(url, '/fragment'); fetches++; return {ok: true, text: async () => input.fragment};}
});
(async () => {
  listeners.hello({data: JSON.stringify({drafting: [{key: 'm1', from_address: input.address, body: null}]})});
  timers.splice(0).forEach(fn => fn());
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(fetches, 1); // first hello must fetch, even on a fresh page
  assert.equal(thread.innerHTML, input.fragment);
  assert.equal(heading.textContent, 'Holly Marsh');
  assert.equal(heading.href, '/app/crm/open?email=holly%40example.test');
  assert.equal(doc.title, 'Holly Marsh · BLAM8');
  assert.equal(initialised, 1);
})().catch(err => {console.error(err); process.exitCode = 1;});
"""
    result = subprocess.run([node, "-e", script], input=json.dumps({
        "address": ADDRESS, "fragment": fragment,
        "js": (Path(__file__).resolve().parents[1] / "src/app/static/live.js").read_text(encoding="utf-8"),
    }), text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr


def open_env(monkeypatch):
    monkeypatch.setattr(dashboard, "_selected_mailbox_id", lambda *a, **k: "mb1")
    monkeypatch.setattr(dashboard.db, "get_email_log_by_message", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.trial, "gate_allows", lambda *a: True)


@pytest.mark.asyncio
async def test_click_to_draft_renders_provider_message_with_no_row(client, monkeypatch):
    open_env(monkeypatch)
    set_rows(monkeypatch, [])
    started, release = asyncio.Event(), asyncio.Event()
    body = BODY + " More details." * 80
    msg = {"from": {"address": ADDRESS, "name": "Holly Marsh"}, "subject": "A visit", "body": body}
    fetched, drafted = [], []

    async def get_message(*a, **k):
        fetched.append((a, k))
        return msg

    async def draft(*a, **k):
        drafted.append(k)
        started.set()
        await release.wait()
        return "e1"

    monkeypatch.setattr(dashboard.mailbox, "get_message", get_message)
    monkeypatch.setattr(pipeline, "draft_for_message", draft)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as browser:
        response = await browser.get("/app/conversations/open", params={
            "message_id": "m1", "contact": "wrong@example.test"})
        assert response.status_code == 303
        await asyncio.wait_for(started.wait(), 2)
        tasks = list(dashboard._OPEN_DRAFT_TASKS)
        try:
            html = (await browser.get(response.headers["location"])).text
            assert stage_body(html) == body
            assert ">Holly Marsh</a>" in html and "A visit" in html
            assert "Agent writing reply" in html and "None" not in html
            assert len(fetched) == 1 and drafted[0]["provider_message"] == msg
            # Reopening an in-flight provider message only reads its thread.
            again = await browser.get("/app/conversations/open", params={"message_id": "m1"})
            assert again.headers["location"] == response.headers["location"]
            assert len(fetched) == len(drafted) == 1
        finally:
            release.set()
            await asyncio.gather(*tasks)


@pytest.mark.parametrize("status", ["skipped", "pending", "sent"])
def test_clicking_a_handled_message_reads_it_without_starting_any_draft(client, monkeypatch, status):
    open_env(monkeypatch)
    monkeypatch.setattr(dashboard.db, "get_email_log_by_message",
                        lambda *a, **k: {"id": "e1", "status": status})
    monkeypatch.setattr(dashboard.db, "get_email_log_entry", lambda *a, **k: row(status=status))

    async def never(*a, **k):
        pytest.fail("Handled messages must not fetch or draft again")

    monkeypatch.setattr(pipeline, "draft_for_message", never)
    monkeypatch.setattr(dashboard.mailbox, "get_message", never)
    response = client.get("/app/conversations/open?message_id=m1&contact=wrong@example.test",
                          follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/app/conversations/view?contact=holly%40example.test"
    assert events.drafting_now("t1") == []


@pytest.mark.asyncio
async def test_simultaneous_clicks_during_provider_fetch_start_one_draft(client, monkeypatch):
    open_env(monkeypatch)
    fetching, release_fetch, release_draft = asyncio.Event(), asyncio.Event(), asyncio.Event()
    fetches, drafts = [], []

    async def get_message(*a, **k):
        fetches.append(1)
        if len(fetches) == 2:
            fetching.set()
        await release_fetch.wait()
        return {"from": {"address": ADDRESS, "name": "Holly Marsh"}, "subject": "A visit", "body": BODY}

    async def draft(*a, **k):
        drafts.append(1)
        await release_draft.wait()

    monkeypatch.setattr(dashboard.mailbox, "get_message", get_message)
    monkeypatch.setattr(pipeline, "draft_for_message", draft)
    ctx = TenantCtx(tenant_id="t1", client=None)
    clicks = [asyncio.create_task(dashboard.open_conversation("m1", contact=ADDRESS, ctx=ctx)) for _ in range(2)]
    try:
        await asyncio.wait_for(fetching.wait(), 2)
        release_fetch.set()
        responses = await asyncio.gather(*clicks)
        await asyncio.sleep(0)
        assert len(drafts) == 1 and all(r.status_code == 303 for r in responses)
    finally:
        release_fetch.set()
        release_draft.set()
        await asyncio.gather(*clicks, *list(dashboard._OPEN_DRAFT_TASKS))


def test_row_landing_during_provider_fetch_is_not_drafted_again(client, monkeypatch):
    open_env(monkeypatch)
    landed = []
    monkeypatch.setattr(dashboard.db, "get_email_log_by_message",
                        lambda *a, **k: {"id": "e1", "status": "skipped"} if landed else None)
    monkeypatch.setattr(dashboard.db, "get_email_log_entry", lambda *a, **k: row(status="skipped"))

    async def get_message(*a, **k):
        landed.append(True)
        return {"from": {"address": ADDRESS}, "body": BODY}

    async def never(*a, **k):
        pytest.fail("A row landed during the provider fetch")

    monkeypatch.setattr(dashboard.mailbox, "get_message", get_message)
    monkeypatch.setattr(pipeline, "draft_for_message", never)
    response = client.get("/app/conversations/open?message_id=m1&contact=" + ADDRESS, follow_redirects=False)
    assert response.status_code == 303 and events.drafting_now("t1") == []


def test_ignored_provider_messages_are_listed_as_one_skipped_thread(client, monkeypatch):
    logged = {"contact": ADDRESS, "state": "skipped", "subject": "A visit", "category": "fyi",
              "category_reason": "Sender is on your ignore list", "last_at": "2026-09-27T17:52:00+00:00"}
    monkeypatch.setattr(dashboard.db, "ignored_addresses", lambda *a, **k: {ADDRESS.upper()})
    monkeypatch.setattr(dashboard.db, "list_conversations", lambda *a, **k: [dict(logged)])
    monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: {})

    async def messages(*a, **k):
        return [{"id": mid, "from": {"address": ADDRESS.upper(), "name": "Holly Marsh"},
                 "subject": "A visit"} for mid in ("m1", "older-dropped-email")]

    monkeypatch.setattr(dashboard.mailbox, "list_messages", messages)
    snapshot = asyncio.run(inbox_labels.conversation_snapshot("t1"))
    assert len(snapshot) == 1 and snapshot[0]["state"] == "skipped"
    assert "new_message_id" not in snapshot[0]
    html = client.get("/app/conversations").text
    assert ">Skipped</span>" in html
    assert "/app/conversations/view?contact=holly%40example.test" in html
    assert "/app/conversations/open?message_id=" not in html


def test_prefetched_message_is_reused_by_the_pipeline(monkeypatch):
    msg = {"from": {"address": ADDRESS, "name": "Holly Marsh"}, "subject": "A visit", "body": BODY}
    seen = []
    monkeypatch.setattr(pipeline.db, "get_email_log_by_message", lambda *a, **k: None)

    async def never(*a, **k):
        pytest.fail("The provider message was already fetched")

    async def draft(tenant, email, **kw):
        seen.append(email)
        return "e1"

    async def noop(*a, **k):
        return []

    from app import photos
    monkeypatch.setattr(pipeline.mailbox, "get_message", never)
    monkeypatch.setattr(pipeline, "draft_for_email", draft)
    monkeypatch.setattr(pipeline, "keep_attachments", noop)
    monkeypatch.setattr(photos, "fetch_for_draft", noop)
    assert asyncio.run(pipeline.draft_for_message("t1", "m1", provider_message=msg)) == "e1"
    assert seen[0]["body"] == BODY and seen[0]["from_name"] == "Holly Marsh"
