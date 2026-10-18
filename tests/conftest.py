"""Shared test fixtures."""
import pytest
from cryptography.fernet import Fernet

from app import crypto, db, navcache


@pytest.fixture(autouse=True)
def _booking_scenario_contact_fields(request, monkeypatch):
    """These booking fakes have no contact ladders unless a test sets them.

    Explicitly model a successful empty read: failed reads now hold for review.
    Scope this to booking scenarios so DB-client tests still exercise real reads.
    A test's own monkeypatch takes precedence over this fixture.
    """
    if request.path.stem in {
        "test_already_booked_followup", "test_appointment_and_stay", "test_courses",
        "test_inbound_depth", "test_ladders", "test_makeups", "test_out_of_hours",
        "test_service_executor", "test_subject_holds",
    }:
        monkeypatch.setattr(db, "crm_list_field_definitions", lambda *a, **k: [])


@pytest.fixture(autouse=True)
def _fresh_navcache(monkeypatch):
    """The per-navigation TTL cache (src/app/navcache.py) is process-global AND
    tests re-render pages after re-patching their Env mid-test — so under tests
    the cache is disabled outright (every lookup misses, fill always runs).
    The cache is timing, never semantics; the store is cleared on both sides
    so no test can inherit another's state either way."""
    navcache._store.clear()
    monkeypatch.setattr(navcache, "_disabled", True)
    yield
    navcache._store.clear()


@pytest.fixture(autouse=True)
def _concierge_on_by_default(monkeypatch):
    """The business Concierge is its own tier since 2026-09-25 (Pro +
    Concierge). Every test written before that assumed she was there, so the
    route and page checks default to yes; tests/test_concierge_tier.py turns
    them off to prove the gate. The templates' concierge_on still comes from
    the tenant's modules."""
    from app import auth, dashboard
    monkeypatch.setattr(auth, "has_concierge", lambda *a, **k: True)
    monkeypatch.setattr(dashboard, "has_concierge", lambda *a, **k: True)


@pytest.fixture(autouse=True)
def _no_live_supabase(monkeypatch):
    """Fail loudly if a test reaches the real Supabase client.

    Tests monkeypatch individual functions on app.db (get_tenant, mailbox_address,
    ...), so a legitimate patched call never constructs a client. Anything that
    falls through — a new db.* call in the code under test that an older test's
    Env doesn't patch — used to silently fire network requests at live Supabase
    with fake tenant ids (see commits aa7c69e/29b9a9d). Guard every construction
    path: db()/db_user() by name for a clear traceback, create_client for callers
    that bound the original db function at import time (app/jobs/*), and reset the
    _client cache so a client cached before the guard can't leak through."""
    def _blocked(*a, **k):
        raise RuntimeError(
            "test reached live Supabase — monkeypatch this db function on app.db"
        )

    monkeypatch.setattr(db, "_client", None)
    monkeypatch.setattr(db, "create_client", _blocked)
    monkeypatch.setattr(db, "db", _blocked)
    monkeypatch.setattr(db, "db_user", _blocked)


@pytest.fixture(autouse=True)
def _deterministic_encryption_key(monkeypatch):
    """Token crypto must not depend on the developer's .env. Tests that exercise
    upsert_email_connection/get_connection call the real crypto.encrypt/decrypt,
    which otherwise lazily builds a Fernet from settings.token_encryption_key —
    absent (fresh worktree, CI) that crashes, present it silently couples tests to
    the local key. Pin a fixed valid key by pre-seeding the module cache."""
    monkeypatch.setattr(
        crypto, "_fernet", Fernet(b"MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")
    )


@pytest.fixture(autouse=True)
def _no_preview_modules(monkeypatch):
    """OS preview (db.preview_modules) is an additive overlay on the module
    switchboard — visible_modules/module_ui_visible/_ctx call it alongside the
    active_modules/module_active primitives that existing tests stub. Default it
    to 'nothing previewing' so those tests keep exercising exactly the
    pre-preview world; a preview test re-patches it (later setattr wins)."""
    monkeypatch.setattr(db, "preview_modules", lambda *a, **k: set())


@pytest.fixture(autouse=True)
def _no_llm_call_writes(monkeypatch):
    """The full-transcript LLM log (db.insert_llm_call via src/app/llm.py) is a
    best-effort side effect on every drafting hot path — it must NEVER hit the
    real database during tests. Stub to a no-op by default; a test that asserts
    logging re-patches it to a recorder (the later setattr wins)."""
    monkeypatch.setattr(db, "insert_llm_call", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _no_push_sends(monkeypatch):
    """Web Push (src/app/push.py) is a best-effort side effect on the drafting hot
    path — with VAPID keys present in the developer's .env it would spawn real
    delivery threads during tests. Stub the notifier to a no-op by default; a
    test that asserts pushes re-patches it to a recorder (later setattr wins)."""
    from app import push

    monkeypatch.setattr(push, "notify_draft_ready", lambda *a, **k: None)
    monkeypatch.setattr(push, "notify", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _no_health_writes(monkeypatch):
    """Connection health (src/app/health.py) is a best-effort side effect on every
    background sweep — the Gmail poller, the calendar sync and the Graph
    subscription renewer all call it, so any test exercising those would reach
    live Supabase. Stubbed at the DB layer rather than at health.*, so the
    module's own logic still runs: get_connection_health returning None is
    "no such connection", which record_failure/record_success already treat as
    nothing to do. A test about health re-patches these to real fakes (the
    later setattr wins)."""
    monkeypatch.setattr(db, "get_connection_health", lambda *a, **k: None)
    monkeypatch.setattr(db, "set_connection_health", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _no_agent_trace_writes(monkeypatch):
    """The agent trace (db.insert_agent_trace via src/app/trace.py) is a best-effort
    side effect on every instrumented path — it must NEVER hit the real database
    during tests. Stub to a no-op by default; a test that asserts trace emission
    re-patches app.trace.emit to a recorder (the later setattr wins)."""
    monkeypatch.setattr(db, "insert_agent_trace", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _no_draft_suppression_state(monkeypatch):
    """Draft suppression (src/app/suppression.py) reads/writes contact_draft_stats as
    a best-effort side effect on the drafting hot path — its swallow-everything
    stance would otherwise silently absorb the _no_live_supabase RuntimeError on
    every pipeline test. Default to 'no contact has any streak' so existing tests
    exercise exactly the pre-suppression world; a suppression test re-patches
    these to a recorder/fake store (the later setattr wins)."""
    monkeypatch.setattr(db, "get_contact_draft_stats", lambda *a, **k: None)
    monkeypatch.setattr(db, "upsert_contact_draft_stats", lambda *a, **k: None)
    monkeypatch.setattr(db, "list_draft_suppressed", lambda *a, **k: [])
    monkeypatch.setattr(db, "recent_inbound_body_lengths", lambda *a, **k: [])


@pytest.fixture(autouse=True)
def _no_agent_event_writes(monkeypatch):
    """The Agent Log writes (db.log_agent_event) are best-effort side effects on the
    drafting hot path — they must NEVER hit the real database during tests. Stub to a
    no-op by default; a test that asserts emission re-patches it to a recorder (the
    later monkeypatch.setattr wins, since both share this function's monkeypatch)."""
    monkeypatch.setattr(db, "log_agent_event", lambda *a, **k: None)
