"""Diary card acceptance through the route against the real change_setup_card function.

The route builds the expected settings and the patch; only the SQL function decides.
Each case starts on a fresh disposable database with no scheduling row, holding one
Diary card of five scalar lines and a booking-mode line, and says which function it
runs: the 2026-10-04 original (what `initialise` installs) or the 2026-10-19 version
that adds booking_mode and intake_fields to the Diary allowlist.
"""
from copy import deepcopy
import json
from types import SimpleNamespace
import uuid

import pytest
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

from app import db, setup_cards as cards, setup_card_store as store
from app.auth import require_onboarded, TenantCtx
from app.main import app
from tests.test_booking_capacity_database import cluster as base_cluster
from tests.test_setup_cards_database import DIARY_MODE_FORWARD, initialise

BASE = "/concierge/setup-cards"
SCALARS = [("working_days", ["mon", "tue", "wed", "thu", "fri"]), ("open_time", "09:00"), ("close_time", "16:00"),
           ("timezone", "Europe/London"), ("bank_holiday_nation", "england-and-wales")]
MODE = ("stay_mode", False)


@pytest.fixture
def cluster():
    yield from base_cluster.__wrapped__()


class Rpc:
    """The route's client: one RPC per save, errors shaped as PostgREST reports them."""

    def __init__(self, driver, dsn):
        self.driver, self.dsn, self.calls = driver, dsn, []

    def rpc(self, name, args):
        self.name, self.args = name, args
        return self

    def execute(self):
        assert self.name == "change_setup_card"
        self.calls.append(deepcopy(self.args))
        args = {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in self.args.items()}
        with self.driver.connect(self.dsn, autocommit=True) as conn:
            try:
                data = conn.execute("select change_setup_card(" + ",".join(k + " => %s" for k in args) + ")",
                                    tuple(args.values())).fetchone()[0]
            except self.driver.errors.RaiseException as exc:
                raise APIError({"message": exc.diag.message_primary, "code": exc.diag.sqlstate,
                                "hint": exc.diag.message_hint, "details": exc.diag.message_detail}) from None
        return SimpleNamespace(data=data)


def line(key, value):
    return {"op": "append", "name": "setting", "held": True, "fields": {"key": key, "value": value},
            "sources": {"key": {"kind": "owner_said", "reference": "Owner turn"},
                        "value": {"kind": "owner_said", "reference": "Owner turn"}}}


@pytest.fixture
def diary(cluster, monkeypatch):
    """A tenant with no scheduling row and a held Diary card: five scalars plus the mode line."""
    from app import security
    driver, dsn = cluster
    tenant = str(uuid.uuid4())
    with driver.connect(dsn, autocommit=True) as conn:
        initialise(conn)  # installs the 2026-10-04 change_setup_card
        conn.execute("insert into tenants values(%s)", (tenant,))
    client = Rpc(driver, dsn)

    def query(sql, *params):
        with driver.connect(dsn, autocommit=True) as conn:
            return conn.execute(sql, params).fetchone()

    def scheduling(tid, client=None):
        row = query("select to_jsonb(s) from scheduling_config s where tenant_id=%s", tid)
        return row[0] if row else None

    monkeypatch.setattr(security, "_BUCKETS", {})
    monkeypatch.setattr(db, "get_tenant_config", lambda tid, client=None: {"setup_cards": query(
        "select setup_cards from tenant_config where tenant_id=%s", tid)[0]})
    monkeypatch.setattr(db, "get_scheduling_config", scheduling)
    monkeypatch.setattr(db, "setup_has_booking_history", lambda tid, client=None: bool(query(
        "select 1 from bookings where tenant_id=%s limit 1", tid)))
    monkeypatch.setattr(db, "visible_modules", lambda *a, **kw: {"scheduling", "email"})
    monkeypatch.setattr(db, "list_requirement_groups", lambda *a, **kw: [])
    monkeypatch.setattr(db, "crm_list_stages", lambda *a, **kw: [])
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id=tenant, client=client, user_id="owner")
    proposal = cards.change("diary_settings", None, 0, "turn-1", [line(k, v) for k, v in SCALARS + [MODE]])
    store.save(tenant, "diary_settings", 0, proposal, client=client)
    http = TestClient(app)

    def install_20261019():
        with driver.connect(dsn, autocommit=True) as conn:
            conn.execute(DIARY_MODE_FORWARD.read_text(encoding="utf-8"))

    try:
        http.cookies.set("bl_csrf", "token")
        http.headers["x-csrf-token"] = "token"
        yield SimpleNamespace(http=http, client=client, install_20261019=install_20261019,
                              card=lambda: db.get_tenant_config(tenant)["setup_cards"]["diary_settings"],
                              scheduling=lambda: scheduling(tenant))
    finally:
        http.close()
        app.dependency_overrides.clear()


