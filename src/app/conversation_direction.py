"""Interpret stored message provenance; message text never chooses its role."""

from app.booking_errors import VerificationUnavailable


OUTBOUND_KINDS = frozenset({
    "invoice", "quote", "invoice_reminder", "invoice_overdue", "quote_reminder",
    "quote_expired", "quote_keeps_looking", "reminder", "offer_nudge", "recall",
    "special", "on_my_way", "owner_message", "renewal_request",
    "ready", "sequence", "tenant_outreach", "scheduled_plan", "event_chase", "run_sheet", "hand_cancel",
    "waitlist_offer", "reenrol_window", "level_change", "package_balance",
    "missed_call", "wholesale_order", "wholesale_hold",
})
SELF_LOOP_REASON = "Our own outgoing mail re-arriving — left alone"


def direction(row: dict) -> str:
    """Return inbound, outbound, self_loop or unknown from recorded facts.

    The old self-loop gate recorded its decision in category_reason. Keep that
    exact receipt readable without rewriting history. New receipts use kind.
    from_address is the CONTACT on generated outbound rows, not their sender.
    """
    kind = row.get("kind")
    if kind == "self_loop" or (
        row.get("category") == "automated"
        and row.get("category_reason") == SELF_LOOP_REASON
    ):
        return "self_loop"
    if kind in OUTBOUND_KINDS:
        return "outbound"
    if kind == "inbound":
        return "inbound"
    return "unknown"


def replayable_history(rows: list[dict]) -> list[dict]:
    """Copies for context consumers; stored history and send receipts stay intact.

    Unknown non-empty bodies require owner review. Outbound facts/placeholders
    and self-loop copies cannot be read as customer evidence by any consumer.
    """
    result = []
    for row in rows:
        role = direction(row)
        if role == "unknown" and (row.get("body_in") or "").strip() not in ("", "[redacted]"):
            raise VerificationUnavailable("the direction of earlier messages")
        if role == "self_loop":
            continue
        result.append({**row, "body_in": row.get("body_in") if role == "inbound" else ""})
    return result
