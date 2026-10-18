"""A quote auto-drafted from holds must not outlive them.

Round three, 1 Oct 2026 (F26): an email placed two holds and an auto-drafted
quote. The owner corrected the reply three times; each redraft cancelled the
holds and placed new ones at new prices, but the draft quote stayed, its lines
pointing at CANCELLED bookings at the old prices, and no quote existed for the
new holds.

Pins: releasing the holds withdraws the AI's own untouched draft quote (status
'cancelled', an agent event, its Today card settled); a sent quote, a hand-made
draft and a draft the owner edited are never touched; a redraft that re-holds
ends with exactly one live draft quote built from the NEW bookings; and with
quote_auto_draft off nothing is drafted or withdrawn.

Run: python -m pytest tests/test_stale_auto_quote.py -q
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app import db, pipeline, push, quote_autodraft

T = "11111111-1111-1111-1111-111111111111"
LOG = "log-1"


class _Q:
    def __init__(self, store, name):
        self.s, self.name = store, name
        self.f, self.payload, self.patch = [], None, None

    def select(self, *a, **k): return self
    def order(self, *a, **k): return self
    def limit(self, n): return self
    def eq(self, k, v): self.f.append(lambda r: r.get(k) == v); return self
    def neq(self, k, v): self.f.append(lambda r: r.get(k) != v); return self
    def in_(self, k, vs): self.f.append(lambda r: r.get(k) in vs); return self
    def is_(self, k, v): self.f.append(lambda r: r.get(k) is None); return self
    def ilike(self, k, v): self.f.append(lambda r: str(r.get(k) or "").lower() == v.lower()); return self
    def insert(self, p): self.payload = p; return self
    def update(self, p): self.patch = p; return self

    def execute(self):
        rows = self.s.setdefault(self.name, [])
        if self.payload is not None:
            new = self.payload if isinstance(self.payload, list) else [self.payload]
            out = []
            for p in new:
                p = dict(p)
                p.setdefault("id", f"{self.name}-{len(rows) + 1}")
                p.setdefault("status", "draft" if self.name == "quotes" else None)
                rows.append(p)
                out.append(p)
            return SimpleNamespace(data=out)
        hit = [r for r in rows if all(f(r) for f in self.f)]
        if self.patch is not None:
            for r in hit:
                r.update(self.patch)
        return SimpleNamespace(data=[dict(r) for r in hit])


class Client:
    def __init__(self):
        self.store = {"tenant_config": [{"tenant_id": T}]}

    def table(self, name): return _Q(self.store, name)


@pytest.fixture
def world(monkeypatch):
    c = Client()
    w = SimpleNamespace(c=c, events=[], pushes=[], notes=[], refs=iter(range(1001, 2000)))
    monkeypatch.setattr(db, "get_invoice_settings", lambda *a, **k: {})
    monkeypatch.setattr(db, "next_quote_ref", lambda *a, **k: f"QUO-{next(w.refs)}")
    monkeypatch.setattr(db, "deal_id_for_job", lambda *a, **k: None)
    monkeypatch.setattr(db, "open_deal_id_for_contact", lambda *a, **k: None)
    monkeypatch.setattr(db, "crm_contact_by_email", lambda *a, **k: None)
    monkeypatch.setattr(db, "list_job_parties", lambda *a, **k: [])
    monkeypatch.setattr(db, "open_quote_for_customer", lambda *a, **k: None)
    monkeypatch.setattr(db, "log_agent_event",
                        lambda t, kind, summary, **k: w.events.append((kind, summary, k)))

    def notify(tenant_id, **k):
        w.pushes.append(k)
        c.table("concierge_notifications").insert(
            {"tenant_id": tenant_id, "kind": k["kind"], "url": k["url"],
             "dismissed_at": None}).execute()
    monkeypatch.setattr(push, "notify", notify)
    return w


def _hold(w, n, ref, amount, status="proposed"):
    w.c.table("bookings").insert(
        {"id": f"bk{n}", "tenant_id": T, "email_log_id": LOG, "status": status,
         "booking_ref": ref, "job_type": "Service visit", "quoted_amount": amount}).execute()


def _draft(w, cfg=None):
    return quote_autodraft.maybe_draft_for_log(
        T, cfg if cfg is not None else {}, LOG, "pat@example.com", client=w.c)


def _quotes(w):
    return w.c.store.get("quotes", [])


def _live(w):
    return [q for q in _quotes(w) if q["status"] != "cancelled"]


def _release(w):
    return db.resolve_holds_for_log(LOG, "cancelled", T, client=w.c)


def test_releasing_the_holds_withdraws_the_auto_drafted_quote(world):
    """Round three, 1 Oct 2026 (F26): two holds, one auto-drafted quote, then released."""
    _hold(world, 1, "BK-1001", 95.0)
    _hold(world, 2, "BK-1002", 40.0)
    q = _draft(world)
    assert q and q["created_by"] == "ai" and q["email_log_id"] == LOG
    assert len(world.pushes) == 1
    _release(world)
    assert [x["status"] for x in _quotes(world)] == ["cancelled"]
    kinds = [e[0] for e in world.events]
    assert kinds == ["quote_drafted", "quote_withdrawn"]
    summary = world.events[1][1]
    assert q["quote_ref"] in summary and "released" in summary
    assert world.events[1][2]["email_log_id"] == LOG
    # its "Quote drafted" card is settled like any superseded card
    card = world.c.store["concierge_notifications"][0]
    assert card["url"] == f"/app/quotes/{q['id']}" and card["dismissed_at"]


def test_a_sent_quote_is_untouched(world):
    """Round three, 1 Oct 2026 (F26): a quote that has left draft is the owner's."""
    _hold(world, 1, "BK-1001", 95.0)
    q = _draft(world)
    _quotes(world)[0]["status"] = "sent"
    _release(world)
    assert _quotes(world)[0]["status"] == "sent"
    assert [e[0] for e in world.events] == ["quote_drafted"]
    assert not world.c.store["concierge_notifications"][0]["dismissed_at"]
    assert q["id"] == _quotes(world)[0]["id"]


