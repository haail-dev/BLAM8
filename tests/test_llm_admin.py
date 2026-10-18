"""Operator LLM-call viewer — exchange grouping, cost estimation, and the pages.

llm_calls is the first diagnostic stop when the assistant misbehaves, but reading it
meant SQL until now. The unit that matters is the EXCHANGE (request_id), not the row:
one owner question is usually several API calls in a tool loop.

Run: python -m pytest tests/test_llm_admin.py -q
"""
import pytest
from fastapi.testclient import TestClient

import app.admin as admin_mod
from app import llm_admin
from app.admin_auth import AdminCtx, require_admin
from app.main import app

# Must be a model that exists in usage.PRICING, else cost is 0 by design.
MODEL = "claude-sonnet-4-6"


def _call(cid, request_id, *, op="concierge", module="concierge", inp=1000, out=200,
          ms=900, stop="end_turn", tenant="t1", created="2026-07-30T10:00:00+00:00", **kw):
    return {"id": cid, "request_id": request_id, "operation": op, "module": module,
            "model": MODEL, "input_tokens": inp, "output_tokens": out,
            "duration_ms": ms, "stop_reason": stop, "tenant_id": tenant,
            "created_at": created, **kw}


# ── cost estimation ───────────────────────────────────────────────────────────
def test_cost_is_derived_from_tokens():
    c = llm_admin.call_cost_usd(_call("a", "r1", inp=1_000_000, out=0))
    assert c > 0


def test_unknown_model_cost_stays_unknown():
    # Unknown prices are not zero; the page still renders an incomplete subtotal.
    assert llm_admin.call_cost_usd({"model": "not-a-model",
                                    "input_tokens": 1000, "output_tokens": 1000}) is None


def test_missing_token_counts_are_unknown():
    assert llm_admin.call_cost_usd({"model": MODEL}) is None
    assert llm_admin.annotate([{"model": MODEL, "input_tokens": None}])[0]["tokens"] == 0


# ── annotation ────────────────────────────────────────────────────────────────
def test_annotate_adds_tokens_cost_source_without_mutating_the_input():
    row = _call("a", "r1")
    out = llm_admin.annotate([row])[0]
    assert out["tokens"] == 1200 and out["cost_usd"] > 0 and out["source"]
    assert "tokens" not in row, "annotate mutated its input"


@pytest.mark.parametrize("stop", llm_admin.BAD_STOP_REASONS)
def test_bad_stop_reasons_are_flagged_as_failures(stop):
    assert llm_admin.annotate([_call("a", "r1", stop=stop)])[0]["failed"] is True


def test_a_normal_end_turn_is_not_a_failure():
    assert llm_admin.annotate([_call("a", "r1", stop="end_turn")])[0]["failed"] is False


# ── exchange grouping ─────────────────────────────────────────────────────────
def test_calls_sharing_a_request_id_collapse_into_one_exchange():
    rows = [_call("c3", "r1", ms=100), _call("c2", "r1", ms=200), _call("c1", "r1", ms=300)]
    ex = llm_admin.group_exchanges(rows)
    assert len(ex) == 1
    assert ex[0]["calls"] == 3 and ex[0]["duration_ms"] == 600
    assert ex[0]["tokens"] == 3600


def test_separate_request_ids_stay_separate_exchanges():
    rows = [_call("a", "r1"), _call("b", "r2")]
    assert len(llm_admin.group_exchanges(rows)) == 2


def test_one_failing_call_marks_the_whole_exchange_failed():
    rows = [_call("a", "r1", stop="end_turn"), _call("b", "r1", stop="api_error")]
    ex = llm_admin.group_exchanges(rows)
    assert ex[0]["failed"] is True and "api_error" in ex[0]["stop_reasons"]


def test_exchange_cost_is_the_sum_of_its_calls():
    rows = [_call("a", "r1"), _call("b", "r1")]
    ex = llm_admin.group_exchanges(rows)[0]
    one = llm_admin.call_cost_usd(rows[0])
    assert ex["cost_usd"] == pytest.approx(one * 2, rel=1e-6)


