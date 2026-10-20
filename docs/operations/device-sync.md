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

The two-document native test runs the sync and guide clients with a small
transcript display adapter, actual guide decisions and disposable SQL. It does
not run the complete Concierge renderer. The separate choice-history replay
executes the actual Concierge card, send and transcript rendering functions.
