"""Confirm first, then the email (docs/plan-concierge-confirm-first.md).

Found on the Hartwell rig, 2026-09-30: "Tell Ann she's booked in" with BK-1015
only `proposed`. A held time waits on the BUSINESS, so the Concierge offers to
confirm it first (one card: confirm, then the email) instead of a held draft
and a "pencilled in, please confirm" question the customer cannot answer.

Pinned here:
  * a firm claim on a held booking is not reworded - voice and typed both
    offer the confirm card; a past date is a move-first hold; an offered slot
    keeps the pick-a-time wording;
  * confirm_booking stages and executes through ONE shared confirm function
    (the Diary's Confirm button uses it too), and if the confirm fails no
    email is queued;
  * show_draft never resurfaces an older, unrelated draft after a hold.

Run: python -m pytest tests/test_concierge_confirm_first.py -q
"""
import asyncio
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from app import concierge_actions as actions, concierge_booking_claims as guard
from app import concierge_draft, agent, dashboard, pipeline, booking_confirm
from app.auth import TenantCtx

CTX = TenantCtx(tenant_id="t1", client=None)
ANN = "ann@example.com"
FUTURE = "2999-01-01"
BK = {"id": "b-1015", "booking_ref": "BK-1015", "booking_date": FUTURE, "status": "proposed",
      "summary": "Emergency callout - Ann", "window": "pm", "contact": ANN}
BAD = "Hi Ann, your emergency callout is booked in (ref BK-1015)."
PENCILLED = "Hi Ann, BK-1015 is pencilled in. Please confirm."


def _diary(monkeypatch, rows):
    monkeypatch.setattr(guard, "read_bookings", lambda *a, **k: rows)
    monkeypatch.setattr(guard, "_business_today", lambda *a, **k: date(2026, 9, 30))
    monkeypatch.setattr(guard.db, "effective_config", lambda *a, **k: {})
    monkeypatch.setattr(guard.db, "active_modules", lambda t: {"scheduling"})
    monkeypatch.setattr("app.usage.record_anthropic", lambda *a, **k: None)


def _voice(monkeypatch, rows):
    """Wire the voice draft path: compose writes BAD, redraft writes BAD."""
    from tests.test_concierge_voice_draft import _wire, WAITING
    calls = _wire(monkeypatch, WAITING)
    _diary(monkeypatch, rows)
    composed, events = [], []

    async def compose(cfg, purpose, facts, **k):
        composed.append(purpose)
        return SimpleNamespace(text=BAD, model="m", usage={})

    async def redraft(*a, **k):
        calls["redraft"].append(k)
        return BAD
    monkeypatch.setattr(agent, "compose_outbound", compose)
    monkeypatch.setattr(pipeline, "redraft_email_log", redraft)
    monkeypatch.setattr(concierge_draft, "_retire_earlier_drafts", lambda *a: 0)
    monkeypatch.setattr(concierge_draft.db, "update_email_log_draft", lambda *a, **k: None)
    monkeypatch.setattr(concierge_draft.db, "set_email_log_category", lambda *a, **k: None)
    monkeypatch.setattr(concierge_draft.db, "log_agent_event",
                        lambda tenant, kind, summary, **k: events.append((kind, k.get("detail"))))
    return calls, composed, events


# ── voice: confirm first, never a reworded email ─────────────────────────────
@pytest.mark.parametrize("mode", ["new", "reply"])
def test_a_held_booking_offers_confirm_first_and_is_not_reworded(monkeypatch, mode):
    calls, composed, events = _voice(monkeypatch, [BK])
    result, card = asyncio.run(concierge_draft.draft(
        CTX, {"contact": ANN, "mode": mode, "instruction": "Tell Ann she is booked in"}, member=None))
    msg = result["message"]
    assert not result["ok"] and card["status"] == "empty"
    # the server stages the card (the model writes no action): booking, recipient, owner's words
    assert result["confirm_action"] == {"kind": "confirm_booking", "booking_id": "BK-1015",
                                        "contact": ANN, "instruction": "Tell Ann she is booked in"}
    assert "ALREADY on the owner's screen" in msg and "Shall I confirm it and draft the email?" in msg
    assert "Do NOT offer a 'pencilled in' email" in msg and "do NOT call show_draft" in msg
    # no rewrite attempt, nothing queued
    if mode == "new":
        assert len(composed) == 1 and not calls["insert"]
    else:
        assert len(calls["redraft"]) == 1
    assert any(k == "concierge_refused" and d["reason"] == "booking_wording" and d["hold"]
               for k, d in events)


