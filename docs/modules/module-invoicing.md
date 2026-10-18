# Invoicing — standalone core module + payments

*Written 2026-07-26 alongside the Invoicing & Payments enhancement directive. This file
was referenced by ten code sites before it existed; it now records both the original
design and the extraction/enhancement.*

## The shape

Invoicing is ONE shared engine for every tenant:

- **Engine** — `invoices` + `invoice_lines` (db/schema.sql), `db.create_invoice`
  (totals + VAT computed server-side), atomic per-tenant refs `INV-1001…`
  (`next_invoice_ref` plpgsql upsert on `tenant_counters.invoice_seq` — gapless,
  never reused; cancelled/void keep their number).
- **Draft builders** — each vertical contributes only *how completed work becomes
  lines*: `tms` (delivered loads at quote/rate card), `fsm` (completed jobs),
  `scheduling` (completed appointments/stays — live since 2026-07-26, previously
  behind `OFFICE_INVOICING_ENABLED`). Registry: `invoicing._BUILDERS`.
- **Modular settings** — `invoice_settings` (VAT toggle+rate, business header,
  payment terms, footer, bank details). Shaped by `src/app/invoice_config.py`; applied
  identically to the HTML view and the fpdf2 PDF (`src/app/invoice_pdf.py`).

**Since the extraction (2026-07-26)** invoicing is a **standalone core module**:
active for every tenant (the `/app/modules` card is computed, never a subscriptions
row), no Field OS dependency. The generate-from-period flow still needs a vertical;
standalone invoices come from **/app/invoices/new** or the **Concierge**
(`invoice_create` from a booking + KB pricing).

## Data model (post-enhancement)

`invoices` gained: `contact_id`/`deal_id`/`booking_id` (CRM + booking links),
`issue_date`, `due_date` (default: issue + 14 days — `db.DEFAULT_INVOICE_DUE_DAYS`),
`notes`, `created_by` ('ai'|'owner'), `amount_paid`, `payment_method`
(card|bank_transfer|cash|other), `stripe_payment_intent_id`, `payment_link`,
`viewed_at`, `overdue_notified_days` (last milestone pushed), `reminders_sent`,
`last_reminder_at`. `invoice_lines` gained `quantity`, `unit_price` (`amount` stays
the line TOTAL — every legacy reader unchanged) and `kb_entry_id`.

Statuses: `draft | sent | paid | partially_paid | overdue | cancelled | void`.
"Viewed" is a stamp (`viewed_at`), not a status. `overdue` is stamped by the daily
job AND derived in real-time readers (`sent` + `due_date < today`). Balance due is
always derived (`total - amount_paid`), never stored.

**VAT**: defaults **off** for tenants with no settings row (most aren't VAT
registered — Companies House registration for BLAM8 itself lands 31 Jul 2026); a
registered business ticks Show VAT (rate pre-filled 20%). No VAT filing/reporting.

## Payments (src/app/payments.py)

- **Stripe Connect OAuth** — the tenant connects their OWN Stripe
  (`tenants.stripe_account_id`; no keys stored). Env: `STRIPE_CONNECT_CLIENT_ID`,
  `STRIPE_CONNECT_WEBHOOK_SECRET` (+ the existing platform `STRIPE_SECRET_KEY`).
  Money lands in the tenant's Stripe; BLAM8 takes no cut.
