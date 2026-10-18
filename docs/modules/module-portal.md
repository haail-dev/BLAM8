# Customer Portal — public self-service page (Phase 1)

Every BLAM8 business gets a customer-facing page at `blam8.com/c/{portal_slug}`.
Customers book against the real diary, look up and reschedule their own
bookings by reference code, get quotes answered from the knowledge base, and
anything the assistant can't handle lands in the business's inbox. No customer
login, no app. Same brain as the email agent; other side of the counter.

**Invite-only (Kris 2026-09-24).** The portal serves customers the business
already knows. The email sign-off (reference code + link) is the invitation;
there is no public front door. An unverified visitor gets a zero-token gate
(`portal._identity_switch`) that accepts only a reference code, or an email
address on file (the code is emailed there — one reply either way, so the
gate never reveals who is a customer). The model never runs for an unverified
visitor, and `find_contact` never creates a contact (`not_invited`). Settings
no longer offer website snippets; `?intent=book` stages its ask only for a
visitor who arrived verified via `?ref=`.

Replaces: a Calendly booking link (conversational booking grounded in real
capacity), a Podium webchat widget (chat on a dedicated page), "call us to
book" phone tag, and "what's my appointment status?" calls.

## Files

| Piece | Where |
|---|---|
| Routes + slug/ref minting + sign-off footer | `src/app/portal.py` |
| Chat engine (prompt, schema, tool loop) | `src/app/portal_chat.py` |
| Tool catalogue + executor (ALL guards) | `src/app/portal_data.py` |
| Page template (standalone, mobile-first) | `src/app/templates/portal.html` |
| Settings section | `src/app/templates/app/settings.html` + `dashboard.py /app/settings/portal` |
| Schema | `db/schema.sql` "Customer Portal" block; `scripts/db/migrations/apply_portal_migration.py` |
| Tests | `tests/test_portal.py` |

## Identity: reference codes

`{PREFIX}-{4 chars}` (e.g. `ACME-7K3M`) — prefix = first word of the business
name (uppercase alnum, max 6), suffix from an unambiguous alphabet (no O/0/I/1).
Stored on `crm_contacts.reference_code`, unique per tenant, minted lazily by
`portal.ensure_reference()` on first outbound reply or first portal
identification. The code is a lookup capability (like `bookings.track_token`,
only human-typeable); the portal rate limits are what keep it un-guessable.

Every outbound EMAIL reply gains a server-side sign-off block (never written by
the LLM; stored draft stays clean — same wrap-at-send convention as
`src/app/disclosure.py`):

```
---
Your reference: ACME-7K3M
Manage your booking: https://blam8.com/c/acme-plumbing?ref=ACME-7K3M
```

Injection sites: the auto-send branch (`pipeline.py`, after `disclosure.wrap`)
and approve-and-send (`dashboard.py send_draft`). Proactive job emails
(reminders, recalls, chases) don't carry it yet — Phase 2 if wanted.
`portal.email_footer()` returns `""` on any failure or when the portal is off.

## Security model

- `tenant_id` derives from the slug in the URL — never from the request body.
- **Identity (hotfix 2026-07-26 — reference codes never appear in chat):**
  - Path 1: the customer types their reference code → `lookup_reference`
    verifies it and links the session. Tool results carry the code MASKED
    (`TEST-KR**`, `portal_data.mask_code`) so the model can't echo what it
    never saw; the prompt additionally forbids repeating a code the customer
    typed.
  - Path 2 (no code): `find_contact` with name+email. If the email already
    has a contact OR any bookings, it never links — the code is EMAILED to
    the address on file (`portal.send_verification_email`, audit-logged via
    `agent_events` kind `portal_verification`; 3/email/hour + 5/IP/hour) and
    the customer types it back into Path 1. An email with no contact and no
    bookings is refused (`not_invited`) — invite-only, nothing is created.
  - Sticky identity across turns is an opaque server-side session token
    (in-memory, 6h TTL, tenant-scoped) minted after a verified turn — the
    full code never rides the chat wire or sessionStorage. The
    `/api/portal/lookup` name+email path returns `verification_required`
    and no data.
- Identity-gated tools refuse until linked; every booking read/write filters
  to the identified contact; `move_booking` additionally ownership-checks the
  row.
- Writes are three: `create_booking` / `move_booking` (both through the same
  advisory-locked capacity RPCs as the email agent — racing customers can't
  overbook) and `escalate_to_business`. No deletes, no sends, no CRM access
  beyond the caller's own contact.
- Rate limits (per IP, in-memory): chat 8/min + 20/hour, lookups 12/hour,
  page 60/min, bookings 5/day per (tenant, visitor).
- Prompt injection: customer input and tool output are both declared untrusted
  data in the system prompt; the real guards are server-side anyway.
