"""Agent trace — the execution-narrative observability layer (src/app/trace.py).

What this pins:
  * emit() writes one agent_trace row with the ambient llm request_id, and a
    db failure is swallowed (observability never breaks the action);
  * session_ref() never exposes the portal session token;
  * Concierge Phase 2: confirm/execute, expiry and deny all leave trace rows
    with the proposal id and actor;
  * Portal: reference-code verification attempts (success AND failure),
    KB retrieval, and escalation each leave a trace row;
  * push delivery: per-device outcomes roll up honestly (ok / partial /
    no_devices), expired subscriptions are recorded before removal, and the
    row never claims a delivery receipt Web Push cannot give;
  * voice learning: every run ends in exactly one tone_analysis row;
  * pipeline classification: the categorisation row carries the model's call,
    the final label, and the method (llm vs rule override).

Run: python -m pytest tests/test_agent_trace.py -q
"""
import asyncio
from proposal_receipts import execution_result
import json
import types

import pytest
from fastapi.testclient import TestClient

from app import db, llm, portal_data, trace
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf_header


T = "0b6f8f9e-0000-0000-0000-000000000001"


@pytest.fixture
def rows(monkeypatch):
    """Recorder replacing the conftest no-op stub for db.insert_agent_trace."""
    out: list[dict] = []
    monkeypatch.setattr(db, "insert_agent_trace", out.append)
    return out


@pytest.fixture
def emitted(monkeypatch):
    """Recorder one level up: captures trace.emit calls themselves (for call
    sites reached through routes/threads where the row shape is emit's job)."""
    out: list[tuple] = []
    monkeypatch.setattr(trace, "emit",
                        lambda tenant_id, trace_type, **kw:
                        out.append((tenant_id, trace_type, kw)))
    return out


# ── emit() ────────────────────────────────────────────────────────────────────
def test_emit_writes_one_row_with_defaults(rows):
    trace.emit(T, "portal_chat", contact="c@x.com", latency_ms=12.7,
               detail={"message": "hi"})
    assert len(rows) == 1
    row = rows[0]
    assert row["tenant_id"] == T and row["trace_type"] == "portal_chat"
    assert row["outcome"] == "ok"
    assert row["contact"] == "c@x.com"
    assert row["latency_ms"] == 12          # int-coerced
    assert row["detail"] == {"message": "hi"}
    assert row["request_id"] is None        # outside any llm scope


def test_emit_picks_up_the_ambient_llm_request_id(rows):
    with llm.request_scope():
        ambient = llm.current_request_id()
        trace.emit(T, "concierge_turn")
    assert ambient and rows[0]["request_id"] == ambient
    # An explicit request_id wins over the ambient one.
    trace.emit(T, "categorisation", request_id="req-9")
    assert rows[1]["request_id"] == "req-9"


def test_emit_swallows_a_db_failure(monkeypatch):
    def boom(row):
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "insert_agent_trace", boom)
    trace.emit(T, "push_send")   # must not raise


def test_session_ref_hides_the_token():
    token = "secret-session-token-abc"
    ref = trace.session_ref(token)
    assert ref and token not in ref and len(ref) == 16
    assert trace.session_ref(token) == ref          # stable
    assert trace.session_ref("") is None and trace.session_ref(None) is None


