# Grouped onboarding review

Local implementation, 23 October 2026. No deployment, migration execution or
production activation is claimed. The first four routes keep their existing
forms and validation. `/app/setup` resumes the stored section; the later review
has Diary, Records and workflows, and Charges and payments, each with compact
independently saved A/B/C sections. Old settings URLs still edit their own
domains and link back to the group. Team assignment remains in People.

`onboarding_contract.py` owns stable capability, group, substep and question IDs.
Labels are bounded, escaped presentation metadata with their own provenance and
version. Saved related-subject labels can suggest contextual wording. Industry
classification never installs a schema or grants permission. Resetting wording
does not reset answers, scope identities or settings. Group rendering is
deterministic and does not request model output. Existing help uses its existing
model/provider and receives the scoped answer ledger as data, with explicit
instructions to keep facts, settings proposals and operational permission apart.

## Review and operational effects

Capability relevance is needed, not needed, deferred or undecided. It controls
question visibility only. Existing configured features stay active when a review
branch is hidden; changing operation requires its existing settings form.
Section review is unreviewed, reviewed, off or deferred. Unselected optional
questions never block review. Explicit unknown/deferred answers block a reviewed
claim only for their section; the section may instead be reviewed as deferred.
A saved billing-grouping question depends on reviewing the related-record
decision. Its fact does not implement consolidated invoicing.

Agreement terms, combined charging, out-of-hours response, repeat-activity facts,
booking timing and prepaid-credit cancellation have specific optional relevance
choices. Selecting a coarse group does not ask all of those questions. An exact
owner answer choosing review of booking rules can supply relevance for timing
questions; imported owner-reviewed questions also retain their own relevance.
Existing answers stay accessible when a topic is deselected. Record information
and subject evidence remain reviewable with diary modules even without CRM;
links are filtered by actual available modules.

The primary UI uses business-facing choices: Answer now, Not sure, Decide later
and Doesn't apply. Internal answer states, scope IDs and capability IDs stay in
the contract. Technical limitations and provenance are expandable details, and
the Apply preview shows named current/proposed values rather than raw JSON.
An explicit not-needed/later choice records that decision for the associated
sections directly, without requiring a second completion form.

Review completion never supplies capacity, booking authority, payment permission,
bank details, a sales stage or a record type. `booking_policy` and the existing
booking/payment workflows remain authoritative. Checklist saves still preserve
booking mode and have their own validation and revision checks.

## Answers, scopes and proposals

Answers distinguish unanswered, answered, unknown, deferred, not applicable and
superseded history. False, zero and explicit empty text survive. One active
answer is keyed by question identity plus scope type and stable scope ID.
Organisation settings cannot be proposed from an activity answer. Existing
related-record group IDs and individual record IDs retain their exact scope.
Activity UUIDs bind explicitly to unambiguous existing saved services; removed
or renamed services fail safely instead of rebinding by a generated label.
Duration and price proposals change only that bound service. Organisation-wide
availability/authority cannot express different per-activity rules here.

Diary discovery answers use a separate `diary` scope whose ID is one of the
owner's named diaries in the state's `diaries` map, never a service-bound
activity. A removed diary keeps its answers in history, but they no longer
count towards review, and neither do answers to questions a diary stopped
asking after an earlier answer changed. Discovery questions are marked
`discovery` and are asked only by the guide's discovery stops, never as generic
facts or grouped-review rows. A pre-filled answer the owner confirms carries the
`owner_confirmed_prefill` provenance with the facts it came from.

Saved Knowledge question decisions can be explicitly routed/reused without
rewriting Knowledge. Pending extraction and generated default questions are not
imported. Owner answers are reused only with current coverage evidence; expired
evidence becomes unknown. Changed evidence creates a conflict marker and retains
the active owner answer. Existing Knowledge effective-fact/source filtering is
owned by the separate Knowledge change. Imported question prose is data and has
no executable settings adapter. Agreement notice, scoped activity intervals,
billing grouping and urgent response remain factual answers, separate from
appointment cancellation, recurrence engines and resource availability.

