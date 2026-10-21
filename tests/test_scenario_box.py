"""FAQ scenario box — POST /concierge/scenario.

Pins the security layers (each is its own dependency, so each is tested alone):
kill switch, same-origin, CSRF header, honeypot, site-wide ceiling (in memory and
durable), per-IP burst and daily allowance; plus the input cleaning, the
off-topic path, and that a model failure never surfaces as a 500. Also pins the
privacy promise: the scenario call logs in meta mode, so no visitor text reaches
llm_calls, while the envelope row the durable ceiling counts is still written.
The model is stubbed — no network.

Run: python -m pytest tests/test_scenario_box.py -q
"""
import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.concierge_routes as routes
from app import concierge, db, llm, security
from app.config import settings
from app.main import app

TOKEN = "t" * 40
Q = "I have three staff and we work seven days a week. Would it overload my team?"
# The autouse fixture below swaps scenario_answer for a stub; the logging tests
# at the end exercise the real function, so keep a handle to it from import time.
_real_scenario_answer = concierge.scenario_answer


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    security._BUCKETS.clear()
    routes._scenario_seen.clear()
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "scenario_box_enabled", True)
    monkeypatch.setattr(settings, "scenario_global_daily", 150)
    monkeypatch.setattr(routes.db, "count_llm_calls_since", lambda op, since: 0, raising=False)
    calls = []

    async def fake(question):
        calls.append(question)
        return "It books against the capacity you set.", True, None, "m"

    monkeypatch.setattr(concierge, "scenario_answer", fake)
    monkeypatch.setattr(routes.usage, "record_anthropic", lambda *a, **k: None)
    yield calls
    security._BUCKETS.clear()
    routes._scenario_seen.clear()


def post(body=None, *, origin="http://testserver", csrf=True, cookies=True):
    c = TestClient(app)
    if cookies:
        c.cookies.set("bl_csrf", TOKEN)
    headers = {}
    if origin is not None:
        headers["Origin"] = origin
    if csrf:
        headers["X-CSRF-Token"] = TOKEN
    return c.post("/concierge/scenario", json=body if body is not None else {"question": Q},
                  headers=headers)


def test_happy_path_returns_reply_and_no_store(_clean):
    r = post()
    assert r.status_code == 200
    assert r.json() == {"reply": "It books against the capacity you set.", "ok": True}
    assert r.headers["cache-control"] == "no-store"
    assert _clean == [Q]


def test_kill_switch_404s(monkeypatch):
    monkeypatch.setattr(settings, "scenario_box_enabled", False)
    assert post().status_code == 404


@pytest.mark.parametrize("origin", [None, "https://evil.example", "http://testserver.evil.com"])
def test_wrong_or_missing_origin_is_refused(origin, _clean):
    assert post(origin=origin).status_code == 403
    assert _clean == []


def test_csrf_header_required(_clean):
    assert post(csrf=False).status_code == 403
    assert post(cookies=False).status_code == 403
    assert _clean == []


def test_honeypot_never_reaches_the_model(_clean):
    r = post({"question": Q, "website": "http://spam"})
    assert r.status_code == 200 and r.json()["ok"] is False
    assert _clean == []


def test_length_cap_and_unknown_fields_rejected(_clean):
    for body in ({"question": "x" * 401}, {"question": Q, "extra": 1}, {"question": ""}):
        security._BUCKETS.clear()                      # each try is a fresh visitor
        assert post(body).status_code == 422
    assert _clean == []


def test_too_short_after_cleaning_is_off_topic_without_the_model(_clean):
    r = post({"question": "  hi \x00\x01  "})
    assert r.json()["ok"] is False and _clean == []


def test_clean_scenario_strips_controls_and_collapses_space():
    assert concierge.clean_scenario("a\x00b\n\n  c\t d") == "a b c d"
    assert len(concierge.clean_scenario("z" * 1000)) == concierge.SCENARIO_MAX_CHARS


def test_off_topic_reply_is_replaced_with_the_canned_line(monkeypatch):
    async def off(q):
        return "", False, None, "m"
    monkeypatch.setattr(concierge, "scenario_answer", off)
    r = post()
    assert r.json() == {"reply": concierge.SCENARIO_OFF_TOPIC, "ok": False}


def test_model_failure_is_a_friendly_200_not_a_500(monkeypatch):
    async def boom(q):
        raise RuntimeError("model down")
    monkeypatch.setattr(concierge, "scenario_answer", boom)
    r = post()
    assert r.status_code == 200 and r.json()["ok"] is False
    assert "hello@blam8.com" in r.json()["reply"]


