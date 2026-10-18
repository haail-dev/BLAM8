"""Enterprise multi-mailbox (docs/architecture/enterprise-multi-mailbox.md).

Unit coverage for the spec's verification list, with the DB faked:
  - effective_config merge: null-inherit, override, purpose role line,
    per-mailbox auto_send tri-state, row metadata never leaking into the cfg
  - connect append-vs-replace by oauth_states mode (and same-address reconnects
    updating in place so the mailbox keeps its identity)
  - sibling-set self-mail guard: inbound from ANY of the tenant's own mailboxes
    drafts but never auto-sends; a stranger still auto-sends from the receiving
    mailbox, and the draft row is stamped with it
  - layered KB plumbing: retrieval and ingest carry the mailbox scope
  - Gmail poller set comes from the CONNECTION's provider (mixed tenants)
  - legacy rendering: null-mailbox_id conversations land under the default
    mailbox; the enterprise gate itself is plan == 'enterprise'

Run: python -m pytest tests/ -q   (from the project root)
"""
import asyncio
from datetime import datetime, timezone

from app import agent, db, pipeline


# ── effective_config (D3 — tenant defaults ⊕ mailbox overrides) ──────────────
def _wire_configs(monkeypatch, tenant_cfg, mailbox_cfg):
    monkeypatch.setattr(db, "get_tenant_config", lambda *a, **k: tenant_cfg)
    monkeypatch.setattr(db, "get_mailbox_config", lambda *a, **k: mailbox_cfg)


def test_effective_config_without_mailbox_is_pure_tenant_config(monkeypatch):
    _wire_configs(monkeypatch, {"tone": "warm", "auto_send": True}, {"tone": "MUST NOT APPEAR"})
    assert db.effective_config("t1") == {"tone": "warm", "auto_send": True}


def test_effective_config_null_fields_inherit(monkeypatch):
    _wire_configs(monkeypatch,
                  {"tone": "warm", "sign_off": "The Team", "auto_send": False},
                  {"mailbox_id": "m1", "tenant_id": "t1", "tone": None, "sign_off": None,
                   "auto_send": None, "updated_at": "2026-01-01"})
    cfg = db.effective_config("t1", "m1")
    assert cfg["tone"] == "warm" and cfg["sign_off"] == "The Team"
    assert cfg["auto_send"] is False
    # row plumbing must never leak into the agent's config
    assert "mailbox_id" not in cfg and "updated_at" not in cfg


def test_effective_config_set_fields_override(monkeypatch):
    _wire_configs(monkeypatch,
                  {"tone": "warm", "never_do": "Never quote prices", "escalation": "complaint"},
                  {"tone": "brisk", "never_do": "Never promise dates", "escalation": "legal"})
    cfg = db.effective_config("t1", "m1")
    # D3: escalation/never_do OVERRIDE when set (append semantics = later refinement)
    assert cfg == {"tone": "brisk", "never_do": "Never promise dates", "escalation": "legal"}


def test_effective_config_auto_send_tristate(monkeypatch):
    # orders@ runs full-auto while the company default is draft-only…
    _wire_configs(monkeypatch, {"auto_send": False}, {"auto_send": True})
    assert db.effective_config("t1", "m1")["auto_send"] is True
    # …and complaints@ pins draft-only against a full-auto company default.
    _wire_configs(monkeypatch, {"auto_send": True}, {"auto_send": False})
    assert db.effective_config("t1", "m1")["auto_send"] is False


def test_effective_config_purpose_becomes_role_line(monkeypatch):
    _wire_configs(monkeypatch, {"business_brief": "We haul freight."},
                  {"purpose": "this mailbox handles order intake"})
    brief = db.effective_config("t1", "m1")["business_brief"]
    assert brief == "We haul freight.\n\nThis mailbox: this mailbox handles order intake"
    # the merged brief flows into the prompt unchanged (agent.py untouched)
    assert "This mailbox: this mailbox handles order intake" in agent.build_system_prompt(
        {"business_brief": brief})


