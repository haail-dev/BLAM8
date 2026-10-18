"""Scheduling module — pipeline + approval wiring (build step 4).

What this pins (D4 — one approval drives both):
  - tools are injected iff the tenant's scheduling module is active
    (db.module_active — a data gate, never a code path)
  - holds placed while drafting get bookings.email_log_id stamped once the
    email_log row exists; a DUPLICATE webhook delivery (insert deduped to the
    existing row) releases its holds instead — capacity is never double-held
  - auto-send success confirms the linked holds; a send failure leaves them
    proposed (the row stays pending, so approve/skip decide later)
  - re-draft releases the old proposed holds BEFORE drafting, so the agent can
    place a fresh one, then links the new holds
  - db helpers only ever move PROPOSED holds (a Diary-confirmed booking is
    never touched by approve/skip), always tenant-scoped

Run: python -m pytest tests/ -q
"""
from tests.booking_fixtures import carried_config
import asyncio
import inspect

import pytest

from app import agent, db, pipeline, scheduling


# ── db helpers: query shapes via a fake supabase client ──────────────────────
class _FakeQuery:
    def __init__(self):
        self.updates = []
        self.filters = []

    def table(self, name):
        self.filters.append(("table", name)); return self

    def update(self, payload):
        self.updates.append(payload); return self

    def eq(self, col, val):
        self.filters.append((col, val)); return self

    def in_(self, col, vals):
        self.filters.append((col, tuple(vals))); return self

    def execute(self):
        return type("Res", (), {"data": []})()


def test_resolve_holds_for_log_moves_only_proposed_tenant_scoped():
    fake = _FakeQuery()
    db.resolve_holds_for_log("log-1", "confirmed", "t1", client=fake)
    assert fake.updates[0]["status"] == "confirmed"
    assert ("email_log_id", "log-1") in fake.filters
    # Sending a draft must NOT confirm offered slots — the customer hasn't picked.
    assert ("status", ("proposed",)) in fake.filters
    assert ("tenant_id", "t1") in fake.filters


def test_resolve_holds_for_log_cancel_releases_offers_too():
    # Skip / re-draft releases the whole offer set along with any proposed hold —
    # the email that offered those slots never went out.
    fake = _FakeQuery()
    db.resolve_holds_for_log("log-1", "cancelled", "t1", client=fake)
    assert fake.updates[0]["status"] == "cancelled"
    assert ("status", ("proposed", "offered")) in fake.filters


def test_set_booking_status_is_tenant_scoped():
    fake = _FakeQuery()
    db.set_booking_status("b-1", "cancelled", "t1", client=fake)
    assert fake.updates[0]["status"] == "cancelled"
    assert ("id", "b-1") in fake.filters and ("tenant_id", "t1") in fake.filters


def test_link_bookings_to_log_scopes_and_noops_when_empty(monkeypatch):
    fake = _FakeQuery()
    monkeypatch.setattr(db, "db", lambda: fake)
    db.link_bookings_to_log(["b-1", "b-2"], "log-1", "t1")
    assert fake.updates[0]["email_log_id"] == "log-1"
    assert ("id", ("b-1", "b-2")) in fake.filters and ("tenant_id", "t1") in fake.filters

    before = len(fake.updates)
    db.link_bookings_to_log([], "log-1", "t1")
    assert len(fake.updates) == before  # no holds — no query


