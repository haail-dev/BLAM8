"""FAQ scenario box — POST /concierge/scenario.

Pins the security layers (each is its own dependency, so each is tested alone):
kill switch, same-origin, CSRF header, honeypot, site-wide ceiling (in memory and
durable), per-IP burst and daily allowance; plus the input cleaning, the
off-topic path, and that a model failure never surfaces as a 500. The model is
stubbed — no network.

Run: python -m pytest tests/test_scenario_box.py -q
"""
import pytest
from fastapi.testclient import TestClient

import app.concierge_routes as routes
from app import concierge, security
from app.config import settings
from app.main import app

TOKEN = "t" * 40
Q = "I have three staff and we work seven days a week. Would it overload my team?"


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
