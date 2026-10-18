# Partner settlement — subscription credit, then withdrawal

**Status:** 🔨 Built 2026-07-30, **switched OFF**. Code: `src/app/partner_settlement.py` (rules),
`src/app/partner_credit.py` (the Stripe credit, flag-gated), `src/app/db.py` (settlement +
withdrawal queries), `src/app/partner_routes.py` (`/partner/settlement`, `/partner/withdraw`),
`src/app/admin.py` (`/admin/withdrawals/{id}/pay|cancel`), templates `partner/home.html` +
`admin/partners.html`. Tests: `tests/test_partner_settlement.py` (42),
`tests/test_partner_credit_flow.py` (27).
**Migration:** `scripts/db/migrations/apply_partner_settlement_migration.py` — **not yet applied to prod**.
**Companion:** `docs/modules/module-invoicing.md`, and the agreement body in
`src/app/templates/partner/_agreement_body.html`.

---

## §0 Why

The second referral funnel. A tenant who refers *other* businesses takes the commission
off their own bill instead of receiving a bank transfer. "Refer two, yours is free"
(2 × 20% of £99 = £39.60 against a £39 Starter) is a far better pitch than "earn £19.80 a
month", it removes a manual bank transfer per partner per month forever, and — the part
that matters while chasing the first ten tenants — **cancelling then costs them the
discount**, so it buys retention that cash payouts don't.

Pure referrers (a bookkeeper who never uses BLAM8) stay on cash. They have no bill to
reduce, and forcing them to become a tenant just to be paid £19.80 a month would be
absurd.

## §1 The three rules everything else follows from

1. **Stripe will never pay a customer out.** A customer-balance credit can reduce an
   invoice to £0 and no further. Surplus commission does *not* quietly get paid
   eventually — it accrues unusable (5 Pro referrals on a £39 bill banks ~£60/month,
   ~£360 by month six). That is the *only* reason a withdrawal step exists.
2. **The retained buffer is for CLAWBACKS, not for securing the discount.** A refund
   reverses the commission on that payment, and chasing cash back is what to avoid. The
   user-facing framing is friendlier and equally true: *"we always keep your next month
   covered, so your service never lapses."*
3. **A big partner is LESS risky than a small one.** A clawback auto-deducts from the
   next statement (clause 4.7), so ongoing commission is itself collateral. Real exposure
   needs clawbacks *and* earnings stopping together. So the buffer grows only gently with
   run-rate (a 20% slice) and is **capped at 3× the subscription** — sizing it to "every
   referral refunds at once" would trap hundreds of pounds against a near-zero risk.

## §2 The flow

1. Commission earns into the existing ledger, unchanged (20% of collected, net-30 hold,
   clawbacks netted).
2. On `credit`, it applies as a **Stripe customer-balance credit** against the partner's
   own next subscription invoice, capped at the bill.
3. Surplus accrues.
4. Above **£50** the partner picks an amount on a slider (£0 → available) and requests it.
5. The operator sends the money, then records it on `/admin/partners`. A payout row is
   written with `method='bank'` and the settling commissions are marked paid.

## §3 Mechanics that are easy to get wrong

- ⚠️ **Credit is NOT Stripe Connect.** Connect (`src/app/payments.py`) is how a *tenant*
  collects from *their* customers — money into the tenant's own Stripe account. This is a
  **customer balance credit** on BLAM8's own billing: a credit is simply a smaller
  invoice. No transfer, no KYC, nothing to onboard. Confusing the two would also
  repurpose a Connect platform approved for something else.
- **Whole commission rows only** — never split one, or the ledger's
  one-row-per-collected-invoice invariant forks. `select_for_amount` therefore lands
  *near* the target, and which side depends on the path:
  - **cash → never overshoot.** Money out of the door must not exceed the ask.
  - **credit → overshoot by one row.** Otherwise £19.80 rows against a £39 bill credit
    only £19.80, leaving 60p outstanding and making "refer two, yours is free" a lie.
    Overshooting is safe because a credit larger than the invoice carries the remainder
    to the next one.
- **The settle set is chosen at PAYMENT time, never stored on the request.** A clawback
  landing between "I'd like £120" and the operator paying it must reduce what's paid. The
  request row holds only the amount.
- **Clawbacks are always included** in a settle set (clause 4.7). Including them first
  also lowers the running total, letting more positive rows fit.
- **One open request per partner**, enforced by a unique partial index rather than a
  read-then-write, so a double submit can't create two.
- **Settlement is status-guarded** (`settle_partner_withdrawal` filters on
  `status='requested'`), so two operators clicking at once can't pay twice.
- **Requested amounts are clamped, never trusted.** The UI is a slider, so an
  out-of-range value means a hand-built post.
- **Credit first, ledger second** in `apply_monthly_credit`. If Stripe fails we've changed
  nothing; the reverse order could give money away with no record of why.
- The Stripe call carries an **idempotency key derived from the settled row ids**, so a
  retry of the same settlement can't double-credit.
- One ledger for both methods: `partner_payouts.method` is `'bank'` or `'credit'`, and
  scheduled → paid still happens in exactly one place (`db.mark_commission_ids_paid`).

## §4 Before this can be switched on

`settings.partner_credit_enabled` is **False**. With it off, partners can see and choose
the mode and nothing is ever credited.

- ✅ **Migration applied to prod 2026-07-30**, verified from both the DDL side and through
  `app.db`. The partners table was empty at the time, so nothing existing was touched.
- ✅ **Agreement v2.3 shipped** — clauses 4.3 (the election), 4.9 (how credit applies),
  4.10 (withdrawals), 4.11 (retained cover) and 4.12 (VAT/invoicing).
  **4.11 was the important one:** the product holds back part of cleared commission
  against refunds, and without that clause it was doing something the contract never
  permitted. It states plainly that retained cover is not forfeited, remains the
  partner's, is visible to them, and is released on termination other than for their
  material breach.
- ⚠️ **VAT treatment — the only remaining gate.** Credit is a discount on our supply to
  them; cash is them supplying a service to us; and the moment a bill hits £0 and money
  flows, they've moved from discount to income. 4.12 allocates responsibility and allows
  BLAM8 to require a VAT invoice or agree self-billing — it does not decide the
  treatment. That needs an accountant.

⚠️ `TERMS_VERSION` was bumped to 2.3 while **zero partners existed**, so nobody is left
holding older terms. There is no re-acceptance flow by design — a partner's signed terms
are the ones they signed — so a future bump with live partners needs explicit
re-acceptance before new clauses apply to them.

Still undecided: the rolling-30-day payout hold discussed on 07-30. Clause 4.2 is
unchanged and the code still matches it (commission earned in a month is paid at the
following month end, i.e. within 30 days of that month's end). If the rolling version is
adopted it needs its own amendment, not a quiet code change.

## §5 Not built

Automatic monthly credit application (no job runs `apply_monthly_credit` — it's a
function waiting for a trigger, deliberately, until the flag can go on) · the
system-generated invoice *from* the partner *to* BLAM8 (the paperwork that makes the VAT
answer clean; withdrawals currently settle as a payout row + bank reference) · partial
settlement of a single commission row · per-partner commission rate overrides in the UI.
