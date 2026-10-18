"""Spreadsheet slice (route-planning D4) — manifests out, order books in.

The contracts under test: parsing is forgiving exactly where real exports go
wrong (aliases, BOM, cp1252, semicolons, UK dates, numbers-as-text) and never
silently drops a row; the preview simulates the file CUMULATIVELY so same-day
overfill shows up before commit; the commit books confirmed/source=import
through the atomic RPC and stamps routing fields; every surface 404s unless
BOTH fleet and routing are active.

Run: python -m pytest tests/ -q
"""
import io
import json
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

import app.dashboard as dashboard
from app import spreadsheets
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.scheduling import DIARY_TZ
from app.security import require_csrf
from app.spreadsheets import (manifest_csv, manifest_filename, manifest_xlsx,
                              parse_order_book, preview_order_rows)

NOW = datetime(2026, 6, 16, 10, 0, tzinfo=DIARY_TZ)  # Tuesday

VAN = {"id": "v-aaa", "name": "7.5t box", "registration": "YX24 ABC",
       "capacity_spaces": 6, "max_payload_kg": 2500, "active": True,
       "branch_id": "br-leeds"}
BRANCHES = [{"id": "br-leeds", "name": "Leeds", "postcode": "LS9 8AS"}]
FACTORS = {"full": 1.0, "euro": 1.0, "half": 0.5, "quarter": 0.25}
RAW_BOOKING = {"id": "b-1", "vehicle_id": "v-aaa", "booking_date": "2026-06-18",
               "status": "confirmed", "summary": "6 pallets to Leeds",
               "contact": "ops@x.co", "spaces": 6, "weight_kg": 1800,
               "reference": "PO-1042", "collection_postcode": "WF17 9LU",
               "delivery_postcode": "LS9 8AS",
               "created_at": "2026-06-16T09:00:00+00:00", "source": "email"}


# ── parsing: csv ──────────────────────────────────────────────────────────────
CSV_BOOK = (
    "﻿Delivery Date,Qty,Type,Weight (kg),Ref,From,To,Customer,Notes,Depot\r\n"
    '13/06/2026,6,full,"1,800 kg",PO-1042,WF17 9LU,ls9 8as,ops@acme.co,Birstall job,Leeds\r\n'
    "2026-06-19,2,half,,,,,,,\r\n"
    ",,,,,,,,,\r\n"                       # fully blank — the one thing skipped
    "not-a-date,oops,,,,,,,,\r\n"         # junk — kept, with problems attached
).encode("utf-8")


def test_csv_parse_aliases_dates_and_numbers():
    rows, fatal = parse_order_book("orders.csv", CSV_BOOK)
    assert fatal is None and len(rows) == 3
    r = rows[0]
    assert r["date"] == "2026-06-13"          # UK day-first
    assert r["qty"] == 6.0
    assert r["weight_kg"] == 1800.0           # '1,800 kg' → 1800
    assert r["reference"] == "PO-1042"
    assert r["collection_postcode"] == "WF17 9LU"
    assert r["delivery_postcode"] == "LS9 8AS"   # uppercased
    assert r["contact"] == "ops@acme.co" and r["branch"] == "Leeds"
    assert r["problems"] == []
    assert rows[1]["date"] == "2026-06-19" and rows[1]["pallet_type"] == "half"
    assert rows[2]["problems"] == ["no usable date", "no pallet quantity"]


def test_csv_semicolon_and_cp1252():
    raw = "Date;Pallets\n18/06/2026;3 caf\xe9\n".encode("cp1252")
    rows, fatal = parse_order_book("export.csv", raw)
    assert fatal is None and rows[0]["date"] == "2026-06-18" and rows[0]["qty"] == 3.0


def test_csv_title_row_above_header_is_survived():
    raw = ("Acme Haulage weekly orders,,\r\n"
           "Date,Pallets,Ref\r\n"
           "2026-06-18,4,A1\r\n").encode("utf-8")
    rows, fatal = parse_order_book("orders.csv", raw)
    assert fatal is None and rows[0]["qty"] == 4.0 and rows[0]["reference"] == "A1"


def test_unusable_files_fail_loudly():
    assert parse_order_book("orders.csv", b"hello\nworld\n")[1] is not None   # no header
    assert parse_order_book("orders.pdf", b"x")[1] is not None                # bad type
    assert parse_order_book("orders.xlsx", b"not a zip")[1] is not None       # bad xlsx
    only_header = "Date,Pallets\r\n".encode("utf-8")
    assert parse_order_book("orders.csv", only_header)[1] is not None         # no rows