def accept(diary, keys=None):
    """Preview then accept the pending lines for `keys` (default: every pending line)."""
    card = diary.card()
    ids = [v["id"] for v in card["lines"] if v["status"] in cards.PENDING and (keys is None or v["fields"]["key"] in keys)]
    preview = diary.http.post(BASE + "/diary_settings/preview", json={"revision": card["revision"], "ids": ids})
    assert preview.status_code == 200, preview.json()
    return diary.http.post(BASE + "/diary_settings/accept", json={
        "revision": card["revision"], "ids": ids, "settings_version": preview.json()["settings_version"]})


def leave_mode_line(diary):
    card = diary.card()
    mode = next(v for v in card["lines"] if v["fields"]["key"] == "stay_mode")
    left = diary.http.post(BASE + "/diary_settings/changes", json={
        "revision": card["revision"], "changes": [{"op": "remove", "id": mode["id"]}]})
    assert left.status_code == 200, left.json()


def statuses(diary):
    return {v["fields"]["key"]: v["status"] for v in diary.card()["lines"]}


def test_function_20261004_refuses_the_mixed_accept_and_the_owner_is_told_why(diary):
    assert diary.scheduling() is None
    response = accept(diary)
    patch = diary.client.calls[-1]["p_settings_patch"]
    # The route sends the mode line's booking_mode and intake_fields; the 2026-10-04 Diary
    # allowlist ends at 'nouns', so the key check raises before any write.
    assert {"booking_mode", "intake_fields", "working_days"} <= set(patch)
    assert diary.client.calls[-1]["p_expected_settings"] == {"_setup_has_bookings": False}
    assert response.status_code == 422
    assert "database migrations" in response.json()["detail"]
    # Nothing was written: no row, the card is still at revision 1 with six held lines.
    assert diary.scheduling() is None
    assert diary.card()["revision"] == 1 and set(statuses(diary).values()) == {"held"}


def test_function_20261004_accepts_the_five_scalars_after_the_mode_line_is_left(diary):
    leave_mode_line(diary)
    response = accept(diary)
    assert response.status_code == 200, response.json()
    saved = diary.scheduling()
    assert saved["working_days"] == "mon,tue,wed,thu,fri" and saved["timezone"] == "Europe/London"
    assert (saved["open_time"], saved["close_time"]) == ("09:00:00", "16:00:00")
    assert saved["booking_mode"] == "appointment" and saved["booking_revision"] == 1
    assert statuses(diary)["stay_mode"] == "left"
    assert "booking_mode" not in diary.client.calls[-1]["p_settings_patch"]


def test_function_20261019_accepts_the_mixed_accept_and_keeps_the_appointment_mode(diary):
    diary.install_20261019()
    response = accept(diary)
    assert response.status_code == 200, response.json()
    saved = diary.scheduling()
    assert saved["working_days"] == "mon,tue,wed,thu,fri"
    assert (saved["open_time"], saved["close_time"]) == ("09:00:00", "16:00:00")
    assert saved["timezone"] == "Europe/London" and saved["bank_holiday_nation"] == "england-and-wales"
    assert saved["booking_mode"] == "appointment" and saved["intake_fields"] == []
    assert saved["booking_revision"] == 1
    assert {k: v["state"] for k, v in saved["booking_setting_review"].items()} == {
        "working_days": "set", "opening_hours": "set", "timezone": "set"}
    card = response.json()["card"]
    assert card["revision"] == 2 and card["status"] == "applied"
    assert diary.card() == card and set(statuses(diary).values()) == {"applied"}


def test_function_20261019_accepts_the_scalars_alone_while_the_mode_line_stays_held(diary):
    diary.install_20261019()
    response = accept(diary, {k for k, _ in SCALARS})
    assert response.status_code == 200, response.json()
    saved = diary.scheduling()
    assert saved["working_days"] == "mon,tue,wed,thu,fri" and saved["booking_mode"] == "appointment"
    assert statuses(diary)["stay_mode"] == "held"