def test_burst_then_daily_allowance_per_ip(monkeypatch):
    monkeypatch.setattr(security.time, "monotonic", lambda: 1000.0)
    assert post().status_code == 200
    assert post().status_code == 429                   # burst: 1 per 20 s
    monkeypatch.setattr(security.time, "monotonic", lambda: 1100.0)
    assert post().status_code == 200                   # second of the day
    monkeypatch.setattr(security.time, "monotonic", lambda: 1200.0)
    r = post()
    assert r.status_code == 429                        # daily allowance (2) spent
    assert r.json()["detail"] != routes._SCENARIO_BUSY


def test_in_memory_site_ceiling(monkeypatch, _clean):
    monkeypatch.setattr(settings, "scenario_global_daily", 1)
    assert post().status_code == 200
    security._BUCKETS.clear()                          # a fresh IP
    r = post()
    assert r.status_code == 429 and r.json()["detail"] == routes._SCENARIO_BUSY
    assert len(_clean) == 1


def test_durable_site_ceiling_survives_a_restart(monkeypatch, _clean):
    monkeypatch.setattr(settings, "scenario_global_daily", 5)
    monkeypatch.setattr(routes.db, "count_llm_calls_since", lambda op, since: 5)
    r = post()
    assert r.status_code == 429 and r.json()["detail"] == routes._SCENARIO_BUSY
    assert _clean == []


def test_a_storage_error_falls_back_to_the_in_memory_cap(monkeypatch):
    def down(op, since):
        raise RuntimeError("db down")
    monkeypatch.setattr(routes.db, "count_llm_calls_since", down)
    assert post().status_code == 200


def test_refused_requests_do_not_spend_the_allowance(monkeypatch):
    for _ in range(5):
        assert post(origin="https://evil.example").status_code == 403
    monkeypatch.setattr(security.time, "monotonic", lambda: 5000.0)
    assert post().status_code == 200


def test_prompt_is_agnostic_and_carries_the_guards():
    p = concierge.build_scenario_prompt(facts="FACT-X", knowledge="KNOW-Y")
    assert "FACT-X" in p and "KNOW-Y" in p
    assert "on_topic" in p and "untrusted" in p and "does NOT do" in p
    assert "BLAM8" not in p.split("<facts>")[0]        # the rules never name the product


# ── Privacy: what the scenario call leaves in the LLM log ────────────────────
# privacy.html promises that the public assistant's prompt and answer content is
# not kept in the LLM call log. answer() honours that with log_mode="meta"; the
# scenario box must too, while still writing the metadata envelope row that
# routes._scenario_ceiling counts (db.count_llm_calls_since on "scenario_ask").
def _fake_message(text='{"reply": "It books against the capacity you set.", "on_topic": true}'):
    usage_ = SimpleNamespace(input_tokens=10, output_tokens=20,
                             cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)],
                           stop_reason="end_turn", model="fake-model", usage=usage_,
                           model_dump=lambda mode=None: {"content": [{"type": "text", "text": text}]})


def test_scenario_call_asks_for_meta_logging(monkeypatch):
    captured = []

    async def fake_create(**kw):
        captured.append(kw)
        return _fake_message()

    monkeypatch.setattr(concierge, "client", lambda: None)
    monkeypatch.setattr(llm, "create", fake_create)
    reply, on_topic, _usage, model = asyncio.run(_real_scenario_answer(Q))
    assert reply == "It books against the capacity you set." and on_topic is True
    assert model == "fake-model"
    (kw,) = captured
    assert kw["operation"] == "scenario_ask" and kw["module"] == "concierge"
    assert kw["log_mode"] == "meta"


def test_scenario_text_is_not_kept_in_the_llm_log_but_the_row_still_counts(monkeypatch):
    rows = []
    monkeypatch.setattr(db, "insert_llm_call", rows.append)
    monkeypatch.setattr(settings, "llm_log_mode", "full")          # the production default
    seen = []

    async def create(**kw):
        seen.append(kw)
        return _fake_message()

    monkeypatch.setattr(concierge, "client", lambda: SimpleNamespace(
        messages=SimpleNamespace(create=create), max_retries=0))
    asyncio.run(_real_scenario_answer(Q))
    (sent,) = seen
    assert Q in json.dumps(sent["messages"])                         # the model saw the scenario
    (row,) = rows                                                    # exactly one envelope row...
    assert row["operation"] == "scenario_ask"                        # ...which the ceiling counts
    assert row["input_tokens"] == 10 and row["stop_reason"] == "end_turn"
    assert "request" not in row and "response" not in row
    assert Q not in json.dumps(row, default=str)                     # no visitor text anywhere

