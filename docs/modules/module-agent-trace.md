# Agent Trace — execution-narrative observability (2026-07-26)

The third leg of BLAM8's observability, alongside its two older siblings:

| System | Table | Audience | Job |
|---|---|---|---|
| LLM log (`src/app/llm.py`) | `llm_calls` | operator | every word that crossed the API, grouped per exchange by `request_id` |
| Agent Log (`db.log_agent_event`) | `agent_events` | tenant | the curated activity narrative page |
| **Agent trace (`src/app/trace.py`)** | **`agent_trace`** | **operator** | **what ran, with what inputs, what came back, and why** |

`concierge_proposals` remains the Phase 2 compliance audit and was not changed.

## Contract

* `trace.emit(tenant_id, trace_type, *, outcome, request_id, session_id, user_id,
  contact, email_log_id, booking_id, proposal_id, latency_ms, detail)` —
  best-effort: a trace failure logs a warning and never breaks the action.
* `request_id` defaults to the ambient `llm.request_scope()` id, so a trace row
  written anywhere inside a tool loop joins that exchange's `llm_calls` rows for
  free. Entry points that stage work *after* the loop (`concierge_routes.business`,
  `portal.portal_chat_endpoint`, `voice_learning.run_learning`) open one outer
  scope over the whole turn; `agent.DraftResult.request_id` carries the draft
  exchange id back to the pipeline the same way.
* `session_id` for the portal is `trace.session_ref(token)` — a sha256 prefix,
  never the capability token. Reference codes appear masked only.
* Service-role only (RLS on, no tenant policy), same stance as `llm_calls`;
  rows are purged with `llm_calls` on the daily sweep (`llm_log_retention_days`)
  because `detail` may carry message snippets.
* Writes are single-row inserts on paths that already write `llm_calls`
  transcripts inline, so added latency is negligible; `push_send` rows are
  written on the existing delivery daemon thread, off the request path entirely.

## Query surface

`db.list_agent_trace(tenant_id, trace_type=, outcome=, request_id=, email_log_id=,
before=, limit=)` and `db.list_agent_trace_failures(tenant_id)` — the three
directive shapes: everything for one tenant chronologically, one feature across
all tenants, all failures. Indexes: `(tenant_id, created_at)`,
`(trace_type, created_at)`, `(outcome, created_at)`, `request_id`, `email_log_id`.

## trace_type catalogue

| trace_type | emitted from | outcomes | key detail fields |
|---|---|---|---|
| `concierge_turn` | `concierge_routes.business` | ok, error | question, reply_chars, **tools_called** (empty list = the answered-without-looking-anything-up bug pattern), proposed_action |
| `concierge_propose` | same | ok, rejected, error | kind, summary, expires_s / rejection reason; `proposal_id` column |
| `concierge_confirm` | `business_execute` | confirmed, replayed | kind, proposed_by, **time_to_confirm_ms**, draft_edited; `user_id` = who clicked |
| `concierge_execute` | `business_execute` | ok, error | kind, executed_payload, result_summary / error; `proposal_id` links the audit row |
| `concierge_expire` | `business_execute` (lazy discovery) | expired | reason=timeout, kind |
| `concierge_deny` | `business_cancel` | denied | reason=user_denied, kind |
| `portal_chat` | `portal.portal_chat_endpoint` | ok, error | message, reply (truncated), tools_called, identified; session_id, contact, latency_ms |
| `portal_kb_retrieval` | `portal_data._search_knowledge_base` | ok, empty | query, results_returned, sources, retrieval_method=retrieve; latency_ms. ("empty" cannot distinguish empty-KB from retrieval failure — that stays in knowledge.py warnings) |
| `portal_identity_verify` | `_lookup_reference`, `_find_contact` | verified, not_found, code_issued, limit, send_failed, not_invited (new_customer before 2026-09-24) | verification_method (reference_code / email_code; new_contact before 2026-09-24), code_masked, email. One row per attempt → brute-force shows as stacked not_found rows |
| `portal_verification_email` | `portal.send_verification_email` | provider_error | the mail-provider raise, distinct from send_failed |
| `portal_booking_create` | `_create_booking` | ok, limit, full, … | booking_ref, date, window, summary; booking_id column |
| `portal_booking_move` | `_move_booking` | ok, not_found, not_movable, full, … | old/new date+window, booking_ref |
| `portal_escalation` | `_escalate_to_business` | ok | message (truncated), escalated_to=inbox, category; email_log_id |
| `portal_tool` | `portal_data.execute` catch-all | error | tool, exception |
| `categorisation` | pipeline (draft, redraft, no-reply insert) | ok | category, model_category, **method (llm / rule)**, reason, trigger, informational; request_id = the draft's llm exchange |
| `kb_retrieval` | pipeline (draft + redraft, 2026-07-30) | ok, empty | query (truncated), results_returned, sources, trigger (draft_generated / draft_regenerated); email_log_id + request_id. Skipped when the KB is off — "empty" always means *ran and matched nothing*. The same rows become the draft's persisted `email_log.draft_sources` (the "Grounded on" pills) |
| `label_transition` | pipeline auto-send + holds, dashboard send/skip/override | ok | to_category, trigger (reply_sent / booking_created / draft_skipped / manual_override), via, user_id for human actions |
| `push_send` | `push._send_to_tenant` | ok, partial, failed, no_devices, unknown, error | kind, tag, title, devices[{subscription_id, user_id, status: accepted/failed/expired_removed, http_status}], delivery_receipts=unavailable |
| `tone_analysis` | `voice_learning.run_learning` | ok, insufficient, error | emails_analysed, analysis_method=batched, batches{attempted,succeeded}, profile_created, profile_summary (never the corpus — meta-only privacy contract) |

