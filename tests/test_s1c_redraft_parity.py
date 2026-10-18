"""S1c: real callers, frozen I/O and E2/E7/E10 side-effect checks."""
import asyncio
from copy import deepcopy
from datetime import date, datetime, timezone

import pytest

from app import agent, db, pipeline, scheduling, wholesale
from tests.agnostic_harness import EMAIL, NOW, World, fixtures
from tests.test_model_outage import _credit_error


@pytest.mark.parametrize("path", ["draft", "redraft"])
@pytest.mark.parametrize("read", ["effective_config", "get_scheduling_config", "active_modules"])
def test_unreadable_settings_hold_without_model_or_booking_mutation(monkeypatch, path, read):
    world = World(monkeypatch, fixtures()["exact_times"])
    def broken(*a, **k):
        raise RuntimeError("private database diagnostic")
    monkeypatch.setattr(db, read, broken)
    world.run(path)
    assert not world.io.calls
    saved = next(w for w in world.writes if w["name"] in {"insert_email_log", "update_email_log_draft"})
    assert saved["args"][4 if path == "draft" else 1] == ""
    fields = saved["kwargs"]
    assert fields["thread_state"] == "needs_attention" and fields["category"] == "needs_reply"
    assert "couldn't check" in fields["category_reason"] and "please review" in fields["action_summary"]
    assert "private database" not in str(saved)
    assert not any(w["name"] in {"resolve_holds_for_log", "set_booking_status", "link_bookings_to_log"}
                   for w in world.writes)
    assert not pipeline._drafts_in_flight


@pytest.mark.parametrize("path", ["draft", "redraft"])
@pytest.mark.parametrize("channel", ["email", "sms"])
def test_shared_customer_context_channel_mailbox_shop_credit_and_proof(monkeypatch, path, channel):
    spec = deepcopy(fixtures()["mixed_office_shop"])
    spec["modules"] += ["wholesale"]
    world = World(monkeypatch, spec)
    sender = EMAIL["from_address"] if channel == "email" else "+447700900123"
    email = {**EMAIL, "from_address": sender, "body": "Please supply Item A"}
    entry = {"id": "log-a", **email, "kind": "inbound", "body_in": email["body"], "status": "pending",
             "channel": channel, "mailbox_id": "mailbox-b"}
    monkeypatch.setattr(db, "get_email_log_entry", lambda *a, **k: entry)
    monkeypatch.setattr(db, "get_mailbox_config", lambda mid, **k: {"sign_off": "Mailbox B voice"})
    monkeypatch.setattr(db, "crm_contact_by_email", lambda *a, **k: {"id": "contact-a"})
    monkeypatch.setattr(db, "crm_contact_by_phone", lambda *a, **k: {"id": "contact-a"})
    monkeypatch.setattr(db, "shop_list_products", lambda *a, **k: [{"name": "Item A"}])
    shop_reads = []
    monkeypatch.setattr(db, "shop_storefront_context", lambda tid, cid, mentioned:
        shop_reads.append((tid, cid, mentioned)) or "Item A: 3 on hand")
    monkeypatch.setattr(wholesale, "inbound_credit_context", lambda *a, **k: "HARD RULE: credit limit reached")
    monkeypatch.setattr(db, "list_documents", lambda *a, **k: [
        {"filename": "evidence.pdf", "title": "Verified evidence", "id": "document-a"}])
    kb_reads = []
    async def retrieve(tid, query, **kw):
        kb_reads.append((tid, kw))
        return [{"content": "Owner supplied fact", "source": "Owner facts", "id": "proof-a", "similarity": 0.9}]
    monkeypatch.setattr(pipeline.kb, "retrieve", retrieve)
    monkeypatch.setattr(pipeline.kb, "is_configured", lambda: True)
    seen = []
    original = agent.draft_reply
    async def capture(cfg, email, **kw):
        seen.append((cfg, kw))
        return await original(cfg, email, **kw)
    monkeypatch.setattr(agent, "draft_reply", capture)
    if path == "draft":
        asyncio.run(pipeline.draft_for_email("tenant-a", email, mailbox_id="mailbox-b", channel=channel))
    else:
        asyncio.run(pipeline.redraft_email_log("tenant-a", "log-a"))
    cfg, kw = seen[0]
    assert cfg["sign_off"] == "Mailbox B voice"
    assert kw["channel"] == channel and kw["storefront_context"] == "Item A: 3 on hand"
    assert "HARD RULE: credit limit reached" in kw["crm_context"]
    assert "evidence.pdf" in kw["party_context"]
    assert shop_reads == [("tenant-a", "contact-a", ["Item A"])]
    assert kb_reads == [("tenant-a", {"mailbox_id": "mailbox-b", "whole_if_small": True})]
    saved = next(w for w in world.writes if w["name"] in {"insert_email_log", "update_email_log_draft"})
    assert saved["kwargs"]["draft_sources"][0]["chunk_id"] == "proof-a"
    assert world.io.calls


