"""Support tickets — the chatbot's collect-and-route flow (src/app/support.py, the
ask-mode ticket draft/lookup in src/app/concierge.py, and the pre-auth write path
POST /concierge/ticket + status attachment in src/app/concierge_routes.py).

Pins:
  * secret redaction scrubs passwords/tokens/JWTs/card numbers but never mangles
    ordinary prose ("I forgot my password" stays intact);
  * the ask-mode schema/prompt carry the ticket + lookup contract, and the
    instructions stay product-agnostic (no product name, no prices);
  * the draft/lookup sanitizers only surface complete, plausible payloads;
  * create_ticket maps category → kind/severity, redacts before storing, folds
    duplicates into the existing reference, and enforces the per-email cap;
  * lookup_ticket reports found / not-found / unreachable HONESTLY — the widget
    renders this server truth, so a status can never be invented;
  * the route validates the email, 429s past the cap, 503s (not fabricates) when
    storage is down, and attaches lookup results to /ask replies.

Run: python -m pytest tests/test_support_tickets.py -q
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

import app.concierge_routes as routes
from app import concierge, support
from app.concierge import _parse, _sanitize_lookup, _sanitize_ticket, build_system_prompt
from app.main import app
from app.support import redact


# ── secret redaction ─────────────────────────────────────────────────────────
def test_redact_scrubs_keyword_value_pairs():
    for text in ("my password is hunter2", "password: hunter2", "pwd=hunter2",
                 "the OTP was 482913", "client secret: abc123xyz"):
        out, hit = redact(text)
        assert hit and "hunter2" not in out and "482913" not in out and "abc123xyz" not in out
        assert "[removed]" in out


def test_redact_leaves_prose_alone():
    for text in ("I forgot my password and can't reset it",
                 "the token expired so sign-in fails",
                 "Error 500 when connecting my mailbox at 9am",
                 "call me on 07911 123456"):
        out, hit = redact(text)
        assert out == text and hit is False


def test_redact_scrubs_bearer_jwt_url_and_card():
    out, hit = redact("Authorization: Bearer abc123def456ghi")
    assert hit and "abc123def456ghi" not in out
    out, hit = redact("it returned eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dBjftJeZ4CVP")
    assert hit and "eyJhbGci" not in out
    out, hit = redact("stuck at /callback?code=4slkdjf98234&state=x")
    assert hit and "4slkdjf98234" not in out and "state=x" in out
    out, hit = redact("I paid with 4111 1111 1111 1111 yesterday")
    assert hit and "4111" not in out


def test_redact_handles_empty():
    assert redact(None) == ("", False)
    assert redact("") == ("", False)


# ── ask-mode contract (schema + prompt + sanitizers) ─────────────────────────
def test_schema_carries_ticket_and_lookup():
    props = concierge.CONCIERGE_SCHEMA["properties"]
    assert set(concierge.CONCIERGE_SCHEMA["required"]) == \
        {"reply", "offer_signup", "suggested_industry", "ticket", "lookup"}
    assert tuple(props["ticket"]["properties"]["category"]["enum"]) == concierge.TICKET_CATEGORIES
    assert tuple(props["ticket"]["properties"]["account_type"]["enum"]) == concierge.TICKET_ACCOUNT_TYPES
    assert set(props["lookup"]["properties"]) == {"reference", "email"}


def test_ticket_categories_track_the_filing_layer():
    # "" is the schema's nothing-yet value; every real slug must be file-able.
    assert set(concierge.TICKET_CATEGORIES) - {""} == set(support.CATEGORIES)
    assert set(concierge.TICKET_ACCOUNT_TYPES) == set(support.ACCOUNT_TYPES)


def test_prompt_has_ticket_rules_and_stays_agnostic():
    p = build_system_prompt()
    assert "SUPPORT TICKETS" in p and "TICKET STATUS" in p
    assert "ONE question at a time" in p
    assert "NOTHING is sent" in p                      # the card is the only write path
    assert "never ask for a password" in p.lower() or "never ask for a password" in p
    assert "NEVER state, guess, or remember a ticket's status" in p
    assert "BLAM8" not in p and "£" not in p           # instructions stay product-agnostic


def test_facts_sheet_names_the_ticket_capability():
    from app import platform_facts
    s = platform_facts.sheet()
    assert "support ticket" in s.lower() and "BLAM8-1042" in s


def test_sanitize_ticket_complete_draft_passes():
    out = _sanitize_ticket({"summary": " Microsoft sign-in fails ", "description": "",
                            "error_message": "AADSTS900561", "email": "kim@firm.co.uk",
                            "category": "login_auth", "account_type": "microsoft"})
    assert out["summary"] == "Microsoft sign-in fails"
    assert out["description"] == "Microsoft sign-in fails"     # empty ← summary
    assert out["category"] == "login_auth" and out["account_type"] == "microsoft"


def test_sanitize_ticket_incomplete_or_bad():
    base = {"summary": "x", "description": "", "error_message": "", "email": "kim@firm.co.uk",
            "category": "login_auth", "account_type": ""}
    assert _sanitize_ticket(None) is None
    assert _sanitize_ticket({**base, "summary": ""}) is None            # no summary → no card
    assert _sanitize_ticket({**base, "email": "not-an-email"}) is None  # implausible email
    assert _sanitize_ticket({**base, "category": "weird"})["category"] == "other"
    assert _sanitize_ticket({**base, "account_type": "aol"})["account_type"] == ""


def test_sanitize_lookup():
    out = _sanitize_lookup({"reference": " blam8-1042. ", "email": "kim@firm.co.uk"})
    assert out == {"reference": "BLAM8-1042", "email": "kim@firm.co.uk"}
    assert _sanitize_lookup({"reference": "", "email": "kim@firm.co.uk"}) is None
    assert _sanitize_lookup({"reference": "BLAM8-1", "email": "nope"}) is None
    assert _sanitize_lookup("junk") is None


def test_parse_carries_ticket_and_lookup():
    payload = ('{"reply": "Ready to review.", "offer_signup": false, '
               '"suggested_industry": "", '
               '"ticket": {"summary": "Sign-in fails", "description": "d", '
               '"error_message": "", "email": "a@b.co", "category": "login_auth", '
               '"account_type": "google"}, '
               '"lookup": {"reference": "", "email": ""}}')
    reply, _, _, ticket, lookup = _parse(payload)
    assert ticket["summary"] == "Sign-in fails" and lookup is None
    payload2 = ('{"reply": "Checking now.", "offer_signup": false, '
                '"suggested_industry": "", '
                '"ticket": {"summary": "", "description": "", "error_message": "", '
                '"email": "", "category": "", "account_type": ""}, '
                '"lookup": {"reference": "blam8-1042", "email": "a@b.co"}}')
    _, _, _, ticket2, lookup2 = _parse(payload2)
    assert ticket2 is None and lookup2 == {"reference": "BLAM8-1042", "email": "a@b.co"}


# ── create_ticket semantics (db monkeypatched) ───────────────────────────────
class TicketEnv:
    def __init__(self, monkeypatch, recent=0, duplicate=None):
        self.filed = []
        monkeypatch.setattr(support.db, "count_recent_tickets_by_email", lambda *a, **k: recent)
        monkeypatch.setattr(support.db, "find_recent_ticket_duplicate", lambda *a, **k: duplicate)
        monkeypatch.setattr(support.db, "add_feature_request",
                            lambda tid, body, **kw: (self.filed.append({"tid": tid, "body": body, **kw})
                                                     or "BLAM8-2001"))
        # notification off (no mailbox configured) — covered separately
        monkeypatch.setattr(support.settings, "platform_notify_mailbox", "")


def test_create_ticket_files_and_maps_fields(monkeypatch):
    env = TicketEnv(monkeypatch)
    out = asyncio.run(support.create_ticket(
        email="kim@firm.co.uk", category="login_auth", summary="Microsoft sign-in fails",
        description="Tried to log in at 9am, bounced back with an error.",
        error_message="AADSTS900561", account_type="microsoft",
        page_url="https://blam8.com/login", user_agent="UA"))
    assert out == {"ok": True, "reference": "BLAM8-2001", "deduped": False, "redacted": False}
    row = env.filed[0]
    assert row["tid"] is None and row["kind"] == "bug" and row["severity"] == "high"
    assert row["status"] == "open" and row["category"] == "login_auth"
    assert row["user_email"] == "kim@firm.co.uk" and row["submitted_by"] == "website chat"
    assert row["body"].startswith("Microsoft sign-in fails\n\n")


def test_create_ticket_redacts_before_storing(monkeypatch):
    env = TicketEnv(monkeypatch)
    out = asyncio.run(support.create_ticket(
        email="kim@firm.co.uk", category="bug", summary="Can't connect",
        description="my password is hunter2 and it still fails"))
    assert out["redacted"] is True
    assert "hunter2" not in env.filed[0]["body"]


def test_create_ticket_feature_category_maps_to_feature_kind(monkeypatch):
    env = TicketEnv(monkeypatch)
    asyncio.run(support.create_ticket(email="a@b.co", category="feature_request",
                                      summary="Please add a dark mode"))
    assert env.filed[0]["kind"] == "feature" and env.filed[0]["severity"] == "normal"


def test_create_ticket_folds_duplicates(monkeypatch):
    env = TicketEnv(monkeypatch, duplicate="BLAM8-1999")
    out = asyncio.run(support.create_ticket(email="a@b.co", category="bug", summary="Same issue"))
    assert out["reference"] == "BLAM8-1999" and out["deduped"] is True
    assert not env.filed                                   # nothing filed twice


def test_create_ticket_enforces_email_cap(monkeypatch):
    TicketEnv(monkeypatch, recent=support.MAX_TICKETS_PER_EMAIL_PER_HOUR)
    with pytest.raises(support.TicketRateLimited):
        asyncio.run(support.create_ticket(email="a@b.co", category="bug", summary="x"))


def test_create_ticket_notifies_when_configured(monkeypatch):
    env = TicketEnv(monkeypatch)
    sent = []
    monkeypatch.setattr(support.settings, "platform_notify_mailbox", "admin.blam8@gmail.com")
    monkeypatch.setattr(support.db, "get_connection_by_address",
                        lambda addr: {"tenant_id": "t-admin", "id": "mb1"})

    async def fake_send(tenant_id, to, subject, body, mailbox_id=None):
        sent.append({"tenant_id": tenant_id, "to": to, "subject": subject, "body": body})

    import app.mailbox as mailbox
    monkeypatch.setattr(mailbox, "send_mail", fake_send)
    asyncio.run(support.create_ticket(email="kim@firm.co.uk", category="login_auth",
                                      summary="Sign-in fails", error_message="AADSTS1"))
    assert sent and sent[0]["to"] == support.settings.access_request_notify_to
    assert "[BLAM8-2001]" in sent[0]["subject"] and "login_auth" in sent[0]["subject"]
    assert "kim@firm.co.uk" in sent[0]["body"] and "AADSTS1" in sent[0]["body"]


def test_notify_failure_never_loses_the_ticket(monkeypatch):
    env = TicketEnv(monkeypatch)
    monkeypatch.setattr(support.settings, "platform_notify_mailbox", "admin.blam8@gmail.com")
    monkeypatch.setattr(support.db, "get_connection_by_address",
                        lambda addr: (_ for _ in ()).throw(RuntimeError("boom")))
    out = asyncio.run(support.create_ticket(email="a@b.co", category="bug", summary="x"))
    assert out["ok"] is True and env.filed                 # filed despite the mail hiccup


# ── operator push (so /admin/requests never has to be polled) ────────────────
class _PushEnv:
    """Arms the push path and captures what would be delivered."""
    def __init__(self, monkeypatch, enabled=True):
        import app.push as push
        self.pushed = []
        monkeypatch.setattr(support.settings, "platform_notify_mailbox", "admin.blam8@gmail.com")
        monkeypatch.setattr(support.db, "get_connection_by_address",
                            lambda addr: {"tenant_id": "t-admin", "id": "mb1"})
        monkeypatch.setattr(push, "push_enabled", lambda: enabled)
        monkeypatch.setattr(push, "notify",
                            lambda tid, **kw: self.pushed.append({"tid": tid, **kw}))
        # the email leg is covered above — keep it out of the way here
        import app.mailbox as mailbox

        async def _noop(*a, **k):
            return None
        monkeypatch.setattr(mailbox, "send_mail", _noop)


def test_create_ticket_pushes_the_operator(monkeypatch):
    TicketEnv(monkeypatch)
    env = _PushEnv(monkeypatch)
    asyncio.run(support.create_ticket(email="kim@firm.co.uk", category="login_auth",
                                      summary="Microsoft sign-in fails"))
    assert len(env.pushed) == 1
    p = env.pushed[0]
    # routed to the tenant that owns the platform mailbox — push subs are per-tenant
    assert p["tid"] == "t-admin"
    assert "BLAM8-2001" in p["title"]
    assert p["url"] == "/admin/requests"
    assert p["tag"] == "ticket-BLAM8-2001"      # redelivery replaces, never stacks
    assert p["kind"] == "support_ticket"
    assert "HIGH" in p["body"]                  # login_auth is auto-high, say so


def test_operator_push_omits_the_reporters_address(monkeypatch):
    # A push payload travels through a third-party push service; the reference is
    # enough to pull the full ticket, so PII must not ride along.
    TicketEnv(monkeypatch)
    env = _PushEnv(monkeypatch)
    asyncio.run(support.create_ticket(email="kim@firm.co.uk", category="bug",
                                      summary="Broken", error_message="AADSTS1"))
    blob = " ".join(str(v) for v in env.pushed[0].values())
    assert "kim@firm.co.uk" not in blob and "AADSTS1" not in blob


def test_operator_push_trims_a_long_summary(monkeypatch):
    TicketEnv(monkeypatch)
    env = _PushEnv(monkeypatch)
    asyncio.run(support.create_ticket(email="a@b.co", category="bug",
                                      summary="word " * 80))
    assert len(env.pushed[0]["body"]) <= 90 and env.pushed[0]["body"].endswith("…")


def test_no_push_when_vapid_is_unconfigured(monkeypatch):
    TicketEnv(monkeypatch)
    env = _PushEnv(monkeypatch, enabled=False)
    asyncio.run(support.create_ticket(email="a@b.co", category="bug", summary="x"))
    assert env.pushed == []


def test_push_failure_never_loses_the_ticket(monkeypatch):
    import app.push as push
    tenv = TicketEnv(monkeypatch)
    _PushEnv(monkeypatch)
    monkeypatch.setattr(push, "notify",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = asyncio.run(support.create_ticket(email="a@b.co", category="bug", summary="x"))
    assert out["ok"] is True and tenv.filed


# ── lookup honesty ───────────────────────────────────────────────────────────
def test_lookup_found_not_found_unreachable(monkeypatch):
    row = {"reference": "BLAM8-2001", "status": "in_progress", "status_note": "Fix rolling out",
           "category": "login_auth", "kind": "bug", "severity": "high",
           "created_at": "2026-07-25T09:00:00+00:00", "user_email": "a@b.co",
           "body": "Sign-in fails\n\nlong detail"}
    monkeypatch.setattr(support.db, "get_ticket_by_reference", lambda ref, email: row)
    out = support.lookup_ticket("BLAM8-2001", "a@b.co")
    assert out["found"] and out["status_label"] == "In progress"
    assert out["status_note"] == "Fix rolling out"
    assert out["summary"] == "Sign-in fails" and out["created_at"] == "2026-07-25"

    monkeypatch.setattr(support.db, "get_ticket_by_reference", lambda ref, email: None)
    assert support.lookup_ticket("BLAM8-9999", "a@b.co") == {"ok": True, "found": False}

    monkeypatch.setattr(support.db, "get_ticket_by_reference",
                        lambda ref, email: (_ for _ in ()).throw(RuntimeError("db down")))
    assert support.lookup_ticket("BLAM8-2001", "a@b.co") == {"ok": False}


def test_status_labels_cover_both_lifecycles():
    for s in support.TICKET_STATUSES + support.FEATURE_STATUSES:
        assert support.STATUS_LABELS.get(s)


# ── the route: POST /concierge/ticket + /ask attachment ──────────────────────
@pytest.fixture
def client(monkeypatch):
    # The per-IP limiter is stateful across tests (every TestClient shares one
    # "IP") — switch it off here; the cap semantics are unit-tested above.
    from app.config import settings as cfg
    monkeypatch.setattr(cfg, "rate_limit_enabled", False)
    yield TestClient(app)   # no `with`: startup events must not run
    app.dependency_overrides.clear()


def test_ticket_route_files_and_returns_reference(client, monkeypatch):
    async def fake_create(**kw):
        assert kw["email"] == "kim@firm.co.uk" and kw["user_agent"]  # UA auto-collected
        return {"ok": True, "reference": "BLAM8-2002", "deduped": False, "redacted": True}
    monkeypatch.setattr(routes.support, "create_ticket", fake_create)
    r = client.post("/concierge/ticket", json={
        "email": "kim@firm.co.uk", "category": "login_auth",
        "summary": "Sign-in fails", "description": "details",
        "error_message": "", "account_type": "microsoft",
        "page_url": "https://blam8.com/login"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "reference": "BLAM8-2002", "deduped": False, "redacted": True}


def test_ticket_route_rejects_bad_email(client):
    r = client.post("/concierge/ticket", json={"email": "nope", "summary": "x"})
    assert r.status_code == 400


def test_ticket_route_429_on_email_cap(client, monkeypatch):
    async def fake_create(**kw):
        raise support.TicketRateLimited()
    monkeypatch.setattr(routes.support, "create_ticket", fake_create)
    r = client.post("/concierge/ticket", json={"email": "a@b.co", "summary": "x"})
    assert r.status_code == 429 and "hello@blam8.com" in r.json()["detail"]


def test_ticket_route_503_when_storage_down(client, monkeypatch):
    async def fake_create(**kw):
        raise RuntimeError("supabase down")
    monkeypatch.setattr(routes.support, "create_ticket", fake_create)
    r = client.post("/concierge/ticket", json={"email": "a@b.co", "summary": "x"})
    assert r.status_code == 503


def test_ask_attaches_server_side_lookup(client, monkeypatch):
    async def fake_answer(question, history):
        return concierge.ConciergeReply(
            reply="Let me check that for you.", offer_signup=False, suggested_industry="",
            ticket=None, lookup={"reference": "BLAM8-2001", "email": "a@b.co"},
            usage=None, model="test")
    monkeypatch.setattr(routes.concierge, "answer", fake_answer)
    monkeypatch.setattr(routes.usage, "record_anthropic", lambda *a, **k: None)
    monkeypatch.setattr(routes.support, "lookup_ticket",
                        lambda reference, email: {"ok": True, "found": True,
                                                  "reference": reference, "status": "open",
                                                  "status_label": "Open", "status_note": None,
                                                  "summary": "s", "created_at": "2026-07-25"})
    r = client.post("/concierge/ask", json={"question": "status of BLAM8-2001? a@b.co"})
    assert r.status_code == 200
    data = r.json()
    assert data["ticket_status"]["found"] is True
    assert data["ticket_status"]["reference"] == "BLAM8-2001"


def test_ask_passes_ticket_draft_through(client, monkeypatch):
    draft = {"summary": "Sign-in fails", "description": "d", "error_message": "",
             "email": "a@b.co", "category": "login_auth", "account_type": "google"}

    async def fake_answer(question, history):
        return concierge.ConciergeReply(
            reply="Here's your ticket to review.", offer_signup=False,
            suggested_industry="", ticket=draft, lookup=None, usage=None, model="test")
    monkeypatch.setattr(routes.concierge, "answer", fake_answer)
    monkeypatch.setattr(routes.usage, "record_anthropic", lambda *a, **k: None)
    r = client.post("/concierge/ask", json={"question": "yes please raise it"})
    assert r.status_code == 200
    assert r.json()["ticket"] == draft and r.json()["ticket_status"] is None