Label lifecycle: `from_category` is not stored (it would cost a read on the hot
path); the previous label is the preceding `categorisation`/`label_transition`
row for the same `email_log_id` — the full New → Needs Reply → Draft Ready →
Quote Sent → Booking Pending story is `list_agent_trace(email_log_id=…)` in
chronological order.

## Honest semantics

* **Push**: a 2xx from the push service means *accepted for delivery* — Web Push
  gives no device receipts, so per-device status is accepted/failed/
  expired_removed and every row says `delivery_receipts: "unavailable"`. There is
  no "delivered". `unknown` = pywebpush absent. Expired-subscription removal
  (previously silent) is now recorded in the row that triggered it.
* **Voice learning**: dropped batches (unparseable JSON) are now warned per-batch
  and counted in `batches.succeeded` — a 1-of-8 run is visible.
* **Concierge**: `status='executed'` is still stamped at claim time (before the
  write) on `concierge_proposals`; the trace's `concierge_execute` row is the
  record of whether the write actually landed.

## Coverage inventory (audit of 2026-07-26)

"Before" = what existed this morning; agent trace closes the *missing* column.

| Feature / Action | Before | After |
|---|---|---|
| Draft generation | `agent_events` drafted/transport/office + `llm_calls` | + `categorisation` row w/ request_id |
| Concierge Q&A + lookups | `llm_calls` only; lookups invisible | `concierge_turn` w/ tools_called |
| Concierge propose/confirm/expire/deny | nothing (only final executed row) | full lifecycle, attributed |
| Concierge execution | 6 `agent_events` (no proposal link, no actor) | + `concierge_execute` w/ proposal_id, actor, error detail |
| Portal chat / KB / tools | `llm_calls` only | `portal_chat`, `portal_kb_retrieval`, `portal_tool` |
| Portal verification attempts | nothing (brute-force invisible) | `portal_identity_verify` per attempt + limits |
| Portal booking / escalation | `agent_events` success-only | + failure outcomes, budgets, old→new values |
| Label transitions | destructive in-place overwrite | per-transition rows w/ trigger + actor |
| Push delivery | `last_used_at` timestamp only; silent sub deletion | `push_send` w/ per-device outcomes |
| Voice learning runs | `tenant_config` status field (last run only) | `tone_analysis` per run w/ batch stats |

## Not (yet) traced

* `concierge_followup_context` — lands with the pending Concierge follow-up fix
  (its stager currently makes one CRM lookup; the propose row carries the kind).
* Concierge platform surfaces (`concierge_ask` / help / fix / onboarding) — out
  of this directive's scope; `llm_calls` covers them.
* An admin viewer page — `db.list_agent_trace` is ready for it; the llm_calls
  admin viewer remains the pattern to extend.

(Closed 2026-07-30: draft-path KB retrieval now emits `kb_retrieval` —
docs/modules/module-concierge-transparency.md §B.)
