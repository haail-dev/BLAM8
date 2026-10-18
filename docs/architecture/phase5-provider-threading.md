# Phase 5 — Provider-native conversation threading (SCOPE / DRAFT)

> **Status (2026-09-27, confirmed by the clean-up pass):** Option A + 5b BUILT on main (1598ef9, 292a9c5), proven live 2026-06-10. Options B/C (the inbox grouping by thread) NOT built — the inbox still groups by contact.

**Status:** Option A (model-only threading) **IMPLEMENTED** on branch `feature/phase5-threading`
(2026-06-09) — capture the provider thread id at ingest + scope the agent's conversation memory
to it (§Decisions D2). Options B/C (dashboard re-grouping) remain unbuilt; the thread-title tweak
(Q2) was left as Phase 4's current-email subject; no backfill (Q3, as recommended). Origin:
Phase 4 follow-up discussion.

## Goal
Thread conversations on the **email provider's own thread id** (Microsoft Graph
`conversationId`, Gmail `threadId`) instead of grouping everything from a sender together.
This gives us:
- A correct, robust notion of "a conversation" — a new email (new compose) starts a new
  thread even if it reuses a subject; a reply stays in the same thread even if the subject
  is edited. This is what real mail clients thread on, and it's far less fragile than
  matching on subject text (`Re:`/`Fwd:` noise, edited subjects, "reply-to-an-old-email-to-
  start-a-new-topic").
- **Fresh model context per conversation** (the Phase 4 follow-up the user asked for): when a
  customer starts a genuinely new thread, the drafting agent no longer drags in 20 turns of an
  unrelated prior topic. "Clear context for a new conversation" falls out of the keying — the
  old thread's rows stay stored (audit intact); they're just not in the new thread's history.
- A well-defined **thread title**: the originating (oldest) subject of a thread id, which
  Phase 4's system-prompt subject line can use as a stable title instead of the latest subject.

## Why not subject-matching
Considered and rejected as the key. Subject is a weak signal: `Re:`/`Fwd:` prefixes, users
editing the subject mid-thread, and "hit reply on last month's email to ask something new"
all break it — you either split a real conversation (agent loses needed context) or merge two
(the bleed we're trying to stop). The provider thread id has none of these failure modes and
is already present on every message. Its one residual limitation (replying within an existing
thread to raise a new topic stays one thread) is rarer and more defensible — and Phase 4 now
shows the model the subject, so it can still tell the topic shifted.

## Current state (as-is — confirmed in code)
- A "conversation" today = all `email_log` rows for one `(tenant_id, from_address)`.
  `db.get_conversation(tenant_id, contact)` ([src/app/db.py](../../src/app/db.py)) selects rows by
  `from_address`, oldest→newest; it feeds **both** the dashboard thread view
  (`/app/conversations/view`) **and** Phase 4's model history (`pipeline.draft_for_email` /
  `redraft_email_log`).
- `db.list_conversations` aggregates app-side by `from_address` → one row per contact (the
  Inbox thread list); `/app/conversations` also folds in undrafted inbox senders as "New".
- `email_log` columns: `from_address, subject, body_in, draft_reply, status, knowledge_gap,
  source_message_id, created_at` ([db/schema.sql](../../db/schema.sql)). `source_message_id` is
  the inbound **message** id (per-message idempotency key), **not** a thread id.
- **Neither provider's thread id is captured today.** Graph reads `$select` `id,subject,from,
  toRecipients,receivedDateTime,body` (no `conversationId`); Gmail extracts headers + body
  (no `threadId`) — see `src/app/mailbox.py` / `src/app/gmail.py`. Both are one field away.

## Decisions
### D1 — Threading key: a new `email_log.thread_id text` column
Store the provider thread id per row. Nullable. When null (historical rows; any path that
didn't capture it), **fall back to today's `from_address` grouping** — the system degrades
gracefully and old data still threads by contact.

### D2 — Scope of the change (THE call to make before building)
Model-context threading and dashboard-UI threading are separable:

- **Option A — model-only threading (recommended first step).** Scope only the *agent's*
  history to the current thread id; leave the dashboard grouping per-contact (a human seeing
  "everything from Dave" in one place is a reasonable view, and it's the IA we just shipped in
  Phase 3.3). Smallest surface: `get_conversation` gains an optional `thread_id` filter that
  only the pipeline uses. Delivers the actual drafting-quality win with near-zero UI churn and
  is the natural extension of Phase 4 (which is already model-context-only).
- **Option B — thread-based dashboard too.** `list_conversations`, the thread view, and the
  "New" merge all group by thread id. More faithful to email, but reworks the Phase 3.3 IA and
  the new-sender merge, and needs a representative subject/contact per thread. Larger UX change.
- **Option C — hybrid.** Per-contact list at the top level, thread separators within a
  contact's view; model context per thread. Middle effort.

**Recommendation:** ship **A** first (it fixes the problem the user raised), then layer **B**
only if per-thread is wanted in the UI once `thread_id` exists. The rest of this doc specs A,
flagging where B would extend it.

## Schema (one migration, applied directly via psycopg2 — see memory `apply-supabase-migrations-directly`)
Append to `db/schema.sql` (same pattern as the `source_message_id` block):
```sql
-- Phase 5 — provider-native threading. thread_id is the email provider's own
-- conversation id (Graph conversationId / Gmail threadId), captured at ingest so a
-- conversation = one provider thread, not "all mail from a sender". Nullable: historical
-- rows and any uncaptured path keep null and fall back to from_address grouping.
alter table email_log add column if not exists thread_id text;
create index if not exists email_log_tenant_thread_idx
  on email_log (tenant_id, thread_id) where thread_id is not null;
```
**No backfill.** We never stored the provider thread id, and re-fetching every historical
message by `source_message_id` is expensive and unreliable (messages get deleted). Old rows
stay `thread_id = null` → contact-grouping fallback. New inbound captures it going forward; at
pilot volume this converges within days.

## Capture points (ingest → store)
1. **`src/app/mailbox.py` (Graph):** add `conversationId` to the `$select` in `get_message`,
   `fetch_latest_email`, `list_messages`; return it as `thread_id` (e.g. `m.get("conversationId")`).
2. **`src/app/gmail.py`:** `threadId` is a top-level field on the message resource (and on list
   refs) — extract `m.get("threadId")` in `get_message` / `fetch_latest_email` / `list_messages`
   and return it as `thread_id`. (No new API calls; `format: "full"`/`"metadata"` already include it.)
3. **`src/app/pipeline.py`:** `draft_for_message` reads `msg["thread_id"]` and passes it through
   `draft_for_email(..., thread_id=...)`; `draft_for_email` adds a `thread_id` param and forwards
   it to `db.insert_email_log(..., thread_id=...)`. `draft_for_latest` does the same from
   `fetch_latest_email`.
4. **`src/app/db.py`:** `insert_email_log` gains a `thread_id` param (column write).

## Model + history wiring (Option A)
- **`db.get_conversation(tenant_id, contact, thread_id=None, client=None)`** — when `thread_id`
  is provided, filter on `thread_id` instead of `from_address`; otherwise unchanged.
- **`pipeline.draft_for_email`:** if the inbound has a `thread_id`, fetch
  `db.get_conversation(tenant_id, contact, thread_id=thread_id)`; else fall back to the
  contact query (today's behaviour). Pass to `draft_reply(history=...)` exactly as now.
- **`pipeline.redraft_email_log`:** read the stored row's `thread_id`; scope history by it
  (still excluding the row being regenerated) when present, else by contact.
- **`agent.thread_messages` is unchanged** — it just receives a thread-scoped list. The
  20-row cap (`THREAD_CONTEXT_MAX_ROWS`) now applies per thread.
- **Optional Phase 4 tie-in:** surface the thread's *originating* subject (oldest row in the
  thread) as the title in `build_system_prompt`, instead of the current email's subject.

## Edge cases
- **Null thread_id** (old rows, provider edge, manual path that didn't capture) → contact
  grouping fallback. ✓
- **Subject changed within one thread** → same `conversationId`/`threadId` → stays one
  conversation (correct). ✓
- **New email reusing an old subject** → new thread id → new conversation, fresh context. ✓
- **Reply-to-old-thread-to-start-new-topic** → same thread id → stays merged (known, rare;
  the model sees the subject so it can still adapt). Documented limitation, not a blocker.
- **Same person across two mailboxes/addresses** → out of scope (cross-address identity, also
  deferred in Phase 4).
- **Idempotency unaffected:** `source_message_id` (per-message) still keys the one-draft-per-
  inbound unique index; `thread_id` is per-conversation and independent.

## Verification
- **Unit:** mock Graph (`conversationId`) and Gmail (`threadId`) payloads → assert
  `mailbox.get_message` / `gmail.get_message` surface `thread_id`; `get_conversation(thread_id=…)`
  filters to one thread; `draft_for_email` selects thread- vs contact-scoped history by
  presence of `thread_id`.
- **Live:** two distinct threads from the same sender → each new draft only reflects its own
  thread; a brand-new thread starts with no prior-topic context; a long single thread still
  caps at 20 rows.

## Out of scope (later, if ever)
- Option B/C dashboard re-grouping (separate piece; spec extends §Model wiring).
- Backfilling historical `thread_id`.
- Cross-address / cross-provider identity.
- Long-thread summarisation/compaction (still only needed far beyond pilot volume).

## Open questions for the user
1. **Option A, B, or C** (D2)? Recommendation: A now, B later if wanted.
2. Use the thread's **originating subject** as the Phase 4 title, or keep the current email's
   subject? (Only meaningful once `thread_id` exists.)
3. Confirm **no backfill** is acceptable (historical threads stay contact-grouped).
