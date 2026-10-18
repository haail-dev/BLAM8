"""File-based spreadsheet I/O for the routing module (route-planning D4).

csv/xlsx ONLY — deliberately no Google Sheets API: files need no OAuth scopes
(the Google app is still in verification), and Sheets users export natively
with File → Download. Excel-only shops are covered by the same code. Two jobs:

  manifests OUT — a resource-day manifest as csv or xlsx for drivers, customers
  and the office (built from scheduling.vehicle_manifest()'s view-model).

  order books IN — parse + normalise an uploaded csv/xlsx into booking rows
  for the upload → preview → commit flow. The preview is ADVISORY ("here's
  what I understood, row by row"); the commit books every non-error row
  through the atomic best-fit RPC, so a bulk import can NEVER overload a van
  whatever the preview said.

Parsing is forgiving where real spreadsheets actually go wrong — header
aliases, UTF-8 BOMs, cp1252 Excel exports, semicolon delimiters, UK
dd/mm/yyyy dates, "1,800 kg" numbers-as-text — and strict where it matters:
a row with no usable date or quantity becomes an ERROR row that is shown,
never silently dropped (no silent caps, no silent skips).
"""
import csv
import io
import re
from datetime import date, datetime

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font

from app import scheduling

# ── Manifests OUT ─────────────────────────────────────────────────────────────
# "Booking" = the business-issued reference (BK-1042) quoted to the customer on
# confirmation; "Customer ref" = theirs (a PO number), captured from the email.
MANIFEST_COLUMNS = ["Booking", "Customer ref", "Job", "Contact", "Collection",
                    "Delivery", "Spaces", "Weight kg", "Status"]


def _manifest_rows(m: dict) -> list[list]:
    return [[r.get("booking_ref") or "", r["reference"] or "", r["summary"],
             r["contact"] or "",
             r["collection_postcode"] or "", r["delivery_postcode"] or "",
             r["spaces"], r["weight_kg"] if r["weight_kg"] is not None else "",
             r["status"]]
            for r in m["rows"]]


def manifest_filename(m: dict, ext: str) -> str:
    veh = m["vehicle"]
    tag = veh.get("registration") or veh.get("name") or "vehicle"
    safe = re.sub(r"[^A-Za-z0-9]+", "-", str(tag)).strip("-").lower() or "vehicle"
    return f"manifest-{safe}-{m['date']}.{ext}"


def manifest_csv(m: dict) -> str:
    """Pure table — no title rows, so the file machine-reads cleanly (and
    round-trips through the importer's own header matching)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(MANIFEST_COLUMNS)
    w.writerows(_manifest_rows(m))
    return buf.getvalue()


def manifest_xlsx(m: dict) -> bytes:
    veh = m["vehicle"]
    title = veh["name"] + (f" — {veh['registration']}" if veh.get("registration") else "")
    where = m["label"] + (f" · {m['branch']['name']}" if m.get("branch") else "")
    wb = Workbook()
    ws = wb.active
    ws.title = "Manifest"
    ws.append([f"Manifest — {title}"])
    ws.append([where])
    ws.append([])
    ws.append(MANIFEST_COLUMNS)
    for cell in ws[1] + ws[4]:
        cell.font = Font(bold=True)
    for row in _manifest_rows(m):
        ws.append(row)
    ws.append(["", "", "Total", "", "", "", m["used_spaces"], m["used_kg"],
               f"{m['drops']} booking{'' if m['drops'] == 1 else 's'}"])
    ws[ws.max_row][2].font = Font(bold=True)
    widths = [11, 14, 38, 26, 12, 12, 9, 11, 11]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=4, column=i).column_letter].width = w
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── Order books IN — parsing ─────────────────────────────────────────────────
# Header aliases: matched case-insensitively after punctuation is stripped, so
# "Delivery Date", "delivery_date" and "Delivery date:" all land on 'date'.
# Kept deliberately small — every alias here is one less reason an upload of a
# real transport operator's existing order book needs reformatting first.
HEADER_ALIASES = {
    "date": "date", "delivery date": "date", "booking date": "date",
    "collection date": "date", "day": "date",
    "pallets": "qty", "qty": "qty", "quantity": "qty", "no of pallets": "qty",
    "number of pallets": "qty", "pallet count": "qty",
    "type": "pallet_type", "pallet type": "pallet_type", "pallet": "pallet_type",
    "weight": "weight_kg", "weight kg": "weight_kg", "kg": "weight_kg",
    "ref": "reference", "reference": "reference", "po": "reference",
    "po number": "reference", "order ref": "reference", "order no": "reference",
    "our ref": "reference", "your ref": "reference",
    "collection": "collection_postcode", "collection postcode": "collection_postcode",
    "from": "collection_postcode", "origin": "collection_postcode",
    "delivery": "delivery_postcode", "delivery postcode": "delivery_postcode",
    "to": "delivery_postcode", "destination": "delivery_postcode",
    "customer": "contact", "contact": "contact", "email": "contact",
    "customer email": "contact",
    "summary": "summary", "description": "summary", "notes": "summary",
    "job": "summary", "goods": "summary",
    "branch": "branch", "depot": "branch",
}

FIELDS = ("date", "qty", "pallet_type", "weight_kg", "reference",
          "collection_postcode", "delivery_postcode", "contact", "summary", "branch")


def _norm_header(value) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", str(value or "").lower())).strip()


def _parse_date(value):
    """ISO and xlsx date cells first; then UK day-first dd/mm/yy(yy). American
    mm/dd would mis-read here — the preview shows the parsed date, which is
    exactly what the preview step is for."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value or "").strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
    m = re.match(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})$", s)
    if m:
        d, mth, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        try:
            return date(y, mth, d)
        except ValueError:
            return None
    return None


