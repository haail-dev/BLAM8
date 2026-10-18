"""Onboarding knowledge step — the reviewer's "knowledge not saved" flag (293a210).

The wizard's Continue used to be a bare link, so a typed-but-not-Added note was
silently discarded on the way out: prod showed ZERO knowledge_chunks for the
reviewer's tenant. The fix routes Continue through the note form (then=continue)
and re-renders errors with the note intact. These pin that contract end-to-end:
type note -> Continue preserves it -> Review note stages it -> final admission
is separate from both actions.

Run: python -m pytest tests/test_onboarding_knowledge.py -q
"""
import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import db
from app import knowledge as kb
from app.auth import TenantCtx, require_onboarded, require_tenant
from app.main import app
from app.security import require_csrf

from tests.knowledge_review_fixture import review_store

NOTE = {"label": "Opening hours and pricing",   # no '&': assertions read raw HTML
        "content": "Standard pallet to mainland UK is £45 + VAT, 2–3 working days."}


@pytest.fixture
def client():
    ctx = lambda: TenantCtx(tenant_id="t1", client=None)  # noqa: E731
    app.dependency_overrides[require_tenant] = ctx        # /onboarding/*
    app.dependency_overrides[require_onboarded] = ctx     # /app/knowledge
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)   # no `with`: startup events must not run
    app.dependency_overrides.clear()


@pytest.fixture
def store(monkeypatch, review_store):
    """An in-memory knowledge_chunks stand-in: ingest writes it, both listing
    surfaces (wizard + /app/knowledge) read it — so these tests catch a split
    between the write path and either read path, not just the write."""
    sources: list[dict] = []

    async def fake_ingest(tenant_id, source, text, client=None, mailbox_id=None):
        assert tenant_id == "t1"
        sources.append({"source": source, "content": text, "mailbox_id": mailbox_id,
                        "chunks": 1, "created_at": "2026-07-25T12:00:00+00:00"})
        return 1

    monkeypatch.setattr(kb, "ingest", fake_ingest)
    monkeypatch.setattr(kb, "is_configured", lambda: True)
    monkeypatch.setattr(db, "list_knowledge_sources", lambda tid, client=None: list(sources))
    # the step reads the website given at the interview (2026-09-13)
    monkeypatch.setattr(db, "get_tenant_config", lambda tid, client=None: {})
    monkeypatch.setattr(dashboard.knowledge_forms,"read",lambda ctx: [{**s,"version":"a"*32} for s in sources])
    return sources


def _app_page_env(monkeypatch):
    """The minimal _ctx collaborators for rendering /app/knowledge."""
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)


# ── Continue preserves typed notes without silently staging them ────────────

# Knowledge is the wizard's LAST step since 2026-08-02 (the live-data step is
# off-flow). Typed notes require Review note; empty Continue can finish only
# after an explicit decision about any pending drafts.
def test_continue_with_typed_note_requires_review_before_finishing(client, store, monkeypatch, review_store):
    done = []
    monkeypatch.setattr(db, "set_onboarded", lambda tid, client=None: done.append(tid))
    # finishing also stamps the go-live hold (2026-09-01) — stub it so these
    # tests exercise the real path rather than passing on its best-effort catch
    monkeypatch.setattr(db, "upsert_tenant_config", lambda *a, **k: None)
    r = client.post("/onboarding/knowledge/text", data={**NOTE, "then": "continue"},
                    follow_redirects=False)
    assert r.status_code == 200 and 'Your pasted note is unsaved' in r.text
    assert NOTE['content'] in r.text
    assert done == [] and store == []
    assert review_store.rows==[]


def test_finish_with_an_empty_box_just_completes(client, store, monkeypatch):
    done = []
    monkeypatch.setattr(db, "set_onboarded", lambda tid, client=None: done.append(tid))
    # finishing also stamps the go-live hold (2026-09-01) — stub it so these
    # tests exercise the real path rather than passing on its best-effort catch
    monkeypatch.setattr(db, "upsert_tenant_config", lambda *a, **k: None)
    r = client.post("/onboarding/knowledge/text",
                    data={"label": "", "content": "   ", "then": "continue"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/app/"
    assert done == ["t1"]
    assert store == []


def test_add_note_stages_without_admitting_facts(client, store, review_store):
    r = client.post("/onboarding/knowledge/text", data=NOTE, follow_redirects=False)
    assert r.status_code == 303
    assert "/knowledge/review/" in r.headers["location"]
    assert store == []
    assert review_store.rows[-1]["payload"]["entries"][0]["label"] == NOTE["label"]


def test_unlabelled_notes_get_unique_names_never_overwrite(client, store, review_store):
    # ingest replaces a source wholesale, so two notes both called "Pasted note"
    # would silently overwrite each other — the fallback label is dated-unique.
    client.post("/onboarding/knowledge/text", data={"label": "", "content": "First fact."})
    assert review_store.rows[-1]["payload"]["entries"][0]["label"] != "Pasted note"
    assert store == []


# ── an error must never eat typed work ───────────────────────────────────────

def test_pending_note_survives_reload_before_embedding(client, store, monkeypatch, review_store):
    async def boom(*a, **k): raise RuntimeError("Embedding unavailable")
    monkeypatch.setattr(kb,"ingest",boom)
    r=client.post("/onboarding/knowledge/text",data=NOTE)
    assert r.status_code==200 and NOTE["label"] in r.text and NOTE["content"] in r.text
    assert store==[] and len(review_store.rows)==1


def test_unconfigured_kb_keeps_the_note_visible(client, store, monkeypatch):
    monkeypatch.setattr(kb, "is_configured", lambda: False)
    r = client.post("/onboarding/knowledge/text", data=NOTE)
    assert r.status_code == 200
    assert "VOYAGE_API_KEY" in r.text
    assert NOTE["content"] in r.text


# ── the note the wizard saved is what /app/knowledge shows ───────────────────

def test_pending_note_is_visible_in_review_and_not_app_saved_sources(client, store, monkeypatch, review_store):
    _app_page_env(monkeypatch)
    r=client.post("/onboarding/knowledge/text",data=NOTE)
    assert r.status_code==200 and NOTE["label"] in r.text
    r=client.get("/app/knowledge")
    assert r.status_code==200 and "No sources yet" in r.text
    assert store==[]


def test_app_knowledge_empty_state_without_sources(client, store, monkeypatch):
    _app_page_env(monkeypatch)
    r = client.get("/app/knowledge")
    assert r.status_code == 200
    assert "No sources yet" in r.text
