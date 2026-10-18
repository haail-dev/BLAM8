# Source-backed Knowledge corrections

Website imports can retain the text actually read so an owner can ask for
corrections to specific pending notes. The correction service preserves IDs,
labels, order and original text. It proposes only complete passages from the
retained source, before or after an existing note. Citations prove attribution,
not that a statement is true or appropriate; the owner must still review it.

The workflow is: import website drafts, request a source review, inspect the
proposed additions, apply them to drafts, then use **Check changes and overlaps**
and **Save reviewed decisions**. Applying corrections does not save Knowledge.
Existing admission rules still account for source statements, carry source
qualifications into retrieved chunks, and require explicit overlap decisions.
Unanswered questions remain unknown. Owner wording and sourced additions keep
separate attribution, including after another review or manual edit.

The Assistant uses this source-review path when exactly one eligible draft
batch is present. Multiple batches can be reviewed using their individual
controls. Older drafts without retained evidence remain manually reviewable.
Corrections support at most 30 notes, labels up to 80 characters and note text
up to 4,000 characters; larger existing admission drafts remain available for
manual review. Source pages are limited to eight, 12,000 characters each and
60,000 characters in total. At most ten unexpired snapshots are retained per
tenant. No live browser rig, archived run or external service is needed in CI.

## Installation and release

1. Verify the existing `20261018_knowledge_review.sql` admission migration is
   installed. It is a different migration from the similarly named historical
   setup-card prototype; do not replace it with that old file.
2. Apply `db/migrations/20261018_knowledge_source_review.sql` as a deliberate
   database release step. It creates source tables and two service-role RPCs;
   it does not change existing saved notes or enable the feature.
3. Deploy the code and, after verification, set
   `KNOWLEDGE_SOURCE_REVIEW_ENABLED=true`. The default is false.
4. Verify a test account's website import, exact draft correction, overlap
   preview, explicit save, reload, and leave-for-later behaviour. A pasted note
   must never be silently staged by Continue. Check both permission rejection
   and expiry fallback before enabling more broadly.

The new `knowledge_review_sources` table stores immutable raw snapshots scoped
by tenant and review. Browser roles have no access. Read-time expiry is 30 days;
the existing `app.jobs.purge_email_log` retention job deletes expired snapshots
while this feature is enabled. Keep that retention job scheduled. If disabling
the feature without rolling back the database, arrange continued deletion of
expired rows explicitly; the feature-gated job no longer sweeps them.
The separate identity registry prevents reuse of a purged snapshot ID. It
contains IDs only. Existing admission drafts, source-statement inventories,
approved notes and their audit records follow the existing retention policy;
the 30-day raw-snapshot expiry does not promise their deletion.

Preview is read-only. Applying corrections compares the full admission context
version under the same lock order as final admission. Competing corrections,
new saved facts or a concurrent approval make stale requests fail. Uncertain
responses require reload; no automatic mutation retry is performed.

## Rollback

Disable the flag first. Existing admission and manual review continue to work.
The `.rollback.sql` file removes raw snapshots, ID reservations and the new
RPCs; pending drafts and accepted Knowledge are deliberately retained. Export
raw evidence first if it is required for an investigation. Do not run rollback
as a routine deployment action. The disposable database test verifies rollback
and reapplication; no production migration is run by tests.

## Verification

Use the offline runner for `test_knowledge_source_review.py`,
`test_knowledge_source_routes.py`, `test_knowledge_source_database.py` and
`test_knowledge_source_ui.py`, alongside the existing admission, fidelity,
onboarding and setup-card tests. PostgreSQL tests verify atomic capture,
immutable identity, tenant scope, conflicting corrections and approval races.
The mandatory CI database step rejects skips. Synthetic examples remain in
`tests/fixtures/knowledge_review/`; historical run evidence stays outside the
platform repository.
