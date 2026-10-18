# Quotes — client-facing quote documents, tracked through to invoice

*Spec written 2026-07-30 from the competitor research in
docs/competitor-quoting-research.md. Supersedes the proposals half of CRM_design.md
§22 (whose invoicing half already shipped differently as docs/modules/module-invoicing.md;
its `proposals` schema, DocuSign integration and payment-detection flow are all
obsolete — payments landed via Stripe Connect webhooks, e-sign is in-house).
STATUS: Phases 1+2 BUILT 2026-07-30 (schema applied to prod; /app/quotes
surface, public /q/{id} view/accept/decline/question, sender, PDF,
convert-to-invoice, deposit-at-acceptance mint, the 3/7/pre-expiry auto-chase
job + expiry flip + keeps-looking escalation, concierge list_quotes + 7
confirm-first actions, portal list_my_quotes, CRM contact quote block +
acceptance deal nudge, admin quote funnel + Quoted £ column, /app/money
pipeline strip; tests/test_quotes.py + test_quotes_phase2.py). Phase 3 BUILT
same day: src/app/quote_autodraft.py — when the reply drafter prices an enquiry,
the quote doc drafts itself (tenant_config.quote_auto_draft off|propose|auto,
default propose; auto weaves the /q accept link into the outgoing reply on
both the approve and auto-send paths). ALL THREE PHASES SHIPPED 2026-07-30.
OPEN: live e2e (Julius).*

## Why

The quote thread already half-exists: the AI prices replies from rate cards / KB
pricing, stamps `bookings.quoted_amount`, flips the inbox label to Quote Sent, and
invoicing later bills at the stored quote. What's missing is the **document in the
middle**: a branded, itemised quote the client can view, ask about, and accept
online — with the acceptance recorded. Research says that document (and the chase
around it) is where jobs are won and disputes are prevented.

Positioning line the research supports: **"The quote writes itself from the enquiry —
then chases itself until it's answered."** Nobody else starts from the conversation.

## Design principles (each traces to a researched complaint)

1. **No editor.** Quotes are *generated* (from the enquiry thread + KB pricing/rate
   card) and *touched up* in the same plain line editor invoices use. The #1 category
   complaint is clunky editors; we refuse to have one.
