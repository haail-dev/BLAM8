"""GPT-Live voice sessions are on the cost ledger, priced by the second."""
import asyncio

import pytest

from app import db, usage
from tests.test_gptlive import live, session, unit  # noqa: F401  (fixture + helpers)


def test_voice_cost_is_per_second_at_the_published_rate():
    assert usage.cost_usd("openai", "gpt-live-1", voice_seconds=60) == pytest.approx(0.05)
    assert usage.cost_usd("openai", "gpt-live-1", voice_seconds=90) == pytest.approx(0.075)
    assert usage.cost_usd("openai", "gpt-live-1", voice_seconds=0) == 0
    # An unknown voice model has no verified price: unknown, never zero.
    assert usage.cost_usd("openai", "gpt-live-9", voice_seconds=60) is None
    assert "gptlive_session" in usage.SOURCE_BY_OPERATION


def _capture(monkeypatch):
    rows = []
    monkeypatch.setattr(db, "insert_usage_event", rows.append)
    return rows


def test_a_confirmed_session_is_a_complete_priced_event(monkeypatch):
    rows = _capture(monkeypatch)
    usage.record_voice_session("t1", "gpt-live-1", 90, confirmed=True)
    row, = rows
    assert (row["provider"], row["model"], row["operation"], row["module"]) == ("openai", "gpt-live-1", "gptlive_session", "concierge")
    assert row["cost_usd"] == pytest.approx(0.075)
    assert row["input_tokens"] == row["output_tokens"] == 0
    raw = row["raw_usage"]
    assert raw["seconds"] == 90 and raw["complete"] is True and raw["final_usage_confirmed"] is True
    assert raw["unit"] == "voice_seconds" and raw["rate_per_minute"] == 0.05
    assert usage.cost_complete(row)


def test_an_unconfirmed_session_is_a_lower_bound_never_zero(monkeypatch):
    rows = _capture(monkeypatch)
    usage.record_voice_session("t1", "gpt-live-1", 0, confirmed=False)       # created, nothing reported
    usage.record_voice_session("t1", "gpt-live-1", 40, confirmed=False)      # last update seen, close not seen
    first, second = rows
    # Creating a session bills the 15 s start charge, so a missing figure is 15 s, not 0.
    assert first["raw_usage"]["seconds"] == 15 and first["cost_usd"] == pytest.approx(0.0125)
    assert second["raw_usage"]["seconds"] == 40
    assert not usage.cost_complete(first) and not usage.cost_complete(second)


def test_metering_never_breaks_a_session(monkeypatch):
    def boom(row):
        raise RuntimeError("database down")
    monkeypatch.setattr(db, "insert_usage_event", boom)
    usage.record_voice_session("t1", "gpt-live-1", 30, confirmed=True)   # must not raise


@pytest.mark.asyncio
async def test_a_closed_session_is_metered_with_the_providers_final_seconds(unit, monkeypatch):
    calls = []
    monkeypatch.setattr(live.usage, "record_voice_session",
                        lambda tenant, model, seconds, confirmed: calls.append((tenant, model, seconds, confirmed)))
    s = session(); live._sessions[s.id] = s; s.reader = asyncio.current_task()
    await live.finish(s)
    assert s.finalized and s.usage_seconds == 12
    assert calls == [("t1", "gpt-live-1", 12, True)]


@pytest.mark.asyncio
async def test_a_close_without_the_providers_ack_is_metered_as_unconfirmed(unit, monkeypatch):
    calls = []
    monkeypatch.setattr(live.usage, "record_voice_session",
                        lambda tenant, model, seconds, confirmed: calls.append((tenant, model, seconds, confirmed)))
    s = session(); s.reader = asyncio.current_task()
    async def silent(raw):
        s.socket.sent.append(raw)
    s.socket.send = silent
    original = asyncio.wait_for
    async def short(awaitable, timeout):
        return await original(awaitable, .02)
    monkeypatch.setattr(live.asyncio, "wait_for", short)
    await live.finish(s)
    assert not s.finalized and calls == [("t1", "gpt-live-1", 0, False)]
