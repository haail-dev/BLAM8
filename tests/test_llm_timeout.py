"""Every Anthropic client carries the 90 s per-call timeout (app/llm.CALL_TIMEOUT_S).
Live 2026-09-03: a drafting turn ran 168 s emitting a lone "{" until max_tokens
stopped it; the SDK default of 600 s was the only other bound."""
import pytest

from app import agent, concierge, llm, planner, portal_chat
from app.config import settings


@pytest.mark.parametrize("mod", [agent, concierge, planner, portal_chat],
                         ids=lambda m: m.__name__)
def test_clients_are_built_with_the_call_timeout(mod, monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test")
    monkeypatch.setattr(mod, "_client", None)
    c = mod.client()
    assert float(c.timeout) == llm.CALL_TIMEOUT_S == 90.0
    assert c.max_retries >= 1          # a timed-out stall is re-asked, not fatal
