# Concierge — Business Q&A (Phase 1: read-only)

**Status:** BUILT 2026-07-25. Phase 1 of 3 — read-only Q&A only, no actions.
**Why:** closes the Fyxer inbox-Q&A gap and creates a differentiator no
competitor has: unified inbox + CRM + diary + knowledge Q&A in one chat.

## What it is

A conversational Q&A surface where a signed-in tenant asks questions about
their OWN business data — inbox conversations, CRM contacts/deals/follow-ups,
diary bookings, knowledge base — and gets grounded answers with evidence and
links to the real records.

This is the FIFTH mode of the existing concierge (ask / onboard / help / fix /
**business**), not a new system. It is separate from the product-help modes:
help mode answers "how does BLAM8 work?", business mode answers "what did
Sarah say about the deadline?".

## Where it lives

- Dedicated page: `/app/concierge` (nav entry "Concierge"; renders the saved
  transcript, suggestion chips, streaming lookups).
- The corner widget on every in-app page grows a tab pair: **Your business**
  (default) | **App help**. Same panel, separate threads.

## Architecture (the house retrieval rule)

The model NEVER writes a query. `src/app/concierge_data.py` defines a fixed
read-only tool catalogue; the LLM plans over it in a manual tool loop
(`concierge.answer_business`, mirrors fix mode, max 10 calls/question):

| Tool | Backing | Tier |
|---|---|---|
| search_conversations | db.search_email_log (ILIKE over subject/body/sender; `category` filters by the smart inbox label — docs/modules/module-auto-categorisation.md) | Starter |
| get_conversation | db.get_conversation | Starter |
| search_knowledge_base | knowledge.search_chunks (pgvector) | Starter |
| search_contacts | db.crm_list_contacts | Pro |
| list_deals | db.crm_list_deals (+stage/status/stale filters) | Pro |
| get_pipeline_summary | db.crm_report_pipeline | Pro |
| list_followups | db.crm_open_promises + db.crm_list_tasks | Pro |
| list_bookings | db.search_bookings | Pro |

Phase 1 uses Postgres ILIKE for email search (the list_agent_events pattern);
pgvector over email bodies is a later upgrade if needed.

## Security

