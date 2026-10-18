"""Smart inbox labels — the ONE derivation of a conversation's category.

The stored email_log.category (stamped by the pipeline since 2026-07-26) is
only half the truth: legacy rows keep NULL and derive a label from the old
status/thread_state signals, bookings adjust at read time, and brand-new
senders folded in straight from the mailbox are Needs Reply before any row
exists. The inbox UI always did all of that app-side — which meant any other
surface querying the raw column saw a DIFFERENT inbox. Live consequence
(2026-07-26): the concierge filtered email_log on category = 'needs_reply',
every row predated the feature (all NULL), and it told the owner "your inbox
is clear" while the inbox page showed six conversations needing attention.

So this module is now the single source of truth: derive_category() is the
label logic, conversation_snapshot() is the whole inbox exactly as the
conversations page computes it. The dashboard AND the concierge (and any
future surface) must answer "what's in the inbox?" from here — never from a
raw category filter.
"""
import asyncio

from app import db, navcache

# One primary label per conversation. Order = the inbox's pill order.
CONVO_CATEGORIES = ("needs_reply", "draft_ready", "quote_sent",
                    "booking_pending", "fyi", "automated")

# The labels that mean "the owner has something to do" — what inbox-status
# questions ('what needs replying to?') are really asking about.
ACTIVE_CATEGORIES = ("needs_reply", "draft_ready", "booking_pending")

# How long a mailbox's inbox listing may be reused (seconds). Short, because it
# is the one signal that arrives from OUTSIDE the app; an inbound the pipeline
# handles busts the key immediately, so this only ever covers mail nothing has
# reacted to yet.
_INBOX_SCAN_TTL = 45.0


def derive_category(c: dict) -> str:
    """The smart inbox label for one conversation row. A stored label (latest
    email_log row, stamped by the pipeline) wins; a legacy row drafted before
    categories existed derives one from the old status/thread_state signals.
    Bookings are ground truth at read time: an upcoming (proposed/confirmed)
    booking keeps a replied thread Booking Pending, and a booking_pending label
    whose date has passed completes to Quote Sent — a manual override skips both
    adjustments (the owner said otherwise; the next pipeline event restamps)."""
    if c.get("state") == "new":
        return "needs_reply"   # brand-new undrafted sender — a first reply is owed
    cat = c.get("category")
    manual = c.get("category_source") == "manual"
    if cat not in CONVO_CATEGORIES:
        manual = False
        ts = c.get("thread_state")
        if ts == "booked":
            cat = "booking_pending"
        elif ts == "needs_attention" or c.get("gap") or c.get("sched_gap"):
            cat = "needs_reply"
        elif c.get("state") == "sent":
            cat = "quote_sent"
        elif c.get("state") in ("pending", "approved"):
            cat = "draft_ready"
        else:
            cat = "fyi"   # decided long ago (skipped/resolved) — nothing to act on
    if manual:
        return cat
    if cat == "quote_sent" and c.get("has_upcoming_booking"):
        return "booking_pending"
    if cat == "booking_pending" and not c.get("has_upcoming_booking"):
        return "quote_sent"
    return cat


async def conversation_snapshot(tenant_id: str, client=None, *,
                                mailbox_id: str | None = None,
                                default_mailbox_id: str | None = None,
                                scan: tuple = (None,)) -> list[dict]:
    """Every conversation exactly as the inbox page sees it, newest-first, each
    wearing its derived `category`: logged conversations (ignored senders
    hidden unless newly received mail records the ignore decision) plus brand-new
    senders folded in straight from the mailbox (an
    inbound the agent hasn't drafted yet is a Needs Reply the owner would see
    on the page — so every other surface must see it too). `scan` is the
    mailbox ids to sweep for new senders ((None,) = the default mailbox;
    enterprise callers pass their tab selection). A mailbox read failure
    degrades to the logged rows — same as the page."""
    from app import mailbox, pipeline   # lazy: keep this module light to import

    async def _inbox_of(box_id):
        """The mailbox's recent inbox, briefly cached. This is a live Graph/Gmail
        call — one round trip for Microsoft, twenty-six for Gmail (list + one
        metadata fetch per message) — and it was paid on EVERY render of the
        Inbox and of its live fragment, which the SSE tick re-fetches. The scan
        only exists to fold in senders the pipeline has not logged yet, and the
        pipeline publishes an event the moment it logs one — which busts this key
        (src/app/events.py). So the window is short and anything real inside it
        arrives through the logged path anyway."""
        key = "t:" + tenant_id + ":inbox:" + (box_id or "-")
        hit, val = navcache.peek(key)
        if hit:
            return val
        try:
            msgs = await mailbox.list_messages(tenant_id, "inbox", mailbox_id=box_id)
        except Exception:
            return []          # a read failure degrades to the logged rows, and is NOT cached
        navcache.put(key, _INBOX_SCAN_TTL, msgs)
        return msgs

    def _logged():
        """The logged half — two Supabase round trips, run in a thread so they
        overlap the mailbox fetch instead of following it, and so they never
        block the event loop of an `async def` caller."""
        ignored = {a.lower() for a in db.ignored_addresses(tenant_id, client=client)}
        return ignored, [
            c for c in db.list_conversations(
                tenant_id, client=client,
                mailbox_id=mailbox_id, default_mailbox_id=default_mailbox_id,
            )
            if c["contact"].lower() not in ignored or (
                c.get("state") == "skipped"
                and c.get("category_reason") == "Sender is on your ignore list")
        ]

    (ignored, convos), *scanned = await asyncio.gather(
        asyncio.to_thread(_logged), *(_inbox_of(b) for b in scan))
    known = {c["contact"].lower() for c in convos}
    new_rows: dict[str, dict] = {}
    for scan_box, inbox_msgs in zip(scan, scanned):
        for m in inbox_msgs:  # newest-first, so the first per sender is the latest
            addr = ((m.get("from") or {}).get("address") or "").lower()
            if not addr or addr in known or addr in ignored or addr in new_rows:
                continue
            if pipeline.is_no_reply_address(addr):
                # System senders never fold in as "New": the pipeline logs them with
                # the Automated label the moment it sees them — a synthetic row here
                # would beg for a reply nobody should write.
                continue
            new_rows[addr] = {
                "contact": addr, "count": 1, "subject": m.get("subject"), "preview": m.get("preview"),
                "state": "new", "gap": None, "last_at": m.get("received"), "new_message_id": m.get("id"),
                "mailbox_id": scan_box,
                # brand-new senders get the From header's display name straight away
                "display_name": (m.get("from") or {}).get("name"),
            }
    convos = sorted(list(new_rows.values()) + convos, key=lambda c: c.get("last_at") or "", reverse=True)
    for c in convos:
        c["category"] = derive_category(c)
    return convos