# ── Concierge Phase 2 lifecycle (routes) ─────────────────────────────────────
@pytest.fixture
def client(monkeypatch):
    ctx = TenantCtx(tenant_id="t1", user_id="u-7", client=None)
    app.dependency_overrides[require_onboarded] = lambda: ctx
    app.dependency_overrides[require_csrf_header] = lambda: None
    from app import concierge_routes
    monkeypatch.setattr(concierge_routes.db, "add_concierge_message",
                        lambda *a, **k: None)
    # The account's own owner (no People row): the confirm route now reads the
    # asker, and a failed read refuses (security review M1/M34).
    monkeypatch.setattr(concierge_routes.db, "get_member", lambda *a, **k: None)
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_execute_route_traces_confirm_and_execute(client, monkeypatch, emitted):
    from app import concierge_actions, concierge_routes
    monkeypatch.setattr(concierge_routes.db, "claim_concierge_proposal",
                        lambda pid, tid, max_age_s=300, client=None, **kw: {
                            "id": pid, "result": execution_result(pid, kw.get("execution_input")), "kind": "add_kb_entry", "user_id": "u-1",
                            "created_at": "2026-07-26T10:00:00+00:00",
                            "payload": {"title": "Power flush", "content": "350"}})
    monkeypatch.setattr(concierge_routes.db, "finish_concierge_proposal",
                        lambda *a, **k: True)

    async def fake_exec(tenant_id, client, kind, payload, edited_draft_text=None, selected=None):
        return {"summary": "Done.", "link": None}
    monkeypatch.setattr(concierge_routes.concierge_actions, "execute", fake_exec)

    res = client.post("/concierge/business/execute",
                      json={"proposal_id": "0b6f8f9e-1111-2222-3333-444455556666"})
    assert res.json()["ok"] is True
    kinds = [(t, kw.get("outcome", "ok")) for _tid, t, kw in emitted]
    assert ("concierge_confirm", "confirmed") in kinds
    assert ("concierge_execute", "ok") in kinds
    confirm = next(kw for _t, ty, kw in emitted if ty == "concierge_confirm")
    assert confirm["user_id"] == "u-7"                      # who clicked
    assert confirm["detail"]["proposed_by"] == "u-1"        # who asked
    assert confirm["detail"]["time_to_confirm_ms"] is not None
    execute = next(kw for _t, ty, kw in emitted if ty == "concierge_execute")
    assert execute["detail"]["kind"] == "add_kb_entry"
    assert execute["proposal_id"] == "0b6f8f9e-1111-2222-3333-444455556666"


def test_execute_route_traces_failed_execution(client, monkeypatch, emitted):
    from app import concierge_actions, concierge_routes
    monkeypatch.setattr(concierge_routes.db, "claim_concierge_proposal",
                        lambda *a, **k: {"id": a[0], "result": execution_result(a[0], k.get("execution_input")), "kind": "move_booking",
                                         "payload": {}})
    monkeypatch.setattr(concierge_routes.db, "finish_concierge_proposal",
                        lambda *a, **k: True)

    async def fake_exec(*a, **k):
        raise concierge_actions.ActionError("That slot filled up.")
    monkeypatch.setattr(concierge_routes.concierge_actions, "execute", fake_exec)

    client.post("/concierge/business/execute", json={"proposal_id": "p-1"})
    execute = next(kw for _t, ty, kw in emitted if ty == "concierge_execute")
    assert execute["outcome"] == "error"
    assert execute["detail"]["state"] == "unknown"


def test_expired_proposal_traces_the_timeout(client, monkeypatch, emitted):
    from app import concierge_routes
    monkeypatch.setattr(concierge_routes.db, "claim_concierge_proposal",
                        lambda *a, **k: None)
    monkeypatch.setattr(concierge_routes.db, "get_concierge_proposal",
                        lambda *a, **k: {"status": "pending", "kind": "move_booking"})
    monkeypatch.setattr(concierge_routes.db, "finish_concierge_proposal",
                        lambda *a, **k: True)
    client.post("/concierge/business/execute", json={"proposal_id": "p-1"})
    expire = next(kw for _t, ty, kw in emitted if ty == "concierge_expire")
    assert expire["outcome"] == "expired"
    assert expire["detail"] == {"reason": "timeout", "kind": "move_booking"}


def test_cancel_route_traces_the_denial(client, monkeypatch, emitted):
    from app import concierge_routes
    monkeypatch.setattr(concierge_routes.db, "cancel_concierge_proposal",
                        lambda pid, tid, client=None: True)
    monkeypatch.setattr(concierge_routes.db, "get_concierge_proposal",
                        lambda *a, **k: {"kind": "create_contact"})
    client.post("/concierge/business/cancel", json={"proposal_id": "p-1"})
    deny = next(kw for _t, ty, kw in emitted if ty == "concierge_deny")
    assert deny["outcome"] == "denied" and deny["user_id"] == "u-7"
    assert deny["detail"] == {"reason": "user_denied", "kind": "create_contact"}


def test_cancel_of_a_closed_proposal_traces_nothing(client, monkeypatch, emitted):
    from app import concierge_routes
    monkeypatch.setattr(concierge_routes.db, "get_concierge_proposal",
                        lambda *a, **k: {"id": a[0], "status": "cancelled", "kind": "create_contact"})
    monkeypatch.setattr(concierge_routes.db, "cancel_concierge_proposal",
                        lambda *a, **k: False)
    client.post("/concierge/business/cancel", json={"proposal_id": "p-1"})
    assert not [e for e in emitted if e[1] == "concierge_deny"]