- **Pay Now** — `payment_link` = our public `/pay/{invoice_id}` page (the uuid is
  the capability, like `/track/{token}`). The page mints a FRESH Checkout Session
  on the connected account per click (sessions expire in 24h; our link doesn't) and
  first-open stamps `viewed_at` (+ opt-in push, `tenant_config.notify_invoice_viewed`).
  No Stripe → the same page shows the invoice + bank-transfer details
  (`invoice_settings.bank_*`), and the owner marks paid manually.
- **Webhook** — `POST /webhooks/stripe` (Connect endpoint, own signing secret;
  the platform-billing webhook at `/stripe/webhook` is untouched). Signature-verified;
  idempotent twice over (`stripe_events` id wall + `mark_invoice_paid`'s
  status-guarded update). Routes ONLY `metadata.kind == 'invoice'` events:
  `payment_intent.succeeded` → paid + followup; `payment_intent.payment_failed` →
  agent event + push. Every event traces as `stripe_webhook`.
- **Paid followup** (`payments.invoice_paid_followup`, shared by webhook / manual /
  concierge): owner push (`invoice_paid`), agent-log entry, CRM timeline activity,
  and — when `deal_id` is set — a Concierge transcript nudge proposing the move to
  Won (the owner's reply mints the normal confirm-first proposal; nothing
  auto-executes).

## Concierge

Read tool `list_invoices` (all tiers) returns `invoice_id` — the handle every
action must use (same contract as `booking_id`). Action kinds, all through
propose → confirm → execute (single-use claim, 5-min TTL): `invoice_create`
(contact + line description/amount from the KB price + optional booking),
`invoice_send`, `invoice_mark_paid` (payment_method), `invoice_reminder_draft`
(editable draft in the card), and, for a DRAFT invoice only (15 Oct 2026, always on a
card, `money:send`): `invoice_edit_draft` (replaces the lines through the same
`db.update_invoice_lines` the invoice page's editor uses; the card shows before and
after) and `invoice_delete_draft` (one guarded `DELETE`, refused for anything ever sent,
opened, payment-linked or paid, or that a quote was built on; the number is never
reissued: refs are a counter). The delete is confirmed by a tap on its card only, never a
spoken or typed yes (16 Oct 2026; the server refuses any confirm without the card button's
`confirm_via: "tap"` marker). See docs/modules/module-concierge-business.md. Plus `update_deal_value` (Pro) for setting deal value
from invoice totals. **Doctrine note**: `invoice_send`/`invoice_reminder_draft` DO
email on Confirm — an owner-initiated commercial send where the card is the
approval; reply drafting still goes through the inbox queue. Both compose the one
shared sender, `src/app/invoice_send.py` (also used by the Send button).

## Portal

`list_my_invoices` (identity-gated, server-side email filter, drafts never shown)
answers "do I owe anything / can I pay / when did I last pay", with `pay_url` per
payable invoice; "My invoices" quick-action chip; prompt rule 5b. Portal spend
attribution: `list_my_invoices` is in `portal_chat.BUSINESS_TOOLS`.

## Inbox integration

- Payment confirmations (Stripe/PayPal/… senders, or receipt phrases) are caught
  rule-based pre-LLM (`pipeline.looks_like_payment_confirmation`): categorised
  `automated` when they name an invoice we hold (linked by ref in the reason),
  `fyi` otherwise; never drafted, zero tokens. A confirmation for an invoice the
  webhook did NOT settle surfaces "want me to mark it as paid?" (event + push).
- Sent invoices/reminders land in the Sent list as `quote_sent`
  (kind `invoice` / `invoice_reminder` email_log rows).
- **Draft suppression**: payment reminders never touch the ignored-drafts counter
  (`record_draft_generated` is called only from `pipeline.draft_for_email`); the
  chase signal is `invoices.reminders_sent`, and ≥3 reminders escalates as
  "hasn't paid", not "hasn't responded" (daily job).

## Jobs

`src/app/jobs/check_overdue_invoices.py` (08:45 UTC): flips due `sent` invoices to
`overdue`, pushes once per 7/14/30-day milestone (`overdue_notified_days` dedupes),
escalates the ≥3-reminders case.

## Admin

`usage.operator_tenant_detail` carries `invoices` / `invoices_all_time`
(sent/paid/overdue counts, invoiced/collected/outstanding, avg days to pay) →
"Invoicing revenue" card on the tenant page; the overview table has an
"Invoiced £ in" column. Their money, not platform revenue.

## Gap analysis (what existed before 2026-07-26 vs what this build added)

Existed (working): shared surface for tms/fsm, generate-from-period, draft line
editing, PDF email from the tenant's mailbox, manual mark-paid, modular settings,
atomic refs, RLS. Existed but flagged off: office builder. Missing entirely (added
by this build): standalone module status for all tenants, due/issue dates, CRM
contact/deal links, payment concept of any kind (Stripe Connect, Pay Now links,
webhook, bank fallback), Concierge tools, portal visibility, payment-email
categorisation, invoice pushes, overdue chasing, revenue reporting, `/invoices/new`,
uniqueness guarantee on refs.

## Generate flow hardening — 2026-07-30

Four changes to the generate path, in order of severity.

**1. Duplicate-invoice guard (was a live double-billing bug).** `list_completed_*`
filtered on `status='completed'` plus the date window and nothing else, so generating
for July and then for June–August invoiced the same jobs **twice**, silently. Every
`*_drafts_view` now passes its bookings through `invoicing._unbilled` before the
builder runs. The key is `invoice_lines.booking_id`, which all three builders already
stamp — dates and mail threads are both fuzzy, the booking id is exact.
`db.invoiced_booking_ids` deliberately uses two queries rather than one join, because
a bare select on `invoice_lines` would also hold work billed by an invoice that was
later **cancelled or voided**, and that work should be re-invoiceable
(`db.INVOICE_DEAD_STATUSES`). Filtering happens on the BOOKINGS, not the finished
drafts, so the pure builders stay list-in/list-out for their unit tests and per-draft
totals never need recomputing.

⚠️ The guard has one dependency worth protecting: the line editor deletes and
re-inserts every line, and it used to **drop `booking_id`** — so editing a draft
silently freed its work to be invoiced again. `booking_id` now rides hidden on every
editor row (including the blank "add a line" rows, so the posted columns stay
index-aligned) and `update_invoice_lines` persists it. Mis-alignment would be worse
than dropping it — a line would claim another job — so `zip_longest` plus a route test
pin the pairing. `quantity`/`unit_price` are still dropped on edit; the editor doesn't
expose them.

**2. Generated invoices are linked to their CRM contact.** The bulk path was the one
creation route that never passed `contact_id` — `/invoices/new` and the concierge path
both did — so every invoice it produced was orphaned from the CRM. Contacts pre-fill on
every tier, so this works on Starter too; only *viewing* the CRM is Pro.

**3. Invoice history on the CRM contact page.** `db.invoices_for_crm_contact` +
the existing `summarize_invoice_rows`: billed, paid, outstanding, average days to pay,
and the invoice list. It matches on `contact_id` **and** on `customer_email` for rows
whose `contact_id` is null, so invoices generated before (2) appear without a backfill
migration. Kept separate from `invoices_for_contact`, which is the customer PORTAL's
reader and is deliberately email-only and verified-scoped — a change to the owner's
view must never be able to widen what a customer sees. Wrapped best-effort: a tenant
who has never touched invoicing must not 500 their contact page.

**4. Customer selection on generate.** Tick-box per customer in the preview, all
ticked by default; an empty selection still means *everything*, so any caller posting
no selection behaves exactly as before (`invoicing.select_drafts`). This also fixed a
latent trap: the date inputs used to live in the POST form while the preview came from
the URL's query params, so editing the dates without reloading left the preview
describing a different period from the one being billed. The window is now chosen with
a **GET** ("Preview") and the POST carries the period the preview was built from —
harmless when Generate meant "everything", dangerous once you tick individual
customers. Non-CRM tenants get a locked Pro teaser that sells what actually becomes
true (invoices filed against the customer's record), not a feature list.

⚠️ Layout note: the invoices page was verified in a real browser at 375px and 768px.
Tests can only see markup — the first cut of the export control was clipped by the card
edge on a phone and no test noticed. `.claude/launch.json` has a `layout-snapshots`
entry that serves rendered pages for exactly this check.

## Not built (deliberate)

VAT filing/reporting · recurring invoices · quote-to-invoice conversion ·
expense tracking · multi-currency (GBP only) · customer-facing push · CRM-sourced
picking of a client who has **no** completed work (different feature; the selection in
(4) can only ever offer customers who genuinely have billable work).

## Card payments panel, refunds, pay-all, bank methods (2026-09-18)

Spec and build order: **docs/plan-payments-panel.md**. In one line: the owner's
read of their connected Stripe account inside BLAM8 (`/app/payments`,
`src/app/payments_panel.py`: balance, next payout, today, the last 30 days with
Stripe's fee per payment, refunds beside each invoice), refunds from BLAM8 or
from Stripe's dashboard landing identically (`payments.invoice_refund_followup`,
keyed on the charge's absolute `amount_refunded`), several open invoices in one
checkout (`/pay/{id}/checkout-all`, `metadata.kind='invoice_batch'`), bank
methods on the Pay Now page from an owner-set invoice size
(`invoice_settings.extras.bank_methods_min_gbp`), and the Connect webhook grown
to `payment_intent.processing`, `charge.refunded` and `charge.dispute.created`.
Standard accounts + direct charges stay; BLAM8 takes no application fee.
