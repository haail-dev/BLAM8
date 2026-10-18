# Historical Inbox import

Connecting a mailbox enables ongoing new-mail sync. Historical import is a
separate operation, never an OAuth callback side effect. Onboarding and Settings
show the confirmed mailbox, purpose and scope before offering Import now, Skip
import or Decide later. Skipping/deferment does not pause new-mail sync or change
drafting/sending settings. Setup may continue without historical import.

Import copies sender, subject, body and conversation identifiers for up to 100
Inbox messages from the 14 days before the mailbox was first connected. This
fixed cutoff survives reconnects and retries, so post-connection messages are not
incorrectly described as pre-connection history. Automated messages are separated;
the importer creates no drafts, contacts or outgoing messages.

The authenticated, CSRF-protected choice names a tenant-scoped connection and the
displayed previous run. Settings-change permission is checked, including View as.
The database atomically records actor, decision, scope and explanation version.
Only an unconsumed Import choice can acquire a worker lease; the worker verifies
the current approved run before any provider access. No choice, old workers,
expired leases and unavailable approval state fail closed. Status GETs never start
work. Concurrent/replayed starts do not extend permission or create duplicate work.

Skip/later remain durable choices until a new explicit choice. A failed, partial
or interrupted run can be retried explicitly against the displayed run; expiration
alone cannot restart it. A completed import cannot be replayed. Reconnecting the
same provider/account preserves connection identity and import history. A different
connection has no inherited approval. Existing completed imports are not deleted
or retroactively labelled approved.

## Release and rollback

Apply `db/migrations/20261018_mailbox_import_choice.sql` before deploying the
application. It is idempotent and replaces the previous unrestricted service-role
claim with an approval-gated claim. No existing mailbox is imported by migration.
The UI reports unavailable status rather than starting work when the migration or
approval record cannot be read. No migration was applied to a live database during
development.

The paired rollback removes the new choice function/column but deliberately leaves
the claim function disabled. Restoring automatic import is not a safe rollback.
Already imported data and external connection state are not undone by code/schema
rollback. A worker already started before release cannot be retroactively prevented.

Focused fake-provider and disposable-PostgreSQL tests cover no-fetch-without-choice,
both providers' fixed date queries, explicit selection, permissions/CSRF, counters,
concurrent claims, replay, expiry recovery and skip/later. Existing Gmail watermark
and Microsoft new-message subscription behavior is retained. Offline evidence is
not proof of live provider consent screens or a real mailbox run.
