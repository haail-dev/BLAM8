# Knowledge review and required Voice completion

Author implementation contract, 23 October 2026. Offline synthetic verification
is recorded by the regression modules below; this is not a production rollout.

Voice requires **agent_name**, **tone**, and **sign_off**, each a nonblank string
after trimming. Business brief and extra voice notes remain optional. A displayed
placeholder is not an answer. Existing saved valid values count regardless of
whether their source is manual, learned or hybrid. A learned profile needs the
same complete values before Accept. Suggestions populate the actual form for
owner review. Continue saves before navigation; failed saves retain the form.
Direct Knowledge navigation and final wizard completion enforce the same server
contract. Both manual and learned-profile saves use an atomic original-snapshot
comparison; stale forms cannot overwrite a newer Voice edit. The step disclosures do not navigate. There is no Voice Skip action.

Knowledge review keeps editable facts separate from source evidence. Conservative
lexical checks recognise sentence framing, typography and a small set of noun
forms; these are not a general semantic verifier. Unrecognised paraphrases remain
in one grouped ambiguity view. The owner can confirm represented meaning against
a retained note; names, numbers, units and conditions still have to be present.
Neither source links nor this confirmation append old sentences to active facts.
Missing facts must be written into a note or explicitly left unknown/excluded.
The audit retains quotations and source URLs. Only a small whitelist of structural
labels is automatically grouped; unfamiliar headings remain reviewable facts.

Paragraph cleanup cannot remove unique critical names, numbers, units, conditions
or source caveats. Other proposed factual exclusions are explicitly listed before
Apply. Apply updates a draft, and final Save admits Knowledge. Exact saved owner
replacements are reapplied when the same labelled source is reread; ambiguous
matches are not rewritten. Original source snapshots remain in the audit.

Review checkpoints store draft choices in the existing review payload, under an
optimistic context-version check. Note approval binds to that note's content;
overlap identities bind to both notes, and source choices bind to the relevant
note revision. Reload and unrelated edits retain decisions. No request retries
automatically against a competing revision. Invalid overlap annotations are
reported and omitted from suggestions without discarding verified corrections.
Multi-answer turns validate every supplied decision before preparing any patch;
answer and exclusion wording must come from the owner message.

The generic policy questionnaire is optional under unknown details. Explicit
setup questions persist with stable IDs and destinations in the saved review,
and use the existing grouped setup `knowledge_candidates`/`import_knowledge`
consumer. Import is explicit and idempotent; unknown facts do not apply settings.
Agreement-specific inspections do not create a universal frequency requirement.

Release dependency: apply `20261023_knowledge_review_choices.sql` deliberately
after the existing Knowledge review migrations. It adds a service-role-only CAS
checkpoint function; it does not write active Knowledge. Its paired rollback
removes the function and retains draft audit data. No production migration is
part of source implementation. Also apply `20261023_onboarding_voice.sql` before
shipping the new Voice form. Its rollback removes the save function and retains
all saved Voice values; old valid users remain complete. Diary engine and guided
tour changes are separate. At integration, `voice_completion.require_saved(ctx)`
must run before any guided-tour preparation inside `onboarding._finish`.

Regression boundaries: `test_knowledge_voice_simplification.py`, the existing
Knowledge route/source/admission/database suites, Voice/browser readiness tests,
and business-agnostic guard. Model outputs and embeddings in offline tests are
synthetic; no claim of general semantic accuracy or live-model performance is made.

Automatic lexical coverage retains conjunctions and prepositions: days joined by
"and" are not a range joined by "to", and reversing "from"/"to" changes direction.
Normalized fallback clauses must match in full, not as a subsequence with extra
scope words. Unproven paraphrases remain unresolved until explicit owner review.
Removal previews cannot count a different relationship as a retained duplicate;
dropping that source occurrence requires a visible factual exclusion decision.

Review hydration must not count as an owner edit. Device sync restores existing
text and its review stamps together; the note controller ignores synthetic
restore input events. Genuine label/content edits still clear only that note's
decision, and server-side fingerprints and CAS still reject obsolete approvals.
The native startup regression renders stored decisions before replaying the
shared form, then validates its Save fields and checks real edit invalidation.

Knowledge guide and source-correction output schemas are included in the provider
subset guard, including their dynamic variants. The guide enforces the thirty
answer limit in `apply_many`, before processing choices; it must not send the
unsupported `maxItems` keyword to the model provider.


Knowledge usability follow-up: Apply automatically invokes the existing authoritative
preview/check endpoint, preserving the stale-token guard until that succeeds. It
never approves the corrected note or bypasses final admission. A failed recheck
leaves the explicit Check changes and overlaps recovery action. Other note choices
and exact source exclusions remain intact across the refreshed form and reload.
Correction previews show before/after first, with detailed citations and diagnostics
in expandable disclosures; factual removals remain visible beside the proposal.
Optional source details stay collapsed until the owner asks to inspect them.
Their original text and explicit grouped unknown decisions remain in the audit.

The saved page uses original note headings rather than retrieval identifiers.
Identifiers and stored section counts remain under Source details. Topic coverage
is an optional exploration, not a competing completion counter. A server-rendered
saved status appears only when saved notes exist and no review is pending. The
assistant collapses cards belonging to server-confirmed saved review IDs into
history and shows that same current status; it does not rewrite the transcript.
