"""Durable proposal receipts, with conservative interpretation of legacy rows.

Stored `executed` means API `completed`. An observation of domain state is
not proof that all effects of this operation completed. Never redispatch here.
"""
from datetime import datetime, timezone

STALE_SECONDS = 1800
UNKNOWN = "The outcome isn't confirmed. Work may have happened — check the record before taking another action."


def _dict(value):
    return value if isinstance(value, dict) else {}


def _items(result):
    items = result.get("results", [])
    if not isinstance(items, list):
        return None
    seen = set()
    for item in items:
        if (not isinstance(item, dict) or type(item.get("index")) is not int or item["index"] < 0
                or item["index"] in seen or type(item.get("ok")) is not bool
                or not isinstance(item.get("summary"), str) or not item["summary"].strip()
                or not isinstance(item.get("state"), str) or item["state"] not in {"completed", "failed", "unknown"}
                or item["ok"] != (item["state"] == "completed")):
            return None
        seen.add(item["index"])
    return items


def _versioned(row, result):
    execution = _dict(result.get("execution"))
    return (execution.get("version") == 2 and isinstance(execution.get("id"), str) and bool(execution["id"])
            and isinstance(row.get("id"), str) and bool(row["id"])
            and execution.get("operation_id") == row.get("id") and isinstance(execution.get("input"), dict))


def state(row):
    row = _dict(row)
    status, result = row.get("status"), _dict(row.get("result"))
    if not isinstance(status, str):
        return "unknown"
    if status in {"executed", "failed"}:
        summary = result.get("summary")
        expected = "completed" if status == "executed" else "failed"
        items = _items(result)
        if (_versioned(row, result) and _dict(result.get("execution")).get("state") == expected
                and isinstance(summary, str) and summary.strip() and not result.get("error")
                and result.get("state", expected) == expected and items is not None
                and not any(item["state"] == "unknown" for item in items)
                and (expected != "failed" or not any(item["ok"] for item in items))
                and (expected != "completed" or result.get("ok", True) is True)):
            return expected
        # Legacy receipts lacked a terminal execution marker. Summary wording
        # is not causal evidence (and can describe partial delivery/failure).
        # Retain the original audit data, but require manual inspection.
        return "unknown"
    if status == "executing":
        if not _versioned(row, result) or _dict(result.get("execution")).get("state") != "executing":
            return "unknown"
        started = _dict(result.get("execution")).get("started_at")
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(started.replace("Z", "+00:00"))).total_seconds()
            if age > STALE_SECONDS:
                return "unknown"
        except (AttributeError, TypeError, ValueError):
            return "unknown"
    return status if status in {"pending", "executing", "unknown", "cancelled", "superseded", "expired"} else "missing"


def domain_link(row):
    """Safe navigation for manual inspection; no claim of causal evidence."""
    from urllib.parse import quote
    kind, payload = str(row.get("kind") or ""), _dict(row.get("payload"))
    if kind.startswith("invoice_") and payload.get("invoice_id"):
        return {"label": "Check the invoice", "href": "/app/invoices/" + quote(str(payload["invoice_id"]), safe="")}
    if kind.startswith("quote_") and payload.get("quote_id"):
        return {"label": "Check the quote", "href": "/app/quotes/" + quote(str(payload["quote_id"]), safe="")}
    if payload.get("contact") and "@" in str(payload["contact"]):
        return {"label": "Check the thread", "href": "/app/conversations/view?contact=" + quote(str(payload["contact"]))}
    if "booking" in kind or kind in {"block_date", "unblock_date"}:
        return {"label": "Check the diary", "href": "/app/diary"}
    return {"label": "Check your records", "href": "/app"}


def public(row):
    """Expose receipts, never accepted edits, raw payload or provider errors."""
    row = _dict(row)
    current = state(row)
    result = _dict(row.get("result"))
    summary = {
        "executing": "This action is still running. Check its result shortly; don't repeat it.",
        "unknown": UNKNOWN,
        "pending": "This proposal hasn't been claimed. You can confirm this same card.",
        "cancelled": "This proposal was cancelled before execution.",
        "superseded": "This proposal was replaced before execution.",
        "expired": "This proposal expired before execution.",
        "missing": "That proposal isn't available.",
    }.get(current)
    if current in {"completed", "failed"}:
        summary = result.get("summary") or ("Done." if current == "completed" else "The action was refused before dispatch.")
    link = result.get("link") if isinstance(result.get("link"), dict) else None
    if link and (not str(link.get("href") or "").startswith("/") or str(link.get("href")).startswith("//")):
        link = None
    if link:
        link = {"href": str(link["href"]), "label": str(link.get("label") or "Open record")}
    return {"ok": current == "completed", "state": current,
            "proposal_id": row.get("id"), "summary": summary or UNKNOWN,
            "link": link or (domain_link(row) if current in {"unknown", "executing"} else None),
            "results": ([{key: item[key] for key in ("index", "ok", "state", "summary")} for item in _items(result)]
                        if _items(result) is not None and _items(result) else None)}


def receipt(claimed, current, result=None):
    execution = dict(_dict(_dict(claimed.get("result")).get("execution")))
    execution.update(state=current, finished_at=datetime.now(timezone.utc).isoformat())
    return {**(result or {}), "execution": execution}
