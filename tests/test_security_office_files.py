"""Security review 2026-09, M31/M32: Office files are zip archives — check the
zip's own directory before a parser opens it, and never walk a sheet's
declared size unbounded."""
import io
import zipfile

import pytest
from openpyxl import Workbook

from app import knowledge, safe_parse, spreadsheets


def _zip(parts: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in parts.items():
            z.writestr(name, body)
    return buf.getvalue()


def _workbook(rows) -> bytes:
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_zip_bomb_docx_refused_before_python_docx():
    bomb = _zip({"[Content_Types].xml": b"<x/>", "word/document.xml": b"\0" * (50 * 1024 * 1024)})
    assert len(bomb) < 200_000                      # tiny on the wire
    assert safe_parse.office_zip_ok(bomb) is False
    with pytest.raises(ValueError):
        knowledge.extract_text("prices.docx", bomb)


def test_zip_bomb_xlsx_refused_on_both_paths():
    bomb = _zip({"xl/worksheets/sheet1.xml": b" " * (50 * 1024 * 1024)})
    with pytest.raises(ValueError):
        knowledge.extract_text("prices.xlsx", bomb)
    rows, fatal = spreadsheets.parse_order_book("orders.xlsx", bomb)
    assert rows == [] and fatal


def test_not_a_zip_is_refused_cleanly():
    assert safe_parse.office_zip_ok(b"not a zip at all") is False
    rows, fatal = spreadsheets.parse_order_book("orders.xlsx", b"nope")
    assert rows == [] and fatal


def test_far_cell_does_not_expand_the_sheet():
    # A value at row 50,000 / column 300: openpyxl would otherwise yield every
    # empty cell up to it. The reader stops at the clamp.
    wb = Workbook()
    ws = wb.active
    ws["A1"] = "Price list"
    ws.cell(row=50_000, column=300, value="far")
    buf = io.BytesIO()
    wb.save(buf)
    text = knowledge.extract_text("prices.xlsx", buf.getvalue())
    assert "Price list" in text and "far" not in text


def test_ordinary_files_still_read():
    data = _workbook([["Item", "Price"], ["Call-out", 85], ["Hour", 60]])
    assert safe_parse.office_zip_ok(data) is True
    text = knowledge.extract_text("prices.xlsx", data)
    assert "Call-out, 85" in text and "Hour, 60" in text


def test_ordinary_docx_still_reads():
    from docx import Document
    d = Document()
    d.add_paragraph("We cover the whole county.")
    buf = io.BytesIO()
    d.save(buf)
    assert "whole county" in knowledge.extract_text("about.docx", buf.getvalue())