def test_a_past_booking_is_a_move_first_hold(monkeypatch):
    _, composed, _ = _voice(monkeypatch, [{**BK, "booking_date": "2026-09-28"}])
    result, _ = asyncio.run(concierge_draft.draft(
        CTX, {"contact": ANN, "mode": "new", "instruction": "Tell Ann she is booked in"}, member=None))
    assert "has passed" in result["message"] and "'move_booking'" in result["message"]
    assert "confirm_action" not in result and len(composed) == 1


def test_without_a_diary_a_held_booking_is_a_plain_hold(monkeypatch):
    _voice(monkeypatch, [BK])
    monkeypatch.setattr(guard.db, "active_modules", lambda t: set())
    result, _ = asyncio.run(concierge_draft.draft(
        CTX, {"contact": ANN, "mode": "new", "instruction": "Tell Ann she is booked in"}, member=None))
    assert "confirm_action" not in result and "/app/diary" in result["message"]
    assert "do NOT call show_draft" in result["message"] or "Do NOT call show_draft" in result["message"]


def test_the_voice_route_attaches_the_servers_confirm_card_not_one_the_model_wrote():
    src = open("src/app/concierge_routes.py", encoding="utf-8").read()
    assert src.count('drafted["confirm_action"] = result.pop("confirm_action")') == 2   # draft_email + show_draft
    assert 'result = result._replace(action=drafted["confirm_action"])' in src
    assert 'drafted.get("confirm_action") and not (result.action or {}).get("kind")' in src


def test_an_offered_slot_keeps_the_pick_a_time_wording(monkeypatch):
    _, composed, _ = _voice(monkeypatch, [{**BK, "status": "offered"}])
    result, _ = asyncio.run(concierge_draft.draft(
        CTX, {"contact": ANN, "mode": "new", "instruction": "Tell Ann she is booked in"}, member=None))
    assert len(composed) == 2                       # the one corrective rewrite still runs
    assert "has not picked a time yet" in result["message"] or "reworded" in result["message"]


# ── show_draft never swaps in an older draft after a hold ────────────────────
def test_show_draft_after_a_hold_does_not_surface_an_older_draft(monkeypatch):
    _diary(monkeypatch, [BK])
    older = {"id": "old", "kind": "inbound", "status": "pending", "contact_name": "Ann",
             "draft_reply": "Hi Ann, we'll be there Monday."}
    monkeypatch.setattr(concierge_draft.db, "get_conversation", lambda *a, **k: [older])
    monkeypatch.setattr(dashboard, "_require_their_log", lambda *a: None)
    monkeypatch.setattr(concierge_draft, "_recent_hold", lambda *a: guard.violation(BAD, [BK]))
    result, card = asyncio.run(concierge_draft.show(CTX, {"contact": ANN}, member=None))
    assert not result["ok"] and card["status"] == "empty" and not card["text"]
    assert result["confirm_action"]["booking_id"] == "BK-1015" and "No heating" not in str(card)


def test_show_draft_without_a_recent_hold_still_brings_back_the_waiting_draft(monkeypatch):
    _diary(monkeypatch, [{**BK, "status": "confirmed"}])
    rows = [{"id": "old", "kind": "inbound", "status": "pending", "contact_name": "Ann",
             "draft_reply": "Hi Ann, all fine."}]
    monkeypatch.setattr(concierge_draft.db, "get_conversation", lambda *a, **k: rows)
    monkeypatch.setattr(dashboard, "_require_their_log", lambda *a: None)
    monkeypatch.setattr(concierge_draft, "_recent_hold", lambda *a: None)
    result, card = asyncio.run(concierge_draft.show(CTX, {"contact": ANN}, member=None))
    assert result["ok"] and card["status"] == "pending"


def test_recent_hold_reads_the_log_and_a_later_draft_clears_it():
    def client(rows):
        class Q:
            def __getattr__(self, name):
                def call(*a, **k):
                    return SimpleNamespace(data=rows) if name == "execute" else self
                return call
        return SimpleNamespace(table=lambda t: Q())
    held = {"kind": "concierge_refused", "detail": {"reason": "booking_wording", "hold": "BK-1015: held"}}
    ctx = lambda rows: TenantCtx(tenant_id="t1", client=client(rows))
    assert concierge_draft._recent_hold(ctx([held]), ANN) == "BK-1015: held"
    assert concierge_draft._recent_hold(ctx([{"kind": "drafted", "detail": {}}, held]), ANN) is None
    assert concierge_draft._recent_hold(ctx([]), ANN) is None
    assert concierge_draft._recent_hold(TenantCtx(tenant_id="t1", client=object()), ANN) is None