# ── pipeline wiring ───────────────────────────────────────────────────────────
class Env:
    """Patches every collaborator of draft_for_email/redraft and records calls.
    `module_on` lights the scheduling module (the OFFICE diary path); `fleet` lights
    TMS instead — in Phase 1 the base agent gets the ask_transport HAND-OFF tool and a
    stubbed TMS specialist (app.agents.tms.answer) places the hold via the fleet
    executor. Either way the hold linking is the same scheduling machinery (D5)."""

    def __init__(self, monkeypatch, module_on=True, created=True, auto_send=False,
                 send_fails=False, place_hold=True, fleet=False):
        self.calls = []
        self.state = {"bookings": []}
        self.draft_kwargs = {}
        self.executor_kwargs = {}
        rec = lambda name: (lambda *a, **k: self.calls.append((name, a)))

        async def fake_draft(cfg, email, knowledge=None, history=None, tools=None,
                             tool_executor=None, **kw):
            self.draft_kwargs = {"tools": tools, "executor": tool_executor, **kw}
            if tool_executor and place_hold:
                # Drive whichever tool this tenant exposes: a TMS tenant hands off via
                # ask_transport (async), a scheduling tenant books the office diary.
                if kw.get("transport"):
                    name, args = "ask_transport", {"request": "Friday?"}
                else:
                    name, args = "hold_booking", {"date": "x", "window": "day", "summary": "s"}
                out = tool_executor(name, args)
                if inspect.isawaitable(out):
                    out = await out
            return agent.DraftResult(text="Reply.", usage={"input_tokens": 1}, model="m")

        def fake_make_executor(tenant_id, contact=None, thread_id=None, **kw):
            self.calls.append(("make_executor", (tenant_id, contact, thread_id)))
            self.executor_kwargs = kw
            def execute(name, args):
                self.state["bookings"].append({"id": "b-1"})
                return {"held": True}
            return execute, self.state

        async def fake_tms_answer(request, *, executor, diary_shape=None, today=None, **kw):
            # The specialist places the real hold through the fleet executor (the same
            # machinery, stubbed here) and returns a structured result + usage.
            executor("hold_booking", {"date": "x", "summary": "s",
                                      "pallets": [{"type": "full", "qty": 1}]})
            return ({"status": "held", "date": "2026-06-19", "booking_ref": "BK-1",
                     "spaces": 1, "weight_kg": 0, "branch": "", "assumed": [],
                     "alternatives": [], "ask": "", "message": ""},
                    {"input_tokens": 2})
        monkeypatch.setattr("app.agents.tms.answer", fake_tms_answer)

        async def fake_send(*a, **k):
            self.calls.append(("send_reply", a))
            if send_fails:
                raise RuntimeError("smtp down")

        async def fake_retrieve(*a, **k):
            return []

        mods = ({"tms"} if fleet else {"scheduling"}) if (module_on or fleet) else set()
        monkeypatch.setattr(pipeline.agent, "draft_reply", fake_draft)
        monkeypatch.setattr(pipeline.scheduling, "make_executor", fake_make_executor)
        monkeypatch.setattr(pipeline.kb, "retrieve", fake_retrieve)
        monkeypatch.setattr(pipeline.kb, "render_for_prompt", lambda *a: None)
        monkeypatch.setattr(pipeline.usage, "record_anthropic", rec("usage"))
        monkeypatch.setattr(pipeline.mailbox, "send_reply", fake_send)
        monkeypatch.setattr(pipeline.db, "is_ignored", lambda *a, **k: False)
        # A scheduling tenant whose intake onboarding is DONE (configured) — the office
        # diary is unblocked. The migration grandfathers existing tenants the same way.
        monkeypatch.setattr(pipeline.db, "get_scheduling_config",
                            lambda *a, **k: carried_config({"intake_configured_at": "2026-01-01T00:00:00+00:00"}))
        monkeypatch.setattr(pipeline.db, "get_tenant_config", lambda *a, **k: {"auto_send": auto_send})
        monkeypatch.setattr(pipeline.db, "get_mailbox_config", lambda *a, **k: None)
        monkeypatch.setattr(pipeline.db, "own_mailbox_addresses", lambda *a, **k: {"me@biz.co"})
        monkeypatch.setattr(pipeline.db, "mailbox_self_addresses", lambda *a, **k: set())
        monkeypatch.setattr(pipeline.db, "bookings_for_contact", lambda *a, **k: [])
        # CRM (phase 1-3) side effects: no contact, no context, no extraction.
        monkeypatch.setattr(pipeline.db, "crm_contact_by_email", lambda *a, **k: None)
        monkeypatch.setattr(pipeline.crm, "ingest_email_log_row",
                            lambda *a, **k: {"activities": 0, "contact_id": None,
                                             "company_id": None})
        monkeypatch.setattr(pipeline.db, "get_tenant", lambda *a, **k: {"mailbox": "me@biz.co"})
        monkeypatch.setattr(pipeline.db, "get_conversation", lambda *a, **k: [])
        monkeypatch.setattr(pipeline.db, "active_modules", lambda *a, **k: set(mods))
        monkeypatch.setattr(pipeline.db, "list_vehicles", lambda *a, **k: [
            {"id": "v-1", "name": "Van", "capacity_spaces": 8, "active": True}])
        monkeypatch.setattr(pipeline.db, "list_branches", lambda *a, **k: [])
        monkeypatch.setattr(pipeline.db, "insert_email_log", lambda *a, **k: ("log-1", created))
        monkeypatch.setattr(pipeline.db, "mark_email_log_sent", rec("mark_sent"))
        monkeypatch.setattr(pipeline.db, "link_bookings_to_log", rec("link"))
        monkeypatch.setattr(pipeline.db, "set_booking_status", rec("cancel_one"))
        monkeypatch.setattr(pipeline.db, "resolve_holds_for_log", rec("resolve"))
        monkeypatch.setattr(pipeline.db, "get_email_log_entry", lambda *a, **k: {
            "id": "log-1", "from_address": "ops@acme.co.uk", "subject": "Quote",
            "kind": "inbound", "body_in": "Friday?", "thread_id": "th-9",
        })
        monkeypatch.setattr(pipeline.db, "update_email_log_draft", rec("update_draft"))

    def named(self, name):
        return [c for c in self.calls if c[0] == name]


