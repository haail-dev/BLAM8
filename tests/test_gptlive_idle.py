"""GPT-Live idle close: the setting, the 'idle' close reason and what the server records.

The timer itself lives in the browser (src/app/static/voice-gptlive.js, tests/replays/gptlive_replay.mjs);
the server only says how long a conversation may sit idle, accepts the close the browser sends
when that runs out, and records how many seconds it was idle. Nothing here carries speech.
"""
import asyncio
import json
import uuid

import pytest
from pydantic import ValidationError

from app import gptlive as live
from app.config import Settings
from tests.test_gptlive import Socket, session, unit  # noqa: F401  (unit is a fixture)


def _create_mocks(monkeypatch):
    monkeypatch.setattr(live, "create_provider", lambda sdp: asyncio.sleep(0, result=("live_idle", "v=answer")))
    monkeypatch.setattr(live, "attach", lambda provider: asyncio.sleep(0, result=Socket()))
    monkeypatch.setattr(live, "authorized", lambda *a, **k: asyncio.sleep(0))


def test_idle_close_seconds_defaults_to_sixty_and_is_set_from_the_environment(monkeypatch):
    monkeypatch.delenv("GPTLIVE_IDLE_CLOSE_SECONDS", raising=False)
    assert Settings(_env_file=None).gptlive_idle_close_seconds == 60
    monkeypatch.setenv("GPTLIVE_IDLE_CLOSE_SECONDS", "0")
    assert Settings(_env_file=None).gptlive_idle_close_seconds == 0
    monkeypatch.setenv("GPTLIVE_IDLE_CLOSE_SECONDS", "90")
    assert Settings(_env_file=None).gptlive_idle_close_seconds == 90


@pytest.mark.parametrize("raw", ["abc", "", "60s", "1e2", "  ", "1.5", "six"])
def test_a_malformed_idle_setting_does_not_stop_the_app(monkeypatch, caplog, raw):
    """An operator editing the environment at midnight must not be able to stop the app starting."""
    monkeypatch.setenv("GPTLIVE_IDLE_CLOSE_SECONDS", raw)
    with caplog.at_level("WARNING"):
        assert Settings(_env_file=None).gptlive_idle_close_seconds == 60
    assert any("GPTLIVE_IDLE_CLOSE_SECONDS" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("raw,expected", [(" 90 ", 90), ("0", 0), ("1800", 1800), ("-5", -5)])
def test_a_sensible_idle_setting_is_read_as_given_and_not_complained_about(monkeypatch, caplog, raw, expected):
    monkeypatch.setenv("GPTLIVE_IDLE_CLOSE_SECONDS", raw)
    with caplog.at_level("WARNING"):
        assert Settings(_env_file=None).gptlive_idle_close_seconds == expected
    assert not [r for r in caplog.records if "GPTLIVE_IDLE_CLOSE_SECONDS" in r.getMessage()]


@pytest.mark.parametrize("configured,expected", [(60, 60), (0, 0), (-5, 0), (45, 45), (10 ** 6, live.LIFETIME)])
def test_idle_close_seconds_is_bounded(monkeypatch, configured, expected):
    monkeypatch.setattr(live.settings, "gptlive_idle_close_seconds", configured)
    assert live.idle_close_seconds() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [60, 0, 120])
async def test_create_tells_the_browser_how_long_a_conversation_may_sit_idle(unit, monkeypatch, configured):
    monkeypatch.setattr(live.settings, "gptlive_idle_close_seconds", configured)
    _create_mocks(monkeypatch)
    result = await live.create(live.Create(sdp="v=offer", voice_session=uuid.uuid4()), unit)
    assert result["idle_close_seconds"] == configured


@pytest.mark.asyncio
async def test_idle_close_records_the_seconds_idle_and_nothing_spoken(unit, monkeypatch):
    s = session(); live._sessions[s.id] = s; traces = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append(kw))
    body = live.Control(session=s.id, operation="close", close_reason="idle", idle_seconds=60,
                        startup_stage="ready", peer_state="connected", channel_state="open")
    result = await live.control(body, unit)
    assert result["ok"] is True and s.closed
    reported = [t for t in traces if t.get("outcome") == "idle"]
    assert len(reported) == 1 and reported[0]["detail"]["idle_seconds"] == 60
    assert s.close_reason == "local_close"
    assert s.terminal["client_reason"] == "idle" and s.terminal["idle_seconds"] == 60
    session_rows = [t for t in traces if "first_terminal" in t.get("detail", {})]
    assert session_rows and session_rows[-1]["detail"]["first_terminal"]["idle_seconds"] == 60


@pytest.mark.asyncio
async def test_idle_close_is_recorded_even_when_the_session_is_already_gone(unit, monkeypatch):
    voice = str(uuid.uuid4()); traces = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append(kw))
    await live.control(live.Control(session=voice, operation="close", close_reason="idle", idle_seconds=61), unit)
    reported = [t for t in traces if t.get("outcome") == "idle"]
    assert len(reported) == 1 and reported[0]["detail"]["idle_seconds"] == 61


@pytest.mark.asyncio
async def test_idle_seconds_is_kept_only_for_an_idle_close(unit, monkeypatch):
    s = session(); live._sessions[s.id] = s; traces = []
    monkeypatch.setattr(live.trace, "emit", lambda *a, **kw: traces.append(kw))
    await live.control(live.Control(session=s.id, operation="close", close_reason="end", idle_seconds=60), unit)
    assert "idle_seconds" not in json.dumps(traces) and "idle_seconds" not in s.terminal


@pytest.mark.parametrize("bad", [-1, 86401, 1.5, "60", True])
def test_idle_seconds_must_be_a_small_whole_number(bad):
    assert live.Control(session="owned", operation="close", close_reason="idle", idle_seconds=60).idle_seconds == 60
    with pytest.raises(ValidationError):
        live.Control(session="owned", operation="close", close_reason="idle", idle_seconds=bad)


def test_an_older_browser_without_idle_seconds_still_closes():
    body = live.Control(session="owned", operation="close", close_reason="idle")
    assert body.idle_seconds is None


def test_idle_close_client_clock_wiring_and_close_report():
    import pathlib, subprocess
    root = pathlib.Path(__file__).resolve().parents[1]
    result = subprocess.run(["node", "tests/replays/gptlive_idle.mjs"], cwd=root, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
