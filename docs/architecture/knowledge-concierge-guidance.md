# Guided Knowledge review

When source review is enabled, the onboarding Concierge starts from the current
pending notes rather than sending every message to the source correction tool.
It shows a note's actual title and text, then offers keep, leave out or correction.
Supporting source wording is handled with its note: the owner may explicitly
retain the original wording together, or inspect the differences. This choice
can preserve headings and repetition verbatim; it is not semantic deduplication.
The guide never silently excludes source text or declares a paraphrase complete.
Suggested source destinations consider labels, wording and citations across all
drafts, including undecided notes. A shared page or the fact that a note was kept
is not enough to assign unrelated wording to it. Short headings, navigation and
ambiguous passages remain unresolved for an explicit destination or exclusion
with the owner's reason. These suggestions are lexical hints, not semantic
coverage certification. The first kept note is never the fallback destination.

The `/onboarding/knowledge/review/{review_id}/guide` endpoint reads the same
tenant-scoped, versioned review used by admission. It returns a bounded patch to
the existing review form, not a database mutation. Model interpretation is used
for natural messages; Show and explicit action buttons do not call a model.
The action is restricted to the displayed item, with exact existing note targets.
Model-inferred keep/discard responses never change a note decision. Natural
approval asks for confirmation using the current note's explicit Keep or Leave
out control (the exact action text also works in chat). The edit intent and
model-inferred note decisions preserve prior note choices, do not advance the
review and do not enable Save. This also protects mixed or conditional requests
misclassified as keep/discard. The edit intent points to the existing manual editor;
it neither rewrites a note nor turns an edit request into approval. Stale-item
validation also applies to explicit confirmations. Source exclusions and overlap
acknowledgments retain their existing separate review paths.
Owner answers and exclusion reasons must occur in the owner's message.
For messages up to 1,000 characters, the structured reason is constrained to
empty or the complete verbatim owner message; it is nonempty only when the owner
supplies an exclusion rationale. Longer messages keep guarded excerpt extraction,
so a short reason within a longer request remains usable. Answers still require
an owner-authored excerpt. Literal source coverage permits a sentence-opening
word such as “Our” to become “our” inside a sentence, without changing the rest
of the wording, identifiers, amounts, negation or attached qualifications.
Details can remain unknown. Reopening a named note changes its review decision,
not its text. Wording/source repairs still use the existing source-correction
proposal flow and require review/application there.
An owner can also undo the unsaved source-retention choices for a named note.
This clears those local choices (including a matching legacy blanket default),
not the note body, owner edits or shared qualification context. The affected
source statements become unresolved again. It does not mark them excluded or
silently redistribute them to other notes. Overlap comparisons are short excerpts;
the full text remains in the form, and stale-item checks bind the full content.

The guide handles original source wording, overlap acknowledgements and optional
questions in plain language. It preserves shared qualifications and requires
the existing admission planner to succeed before offering Save. Nothing is
saved by chat decisions: the separate Save reviewed decisions action submits
the ordinary form, whose permission/version/source/overlap checks still apply.
Form text is line-ending normalised before any fingerprint or comparison, so the
overlap key the guide acknowledged from textarea values (LF) is the key the
native submission (CRLF) is checked against.
It does not navigate into later onboarding steps or override sending settings.

The browser checks that the form and its local values have not changed while a
turn is in flight. Old action buttons are invalid after another choice, manual
edits or form replacement. Guide requests participate in the existing busy
guard, preventing website import/correction/save races. Unsaved review choices
remain browser form state; a full reload can lose them. They are not advertised
as persisted preferences or saved Knowledge.

Detailed source inventory presentation belongs to the separate Knowledge UI
change. Removing its visible block must preserve hidden `source_<id>`,
`source_reason_<id>` and `source_default` controls for admission. Preview renders
should also carry `guided_unknown_<id>` and `guided_expand_<id>` hidden fields
from the submitted review form to retain guide progress. These markers are only
review navigation state, never evidence of a business fact.

Correction requests include the current local form snapshot. The source reviewer
sees editable note bodies and effective retained wording as separate states;
local retention is not misrepresented as an already stored body edit. Source
corrections remain additive: undoing local retention uses the guide, and local
merges must be finished or undone before source corrections. Both preview and
apply reject conflicting local/body snapshots. Applying a correction records a
hash of its local review context without implicitly saving the source decisions.
Provider credit, quota and availability failures return an actionable service
message while preserving the local form. After a failed natural-language turn,
the browser makes one read-only request to show the current note and restore its
current actions; it never retries the failed choice or correction. Unreadable
server responses use the same recovery path rather than exposing a JSON parser
error. Owner exclusion reasons remain owner-authored while comparison tolerates
typographic apostrophe and quotation substitutions from structured model output;
the saved reason is the matching original span from the owner's message.

Empty model changes, already-retained passages and already-recorded exact citations produce a visible no-op
explanation. An identical citation does not suppress independent valid changes;
invalid references, claims and versions still reject the proposal. Citation-only
previews explicitly say they change no wording. A no-op cannot reach the draft
writer. Apply resolves the batch by ID and compares the complete current snapshot,
allowing an identical form refresh while rejecting changed choices. Busy, missing
preview and stale snapshot states show recovery instructions rather than returning
silently. None of these paths admits Knowledge or changes source-release flags.


Current-note wording corrections can propose removal of exact displayed-body
paragraphs using server-generated occurrence references and the full content hash.
Retained source attachments are separate context, not additional body paragraphs.
The proposal never approves a note. Its preview shows the exact removed text,
remaining text and every source choice losing retention, including destinations
and the owner's exact request as its reason. Separate Apply writes pending drafts
and those listed choices, resets the edited note/overlap decisions, and leaves
Keep and final Save separate. Unrelated local edits stay on the page.

Applied source choices and non-approval resets are durable pending draft state,
so an ordinary reload or lost acknowledgement recovers them without restoring an
old Keep. Raw evidence remains immutable. Exact duplicate occurrences do not
exclude facts still retained elsewhere; partial source-unit deletion and unique
caveat/negative-limitation loss are rejected. Removed clause-specific citations
move to the edit audit, not current note attribution. This supports bounded
removal; it does not invent replacement facts or change operational settings.
