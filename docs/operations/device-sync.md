# Cross-device setup and conversations

Implemented behind `DEVICE_SYNC_ENABLED=false`. This document describes the
code and local verification, not a production migration or activation.

Install `db/migrations/20261020_device_sync.sql` before enabling the flag. The
server uses the existing service-role connection. Both tables have RLS enabled
and deny browser roles; authenticated routes derive the tenant and user from
the session. View as cannot read or write personal workspaces. Anonymous
assistant conversations remain local to their browser.

During initial onboarding, every participating device follows the same saved
location. Form edits are checkpointed after a short pause and before a normal
submission. Continue uses the real form handler and its existing validation.
The settings walkthrough continues this behavior on Diary settings, Booking
requirements, Dependants, CRM settings and Getting paid. Once the walkthrough
is complete, navigation is independent. Visiting another ordinary application
page does not opt that page into forced setup navigation.

Knowledge decisions, working wording, active batch, assistant messages and
the current guide focus are checkpoints, not approved Knowledge. Only the
existing final Save admits Knowledge. A changed stored review version prevents
old checkpoint fields from being applied to a newer review. Conflicting edits
are preserved on the current screen and require reconciliation by reloading;
they are not silently overwritten or automatically merged.

The signed-in Ask assistant uses a shared transcript. Automatic arrival turns
have a stable account-scoped operation identity, so two devices cannot start
the same interview twice. The server saves successful help and guide replies
before acknowledging them, even if the originating browser closes. Clearing
that conversation clears its shared transcript. Unsigned public, scenario and
draft-fix conversations retain their existing local behavior.

The business Concierge reads its existing permission-filtered saved history.
New messages trigger refresh hints. Each visible panel also checks for new
history every three seconds, including messages from other devices. Live voice
sessions retain their own audio connection; saved voice transcript segments
join the same history when persisted. Microphone control and live audio are
not transferred between devices.

Workspace clients read durable state every two seconds while visible and on
reconnect, online and visibility events. The application's existing event
stream accelerates app refreshes; polling also works across server processes.
Sleeping phones catch up when reopened. This is shared submitted work, not
per-keystroke mirroring of the assistant composer.

Claims and revisions are serialized in PostgreSQL. Replaying an operation
returns its recorded response. Different requests at an old revision or while
another request is unresolved are rejected before the handler runs. Existing
Knowledge admission and setup-card revision checks remain authoritative too.
If a process stops after claiming an action but before recording its outcome,
the unresolved claim deliberately blocks retries. An operator must reconcile
the stored business outcome before recording a terminal receipt with
`finish_device_sync`; do not delete the claim and blindly repeat the action.
This favors avoiding duplicate side effects over automatic retry after an
unconfirmed process failure.

State and operation receipts are retained until tenant deletion, which cascades
to both tables. They contain private conversation and draft content and belong
under the same access and deletion controls as the business's other records.

Local verification uses a disposable PostgreSQL cluster, actual HTTP handlers
and two separate native browser documents. Tests cover bidirectional Continue,
unfinished forms, Knowledge decision/focus handover, the settings handoff,
independent navigation after completion, messages, competing claims, stale
revisions, cached responses, account isolation, CSRF and database permissions.
No production accounts or paid models are used.

To disable, turn off `DEVICE_SYNC_ENABLED` before applying the paired rollback.
Rollback removes workspace checkpoints and replay receipts; it leaves normal
business settings, approved Knowledge and business Concierge messages intact.

Integration with the Knowledge review history preserves item-scoped, versioned
choice cards. Only a confirmed guide receipt settles a prepared choice; model
proposals leave choices unchanged and editor requests remain editing-only. The
server records this history before acknowledging a turn, so closing the sender
cannot lose it. Restored checkbox state retains the control's configured value.
A poll that finishes during an active guide turn waits for the next refresh;
it cannot replace that turn's native form snapshot.

Initial review hydration completes before readiness becomes available. A fresh
Knowledge page rederives its current pending decision once from the restored
form, including source reasons and overlap choices; persisted focus does not
replay an approval. Automatic guide-view identities include a canonical form
fingerprint, so changed review choices cannot replay the original first card.
Unchanged forms still share replay protection. Revision conflicts remain terminal
until reconciliation: settled readiness promises never schedule an automatic
retry or a repeated microtask loop. Later normal polls do not restart the guide.

A successful Knowledge request other than a guide turn records a refresh hint
naming its operation, so other devices reload and show the stored drafts. The
device that sent the operation recognises its own hint and keeps its page and
unsaved decisions. Applying a source correction also records the applied
wording and the advanced stored version in the review's form checkpoint within
the same revision, so a reload on any device restores the remaining note and
overlap decisions and derives the current note from them again. The review
form's own hidden version still changes only when Check changes and overlaps
renders a fresh preview; a Save or guide turn against the old version remains
refused until then.

A page's own concurrency version is never synced. Checkpoints skip
`settings_snapshot` and `invoice_snapshot` (with the CSRF token, the review
`version` and the sync fields) when capturing and when restoring, so a restored
draft cannot replace the version the server rendered with an older one.
Each synced form records the page it was captured on as well as the path it
posts to: a settings page's forms do not all post to the page itself (Diary
settings posts to `/app/settings/scheduling`, CRM settings to its per-stage
paths). Accepting a setup card writes that page's settings directly and leaves
its synced draft stale. A successful accept on the flow channel therefore drops
every synced form captured on that page (other pages' drafts are kept; a draft
an older client stored without its page is matched by its action) and records
a refresh hint naming the operation. Other devices reload and show the saved
values; the device that accepted reloads itself when the card's page is the
page it has open. Unsaved edits typed on that page before the accept are not kept in sync
afterwards. A Getting paid save that finds the settings changed since the form
opened redirects back to the page with a message and the latest saved values,
instead of answering with a bare 409; the redirect also clears the synced draft.
The other settings pages (Diary settings, Booking requirements, Dependants, CRM
settings) still answer that refusal as a 409 document.

The two-document native test runs the sync and guide clients with a small
transcript display adapter, actual guide decisions and disposable SQL. It does
not run the complete Concierge renderer. The separate choice-history replay
executes the actual Concierge card, send and transcript rendering functions.
The startup/conflict native test additionally runs the full Concierge renderer,
guide and sync clients on the rendered Knowledge page with isolated response
fixtures. It verifies complete hydration, real checkbox/FormData retention,
one current-note derivation and event-loop liveness after HTTP409; separate
HTTP/disposable-SQL tests cover actual automatic-operation replay identities.
The Apply native test runs the real source-correction routes behind the sync
wrapper for two documents with disposable SQL: it forces a poll between the
shared revision change and the Apply response, then checks that the originating
device keeps its page and decisions while the second device reloads and
recovers them.

The Getting paid replay runs the sync client, the dirty-save script and the
setup-card panel in one native document against the same wrapper and disposable
SQL: it messages the assistant, presses Accept all, and checks that the reloaded
form shows the saved values, carries the new concurrency version and keeps its
Save row hidden until an edit. A Diary settings replay, whose three forms post
elsewhere than the page, stores the untouched page through an assistant
request, accepts a card for the page and reloads: the inputs then equal the
server render, the Save rows stay hidden until an edit, and the flow holds no
form for the page.
