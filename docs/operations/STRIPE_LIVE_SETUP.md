# Switching Stripe to LIVE

What only you can do (account access). The env var names below match `src/app/config.py`
exactly; they're declared in `render.yaml` and their values are set in the Render
dashboard (`sync: false`).

> ⚠️ Test-mode price IDs and the test webhook secret **do not work in live mode** —
> you must create new LIVE products/prices and a new LIVE webhook endpoint.
> This switches on **real charging**. (The access gate `REQUIRE_SUBSCRIPTION_GATE`
> is separate — live keys enable charging; the gate stays off until you flip it.)

## A. In the Stripe Dashboard — toggle to **Live mode** (top-right)

1. **Activate live payments** if you haven't — business details + payout bank account.
2. **Products → create your plans in LIVE** (test products don't carry over). Each as a
   recurring monthly GBP price; copy each **Price ID** (`price_…`):
   | Plan | Price | → env var |
   |---|---|---|
   | BLAM8 Email | £39/mo | `STRIPE_EMAIL_PRICE_ID` |
   | BLAM8 Core | £99/mo | `STRIPE_STARTER_PRICE_ID` |
   | BLAM8 Core + TMS | £299/mo | `STRIPE_PROFESSIONAL_PRICE_ID` |
   | BLAM8 Scale | £799/mo | `STRIPE_SCALE_PRICE_ID` |
   | Enterprise | invoice-billed | `STRIPE_ENTERPRISE_PRICE_ID` (leave blank) |
   | Extra mailbox add-on | £39/mailbox/mo | `STRIPE_EXTRA_MAILBOX_PRICE_ID` |
3. **Developers → API keys (Live)** → copy the **Secret key** (`sk_live_…`) → `STRIPE_SECRET_KEY`.
   (Publishable `pk_live_…` → `STRIPE_PUBLISHABLE_KEY` is optional — currently unused by the app.)
4. **Developers → Webhooks → Add endpoint** (Live):
   - **Endpoint URL:** `https://blam8.haail.dev/stripe/webhook`
   - **Events:** `checkout.session.completed`, `customer.subscription.created`,
     `customer.subscription.updated`, `customer.subscription.deleted`,
     `invoice.paid`, `invoice.payment_failed` (or "send all events" — the handler ignores the rest).
   - After saving, copy the endpoint's **Signing secret** (`whsec_…`) → `STRIPE_WEBHOOK_SECRET`.
5. **Settings → Billing → Customer portal** → activate it in **Live** so "Manage subscription" works.

## B. In Render → `blam8` web service → **Environment**

Set/replace these with the LIVE values (saving restarts the service — applies immediately, no redeploy):

| Variable | Value |
|---|---|
| `STRIPE_SECRET_KEY` | `sk_live_…` |
| `STRIPE_WEBHOOK_SECRET` | `whsec_…` (the **live** endpoint's signing secret) |
| `STRIPE_EMAIL_PRICE_ID` | `price_…` (live) |
| `STRIPE_STARTER_PRICE_ID` | `price_…` (live) |
| `STRIPE_PROFESSIONAL_PRICE_ID` | `price_…` (live) |
| `STRIPE_SCALE_PRICE_ID` | `price_…` (live) |
| `STRIPE_ENTERPRISE_PRICE_ID` | blank (or a live price) |
| `STRIPE_EXTRA_MAILBOX_PRICE_ID` | `price_…` (live) |
| `STRIPE_PUBLISHABLE_KEY` | `pk_live_…` (optional) |

Also confirm `APP_BASE_URL = https://blam8.haail.dev` (drives the Checkout return URLs — should already be set).

## C. Verify (one real transaction)

1. Go to **/billing/plans**, subscribe to a plan with a **real card**.
2. You should land on `/billing/success` and the subscription should read **active**.
3. Stripe → Developers → Webhooks → your live endpoint → **recent deliveries are 200**
   (`checkout.session.completed` etc.); Render logs show no signature errors.
4. **Refund** the test charge in Stripe afterwards.

## Notes
- The app is pinned to `stripe==15.1.0` (see `requirements.txt`) — the live test above
  is also the check that this SDK version drives live Checkout/Portal/webhook correctly.
- Keep **test** keys on any staging service; only the prod Render service gets **live** keys.
- The signing secret is per-endpoint — the live endpoint has its own `whsec_…`, different
  from the test one.
