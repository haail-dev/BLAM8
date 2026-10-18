"""Events OS — the run-of-show as a PDF (docs/os/events.md §8, §14).

"PDF reuses the invoicing PDF path": this REUSES src/app/invoice_pdf.py's fpdf2 generator
wholesale — same fpdf2 (pure-python, no system libs, runs on Render unchanged), same
latin-1-safe `_s` helper (assets_pdf does the identical thing for the evidence bundle).

The document is the DETERMINISTICALLY-assembled sheet (events_os.assemble_run_of_show):
the ordered timing lines with their responsible party, staff call-times, the dietary
summary for the supplier, contingency notes, and — LOUDLY (§14) — the flagged GAPS. It
renders ONLY what the assembler produced; it never fabricates a timing, name or number
(a gap prints as a gap). A sheet with no timings prints an empty schedule + its gaps, not
phantom rows.
"""
from fpdf import FPDF

from app.invoice_pdf import _s


def render(event: dict, deal: dict, sheet: dict) -> bytes:
    """The run-of-show as a PDF (A4). `sheet` is an events_os.assemble_run_of_show dict
    ({lines, staff_call_times, dietary_summary, contingency_notes, gaps, ...})."""
    sheet = sheet or {}
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(18, 18, 18)
    pdf.add_page()
    w = pdf.epw

    # ── header ────────────────────────────────────────────────────────────────
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 9, "RUN SHEET", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 6, _s(deal.get("title") or "Event"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    meta = []
    if event.get("event_date"):
        meta.append(f"Date: {event.get('event_date')}")
    gc = event.get("guest_count")
    if gc is not None:
        meta.append(f"Headcount: {gc}" + ("" if event.get("guest_count_final") else " (not final)"))
    if event.get("event_type"):
        meta.append(f"Type: {event.get('event_type')}")
    if meta:
        pdf.cell(0, 5, _s("   ".join(meta)), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    # ── the schedule (ordered timing lines + responsible party) ───────────────
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, "Schedule", new_x="LMARGIN", new_y="NEXT")
    lines = sheet.get("lines") or []
    if not lines:
        pdf.set_font("Helvetica", "I", 9)
        pdf.cell(0, 5, "No timings pinned yet.", new_x="LMARGIN", new_y="NEXT")
    else:
        c_time, c_label, c_who = w * 0.16, w * 0.44, w * 0.40
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(c_time, 7, "Time", border="B")
        pdf.cell(c_label, 7, "Item", border="B")
        pdf.cell(c_who, 7, "Responsible", border="B", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        for ln in lines:
            resp = ln.get("responsible") or {}
            who = str(resp.get("role") or "").replace("_", " ")
            if resp.get("name"):
                who = f"{who} — {resp['name']}" if who else str(resp["name"])
            who = who or "-- unassigned --"
            pdf.cell(c_time, 6, _s(str(ln.get("time") or "")))
            pdf.cell(c_label, 6, _s(str(ln.get("label") or ""))[:44])
            pdf.cell(c_who, 6, _s(who)[:40], new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    # ── staff call-times ──────────────────────────────────────────────────────
    staff = sheet.get("staff_call_times") or []
    if staff:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 6, "Staff call-times", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        for s in staff:
            who = str(s.get("role") or "staff").replace("_", " ")
            if s.get("name"):
                who = f"{who} ({s['name']})"
            tail = f" — {s['outstanding']}" if s.get("outstanding") else ""
            conf = s.get("confirmation")
            conf_s = f" [{conf}]" if conf else ""
            pdf.multi_cell(0, 5, _s(f"- {who}{conf_s}{tail}"),
                           new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

    # ── dietary summary (the supplier line) ────────────────────────────────────
    diet = sheet.get("dietary_summary") or {}
    if diet:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 6, "Dietary summary", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        pdf.multi_cell(0, 5, _s(", ".join(f"{n} {str(tag).replace('_', ' ')}"
                                          for tag, n in diet.items())),
                       new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

    # ── contingency notes ─────────────────────────────────────────────────────
    if sheet.get("contingency_notes"):
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 6, "Contingency", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        pdf.multi_cell(0, 5, _s(sheet["contingency_notes"]),
                       new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

    # ── GAPS — flagged loudly (§14). A gap prints as a gap; never fabricated away. ─
    gaps = sheet.get("gaps") or []
    pdf.ln(1)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, _s(f"Gaps flagged ({len(gaps)})"), new_x="LMARGIN", new_y="NEXT")
    if not gaps:
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(0, 5, "No gaps - the sheet is complete.", new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.set_font("Helvetica", "", 9)
        for g in gaps:
            # "!" prefix (latin-1 safe) — the warning marker; the on-screen sheet uses the
            # louder warning glyph, but the PDF core fonts stay latin-1.
            pdf.multi_cell(0, 5, _s(f"!  {g.get('detail') or g.get('kind')}"),
                           new_x="LMARGIN", new_y="NEXT")

    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 8)
    pdf.multi_cell(0, 5, _s("Assembled by BLAM8 from the stated facts. Gaps are shown as "
                            "gaps - no timing, name or number here is invented. Confirm "
                            "every flagged item before the day."),
                   new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())
