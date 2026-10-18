"""Operator view over llm_calls — every Anthropic call, grouped into exchanges.

`llm_calls` is the first diagnostic stop when the assistant misbehaves (the launch-day
structured-output outage was found there), but until now reading it meant SQL. This is
the page. Pure functions over rows so the grouping is testable without a database.

Why exchanges rather than calls: one question from an owner is usually SEVERAL API
calls — the model asks for a tool, we run it, the model answers. `request_id` ties
those together (src/app/llm.py stamps it from a contextvar), so the honest unit of "what
did this cost and how long did the user wait" is the exchange, not the row.

Costs use versioned, content-free attempt receipts. Historical rows expose only
known input/output subtotals, labelled incomplete; no cache split is inferred.

⚠️ Rows can carry the full request/response including customer email bodies (log mode
'full'). Nothing in the list view renders a body, and the detail view hides them behind
an explicit opt-in — an operator should have to choose to read a customer's mail.
"""
from __future__ import annotations

from app import usage

PROVIDER = "anthropic"

# stop_reasons worth surfacing as a problem rather than a normal ending.
BAD_STOP_REASONS = ("api_error", "max_tokens", "refusal")


def _i(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def call_cost_usd(row: dict) -> float | None:
    """Known attempt cost; None means no verified arithmetic is possible."""
    if isinstance(row.get("raw_usage"), dict):
        return row["raw_usage"].get("cost_usd")
    if row.get("input_tokens") is None or row.get("output_tokens") is None:
        return None
    return usage.cost_usd(PROVIDER, row.get("model") or "",
                          input_tokens=_i(row.get("input_tokens")),
                          output_tokens=_i(row.get("output_tokens")))


def annotate(rows: list[dict]) -> list[dict]:
    """Add derived display fields to each call row, without mutating the input."""
    out = []
    for r in rows:
        out.append({**r,
                    "cost_usd": round(call_cost_usd(r) or 0, 6),
                    "cost_known": call_cost_usd(r) is not None,
                    "cost_complete": bool((r.get("raw_usage") or {}).get("complete")
                                          and call_cost_usd(r) is not None),
                    "tokens": (_i(r.get("input_tokens")) + _i(r.get("output_tokens"))
                               + sum(_i(((r.get("raw_usage") or {}).get("usage") or {}).get(k))
                                     for k in ("cache_read_input_tokens", "cache_creation_input_tokens"))),
                    "source": usage.source_for(r.get("module"), r.get("operation")),
                    "failed": (r.get("stop_reason") or "") in BAD_STOP_REASONS})
    return out


def group_exchanges(rows: list[dict]) -> list[dict]:
    """Collapse calls into exchanges by request_id, newest first.

    The exchange's operation/module is taken from its FIRST call (the one the user
    actually triggered); later calls in a tool loop repeat the same operation anyway.
    A single failing call marks the whole exchange failed — that is what an operator
    is scanning for.
    """
    groups: dict[str, dict] = {}
    for r in annotate(rows):
        key = r.get("request_id") or r.get("id") or ""
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "request_id": key, "tenant_id": r.get("tenant_id"),
                "operation": r.get("operation") or "—", "module": r.get("module"),
                "source": r["source"], "model": r.get("model") or "—",
                "calls": 0, "tokens": 0, "cost_usd": 0.0, "duration_ms": 0,
                "started_at": r.get("created_at"), "last_at": r.get("created_at"),
                "failed": False, "stop_reasons": [], "cost_complete": True,
            }
        g["cost_complete"] = g["cost_complete"] and r["cost_complete"]
        g["calls"] += 1
        g["tokens"] += r["tokens"]
        g["cost_usd"] = round(g["cost_usd"] + r["cost_usd"], 6)
        g["duration_ms"] += _i(r.get("duration_ms"))
        g["failed"] = g["failed"] or r["failed"]
        if r.get("stop_reason") and r["stop_reason"] not in g["stop_reasons"]:
            g["stop_reasons"].append(r["stop_reason"])
        # rows arrive newest-first, so the LAST one seen is the earliest call
        g["started_at"] = r.get("created_at") or g["started_at"]
    return sorted(groups.values(), key=lambda g: g.get("last_at") or "", reverse=True)


def totals(rows: list[dict]) -> dict:
    """Page footer: what this slice cost, and how much of it went wrong."""
    ann = annotate(rows)
    return {"cost_complete": all(r["cost_complete"] for r in ann), "calls": len(ann),
            "tokens": sum(r["tokens"] for r in ann),
            "cost_usd": round(sum(r["cost_usd"] for r in ann), 4),
            "failed": sum(1 for r in ann if r["failed"])}


def operations(rows: list[dict]) -> list[str]:
    """Distinct operations in this slice, for the filter dropdown. Derived from the
    page rather than a DISTINCT query — the filter only needs to offer what's visible."""
    return sorted({(r.get("operation") or "").strip() for r in rows if r.get("operation")})


def has_body(row: dict) -> bool:
    """Whether this row was logged in 'full' mode and therefore holds prompt content."""
    return bool(row.get("request") or row.get("response"))