@pytest.mark.parametrize("path", ["draft", "redraft"])
def test_unaffected_business_does_not_receive_shop_or_credit_context(monkeypatch, path):
    world = World(monkeypatch, fixtures()["neutral_unseen"])
    def forbidden(*a, **k):
        pytest.fail("another business's module must not be read")
    monkeypatch.setattr(db, "shop_storefront_context", forbidden)
    monkeypatch.setattr(wholesale, "inbound_credit_context", forbidden)
    world.run(path)
    assert world.io.calls
    assert "HARD RULE: credit" not in str(world.io.calls)


@pytest.mark.parametrize("status", ["pending", "sent"])
def test_fsm_releases_only_previous_unsent_proposal_with_real_resolver(monkeypatch, status):
    real_resolve = db.resolve_holds_for_log
    world = World(monkeypatch, fixtures()["fsm"])
    entry = {"id": "log-a", **EMAIL, "kind": "inbound", "body_in": EMAIL["body"], "status": status}
    monkeypatch.setattr(db, "get_email_log_entry", lambda *a, **k: entry)
    rows = [dict(id=ident, tenant_id=tenant, email_log_id=log_id, status=state)
            for ident, tenant, log_id, state in [
                ("old", "tenant-a", "log-a", "proposed"),
                ("confirmed", "tenant-a", "log-a", "confirmed"),
                ("other-log", "tenant-a", "log-b", "proposed"),
                ("other-tenant", "tenant-b", "log-a", "proposed")]]
    class Query:
        def table(self, name):
            assert name == "bookings"
            self.selected = list(rows)
            return self
        def update(self, values):
            self.values = values
            return self
        def eq(self, key, value):
            self.selected = [r for r in self.selected if r[key] == value]
            return self
        def in_(self, key, values):
            self.selected = [r for r in self.selected if r[key] in values]
            return self
        def execute(self):
            from types import SimpleNamespace
            for row in self.selected:
                row.update(self.values)
            return SimpleNamespace(data=self.selected)
    def resolve(*args, **kw):
        return real_resolve(*args, client=Query())
    monkeypatch.setattr(db, "resolve_holds_for_log", resolve)
    world.run("redraft")
    assert [r["status"] for r in rows] == ["cancelled" if status == "pending" else "proposed",
                                          "confirmed", "proposed", "proposed"]
    assert world.io.calls
    names = {t["name"] for t in world.io.calls[0].get("tools", [])}
    assert ("ask_field_service" in names) is (status == "pending")


@pytest.mark.parametrize("path", ["draft", "redraft"])
def test_outage_keeps_visible_empty_owner_hold_and_reason(monkeypatch, path):
    world = World(monkeypatch, fixtures()["email_only"])
    async def outage(*a, **k):
        raise _credit_error()
    monkeypatch.setattr(agent, "draft_reply", outage)
    world.run(path)
    saved = next(w for w in world.writes if w["name"] in {"insert_email_log", "update_email_log_draft"})
    assert saved["args"][4 if path == "draft" else 1] == ""
    assert saved["kwargs"]["category"] == "needs_reply"
    assert saved["kwargs"]["thread_state"] == "needs_attention"
    assert saved["kwargs"]["category_reason"] == pipeline.MODEL_OUTAGE_REASON


