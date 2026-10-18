# Concierge transparency & proactivity

> **Status (2026-09-27, confirmed by the clean-up pass):** A + B + C SHIPPED 2026-07-30 (de3b8b2); the trail rewritten to the real call + result 2026-09-21 (64b08ce). A + B seen live (08-31, 09-06, 09-26); C (badge / glow / 'While you were away') not yet proven live.

Spec for three connected features, 2026-07-30. Goal: the assistant shows its
working — where it looked, what it quoted, and what needs your attention —
without sending you to the agent log.

## Why

- The concierge streams "Searching your inbox…" hints while answering, but the
  frontend renders them in one transient typing bubble that is deleted when the
  answer lands. `tools_called` is accumulated server-side and written to
  `agent_trace` only — never sent to, or kept by, the client.
- Inbox drafts persist **no provenance at all**: `kb.retrieve` returns
  `{source, id, similarity}` rows, then `render_for_prompt` flattens them to
  prompt text and the metadata dies there (documented gap,
  `docs/modules/module-agent-trace.md` "Not (yet) traced").
- The chat bubble has no attention states (visible/hidden only). All proactive
  signals go out as web push; in-app there is only the inbox draft badge.

## A. Ghost trail (concierge + portal)

Every tool call an answer used stays visible as a small pill attached to that
answer — a permanent trail, not a transient status.

### Stream protocol (additive, backwards compatible)

`POST /concierge/business`, `/concierge/fix`, `/api/portal/chat` keep emitting
`{"type":"tool", ...}` per tool start, gaining:

- `detail`: short sanitised summary of the tool input (e.g. the search query,
  the contact name). Server-side allowlist per tool — never the raw input dict.
- The `done` event gains `trail`: the full list
  `[{name, hint, detail}]` so the client renders the final pills from one
  authoritative payload (pills during streaming are progressive rendering of
  the same data).

### Persistence

`concierge_messages` gains `trail jsonb default '[]'` — stored on the
assistant row alongside `links`. `/concierge/business/history` returns it;
the full-page concierge replays pills on reload. Widget sessionStorage log
stores `{role, content, links?, trail?}`.

### Rendering

- While streaming: each tool event appends a pill to a trail strip above the
  incoming answer position (replaces the single mutating status bubble).
  Current pill pulses; settled pills go quiet.
- After `done`: the strip stays, attached above that answer bubble.
- Click a pill → inline expansion showing `detail` ("looked for: *van hire
  Tuesday*"). No extra round-trips.
- Portal gets the same treatment (its `_TOOL_HINTS` already exist); portal
  trail is per-visit only (no server persistence — customer-facing history is
  deliberately not stored).

## B. Draft grounding (inbox)

Same pill language under a pending draft in the thread view: which knowledge
sources the draft was grounded on, click to see the quoted chunk.

### Capture

- `kb.retrieve` callers in the draft path (`pipeline.draft_for_email`,
  `pipeline.redraft_email_log`) keep the retrieval rows and persist
  `email_log.draft_sources jsonb`:
  `[{"source": str, "chunk_id": str, "similarity": float}]`.
- Emit `trace.emit(tenant_id, "kb_retrieval", email_log_id=..., detail={query
  (trimmed), results_returned, sources})` — closes the documented trace gap,
  mirroring `portal_kb_retrieval`.
- Old rows have `draft_sources = null` → no pills, no fallback fabrication.
  Zero-pill drafts are an honest "inferred, not from your data" signal.

### Rendering

- `_thread.html`: under the draft textarea, a "Grounded on" strip of pills,
  one per distinct source, strongest match first.
- Click → fetch `GET /app/drafts/{log_id}/sources` (new, auth'd, tenant-scoped)
  which returns the live chunk content for the stored chunk ids + similarity;
  shown in an inline expansion (same visual as the pill expansions in A).
  Fetch-on-click avoids duplicating chunk text into email_log.
- Chunks edited/deleted since drafting: endpoint returns what still exists;
  missing chunks render as "(source updated since drafting)".

## C. Proactive concierge

The bubble becomes the in-app notification surface; the panel opens with a
digest. Detection reuses the existing push triggers — no new detectors.

### Store

New table `concierge_notifications`:

```
id uuid pk, tenant_id uuid not null, kind text not null, title text not null,
body text default '', url text default '', created_at timestamptz default now(),
seen_at timestamptz
```

Index `(tenant_id, seen_at, created_at desc)`. RLS: tenant-isolated (it is
tenant-facing, unlike agent_trace). Retention: purge > 30 days in the
existing 03:30 purge job.

### Write path

`push.notify(...)` writes a notification row for every tenant-facing kind
**before** attempting web push, and regardless of `push_enabled()` (push is
delivery; the row is the record). Excluded kinds: `support_ticket` (operator
plumbing, routed to the platform mailbox tenant — not for tenants' own feed).
`notify_draft_ready` flows through `notify` so drafts are covered too, but
`draft_ready` rows auto-mark seen when the draft is approved/skipped is NOT
attempted in v1 — they age out on open like everything else.

### Surfacing

- `GET /concierge/notifications` → `{unseen: n, items: [...latest 20]}`.
- `POST /concierge/notifications/seen` → stamps `seen_at` on all unseen.
- Bubble: amber count badge + a slow soft glow (CSS keyframe, ~2.5s ease,
  `prefers-reduced-motion` disables) while `unseen > 0`. No bouncing. Glow and
  badge clear when the panel opens (which fires the `seen` POST).
- Panel: when unseen > 0 at open, a digest card renders at the top of the
  business tab: one line per notification (dot, title, relative time),
  each tappable → its `url`; plus a "What needs my attention?" chip that seeds
  the chat with a question the concierge answers from its existing tools.
- Live: SSE `notification` event published on row insert (in-process events
  bus); `live.js` forwards it → bubble badge refresh without reload. Poll
  fallback: badge count fetched once on page load.

## D. Styling (woven through A–C)

Not a separate reskin. While touching each surface: assistant bubbles get a
subtle brand tint (not flat white), trail pills use a muted accent
(slate→indigo tint) not grey, message arrival gets a soft fade/slide,
digest card uses the amber family already used for attention chips. All
Tailwind utilities inline, matching existing conventions; respect
`prefers-reduced-motion`.

## Explicitly out of scope (v1)

- Tool *results* in the trail (row counts are cheap but noisy; revisit).
- Persisting portal chat transcripts.
- Per-notification read state / a full notification centre page.
- New proactive detectors (knowledge-gap digests, weekly summaries) — the
  table + digest card make these one-line additions later.
- Concierge widget replaying `trail` for pre-change stored messages (absent
  field → no pills).

## Migrations

Additive only, applied to prod before the code deploys (Render auto-deploys
main): `concierge_messages.trail jsonb`, `email_log.draft_sources jsonb`,
`create table concierge_notifications` + RLS + index. Script:
`scripts/db/migrations/apply_concierge_transparency_migration.py` (psycopg2 direct, same
pattern as agent_trace migration).
