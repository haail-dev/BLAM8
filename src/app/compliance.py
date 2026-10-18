"""Compliance — driver hours + driver and fleet-resource document expiry (TMS baseline).

Pure date math: drivers + fleet resources + this week's hours + today in, RAG-flagged
rows out. No DVLA/tachograph integration (the named follow-on) — just the dates a
transport office must not let lapse, and a weekly-hours proxy.
"""
from datetime import date, datetime, timedelta

from app import db
from app.scheduling import DIARY_TZ

SOON_DAYS = 30
WEEKLY_DRIVING_LIMIT = 56.0   # EU weekly driving-hours limit — the demo yardstick

# worst-first ordering for rolling several statuses into one overall flag
_RANK = {"expired": 0, "soon": 1, "unknown": 2, "ok": 3}


def _date_status(d, today: date) -> str:
    """RAG for an expiry date vs today: expired | soon | ok | unknown."""
    if not d:
        return "unknown"
    dd = d if isinstance(d, date) else date.fromisoformat(str(d)[:10])
    if dd < today:
        return "expired"
    if dd <= today + timedelta(days=SOON_DAYS):
        return "soon"
    return "ok"


def _worst(*statuses) -> str:
    return min(statuses, key=lambda s: _RANK.get(s, 3)) if statuses else "unknown"


def driver_row(driver: dict, hours_this_week: float, today: date) -> dict:
    lic = _date_status(driver.get("licence_expiry"), today)
    cpc = _date_status(driver.get("cpc_expiry"), today)
    hrs = round(float(hours_this_week or 0), 1)
    hours_status = ("expired" if hrs > WEEKLY_DRIVING_LIMIT
                    else "soon" if hrs > 0.9 * WEEKLY_DRIVING_LIMIT else "ok")
    return {
        "name": driver.get("name"),
        "licence_expiry": str(driver.get("licence_expiry") or "—")[:10], "licence_status": lic,
        "cpc_expiry": str(driver.get("cpc_expiry") or "—")[:10], "cpc_status": cpc,
        "hours": hrs, "hours_limit": WEEKLY_DRIVING_LIMIT, "hours_status": hours_status,
        "overall": _worst(lic, cpc, hours_status),
    }


def vehicle_row(vehicle: dict, today: date) -> dict:
    fields = [("mot_expiry", "MOT"), ("tax_expiry", "Tax"),
              ("service_due", "Service"), ("insurance_expiry", "Insurance")]
    items, statuses = {}, []
    for key, label in fields:
        s = _date_status(vehicle.get(key), today)
        items[key] = {"label": label, "date": str(vehicle.get(key) or "—")[:10], "status": s}
        statuses.append(s)
    # NB: key is 'docs' not 'items' — Jinja's `v.items` resolves to dict.items().
    return {"name": vehicle.get("name"), "registration": vehicle.get("registration"),
            "docs": items, "overall": _worst(*statuses)}


def overview(drivers: list[dict], vehicles: list[dict], driver_hours: list[dict],
             today: date) -> dict:
    """The compliance board: driver rows + fleet resource rows + an alert count (anything
    expired or due soon). Hours are summed for the current week (Mon→today)."""
    week_start = today - timedelta(days=today.weekday())
    by_driver: dict = {}
    for h in driver_hours:
        wd = h.get("work_date")
        wd = wd if isinstance(wd, date) else date.fromisoformat(str(wd)[:10])
        if wd >= week_start:
            by_driver[h.get("driver_id")] = by_driver.get(h.get("driver_id"), 0.0) + float(h.get("hours") or 0)
    drows = [driver_row(d, by_driver.get(d.get("id"), 0.0), today) for d in drivers]
    vrows = [vehicle_row(v, today) for v in vehicles]
    alerts = sum(1 for r in drows + vrows if r["overall"] in ("expired", "soon"))
    return {"drivers": drows, "vehicles": vrows, "alerts": alerts,
            "week_start": week_start.isoformat(), "driving_limit": WEEKLY_DRIVING_LIMIT}


def view(tenant_id: str, client=None, now: datetime | None = None) -> dict:
    now = (now or datetime.now(DIARY_TZ)).astimezone(DIARY_TZ)
    today = now.date()
    week_start = (today - timedelta(days=today.weekday())).isoformat()
    return overview(
        db.list_drivers(tenant_id, client=client),
        db.list_vehicles(tenant_id, client=client, active_only=True),
        db.list_driver_hours(tenant_id, date_from_iso=week_start, client=client),
        today,
    )
