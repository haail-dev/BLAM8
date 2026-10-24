# Guided setup on actual application pages

The four initial onboarding steps retain their existing validation. Knowledge
Continue opens a brief introduction. Starting explicitly acknowledges it and
opens Diary, which first asks which diaries the business keeps (Diary
discovery, below), followed by the existing settings owners in grouped
substep order. Existing completed tenants acquire no new mandatory flow.

The introduction has one route: its Start/Resume button and the assistant's
matching button call the same guide action. The old grouped-review shortcut is
not offered there; remaining setup nudges elsewhere lead to the guide entry.
The grouped review remains an internal summary route. Moving through the guide
opens the relevant page and fields without a tour outline. Keyboard focus and
unsaved proposal indicators retain their own styling.

The introduction, all three grouped reviews and their native setup pages reuse
Knowledge's onboarding shell: the B mark, BLAM8/Automate identity header, seven
progress positions, card, background and assistant gutter. Progress names disclose
labels without navigating or saving. Saved group reviews determine ticks; the
current guide section determines the active group, including when two groups
use the same settings page. Pausing retains this layout on setup pages; ordinary
app pages and completed or dismissed setup retain their normal application shell.
The header and seven progress positions stay visible while the guide reveals
fields. The mobile guide keeps the native form scrollable above its assistant sheet.

`guided_setup.registry()` accounts for the complete native setup catalogue.
It defines bounded routes, anchors, small decisions and dependent batches. The
guide never accepts model selectors or arbitrary navigation destinations.
Knowledge stays in its existing initial review. Existing grouped review and
deep links remain available as a summary and fallback.

The guide uses additive `onboarding_state.guide` metadata and the existing
compare-and-swap RPC from the grouped onboarding migration. Position,
explanation coverage and review are separate from operational settings. Next
and Back never apply settings. Exact earlier setting answers can prepare a
proposal on arrival at their owning decision; accepting it remains explicit.
Pause keeps the current question and pending
cards; explicit deferral keeps unresolved choices visible. Completing all
explanations cannot make a section reviewed or make bookings/payments ready.

Conversation uses the existing Help model, provider, catalogue and typed card
adapters. Factual answers use an additional bounded schema only while a factual
guide question is active. The server checks exact question, scope and types.
Operational suggestions must belong to the current registry decision; they
remain proposals. Existing card edits and acceptance reconcile linked answers,
superseded history, proposal revisions and receipts through the same atomic
metadata/card writer. Unrelated native card behavior remains supported.

`proposal_forms.js` projects supported scalar values into real owning controls.
Their pre-projection values remain authoritative for dirty-save and device-sync
serialization. Enter and native form submission are blocked while a proposal
owns controls. Selects and compound choices use the existing typed card editor;
the visible preview is labelled Proposed — not saved. Composite native editors
currently identify some rows positionally or by editable label, so the guide
highlights their real editor and retains editing on the stable card line ID.
It does not select a dynamic native row by its label or array offset. Unsupported
catalogue controls use the actual native editor explicitly and remain unresolved
until the owner reviews or defers them.

Card Accept uses the visible exact line IDs and native preview. Changed config,
card or answer revisions require a fresh explicit review. A response failure
requires read-back before another write. Accept and page navigation prompt
before discarding unrelated unsaved edits. Projection restore detects conflicting
device drafts and removes a misleading preview. Device sync remains enabled.

Related catalogue controls are grouped into owner decisions. A proposal adds
durable explanation stops for its affected native controls, with the same card
available throughout. The current conversation stays visible; earlier discussion
can be expanded. On phones the actual relevant page stays above the assistant,
with an Expand page control. The assistant's header remains a resize handle:
dragging it (or its arrow keys) sets the sheet height, which the guide keeps for
the session and clears when Expand page is used. In the onboarding shell the
Diary page's card heading is visually hidden, since its progress step already
names it. No cloned form or screenshot replaces the editor.

Starting the guide carries forward reviewed Knowledge answers with their source
and exact identity, plus the owner's About you description. Unknown or stale
source answers remain unresolved. No extraction alone acquires approval.
Changed or removed Knowledge invalidates its imported answer in the current
guide view, retains the earlier value in history and blocks old pending-card
acceptance. The owner can explicitly choose the earlier or updated answer;
this revises the same pending card and still requires Accept to save settings.
Owner-edited or accepted settings retain their independent authority. Reads
derive freshness without writing metadata; explicit mutations persist it.
Section reviews link directly to unanswered required decisions. The read-only
Diary page closes the Diary group, after its settings, and a fresh tenant can
see it before configuring intake; this does not bypass validation for creating
a booking.