def _parse_number(value):
    """Floats from cells or text: '6', ' 6.5 ', '1,800', '1800 kg' all work."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "")
    m = re.match(r"^-?\d+(\.\d+)?", s)
    return float(m.group(0)) if m else None


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value == int(value):
        return str(int(value))           # 1042.0 → '1042' (Excel's favourite trick)
    return str(value).strip()


def _csv_table(data: bytes):
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return None, "Couldn't read that file as text — export it as CSV (UTF-8) and try again."
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return list(csv.reader(io.StringIO(text), dialect)), None


def _xlsx_table(data: bytes):
    from app import safe_parse
    bad = "Couldn't open that file as a spreadsheet — save it as .xlsx or .csv and try again."
    if not safe_parse.office_zip_ok(data):   # M31: zip bomb / not a workbook
        return None, bad
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        return None, bad
    ws = wb.active   # first tab; one order book per tab, like a CSV
    # Clamped (M31): the import keeps 500 rows; a header search reads the first 10.
    return [list(row) for row in safe_parse.xlsx_rows(ws, max_rows=1000, max_cols=60)], None


def parse_order_book(filename: str, data: bytes):
    """(rows, fatal). fatal is a whole-file problem; rows are normalised dicts
    — {line, date, qty, pallet_type, weight_kg, reference, collection_postcode,
    delivery_postcode, contact, summary, branch, problems[]} — one per
    non-blank data row, errors attached rather than dropped."""
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        table, fatal = _xlsx_table(data)
    elif name.endswith(".csv") or "." not in name.rsplit("/", 1)[-1]:
        table, fatal = _csv_table(data)
    else:
        return [], "Unsupported file type — upload a .csv or .xlsx export."
    if fatal:
        return [], fatal
    table = table or []

    # The header row: among the first 10 rows, the one mapping the most known
    # columns — and it must give us 'date' and 'qty' or the file is unusable.
    best_idx, best_map = None, {}
    for i, raw in enumerate(table[:10]):
        mapped = {}
        for col, cell in enumerate(raw or []):
            field = HEADER_ALIASES.get(_norm_header(cell))
            if field and field not in mapped.values():
                mapped[col] = field
        if len(mapped) > len(best_map):
            best_idx, best_map = i, mapped
    if best_idx is None or not {"date", "qty"} <= set(best_map.values()):
        return [], ("Couldn't find a header row — the sheet needs at least a "
                    "date column and a pallet-quantity column (e.g. 'Date' and 'Pallets').")

    rows = []
    for line_no, raw in enumerate(table[best_idx + 1:], start=best_idx + 2):
        cells = list(raw or [])
        if not any(_text(c) for c in cells):
            continue   # fully blank — the one thing we do skip
        rec = {f: "" for f in FIELDS}
        for col, field in best_map.items():
            rec[field] = cells[col] if col < len(cells) else ""
        problems = []
        parsed_date = _parse_date(rec["date"])
        if parsed_date is None:
            problems.append("no usable date")
        qty = _parse_number(rec["qty"])
        if qty is None or qty <= 0:
            problems.append("no pallet quantity")
        weight = _parse_number(rec["weight_kg"])
        rows.append({
            "line": line_no,
            "date": parsed_date.isoformat() if parsed_date else _text(rec["date"]),
            "qty": qty,
            "pallet_type": _text(rec["pallet_type"]).lower(),
            "weight_kg": weight if (weight and weight > 0) else None,
            "reference": _text(rec["reference"])[:80],
            "collection_postcode": _text(rec["collection_postcode"]).upper()[:16],
            "delivery_postcode": _text(rec["delivery_postcode"]).upper()[:16],
            "contact": _text(rec["contact"])[:120],
            "summary": _text(rec["summary"])[:200],
            "branch": _text(rec["branch"]),
            "problems": problems,
        })
    if not rows:
        return [], "No data rows under the header — the sheet looks empty."
    return rows, None


# ── Order books IN — the preview verdicts ────────────────────────────────────
def preview_order_rows(rows: list[dict], factors: dict, branches: list[dict],
                       vehicles: list[dict], booked: list[dict], now: datetime) -> list[dict]:
    """Per-row bookability for the preview page. ADVISORY — the commit
    re-decides through the atomic RPC — but honest: rows are simulated
    CUMULATIVELY (row 3 sees rows 1–2's load on the same day), so a file that
    overfills a day shows its first no-fit row in the preview, not at commit.
    Statuses: error (won't be committed) · no_fit (committed — the RPC is the
    judge; flagged so nobody is surprised) · ok."""
    today = now.astimezone(scheduling.DIARY_TZ).date()
    by_name = {(b.get("name") or "").strip().lower(): b["id"] for b in branches}
    ref_counts: dict = {}
    for r in rows:
        if r["reference"]:
            ref_counts[r["reference"]] = ref_counts.get(r["reference"], 0) + 1

    virtual = list(booked)   # the board + this file's own accepted rows so far
    out = []
    for r in rows:
        problems = list(r["problems"])
        warnings = []
        spaces = None
        if r["qty"]:
            try:
                spaces, assumed = scheduling.order_spaces(
                    factors, [{"type": r["pallet_type"] or "full", "qty": r["qty"]}])
                if assumed:
                    warnings.append(f"type '{assumed[0]}' counted as a full pallet")
            except (TypeError, ValueError, KeyError):
                problems.append("pallet quantity looks wrong")
        branch_id = None
        if r["branch"]:
            branch_id = by_name.get(r["branch"].strip().lower())
            if branch_id is None:
                problems.append(f"unknown branch '{r['branch']}'")
        try:
            day = date.fromisoformat(r["date"])
        except (TypeError, ValueError):
            day = None
        if day and day < today:
            problems.append("date is in the past")
        if r["reference"] and ref_counts[r["reference"]] > 1:
            warnings.append("duplicate reference in this file")

        fits = None
        if not problems and spaces and day:
            pool = [v for v in vehicles
                    if branch_id is None or v.get("branch_id") == branch_id]
            if not pool:
                problems.append("no vehicles for that branch")
            else:
                loads = scheduling._day_loads(pool, virtual, day)
                pick = scheduling.best_fit(loads, spaces, r["weight_kg"])
                fits = pick is not None
                if pick:
                    virtual.append({"booking_date": day.isoformat(),
                                    "vehicle_id": pick["vehicle"]["id"],
                                    "spaces": spaces, "weight_kg": r["weight_kg"],
                                    "status": "confirmed"})
        status = "error" if problems else ("no_fit" if fits is False else "ok")
        out.append({**r, "spaces": spaces, "branch_id": branch_id,
                    "problems": problems, "warnings": warnings, "status": status})
    return out