# ── Portal identity verification + KB + escalation ───────────────────────────
def _session(**kw):
    return portal_data.PortalSession(tenant_id="t1", business_name="Acme",
                                     trace_session="abcd1234abcd1234", **kw)


def test_lookup_reference_traces_both_outcomes(monkeypatch, emitted):
    monkeypatch.setattr(portal_data.db, "crm_contact_by_reference",
                        lambda t, code: None)
    s = _session()
    portal_data._lookup_reference(s, {"code": "ACME-7K3M"})
    _tid, ty, kw = emitted[0]
    assert ty == "portal_identity_verify" and kw["outcome"] == "not_found"
    assert kw["session_id"] == "abcd1234abcd1234"
    # The full code never lands in the trace — masked form only.
    assert kw["detail"]["code_masked"] == "ACME-7K**"
    assert "ACME-7K3M" not in json.dumps(kw["detail"])

    monkeypatch.setattr(portal_data.db, "crm_contact_by_reference",
                        lambda t, code: {"id": "c1", "email": "v@x.com",
                                         "reference_code": "ACME-7K3M"})
    monkeypatch.setattr(portal_data.db, "bookings_for_contact", lambda t, e: [])
    portal_data._lookup_reference(s, {"code": "ACME-7K3M"})
    _tid, ty, kw = emitted[1]
    assert kw["outcome"] == "verified" and kw["contact"] == "v@x.com"


def test_kb_retrieval_traces_query_and_hit_count(monkeypatch, emitted):
    async def fake_retrieve(tenant_id, query, k=5):
        return [{"source": "Pricing", "content": "Power flush £350"}]
    monkeypatch.setattr(portal_data.kb, "retrieve", fake_retrieve)
    out = asyncio.run(portal_data._search_knowledge_base(_session(), {"query": "flush?"}))
    assert out["entries"]
    _tid, ty, kw = emitted[0]
    assert ty == "portal_kb_retrieval" and kw["outcome"] == "ok"
    assert kw["detail"]["results_returned"] == 1
    assert kw["detail"]["sources"] == ["Pricing"]
    assert kw["latency_ms"] is not None


