# Support tickets — the chatbot raises tracked tickets

**Shipped:** 2026-07-25 · **Directive:** Julius, 25 Jul 2026 ("BLAM8 Chatbot Ticket
Support System") · **Engine:** `src/app/support.py` · **Chat wiring:** `src/app/concierge.py`
(ask mode) + `src/app/concierge_routes.py` (`POST /concierge/ticket`, lookup attachment
on `/concierge/ask`) · **Widget:** `src/app/static/concierge.js` (ticket + status cards)

## What it does

When the pre-auth website assistant hits a problem it can't resolve (the point
where it used to say only "email hello@blam8.com"), it now offers to raise a
support ticket, collects the details conversationally (one question at a time,
pre-filled from what the visitor already said), and shows a **review card**. The
visitor presses **Raise ticket** — the house draft-first rule: the button is the
only write path — and gets a `BLAM8-XXXX` reference. Asking *"what's the status
of BLAM8-1042?"* (plus the follow-up email) returns the ticket's live status as
a **server-rendered card**, so the model can never invent a status.

## Design decisions (adapting the directive to the real architecture)

1. **No new table, no Edge Functions.** The directive assumed a client-side
   Supabase app; BLAM8 is a server-side FastAPI app with a service-role client.
   Tickets extend the existing `feature_requests` table (directive §13.5:
   "extend, don't duplicate") — one pipeline for roadmap ideas, in-app reports
   and website tickets. Anonymous rows have `tenant_id NULL`, which RLS makes
   service-role-only automatically (NULL never matches `auth_tenant_ids()`).
2. **References** come from a global sequence via `next_ticket_reference()`
   (security definer). All pre-existing rows were backfilled.
3. **Two status vocabularies, one column:** issue rows (`kind='bug'`) use
   `open|in_progress|resolved|closed`; feature rows keep
   `new|reviewing|planned|done`. `app/support.STATUS_LABELS` maps both for
   display everywhere.
4. **Notification email** reuses the platform-notify pattern
   (`PLATFORM_NOTIFY_MAILBOX` → send from the company tenant's connected
   mailbox → `ACCESS_REQUEST_NOTIFY_TO`, default hello@blam8.com). Best-effort:
   a mail failure never loses the ticket (it still queues in `/admin/requests`,
   which also has the unseen badge).
5. **Confirmation is a button, not a chat "yes".** The model's structured
   `ticket` draft renders as a card; `POST /concierge/ticket` fires only on the
   press. Same pattern as the help-mode feature/bug submit card.
6. **Status lookups are resolved server-side.** The model only ever emits
   `{reference, email}`; the route queries the DB and attaches the result. The
   email must match the one the ticket was filed with (no enumeration by
   reference alone). Unreachable DB → an honest "can't check right now, email
   hello@" card.

## Guardrails

- **Secret redaction (server-side, `app/support.redact`)** on summary /
  description / error message: `password: x`-style pairs, bearer tokens, JWTs,
  credential query-params, card-number-shaped digit runs. The prompt also tells
  the model to refuse pasted passwords ("support never needs your password") —
  verified live. Prose like "I forgot my password" is untouched.
- **Anti-spam:** per-IP route limits (5/hour + 2/minute burst), a per-email cap
  (3/hour, DB-checked), and duplicate fold-in (same email + same body within
  10 minutes returns the existing reference — double-click idempotency).
- **Auto-collected triage** (never asked): page URL, user agent, severity
  (auto-`high` for `login_auth` — a broken sign-in blocks everything).
- **Retention (UK GDPR):** the daily sweep strips contact + device fields
  (`user_email`, `error_message`, `page_url`, `user_agent`) from resolved issue
  tickets older than `TICKET_RETENTION_DAYS` (default 365); the de-identified
  body stays as the bug-history record.
- **No conversation log stored** — only the bot's summary + description
  (directive §9 data minimisation).

## Operator flow

`/admin/requests` now shows reference, contact, severity, error message and page
context, and has a per-row **status + note** form (`POST
/admin/requests/{id}/status`). The note is quoted back to the user on a chatbot
status check and on `/app/requests`. Only the team changes status.

## Authenticated side

Help-mode bug/feature submissions (`POST /concierge/submit`) and the
`/app/requests` form now also mint references, store the tenant mailbox as the
follow-up email (so chat status checks work for them too), and issue rows start
`open`.

## Open items (Julius)

- Confirm `PLATFORM_NOTIFY_MAILBOX=admin.blam8@gmail.com` is set on Render —
  without it ticket emails are silently skipped (tickets still queue in admin).
- Confirm the 12-month retention default (`TICKET_RETENTION_DAYS`).
- The notification lands in the hello@ inbox, which the self-demo agent watches
  — if the agent starts drafting replies to ticket notifications, add the
  sender to Ignored contacts on the BLAM8 tenant.