def test_effective_config_no_mailbox_row_is_pure_saas_identity(monkeypatch):
    _wire_configs(monkeypatch, {"tone": "warm"}, None)
    assert db.effective_config("t1", "m1") == {"tone": "warm"}


# ── connect: append vs replace by mode (D1 + the oauth_states.mode flow) ─────
class _FakeConnections:
    """Just enough of the supabase chain for upsert_email_connection: records
    deletes/updates/inserts against an in-memory email_connections list."""

    def __init__(self, rows):
        self.rows = [dict(r) for r in rows]
        self.deleted_other_than = "UNSET"   # the .neq() guard, if any
        self.updated: list[tuple] = []
        self.inserted: list[dict] = []

    def table(self, name):
        assert name == "email_connections"
        return _Query(self)


class _Query:
    def __init__(self, store):
        self.store = store
        self.op = None
        self.payload = None
        self.filters = {}
        self.neq_id = None

    def select(self, _cols):
        self.op = "select"; return self

    def delete(self):
        self.op = "delete"; return self

    def update(self, payload):
        self.op, self.payload = "update", payload; return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload; return self

    def eq(self, col, val):
        self.filters[col] = val; return self

    def neq(self, col, val):
        assert col == "id"
        self.neq_id = val; return self

    def execute(self):
        if self.op == "select":
            return type("Res", (), {"data": [dict(r) for r in self.store.rows]})()
        if self.op == "delete":
            self.store.deleted_other_than = self.neq_id
            keep = [r for r in self.store.rows if r["id"] == self.neq_id]
            self.store.rows = keep
            return type("Res", (), {"data": []})()
        if self.op == "update":
            self.store.updated.append((self.filters.get("id"), self.payload))
            return type("Res", (), {"data": []})()
        if self.op == "insert":
            self.store.inserted.append(self.payload)
            return type("Res", (), {"data": [{"id": "new-conn"}]})()
        raise AssertionError(f"unexpected op {self.op}")


def _expires():
    return datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)


def test_connect_add_mode_inserts_alongside_siblings(monkeypatch):
    store = _FakeConnections([{"id": "c-info", "address": "info@acme.co.uk"}])
    monkeypatch.setattr(db, "db", lambda: store)
    new_id = db.upsert_email_connection(
        "t1", "AT", "RT", _expires(),
        address="orders@acme.co.uk", provider="microsoft", mode="add")
    assert new_id == "new-conn"
    assert store.deleted_other_than == "UNSET"          # nothing deleted in add mode
    assert store.inserted[0]["address"] == "orders@acme.co.uk"
    assert store.inserted[0]["provider"] == "microsoft"


def test_connect_replace_mode_drops_siblings(monkeypatch):
    store = _FakeConnections([{"id": "c-old", "address": "old@acme.co.uk"}])
    monkeypatch.setattr(db, "db", lambda: store)
    db.upsert_email_connection(
        "t1", "AT", "RT", _expires(),
        address="new@acme.co.uk", provider="google", mode="replace")
    assert store.deleted_other_than is None             # delete ran, no survivor carve-out
    assert store.inserted[0]["address"] == "new@acme.co.uk"


def test_reconnecting_same_address_updates_in_place_keeping_the_id(monkeypatch):
    # The connection id IS the mailbox identity (config, scoped KB, log stamps),
    # so a token refresh must never mint a new row — in either mode. Address
    # matching is case-insensitive; a legacy row with no provider matches any.
    for mode in ("add", "replace"):
        store = _FakeConnections([{"id": "c-orders", "address": "Orders@Acme.co.uk"},
                                  {"id": "c-info", "address": "info@acme.co.uk"}])
        monkeypatch.setattr(db, "db", lambda s=store: s)
        kept = db.upsert_email_connection(
            "t1", "AT", "RT", _expires(),
            address="orders@acme.co.uk", provider="microsoft", mode=mode)
        assert kept == "c-orders"
        assert not store.inserted
        assert store.updated and store.updated[0][0] == "c-orders"
        if mode == "replace":
            assert store.deleted_other_than == "c-orders"   # siblings go, this row stays
        else:
            assert store.deleted_other_than == "UNSET"      # add mode: siblings untouched


