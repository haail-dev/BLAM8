# Draft suppression — stop drafting for contacts the owner ignores (2026-07-26)

Directive: stop generating drafts for contacts whose previous drafts have been
repeatedly ignored; flag them for owner attention instead. Cuts token waste on
never-answered senders (newsletters, digests) and surfaces the "you've been
ignoring this person" signal instead of burying it under a 5th unsent draft.

## The state machine

One `contact_draft_stats` row per (tenant, sender), `status`:

```
active ──(3rd consecutive unsent draft)──▶ suppressed
suppressed ──(2 more inbounds, owner silent — 5 emails total)──▶ permanently_ignored [auto]
suppressed ──(banner: Ignore permanently)──▶ permanently_ignored [owner]
suppressed ──(banner: Resume / any draft to them is sent)──▶ active
permanently_ignored ──(Un-ignore / Resume)──▶ active
```

* **The streak** (`consecutive_unsent_drafts`) increments when a draft enters
  the approval queue (`src/app/pipeline.py` after the non-informational insert) and
  resets to 0 when a reply to that contact is **sent** — human approve
  (`dashboard.send_draft`) or full-auto (pipeline auto-send), both via
  `suppression.record_sent`. Editing without sending, or skipping the draft,
  does NOT reset (the draft was still never sent — it was already counted at
  generation time). Sending also reactivates a `suppressed` contact;
  `permanently_ignored` survives a send (the owner opted out explicitly —
  only Resume/Un-ignore lifts it).
* **At the threshold (3)** the flip to `suppressed` happens as the 3rd unsent
  draft is generated; the *next* email from that sender costs zero LLM tokens.
* **fyi/automated inbounds** never advance the streak (they were filed as
  informational — there was no queued draft to ignore), and neither do
  no-reply senders or hard-ignored contacts (guarded earlier in the pipeline).
* **A one-off draft** (re-engagement break, or the banner's "Draft now") never
  advances the streak either — `record_draft_generated` no-ops unless the
  contact is `active`.

## What a suppressed contact still gets

The pipeline guard (`suppression.check_inbound`, before any LLM spend) logs the
inbound into `email_log` with `status='skipped'`, `category='fyi'` and a
"Drafting paused" reason — so the mail still arrives, still shows in the inbox
(under the FYI pill), CRM/portal/concierge are untouched. This is the soft
sibling of `ignored_contacts` (the hard ignore, which hides the thread and logs
nothing).

## The escalation banner (thread view)

While `status='suppressed'`, `dashboard._thread_data` passes the stats row to
`_thread.html`, which renders: *"⚠️ You haven't responded to {contact} in N
emails"* with three actions (all `POST /app/suppression/...`):

* **Resume drafting** — streak → 0, status → active.
* **Ignore permanently** — status → `permanently_ignored` (`suppressed_by='owner'`),
  silent from then on.
* **Draft now** — one-off draft for the latest email via the redraft path, row
  re-opened to `pending`; the streak is untouched.

`permanently_ignored` shows no banner (the owner already decided, or the fuse
burned out).

## Auto-suppression fuse

Each inbound arriving while `suppressed` bumps `emails_since_suppressed`; at 2
(i.e. 5 total unanswered emails) the status silently flips to
`permanently_ignored` with `suppressed_by='auto'`, logged in the Agent Log
(kind `suppression`, filterable as "Draft pauses").

## Re-engagement safety valve (§7)

An email from a suppressed/ignored sender that *looks unusual for them* still
gets a one-off draft: subject contains an urgency keyword (urgent, complaint,
cancel, legal, final notice/demand, overdue, deadline, asap) or the body is ≥3x
the sender's average inbound length (last 20 emails). The draft is forced
non-informational (an fyi call would defeat the point), lands `pending` with
reason "Unusual email from a paused contact — drafted a reply just in case",
and the break is logged in the Agent Log. The streak is not advanced or reset.

## Surfaces

* **Ignored page** (`/app/ignored`): second section "Drafting paused" lists
  every non-active contact with an Un-ignore button (→ `suppression.resume`).
* **Concierge**: `search_conversations` results carry a `suppressed_contacts`
  sidecar (top 10) and the tool description tells the model to mention them on
  "what needs replying?" questions.
* **Agent Log**: kind `suppression` — pause triggered, auto-suppression,
  re-engagement break.

## Deliberate non-changes (directive §"What NOT to Change")

Emails keep arriving; existing unsent drafts are not deleted; Concierge and
Portal drafting paths are untouched (only `pipeline.draft_for_email` guards);
the threshold is a constant (`suppression.THRESHOLD = 3`), not tenant config.

Everything in `src/app/suppression.py` is best-effort: any stats failure fails open
(draft as before) and never costs a tenant their draft.

Schema block: `db/schema.sql` marker
`-- Draft suppression — stop drafting for contacts the owner ignores (2026-07-26)`;
apply with `scripts/db/migrations/apply_draft_suppression_migration.py`.
Tests: `tests/test_draft_suppression.py`.
