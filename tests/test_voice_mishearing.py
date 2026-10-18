"""Voice mishearings (Kris 2026-09-23: "psychosis on my mind", "I need no
forgiveness" — both spoken while the concierge was still talking):

  1. the STT session carries a hint — the business and the names it will
     hear — and a refused hint never costs the session;
  2. a turn that began while the mic was gated for playback goes up flagged
     `clipped`, and the model sees a note (the persisted question does not);
  3. the voice prompt says: a message that makes no sense, or is marked cut
     off and isn't clearly complete → no lookups, ask them to say it again.
"""
import asyncio
import json
import pathlib
import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import concierge, llm
from app.auth import TenantCtx, require_onboarded
from app.config import settings
from app.main import app
from app.security import require_csrf_header

VOICE_JS = (pathlib.Path(__file__).parent.parent / "src/app" / "static"
            / "voice-live.js").read_text(encoding="utf-8")


# ── 1. the transcription hint ────────────────────────────────────────────────
class _Q:
    def __init__(self, rows):
        self.rows = rows

    def __getattr__(self, _name):
        return lambda *a, **k: self

    def execute(self):
        return SimpleNamespace(data=self.rows)


class _Db:
    def __init__(self, rows):
        self.rows = rows

    def table(self, name):
        assert name == "crm_contacts"
        return _Q(self.rows)


@pytest.fixture
def hint_db(monkeypatch):
    from app import concierge_routes
    monkeypatch.setattr(concierge_routes.db, "get_tenant",
                        lambda tid, client=None: {"name": "Little Dogs", "industry": "dog_grooming"})
    monkeypatch.setattr(concierge_routes.db, "list_requirement_instances_all",
                        lambda tid, limit=1000, client=None: [
                            {"name": "Mabel", "group_label": "Dog", "group_label_plural": "Dogs"},
                            {"name": "Bruno", "group_label": "Dog", "group_label_plural": "Dogs"}])
    fake = _Db([{"name": "Dan Price"}, {"name": "dan price"}, {"name": "jess@example.com"},
                {"name": "Jess Hart"}, {"name": None}])
    return SimpleNamespace(ctx=TenantCtx(tenant_id="t1", client=fake, user_id="u1"))


def test_hint_names_the_business_and_the_people(hint_db):
    from app import concierge_routes
    hint = concierge_routes._transcription_hint(hint_db.ctx)
    assert "(dog grooming)" in hint and "at Little Dogs" in hint
    assert "They keep records of: dogs." in hint
    assert "Names that come up: Dan Price, Jess Hart, Mabel, Bruno." in hint
    assert "@" not in hint                 # an email-as-name is never a hint
    assert len(hint) <= 1200


def test_hint_survives_every_read_failing(monkeypatch):
    from app import concierge_routes

    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(concierge_routes.db, "get_tenant", boom)
    monkeypatch.setattr(concierge_routes.db, "list_requirement_instances_all", boom)
    monkeypatch.setattr(concierge_routes.db, "db", boom)
    hint = concierge_routes._transcription_hint(TenantCtx(tenant_id="t1", client=None, user_id="u1"))
    assert hint.startswith("A UK small-business owner talking to their assistant")


class _Http:
    sent: list = []
    refuse_prompt = False

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        _Http.sent.append(json)
        cfg = json["session"]["audio"]["input"]["transcription"]
        if _Http.refuse_prompt and "prompt" in cfg:
            return SimpleNamespace(status_code=400, text='{"error":"unknown prompt"}', json=lambda: {})
        return SimpleNamespace(status_code=200, text="{}",
                               json=lambda: {"value": "ek", "expires_at": 1})


