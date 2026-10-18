"""A redraft of an already-sent reply is text-only (live 2026-09-04, hot tub
run: the fix-a-draft redraft re-held the confirmed visit as BK-1005).

Run: python -m pytest tests/test_redraft_sent_text_only.py -q
"""
import asyncio
from types import SimpleNamespace

from app import pipeline


def test_the_flag_reads_the_row_status():
    assert pipeline.redraft_is_text_only({"status": "sent"})
    assert pipeline.redraft_is_text_only({"status": "SENT"})
    assert not pipeline.redraft_is_text_only({"status": "pending"})
    assert not pipeline.redraft_is_text_only(None)


def _env(monkeypatch, status):
    seen = {}
    entry = {"id": "log1", "from_address": "kris@x.co", "subject": "s", "kind": "inbound", "body_in": "b",
             "status": status, "mailbox_id": None, "thread_id": "t"}
    monkeypatch.setattr(pipeline.db, "get_email_log_entry", lambda log_id, t, client=None: entry)
    monkeypatch.setattr(pipeline.db, "effective_config", lambda t, m, client=None: {})
    monkeypatch.setattr(pipeline.db, "get_tenant", lambda t, client=None: {"industry": "trades"})
    monkeypatch.setattr(pipeline.db, "get_scheduling_config", lambda *a, **k: {})
    monkeypatch.setattr(pipeline.db, "get_conversation", lambda *a, **k: [])
    monkeypatch.setattr(pipeline.db, "active_modules", lambda t, client=None: {"scheduling"})
    monkeypatch.setattr(pipeline.db, "resolve_holds_for_log",
                        lambda *a, **k: seen.setdefault("released", True))
    monkeypatch.setattr(pipeline.db, "bookings_for_contact", lambda *a, **k: [{"booking_ref": "BK-1001"}])
    monkeypatch.setattr(pipeline.db, "update_email_log_draft", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_merge_portal_history", lambda t, f, h, client=None: h)
    monkeypatch.setattr(pipeline, "_party_context", lambda *a, **k: "")
    monkeypatch.setattr(pipeline, "_merge_handoff_usage", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_unbacked_refs", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "_draft_sources", lambda r: [])
    monkeypatch.setattr(pipeline, "_scheduling_setup",
                        lambda *a, **k: (["tool"], "exec", {"bookings": []}, "diary",
                                         {"scheduling": True, "transport": False}))
    monkeypatch.setattr(pipeline.kb, "retrieve", _async([]))
    monkeypatch.setattr(pipeline.kb, "render_for_prompt", lambda r: "")
    monkeypatch.setattr(pipeline.kb, "is_configured", lambda: False)
    monkeypatch.setattr(pipeline.events, "drafting_step", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.agent, "escalation_match", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.agent, "strip_quoted", lambda b: b)
    monkeypatch.setattr(pipeline.textfmt, "to_plain_text", lambda t: t)

    async def draft_reply(cfg, email, **kw):
        seen["tools"] = kw.get("tools")
        seen["executor"] = kw.get("tool_executor")
        seen["scheduling"] = kw.get("scheduling")
        seen["existing"] = kw.get("existing_bookings")
        return SimpleNamespace(text="reworded", missing=None, deferred=False,
                               thread_state="booked", request_summary="", action_summary="",
                               contact_name=None, contact_company=None, usage=None,
                               searched_for=[], booking_details=[], category_reason=None,
                               reasoning=None, request_id=None, category="draft_ready",
                               sentiment=None, promises=[], extracted=None, parties=[],
                               dependants=None, usage_meta=None, model="m", scheduling_requested=False,
                               scheduling_gap=False, held=False, offered=False, cancel_requested=False,
                               reschedule_requested=False, stock=None, transport=None)

    monkeypatch.setattr(pipeline.agent, "draft_reply", draft_reply)
    return seen


def _async(value):
    async def f(*a, **k):
        return value
    return f


def test_a_sent_reply_redrafts_without_the_diary(monkeypatch):
    seen = _env(monkeypatch, "sent")
    out = asyncio.run(pipeline.redraft_email_log("t1", "log1"))
    assert out == "reworded"
    assert seen["tools"] is None and seen["executor"] is None
    assert seen["scheduling"] is False
    assert "released" not in seen                       # nothing to release, nothing touched
    assert seen["existing"] == [{"booking_ref": "BK-1001"}]   # the text still knows the visit


def test_a_pending_reply_keeps_the_diary(monkeypatch):
    seen = _env(monkeypatch, "pending")
    asyncio.run(pipeline.redraft_email_log("t1", "log1"))
    assert seen["tools"] == ["tool"] and seen["scheduling"] is True
    assert seen["released"] is True


def test_an_empty_redraft_is_filed_as_failed_not_as_a_blank_draft(monkeypatch):
    """Run #8: the redraft's structured output came back as placeholders, the
    agent returned "", and the row was stored as an ordinary draft_ready row
    with an empty reply and no reason. The redraft now files it as the inbound
    path does: needs_attention, needs_reply, the 'Draft failed' summary and a
    reason the thread page shows."""
    _env(monkeypatch, "pending")
    saved = {}
    monkeypatch.setattr(pipeline.db, "update_email_log_draft",
                        lambda log_id, draft, t, client=None, **kw: saved.update(kw, draft=draft))

    async def empty_draft(cfg, email, **kw):
        return SimpleNamespace(text="", missing=None, deferred=False, blocked=None,
                               thread_state="in_progress", request_summary="", action_summary="",
                               contact_name=None, contact_company=None, usage=None,
                               searched_for=[], booking_details=[], category_reason=None,
                               reasoning=None, request_id=None, category="draft_ready",
                               sentiment=None, promises=[], extracted=None, parties=[],
                               dependants=None, usage_meta=None, model="m", scheduling_requested=True,
                               scheduling_gap=False, held=False, offered=False, cancel_requested=False,
                               reschedule_requested=False, stock=None, transport=None)
    monkeypatch.setattr(pipeline.agent, "draft_reply", empty_draft)
    asyncio.run(pipeline.redraft_email_log("t1", "log1"))
    assert saved["draft"] == ""
    assert saved["thread_state"] == "needs_attention" and saved["category"] == "needs_reply"
    assert saved["action_summary"] == "Draft failed — write a reply or hit Redraft"
    assert "couldn't write this one" in saved["category_reason"]
