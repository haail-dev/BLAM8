"""Knowledge via chat (Kris 2026-09-04): the assistant interviews the owner for
the facts their agent still can't answer and writes the notes itself.

Pins: the coverage read (eight kinds, keyword hint), the auto-ask text, the
prompt block, the `knowledge` recommend page and its apply route, the
onboarding knowledge step's `notes`, and the coverage strip on both pages.

Run: python -m pytest tests/test_knowledge_via_chat.py -q
"""
import pytest

from app import knowledge_coverage as kc
from app import settings_recommend
from tests.knowledge_review_fixture import review_store


def test_coverage_reads_the_pile_as_the_eight_kinds():
    cov = kc.coverage([
        {"source": "Prices", "text": "Full set of shoes £95; trim £35."},
        {"source": "Lameness rule", "text": "A lame horse sees the vet before the farrier. We do not book remedial work without the vet's notes."},
        {"source": "Coverage", "text": "We cover the Mendips: Wells, Frome. We don't cover Bath."},
    ])
    assert [k["key"] for k in cov["covered"]] == ["prices", "rules", "coverage"]
    assert "cadence" in [k["key"] for k in cov["missing"]] and cov["score"] == "3 of 8"
    assert cov["by_kind"]["prices"] == ["Prices"]
    empty = kc.coverage([])
    assert empty["covered"] == [] and len(empty["missing"]) == 8


def test_ask_text_asks_for_the_missing_kinds_three_at_a_time():
    cov = kc.coverage([{"source": "Prices", "text": "£95"}])
    ask = kc.ask_text(cov["missing"], "a farrier")
    assert ask.startswith("Interview me for the facts my agent still can't answer: rules and judgement, how a first booking works")
    assert "(a farrier)" in ask and "Then write the notes from what I said, and nothing I didn't." in ask
    assert "anything you won't do" in ask and "How and when do customers pay" not in ask   # only the first three
    assert "Every kind of fact the agent needs is covered" in kc.ask_text([])


def test_prompt_block_says_covered_or_not_and_forbids_invention():
    block = kc.prompt_block(kc.coverage([{"source": "Hours", "text": "Mon-Fri 8am to 5pm"}]))
    assert "- Hours and availability — when it may offer times, and when it says you're closed: covered (Hours)" in block
    assert "- Prices — quoted in the reply, never deferred to a colleague: NOT YET — ask:" in block
    assert "never invent a price, a rule or an hour" in block


def test_notes_parse_from_the_card_and_the_onboarding_reply():
    assert kc.parse_notes([{"name": "note", "value": "Prices | Full set £95; trim £35"},
                           {"name": "note", "value": "no separator"},
                           {"name": "stage", "value": "x | y"}]) == [{"label": "Prices", "content": "Full set £95; trim £35"}]
    assert kc.clean_notes([{"label": " Prices ", "content": "£95"}, {"label": "prices", "content": "dupe"},
                           {"label": "", "content": "x"}, "junk"]) == [{"label": "Prices", "content": "£95"}]


def test_the_recommend_catalogue_has_a_knowledge_page():
    assert settings_recommend.PAGES["knowledge"]["href"] == "/app/knowledge"
    vals = settings_recommend.clean("knowledge", [{"name": "note", "value": "Prices | Full set £95"}])
    assert vals == [{"name": "note", "value": "Prices | Full set £95"}]


def test_the_onboarding_knowledge_step_carries_notes_and_the_interview():
    from app import concierge
    schema = concierge.onboarding_schema("knowledge")
    assert "notes" in schema["properties"] and "notes" in schema["required"]
    assert schema["properties"]["notes"]["items"]["required"] == ["label", "content"]
    assert "notes" not in concierge.onboarding_schema("voice")["properties"]
    g = concierge.ONBOARDING_STEPS["knowledge"]["guidance"]
    assert "Only interview when the owner asks" in g and "write the notes yourself" in g
    p = concierge.build_onboarding_prompt("knowledge", {}, coverage=kc.coverage([]))
    assert "Knowledge coverage" in p and "NOT YET — ask:" in p


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    from app.auth import TenantCtx, require_tenant
    from app.main import app
    from app.security import require_csrf_header
    app.dependency_overrides[require_tenant] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf_header] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_knowledge_apply_stages_notes_for_explicit_review(client, monkeypatch, review_store):
    from app import concierge_routes as cr
    saved = []

    async def fake_ingest(tenant_id, source, text, client=None, mailbox_id=None):
        saved.append((source, text))
        return 1
    monkeypatch.setattr(cr.kb, "ingest", fake_ingest)
    monkeypatch.setattr(cr.kb, "is_configured", lambda: True)
    monkeypatch.setattr(cr.db, "log_agent_event", lambda *a, **k: None)
    r = client.post("/concierge/knowledge-apply", json={"values": [
        {"name": "note", "value": "Prices | Full set £95; trim £35"},
        {"name": "note", "value": "Coverage | Wells, Frome; not Bath"}]})
    assert r.status_code == 200, r.text
    assert saved == []
    assert [n["label"] for n in review_store.rows[-1]["payload"]["entries"]] == ["Prices", "Coverage"]
    assert "2 notes" in r.json()["summary"]
    r = client.post("/concierge/knowledge-apply", json={"values": [{"name": "stage", "value": "x"}]})
    assert r.status_code == 400