# ── typed: the draft card becomes a confirm-first card ───────────────────────
def test_typed_draft_for_a_held_booking_becomes_a_confirm_card(monkeypatch):
    _diary(monkeypatch, [BK])
    monkeypatch.setattr(guard, "check", lambda t, c, text, client=None: (
        guard.violation(text, [BK], business_today=date(2026, 9, 30)), [BK]))
    monkeypatch.setattr(guard.db, "active_modules", lambda t: {"scheduling"})
    action = {"kind": "draft_reply", "contact": ANN, "draft_text": BAD}
    assert asyncio.run(guard.repair_action(action, "t1"))
    assert action["kind"] == "confirm_booking" and action["booking_id"] == "BK-1015"
    assert action["draft_text"] == BAD and action["contact"] == ANN     # the email rides the card
    assert action["hold_code"] == "firm_on_proposed"
    reply = guard.repair_reply(action)
    assert "only held" in reply and "Shall I confirm it and queue the email" in reply
    assert "pencilled" not in reply


def test_typed_bulk_items_are_converted_one_by_one(monkeypatch):
    _diary(monkeypatch, [BK])
    monkeypatch.setattr(guard, "check", lambda t, c, text, client=None: (
        guard.violation(text, [BK], business_today=date(2026, 9, 30)), [BK]))
    monkeypatch.setattr(guard.db, "active_modules", lambda t: {"scheduling"})
    action = {"kind": "bulk", "items": [
        {"kind": "draft_reply", "contact": ANN, "draft_text": BAD},
        {"kind": "add_kb_entry", "title": "x"}]}
    assert asyncio.run(guard.repair_action(action, "t1"))
    assert [i["kind"] for i in action["items"]] == ["confirm_booking", "add_kb_entry"]


def test_typed_without_a_diary_falls_back_to_the_rewrite(monkeypatch):
    calls = []

    async def compose(cfg, purpose, facts, **k):
        calls.append(purpose)
        return SimpleNamespace(text=PENCILLED, model="m", usage={})
    _diary(monkeypatch, [BK])
    monkeypatch.setattr(agent, "compose_outbound", compose)
    monkeypatch.setattr(guard.db, "active_modules", lambda t: set())
    action = {"kind": "draft_reply", "contact": ANN, "draft_text": BAD}
    asyncio.run(guard.repair_action(action, "t1"))
    assert action["kind"] == "draft_reply" and action["draft_text"] == PENCILLED and len(calls) == 1


def test_typed_past_date_is_held_as_move_first(monkeypatch):
    _diary(monkeypatch, [{**BK, "booking_date": "2026-09-28"}])
    monkeypatch.setattr(guard, "check", lambda t, c, text, client=None: (
        guard.violation(text, [{**BK, "booking_date": "2026-09-28"}],
                        business_today=date(2026, 9, 30)), []))
    monkeypatch.setattr(guard.db, "active_modules", lambda t: {"scheduling"})
    action = {"kind": "draft_reply", "contact": ANN, "draft_text": BAD}
    asyncio.run(guard.repair_action(action, "t1"))
    assert action["kind"] == "draft_reply" and action["hold_code"] == "past_date"
    assert "Shall I move it to a new date first" in guard.repair_reply(action)


# ── confirm_booking: stage + exec ────────────────────────────────────────────
def _stage_env(monkeypatch, booking):
    monkeypatch.setattr(actions.db, "get_booking", lambda *a, **k: booking)
    monkeypatch.setattr(actions.db, "get_booking_by_ref", lambda *a, **k: booking)
    monkeypatch.setattr(actions.db, "get_conversation", lambda *a, **k: [
        {"id": "log-1", "subject": "Emergency callout", "kind": "inbound"}])


def test_confirm_booking_is_registered_and_uses_the_diary_ability():
    assert "confirm_booking" in actions.allowed_kinds(False, True)
    assert "confirm_booking" not in actions.allowed_kinds(True, False)    # no diary, no confirm
    assert actions.ability_for("confirm_booking") == ("diary", "add")
    assert "confirm_booking" in actions._STAGERS and "confirm_booking" in actions._EXECUTORS
    # server-made only: the model-facing schema and prompt (pinned by the agnostic
    # render baselines) are unchanged - the model never writes a confirm action
    import json
    from app import concierge
    assert "confirm_booking" not in concierge.BUSINESS_ACTION_KINDS
    assert "confirm_booking" not in json.dumps(concierge.BUSINESS_SCHEMA)


