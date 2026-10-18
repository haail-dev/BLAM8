# Invoice CSV export — Xero, QuickBooks, and a plain accountant sheet

**Status:** ✅ Shipped 2026-07-30. Code: `src/app/invoice_export.py`, route in
`src/app/dashboard.py` (`GET /app/invoices/export.csv`), UI on `src/app/templates/app/invoices.html`,
tests `tests/test_invoice_export.py` (28).
**Companion docs:** `docs/modules/module-invoicing.md` (the invoicing surface this exports from),
`docs/partner-pitch-bookkeepers.md` (why this exists commercially).

---

## §0 Why this and not an accounting module

A full accounting module was considered and rejected on 2026-07-30. The decisive
argument is commercial, not technical: **bookkeepers are the highest-multiplier route
to customers, and a bookkeeper will not refer software that replaces them.** Secondary
reasons: accounting is regulated (VAT returns need HMRC-recognised software), a wrong
return is the customer's penalty rather than our embarrassment, and Xero/QuickBooks are
entrenched on bank feeds we can't shortcut.

So the rule is **integrate where we don't compete, replace where we do.** BLAM8 owns the
*source documents* — it raised the invoice, it knows when it was paid. The ledger stays
with whatever the customer's bookkeeper already uses. This export makes BLAM8 *more*
referable by that channel, because it makes their job easier.

CSV before API, deliberately: one file serves Xero, QuickBooks, FreeAgent and Sage at
once with no OAuth app to certify and no integration to maintain. A real API sync waits
until a paying customer asks, and starts with Xero (UK small-business dominant), then
QuickBooks. Four integrations for zero customers is the classic pre-revenue trap.

## §1 The three formats

| Format | Shape | When |
|---|---|---|
| `accountant` | ISO dates, every field held including `status` / `paid_at` / `payment_method`. Lossless. | **Default.** Any package or spreadsheet reads it; it's also what a bookkeeper actually wants. |
| `xero` | Xero sales-invoice import columns, one row per line item with header fields repeated, UK `dd/mm/yyyy`. | Importing straight into Xero. |
| `quickbooks` | QBO invoice-import equivalent, `Taxable` Y/N. | Importing straight into QBO. |

`GET /app/invoices/export.csv?format=&from=&to=&status=&account=&tax=`

`from`/`to` filter on `issue_date` inclusive; `status` is an exact match; `account` and
`tax` override the org-specific fields below. An unknown `format` silently falls back to
`accountant` rather than erroring.

## §2 The two fields we cannot know

⚠️ **Account/nominal code** and **tax rate name** are properties of the customer's own
chart of accounts. We default to Xero's UK standard Sales code (`200`) and derive the tax
type from the tenant's VAT toggle (`OUTPUT2` when VAT is on, `NONE` when off — most BLAM8
tenants are not VAT registered). Both are overridable on the query string, and the UI
tells the user to check them before importing.

A blank account code makes Xero reject the row, so a default beats a blank — but it is
still a guess about their org, and the copy says so rather than implying we know.

## §3 Invariants worth not breaking

- **`quantity × unit_amount == line total`, always.** `invoice_lines.amount` is the line
  TOTAL and is authoritative; `unit_price` is not, because `db.create_invoice` defaults it
  to the full amount even when a quantity was passed. The export therefore *derives* the
  unit from the total at 4dp (both Xero and QBO accept 4dp) rather than trusting the
  stored `unit_price`, which would export 2 × £100 for a £100 line.
- **An invoice with no lines still exports one row**, described from its notes or its ref
  and carrying its subtotal. Silently dropping an invoice from an accounting export is a
  data-integrity bug; a thin description is not.
- **An invoice with no `issue_date` survives date filtering.** Same reasoning.
- **VAT is invoice-level, never line-level** (see `db.create_invoice`), so exported lines
  are always ex-VAT and the target package derives tax from the tax type / taxable flag.
- **CRLF line endings**, matching `src/app/spreadsheets.py` — Excel is the usual first stop.
- ⚠️ **`/invoices/export.csv` must stay declared BEFORE `/invoices/{invoice_id}`.**
  FastAPI matches in declaration order, so reordering them makes the uuid route swallow
  `export.csv` and 404 a path that exists. There's a test pinning this.
- `invoice_lines_by_invoice` batches the line fetch (chunked at 100, because an `in_`
  filter travels in the query string) so the export isn't N+1 across invoices.

## §4 Not built

- API sync to Xero/QuickBooks (OAuth, push invoices, pull payment status). Do it when a
  paying customer asks; Xero first.
- Payment/credit-note export — only invoices today. Payments are visible via `status`,
  `paid_at` and `payment_method` on the accountant sheet, which covers reconciliation.
- Nominal-code mapping per service or line type. One code for everything today.
- Scheduled/automatic export. Manual download only.