# ── parsing: xlsx ─────────────────────────────────────────────────────────────
def _xlsx_bytes(rows) -> bytes:
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_xlsx_parse_typed_cells():
    data = _xlsx_bytes([
        ["Date", "Pallets", "Weight kg", "Reference"],
        [datetime(2026, 6, 18, 0, 0), 6, 1800, 1042],     # date cell + numeric ref
    ])
    rows, fatal = parse_order_book("orders.xlsx", data)
    assert fatal is None
    assert rows[0]["date"] == "2026-06-18" and rows[0]["qty"] == 6.0
    assert rows[0]["reference"] == "1042"                 # 1042.0 → '1042'


# ── preview verdicts ──────────────────────────────────────────────────────────
def _row(**kw):
    base = {"line": 2, "date": "2026-06-18", "qty": 2.0, "pallet_type": "",
            "weight_kg": None, "reference": "", "collection_postcode": "",
            "delivery_postcode": "", "contact": "", "summary": "", "branch": "",
            "problems": []}
    return {**base, **kw}


def test_preview_cumulative_same_day_fit():
    rows = [_row(qty=4.0), _row(line=3, qty=2.0), _row(line=4, qty=1.0)]
    out = preview_order_rows(rows, FACTORS, BRANCHES, [VAN], [], NOW)
    assert [r["status"] for r in out] == ["ok", "ok", "no_fit"]   # 4+2 fills the 6


def test_preview_flags_branch_past_date_dup_ref_and_assumed_type():
    rows = [
        _row(branch="Manchester"),
        _row(line=3, date="2026-06-10"),
        _row(line=4, reference="PO-1"), _row(line=5, reference="PO-1"),
        _row(line=6, pallet_type="chep"),
    ]
    out = preview_order_rows(rows, FACTORS, BRANCHES, [VAN], [], NOW)
    assert "unknown branch 'Manchester'" in out[0]["problems"]
    assert "date is in the past" in out[1]["problems"]
    assert out[2]["warnings"] == ["duplicate reference in this file"]
    assert out[3]["warnings"] == ["duplicate reference in this file"]
    assert out[4]["status"] == "ok"
    assert "counted as a full pallet" in out[4]["warnings"][0]


def test_preview_respects_existing_board_load():
    booked = [{"booking_date": "2026-06-18", "vehicle_id": "v-aaa", "spaces": 5,
               "weight_kg": None, "status": "confirmed"}]
    out = preview_order_rows([_row(qty=2.0)], FACTORS, BRANCHES, [VAN], booked, NOW)
    assert out[0]["status"] == "no_fit"


# ── manifests out ─────────────────────────────────────────────────────────────
MANIFEST = {
    "vehicle": VAN, "date": "2026-06-18", "label": "Thursday 18 June",
    "branch": BRANCHES[0],
    "rows": [{"id": "b-1", "booking_ref": "BK-1042", "summary": "6 pallets to Leeds",
              "contact": "ops@x.co",
              "reference": "PO-1042", "status": "confirmed", "spaces": 6.0,
              "weight_kg": 1800.0, "collection_postcode": "WF17 9LU",
              "delivery_postcode": "LS9 8AS", "details": {}, "source": "email"}],
    "used_spaces": 6.0, "total_spaces": 6.0, "used_kg": 1800.0, "max_kg": 2500.0,
    "drops": 1, "located": 1, "unlocated": 0,
}


def test_manifest_csv_and_filename():
    out = manifest_csv(MANIFEST)
    assert out.startswith(
        "Booking,Customer ref,Job,Contact,Collection,Delivery,Spaces,Weight kg,Status")
    assert "BK-1042" in out and "PO-1042" in out and "LS9 8AS" in out
    assert manifest_filename(MANIFEST, "csv") == "manifest-yx24-abc-2026-06-18.csv"


def test_manifest_xlsx_round_opens():
    wb = load_workbook(io.BytesIO(manifest_xlsx(MANIFEST)))
    ws = wb.active
    assert "YX24 ABC" in ws["A1"].value
    assert ws["A4"].value == "Booking" and ws["A5"].value == "BK-1042"
    assert ws["B5"].value == "PO-1042"
    assert ws.cell(row=6, column=7).value == 6.0     # totals row: used spaces