EMAIL = {"from_address": "ops@acme.co.uk", "subject": "Quote", "body": "Friday?", "thread_id": "th-9"}


def test_module_on_injects_tools_and_links_holds(monkeypatch):
    env = Env(monkeypatch)
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1"))
    # The windows-mode freeze: the office scheduling tools, executor not in fleet
    # mode, windows diary line — a TMS rollout must never change this path.
    # …plus the cancel tool that rides with every diary tenant (run #8, 2026-09-05).
    assert env.draft_kwargs["tools"] == list(scheduling.SCHEDULING_TOOLS) + [scheduling.CANCEL_BOOKING_TOOL,
                                                                             scheduling.MOVE_BOOKING_TOOL]
    assert env.draft_kwargs["scheduling"] is True and env.draft_kwargs["transport"] is False
    assert env.executor_kwargs.get("fleet") in (None, False)   # office executor, windows mode
    assert "bookings at the same time" in env.draft_kwargs["diary_summary"]  # diary_line reaches the prompt
    assert env.named("make_executor")[0][1] == ("t1", "ops@acme.co.uk", "th-9")
    assert env.named("link")[0][1] == (["b-1"], "log-1", "t1")
    assert not env.named("cancel_one") and not env.named("resolve")


def test_module_off_means_no_tools_anywhere(monkeypatch):
    env = Env(monkeypatch, module_on=False)
    asyncio.run(pipeline.draft_for_email("t1", EMAIL))
    # on_step is the live ticker's callback (src/app/working.py), not a capability:
    # it rides every draft, module on or off, so it is checked for presence and
    # dropped before the exact-shape assertion that is this test's whole point.
    assert callable(env.draft_kwargs.pop("on_step", None))
    assert env.draft_kwargs == {"tools": None, "executor": None, "diary_summary": None,
                                "scheduling": False, "transport": False, "intake": "",
                                # Grounded booking: with the module off no grounding is
                                # built at all, so the "nothing to act on" case cannot
                                # arise and the flag rides through as its default.
                                "no_dates": False,
                                # When it's full: the default choice, i.e. today's rules.
                                "other_dates": "hold",
                                "gather_intake": False, "field_service": False,
                                "existing_bookings": [], "crm_context": "",
                                "images": None,     # no photos attached (src/app/photos.py)
                                "channel": "email", "storefront": False,
                                "storefront_context": "", "tenant_id": "t1",
                                "party_context": ""}
    assert not env.named("make_executor") and not env.named("link")