@pytest.fixture
def client(monkeypatch, hint_db):
    import httpx
    from app import concierge_routes
    monkeypatch.setattr(concierge_routes.settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(concierge_routes.db, "concierge_live_rollout", lambda: False)
    monkeypatch.setattr(httpx, "AsyncClient", _Http)
    _Http.sent = []
    _Http.refuse_prompt = False
    # No membership row = the account's own owner (lookups fail CLOSED).
    monkeypatch.setattr(concierge_routes.db, "get_member", lambda *a, **k: None)
    app.dependency_overrides[require_onboarded] = lambda: hint_db.ctx
    app.dependency_overrides[require_csrf_header] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_turn_session_carries_hint_and_standard_captions_remain_available(client):
    assert client.post("/concierge/voice/token", json={"silence_ms": 500}).status_code == 200
    t = _Http.sent[-1]["session"]["audio"]["input"]["transcription"]
    assert t["model"] == "gpt-4o-transcribe" and "Dan Price" in t["prompt"]
    sent_count = len(_Http.sent)
    response = client.post("/concierge/voice/token", json={"purpose": "captions"})
    assert response.status_code == 200
    assert len(_Http.sent) == sent_count + 1


def test_a_refused_hint_retries_without_it(client):
    _Http.refuse_prompt = True
    res = client.post("/concierge/voice/token", json={"silence_ms": 500})
    assert res.status_code == 200 and res.json()["value"] == "ek"
    assert len(_Http.sent) == 2
    assert "prompt" not in _Http.sent[1]["session"]["audio"]["input"]["transcription"]


# ── 2. clipped turns ─────────────────────────────────────────────────────────
def test_client_flags_speech_that_began_under_the_gate():
    assert "const CLIP_WINDOW_MS = 700;" in VOICE_JS
    assert "gateOpenedAt = Date.now();" in VOICE_JS
    assert "clipped: gateOpenedAt > 0 && now - gateOpenedAt < CLIP_WINDOW_MS," in VOICE_JS
    # only the FIRST segment of an utterance decides — a later one merged in
    # is a continuation, not a cut-off opening
    turns = (pathlib.Path(__file__).parent.parent / "src/app" / "static" / "voice-turns.js").read_text(encoding="utf-8")
    assert "clipped: Boolean(parts[0] && parts[0].clipped)," in turns
    assert "const meta = { durationMs: u.durationMs, clipped: u.clipped };" in VOICE_JS
    assert "if (meta.clipped) body.clipped = true;" in VOICE_JS


def test_clipped_turn_reaches_the_model_with_a_note_and_persists_clean(monkeypatch):
    from app import concierge_routes
    saved, seen = [], {}
    monkeypatch.setattr(concierge_routes.db, "visible_modules", lambda tid, client=None: {"crm"})
    monkeypatch.setattr(concierge_routes.db, "get_tenant", lambda tid, client=None: {"name": "K"})
    monkeypatch.setattr(concierge_routes.db, "get_tenant_config", lambda tid, client=None: {})
    monkeypatch.setattr(concierge_routes.db, "add_concierge_message",
                        lambda tid, role, content, **kw: saved.append((role, content)))
    # The asker is the account's own owner (no membership row). Without this the
    # read hits the blocked db and — failing closed since the 2026-09 security
    # review — reads as a switched-off member.
    monkeypatch.setattr(concierge_routes.db, "get_member", lambda tid, uid: None)
    monkeypatch.setattr(concierge_routes.usage, "record_anthropic", lambda *a, **k: None)
    monkeypatch.setattr(concierge_routes.fairuse, "consume", lambda tid, tenant: (True, None))

    async def fake_answer(question, history, **kwargs):
        seen["note"] = kwargs.get("question_note")
        return concierge.BusinessReply(reply="Sorry, I missed the start — say that again?",
                                       links=[], action=None, model="t",
                                       usage={"input_tokens": 1, "output_tokens": 1,
                                              "cache_read_input_tokens": 0,
                                              "cache_creation_input_tokens": 0})
    monkeypatch.setattr(concierge_routes.concierge, "answer_business", fake_answer)
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None, user_id="u1")
    app.dependency_overrides[require_csrf_header] = lambda: None
    try:
        c = TestClient(app)
        c.post("/concierge/voice/chat", json={"question": "I need no forgiveness", "clipped": True})
        assert seen["note"] == concierge.CLIPPED_NOTE
        c.post("/concierge/voice/chat", json={"question": "How much am I owed?"})
        assert seen["note"] is None
    finally:
        app.dependency_overrides.clear()
    assert ("user", "I need no forgiveness") in saved     # no note in the record