def test_same_address_on_a_different_provider_is_a_sibling_not_a_reconnect(monkeypatch):
    # Live pilot edge: a Microsoft account whose PRIMARY alias is a gmail address
    # reports that gmail address to Graph — so the identical string can name two
    # different provider mailboxes. Adding the Gmail one must NOT swallow the
    # Outlook connection's row (its id anchors config/KB/log identity).
    store = _FakeConnections([{"id": "c-outlook", "address": "kris@gmail.com",
                               "provider": "microsoft"}])
    monkeypatch.setattr(db, "db", lambda: store)
    new_id = db.upsert_email_connection(
        "t1", "AT", "RT", _expires(),
        address="kris@gmail.com", provider="google", mode="add")
    assert new_id == "new-conn"
    assert not store.updated                      # outlook row untouched
    assert store.inserted[0]["provider"] == "google"
    # …while the SAME provider still reconnects in place
    store2 = _FakeConnections([{"id": "c-outlook", "address": "kris@gmail.com",
                                "provider": "microsoft"}])
    monkeypatch.setattr(db, "db", lambda: store2)
    kept = db.upsert_email_connection(
        "t1", "AT", "RT", _expires(),
        address="kris@gmail.com", provider="microsoft", mode="add")
    assert kept == "c-outlook" and not store2.inserted


# ── the enterprise gate + connect-mode resolution ─────────────────────────────
def test_is_enterprise_is_the_plan_gate(monkeypatch):
    assert db.is_enterprise({"plan": "enterprise"}) is True
    assert db.is_enterprise({"plan": "pilot"}) is False
    monkeypatch.setattr(db, "get_tenant", lambda *a, **k: {"plan": "enterprise"})
    assert db.is_enterprise("t1") is True


# ── pipeline: sibling-set guard + mailbox stamping + reply-from (D5) ──────────
class _PipelineEnv:
    """Fakes every pipeline collaborator; records the interesting calls."""

    def __init__(self, monkeypatch, own={"info@acme.co.uk", "orders@acme.co.uk"}):
        self.sent: list[dict] = []
        self.inserted: list[dict] = []
        self.retrieved: list[dict] = []

        async def fake_draft(cfg, email, **kw):
            return agent.DraftResult(text="Reply.", usage={"input_tokens": 1}, model="m")

        async def fake_send(tenant_id, to, subject, body, source_message_id=None, mailbox_id=None):
            self.sent.append({"to": to, "mailbox_id": mailbox_id})

        async def fake_retrieve(tenant_id, query, k=None, mailbox_id=None, whole_if_small=False):
            self.retrieved.append({"mailbox_id": mailbox_id})
            return []

        def fake_insert(tenant_id, from_address, subject, body, draft, **kw):
            self.inserted.append(kw)
            return "log-1", True

        monkeypatch.setattr(pipeline.agent, "draft_reply", fake_draft)
        monkeypatch.setattr(pipeline.mailbox, "send_reply", fake_send)
        monkeypatch.setattr(pipeline.kb, "retrieve", fake_retrieve)
        monkeypatch.setattr(pipeline.kb, "render_for_prompt", lambda *a: None)
        monkeypatch.setattr(pipeline.usage, "record_anthropic", lambda *a, **k: None)
        monkeypatch.setattr(pipeline.db, "is_ignored", lambda *a, **k: False)
        monkeypatch.setattr(pipeline.db, "active_modules", lambda *a, **k: set())
        monkeypatch.setattr(pipeline.db, "get_conversation", lambda *a, **k: [])
        monkeypatch.setattr(pipeline.db, "get_tenant_config", lambda *a, **k: {"auto_send": True})
        monkeypatch.setattr(pipeline.db, "get_mailbox_config", lambda *a, **k: None)
        monkeypatch.setattr(pipeline.db, "get_tenant", lambda *a, **k: {"industry": "logistics"})
        monkeypatch.setattr(pipeline.db, "own_mailbox_addresses", lambda *a, **k: own)
        monkeypatch.setattr(pipeline.db, "mailbox_self_addresses",
                            lambda tid, mid=None, **k: {"orders@acme.co.uk"} if mid == "c-orders" else set())
        monkeypatch.setattr(pipeline.db, "bookings_for_contact", lambda *a, **k: [])
        monkeypatch.setattr(pipeline.db, "get_scheduling_config", lambda *a, **k: None)
        monkeypatch.setattr(pipeline.db, "insert_email_log", fake_insert)
        monkeypatch.setattr(pipeline.db, "set_email_log_status", lambda *a, **k: None)


