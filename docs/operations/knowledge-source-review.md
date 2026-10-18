# Source-backed Knowledge corrections

Website imports can retain the text actually read so an owner can ask for
corrections to specific pending notes. The correction service preserves IDs,
labels, order and original text. It proposes only complete passages from the
retained source, before or after an existing note. Citations prove attribution,
not that a statement is true or appropriate; the owner must still review it.

Website capture separates block boundaries and records source locations. Material
wording in headers, footers, navigation and asides is inventoried too; menu text
is not promoted into business notes automatically. Each read reports failures,
non-HTML/empty pages, duplicate redirects, page-limit omissions and character
limits. Only complete retained blocks or sentences enter the inventory. A digest
and character counts account for the available extracted text, but omitted text
is not retained evidence and cannot be cited as though read. Discovery covers
links on the starting page, not the entire site. Short pages are retained too.

New distillations request a separate exact note clause and one or more contiguous
source quotations per clause. Every quote is checked against its claimed page;
joined nonadjacent passages and clauses absent from the note are rejected. These
attributions remain review hints, not a semantic entailment check or automatic
coverage clearance. Literal source coverage and explicit exclusions still gate
admission. Existing pending batches are not silently recaptured or corrected.
Malformed, output-limited or oversized model responses stage no drafts and report
that limit explicitly. They are not presented as proof that the website has no
usable facts, and excess proposed notes are never silently sliced off.

The workflow is: import website drafts, request a source review, inspect the
proposed additions, apply them to drafts, then use **Check changes and overlaps**
and **Save reviewed decisions**. Applying corrections does not save Knowledge.
Existing admission rules still account for source statements, carry source
qualifications into retrieved chunks, and require explicit overlap decisions.
Unanswered questions remain unknown. Owner wording and sourced additions keep
separate attribution, including after another review or manual edit.

The review shows numbered business notes with visible content and explicit note
decisions. Heading edits and Source and evidence expand on demand. Shared
caveats are flagged once and remain available with each note's evidence. Raw
source-unit classifications, offsets and inventory counts are not displayed.
The Assistant guide supplies source decisions through the existing hidden
`source_<id>`, `source_reason_<id>` and `source_default` form fields. These fields
start empty and never approve anything automatically. Guided unknown/expanded
markers survive a preview refresh. This presentation depends on the note-by-note
guide; deploy the compatible guide and UI together. Existing server admission
checks, retained source records and exclusion-reason requirements are unchanged.
Exact sentence/block repetitions are checked across complete
note text and shown once per pair. They may be intentional: the owner decides
whether to retain both or merge. This is not a complete semantic duplicate or
contradiction audit. Generic default questions are omitted for topics already
represented by evidence; explicit unanswered questions and unknown topic states
remain, and saved answers are preserved.

Check changes and overlaps runs in place with a visible running, completed or
failure result; it saves no facts. Without JavaScript the same read-only preview
renders in the review page. That page carries the same tenant-scoped Assistant
context as onboarding. Use this batch with Assistant selects an exact pending
batch when several are available. Source-review errors are shown directly,
without automatically retrying a potentially completed request.

Citation-only proposals attach exact retained passages to existing note clauses
without rewriting the note. Multiple spans/pages remain separate quotations.
The before/after proposal shows both wording and attribution for owner review.
Applying a proposal records its source identity, revision, owner request, reviewer,
time and before/after details in the pending payload. The audit is bounded at
twenty applications and 350,000 serialized characters; reaching a limit rejects the change without
dropping earlier history. Repeated/stale apply requests cannot duplicate a write.
Attribution still does not certify entailment. Missing footer evidence in an
older batch cannot become a website quote; capture it properly or supply an
explicit owner clarification for separate review.

Each review exposes separate content, evidence and shared-context signatures.
An identical refresh says no new evidence arrived. Changed stored versions do not
overwrite dirty local forms: both versions are available for comparison, with an
explicit choice to adopt the stored form. After the stored context changes, a fresh preview against the current version
is required before admission. A no-longer-pending batch preserves local text
and disables further browser decisions. Refresh/navigation is never correction,
approval or history deletion.

Website status distinguishes idle, reading, pending drafts, actual saved notes,
failure and unconfirmed outcomes. Pending drafts get a counted Review drafts
link; Read again is deliberate. A session attempt marker cannot establish read
completion. After an uncertain read, Check stored drafts performs a read-only
refresh while retaining local edits, before another explicit read is offered.
Knowledge operation changes dispatch the onboarding readiness event; Continue
still requires the real page action and existing pending/unsaved safeguards.
Those navigation safeguards apply with source corrections disabled too. Loading
the review controller does not enable source capture or its correction routes;
the feature flag, permissions and retained evidence still gate those controls.

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
