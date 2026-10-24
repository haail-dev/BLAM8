> Current review changes: see [Knowledge and Voice review](knowledge-voice-review.md).
> Source retention now links evidence without appending duplicate sentences; optional policy gaps do not form a mandatory closing questionnaire.

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
Manual note entry opens from the keyboard-accessible **Add a note** disclosure.
It starts collapsed when empty and opens when a returned form contains a label
or note text, keeping unsaved/error-recovery content visible. Review note and
the form-associated Continue actions retain their existing submission behaviour.
Exact sentence/block repetitions are checked across complete
note text and shown once per pair. They may be intentional: the owner decides
whether to retain both or merge. This is not a complete semantic duplicate or
contradiction audit. Overlap identifiers, owner-edit detection and the final
overlap check use note text with line endings normalised to LF. The Assistant
posts textarea values with LF while Check changes and overlaps and Save reviewed
decisions are native form submissions that send CRLF, so one acknowledgement
holds on every path; an unacknowledged overlap still refuses the save. Generic
default questions are omitted for topics already represented by evidence;
explicit unanswered questions and unknown topic states remain, and saved
answers are preserved.

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
Apply also returns the advanced stored version. With device sync enabled, the
shared checkpoint records that version with the applied wording, so a reload on
any device restores the owner's unsaved note and overlap decisions. The form's
own version changes only when Check changes and overlaps renders a fresh
preview; a Save or guide turn against the old version is still refused.
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

During a website read, Next and Leave drafts for later are disabled and labelled
as waiting for the read. The existing capture-phase navigation and submit guards
also block keyboard/programmatic submission. Success, failure and an unconfirmed
response restore the controls' prior state; pending-draft decisions still apply.

Website drafting retains complete inventory statements intersecting a verified
cited excerpt on the same page, alongside any abbreviated model wording. This
preserves procedure endings and named-party scope in source limitations. Notes
may therefore repeat wording; the owner can edit them before approval. If this
expansion exceeds the 4,000-character correction-note limit, the batch reports
an incomplete draft rather than cutting source clauses. Uncited statements still
need the existing source-coverage review; citations do not certify entailment.

Source-correction questions are advisory: previews display at most three nonblank
questions of at most 500 characters each. Extra/long/blank annotations produce an
explicit warning instead of discarding independently verified corrections. The
original annotation list remains in the proposal revalidated on apply. This does
not settle missing-evidence or coverage decisions, weaken revision/source checks,
or admit Knowledge. Applying still changes only pending drafts, and final
admission still requires the owner's explicit Save reviewed decisions action.

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

## Effective facts and owner replacements (23 October 2026, local verification)

Admission embeds approved note wording and source references only. Full website
quotations, original drafts, excluded statements and correction history remain
in the saved review payload; they are never appended as current retrieval facts.
This also covers manual note edits and explicit source exclusions. Existing
saved chunks are not changed by deploying this code.
An exclusion/unknown decision cannot save while its exact source wording still
survives in a retained note; the owner must edit or retain that wording explicitly.

An authorised settings writer can request a bounded replacement using their
actual words. The model selects an exact, unique old substring and an exact
owner-supplied replacement. The server preserves every other character and
records replacement ranges as owner wording, separate from website evidence.
Ambiguous/conditional requests, overlapping ranges, invented values and no-op
replacements fail closed. Source additions/removals cannot be mixed into the
same replacement proposal. The before/after preview requires a separate Apply;
Apply changes pending drafts and resets their Keep decision. Final Save alone
embeds and admits the effective note under the existing atomic admission lock.
Stale Apply/Save retries are rejected. A deliberate stale-site reread is a new
pending review and must resolve overlap with the corrected saved source; it
cannot silently replace the owner-approved note. Explicit Keep both remains an
owner choice and should be used only when the facts can coexist.

Short structural navigation/heading labels without factual markers are grouped
in expandable Source coverage, with original text and URLs. Final Save requires
explicit acknowledgement of the grouped labels. Any factual heading can instead
be retained with a note or deliberately left unknown. Numbers, negation,
conditions and source qualifications stay reviewable facts. Typography matching
preserves case-sensitive identifiers, numbers and units. Semantic equivalence
is not certified from a model citation or lexical similarity: uncertain
paraphrases remain review items. Related factual gaps stay with their note;
unassigned details have a counted batch review and an explicit Leave unknown
route. Deferred text stays in the audit and outside active Knowledge. Topic
questions remain separate and unknown until the owner supplies an answer.

The source-review gate, settings permission, tenant boundaries and final Save
authority are unchanged. These changes do not configure Diary, CRM or invoicing.
Use the owner-correction, source-database and native correction-layout tests in
addition to the existing caller/recovery checks. Live model answer quality and
already-saved data remediation require separate validation and release approval.


### Context-aware collection and explainable comparisons

Website collection uses a bounded projection of saved business brief, owner goals,
business-wide Knowledge and explicitly recorded unanswered review questions.
Conversation answers enter through saved state; raw chat, unsaved suggestions,
mailbox-private notes, secrets and customer records are excluded. Context only
ranks same-site URLs and anchor labels and focuses draft questions. It is marked
separately from website evidence in the model request; citations must still match
captured pages, and saved owner corrections retain precedence at admission.

The crawler has no sector-specific path priorities. It follows links to depth two,
with at most eight fetch attempts, twelve seconds per attempt (including redirects),
a forty-second total collection deadline, 12,000 characters
per page and 60,000 total. Failures consume the attempt budget. Discovered pages
beyond the budget and omitted text remain in the capture manifest. Discovery is
stable without context. SSRF checks still apply to every redirect; linked pages
redirecting off-site are not admitted. This is HTML capture, not JavaScript
rendering or a guarantee of whole-site coverage. Context itself is bounded to
12 saved items and 20 recorded questions with omission counts.

Overlap review no longer blocks admission for a matching title or shared business
vocabulary alone. Exact repeated statements/contained text and closely aligned
statements with different numbers or conditions produce quoted comparisons.
Both sides are labelled, source URLs are displayed once, and full citation records
remain in the audit. A possible difference is a review hint, never an automatic
contradiction verdict or semantic-coverage certificate. Owner review/merge/discard,
content-bound acknowledgements, source-coverage checks and final atomic Save
remain required. Unknown paraphrases are not silently declared equivalent.

Validation: tests/test_knowledge_context_capture.py, existing capture/canonical,
review/admission, guidance and native Knowledge layout tests. No migration or
automatic rewriting of existing saved notes is part of this change.