def test_a_hand_made_draft_quote_is_untouched(world):
    """Round three, 1 Oct 2026 (F26): the owner's own draft for the same customer stays."""
    _hold(world, 1, "BK-1001", 95.0)
    world.c.table("quotes").insert(
        {"tenant_id": T, "quote_ref": "QUO-0900", "customer_email": "pat@example.com",
         "created_by": "owner", "status": "draft", "email_log_id": None}).execute()
    world.c.table("quote_lines").insert(
        {"tenant_id": T, "quote_id": "quotes-1", "description": "Service visit (BK-1001)",
         "amount": 95.0, "booking_id": "bk1"}).execute()
    _release(world)
    assert [q["status"] for q in _quotes(world)] == ["draft"]
    # a hand-made draft that IS linked to this email is still not the AI's
    _quotes(world)[0]["email_log_id"] = LOG
    _hold(world, 3, "BK-1003", 10.0)
    _release(world)
    assert [q["status"] for q in _quotes(world)] == ["draft"]
    assert not any(e[0] == "quote_withdrawn" for e in world.events)


def test_a_draft_the_owner_edited_is_untouched(world):
    """Round three, 1 Oct 2026 (F26): retyped, re-priced or extra lines are the owner's work."""
    for edit in ("price", "text", "extra", "intro"):
        w = world
        w.c.store.clear()
        w.c.store["tenant_config"] = [{"tenant_id": T}]
        w.events.clear()
        _hold(w, 1, "BK-1001", 95.0)
        q = _draft(w)
        lines = w.c.store["quote_lines"]
        if edit == "price":
            lines[0]["amount"] = 120.0
        elif edit == "text":
            lines[0]["description"] = "Full refit, as agreed"
        elif edit == "extra":
            lines.append({"tenant_id": T, "quote_id": q["id"], "description": "Skip hire",
                          "amount": 60.0, "booking_id": None})
        else:
            _quotes(w)[0]["intro"] = "Thanks for your time"
        _release(w)
        assert _quotes(w)[0]["status"] == "draft", edit
        assert not any(e[0] == "quote_withdrawn" for e in w.events), edit


def test_a_quote_naming_a_still_live_booking_is_untouched(world):
    """Round three, 1 Oct 2026 (F26): only quotes whose bookings were ALL released go."""
    _hold(world, 1, "BK-1001", 95.0)
    _hold(world, 2, "BK-1002", 40.0)
    _draft(world)
    world.c.store["bookings"][1]["status"] = "confirmed"      # settled in the diary
    _release(world)
    assert _quotes(world)[0]["status"] == "draft"


def test_a_redraft_that_re_holds_ends_with_one_draft_for_the_new_bookings(world):
    """Round three, 1 Oct 2026 (F26): three corrections, prices changing each time."""
    _hold(world, 1, "BK-1001", 95.0)
    _hold(world, 2, "BK-1002", 40.0)
    assert _draft(world)
    prices = [(80.0, 35.0), (70.0, 30.0), (65.0, 25.0)]
    for i, (a, b) in enumerate(prices):
        _release(world)                                       # the redraft's release
        n = 10 * (i + 1)
        _hold(world, n + 1, f"BK-{n + 1}", a)
        _hold(world, n + 2, f"BK-{n + 2}", b)
        assert _draft(world)                                  # the redraft re-runs it
    live = _live(world)
    assert len(live) == 1 and live[0]["status"] == "draft"
    lines = [l for l in world.c.store["quote_lines"] if l["quote_id"] == live[0]["id"]]
    assert sorted((l["booking_id"], l["amount"]) for l in lines) == [
        ("bk31", 65.0), ("bk32", 25.0)]
    assert all("BK-3" in l["description"] for l in lines)
    assert live[0]["subtotal"] == 90.0
    assert len([q for q in _quotes(world) if q["status"] == "cancelled"]) == 3


