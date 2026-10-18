"""Content-free provider receipts. Missing counts remain unknown."""
from __future__ import annotations

FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens",
          "cache_creation_input_tokens")
COST_VERSION = "anthropic-standard-global-2026-10-14"
SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
# GPT-Live voice sessions are priced by the second, not by token.
VOICE_COST_VERSION = "openai-gpt-live-1-2026-10-14"
VOICE_SOURCE = "https://developers.openai.com/api/docs/models/gpt-live-1"


def raw_usage(value):
    if value is None:
        return None
    get = value.get if isinstance(value, dict) else lambda k: getattr(value, k, None)
    out = {k: get(k) for k in FIELDS}
    creation = get("cache_creation")
    if creation is not None:
        read = creation.get if isinstance(creation, dict) else lambda k: getattr(creation, k, None)
        out["cache_creation"] = {k: read(k) for k in
                                 ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")}
    return out


class Totals(dict):
    """Keep the existing numeric interface; receipts travel beside it."""
    def __init__(self):
        super().__init__(dict.fromkeys(FIELDS, 0))
        self.attempts = []


def add(totals, value):
    attempts = receipts(value)
    if attempts is not None:
        for k in FIELDS:
            totals[k] += sum((a.get("usage") or {}).get(k) or 0 for a in attempts)
    else:
        for k in FIELDS:
            totals[k] += (value.get(k) if isinstance(value, dict) else getattr(value, k, 0)) or 0
    if isinstance(totals, Totals):
        totals.attempts.extend(attempts if attempts is not None else
                               [{"usage": raw_usage(value), "complete": True}])


def receipts(value):
    return (value.attempts if isinstance(value, Totals) else
            (value.get("_blam8_attempts") if isinstance(value, dict) else
             getattr(value, "_blam8_attempts", None)))


def price(model, receipt):
    from app.usage import cost_usd
    u = receipt.get("usage")
    if not isinstance(u, dict) or any(u.get(k) is None for k in FIELDS):
        return None
    split = u.get("cache_creation") or {}
    return cost_usd("anthropic", model or "",
                    input_tokens=u["input_tokens"], output_tokens=u["output_tokens"],
                    cache_read=u["cache_read_input_tokens"],
                    cache_write=u["cache_creation_input_tokens"],
                    cache_write_5m=split.get("ephemeral_5m_input_tokens"),
                    cache_write_1h=split.get("ephemeral_1h_input_tokens"))


def envelope(attempts):
    known = [a.get("cost_usd") for a in attempts]
    return {"attempts": attempts, "known_cost_usd": sum(c for c in known if c is not None),
            "complete": all(a.get("complete") and a.get("cost_usd") is not None for a in attempts)}
