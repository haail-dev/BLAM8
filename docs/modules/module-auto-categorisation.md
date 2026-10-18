# Auto-categorisation — smart inbox labels

Shipped 2026-07-26. Every inbound gets a category the moment BLAM8 handles it;
the inbox pills filter by it, each row wears one coloured chip, and the
Concierge can query it (`search_conversations.category`).

## The six labels

| Key | Chip | Meaning |
|---|---|---|
| needs_reply | red | a human must act (new/undrafted, escalation, knowledge gap, deferral) |
| draft_ready | blue | a draft is waiting in the approval queue |
| quote_sent | green | latest reply sent — ball with the customer |
| booking_pending | amber | an upcoming booking is held or confirmed |
| fyi | grey | informational — receipt/newsletter/confirmation, no action |
| automated | slate | system no-reply mail, correctly skipped by the ingestion filter |

One primary label per conversation — the latest `email_log` row wins, exactly
like `thread_state`. Labels/colours/glyphs live in `dashboard.CATEGORY_META`.

## Where each piece lives

- **Storage** — `email_log.category / category_reason / category_source`
  (schema.sql marker `-- Auto-categorisation — smart inbox labels (2026-07-26)`,
  applied to prod via `scripts/db/migrations/apply_category_migration.py`). Legacy rows stay
  null; `dashboard._conversation_category` derives a label from
  status/thread_state, so there is NO backfill.
- **Classification** — zero extra LLM calls: `agent.DRAFT_SCHEMA` gained
  `category` (enum `needs_reply|fyi|automated` — the model only ever judges the
  INBOUND) + `category_reason` (prompt rule 8). A malformed answer clamps to
  needs_reply: over-flagging is the safe direction.
- **Transitions** (`pipeline._classify` + the choke points):
  - ingest → needs_reply (escalation/gap/deferral/needs_attention) |
    booking_pending (hold placed) | fyi/automated (clean informational call) |
    draft_ready (everything else);
  - an **informational** inbound stores its draft but lands `status='skipped'` —
    no approval-queue entry, no push, no auto-send; a wrong call is one Redraft
    away from a normal queue entry;
  - a **no-reply system sender** now INSERTS a row (status skipped, category
    automated) instead of vanishing — the inbox can show it was seen and left
    alone; still never drafted, deduped by source_message_id;
  - `db.mark_email_log_sent` → quote_sent (human + auto sends);
  - `db.resolve_holds_for_log('confirmed')` → booking_pending;
  - dashboard skip → fyi ("you decided no reply was needed");
  - redraft → recomputed (never informational — a redraft is the owner asking
    for a reply).
- **Bookings are ground truth at read time** (no date-passed sweep):
  `list_conversations` marks `has_upcoming_booking` from proposed+confirmed
  future bookings; the display upgrade/downgrade (quote_sent ⇄ booking_pending)
  happens in `_conversation_category`. Manual overrides skip both adjustments.
- **Manual override** — the thread-header chip is a menu → POST
  `/app/conversations/category` stamps the latest row with
  `category_source='manual'`; the next pipeline event restamps it.
- **UI decisions** (spec left these to the implementer):
  - the category pills REPLACED the old Attention/In-progress state pills
    (`?cat=` query param; the rose row-tint still carries the attention signal,
    `thread_state` itself is unchanged and still stored);
  - the row's category chip absorbed ✓ Draft / ✓✓ Sent / Resolved (same facts,
    now in colour); glyph-only on mobile;
  - "All" HIDES automated (bookkeeping, not inbox); the FYI/Automated pills
    appear only once they have rows.

## Tests

`tests/test_auto_categorisation.py` (schema, _classify precedence, no-reply
logging, derivations, routes, override, concierge pass-through) +
`test_inbox_mobile_chips.py` / `test_multi_mailbox.py` updated.
