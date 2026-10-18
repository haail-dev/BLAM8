# LLM usage tracking — audit & coverage (2026-07-26)

Audit of every LLM call site after the 2026-07-25/26 wave (Concierge Phase 2,
Customer Portal chat, auto-categorisation, voice auto-learning). Verdict first:
**coverage was already complete** — every generation call flows through the
`src/app/llm.py` choke point (transcript log) *and* is metered into `usage_events`
(cost ledger), and every Voyage embed is metered in `src/app/knowledge.py`. This
audit added exchange correlation (`llm_calls.request_id`), a derived **source**
dimension, a by-source admin breakdown, and fixed one latent double-specialist
under-metering bug.

## The two books

| | `llm_calls` (src/app/llm.py) | `usage_events` (src/app/usage.py) |
|---|---|---|
| Purpose | Full-transcript observability (debugging, red-teaming) | Cost/margin accounting |
| Granularity | One row per **API call** (every tool-loop round) | One row per **exchange** (loop usage summed) |
| Fields | tenant_id, request_id, operation, module, model, stop_reason, input/output tokens, duration_ms, request/response JSON | tenant_id, provider, model, operation, module, all four token classes + embed_tokens, **cost_usd**, email_log_id |
| Failure calls | Logged with `stop_reason='api_error'` (tokens that went to the API are still visible in the request payload) | Not metered (no usage object returned; Anthropic bills completed requests — a truncated `max_tokens` stop IS completed and IS metered) |
| Retention | Purged after `llm_log_retention_days` (transcripts carry customer mail) | Kept (it's the ledger) |
| Cost | Not stored here — join to PRICING via model, or use usage_events | `cost_usd` computed from PRICING incl. cache read/write multipliers |

Both writes are best-effort (never break a draft) and effectively synchronous
but tiny (one insert); no latency-visible overhead was added by this audit.

## Step 1 — call-site inventory (verified in code, not assumed)

| Call site | Operation(s) | Source | llm_calls | usage_events |
|---|---|---|---|---|
| Draft generation ([agent.py](../../src/app/agent.py) `draft_reply`, via pipeline) | `draft` / `redraft` | inbound_email / owner | ✅ every loop round | ✅ summed, incl. nested specialist tokens (`_merge_handoff_usage`) |
| Auto-categorisation | — rides the `draft` call — | inbound_email | ✅ same single row (category fields are part of DRAFT_SCHEMA; token counts cover draft + category + reason) | ✅ same single event |
| TMS / field-service specialist hand-off | `tms_handoff` / `fsm_handoff` | inbound_email | ✅ per round | ✅ folded into the draft's event |
| Concierge public Q&A (marketing widget) | `concierge_ask` | prospect | ✅ | ✅ (billed to PLATFORM_TENANT_ID) |
| Concierge onboarding / help / fix | `concierge_onboard` / `concierge_help` / `concierge_fix` | owner | ✅ per round (fix has a tool loop) | ✅ summed |
| Concierge business Q&A + Phase 2 propose | `concierge_business` | owner | ✅ per round + the degenerate-reply retry | ✅ summed incl. retry |
| Customer Portal chat | `portal_chat` / `portal_chat_general` / `portal_chat_unverified` (module `portal`) | portal / portal_general / portal_unverified | ✅ per round (tenant_id re-resolved per call: platform until the session verifies) | ✅ summed — **who pays is decided per turn**, see "Portal cost split" below |
| Voice auto-learning (sent-mail scan) | `voice_learning` | owner | ✅ **meta-only** (`log_mode='meta'` — the corpus is never stored, envelope/tokens are) | ✅ per batch |
| Voice sample draft | `voice_sample` | owner | ✅ | ✅ |
| CRM extract / intel | `crm_extract` / `crm_intel` | inbound_email | ✅ | ✅ |
| Proactive jobs (reminders, recall, chase, run sheets, sequences, wholesale, no-show, missed-call SMS, follow-ups) | `reminder`, `recall`, `special`, `event_chase`, `run_sheet`, `sequence`, `no_show_score`, `wholesale_*`, `missed_call`, `followup` | automation (followup: owner) | ✅ (via `compose_outbound`) | ✅ |
| Fleet replanner | `plan` | owner | ✅ per round | ✅ summed |
| Storefront OCR / asset condition diff (vision) | `ocr_stock_in` / `condition_diff` | owner | ✅ (base64 redacted to a size stub) | ✅ |
| KB embeddings (Voyage) | `embed_ingest` / `embed_query` / `embed_flag` / `embed_update` | owner / retrieval | n/a (not an Anthropic call; llm_calls is the Anthropic choke point) | ✅ `record_voyage` — separate provider row, `embed` price (currently free tier, tokens still metered) |

**Bypass check:** the only real `client.messages.create` invocation in the app
is [llm.py:49](../../src/app/llm.py) — every `AsyncAnthropic` client factory is passed
*into* `llm.create`. No untracked generation path exists.

## Deliberate decisions (documented per the directive)

- **Rule-based skips are NOT logged as zero-cost rows.** A no-reply/bounce
  sender ([pipeline.py](../../src/app/pipeline.py) `is_no_reply_address`) is logged to
  `email_log` with `category='automated'` and a category_reason — so the
  behaviour is auditable there — but writes **no** llm_calls/usage row, because
  no LLM call happened. "LLM said FYI" vs "rule said automated" is
  distinguishable: the former has a usage `draft` event linked via
  email_log_id, the latter has none.
- **`source` is derived, not stored.** Every operation name is unique to one
  trigger surface, so `usage.source_for(module, operation)` classifies every
  historical row retroactively and cannot drift from a write path. Buckets:
  `owner` / `inbound_email` / `portal` / `portal_unverified` / `portal_general`
  / `automation` / `prospect` / `retrieval` (embed_query serves several
  callers — bucketed honestly rather than guessed) / `other` (unmapped =
  visible, never mislabelled; a test fails if a new operation ships unmapped).

## Portal cost split (2026-07-26 directive)

Anyone can chat on `blam8.com/c/{slug}` — pre-verification traffic mustn't
bill the business. Each portal **turn** is attributed post-hoc by
`portal_chat.billing_operation(verified, tools_called)` in
[portal.py](../../src/app/portal.py):

| Turn state | Operation | Source | usage_events tenant |
|---|---|---|---|
| Session unverified (regardless of tools) | `portal_chat_unverified` | `portal_unverified` | `PLATFORM_TENANT_ID` |
| Verified, turn used a business tool (`search_knowledge_base`, `list_my_bookings`, `check_availability`, `create_booking`, `move_booking`, `escalate_to_business`) | `portal_chat` | `portal` | the business |
| Verified, no business tool this turn (general "what is BLAM8?" chat) | `portal_chat_general` | `portal_general` | `PLATFORM_TENANT_ID` |

Decisions baked in:
- **Verification is platform work.** `lookup_reference` / `find_contact` are
  NOT business tools — a pure "here's my code" turn bills to the platform even
  though the match reads the CRM (it's BLAM8's security gate, not a service to
  the business). A turn that verifies AND then reads bookings bills to the
  business (`verified` is end-of-turn state).
- **`verified` = `session.contact` set** — via the opaque session token, a
  visitor-supplied `?ref=` code, or a `lookup_reference` match. (Invite-only,
  2026-09-24: `find_contact` no longer creates customers, and an unverified
  visitor never reaches the model, so `portal_chat_unverified` stops accruing.)
- **llm_calls (transcripts) vs usage_events (billing):** the transcript rows'
  tenant_id is resolved per API call (platform until `session.contact` is
  set), so State-1 traffic never lands under the business even in the
  transcript book. The State-3 general-question re-attribution is
  billing-only: a verified turn's transcripts stay under the business tenant,
  its usage_event moves to the platform.
- **"What does the portal cost BLAM8?"** = platform tenant's
  `portal_unverified` + `portal_general` buckets (plus `concierge_ask` for the
  marketing widget). The platform sentinel is hidden from the operator tenant
  list, so query it directly:
  ```sql
  select operation, count(*) turns, sum(cost_usd) usd
  from usage_events
  where tenant_id = '00000000-0000-0000-0000-00000000b1a8'
    and module = 'portal' and created_at > now() - interval '30 days'
  group by operation;
  ```
- **llm_calls carries no cost column.** Cost lives in one place
  (usage_events); duplicating it into the transcript log would rot when
  PRICING changes.

## request_id correlation (new)

`llm_calls.request_id` groups every API call of one user-facing exchange:
tool-loop rounds, the nested TMS/field-service specialist (contextvar
inheritance — no threading through executors), concierge's degenerate-reply
retry, and all voice-learning batches of one run. Standalone calls get a fresh
id each. Scopes: `draft_reply`, `answer_fix`, `answer_business`,
`answer_portal`, `tms.answer`, `field_service.answer`, `replan_day`,
`voice_learning.analyse` (all via `@llm.with_request_scope`).

### Monitoring queries (service role)

One exchange's total cost across its internal turns:
```sql
select request_id, operation, count(*) turns,
       sum(input_tokens) input, sum(output_tokens) output
from llm_calls where tenant_id = :t and created_at > now() - interval '1 day'
group by request_id, operation order by max(created_at) desc;
```

The "promised a lookup, called no tool" bug class (found live 2026-07-26):
a single-turn exchange on a tool-looped operation that ended without tool_use —
worth alerting on when the reply text also promises action:
```sql
select request_id, min(created_at) at, max(operation) op
from llm_calls
where operation in ('concierge_business','portal_chat','concierge_fix')
group by request_id
having count(*) = 1 and bool_and(stop_reason = 'end_turn');
```
(In `full` log mode, join back to `response` to check the reply text for
"let me look"/"I'll check" style promises.)

Per-tenant cost by feature and source: the admin tenant page
(`/admin` → tenant → "Usage by operation" + **"Usage by source"**, new) or
`usage.operator_summary()` / `operator_tenant_detail()` — margin status
(healthy / thin / underwater) per tenant already exists on the operator
console.

## Voice-learning cost shape (Step 6 answer)

Not one big call: the corpus is analysed in batches of **40 emails per call**
(`BATCH_SIZE`), max 300 emails sampled — Julius's ~70-sent-email run will be
**2 calls**, each logged (meta) + metered, correlated under one request_id.

## Fixed in this audit

- [pipeline.py](../../src/app/pipeline.py) `_sync`: with tms **and** fsm both active,
  the second specialist's usage overwrote the first's (assignment, not merge) —
  a transport+trades draft would under-meter. Now accumulates.
- `llm_calls.request_id` column (schema.sql + prod migration applied).
- `usage.source_for` + `by_source` in `summarize()` + admin tenant-page table.