EMAIL = {"from_address": "Customer@other.co.uk", "subject": "Quote", "body": "Friday?"}


def test_sibling_mail_drafts_but_never_auto_sends(monkeypatch):
    env = _PipelineEnv(monkeypatch)
    # orders@ receives mail FROM info@ — same tenant, different mailbox (case-insensitive)
    email = {**EMAIL, "from_address": "Info@Acme.co.uk"}
    log_id = asyncio.run(pipeline.draft_for_email(
        "t1", email, source_message_id="m1", auto_send_ok=True, mailbox_id="c-orders"))
    assert log_id == "log-1"            # it DID draft…
    assert env.sent == []               # …but the sibling set held the send


def test_own_outgoing_mail_is_not_re_drafted(monkeypatch):
    env = _PipelineEnv(monkeypatch)
    # orders@ sees its OWN sent reply re-arrive — from == the address it answers from.
    # This must NOT draft a reply to itself (the self-loop guard); the inbound is
    # logged under Automated so the inbox shows it was seen and left alone.
    email = {**EMAIL, "from_address": "Orders@Acme.co.uk"}
    log_id = asyncio.run(pipeline.draft_for_email(
        "t1", email, source_message_id="m1", auto_send_ok=True, mailbox_id="c-orders"))
    assert log_id is None               # no draft
    assert env.sent == []               # nothing sent
    assert [r["status"] for r in env.inserted] == ["skipped"]   # logged, never queued
    assert env.inserted[0].get("category") == "automated"
    assert env.inserted[0].get("kind") == "self_loop"


def test_send_as_copy_is_not_re_drafted(monkeypatch):
    env = _PipelineEnv(monkeypatch)
    # The mailbox sends AS a routed alias (send_as_address) and that alias routes
    # back into the same inbox (2026-07-26 e2e: hello@ via Cloudflare). The self-
    # loop guard must catch the alias identity too — not just the mailbox address —
    # or the agent negotiates bookings with itself.
    monkeypatch.setattr(pipeline.db, "mailbox_self_addresses",
                        lambda tid, mid=None, **k: {"orders@acme.co.uk", "hello@acme.co.uk"})
    email = {**EMAIL, "from_address": "Hello@Acme.co.uk"}
    log_id = asyncio.run(pipeline.draft_for_email(
        "t1", email, source_message_id="m1", auto_send_ok=True, mailbox_id="c-orders"))
    assert log_id is None
    assert env.sent == []
    assert [r["status"] for r in env.inserted] == ["skipped"]
    assert env.inserted[0].get("category") == "automated"
    assert env.inserted[0].get("kind") == "self_loop"