def test_stage_refuses_what_cannot_be_confirmed(monkeypatch):
    for status, word in [("confirmed", "already confirmed"), ("offered", "only offered"),
                         ("cancelled", "isn't a held booking")]:
        _stage_env(monkeypatch, {**BK, "status": status})
        with pytest.raises(actions.ActionError) as e:
            actions._stage_confirm_booking("t1", None, {"booking_id": "BK-1015"})
        assert word in str(e.value)
    _stage_env(monkeypatch, {**BK, "booking_date": "2026-09-28"})
    with pytest.raises(actions.ActionError) as e:
        actions._stage_confirm_booking("t1", None, {"booking_id": "BK-1015"})
    assert "has passed" in str(e.value) and "moving" in str(e.value)
    _stage_env(monkeypatch, None)
    with pytest.raises(actions.ActionError):
        actions._stage_confirm_booking("t1", None, {"booking_id": "BK-0000"})


def test_stage_builds_one_card_for_confirm_and_email(monkeypatch):
    _stage_env(monkeypatch, BK)
    summary, payload, details, draft = actions._stage_confirm_booking(
        "t1", None, {"booking_id": "BK-1015", "contact": ANN, "draft_text": BAD})
    assert summary.startswith("Confirm BK-1015") and f"and email {ANN}" in summary
    assert payload["then"]["log_id"] == "log-1" and payload["draft_text"] == BAD and draft == BAD
    assert any("no email is queued" in d for d in details)
    # no email: a plain confirm card
    summary, payload, _, draft = actions._stage_confirm_booking("t1", None, {"booking_id": "BK-1015"})
    assert payload["then"] is None and draft is None and "and email" not in summary


def _exec_env(monkeypatch, *, status="proposed", confirm_ok=True, reason="full"):
    seen = {"confirm": [], "draft_exec": [], "voice": []}
    monkeypatch.setattr(actions.db, "get_booking", lambda *a, **k: {**BK, "status": status})

    async def confirm(tenant, booking_id, **k):
        seen["confirm"].append((booking_id, k.get("via")))
        return {"ok": True} if confirm_ok else {"ok": False, "reason": reason}
    monkeypatch.setattr(booking_confirm, "confirm", confirm)

    async def draft_exec(tenant, client, payload, edited):
        seen["draft_exec"].append(payload)
        return {"summary": "x", "link": {"label": "Review the draft", "href": "/t"}}
    monkeypatch.setattr(actions, "_exec_draft_reply", draft_exec)
    return seen


def test_exec_confirms_then_queues_the_typed_draft(monkeypatch):
    seen = _exec_env(monkeypatch)
    monkeypatch.setattr(guard, "check", lambda *a, **k: ("", []))   # confirmed: the claim now holds
    payload = {"booking_id": "b-1015", "booking_ref": "BK-1015", "draft_text": BAD,
               "then": {"contact": ANN, "log_id": "log-1", "subject": "S", "instruction": ""}}
    out = asyncio.run(actions._exec_confirm_booking("t1", None, payload, None))
    assert seen["confirm"] == [("b-1015", "by the Concierge")]
    assert seen["draft_exec"][0]["draft_text"] == BAD and seen["draft_exec"][0]["log_id"] == "log-1"
    assert "is confirmed in the diary" in out["summary"] and "approval queue" in out["summary"]


def test_a_failed_confirm_queues_no_email(monkeypatch):
    seen = _exec_env(monkeypatch, confirm_ok=False)
    payload = {"booking_id": "b-1015", "booking_ref": "BK-1015", "draft_text": BAD,
               "then": {"contact": ANN, "log_id": "log-1", "subject": "S", "instruction": ""}}
    with pytest.raises(actions.ActionError) as e:
        asyncio.run(actions._exec_confirm_booking("t1", None, payload, None))
    assert "no email was queued" in str(e.value) and not seen["draft_exec"]