def test_tms_module_hands_off_and_links_the_specialist_hold(monkeypatch):
    """TMS Phase 1 (docs/modules/module-agent-handoff.md): the base agent gets the
    ask_transport HAND-OFF tool (never fleet tools); the specialist places the hold
    through the fleet executor (fleet+routing mode, the mailbox default-branch source —
    D4) and the pipeline links it exactly as the scheduling path does (D5)."""
    from app.agents import tms as tms_agent
    env = Env(monkeypatch, fleet=True)
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1",
                                         mailbox_id="mb-1"))
    assert env.draft_kwargs["tools"] == [tms_agent.ASK_TRANSPORT_TOOL]
    assert env.draft_kwargs["transport"] is True and env.draft_kwargs["scheduling"] is False
    assert env.executor_kwargs.get("fleet") is True
    assert env.executor_kwargs.get("routing") is True
    assert env.executor_kwargs.get("mailbox_id") == "mb-1"   # D4: branch default source
    assert env.named("link")[0][1] == (["b-1"], "log-1", "t1")


def test_duplicate_delivery_releases_its_holds(monkeypatch):
    env = Env(monkeypatch, created=False)
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1"))
    # The original draft's holds stand; the duplicate run's hold is released.
    assert env.named("cancel_one")[0][1] == ("b-1", "cancelled", "t1")
    assert not env.named("link")


def test_duplicate_delivery_keeps_the_customers_accepted_flip(monkeypatch):
    # Live 2026-08-31: push + poller double-processed one acceptance email. The
    # duplicate run FLIPPED the customer's offered slot (BK-1048) and then
    # cancelled it as its own leftover — the surviving reply told the customer
    # the slot was theirs while the diary held nothing. A flipped row is shared
    # state that predates the run: the duplicate must relink it to the
    # canonical log (so approve/skip still resolves it), and cancel only the
    # holds it actually created.
    env = Env(monkeypatch, created=False)

    def flip_executor(tenant_id, contact=None, thread_id=None, **kw):
        def execute(name, args):
            env.state["bookings"].append({"id": "b-flip", "accepted_flip": True})
            env.state["bookings"].append({"id": "b-new"})
            return {"held": True}
        return execute, env.state

    monkeypatch.setattr(pipeline.scheduling, "make_executor", flip_executor)
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1"))
    assert env.named("link")[0][1] == (["b-flip"], "log-1", "t1")
    assert env.named("cancel_one")[0][1] == ("b-new", "cancelled", "t1")
    assert len(env.named("cancel_one")) == 1


def test_unbacked_refs_ground_truth():
    # Backed = a hold this run placed/flipped, or a LIVE existing booking.
    # A cancelled row's ref and a ref from nowhere are ghosts.
    sched = {"bookings": [{"booking_ref": "BK-1"}]}
    existing = [{"booking_ref": "BK-2", "status": "offered"},
                {"booking_ref": "BK-3", "status": "cancelled"}]
    assert pipeline._unbacked_refs("BK-1 confirmed, BK-2 on offer", sched, existing) == []
    assert pipeline._unbacked_refs("your ref is BK-3, or BK-9", sched, existing) == ["BK-3", "BK-9"]
    assert pipeline._unbacked_refs("", sched, existing) == []
    assert pipeline._unbacked_refs("no refs here", None, None) == []


def test_ghost_ref_in_the_draft_flags_the_thread(monkeypatch):
    # Live 2026-08-31 (Kris: "we released the held slots but never booked the
    # requested slot — this could cause a double booking"): a reply claimed
    # "pencilled in... BK-1047" while the run placed NO hold and that ref was a
    # cancelled row quoted from thread history. Ground truth beats the model:
    # the row lands needs_attention with the reason naming the ghost ref.
    env = Env(monkeypatch, place_hold=False)
    captured = {}

    def fake_insert(*a, **k):
        captured.update(k)
        return ("log-1", True)

    async def ghost_draft(cfg, email, **kw):
        return agent.DraftResult(text="All booked — your reference is BK-777.",
                                 usage={"input_tokens": 1}, model="m")

    monkeypatch.setattr(pipeline.db, "insert_email_log", fake_insert)
    monkeypatch.setattr(pipeline.agent, "draft_reply", ghost_draft)
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1"))
    assert captured["thread_state"] == "needs_attention"
    assert captured["category"] == "needs_reply"
    assert "BK-777" in captured["category_reason"]
    assert "nothing is reserved" in captured["category_reason"]