def test_exchanges_are_ordered_newest_first():
    rows = [_call("new", "r-new", created="2026-07-30T12:00:00+00:00"),
            _call("old", "r-old", created="2026-07-29T12:00:00+00:00")]
    assert [e["request_id"] for e in llm_admin.group_exchanges(rows)] == ["r-new", "r-old"]


def test_a_call_with_no_request_id_still_forms_its_own_exchange():
    # Never silently drop a row from an operator view.
    ex = llm_admin.group_exchanges([{"id": "solo", "model": MODEL, "operation": "draft"}])
    assert len(ex) == 1 and ex[0]["calls"] == 1


def test_stop_reasons_are_deduped_within_an_exchange():
    rows = [_call("a", "r1", stop="api_error"), _call("b", "r1", stop="api_error")]
    assert llm_admin.group_exchanges(rows)[0]["stop_reasons"] == ["api_error"]


# ── totals + filters ──────────────────────────────────────────────────────────
def test_totals_count_calls_tokens_and_failures():
    rows = [_call("a", "r1"), _call("b", "r2", stop="api_error")]
    t = llm_admin.totals(rows)
    assert t["calls"] == 2 and t["tokens"] == 2400 and t["failed"] == 1 and t["cost_usd"] > 0


def test_totals_of_an_empty_slice_are_zero_not_an_error():
    assert llm_admin.totals([]) == {"calls": 0, "tokens": 0, "cost_usd": 0.0, "failed": 0, "cost_complete": True}


def test_operations_lists_what_is_visible_sorted_and_deduped():
    rows = [_call("a", "r1", op="draft"), _call("b", "r2", op="concierge"),
            _call("c", "r3", op="draft"), _call("d", "r4", op="")]
    assert llm_admin.operations(rows) == ["concierge", "draft"]


def test_has_body_detects_a_full_mode_row():
    assert llm_admin.has_body(_call("a", "r1")) is False
    assert llm_admin.has_body(_call("a", "r1", request={"messages": []})) is True


# ── the pages ─────────────────────────────────────────────────────────────────
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(admin_mod, "_unseen", lambda *a, **k: 0)
    monkeypatch.setattr(admin_mod, "_pending_os", lambda *a, **k: 0)
    app.dependency_overrides[require_admin] = lambda: AdminCtx(user_id="u1", email="ops@blam8.com", role="owner")
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_llm_page_renders_exchanges_and_totals(client, monkeypatch):
    monkeypatch.setattr(admin_mod.db, "list_llm_calls", lambda *a, **k: [
        _call("c1", "r1", op="concierge"), _call("c2", "r1", op="concierge")])
    r = client.get("/admin/llm")
    assert r.status_code == 200
    assert "Exchanges" in r.text and "concierge" in r.text
    assert "1 exchange" in r.text            # two calls, one exchange


def test_llm_page_survives_an_empty_log(client, monkeypatch):
    monkeypatch.setattr(admin_mod.db, "list_llm_calls", lambda *a, **k: [])
    r = client.get("/admin/llm")
    assert r.status_code == 200 and "No calls logged" in r.text


def test_unknown_attempt_detail_says_unknown_not_zero(client, monkeypatch):
    row = _call("c1", "r1", inp=0, out=0, raw_usage={
        "attempt_id": "attempt-1", "usage": None, "cost_usd": None, "complete": False})
    monkeypatch.setattr(admin_mod.db, "get_llm_call", lambda *a, **k: row)
    monkeypatch.setattr(admin_mod.db, "list_llm_calls", lambda *a, **k: [row])
    response = client.get("/admin/llm/c1")
    assert response.status_code == 200
    assert response.text.count("Unknown") >= 3
    assert "$0.000000" not in response.text


