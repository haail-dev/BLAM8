# Guided setup on actual application pages

The four initial onboarding steps retain their existing validation. Knowledge
Continue opens a brief introduction. Starting explicitly acknowledges it and
opens Diary orientation, followed by the existing settings owners in grouped
substep order. Existing completed tenants acquire no new mandatory flow.

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
with an Expand page control. No cloned form or screenshot replaces the editor.

Starting the guide carries forward reviewed Knowledge answers with their source
and exact identity, plus the owner's About you description. Unknown or stale
source answers remain unresolved. No extraction alone acquires approval.
Section reviews link directly to unanswered required decisions. A fresh tenant
can see the read-only Diary orientation before configuring its later intake
step; this does not bypass validation for creating a booking.

Guide orientations do not stamp whole legacy tours complete: unseen Diary,
Money, Today and Inbox
topics, including explicit drafting activation, retain their existing behavior.
No guide action creates bookings, invoices or payments, sends messages, enables
drafting or changes saved operation through relevance choices.

## Local verification and release

`tests/test_guided_setup.py` covers the real Help caller with synthetic model
transport, native card validation, HTTP handlers and disposable SQL. The notice
and horizon fixture prepares 24/60, edits to 24/45, moves Next/Back without saving,
then accepts the exact values and checks resumed answers and receipts. The
off journey finishes with no scheduling or invoice row.

`tests/test_guided_setup_browser.py` drives trusted native Chromium input over
loopback HTTP and disposable SQL. It covers the actual Knowledge handover,
incomplete-intake Diary orientation, same-card explanations, scalar and weekday
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
