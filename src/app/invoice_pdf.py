"""Invoice PDF generation (docs/modules/module-invoicing.md — Move 2).

Pure: an invoice row + its lines + the tenant's invoice settings in, PDF bytes out
(fpdf2 — pure-python, no system libraries, so it runs on Render unchanged). Renders the
SAME modular sections as the on-screen invoice — the business header, the VAT line,
payment terms, the footer — gated by the settings, so the PDF a customer receives matches
what the tenant configured.
"""
from fpdf import FPDF

from app.dates import pretty_date


def _s(v) -> str:
    """fpdf2's core fonts are latin-1; drop anything outside it so a stray character can
    never crash PDF generation for a whole invoice."""
    return str(v if v is not None else "").encode("latin-1", "replace").decode("latin-1")


def _money(v) -> str:
    return _s(f"£{float(v or 0):,.2f}")   # £ is latin-1 (0xA3), safe in core fonts


def _sec_business_header(pdf, inv, lines, settings):
    has_business = settings.get("business_name") or settings.get("business_address") \
        or settings.get("business_vat_no") or settings.get("business_contact")
    if not (settings.get("show_business_details") and has_business):
        return
    if settings.get("business_name"):
        pdf.set_font("Helvetica", "B", 14)
        pdf.cell(0, 7, _s(settings["business_name"]), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    for ln in str(settings.get("business_address") or "").splitlines():
        if ln.strip():
            pdf.cell(0, 5, _s(ln), new_x="LMARGIN", new_y="NEXT")
    meta = []
    if settings.get("business_contact"):
        meta.append(str(settings["business_contact"]))
    if settings.get("business_vat_no"):
        meta.append("VAT " + str(settings["business_vat_no"]))
    if meta:
        pdf.cell(0, 5, _s("  ".join(meta)), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)


def _sec_core(pdf, inv, lines, settings):
    """Title + meta, the line table, and totals — one welded unit, so no layout
    order can ever put the totals away from the lines they sum."""
    w = pdf.epw
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 9, "INVOICE", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, _s(inv.get("invoice_ref") or ""), new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, _s(f"Bill to: {inv.get('customer_email') or '-'}"), new_x="LMARGIN", new_y="NEXT")
    if inv.get("period_from"):
        pdf.cell(0, 6, _s(f"Period: {pretty_date(inv.get('period_from'))} to {pretty_date(inv.get('period_to'))}"),
                 new_x="LMARGIN", new_y="NEXT")
    dates = []
    if inv.get("issue_date"):
        dates.append(f"Issued: {pretty_date(inv['issue_date'])}")
    if inv.get("due_date"):
        dates.append(f"Due: {pretty_date(inv['due_date'])}")
    if dates:
        pdf.cell(0, 6, _s("   ".join(dates)), new_x="LMARGIN", new_y="NEXT")
    # THEIR number ("Your ref" from where they sit) — the PO that lets an
    # FM company's accounts desk match this invoice without a phone call.
    if inv.get("customer_reference"):
        pdf.cell(0, 6, _s(f"Your ref: {inv['customer_reference']}"),
                 new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    c_item, c_date, c_amt = w * 0.60, w * 0.20, w * 0.20
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(c_item, 7, "Item", border="B")
    pdf.cell(c_date, 7, "Date", border="B")
    pdf.cell(c_amt, 7, "Amount", border="B", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    for l in lines:
        pdf.cell(c_item, 6, _s(l.get("description"))[:64])
        pdf.cell(c_date, 6, _s(str(l.get("date") or "")))
        pdf.cell(c_amt, 6, _money(l.get("amount")), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    def _total_row(label, value, bold=False):
        pdf.set_font("Helvetica", "B" if bold else "", 9)
        pdf.cell(c_item, 6, "")
        pdf.cell(c_date, 6, _s(label), align="R")
        pdf.cell(c_amt, 6, _money(value), align="R", new_x="LMARGIN", new_y="NEXT")

    if inv.get("prices_include_vat") is True and settings.get("show_vat"):
        # One row, the label spanning the item and date columns so it fits.
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(c_item + c_date, 6, _s(f"Total (includes VAT {_money(inv.get('vat'))})"), align="R")
        pdf.cell(c_amt, 6, _money(inv.get("total")), align="R", new_x="LMARGIN", new_y="NEXT")
    else:
        _total_row("Subtotal", inv.get("subtotal"))
        if settings.get("show_vat"):
            _total_row(f"VAT ({float(settings.get('vat_rate') or 0):g}%)", inv.get("vat"))
        _total_row("Total", inv.get("total"), bold=True)
    pdf.ln(5)


def _sec_payment_terms(pdf, inv, lines, settings):
    if settings.get("show_payment_terms") and settings.get("payment_terms"):
        pdf.set_font("Helvetica", "", 8)
        pdf.multi_cell(0, 5, _s(settings["payment_terms"]), new_x="LMARGIN", new_y="NEXT")


def _sec_bank_details(pdf, inv, lines, settings):
    """Bank transfer details ON the document (2026-07-26) — the PDF a customer
    files should carry how to pay it, not just the covering email."""
    if not (settings.get("bank_sort_code") and settings.get("bank_account_number")):
        return
    pdf.set_font("Helvetica", "B", 8)
    pdf.cell(0, 5, "Pay by bank transfer", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 8)
    if settings.get("bank_account_name"):
        pdf.cell(0, 5, _s(f"Account name: {settings['bank_account_name']}"),
                 new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, _s(f"Sort code: {settings['bank_sort_code']}   "
                      f"Account number: {settings['bank_account_number']}"),
             new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, _s(f"Reference: {inv.get('invoice_ref') or ''}"),
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)


def _sec_footer_note(pdf, inv, lines, settings):
    if settings.get("footer_note"):
        pdf.set_font("Helvetica", "I", 8)
        pdf.multi_cell(0, 5, _s(settings["footer_note"]), new_x="LMARGIN", new_y="NEXT")


# section key → drawer, dispatched in the tenant's section_order (the same keys
# app/invoice_config.SECTIONS defines and the HTML macro renders).
_SECTIONS = {
    "business_header": _sec_business_header,
    "core": _sec_core,
    "payment_terms": _sec_payment_terms,
    "bank_details": _sec_bank_details,
    "footer_note": _sec_footer_note,
}


def render(inv: dict, lines: list[dict], settings: dict) -> bytes:
    """The invoice as a PDF (A4). `settings` is an app.invoice_config.settings_for
    dict; sections draw in settings['section_order'] (normalised, always complete —
    a settings dict built by hand without it falls back to the default order)."""
    from app.invoice_config import normalize_section_order
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(18, 18, 18)
    pdf.add_page()
    for key in normalize_section_order(settings.get("section_order")):
        _SECTIONS[key](pdf, inv, lines, settings)
    return bytes(pdf.output())