@pytest.mark.parametrize("when,day,word", [
    (datetime(2040, 5, 1, 12, tzinfo=timezone.utc), date(2040, 5, 1), "today"),
    (datetime(2040, 4, 30, 22, tzinfo=timezone.utc), date(2040, 5, 1), "then"),
    (datetime(2040, 4, 30, 23, 30, tzinfo=timezone.utc), date(2040, 5, 1), "today"),
])
def test_notice_reason_uses_one_injected_local_clock(when, day, word):
    cfg = {**fixtures()["exact_times"]["cfg"], "min_notice_hours": 24, "working_days": "mon,tue,wed,thu,fri,sat,sun"}
    out = scheduling.why_none(cfg, day, day, now=when)
    assert out["why_none"] == "notice"
    assert f"them {word} and offer" in out["why_none_note"]


def test_notice_default_reads_real_clock_once(monkeypatch):
    reads = []
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            reads.append(tz)
            return NOW
    monkeypatch.setattr(scheduling, "datetime", Clock)
    cfg = {**fixtures()["exact_times"]["cfg"], "min_notice_hours": 24}
    out = scheduling.why_none(cfg, NOW.date(), NOW.date())
    assert "today" in out["why_none_note"] and reads == [scheduling.DIARY_TZ]


@pytest.mark.parametrize("path", ["draft", "redraft"])
def test_executor_failed_read_overrides_real_immutable_draft_result(monkeypatch, path):
    world = World(monkeypatch, fixtures()["exact_times"])
    original = agent.draft_reply
    async def execute_then_draft(cfg, email, **kw):
        def broken(*a, **k):
            raise RuntimeError("settings unavailable after assembly")
        monkeypatch.setattr(db, "get_scheduling_config", broken)
        out = kw["tool_executor"]("hold_booking", {"date": "2026-09-29", "window": "10:00"})
        assert out["error"] == "could_not_verify"
        result = await original(cfg, email, **kw)
        assert isinstance(result, agent.DraftResult)
        return result
    monkeypatch.setattr(agent, "draft_reply", execute_then_draft)
    world.run(path)
    saved = next(w for w in world.writes if w["name"] in {"insert_email_log", "update_email_log_draft"})
    assert saved["args"][4 if path == "draft" else 1] == ""
    assert saved["kwargs"]["thread_state"] == "needs_attention"
    assert "Could not verify booking settings" in saved["kwargs"]["category_reason"]
    assert not any(w["name"] == "link_bookings_to_log" for w in world.writes)


def test_sent_redraft_never_assembles_action_tools(monkeypatch):
    world = World(monkeypatch, fixtures()["mixed_office_shop"])
    entry = {"id": "log-a", **EMAIL, "kind": "inbound", "body_in": EMAIL["body"], "status": "sent"}
    monkeypatch.setattr(db, "get_email_log_entry", lambda *a, **k: entry)
    def forbidden(*a, **k):
        pytest.fail("a sent redraft must not build write-capable executors")
    monkeypatch.setattr(pipeline, "_scheduling_setup", forbidden)
    monkeypatch.setattr(db, "crm_get_or_create_contact", forbidden)
    world.run("redraft")
    assert world.io.calls and not world.io.calls[0].get("tools")
    assert not any(w["name"] == "resolve_holds_for_log" for w in world.writes)


def test_redraft_programming_error_is_not_labelled_model_outage(monkeypatch):
    world = World(monkeypatch, fixtures()["email_only"])
    async def broken(*a, **k):
        raise KeyError("programming error")
    monkeypatch.setattr(agent, "draft_reply", broken)
    with pytest.raises(KeyError, match="programming error"):
        world.run("redraft")