# ── routes ────────────────────────────────────────────────────────────────────
class Env:
    def __init__(self, monkeypatch, modules=("tms",)):
        self.holds = []
        self.routing_calls = []

        def hold(tenant_id, date_iso, summary, spaces, **kw):
            self.holds.append({"date": date_iso, "summary": summary,
                               "spaces": spaces, **kw})
            return {"id": f"b-{len(self.holds)}",
                    "booking_ref": f"BK-{1000 + len(self.holds)}",
                    "booking_date": date_iso,
                    "vehicle_id": "v-aaa", "spaces": spaces, "status": kw.get("status")}

        async def push(*a, **k):
            return None

        monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set(modules))
        monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
        monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
        monkeypatch.setattr(dashboard.db, "list_pallet_types", lambda *a, **k: [])
        monkeypatch.setattr(dashboard.db, "list_branches", lambda *a, **k: list(BRANCHES))
        monkeypatch.setattr(dashboard.db, "list_vehicles", lambda *a, **k: [dict(VAN)])
        monkeypatch.setattr(dashboard.db, "list_bookings", lambda *a, **k: [])
        monkeypatch.setattr(dashboard.db, "get_vehicle",
                            lambda vid, tid, **k: dict(VAN) if vid == "v-aaa" else None)
        monkeypatch.setattr(dashboard.db, "list_vehicle_day_bookings",
                            lambda *a, **k: [dict(RAW_BOOKING)])
        monkeypatch.setattr(dashboard.db, "hold_fleet_booking", hold)
        monkeypatch.setattr(dashboard.db, "set_booking_routing",
                            lambda bid, tid, **kw: self.routing_calls.append(
                                {"booking_id": bid, **kw}))
        monkeypatch.setattr(dashboard.calendar_sync, "push_bookings", push)


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)   # no `with`: startup events must not run
    app.dependency_overrides.clear()


def test_exports_download_with_filenames(client, monkeypatch):
    Env(monkeypatch)
    r = client.get("/app/fleet/vehicles/v-aaa/day/2026-06-18/manifest.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert 'filename="manifest-yx24-abc-2026-06-18.csv"' in r.headers["content-disposition"]
    assert "PO-1042" in r.text
    r = client.get("/app/fleet/vehicles/v-aaa/day/2026-06-18/manifest.xlsx")
    assert r.status_code == 200 and "spreadsheetml" in r.headers["content-type"]


def test_everything_gated_on_tms(client, monkeypatch):
    Env(monkeypatch, modules=("scheduling",))   # TMS add-on off
    assert client.get("/app/fleet/import").status_code == 404
    assert client.get("/app/fleet/vehicles/v-aaa/day/2026-06-18/manifest.csv").status_code == 404
    r = client.post("/app/fleet/import/commit", data={"payload": "[]"})
    assert r.status_code == 404


def test_upload_preview_then_commit(client, monkeypatch):
    env = Env(monkeypatch)
    future = (date.today() + timedelta(days=7)).isoformat()
    book = (f"Date,Pallets,Type,Ref,From,To\r\n"
            f"{future},4,full,PO-7,WF17 9LU,LS9 8AS\r\n"
            f"{future},9,full,PO-8,,\r\n").encode("utf-8")   # 9 > the 6-space van

    r = client.post("/app/fleet/import/preview",
                    files={"file": ("orders.csv", book, "text/csv")})
    assert r.status_code == 200
    assert "fits" in r.text and "no van fits today" in r.text
    assert "PO-7" in r.text

    payload = json.dumps([
        {"line": 2, "date": future, "qty": 4, "pallet_type": "full",
         "weight_kg": None, "summary": "", "contact": "ops@x.co",
         "reference": "PO-7", "collection_postcode": "WF17 9LU",
         "delivery_postcode": "LS9 8AS", "branch_id": "br-leeds"},
    ])
    r = client.post("/app/fleet/import/commit", data={"payload": payload})
    assert r.status_code == 200 and "Booked" in r.text and "7.5t box" in r.text
    assert "BK-1001" in r.text                  # the issued ref shows per row
    assert env.holds[0]["status"] == "confirmed"
    assert env.holds[0]["source"] == "import"
    assert env.holds[0]["branch_id"] == "br-leeds"
    assert env.routing_calls == [{"booking_id": "b-1", "client": None,
                                  "collection_postcode": "WF17 9LU",
                                  "delivery_postcode": "LS9 8AS",
                                  "reference": "PO-7"}]


def test_commit_rejects_garbage_payload(client, monkeypatch):
    Env(monkeypatch)
    assert client.post("/app/fleet/import/commit",
                       data={"payload": "not json"}).status_code == 400
    r = client.post("/app/fleet/import/commit",
                    data={"payload": json.dumps([{"date": "junk", "qty": 1, "line": 2}])})
    assert r.status_code == 200 and "Bad row" in r.text


@pytest.fixture(autouse=True)
def _established_booking_policy(monkeypatch):
    from app import db
    from tests.booking_fixtures import carried_config
    monkeypatch.setattr(db, "get_scheduling_config", lambda *a, **k: carried_config())