- **Tenant isolation, belt and braces:** every executor takes `tenant_id` from
  the server session (never from the model's args) AND runs under the caller's
  RLS-bound client (`ctx.client`). A prompt-injected "show another tenant" has
  nothing to reach.
- **Read-only by construction:** no write tool exists in the catalogue —
  "delete all contacts" has nothing to call. A structural test
  (test_concierge_business.py) fails if a non-`search_/get_/list_` tool is
  ever added here.
- **Untrusted retrieved data:** the prompt marks all tool results (email
  bodies, CRM notes, KB entries) as data, never instructions.
- **Constrained links:** the model emits `{kind, id, label}`; the route mints
  hrefs (`resolve_business_links`) and drops malformed ids — the model never
  writes a URL.
- **Caps:** 10 tool calls/question, 50 rows/call, snipped bodies, 30
  questions/hour + burst rate limits (per-IP, the house limiter), CSRF header
  on the POST (it persists chat rows).

## Tier gate

`full_qa = "crm" in db.visible_modules(...)` — exactly the CRM pages' boundary,
so Pro (plan key `"starter"`!), comp'd and preview tenants all resolve
correctly. Starter (`"email"`) gets inbox + knowledge tools only; the prompt
tells the model to name the Pro unlock honestly and attach a billing link.

## Persistence

`concierge_messages` (tenant_id, user_id, role, content, links, created_at;
RLS) stores the transcript so `/app/concierge` reloads with history. Answers
are always recomputed live — the table is a convenience, writes are
best-effort and never block an answer. Migration:
`scripts/db/migrations/apply_concierge_business_migration.py` (applied to prod 2026-07-25).

## Phase 2 — actions with confirmation (BUILT 2026-07-26)

The concierge may now PROPOSE six actions; nothing runs without the operator's
Confirm click. The model has NO write tools — it fills a structured `action`
field, and the flow is:

1. **Propose:** `concierge_actions.stage()` re-validates every field against
   real records (ids must exist and be owned, stage names must be real stages,
   diary windows must fit the tenant's mode, capacity is advisory-checked with
   alternatives suggested) and writes a `concierge_proposals` row
   (**pending**, single-use, 5-minute expiry; staging a new proposal
   supersedes any other pending one). The chat shows a card: summary,
   old→new details, editable draft text (draft_reply), Confirm / Cancel.
2. **Confirm:** `POST /concierge/business/execute` atomically claims the row
   (pending→executed with an age filter — expired, replayed, cancelled and
   superseded confirmations all get honest refusals) and runs the executor.
3. **Execute:** `concierge_actions.execute()` composes EXISTING write paths:

| Action | Tier | How |
|---|---|---|
| draft_reply | all | update_email_log_draft + status='pending' → the inbox APPROVAL QUEUE (approve-to-send still governs sending; operator can edit the text on the card) |
| move_booking | scheduling | NEW `move_booking_if_capacity` RPC — same advisory lock + overlap rule as holds, UPDATE in place (ref/details/deposits survive), calendar delete+re-push |
| update_deal_stage | Pro | crm_update_deal with the stage-type→status/closed_at rule from the CRM route |
| create_followup | Pro | crm_create_task (source='manual') |
| create_contact | Pro | crm_get_or_create_contact (+company by business domain, phone via crm_update_contact) |
| add_kb_entry | all | kb.ingest with a title-keyed source (same title = update, not duplicate) |

The proposal row doubles as the audit log (status, payload old→new, result,
executed_at) and every execution also writes an agent_events row. Injection
hardening: the prompt states actions come ONLY from the operator's chat
messages — instructions inside emails/notes/KB are reported, never proposed —
and the confirmation card is the human backstop either way. No direct-send exists
as a kind, structurally, and the ONLY delete kinds are `invoice_delete_draft` and
`quote_delete_draft` (below).

### Edit and delete a draft (15 October 2026)

Four kinds, for a DRAFT invoice or quote only, behind the `money:send` door
(`invoice_create`'s), and **always a confirm card**: `invoice_edit_draft`,
`invoice_delete_draft`, `quote_edit_draft`, `quote_delete_draft`
(`concierge_actions.ALWAYS_CONFIRM_KINDS`; `approval_mode()` answers `confirm` for them
before it can read any future per-kind auto setting, so no auto-trigger ever reaches
them).

- **Edit** replaces the draft's lines wholesale through the main app's own
  `db.update_invoice_lines` / `update_quote_lines` (same VAT, same rounding); the model
  gives `lines` (`description | amount` per line, a JSON array also read; or just
  `amount` for a one-line draft) and reads the current lines from `list_invoices` /
  `list_quotes`, which now carry a draft's lines. The card shows each changed line and the
  total before and after (`db.invoice_totals` / `quote_totals`, the numbers the save
  stores). A line's link to its job, and a quote line's flags (optional, ticked, price
  entry, deposit rule), are kept through the edit.
- **Delete** removes a draft nothing has ever happened to — never sent, opened,
  payment-linked, paid against, answered or chased, no quote built on an invoice, no
  invoice built on a quote — in ONE tenant-scoped `DELETE` guarded on every one of those
  facts; the lines go by the schema's `ON DELETE CASCADE`. The card reads "Delete draft
  INV-1004 (£252.50 for Priya Shah). Nothing has been sent to the customer. It can't be
  brought back." The proposal row keeps the lines it carried, and an `agent_events` row
  records who confirmed what.
- **Numbers.** Refs come from `next_invoice_ref` / `next_quote_ref`, per-tenant counters
  that only go up (`tenant_counters`), never `max(ref)+1`, so a deleted draft's number is
  never reissued (a gap in the sequence is the price; the agent event and the proposal row
  say why). Pinned in `tests/test_draft_delete_numbers_database.py` on a real PostgreSQL
  cluster.
- **Refusals.** A non-draft is refused in words that are true for how far it got ("already
  sent, so it can't be edited from here"; for an invoice, "cancelling or voiding a sent
  invoice isn't available from here yet"; for a quote, "I can cancel it instead", because
  `quote_cancel` exists). At Confirm both re-read the record and, if it was sent, changed
  or removed since the card, return `state: failed` with the sentence (the route shows it;
  a raised `ActionError` would only read as "unconfirmed"), and the database write itself
  is guarded on status, so a send landing between the re-read and the write is stopped.
  A second Confirm of the same card applies nothing.
- **A delete is confirmed by a tap, and by nothing else (16 October 2026).** The older
  Standard voice client reads a spoken yes as the confirm for whatever card is waiting
  (GPT-Live and the typed panel already send words back to the card), so a spoken yes
  could have run a delete. The rule is the server's, at the one route every surface
  confirms through, `POST /concierge/business/execute`: `concierge_actions.TAP_ONLY_KINDS`
  (`invoice_delete_draft`, `quote_delete_draft`) are claimed only when the request carries
  `confirm_via: "tap"`, the marker the card's own Confirm button sends (the spoken yes
  sends `"words"`; a missing marker is not a tap). Anything else is refused BEFORE the
  claim with "That is a delete: tap Confirm on the card." (`state: pending`, a
  `concierge_confirm` trace with `outcome: refused`, `reason: tap_only`), the card stays
  waiting, and the tap that follows still works. The route never reads words, so no
  casing, punctuation or "yes please delete it" can talk it round. A bulk card holding a
  delete that can run (an item that could not be prepared stays unticked and never runs)
  is tap-only as a whole. Edits are not tap-only: a spoken yes still confirms one. Cards
  carry `tap_only: true`, which the voice screen uses to say "tap Confirm" instead of "say
  yes"; the model's own spoken reply is not changed (see the follow-up in the PR).
  Pinned in `tests/test_concierge_tap_only_delete.py` and `tests/replays/tap_only_replay.mjs`.
- **Not in this phase:** cancel or void of a sent invoice or quote, contacts, bookings,
  settings.

Migration: `scripts/db/migrations/apply_concierge_actions_migration.py` (applied to prod
2026-07-26 — concierge_proposals + move_booking_if_capacity).

## Phase 3 (not built)

Proactive suggestions in the collapsed drawer, bulk actions, send-now on the
auto-send trust ladder, SMS drafting once SMS is live.