def test_answer_business_puts_the_note_on_the_model_message_only(monkeypatch):
    monkeypatch.setattr(llm.db, "insert_llm_call", lambda row: None)
    monkeypatch.setattr(settings, "llm_prompt_cache", False)
    monkeypatch.setattr(concierge, "client", lambda: object())
    sent = []

    async def fake_create(**kwargs):
        sent.append(kwargs["messages"][-1]["content"])
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(
            {"reply": "Sorry, say that again?", "links": [], "action": {"kind": ""}}))],
            stop_reason="end_turn", usage=SimpleNamespace(), model="t")
    monkeypatch.setattr(concierge.llm, "create", fake_create)

    async def ex(name, args):
        return {}
    out = asyncio.run(concierge.answer_business(
        "psychosis on my mind", [], tools=[{"name": "t", "input_schema": {}}],
        tool_executor=ex, tenant_id="t1", voice_mode=True,
        question_note=concierge.CLIPPED_NOTE))
    assert sent[0] == "psychosis on my mind\n\n" + concierge.CLIPPED_NOTE
    assert out.reply == "Sorry, say that again?"


# ── 3. the prompt ────────────────────────────────────────────────────────────
def test_voice_prompt_asks_rather_than_acts_on_a_mishearing():
    v = concierge.build_business_prompt(voice_mode=True)
    assert "speech-to-text, which mishears" in v
    assert "run NO lookups and propose nothing" in v
    assert "A marked message that is complete and clear is answered normally." in v
    assert "speech-to-text, which mishears" not in concierge.build_business_prompt()


def test_prompt_drops_an_untaken_offer_when_the_subject_moves():
    """Live 2026-09-23: 'email Dan about his dog's vaccination' also pulled the
    overdue-invoice list (the offer to chase Meg from the turn before).
    Replayed against the real model: 2/4 and 3/4 before, 0/8 and 0/8 after."""
    for p in (concierge.build_business_prompt(), concierge.build_business_prompt(voice_mode=True)):
        assert "12. ONLY WHAT THIS MESSAGE NEEDS" in p
        assert "never re-run its lookups" in p


def test_search_contacts_finds_an_owner_by_dependant_name(monkeypatch):
    """Live 2026-09-23 (iOS): "When does Mabel's vaccination run out?" →
    "I couldn't find a contact called Mabel" — the search matched people only."""
    from app import concierge_data
    monkeypatch.setattr(concierge_data.db, "crm_contacts_by_known_name", lambda *a, **k: [])
    monkeypatch.setattr(concierge_data.db, "crm_list_contacts", lambda *a, **k: [])
    monkeypatch.setattr(concierge_data.db, "crm_contacts_by_dependant_name",
                        lambda tid, q, limit=10, client=None: [
                            {"id": "c1", "name": "Daniel", "email": "d@x.com",
                             "matched_dependant": {"kind": "Dog", "name": "Mabel"}}])
    out = concierge_data._search_contacts("t1", None, {"query": "Mabel"})
    assert out["count"] == 1
    assert out["contacts"][0]["name"] == "Daniel"
    assert out["contacts"][0]["matched_dependant"] == {"kind": "Dog", "name": "Mabel"}


def test_search_contacts_matches_a_full_name_across_name_and_email(monkeypatch):
    """Live 2026-09-23 (iOS): "when is Laura Finch's meet and greet?" → "I can't
    find anyone called Laura Finch" — she is saved as name "Laura", email
    laura.finch@…; each word of the name may match the name OR the email."""
    from app import concierge_data
    monkeypatch.setattr(concierge_data.db, "crm_contacts_by_known_name", lambda *a, **k: [])
    book = [{"id": "1", "name": "Laura", "email": "b+laura.finch@x.com"},
            {"id": "2", "name": "Laura Price", "email": "lp@x.com"}]

    def fake_list(tid, q=None, limit=100, client=None, **k):
        q = (q or "").lower()
        return [r for r in book if q in r["name"].lower() or q in r["email"].lower()]
    monkeypatch.setattr(concierge_data.db, "crm_list_contacts", fake_list)
    monkeypatch.setattr(concierge_data.db, "crm_contacts_by_dependant_name", lambda *a, **k: [])
    out = concierge_data._search_contacts("t1", None, {"query": "Laura Finch"})
    assert [c["contact_id"] for c in out["contacts"]] == ["1"]
    assert concierge_data._search_contacts("t1", None, {"query": "Zebra Finch"})["count"] == 0