- A switched-off portal 404s identically to an unknown slug (the off switch
  shouldn't confirm the business exists).

## Chat

`POST /api/portal/chat` streams NDJSON exactly like `/concierge/business`
(`{"type":"tool","hint"}` … `{"type":"done","reply","customer"}`). The client
keeps history in-page and its reference code in sessionStorage; the code
re-verifies server-side every turn. Tool loop capped at 8 calls; reply-only
structured output; `llm.create` is the only call site (`operation=portal_chat`,
`module=portal`).

Without the scheduling module the diary tools drop out of the catalogue and
the assistant gathers details + escalates instead.

## Reschedules (2026-07-31)

`settings.portal_reschedule_mode` (tenant_config, Settings → Customer Portal,
scheduling tenants only):

- **`auto`** (default, the original behaviour): `move_booking` applies the
  customer's move on the spot — ownership-checked, capacity-locked — then
  notifies the business (push + Agent Log + CRM timeline + SSE repaint).
- **`approve`**: `move_booking` becomes a REQUEST. Nothing in the diary
  changes; a `needs_attention` inbox row lands (same channel as escalation,
  body carries booking ref + old slot → requested slot), plus an
  `agent_events` row (`reschedule_requested`), a push, and a
  `reschedule_request` CRM activity. The business replies through the normal
  approval flow and applies the move themselves (diary or concierge). The
  requested slot is NOT held — the business may counter-offer, and a pending
  request must never block other customers booking. The prompt flips too: the
  assistant frames the move as a request and never claims the new time is
  booked (the tool-trail pill reads "Passed your reschedule request…").

Budgets: bookings 5/day per visitor-IP per business, and (new) reschedules
5/day likewise — before this, moves had only the 20/h chat ceiling.

## CRM timeline (2026-07-31)

Portal activity is first-class on the contact's `crm_activities` feed (and
bumps `last_activity_at`, so portal-only customers sort properly in the
contacts list):

- `calendar_event` — portal bookings ("Booked via the portal …", id-keyed so
  re-runs dedupe) and auto-mode moves (slot-suffixed key, so repeat moves each
  land). Business-side confirms write the same type: approve-&-send +
  auto-send hold confirms, manual diary/board adds, and the diary's Confirm
  button (email-shaped contacts only; the CSV import path is deliberately
  excluded).
- `reschedule_request` — approve-mode requests.
- `portal_chat` — an identified customer's question ("Asked on the portal:
  …", capped 200 chars, the contact's reference code masked out). Only turns
  that consulted business data (KB/availability/invoices/quotes) land;
  identity handshakes, thank-yous, and turns whose action already wrote its
  own row stay out.
- Escalations ingest through `crm.ingest_email_log_row` immediately (before,
  they only appeared after the owner replied).

The drafter reads the feed (`build_crm_context` "Recent activity" block,
non-mail rows only) and the concierge got a `get_contact_timeline` Pro tool —
"what has Sarah been up to?" now includes portal behaviour. The Agent Log
gained a "Customer portal" group filter over the portal-written kinds.

## Escalation

`escalate_to_business` inserts a normal `email_log` row (`status=pending`,
`thread_state=needs_attention`, `category=needs_reply`, empty draft) — the
owner replies through the standard approval flow (Redraft works on it), the
reply goes to the customer's email and carries the reference footer. Push
notifications (via the new generic `push.notify()`) fire on booking created,
booking moved, and escalation.

## Settings (Settings → Customer Portal)

Portal on/off (default ON), web address (slug — unique, validated), description,
service area, welcome message (shown above the invite gate), "quote prices in
the portal" toggle (off = the
assistant takes details for a personal quote instead of quoting KB prices),
and — for scheduling tenants — the customer-reschedule mode (move straight
away vs ask-me-first; see Reschedules above).
The slug is minted lazily from the business name on first Settings visit.

## Phase 1 scope cuts (deliberate)

- No logo upload (no public asset storage exists yet) — the page shows a
  monogram tile. No "which services shown" picker — services line comes from
  the diary's `service_types` names.
- No standalone `/api/portal/book` / `/api/portal/availability` endpoints —
  booking happens through the chat (the spec's whole UX); fewer public write
  surfaces.
- No customer cancel tool — cancellations escalate to the business.
- No signed link tokens — `?ref={code}` (the code is already the capability).
- Portal chat transcripts aren't persisted; bookings/moves/escalations are
  recorded as `agent_events` + inbox rows (+ since 2026-07-31, summarised
  `crm_activities` rows — still deliberately not a transcript).
- Pages carry `noindex` for now (revisit with the Phase 2 directory).

Phases 2-4 (directory, location matching, reviews, embeddable widget,
marketplace) are out of scope until there are enough businesses.