def test_a_draft_that_still_cannot_go_after_the_confirm_is_not_queued(monkeypatch):
    seen = _exec_env(monkeypatch)
    monkeypatch.setattr(guard, "check", lambda *a, **k: ("BK-9: still not backed", []))
    payload = {"booking_id": "b-1015", "booking_ref": "BK-1015", "draft_text": BAD,
               "then": {"contact": ANN, "log_id": "log-1", "subject": "S", "instruction": ""}}
    out = asyncio.run(actions._exec_confirm_booking("t1", None, payload, None))
    assert "is confirmed" in out["summary"] and "wasn't queued" in out["summary"]
    assert not seen["draft_exec"]


def test_exec_then_drafts_a_new_email_with_the_owners_words(monkeypatch):
    seen = _exec_env(monkeypatch)

    async def draft(ctx, args, *, member, **k):
        seen["voice"].append((ctx.tenant_id, args))
        return {"ok": True, "status": "pending"}, {}
    monkeypatch.setattr(concierge_draft, "draft", draft)
    payload = {"booking_id": "b-1015", "booking_ref": "BK-1015",
               "then": {"contact": ANN, "instruction": "Tell Ann she is booked in"}}
    out = asyncio.run(actions._exec_confirm_booking("t1", None, payload, None))
    assert seen["voice"] == [("t1", {"contact": ANN, "mode": "new",
                                     "instruction": "Tell Ann she is booked in"})]
    assert "drafted and waiting for your approval" in out["summary"]


def test_exec_refuses_a_booking_that_changed_since_the_card(monkeypatch):
    seen = _exec_env(monkeypatch, status="cancelled")
    with pytest.raises(actions.ActionError) as e:
        asyncio.run(actions._exec_confirm_booking("t1", None, {"booking_id": "b", "booking_ref": "BK-1015"}, None))
    assert "nothing was confirmed" in str(e.value) and not seen["confirm"]


# ── the shared confirm function (Diary + Concierge) ──────────────────────────
def test_shared_confirm_does_the_diary_steps_once_and_reports_a_full_slot(monkeypatch):
    import app.job_sheets as job_sheets
    done = {"status": [], "soon": [], "push": [], "events": [], "crm": []}
    monkeypatch.setattr(booking_confirm.db, "get_booking", lambda *a, **k: {**BK, "email_log_id": "e1"})
    monkeypatch.setattr(booking_confirm.db, "set_booking_status",
                        lambda *a, **k: done["status"].append(a[:2]))
    monkeypatch.setattr(job_sheets, "soon", lambda *a: done["soon"].append(a))
    monkeypatch.setattr(booking_confirm.db, "log_agent_event", lambda *a, **k: done["events"].append(a[2]))
    monkeypatch.setattr(booking_confirm.events, "publish", lambda *a, **k: None)

    async def push(*a, **k):
        done["push"].append(k)
    monkeypatch.setattr(booking_confirm.calendar_sync, "push_bookings", push)
    monkeypatch.setattr(booking_confirm.crm, "note_booking_activity", lambda *a: done["crm"].append(a))

    async def resolve(*a, **k):
        return None
    monkeypatch.setattr(booking_confirm.crm_intel, "resolve_from_action", resolve)
    out = asyncio.run(booking_confirm.confirm("t1", "b-1015", via="by the Concierge"))
    assert out["ok"] and done["status"] == [("b-1015", "confirmed")]
    assert len(done["soon"]) == 1 and len(done["push"]) == 1 and len(done["crm"]) == 1
    assert done["events"] and done["events"][0].endswith("by the Concierge")

    from app import booking_errors
    monkeypatch.setattr(booking_errors, "capacity_conflict", lambda exc: True)

    def full(*a, **k):
        raise RuntimeError("full")
    monkeypatch.setattr(booking_confirm.db, "set_booking_status", full)
    done["push"].clear()
    out = asyncio.run(booking_confirm.confirm("t1", "b-1015"))
    assert out == {"ok": False, "reason": "full", "booking": {**BK, "email_log_id": "e1"}} and not done["push"]


def test_the_diary_button_and_the_concierge_share_one_confirm():
    src = open("src/app/dashboard.py", encoding="utf-8").read()
    body = src[src.index("async def diary_set_status("):][:3000]
    assert "booking_confirm.confirm(ctx.tenant_id, booking_id, client=ctx.client)" in body
    assert "push_bookings" not in body and "note_booking_activity" not in body   # no second copy
    act = open("src/app/concierge_actions.py", encoding="utf-8").read()
    assert "booking_confirm.confirm(tenant_id, booking[\"id\"], client=client" in act