def test_a_redraft_does_not_stack_over_a_quote_the_owner_kept(world):
    """Round three, 1 Oct 2026 (F26): an edited draft survives, so no second quote."""
    _hold(world, 1, "BK-1001", 95.0)
    _draft(world)
    world.c.store["quote_lines"][0]["amount"] = 120.0
    _release(world)
    _hold(world, 2, "BK-1002", 80.0)
    assert _draft(world) is None
    assert len(_quotes(world)) == 1


def test_with_the_setting_off_nothing_is_drafted_or_withdrawn(world):
    """Round three, 1 Oct 2026 (F26): quote_auto_draft off is respected both ways."""
    _hold(world, 1, "BK-1001", 95.0)
    assert _draft(world, {"quote_auto_draft": "off"}) is None
    assert _quotes(world) == []
    q = _draft(world)                                         # drafted while it was on
    assert q
    world.c.store["tenant_config"][0]["quote_auto_draft"] = "off"
    _release(world)
    assert _quotes(world)[0]["status"] == "draft"
    assert not any(e[0] == "quote_withdrawn" for e in world.events)


def test_releasing_holds_with_no_quote_is_a_no_op(world):
    _hold(world, 1, "BK-1001", 95.0)
    assert _release(world) == 1
    assert _quotes(world) == [] and world.events == []


# ── the redraft itself re-runs the auto-draft ─────────────────────────────────
def _redraft_env(monkeypatch, entry, calls):
    from types import SimpleNamespace as NS
    for name, value in (("get_email_log_entry", entry), ("effective_config", {"quote_auto_draft": "propose"}),
                        ("get_tenant", {"industry": "x"}), ("get_conversation", []),
                        ("active_modules", set()), ("get_scheduling_config", {}),
                        ("bookings_for_contact", []), ("crm_list_field_definitions", [])):
        monkeypatch.setattr(pipeline.db, name, lambda *a, _v=value, **k: _v)
    monkeypatch.setattr(pipeline.db, "module_active", lambda *a, **k: False)
    monkeypatch.setattr(pipeline.db, "update_email_log_draft", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.db, "resolve_holds_for_log", lambda *a, **k: 0)
    monkeypatch.setattr(pipeline.db, "link_bookings_to_log", lambda *a, **k: None)

    async def kb_retrieve(*a, **k):
        return []

    async def noop(*a, **k):
        return None
    monkeypatch.setattr(pipeline.kb, "retrieve", kb_retrieve)
    monkeypatch.setattr(pipeline.kb, "render_for_prompt", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.kb, "is_configured", lambda: False)
    monkeypatch.setattr(pipeline.agent, "escalation_match", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_merge_portal_history", lambda t, a, h, client=None: h)
    monkeypatch.setattr(pipeline, "_scheduling_setup",
                        lambda *a, **k: (None, None, None, None,
                                         {"scheduling": False, "transport": False,
                                          "field_service": False, "storefront": False,
                                          "intake": "", "gather_intake": False}))
    monkeypatch.setattr(pipeline, "_log_agent_activity", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_trace_kb_retrieval", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_read_parties", noop)
    monkeypatch.setattr(pipeline, "_ground_extract", noop)
    monkeypatch.setattr(pipeline.trace, "emit", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.events, "drafting_step", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.usage, "record_anthropic", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.crm, "ingest_email_log_row", lambda *a, **k: {})

    async def fake_draft(cfg, email, **kw):
        return NS(text="Hello", usage={}, model="m", deferred=False, missing="",
                  blocked="", reasoning="", scheduling_requested=False,
                  thread_state="in_progress", request_summary="s", action_summary="a",
                  searched_for="", contact_name=None, contact_company=None,
                  booking_details=(), category="needs_reply", category_reason="",
                  request_id=None)
    monkeypatch.setattr(pipeline.agent, "draft_reply", fake_draft)
    monkeypatch.setattr(pipeline.quote_autodraft, "maybe_draft_for_log",
                        lambda *a, **k: calls.append((a, k)))


def _entry(**over):
    return {"id": LOG, "from_address": "pat@example.com", "from_name": "Pat",
            "subject": "Visit", "kind": "inbound", "body_in": "hi", "mailbox_id": None,
            "thread_id": None, "status": "pending", **over}


def test_a_redraft_re_runs_the_auto_draft_under_the_same_config(monkeypatch):
    """Round three, 1 Oct 2026 (F26): the redraft drafts the quote for its new holds."""
    calls = []
    _redraft_env(monkeypatch, _entry(), calls)
    assert asyncio.run(pipeline.redraft_email_log(T, LOG)) == "Hello"
    assert len(calls) == 1
    args, _ = calls[0]
    assert args[0] == T and args[1]["quote_auto_draft"] == "propose"
    assert args[2] == LOG and args[3] == "pat@example.com"


def test_a_redraft_of_an_already_sent_reply_drafts_no_quote(monkeypatch):
    """Round three, 1 Oct 2026 (F26): a sent reply re-books nothing, so re-quotes nothing."""
    calls = []
    _redraft_env(monkeypatch, _entry(status="sent"), calls)
    asyncio.run(pipeline.redraft_email_log(T, LOG))
    assert calls == []