The exact existing global payment-terms question has a code-owned alias to the
typed payment-terms answer. This prevents a second question under a new label;
the full answer remains prose and cannot infer numeric deposit settings. A value
too long for the native setting remains a fact instead of being truncated.
About you's owner-saved description can be explicitly reused for organisation
activity purpose once, with its own source revision. Existing answers are not
overwritten when the description changes.

Supported typed scalar answers and activity duration/price can create proposals
using `setup_cards` and the native validators. Preview persists the answer/card
relationship and a current/proposed difference, with no settings effect. Apply
requires both the metadata revision and original configuration snapshot to
match. The metadata receipt and `change_setup_card` settings effect commit in
one transaction. A stale save fails, including manual changes after preview.
Cancel leaves the original settings intact. Replays fail revision checks and
cannot duplicate effects. Separate pages/proposals commit independently; this
does not imply a multi-page transaction. A card applied or left on another
surface can be reconciled from its authoritative terminal line receipt.
This also reads the original card's tenant-scoped history after another surface
starts a new card. If that surface saved some lines and left others, the receipt
records a partial result with each line's actual outcome; reconciliation never
applies or retries settings.
Leaving remaining changes also works after another surface applies only part
of a proposal: pending lines are left, applied settings remain saved, and the
partial outcome is explicit.
Receipt values are labelled as the original preview, with unapplied lines
explicitly marked not applied. A settings-page link shows current saved values;
historical proposals are never labelled as the current configuration.

Related answered invoice choices are previewed together. Native first-save,
tax and deposit dependencies still require explicit answers; the preview never
supplies missing defaults. All displayed effects in one page proposal commit
together, including their answer and action IDs.

The HTTP API rejects unknown IDs/extra fields, checks settings permission and
View as, and derives tenant identity from authentication. The new atomic RPC
is service-only; ordinary database clients cannot submit executable card
patches or read the sensitive history table. Diagnostics omit answer contents.
All grouped read/write routes inherit the existing application subscription
gate, including expired-trial denial before loading or saving review data.

## Compatibility and migration

The `diaries` map is additive: existing states load with it empty, and the
existing state writer accepts it without a migration.

Prepare `db/migrations/20261023_agnostic_onboarding.sql` after existing setup-card
migrations. It adds one JSON metadata column and an immutable revision history
table; it does not backfill tenants or change operational configuration.
Migration is idempotent. Exact read-time legacy mapping:

| Legacy evidence | New review evidence |
|---|---|
| `intake_configured_at` | Diary C previously saved, awaiting explicit review |
| `dependants_configured_at` | Records A previously saved, awaiting explicit review |
| Existing subject groups | Records A has configuration to review |
| Booking readiness, CRM stage/quiet threshold, business name or bank details | Operational/configuration evidence only; no fabricated section approval |

Dismissal, existing setup cards, held values, settings, and old URLs remain in
their original stores. New position is a stable substep ID. On the first new
review visit an old settings URL maps to its owning group/substep; no stored
legacy position is overwritten. Existing page-save stamps never approve a
whole group. A tenant with no bookings/payments can review those branches off
without creating any configuration, while booking remains unavailable.

Rollback: deploy the previous application and run the paired rollback SQL to
remove only the new write function. Keep `onboarding_state` and
`onboarding_revisions`; they contain user-entered data. Reapplying the forward
migration restores writes without clearing answers/history. No production
migration is authorised by this implementation.

## Limits and verification

This review does not add engines, arbitrary relationships, reservation semantics,
route optimisation, additional timezones, or a recurrence/billing schema. Existing
advanced forms remain available; their catalogue entries are not new readiness
claims. Unsupported needs can be retained as scoped facts and reviewed deferred.
See the separate HAAIL diary-extension backlog for later decisions.

`test_agnostic_onboarding.py` exercises HTTP callers backed by disposable local
PostgreSQL, real setup adapters and actual target values, including conflict,
retry, tenant isolation and migration rollback. Browser guards render the actual
group template/shell with production styles, keyboard disclosure, escaped long
labels and light/dark narrow layouts. Generated retained evidence belongs in
HAAIL reports, outside this repository. No live tenant has been continued.