Guide orientations do not stamp whole legacy tours complete: unseen Diary,
Money, Today and Inbox
topics, including explicit drafting activation, retain their existing behavior.
No guide action creates bookings, invoices or payments, sends messages, enables
drafting or changes saved operation through relevance choices.

## Diary discovery

Diary is answers first, settings after. It opens on the real Diary settings page
with four kinds of stop before any setting is proposed:

1. **Your diaries.** The owner picks suggested diaries or names their own (up to
   eight), or says they are not sure or keep none. Each suggestion shows the
   fact it rests on; suggestions that cite no trusted fact are dropped, and a
   fallback list from saved services and record dates is used when the model is
   unavailable.
2. **Each diary.** A few short questions, skipped when an earlier answer makes
   them irrelevant: an example, who starts it, the record date it follows, what
   it takes up and how many at once, how its time is set and how long, whether
   it repeats, and whether anyone outside the team must agree.
3. **Hours.** Working and contact hours are always asked, whatever else was
   answered, in 15-minute steps. Overnight hours are refused with their reason.
4. **Profile.** One plain line per diary with a code-owned support state: ready
   to set up, shown from records, noted for later (with the reason), not
   available on this account, or not decided yet. Confirming it replaces the
   former "Do you need help arranging times?" gate.

Before asking, the guide reads one shared business picture
(`src/app/setup_context.py`): confirmed answers, saved and current Knowledge
answers, the interview description, saved configuration, record fields and
their dates, and counts. Every fact has a stable id, a trust tier and its
source. The industry label, pending Knowledge and message content are not read,
and booking settings the owner has not confirmed are gaps, not values. Hours
can be offered as "Is that right?" only from confirmed settings or the owner's
saved words.

On discovery stops the model returns answers only; no settings proposal is
possible. One message may answer several questions for several diaries. The
server validates every diary, question, value and cited fact, saves the valid
answers in one revision and writes the reply from what was saved. Hours from a
message stay a proposal to confirm, a different answer to an answered question
is offered as a change, and a diary name the owner did not use is offered for
them to add. Diary answers use their own `diary` scope.

After the profile, customer-booking settings appear only for a bookable diary.
A diary the business arranges gets hours, capacity, resources and slots. Diaries
that need something not built yet, such as agreement from someone outside the
team, combined resources, rule-based repeats or a records view of dates, get no
settings and stay listed with their reason. The diary audit's engine extensions
remain separate reviewed work.

## Local verification and release

`tests/test_setup_context.py`, `tests/test_diary_contract.py`,
`tests/test_diary_profile.py` and `tests/test_diary_discovery.py` cover the
shared read, typed answers and scope, skip rules and support states, and the
discovery HTTP journey with disposable SQL, including the 24 October owner
sentence, multi-answer saves and the settings hand-off.

`tests/test_guided_setup.py` covers the real Help caller with synthetic model
transport, native card validation, HTTP handlers and disposable SQL. The notice
and horizon fixture prepares 24/60, edits to 24/45, moves Next/Back without saving,
then accepts the exact values and checks resumed answers and receipts. The
off journey finishes with no scheduling or invoice row.

`tests/test_guided_setup_browser.py` drives trusted native Chromium input over
loopback HTTP and disposable SQL. It covers the actual Knowledge handover,
diary discovery through to a confirmed profile, same-card explanations, scalar and weekday
previews, direct edits, native submit protection, Accept, pause/resume and device
restore. Screenshots cover phone, narrow phone, tablet and desktop layouts;
reduced viewport checks are separate from real on-device keyboard testing.

The `20261023_guided_setup.sql` migration extends the existing atomic writer's
page allowlist to the remaining native guide domains. It adds no grants or data
backfill. Its rollback restores the prior allowlist while retaining metadata and
receipts. The grouped onboarding and native setup-card migration chain must
already be installed. Deployment and
live E2E testing are separate release actions. Native Chromium emulation does
not establish real mobile keyboard behavior or live model quality. Usage and
time savings remain unknown unless directly measured.
