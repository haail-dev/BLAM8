"""Quote PDF generation (docs/modules/module-quotes.md) — invoice_pdf.py's sibling.

Pure: a quote row + the lines to show + the shared invoice settings in, PDF
bytes out. The /q page is the quote; this is the courtesy copy for filing —
so it renders what the page shows: selected lines only (the caller decides,
passing the acceptance snapshot's lines once accepted), lump-sum mode hides
per-line pricing, and an accepted quote carries the acceptance record.
"""
from fpdf import FPDF

from app.dates import pretty_date
from app.invoice_pdf import _money, _s, _sec_business_header, _sec_footer_note


def _sec_core(pdf, quote, lines, settings):
    w = pdf.epw
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 9, "QUOTE", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, _s(quote.get("quote_ref") or ""), new_x="LMARGIN", new_y="NEXT")
    who = quote.get("customer_name") or quote.get("customer_email") or "-"
    pdf.cell(0, 6, _s(f"Prepared for: {who}"), new_x="LMARGIN", new_y="NEXT")
    if quote.get("title"):
        pdf.cell(0, 6, _s(quote["title"]), new_x="LMARGIN", new_y="NEXT")
    dates = []
    if quote.get("issue_date"):
        dates.append(f"Issued: {pretty_date(quote['issue_date'])}")
    if quote.get("valid_until"):
        dates.append(f"Valid until: {pretty_date(quote['valid_until'])}")
    if dates:
        pdf.cell(0, 6, _s("   ".join(dates)), new_x="LMARGIN", new_y="NEXT")
    if quote.get("customer_reference"):
        pdf.cell(0, 6, _s(f"Your ref: {quote['customer_reference']}"),
                 new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    show_prices = quote.get("show_item_prices", True)
    if show_prices:
        c_item, c_qty, c_amt = w * 0.60, w * 0.20, w * 0.20
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(c_item, 7, "Item", border="B")
        pdf.cell(c_qty, 7, "Qty", border="B", align="R")
        pdf.cell(c_amt, 7, "Amount", border="B", align="R",
                 new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        for l in lines:
            pdf.cell(c_item, 6, _s(l.get("description"))[:64])
            pdf.cell(c_qty, 6, _s(f"{float(l.get('quantity') or 1):g}"), align="R")
            pdf.cell(c_amt, 6, _money(l.get("amount")), align="R",
                     new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(w, 7, "Work included", border="B", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        for l in lines:
            pdf.cell(w, 6, _s(l.get("description"))[:90],
                     new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    c_left, c_label, c_amt = w * 0.60, w * 0.20, w * 0.20

    def _total_row(label, value, bold=False):
        pdf.set_font("Helvetica", "B" if bold else "", 9)
        pdf.cell(c_left, 6, "")
        pdf.cell(c_label, 6, _s(label), align="R")
        pdf.cell(c_amt, 6, _money(value), align="R", new_x="LMARGIN", new_y="NEXT")

    if show_prices and quote.get("prices_include_vat") is True and settings.get("show_vat"):
        # One row, the label spanning the left and label columns so it fits.
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(c_left + c_label, 6, _s(f"Total (includes VAT {_money(quote.get('vat'))})"), align="R")
        pdf.cell(c_amt, 6, _money(quote.get("total")), align="R", new_x="LMARGIN", new_y="NEXT")
    else:
        if show_prices:
            _total_row("Subtotal", quote.get("subtotal"))
            if settings.get("show_vat"):
                _total_row(f"VAT ({float(settings.get('vat_rate') or 0):g}%)",
                           quote.get("vat"))
        _total_row("Total", quote.get("total"), bold=True)
    pdf.ln(3)

    dep = quote.get("deposit_percent")
    if dep:
        pdf.set_font("Helvetica", "", 8)
        part = quote.get("deposit_base")
        of = f" of {_money(part)}" if part is not None else ""
        pdf.cell(0, 5, _s(f"A deposit of {float(dep):g}%{of} is payable on acceptance."),
                 new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)


def _sec_acceptance(pdf, quote, lines, settings):
    """The acceptance record — only on an accepted quote. This is the audit
    trail the trades forums say everyone skips: who agreed, to what, when."""
    if not quote.get("accepted_at"):
        return
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(0, 6, "Accepted", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 8)
    pdf.cell(0, 5, _s(f"Signed (typed name): {quote.get('accepted_name') or '-'}"),
             new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, _s(f"Accepted at: {str(quote.get('accepted_at') or '')[:16].replace('T', ' ')} UTC"
                      f"   Version: {quote.get('version') or 1}"),
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)


def _sec_notes(pdf, quote, lines, settings):
    if quote.get("notes"):
        pdf.set_font("Helvetica", "", 8)
        pdf.multi_cell(0, 5, _s(quote["notes"]))
        pdf.ln(1)


def render(quote: dict, lines: list[dict], settings: dict) -> bytes:
    """The quote as a PDF (A4). Fixed section order — quotes don't take part in
    the invoice's drag-to-reorder (one document, one shape)."""
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(18, 18, 18)
    pdf.add_page()
    _sec_business_header(pdf, quote, lines, settings)
    _sec_core(pdf, quote, lines, settings)
    _sec_acceptance(pdf, quote, lines, settings)
    _sec_notes(pdf, quote, lines, settings)
    _sec_footer_note(pdf, quote, lines, settings)
    return bytes(pdf.output())