def test_llm_page_clamps_a_silly_row_limit(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(admin_mod.db, "list_llm_calls",
                        lambda *a, **k: seen.update(k) or [])
    client.get("/admin/llm?limit=99999")
    assert seen["limit"] == 500
    client.get("/admin/llm?limit=notanumber")
    assert seen["limit"] == 100


def test_llm_page_passes_the_filters_through(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(admin_mod.db, "list_llm_calls",
                        lambda tenant=None, **k: seen.update(tenant=tenant, **k) or [])
    client.get("/admin/llm?tenant=t9&operation=draft")
    assert seen["tenant"] == "t9" and seen["operation"] == "draft"


def test_list_page_never_renders_prompt_content(client, monkeypatch):
    # Rows can hold a customer's email; the list must not leak it.
    secret = "PRIVATE-CUSTOMER-SENTENCE"
    monkeypatch.setattr(admin_mod.db, "list_llm_calls", lambda *a, **k: [
        _call("c1", "r1", request={"messages": [{"content": secret}]})])
    assert secret not in client.get("/admin/llm").text


def test_call_detail_hides_the_body_until_asked(client, monkeypatch):
    secret = "PRIVATE-CUSTOMER-SENTENCE"
    row = _call("c1", "r1", request={"messages": [{"content": secret}]})
    monkeypatch.setattr(admin_mod.db, "get_llm_call", lambda *a, **k: row)
    monkeypatch.setattr(admin_mod.db, "list_llm_calls", lambda *a, **k: [row])
    r = client.get("/admin/llm/c1")
    assert r.status_code == 200 and secret not in r.text
    assert "Show prompt" in r.text


def test_call_detail_shows_the_body_on_explicit_opt_in(client, monkeypatch):
    secret = "PRIVATE-CUSTOMER-SENTENCE"
    row = _call("c1", "r1", request={"messages": [{"content": secret}]})
    monkeypatch.setattr(admin_mod.db, "get_llm_call", lambda *a, **k: row)
    monkeypatch.setattr(admin_mod.db, "list_llm_calls", lambda *a, **k: [row])
    r = client.get("/admin/llm/c1?body=1")
    assert secret in r.text and "may include a customer" in r.text


def test_call_detail_says_so_when_no_body_was_stored(client, monkeypatch):
    row = _call("c1", "r1")                       # log mode wasn't 'full'
    monkeypatch.setattr(admin_mod.db, "get_llm_call", lambda *a, **k: row)
    monkeypatch.setattr(admin_mod.db, "list_llm_calls", lambda *a, **k: [row])
    r = client.get("/admin/llm/c1")
    assert "Not stored for this call" in r.text and "Show prompt" not in r.text


def test_call_detail_shows_the_rest_of_the_exchange(client, monkeypatch):
    rows = [_call("c1", "r1"), _call("c2", "r1"), _call("other", "r2")]
    monkeypatch.setattr(admin_mod.db, "get_llm_call", lambda *a, **k: rows[0])
    monkeypatch.setattr(admin_mod.db, "list_llm_calls", lambda *a, **k: rows)
    r = client.get("/admin/llm/c1")
    assert "The whole exchange" in r.text and "2 calls" in r.text
    assert "this call" in r.text                  # the row you're on is marked


def test_unknown_call_is_a_404(client, monkeypatch):
    monkeypatch.setattr(admin_mod.db, "get_llm_call", lambda *a, **k: None)
    assert client.get("/admin/llm/nope").status_code == 404


def test_llm_calls_is_in_the_admin_nav(client, monkeypatch):
    monkeypatch.setattr(admin_mod.db, "list_llm_calls", lambda *a, **k: [])
    assert 'href="/admin/llm"' in client.get("/admin/llm").text


def test_call_detail_renders_when_only_one_side_was_stored(client, monkeypatch):
    """An api_error path can store a response with no request (and vice versa). Piping a
    missing value through tojson raises, so each side is guarded independently."""
    for row in (_call("c1", "r1", request={"messages": []}),
                _call("c1", "r1", response={"error": "boom"})):
        monkeypatch.setattr(admin_mod.db, "get_llm_call", lambda *a, **k: row)
        monkeypatch.setattr(admin_mod.db, "list_llm_calls", lambda *a, **k: [row])
        r = client.get("/admin/llm/c1?body=1")
        assert r.status_code == 200 and "Not stored." in r.text
