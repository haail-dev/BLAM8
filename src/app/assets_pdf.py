"""Assets OS — the damage-case evidence bundle as a PDF (docs/os/assets.md §12b).

The "yours to export" promise: the /app/assets/damage/{id} evidence page rendered to a
portable PDF a holiday-let owner can send Airbnb's resolution centre or a disputing hirer.
JSON export already ships (Phase C); this is the PDF twin.

REUSES src/app/invoice_pdf.py's generator wholesale — same fpdf2 (pure-python, no system libs,
runs on Render unchanged), same latin-1-safe _s / _money helpers. The bundle is: the
side-by-side handover (out) vs return (in) photos, the per-finding annotations, timestamps,
captured_by, the model's reasoning (the case description) and the deposit trail.

GRACEFUL (§12b): photos are downloaded from Storage best-effort — a bucket outage or a
missing path simply omits that image (the finding text still stands), never crashes the
export. A case with no photos at all still produces a complete text evidence PDF.
"""
from __future__ import annotations

import io
import logging

from fpdf import FPDF

from app import storage
from app.invoice_pdf import _money, _s

log = logging.getLogger(__name__)


def _image(pdf: FPDF, path: str | None, w: float, h: float, *, client=None) -> bool:
    """Best-effort: download a Storage photo and place it at the cursor. Returns True on
    success. Any Storage failure (bucket down, missing path) → False, and the caller draws
    a 'photo unavailable' placeholder — the export never fails on a photo."""
    if not path:
        return False
    try:
        data = storage.download_bytes(path, client=client)
        pdf.image(io.BytesIO(data), w=w, h=h)
        return True
    except Exception:  # noqa: BLE001 — storage error / bad bytes / decode
        log.warning("evidence PDF: could not embed photo %s", path, exc_info=True)
        return False


def render(bundle: dict, *, client=None) -> bytes:
    """The evidence bundle (assets.evidence_bundle output) as a PDF (A4). Reuses the
    invoice_pdf fpdf2 spine — same page setup, same latin-1-safe text."""
    case = bundle.get("case") or {}
    unit = bundle.get("unit") or {}
    booking = bundle.get("booking") or {}
    deposit = bundle.get("deposit") or {}
    findings = bundle.get("findings") or []
    out_report = bundle.get("out_report") or {}
    in_report = bundle.get("in_report") or {}

    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(18, 18, 18)
    pdf.add_page()
    w = pdf.epw

    # ── header ────────────────────────────────────────────────────────────────
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 9, "DAMAGE EVIDENCE BUNDLE", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, _s(f"Unit: {unit.get('name') or '-'}"), new_x="LMARGIN", new_y="NEXT")
    if booking.get("contact"):
        pdf.cell(0, 6, _s(f"Hirer: {booking.get('contact')}"), new_x="LMARGIN", new_y="NEXT")
    if booking.get("booking_ref"):
        pdf.cell(0, 6, _s(f"Booking ref: {booking.get('booking_ref')}"),
                 new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, _s(f"Severity: {case.get('severity') or '-'}   "
                      f"Status: {case.get('status') or '-'}"),
             new_x="LMARGIN", new_y="NEXT")
    if not bundle.get("has_baseline"):
        pdf.set_font("Helvetica", "I", 9)
        pdf.multi_cell(0, 5, _s("No handover baseline was captured for this hire — a deposit "
                                "deduction cannot be substantiated (no baseline, no claim)."))
    pdf.ln(3)

    # ── capture provenance ──────────────────────────────────────────────────
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 6, "Capture record", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    for label, rep in (("Handover (out)", out_report), ("Return (in)", in_report)):
        who = rep.get("captured_by") or "-"
        when = str(rep.get("captured_at") or "-")[:19]
        pdf.cell(0, 5, _s(f"{label}: captured by {who} at {when}"),
                 new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    # ── the model's reasoning (the case description) ──────────────────────────
    if case.get("description"):
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 6, "Assessment", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        conf = case.get("diff_confidence")
        if conf is not None:
            pdf.cell(0, 5, _s(f"Overall confidence: {float(conf):.0%}"),
                     new_x="LMARGIN", new_y="NEXT")
        pdf.multi_cell(0, 5, _s(case.get("description")))
        pdf.ln(3)

    # ── findings, each with its side-by-side out/in photos ────────────────────
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 6, _s(f"Findings ({len(findings)})"), new_x="LMARGIN", new_y="NEXT")
    if not findings:
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(0, 5, "No individual findings recorded.", new_x="LMARGIN", new_y="NEXT")
    img_w, img_h = (w - 8) / 2.0, 45.0
    for i, f in enumerate(findings, 1):
        pdf.ln(2)
        pdf.set_font("Helvetica", "B", 9)
        sev = f.get("severity") or "-"
        conf = f.get("confidence")
        conf_s = f" ({float(conf):.0%})" if conf is not None else ""
        pdf.multi_cell(0, 5, _s(f"{i}. {f.get('area') or 'item'} — {f.get('change') or ''} "
                                f"[{sev}{conf_s}]"))
        # Side-by-side BEFORE | AFTER. Downloaded best-effort; a placeholder on failure.
        x0, y0 = pdf.get_x(), pdf.get_y()
        pdf.set_font("Helvetica", "", 7)
        pdf.cell(img_w, 4, "BEFORE (handover)")
        pdf.cell(8, 4, "")
        pdf.cell(img_w, 4, "AFTER (return)", new_x="LMARGIN", new_y="NEXT")
        row_y = pdf.get_y()
        if not _image(pdf, f.get("out_url"), img_w, img_h, client=client):
            pdf.set_xy(x0, row_y)
            pdf.set_font("Helvetica", "I", 8)
            pdf.cell(img_w, img_h, "photo unavailable", border=1, align="C")
        pdf.set_xy(x0 + img_w + 8, row_y)
        if not _image(pdf, f.get("in_url"), img_w, img_h, client=client):
            pdf.set_xy(x0 + img_w + 8, row_y)
            pdf.set_font("Helvetica", "I", 8)
            pdf.cell(img_w, img_h, "photo unavailable", border=1, align="C")
        pdf.set_y(row_y + img_h + 2)

    # ── deposit trail ─────────────────────────────────────────────────────────
    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 6, "Deposit trail", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    if deposit:
        pdf.cell(0, 5, _s(f"Deposit: {_money(deposit.get('amount'))}   "
                          f"Status: {deposit.get('status') or '-'}"),
                 new_x="LMARGIN", new_y="NEXT")
        if case.get("proposed_deduction") is not None:
            pdf.cell(0, 5, _s(f"Suggested deduction: {_money(case.get('proposed_deduction'))} "
                              "(a proposal — the owner's decision, never automatic)"),
                     new_x="LMARGIN", new_y="NEXT")
        if deposit.get("deducted_amount"):
            pdf.cell(0, 5, _s(f"Deducted: {_money(deposit.get('deducted_amount'))}"),
                     new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.cell(0, 5, "No deposit recorded on this hire.", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 8)
    pdf.multi_cell(0, 5, _s("This bundle is an evidence assistant's report, not an "
                            "adjudication. Every deposit deduction is the owner's decision."))

    return bytes(pdf.output())