2. **One responsive surface.** No separate app to be worse than desktop (complaint #2).
3. **Nothing quote-shaped is paywalled.** Optional items, deposits, view tracking and
   auto-chase are all included, every tier (complaint #3 — Jobber/ServiceM8 nickel-
   and-dime exactly these). Quotes = core module for ALL tenants, like invoicing;
   only the CRM *view* of them stays Pro. [Julius to confirm tiering.]
4. **The chain never breaks.** quote → booking → invoice share links; converting
   copies lines, never re-keys (complaint #4). Accounting handoff stays at the
   invoice level (quote CSV import doesn't exist in Xero/QBO — research brief).
5. **No silent failures.** Sends go from the tenant's own mailbox; views/accepts
   push to every device instantly (complaint #5).
6. **Acceptance is evidence.** Typed-name signature + timestamp + an immutable
   snapshot of exactly what was accepted (pain #6: scope disputes).

## Data model

```sql
quotes (
  id uuid pk, tenant_id, ref text,            -- QUO-1001…, next_quote_ref on
                                              -- tenant_counters.quote_seq (gapless,
                                              -- same plpgsql pattern as invoices)
  contact_id, deal_id, booking_id,            -- CRM + booking links, like invoices
  email_log_id,                               -- the enquiry/thread it answers
  title text, intro text, notes text,
  status text,        -- draft | sent | accepted | declined | expired | invoiced | cancelled
                      -- "viewed" is a stamp (viewed_at), not a status — mirror invoices
  valid_until date,   -- default issue + 30d (db.DEFAULT_QUOTE_VALID_DAYS)
  show_vat bool, vat_rate, subtotal, vat_amount, total,   -- server-computed
  show_item_prices bool default true,         -- lump-sum display option (pain #5)
  deposit_percent numeric,                    -- null = no deposit at acceptance
  version int default 1,                      -- bumped on any post-send edit; the
                                              -- public page always shows latest
  viewed_at, sent_at,
  accepted_at, accepted_name text,            -- typed-name e-sign (partners.py pattern)
  acceptance_snapshot jsonb,                  -- frozen lines+totals AS ACCEPTED
  declined_at, decline_reason text,
  deposit_invoice_id, invoice_id,             -- what it became
  reminders_sent int, last_reminder_at,
  created_by text,                            -- 'ai' | 'owner'
  created_at, updated_at
)

quote_lines (
  id, quote_id, tenant_id, description, quantity, unit_price,
  amount,                -- line TOTAL, same contract as invoice_lines
  is_optional bool default false,   -- client can tick/untick on the public page
  is_selected bool default true,    -- server-side state of that tick
  line_order int, kb_entry_id
)
```

Totals always recompute server-side from *selected* lines. RLS as per invoices.

## The public page — /q/{id}

The uuid is the capability (same doctrine as /pay/{id} and /track/{token}).

- Branded via the existing `invoice_settings` business header (one branding config,
  quotes + invoices never drift apart visually). First open stamps `viewed_at` +
  push (`tenant_config.notify_quote_viewed`, default ON — the research's single
  most-loved feature).
- Line items; optional lines render as tickboxes with the total updating live
  (server recomputes on toggle — the client's browser is never the calculator).
  `show_item_prices=false` renders descriptions + one total (lump-sum mode).
- **Accept**: typed full name → server freezes `acceptance_snapshot` (selected lines
  + totals + version), stamps accepted_at/accepted_name, pushes the owner, writes
  the CRM timeline activity, and emails both sides a confirmation copy. If
  `deposit_percent` is set: mint the deposit invoice (`create_invoice`, one line,
  linked back) and redirect straight to its /pay/{id} — sign-then-pay in one flow.
- **Decline**: optional reason; owner push; CRM timeline.
- **Ask a question**: text box → lands in the tenant's inbox as a needs_reply on the
  linked thread (portal Q&A machinery). No email ping-pong.
- After expiry the page still renders but Accept is replaced by "This quote has
  expired — request an updated one" (→ question flow). Statuses only auto-flip
  sent→expired, never accepted→anything.
- PDF download link on the page (fpdf2, invoice_pdf.py sibling) — the page is the
  quote; the PDF is a courtesy.

## Lifecycle

```
draft ──send──► sent ──client──► accepted ──convert──► invoiced
                 │                   │
                 │                   └─► deposit invoice (optional, at accept)
                 ├─client─► declined
                 └─daily job─► expired
```

- **Send** = email from the tenant's own mailbox (invoice_send.py sibling; same
  EU-AI-Act disclosure wrapper as all outbound), body = short note + /q/{id} link
  (never a bare PDF attachment — deliverability + tracking). email_log kind
  `quote`, category `quote_sent` (the existing label, now carrying a real doc).
- **Post-send edits** bump `version`; if already viewed, the client sees the newest
  version with a "revised {date}" line. Accepted quotes are immutable — revising an
  accepted quote means cancelling and re-issuing (the ref chain records both).
- **Convert to invoice** (from an accepted quote): lines copied from
  `acceptance_snapshot` (only selected optionals), minus any deposit already
  invoiced as a credit line; links `invoice_id` back, status → invoiced. Also
  offered from the booking's completion flow so the vertical draft builders prefer
  the accepted quote over recomputing. `bookings.quoted_amount` is stamped on
  acceptance so ALL existing invoicing readers keep working unchanged.
- **Quote number = job number = invoice number** in spirit: the three refs display
  together everywhere any one of them appears (praised Screwfix workflow).

## Auto-chase (the conversion lifter)

Daily job (`check_quote_chases`, sibling of check_overdue_invoices):
- Reminder drafts at **day 3 and day 7** after send, plus **3 days before expiry**
  (evidence-based cadence from the research; Tradify-style), each through the
  normal draft/approval queue respecting the tenant's auto_send ladder.
- Stops dead on accept / decline / expiry / any inbound reply on the thread
  (`reminders_sent` caps at 3; payment-reminder precedent: never touches the
  ignored-drafts counter).
- Expiry day: flip sent→expired, owner push "QUO-1007 expired unanswered".
- Escalation: a quote viewed ≥2 times but unaccepted after day 7 pushes "they keep
  looking — call them?" (Commusoft-grade signal, zero LLM).

## Concierge

Read tool `list_quotes` (returns `quote_id` handles, all tiers). Actions, all
propose → confirm → execute (single-use claim, 5-min TTL, invoice-action doctrine):
- `quote_create` — from a booking / thread / contact + KB pricing; lines editable
  in the card. This is the headline: "quote Mrs Patel for the boiler service we
  discussed" → draft quote card.
- `quote_send`, `quote_reminder_draft` (owner-initiated commercial sends — the card
  is the approval, same as invoice_send).
- `quote_mark_accepted` (client said yes by phone/email — records manual acceptance,
  `accepted_name` = who the owner says; snapshot still frozen),
  `quote_mark_declined`, `quote_convert_invoice`, `quote_cancel`.
- `quote_edit_draft`, `quote_delete_draft` (15 Oct 2026) — a DRAFT quote only, always on a
  card (`money:send`): the edit replaces the lines through `db.update_quote_lines` (the
  optional / ticked / job / deposit-rule flags are kept; the total counts ticked lines
  only); the delete is one guarded `DELETE` for a quote never sent, opened, answered or
  chased and with no invoice built on it. A sent quote is withdrawn with `quote_cancel`,
  never deleted. The delete is confirmed by a tap on its card only, never a spoken or
  typed yes (16 Oct 2026). See docs/modules/module-concierge-business.md.

## Pipeline + auto-draft (Phase 3, the moat)

When the AI drafts a priced reply to an enquiry, it ALSO proposes a quote doc
(same numbers — `pricing.quote` / `fsm_pricing.quote` / KB price), so approving the
reply can attach "View & accept your quote: /q/{id}". Tenant-configurable:
off | propose (default) | auto-attach-with-auto-send-ladder. This is the "quote
before leaving the driveway" moment and the feature no competitor can reach —
their quote starts at a form; ours starts already written.

## Portal

`list_my_quotes` on /c/{slug} (identity-gated, email-filtered, drafts never shown)
answers "what did you quote me?" with view/accept links; quick-action chip.

## CRM

Quote history block on the contact page (Pro view), matching contact_id + email
fallback exactly like `invoices_for_crm_contact`. Deal auto-nudge on acceptance:
Concierge transcript proposal to move the deal stage (the invoice-paid → Won
pattern at payments.invoice_paid_followup).

## Admin

`operator_tenant_detail` gains quotes sent/accepted/expired + acceptance rate +
quoted-£ vs invoiced-£; overview column "Quoted £". Their money, not ours.

## Build order

- **Phase 1 (core loop)**: schema + refs, /app/quotes list + new/edit (invoice
  editor sibling), /q/{id} view/accept/decline/question, viewed/accepted pushes,
  convert-to-invoice, quote_sent linkage. *Ships value on day one.*
- **Phase 2 (the upgrades)**: optional line items, lump-sum mode, deposit at
  acceptance (mint + redirect to /pay), auto-chase job + escalation, concierge
  actions, portal, CRM block, admin metrics.
- **Phase 3 (the moat)**: auto-draft quote alongside the priced reply; acceptance→
  booking-confirmation composition.

## Not built (deliberate)

Drag-and-drop template designer (the category's #1 complaint — refused) ·
multi-currency (GBP) · DocuSign/e-sign vendors (typed-name in-house) · quote-level
Xero/QBO API push (invoice-level CSV export already covers the handoff; API push =
someday) · dependent-option logic (option A forces add-on B — Quotient's gap, noted,
deferred) · margin/markup display (needs cost data we don't hold; revisit with
supplier pricing) · charging for quotes (forum debate, not a software feature).

## Reuse map (why this is cheap to build)

| Need | Already exists |
|---|---|
| Gapless refs | `next_invoice_ref` pattern on tenant_counters |
| Branding + VAT | `invoice_settings` / invoice_config.py |
| Public capability page | /pay/{id} + /track/{token} doctrine |
| Typed-name e-sign | partners.py enrolment (name + time + version) |
| Viewed stamp + push | invoices.viewed_at + notify_invoice_viewed |
| Deposit payment | Stripe Connect + create_invoice + /pay |
| Q&A on the doc | portal question → inbox needs_reply |
| Chase job | check_overdue_invoices shape + reminder doctrine |
| Confirm-first actions | concierge invoice actions contract |
| Priced lines | pricing.quote / fsm_pricing.quote / KB pricing |
| Sent-mail linkage | email_log kinds + quote_sent label |
