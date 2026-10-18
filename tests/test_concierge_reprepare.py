"""R4b part 2: a parked card is prepared AGAIN on tap (a new card, never the old one), only
for the kinds that are safe to, and only within the owner's task-expiry setting.

The store and routes are the in-memory ones from tests/test_concierge_tap_only_delete.py.

Run: python tests/run_offline.py tests/test_concierge_reprepare.py -q
"""
from datetime import datetime, timedelta, timezone

import pytest

from app import concierge_actions, concierge_routes, db
from tests.test_concierge_draft_edit_delete import rig  # noqa: F401
from tests.test_concierge_draft_edit_delete_quotes import qrig  # noqa: F401
from tests.test_concierge_tap_only_delete import T1, world  # noqa: F401


def _park(world, kind, *, age=timedelta(hours=2), status="superseded", user="u1", payload=None):
    pid = f"old-{kind}-{len(world.store.s['concierge_proposals'])}"
    world.store.s["concierge_proposals"].append({
        "id": pid, "tenant_id": T1, "user_id": user, "kind": kind, "summary": f"A {kind}",
        "payload": {"_action": {"kind": kind}, **(payload or {})}, "status": status, "result": {},
        "executed_at": None, "created_at": (datetime.now(timezone.utc) - age).isoformat()})
    return pid


@pytest.fixture
def parked(world, monkeypatch):
    world.staged, world.links, world.stamps = [], [], []
    monkeypatch.setattr(db, "get_tenant", lambda *a, **k: {"concierge_settings": {}})
    monkeypatch.setattr(db, "visible_modules", lambda *a, **k: ["crm", "scheduling"])
    monkeypatch.setattr(db, "stamp_concierge_decision",
                        lambda pid, t, via, **k: world.stamps.append((pid, via)) or True)
    monkeypatch.setattr(db, "link_reprepared", lambda new, old, t: world.links.append((new, old)) or True)
    monkeypatch.setattr(concierge_routes.concierge_tasks, "follow_card", lambda *a, **k: None)

    def fake_stage(tenant, client, action, **kw):
        world.staged.append(action["kind"])
        return {"proposal_id": f"new-{len(world.staged)}", "kind": action["kind"], "summary": "fresh"}
    monkeypatch.setattr(concierge_actions, "stage", fake_stage)
    monkeypatch.setattr(concierge_routes, "_asker", lambda ctx: None, raising=False)
    return world


def _tap(world, pid):
    return world.http.post("/concierge/business/resurface", json={"proposal_id": pid}).json()


@pytest.mark.parametrize("kind", sorted(concierge_actions.REPREPARE_SILENT - {"confirm_booking"}))
def test_a_safe_kind_is_prepared_again_as_a_new_card_and_says_so(parked, kind):
    old = _park(parked, kind)
    out = _tap(parked, old)
    assert out["ok"] and out["proposal"]["proposal_id"] != old
    assert out["proposal"]["notice"] == "Prepared again for you to confirm."
    assert out["proposal"]["reprepared_from"] == old
    assert parked.links == [(out["proposal"]["proposal_id"], old)]
    row = next(r for r in parked.store.s["concierge_proposals"] if r["id"] == old)
    assert row["status"] == "resurfaced"


def test_the_old_card_can_never_be_confirmed_after_it_was_prepared_again(parked):
    old = _park(parked, "block_date")
    assert _tap(parked, old)["ok"]
    res = parked.http.post("/concierge/business/execute", json={"proposal_id": old, "confirm_via": "tap"})
    assert res.json().get("ok") is not True and res.json().get("state") != "completed"
    assert _tap(parked, old)["ok"] is False                 # and it does not come back twice


@pytest.mark.parametrize("kind,payload", [
    ("update_deal_value", None), ("move_booking", None), ("invoice_create", None),
    ("update_contact", {"mode": "field"}), ("confirm_booking", {"then": {"contact": "a@example.com"}}),
])
def test_a_kind_that_may_have_changed_goes_back_through_a_visible_revise(parked, kind, payload):
    out = _tap(parked, _park(parked, kind, payload=payload))
    assert out["ok"] and out["proposal"]["notice"].startswith("This may have changed since I first prepared it.")
    assert "confirm again" in out["proposal"]["notice"]


def test_an_update_contact_note_and_a_plain_confirm_booking_are_silent(parked):
    assert concierge_actions.reprepare_class("update_contact", {"mode": "note"}) == "silent"
    assert concierge_actions.reprepare_class("confirm_booking", {}) == "silent"


@pytest.mark.parametrize("kind", sorted(concierge_actions.REPREPARE_NEVER))
def test_sends_deletes_edits_reminders_renewals_and_bulk_are_never_prepared_again(parked, kind):
    old = _park(parked, kind)
    out = _tap(parked, old)
    assert out == {"ok": False, "error": concierge_actions.NEVER_SENTENCE}
    assert parked.staged == [] and parked.links == []
    assert next(r for r in parked.store.s["concierge_proposals"] if r["id"] == old)["status"] == "superseded"


def test_past_the_owners_expiry_setting_it_is_expired_not_prepared_again(parked):
    old = _park(parked, "block_date", age=timedelta(hours=30))          # default setting: 24 hours
    out = _tap(parked, old)
    assert out["ok"] is False and out["error"].startswith("That one has expired (kept for 24 hours)")
    assert out["error"].endswith("Ask me to prepare it again.")
    assert parked.staged == [] and parked.stamps == [(old, "system")]
    assert next(r for r in parked.store.s["concierge_proposals"] if r["id"] == old)["status"] == "expired"


def test_the_setting_is_what_decides_the_lifetime(parked, monkeypatch):
    monkeypatch.setattr(db, "get_tenant", lambda *a, **k: {"concierge_settings": {"task_expire_minutes": 7 * 24 * 60}})
    assert _tap(parked, _park(parked, "block_date", age=timedelta(hours=30)))["ok"] is True
    monkeypatch.setattr(db, "get_tenant", lambda *a, **k: {"concierge_settings": {"task_expire_minutes": 60}})
    assert _tap(parked, _park(parked, "block_date", age=timedelta(hours=2)))["ok"] is False
    assert concierge_actions.parked_lifetime_s(T1) == 3600


def test_a_colleague_cannot_bring_back_someone_elses_card(parked):
    old = _park(parked, "block_date", user="someone-else")
    assert _tap(parked, old) == {"ok": False, "error": "That one isn't waiting any more."}
    assert parked.staged == []


def test_the_waiting_list_hides_never_kinds_and_honours_the_lifetime():
    class Q:
        def __init__(self, rows):
            self.rows, self.cut = rows, None

        def select(self, *a, **k): return self
        def eq(self, *a, **k): return self
        def in_(self, *a, **k): return self
        def order(self, *a, **k): return self
        def limit(self, *a, **k): return self
        def gte(self, col, v):
            self.cut = v
            return self
        def execute(self):
            from types import SimpleNamespace
            return SimpleNamespace(data=[r for r in self.rows if r["created_at"] >= self.cut])
    now = datetime.now(timezone.utc)
    rows = [dict(id=k, kind=k, summary=k, status="superseded", user_id="u1",
                 created_at=(now - age).isoformat(), payload={"_action": {"kind": "x"}})
            for k, age in (("block_date", timedelta(hours=2)), ("invoice_send", timedelta(hours=2)),
                           ("assign_person", timedelta(hours=30)))]
    client = type("C", (), {"table": lambda s, n: Q(rows)})()
    got = db.list_waiting_proposals(T1, "u1", client=client, max_age_s=24 * 3600)
    assert [r["kind"] for r in got] == ["block_date"]