def test_stranger_auto_sends_from_the_receiving_mailbox(monkeypatch):
    env = _PipelineEnv(monkeypatch)
    asyncio.run(pipeline.draft_for_email(
        "t1", EMAIL, source_message_id="m1", auto_send_ok=True, mailbox_id="c-orders"))
    assert env.sent == [{"to": "Customer@other.co.uk", "mailbox_id": "c-orders"}]


def test_draft_stamps_the_receiving_mailbox_and_scopes_retrieval(monkeypatch):
    env = _PipelineEnv(monkeypatch)
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, mailbox_id="c-orders"))
    assert env.inserted[0]["mailbox_id"] == "c-orders"
    assert env.retrieved == [{"mailbox_id": "c-orders"}]   # layered KB (D4)


def test_saas_path_unchanged_null_mailbox(monkeypatch):
    env = _PipelineEnv(monkeypatch, own={"me@biz.co"})
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1", auto_send_ok=True))
    assert env.inserted[0]["mailbox_id"] is None
    assert env.sent == [{"to": "Customer@other.co.uk", "mailbox_id": None}]


def test_per_mailbox_auto_send_override_gates_the_send(monkeypatch):
    env = _PipelineEnv(monkeypatch)
    # company default full-auto, but THIS mailbox is pinned draft-only
    monkeypatch.setattr(pipeline.db, "get_mailbox_config", lambda *a, **k: {"auto_send": False})
    asyncio.run(pipeline.draft_for_email(
        "t1", EMAIL, source_message_id="m1", auto_send_ok=True, mailbox_id="c-complaints"))
    assert env.sent == []
    assert env.inserted  # still drafted, pending approval


# ── poller set by connection provider (D5 — mixed tenants) ────────────────────
def test_list_google_connections_filters_on_the_connection_provider(monkeypatch):
    class _Q:
        def __init__(self):
            self.eqs = []

        def table(self, name):
            assert name == "email_connections"; return self

        def select(self, _cols):
            return self

        def eq(self, col, val):
            self.eqs.append((col, val)); return self

        def execute(self):
            return type("Res", (), {"data": []})()

    q = _Q()
    monkeypatch.setattr(db, "db", lambda: q)
    db.list_google_connections()
    assert q.eqs == [("provider", "google")]   # never via tenants.provider


# ── legacy rendering: null mailbox_id rows belong to the default mailbox ──────
class _LogQuery:
    def __init__(self, rows):
        self.rows = rows

    def table(self, _n):
        return self

    def select(self, _c, **_k):
        return self

    def range(self, *_a):
        return self

    def eq(self, *_a):
        return self

    def in_(self, *_a):
        return self

    def gte(self, *_a):
        return self

    def lte(self, *_a):
        return self

    def order(self, _c):
        return self

    def execute(self):
        return type("Res", (), {"data": self.rows})()


_LOG_ROWS = [
    {"from_address": "old@x.com", "subject": "Legacy", "body_in": "b", "status": "sent",
     "knowledge_gap": None, "created_at": "2026-06-01", "mailbox_id": None},
    {"from_address": "new@x.com", "subject": "Orders", "body_in": "b", "status": "pending",
     "knowledge_gap": None, "created_at": "2026-06-02", "mailbox_id": "c-orders"},
]


def test_default_mailbox_tab_includes_legacy_null_rows():
    convos = db.list_conversations("t1", client=_LogQuery(_LOG_ROWS),
                                   mailbox_id="c-info", default_mailbox_id="c-info")
    assert [c["contact"] for c in convos] == ["old@x.com"]


def test_non_default_mailbox_tab_shows_only_its_own_rows():
    convos = db.list_conversations("t1", client=_LogQuery(_LOG_ROWS),
                                   mailbox_id="c-orders", default_mailbox_id="c-info")
    assert [c["contact"] for c in convos] == ["new@x.com"]


def test_unfiltered_list_keeps_everything():
    convos = db.list_conversations("t1", client=_LogQuery(_LOG_ROWS))
    assert {c["contact"] for c in convos} == {"old@x.com", "new@x.com"}
