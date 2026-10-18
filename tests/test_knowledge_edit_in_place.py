import json
"""Knowledge notes are editable in place (Kris 2026-09-04).

Correcting a fact on /app/knowledge meant Remove, then retype the whole note
from memory — which is what the plumbing run had to do to fix an out-of-hours
rate. A note now opens with its own text in the box and saves back over itself.

What this pins:
  - the text round-trips: chunks join back into what was written, including the
    overlap a hard-split leaves at the seam (or every save would duplicate a
    sentence)
  - GET /app/knowledge/note reads one note back, tenant-scoped, 404 when absent
  - POST /app/knowledge/edit re-ingests under the SAME name (kb.ingest replaces
    the source, so the old chunks never linger) and, on a rename, removes the
    old note rather than leaving the agent both
  - the list marks notes editable and imported documents not

Run: python -m pytest tests/test_knowledge_edit_in_place.py -q
"""
import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import db
from app import knowledge as kb
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf


# ── the text survives the round trip ─────────────────────────────────────────
def test_chunks_join_back_into_the_note():
    assert kb.join_chunks(["One para.", "Two para."]) == "One para.\n\nTwo para."
    assert kb.join_chunks([]) == ""
    assert kb.join_chunks(["Only one."]) == "Only one."


def test_a_hard_split_seam_is_not_duplicated():
    # One oversized paragraph is split with `overlap` characters repeated; an
    # edit-and-save cycle would grow the note every time if we kept them.
    long_para = "".join(f"sentence {i} words here. " for i in range(200))
    chunks = kb.chunk_text(long_para)
    assert len(chunks) > 1
    assert kb.join_chunks(chunks) == long_para.strip()


# ── the routes ───────────────────────────────────────────────────────────────
@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = \
        lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def store(monkeypatch):
    class Store:
        def __init__(self):
            self.chunks = {("Out-of-hours rates", None): ["£140 first hour."]}
            self.ingested, self.deleted = [], []

    st = Store()

    async def fake_ingest(tenant_id, source, text, client=None, mailbox_id=None, expected_version=None, original_source=None):
        assert expected_version == 'a' * 32
        if original_source and original_source != source:
            st.deleted.append(original_source)
            st.chunks.pop((original_source,mailbox_id))
        st.ingested.append((source, text, mailbox_id))
        st.chunks[(source, mailbox_id)] = [text]
        return 1

    def read(ctx, source=None, mailbox=None):
        return [{'source':name,'mailbox_id':scope,'version':'a'*32,'content':chunks,'chunks':len(chunks)}
                for (name,scope),chunks in st.chunks.items()
                if source is None or (name,scope)==(source,mailbox)]
    monkeypatch.setattr(dashboard.knowledge_forms,'read',read)
    monkeypatch.setattr(kb, "is_configured", lambda: True)
    monkeypatch.setattr(kb, "ingest", fake_ingest)
    monkeypatch.setattr(db, "knowledge_source_chunks",
                        lambda tid, source, client=None, mailbox_id=None:
                        st.chunks.get((source, mailbox_id), []))
    monkeypatch.setattr(db, "delete_knowledge_source",
                        lambda tid, source, client=None, mailbox_id=None:
                        st.deleted.append(source))
    return st


def test_a_note_reads_back_for_the_editor(client, store):
    r = client.get("/app/knowledge/note", params={"source": "Out-of-hours rates"})
    assert r.status_code == 200
    assert r.json()["content"] == "£140 first hour."
    assert r.json()["knowledge_versions"][0]["version"] == "a"*32
    assert client.get("/app/knowledge/note", params={"source": "Nope"}).status_code == 404


def test_saving_rewrites_the_same_note(client, store):
    r = client.post("/app/knowledge/edit", follow_redirects=False, data={
        "knowledge_versions": json.dumps(dashboard.knowledge_forms.read(None)),
        "source": "Out-of-hours rates", "label": "Out-of-hours rates",
        "content": "£140 first hour, then £65/hr."})
    assert r.status_code == 303 and "msg=Updated" in r.headers["location"]
    assert store.ingested == [("Out-of-hours rates", "£140 first hour, then £65/hr.", None)]
    assert store.deleted == []          # same name → replaced, nothing to remove


def test_renaming_moves_the_note_rather_than_cloning_it(client, store):
    r = client.post("/app/knowledge/edit", follow_redirects=False, data={
        "knowledge_versions": json.dumps(dashboard.knowledge_forms.read(None)),
        "source": "Out-of-hours rates", "label": "Call-out rates",
        "content": "£85 standard, £140 first hour out of hours."})
    assert r.status_code == 303
    assert store.ingested[0][0] == "Call-out rates"
    assert store.deleted == ["Out-of-hours rates"]


def test_a_note_that_has_gone_is_not_resurrected(client, store):
    r = client.post("/app/knowledge/edit", follow_redirects=False, data={
        "knowledge_versions": json.dumps(dashboard.knowledge_forms.read(None)),
        "source": "Deleted meanwhile", "label": "Deleted meanwhile", "content": "x"})
    assert r.status_code == 409
    assert store.ingested == []


def test_the_list_marks_notes_editable_and_documents_not(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(kb, "is_configured", lambda: True)
    monkeypatch.setattr(dashboard.knowledge_forms, "read", lambda ctx: [
        {"source": "Opening hours", "mailbox_id": None, "version":"a"*32, "chunks": 1,
         "created_at": "2026-09-04T10:00:00Z"},
        {"source": "price-list.pdf", "mailbox_id": None, "version":"a"*32,
         "chunks": kb.EDITABLE_MAX_CHUNKS + 1, "created_at": "2026-09-04T10:00:00Z"},
    ])
    html = client.get("/app/knowledge").text
    assert 'data-source="Opening hours"' in html
    assert 'data-source="price-list.pdf"' not in html
