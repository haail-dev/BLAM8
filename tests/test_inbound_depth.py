"""Inbound-business depth, wired end-to-end through scheduling.make_executor: typed resource
pools, quantity/party-size (weighted capacity), the turnaround gap, stay pricing/deposits,
per-day hours, recurring series and delivery capture. DB is mocked; the math is slotgrid +
diary_config (tested separately)."""
from tests.booking_fixtures import carried_config
from datetime import datetime

from app import scheduling, slotgrid
from app.scheduling import DIARY_TZ

NOW = datetime(2026, 6, 16, 8, 0, tzinfo=DIARY_TZ)  # Tuesday, before opening
OPEN = carried_config({"open_time": "09:00", "close_time": "17:00", "slot_minutes": 30,
        "working_days": "mon,tue,wed,thu,fri,sat,sun", "min_notice_hours": 0,
        "capacity_per_window": 1})


def _row(day, sb, length, status="confirmed", **extra):
    return {"booking_date": day, "start_block": sb, "length_blocks": length,
            "status": status, "spaces": None, "engineer_id": None, **extra}


def _exec(monkeypatch, cfg, existing, inserted, **kw):
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(cfg))
    monkeypatch.setattr(scheduling.db, "list_active_bookings", lambda *a, **k: existing)
    monkeypatch.setattr(scheduling.db, "set_booking_quote", lambda *a, **k: None)
    monkeypatch.setattr(scheduling.db, "offered_bookings_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "release_offered_for_contact", lambda *a, **k: 0)

    def fake_insert(tenant_id, date_iso, sb, length, summary, **ins):
        row = {"id": f"b{len(inserted)+1}", "booking_ref": f"BK-{9000+len(inserted)}",
               "booking_date": date_iso, "start_block": sb, "length_blocks": length,
               "summary": summary, **ins}
        inserted.append(row)
        return row
    monkeypatch.setattr(scheduling.db, "insert_stay_booking", fake_insert)
    return scheduling.make_executor("t1", contact="c@x.co", now=NOW, **kw)


# ── typed resource pools (#1) ─────────────────────────────────────────────────
RESOURCE_CFG = {**OPEN, "resource_types": [
    {"name": "Single", "count": 1, "rate": 60, "rate_unit": "night"},
    {"name": "Double", "count": 2, "rate": 85, "rate_unit": "night"}]}


def test_resource_pools_are_independent(monkeypatch):
    # One Single booked solid; a Double stay still fits (different pool).
    existing = [_row("2026-06-20", 0, slotgrid.BLOCKS_PER_DAY * 2, resource_type="Single")]
    execute, _ = _exec(monkeypatch, RESOURCE_CFG, existing, [], stay=True)
    out = execute("check_availability", {"arrival": "2026-06-20T14:00",
                                         "departure": "2026-06-22T10:00", "resource": "Double"})
    assert out["available"] is True and out["resource"] == "Double"
    # The Single pool (capacity 1) is full for those dates.
    out2 = execute("check_availability", {"arrival": "2026-06-20T14:00",
                                          "departure": "2026-06-22T10:00", "resource": "Single"})
    assert out2["available"] is False


def test_unknown_resource_is_rejected(monkeypatch):
    execute, _ = _exec(monkeypatch, RESOURCE_CFG, [], [], stay=True)
    out = execute("check_availability", {"arrival": "2026-06-20T14:00",
                                         "departure": "2026-06-21T10:00", "resource": "Penthouse"})
    assert out["error"] == "unknown_resource" and "Double" in out["resources"]


def test_resource_hold_stamps_type_and_quote(monkeypatch):
    inserted = []
    execute, _ = _exec(monkeypatch, RESOURCE_CFG, [], inserted, stay=True)
    out = execute("hold_booking", {"arrival": "2026-06-20T14:00", "departure": "2026-06-22T10:00",
                                   "resource": "Double", "summary": "Double room"})
    assert out["held"] and out["resource"] == "Double"
    assert inserted[0]["resource_type"] == "Double"
    assert out["quote"]["amount"] == 170.0          # 2 nights @ £85


# ── quantity / party-size (#3, #7) ────────────────────────────────────────────
def test_quantity_weighted_capacity(monkeypatch):
    # Hire stock of 3 of one item; 2 already out for the dates → a request for 2 won't fit, 1 will.
    cfg = {**OPEN, "resource_types": [{"name": "Excavator", "count": 3, "rate": 120, "rate_unit": "day"}]}
    existing = [_row("2026-06-20", 0, slotgrid.BLOCKS_PER_DAY * 3, resource_type="Excavator", quantity=2)]
    execute, _ = _exec(monkeypatch, cfg, existing, [], stay=True)
    full = execute("check_availability", {"arrival": "2026-06-20T09:00",
                                          "departure": "2026-06-22T17:00",
                                          "resource": "Excavator", "quantity": 2})
    assert full["available"] is False               # 2 + 2 > 3
    ok = execute("check_availability", {"arrival": "2026-06-20T09:00",
                                        "departure": "2026-06-22T17:00",
                                        "resource": "Excavator", "quantity": 1})
    assert ok["available"] is True                  # 2 + 1 = 3


def test_party_size_counts_people(monkeypatch):
    # Restaurant: a 2-hour "Table" service, capacity = 40 covers, party-size mode on.
    cfg = carried_config({**OPEN, "capacity_per_window": 40, "party_size": {"enabled": True, "max_people": 40},
           "service_types": [{"name": "Table", "minutes": 120}]})
    existing = [_row("2026-06-17", 40, 8, window="10:00", quantity=36)]   # 36 covers 10:00-12:00
    execute, _ = _exec(monkeypatch, cfg, existing, [], service_types=cfg["service_types"])
    out = execute("check_availability", {"date_from": "2026-06-17", "date_to": "2026-06-17",
                                         "service": "Table", "party_size": 6})
    by = {s["window"]: s["remaining"] for s in out["available"]}
    assert "10:00" not in by                          # 36 + 6 > 40
    assert by.get("12:00") == 40                       # after the sitting, full 40 free


# ── turnaround / buffer (#5) ──────────────────────────────────────────────────
def test_turnaround_gap_blocks_adjacent(monkeypatch):
    cfg = {**OPEN, "buffer_minutes": 30, "service_types": [{"name": "Cut", "minutes": 60}]}
    existing = [_row("2026-06-17", 40, 4, window="10:00")]   # 10:00-11:00 booked, cap 1
    execute, _ = _exec(monkeypatch, cfg, existing, [], service_types=cfg["service_types"])
    out = execute("check_availability", {"date_from": "2026-06-17", "date_to": "2026-06-17",
                                         "service": "Cut"})
    windows = [s["window"] for s in out["available"]]
    # 10:00-11:00 booked + 30-min turnaround needs a 30-min gap each side. A 60-min Cut can't
    # end after 09:30 in the morning (opening is 09:00, so nothing fits); the first free start
    # is 11:30 (begins 30 min after the 11:00 finish).
    assert windows[0] == "11:30"
    assert "09:00" not in windows and "11:00" not in windows and "09:30" not in windows


# ── stay pricing, min-nights, check-in/out (#2, #7) ───────────────────────────
PRICED = carried_config({**OPEN, "stay_pricing": {"rate": 30, "rate_unit": "night", "deposit": 50, "min_nights": 2},
          "capacity_per_window": 5,
          "day_hours": {"checkin_after": "14:00", "checkout_before": "11:00"}})


def test_min_nights_and_checkin_rules(monkeypatch):
    execute, _ = _exec(monkeypatch, PRICED, [], [], stay=True)
    short = execute("check_availability", {"arrival": "2026-06-20T14:00", "departure": "2026-06-21T10:00"})
    assert short["error"] == "min_nights"
    early = execute("check_availability", {"arrival": "2026-06-20T09:00", "departure": "2026-06-23T10:00"})
    assert early["error"] == "checkin_time"
    ok = execute("check_availability", {"arrival": "2026-06-20T14:00", "departure": "2026-06-23T10:00"})
    assert ok["available"] and ok["quote"]["amount"] == 90.0    # 3 nights @ £30


def test_stay_hold_records_deposit(monkeypatch):
    inserted = []
    execute, _ = _exec(monkeypatch, PRICED, [], inserted, stay=True)
    out = execute("hold_booking", {"arrival": "2026-06-20T14:00", "departure": "2026-06-23T10:00",
                                   "summary": "Room"})
    assert out["deposit"] == 50.0 and out["quote"]["amount"] == 90.0
    assert inserted[0]["deposit_amount"] == 50.0


# ── per-day hours (#7) ────────────────────────────────────────────────────────
def test_per_day_hours_shorten_saturday(monkeypatch):
    cfg = {**OPEN, "day_hours": {"sat": {"open": "09:00", "close": "13:00"}},
           "service_types": [{"name": "Cut", "minutes": 60}]}
    execute, _ = _exec(monkeypatch, cfg, [], [], service_types=cfg["service_types"])
    sat = execute("check_availability", {"date_from": "2026-06-20", "date_to": "2026-06-20",
                                         "service": "Cut"})   # 2026-06-20 is a Saturday
    windows = [s["window"] for s in sat["available"]]
    assert windows[0] == "09:00" and windows[-1] == "12:00"    # last 60-min start before 13:00


# ── recurring (#4) ────────────────────────────────────────────────────────────
def test_recurring_places_a_series(monkeypatch):
    cfg = {**OPEN, "recurring": {"enabled": True, "max_occurrences": 12, "frequencies": ["weekly"]},
           "service_types": [{"name": "Lesson", "minutes": 60}]}
    inserted = []
    execute, _ = _exec(monkeypatch, cfg, [], inserted, service_types=cfg["service_types"])
    out = execute("hold_booking", {"date": "2026-06-17", "window": "10:00", "service": "Lesson",
                                   "summary": "Weekly lesson", "repeat": "weekly", "occurrences": 3})
    assert out["held"] and out["occurrences_booked"] == ["2026-06-17", "2026-06-24", "2026-07-01"]
    assert len(inserted) == 3
    rec = {r["recurrence_id"] for r in inserted}
    assert len(rec) == 1 and None not in rec          # all linked by one recurrence id


# ── delivery (#8) ─────────────────────────────────────────────────────────────
def test_delivery_captured_on_hold(monkeypatch):
    cfg = {**OPEN, "delivery": {"offered": True, "fee": 15},
           "resource_types": [{"name": "Excavator", "count": 2, "rate": 120, "rate_unit": "day"}]}
    inserted = []
    execute, _ = _exec(monkeypatch, cfg, [], inserted, stay=True)
    out = execute("hold_booking", {"arrival": "2026-06-20T09:00", "departure": "2026-06-22T17:00",
                                   "resource": "Excavator", "summary": "Digger hire",
                                   "delivery_required": True, "delivery_address": "Site B, LS1 1AA"})
    assert out["held"]
    assert inserted[0]["details"]["Delivery"] == "Yes"
    assert inserted[0]["details"]["Delivery address"] == "Site B, LS1 1AA"