def test_a_backed_ref_raises_no_flag(monkeypatch):
    env = Env(monkeypatch)
    captured = {}

    def fake_insert(*a, **k):
        captured.update(k)
        return ("log-1", True)

    def ref_executor(tenant_id, contact=None, thread_id=None, **kw):
        def execute(name, args):
            env.state["bookings"].append({"id": "b-1", "booking_ref": "BK-1"})
            return {"held": True}
        return execute, env.state

    async def honest_draft(cfg, email, tool_executor=None, **kw):
        if tool_executor:
            tool_executor("hold_booking", {"date": "x", "window": "day", "summary": "s"})
        return agent.DraftResult(text="Held for you — ref BK-1.",
                                 usage={"input_tokens": 1}, model="m")

    monkeypatch.setattr(pipeline.scheduling, "make_executor", ref_executor)
    monkeypatch.setattr(pipeline.db, "insert_email_log", fake_insert)
    monkeypatch.setattr(pipeline.agent, "draft_reply", honest_draft)
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1"))
    assert captured["thread_state"] == "booked"
    assert "nothing is reserved" not in (captured["category_reason"] or "")


def test_diary_rules_pin_the_ref_truth():
    assert "released or cancelled" in agent._WINDOWS_DIARY_RULES
    assert "NEVER repeat it" in agent._WINDOWS_DIARY_RULES


def test_the_accept_flip_wears_its_tag(monkeypatch):
    # The scheduling accept path stamps accepted_flip on the row it flips —
    # the pipeline's duplicate cleanup keys off exactly this.
    import app.db as adb
    monkeypatch.setattr(adb, "offered_bookings_for_contact",
                        lambda *a, **k: [{"id": "b-7", "booking_date": "2026-06-19",
                                          "window": "10:00"}])
    monkeypatch.setattr(adb, "accept_offered_booking",
                        lambda *a, **k: {"id": "b-7", "booking_ref": "BK-7",
                                         "booking_date": "2026-06-19", "window": "10:00"})
    monkeypatch.setattr(adb, "release_offered_for_contact", lambda *a, **k: 0)
    monkeypatch.setattr(adb, "get_scheduling_config",
                        lambda *a, **k: carried_config({"intake_configured_at": "2026-01-01T00:00:00+00:00"}))
    executor, state = scheduling.make_executor("t1", contact="kris@x.co")
    out = executor("hold_booking", {"date": "2026-06-19", "window": "10:00", "summary": "s"})
    if inspect.isawaitable(out):
        out = asyncio.run(out)  # pragma: no cover — executor is sync today
    assert out.get("accepted_offer") is True
    assert state["bookings"][0]["accepted_flip"] is True


def test_auto_send_success_confirms_holds(monkeypatch):
    env = Env(monkeypatch, auto_send=True)
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1", auto_send_ok=True))
    assert env.named("send_reply")
    assert env.named("mark_sent")[0][1] == ("log-1", "t1")   # via/edited stamps ride as kwargs
    assert env.named("resolve")[0][1] == ("log-1", "confirmed", "t1")


def test_auto_send_failure_leaves_holds_proposed(monkeypatch):
    env = Env(monkeypatch, auto_send=True, send_fails=True)
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1", auto_send_ok=True))
    assert env.named("link")          # hold linked to the still-pending row
    assert not env.named("resolve")   # neither confirmed nor released — approve/skip decide
    assert not env.named("mark_sent")


def test_redraft_releases_old_holds_first_then_links_new(monkeypatch):
    env = Env(monkeypatch)
    asyncio.run(pipeline.redraft_email_log("t1", "log-1"))
    order = [name for name, _ in env.calls if name in ("resolve", "make_executor", "link")]
    assert order == ["resolve", "make_executor", "link"]
    resolve_args = env.named("resolve")[0][1]
    assert resolve_args[:3] == ("log-1", "cancelled", "t1")
    assert env.named("link")[0][1] == (["b-1"], "log-1", "t1")


def test_redraft_without_module_touches_no_bookings(monkeypatch):
    env = Env(monkeypatch, module_on=False)
    asyncio.run(pipeline.redraft_email_log("t1", "log-1"))
    assert not env.named("resolve") and not env.named("link")
