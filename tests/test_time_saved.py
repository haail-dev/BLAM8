"""Time saved (Kris 2026-09-24) — by-hand minutes per email kind and Concierge
action, net of the human time spent, never double-counted.

Run: python -m pytest tests/test_time_saved.py -q
"""
from app import concierge_actions, db, time_saved as ts
from proposal_receipts import execution_result


def _action(kind, *, secs_to_confirm=10, payload=None, result=None, status="executed"):
    result = dict(result or {})
    for item in result.get("results", []):
        item.update(state="completed" if item["ok"] else "failed", summary="Saved." if item["ok"] else "Refused before dispatch.")
    return {"id": "p1", "tenant_id": "t1", "kind": kind, "status": status, "payload": payload or {},
            "result": execution_result("p1", state="completed", summary="Saved.", **result), "created_at": "2026-09-24T10:00:00+00:00",
            "executed_at": f"2026-09-24T10:00:{secs_to_confirm:02d}+00:00"}


# ── emails ────────────────────────────────────────────────────────────────────
def test_reviewed_reply_saves_by_hand_minus_its_hrt():
    row = {"kind": "inbound", "status": "sent", "sent_via": "human", "review_seconds": 22}
    assert ts.email_saving_s(row) == 6 * 60 - 22


def test_auto_send_saves_the_whole_by_hand_time():
    assert ts.email_saving_s({"kind": "inbound", "status": "sent", "sent_via": "auto"}) == 360


def test_untracked_human_send_assumes_a_minute_of_review():
    row = {"kind": "owner_message", "status": "sent", "sent_via": "human", "review_seconds": None}
    assert ts.email_saving_s(row) == 4 * 60 - ts.UNTRACKED_REVIEW_S


def test_concierge_sent_email_belongs_to_its_action():
    row = {"kind": "invoice_reminder", "status": "sent", "sent_via": "concierge"}
    assert ts.email_saving_s(row) == 0


def test_unsent_email_and_a_long_review_save_nothing():
    assert ts.email_saving_s({"kind": "inbound", "status": "pending"}) == 0
    row = {"kind": "reminder", "status": "sent", "sent_via": "human", "review_seconds": 900}
    assert ts.email_saving_s(row) == 0


# ── Concierge actions ─────────────────────────────────────────────────────────
def test_action_saving_is_net_of_time_to_confirm():
    assert ts.action_saving_s(_action("move_booking", secs_to_confirm=30)) == 4 * 60 - 30


def test_only_executed_actions_count():
    for status in ("failed", "cancelled", "superseded", "pending", "expired"):
        assert ts.action_saving_s(_action("invoice_create", status=status)) == 0


def test_draft_reply_leaves_the_credit_to_the_send():
    assert ts.action_saving_s(_action("draft_reply")) == 0


def test_series_cancel_counts_each_later_visit():
    row = _action("cancel_booking", secs_to_confirm=0, payload={"later_ids": ["a", "b"]})
    assert ts.action_saving_s(row) == (3 + 2 * ts.CANCEL_EXTRA_VISIT_MINUTES) * 60


def test_bulk_counts_only_the_items_that_ran():
    payload = {"items": [{"kind": "block_date", "ok": True, "payload": {}},
                         {"kind": "invoice_create", "ok": True, "payload": {}},
                         {"kind": "update_contact", "ok": True, "payload": {}}]}
    result = {"results": [{"index": 0, "ok": True}, {"index": 1, "ok": False},
                          {"index": 2, "ok": True}]}
    row = _action("bulk", secs_to_confirm=0, payload=payload, result=result)
    assert ts.action_saving_s(row) == (1 + 2) * 60


def test_every_concierge_action_kind_has_a_price():
    # a new action kind must be priced (0 is a valid, deliberate answer)
    assert set(concierge_actions._EXECUTORS) <= set(ts.ACTION_MINUTES)


def test_speaking_time_comes_off_a_voice_card():
    row = _action("move_booking", secs_to_confirm=10, payload={"_spoken_ms": 8000})
    assert ts.action_saving_s(row) == 4 * 60 - 10 - 8


def test_speaking_time_comes_off_a_voice_drafted_email():
    row = {"kind": "owner_message", "status": "sent", "sent_via": "human",
           "review_seconds": None, "spoken_seconds": 12}
    assert ts.email_saving_s(row) == 4 * 60 - ts.UNTRACKED_REVIEW_S - 12


def test_stage_carries_spoken_ms_into_the_payload(monkeypatch):
    saved = {}
    monkeypatch.setattr(concierge_actions, "_STAGERS",
                        {"block_date": lambda t, c, a: ("Block the 3rd", {"date": "x"}, [], None)})
    monkeypatch.setattr(concierge_actions.db, "create_concierge_proposal",
                        lambda t, k, s, payload, **kw: saved.setdefault("p", payload) and "pid")
    concierge_actions.stage("t1", None, {"kind": "block_date"}, full_qa=True,
                            scheduling_on=True, spoken_ms=4200)
    assert saved["p"]["_spoken_ms"] == 4200
    saved.clear()
    concierge_actions.stage("t1", None, {"kind": "block_date"}, full_qa=True, scheduling_on=True)
    assert "_spoken_ms" not in saved["p"]


# ── tally ─────────────────────────────────────────────────────────────────────
def test_by_tenant_splits_and_sums():
    emails = [{"tenant_id": "t1", "kind": "inbound", "status": "sent", "sent_via": "auto"},
              {"tenant_id": "t2", "kind": "inbound", "status": "sent", "sent_via": "concierge"}]
    actions = [_action("block_date", secs_to_confirm=0)]
    out = ts.by_tenant(emails, actions)
    assert out["t1"]["emails"] == {"n": 1, "s": 360}
    assert out["t1"]["actions"] == {"n": 1, "s": 60}
    assert out["t1"]["total_s"] == 420
    assert out["t2"]["total_s"] == 0 and out["t2"]["emails"]["n"] == 0


def test_summary_is_best_effort(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "time_saved_email_rows", boom)
    assert ts.summary() == {}