def test_escalation_traces_the_inbox_row(monkeypatch, emitted):
    monkeypatch.setattr(portal_data.db, "insert_email_log",
                        lambda *a, **k: ("log-9", True))
    monkeypatch.setattr(portal_data.db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(portal_data.push, "notify", lambda *a, **k: None)
    monkeypatch.setattr(portal_data.events, "publish", lambda *a, **k: None)
    s = _session(contact={"id": "c1", "email": "v@x.com"})
    out = portal_data._escalate_to_business(s, {"message": "Refund please"})
    assert out["escalated"] is True
    esc = next(kw for _t, ty, kw in emitted if ty == "portal_escalation")
    assert esc["email_log_id"] == "log-9"
    assert esc["detail"]["escalated_to"] == "inbox"
    assert "Refund" in esc["detail"]["message"]


# ── Push delivery ────────────────────────────────────────────────────────────
def _sub(sub_id, user="u1"):
    return {"id": sub_id, "user_id": user, "endpoint": "https://push.example/e",
            "keys_p256dh": "p", "keys_auth": "a"}


def test_push_send_traces_per_device_outcomes(monkeypatch, emitted):
    import pywebpush
    from app import push as push_mod
    calls = []

    def flaky(**kw):
        calls.append(1)
        if len(calls) == 1:
            raise pywebpush.WebPushException(
                "server error", response=types.SimpleNamespace(status_code=500))

    monkeypatch.setattr(pywebpush, "webpush", flaky)
    monkeypatch.setattr(push_mod.db, "list_push_subscriptions",
                        lambda t: [_sub("s1"), _sub("s2", "u2")])
    monkeypatch.setattr(push_mod.db, "touch_push_subscription", lambda i: None)
    push_mod._send_to_tenant("t1", {"title": "x", "tag": "draft-L1"}, "draft_ready")
    _tid, ty, kw = emitted[0]
    assert ty == "push_send" and kw["outcome"] == "partial"
    d = kw["detail"]
    assert d["kind"] == "draft_ready" and d["tag"] == "draft-L1"
    assert d["delivery_receipts"] == "unavailable"   # honest: no Web Push receipts
    statuses = {dev["subscription_id"]: dev["status"] for dev in d["devices"]}
    assert statuses == {"s1": "failed", "s2": "accepted"}
    assert d["devices"][0]["http_status"] == 500
    assert d["devices"][1]["user_id"] == "u2"


def test_push_send_records_expired_removal_and_no_devices(monkeypatch, emitted):
    import pywebpush
    from app import push as push_mod

    def gone(**kw):
        raise pywebpush.WebPushException(
            "gone", response=types.SimpleNamespace(status_code=410))
    monkeypatch.setattr(pywebpush, "webpush", gone)
    removed = []
    monkeypatch.setattr(push_mod.db, "list_push_subscriptions", lambda t: [_sub("dead")])
    monkeypatch.setattr(push_mod.db, "remove_push_subscription_by_id", removed.append)
    push_mod._send_to_tenant("t1", {"title": "x", "tag": "t"}, "portal_booking_created")
    assert removed == ["dead"]
    _tid, ty, kw = emitted[0]
    assert kw["detail"]["devices"][0]["status"] == "expired_removed"
    assert kw["outcome"] == "failed"       # nothing was accepted

    monkeypatch.setattr(push_mod.db, "list_push_subscriptions", lambda t: [])
    push_mod._send_to_tenant("t1", {"title": "x", "tag": "t"})
    assert emitted[1][2]["outcome"] == "no_devices"


# ── Voice learning ───────────────────────────────────────────────────────────
def test_voice_run_traces_insufficient(monkeypatch, emitted):
    import app.voice_learning as vl
    monkeypatch.setattr(vl, "_store", lambda *a, **k: None)

    async def few(tenant_id, mailbox_id=None):
        return ([{"body": "hi", "to": [{"address": "a@x.com"}]}] * 3, {"me@x.com"})
    monkeypatch.setattr(vl, "fetch_sent_corpus", few)
    monkeypatch.setattr(vl, "usable_text", lambda m, own: "some text")
    assert asyncio.run(vl.run_learning(T)) == "insufficient"
    _tid, ty, kw = emitted[0]
    assert ty == "tone_analysis" and kw["outcome"] == "insufficient"
    assert kw["detail"]["emails_analysed"] == 3
    assert kw["detail"]["profile_created"] is False


def test_voice_run_traces_ready_with_batch_stats(monkeypatch, emitted):
    import app.voice_learning as vl
    stored = []
    monkeypatch.setattr(vl, "_store", lambda tid, cfg: stored.append(cfg))

    async def corpus(tenant_id, mailbox_id=None):
        return ([{}] * 25, {"me@x.com"})
    monkeypatch.setattr(vl, "fetch_sent_corpus", corpus)
    monkeypatch.setattr(vl, "usable_text", lambda m, own: "some text")

    async def analysed(tid, texts):
        return {"tone": "friendly", "formality_level": 2, "sign_off": "Cheers",
                "_batches": {"attempted": 1, "succeeded": 1}}
    monkeypatch.setattr(vl, "analyse", analysed)

    async def sample(tid, profile, brief):
        return "Hi Sarah"
    monkeypatch.setattr(vl, "generate_sample", sample)
    monkeypatch.setattr(vl.db, "get_tenant_config", lambda t: {})

    assert asyncio.run(vl.run_learning(T)) == "ready"
    _tid, ty, kw = emitted[0]
    assert kw.get("outcome", "ok") == "ok"
    assert kw["detail"]["batches"] == {"attempted": 1, "succeeded": 1}
    assert kw["detail"]["profile_summary"]["tone"] == "friendly"
    # The stat bag never pollutes the stored profile.
    ready = next(c for c in stored if c.get("voice_learning_status") == "ready")
    assert "_batches" not in ready["voice_profile"]


def test_analyse_stamps_batch_stats(monkeypatch):
    import app.voice_learning as vl

    async def one_batch(tenant_id, texts):
        return {"tone": "friendly"}
    monkeypatch.setattr(vl, "_analyse_batch", one_batch)
    profile = asyncio.run(vl.analyse(T, ["text"] * 45))   # 2 batches of 40/5
    assert profile["_batches"] == {"attempted": 2, "succeeded": 2}


# ── Categorisation method (llm vs rule) ──────────────────────────────────────
def test_categorisation_method_reflects_an_override():
    # The pipeline computes method as 'llm' when the model's call survived and
    # 'rule' when a ground-truth signal overrode it — pin the expression shape.
    from app import pipeline
    final, _ = pipeline._classify("fyi", escalated=True, scheduling_gap=False,
                                  deferred=False, thread_state="in_progress",
                                  held=False)
    assert final == "needs_reply"
    assert ("llm" if final == "fyi" else "rule") == "rule"
