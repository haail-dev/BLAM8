# Founder outreach: egress repair and project boundary

The `/admin/outreach` job is founder sales tooling. It uses `vcrm_*` and
`admin_outreach_*`, a connected reply inbox and Resend. It is separate from the
customer-facing tenant outreach feature (`tenant_outreach_*`, CRM sequences).
Its presence in the platform process and database is an inherited coupling,
not a reason to present founder prospecting as a BLAM8 product feature.

## Egress repair

The minute tick previously downloaded 45 days of full email rows and repeatedly
downloaded all message payloads and per-message configuration snapshots. It
prepared drafts even while paused or outside the sending window.

The repaired tick scans only inbox identities/subjects/kinds within the same
45-day window, then fetches bodies in batches of at most 100 for unprocessed
inbound messages from exact known prospect addresses. Durable event IDs prevent
processed bodies being downloaded again after a restart. No timestamp cursor
can skip a late import or an inbox row previously seen before its sender became
a prospect. Unclassified known messages are still reconsidered, preserving the
existing reply/stop rules. No email data or suppression history is deleted.

Historical subjects are projected from JSON without their message bodies.
Preparation, candidate selection and digests read only the fields they use.
Snapshot comparison stays in Postgres and returns stale draft IDs. Only the
chosen send's payload is downloaded, before its existing atomic claim.

Reply/stop/bounce checks continue while paused and outside the working window.
Those scheduled ticks skip preparation; the explicit Preview action still
prepares drafts. Provider pagination, health checks, lease, claim, send cap,
approval invalidation, send idempotency and uncertain-send reconciliation remain
in place. A partial or failed reply-body read blocks sending and is retried on
the next tick.

This is a payload reduction, not a constant-cost claim: metadata scans still
grow with the retained history, and previously unclassified known messages may
need their bodies again. The regression fixture exercises 600 processed emails
and 600 historical template copies, with returned JSON below 1% of a single full
history download. That is synthetic evidence, not a production saving estimate.

## Release and rollback

1. Apply `db/migrations/20261018_admin_outreach_egress.sql` before deploying the
   application. It adds a read-only helper callable only by `service_role`.
   Existing rows and send functions are untouched. It can be applied twice.
2. Deploy the application after review and CI. A missing helper fails closed:
   preparation/sending are blocked rather than bypassing approval checks.
3. Check job results, reply cancellation and API logs. Compare daily PostgREST
   egress after rollout; hourly usage figures may lag. Never send a real outreach
   email solely to validate this change.
4. For rollback, deploy the previous application first, then optionally apply
   `20261018_admin_outreach_egress_rollback.sql`. Reverting restores the old bulk
   reads; retaining the unused helper is safe.

## Separate operator project

A separate repository, worker and Supabase project would isolate this tooling's
credentials, costs and release lifecycle. Moving only the worker would leave its
database egress charged to BLAM8's organization. A second Supabase project in the
same organization would still share that organization's quota.

Extraction must preserve prospect IDs, sends/provider receipts, approvals,
suppression and event history, and must replace direct reads of the BLAM8 reply
inbox with a narrow authenticated reply integration. Keep the old sender stopped
during cutover, import and reconcile state, verify reply delivery, then enable
one worker. Do not run two independently arbitrated senders or reset historical
touches. This repair prepares no new paid resources and performs no data move.
