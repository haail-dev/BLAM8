# Horizontal model routing

This is disabled infrastructure, not a release or live-model quality claim.
Policy `horizontal-v1` selects by trusted operation and complete application
authority. Industry, OS, plan and business names are absent from the resolver.
Existing entitlement, mailbox, People, approval and execution owners are unchanged.

## Caller inventory and boundaries

Implementation base: `2096ac3` on `origin/main`, following the handoff inspection
at `47312af`. Only intervening UI commits were present. The original checkout's
unrelated `CONTRIBUTING.md` edit was preserved in its original worktree.
The external pre-edit inventory records 80 direct LLM/composer call sites.

| Path | Prompt/schema owner and baseline | Downstream effect | Candidate eligibility |
|---|---|---|---|
| `crm_extract.extract` | `crm_extract.py`, general setting | Existing field planner may autofill or suggest | All supplied definitions must match server-reviewed semantic fingerprints; candidate results are suggestion-only |
| `agent.compose_outbound` | `agent.py`, general setting | Caller queues, approves or sends | Requires explicit routine operation and customer-draft authority; missing/unknown purposes stay baseline |
| `jobs/send_reminders._remind_one` | Diary facts and shared composer | Existing draft, dedupe and send gates | Draft-only, complete date/reference, directly framed diary facts; auto-send and specialist framing remain baseline |
| `voice_learning.generate_sample` | `voice_learning.py`, general setting | Preview displayed for review | Only the preview; corpus analysis/profile learning stays baseline |
| General inbound, portal, product help, public/owner Concierge | Existing prompt, tools and general/booking overrides | Existing reads, proposals and actions | Baseline |
| Dates, dependants/documents, inferred risk, financial/sensitive fields | Existing extractors/validators | Existing decisions and writes | Baseline; special-category CRM fields remain excluded before any model call |
| Planner/worker/reviewer | Existing task-loop settings | Existing experimental path | Independent switch remains `single`; no change |
| Speech/audio | Existing transport and models | Speech service | No change |

All callers use shared `llm` receipts; established operation names and caller
usage owners remain unchanged. Preview metering now uses the returned model.
Routed calls store policy, selected/returned model, reason, request/call/attempt
IDs, schema failures, fallback reason, duration, tokens and cache usage in the
existing receipt envelope. Candidate success totals also carry usable-result
latency including fallback. Unknown charges remain unknown. Failed provider
calls are metered by `llm`; successfully returned but malformed calls travel with
the final totals, or are metered as failures when no valid result is available.
This adds no billing writer or database migration.

The other composer callers retain the baseline: assets/routes, booking-claim
corrections, Concierge actions/drafts, courses, CRM sequences, events, hand-cancel,
invoice chasing, quote chases, deposit lifecycle, offered-slot nudges, ready/stale
reservation/no-show/best-before/handover/recall/service reminders, ladders,
on-my-way, practice follow-ups, scheduled plans, shop routes, SMS webhook,
tenant outreach, wallet and wholesale standing orders. A source inventory test
requires review when another outbound caller opts in. Shared composer arguments
are supplied by application code, never inferred from message contents.

## Server configuration and rollback

| Setting | Merged default |
|---|---|
| `MODEL_CANDIDATE_ENABLED` | `false` |
| `MODEL_CANDIDATE_MODEL` | `claude-haiku-5-5` |
| `MODEL_CANDIDATE_OPERATIONS` | empty comma-separated allowlist |
| `MODEL_CANDIDATE_CRM_FIELDS` | empty comma-separated semantic SHA256 allowlist |
| `ANTHROPIC_MODEL` | unchanged `claude-sonnet-4-6`; effective env override wins |
| `ANTHROPIC_BOOKING_MODEL` | unchanged `claude-sonnet-4-6`; effective env override wins |
| `CONCIERGE_TASK_LOOP` | unchanged `single` |

Operation IDs are `crm_ordinary_fields`, `routine_admin_message`, `tone_preview`.
CRM eligibility requires `crm_field_fingerprint(definition)` for each ordinary
definition after reviewing its full meaning. Financial decisions, inferred risk
and sensitive fields must never be approved. Fingerprints include labels, hints,
options and workflow attributes: tenant edits invalidate eligibility. Dates,
built-ins, expiring fields and ladder fields stay baseline even if allowlisted.
Fingerprint review is an operator release task, not a customer settings field.

Only enable a family after its separate evaluation gate and explicit release
approval. Effective deployment settings must be inspected read-only at release;
source defaults do not prove production values. Disable the master switch or
remove an operation to restore previous effective routes for new calls. Do not
replay in-flight mutations, discard pending drafts or lose receipts. Restart or
redeploy processes as required to reload environment-backed settings; changing
an environment variable does not hot-reload an existing Settings instance.

## Failure budget

Candidate calls have no tools. Each bounded operation has at most one candidate
and one baseline provider invocation, sharing the existing 90-second deadline.
The first attempt receives half the budget; SDK retries and hedges are disabled
for both. Schema failure, provider unavailability or timeout may fall back before
publication. A cancelled or late result cannot be used. Exhaustion propagates
to existing safe caller failure handling. Unknown operation/authority stays
baseline. Existing baseline retries and validators retain their behavior.

Deterministic validation does not establish factual accuracy. CRM candidates
cannot inherit confidence calibration; the trusted suggestion marker prevents
autofill. A successful baseline fallback retains its existing autofill path.
No tool loop changes models and no send, write or booking operation is replayed.

## Comparative evaluation contract

`tests/fixtures/model-routing/` contains 20 development and 100 reserved cases
per family, with three planned runs per model per case. Cases include expected
facts/fields, forbidden effects, omissions, prompt injection, ambiguity, quoted
history, negation, exact references, missing facts and horizontal context labels.
They are synthetic templates; case counts alone do not prove adequate diversity
or statistical independence. Human corpus review and additional independent
cases may be required. Do not tune against held-out results.

The external reports area contains the preparation harness, frozen request
manifest, receipt/review contract and costed plan. Preparation invokes actual
caller prompt assembly with fake responses and blocked sockets. It does not
contact providers. Do not run a paid comparison without a recorded approved
budget, synthetic-data permission and reverified provider rates.

For each family, require zero authority/leakage/duplicate-effect failures and
zero invented commitments/prices/policies/false completions on adversarial cases.
Require at least 95% usability without factual correction, no more than a 2-point
paired regression, at least 50% measured known cost reduction per successful
task, 99% complete cost coverage and p95 usable-result latency within 10% of
baseline. Report p50/p95 usable-result and whole-task latency; first-token latency
only applies to streaming. Keep cold/warm schema results separate. The existing
general-model warmer does not warm a candidate schema.

Blind human review is required; exact semantic checks supplement it. Report
precision, recall, omissions, wrong-record assignment, unique-case failures and
repeat variability. Compute uncertainty paired by case, with scenario-cluster
sensitivity; repeats are not independent samples. Expand the corpus or retain
baseline if the interval cannot rule out a regression beyond two points. One
critical failure blocks that family, including unsafe baseline failures.
Include every failed, retry, hedge and fallback receipt; unknown costs are not
zero. Haiku prompts above 100K tokens remain excluded from savings claims.
Dates/DST, failed verification, tool failures, permissions and booking holds
remain dedicated baseline regression/evaluation tracks; this cohort does not
promote them. Split-task comparisons require separate whole-workflow evidence.

All three families are implemented and offline-tested only. None is live
evaluated, enabled, deployed or approved for promotion by this change.
