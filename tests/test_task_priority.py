import asyncio

from app import task_priority as tp


def test_classify_tiers():
    assert tp.classify("Check Friday", "what is free on Friday") == tp.LOOKUP
    assert tp.classify("Block Friday", "block off the diary on Friday") == tp.ACTION
    assert tp.classify("Reply to Sam", "draft a reply to Sam about the quote") == tp.DRAFT
    assert tp.classify("Check and block", "check Friday and block it") == tp.ACTION


def test_lanes_and_order_across_batches():
    async def scenario():
        order = []

        async def job(name, tier, hold=0.0):
            async with tp.gate("t", "u", tier):
                order.append(name)
                await asyncio.sleep(hold)

        first = asyncio.create_task(job("draft1", tp.DRAFT, 0.05))
        await asyncio.sleep(0.01)
        # Later request arrives while a draft runs: lookup uses the fast lane
        # at once; the action jumps ahead of the queued second draft.
        later = [asyncio.create_task(job("draft2", tp.DRAFT)),
                 asyncio.create_task(job("action", tp.ACTION)),
                 asyncio.create_task(job("lookup", tp.LOOKUP))]
        await asyncio.gather(first, *later)
        return order

    order = asyncio.run(scenario())
    assert order == ["draft1", "lookup", "action", "draft2"]
    assert not tp._LANES


def test_cancelled_waiter_does_not_wedge_lane():
    async def scenario():
        async def hold():
            async with tp.gate("t", "u", tp.ACTION):
                await asyncio.sleep(0.03)

        a = asyncio.create_task(hold())
        await asyncio.sleep(0.005)
        b = asyncio.create_task(hold())
        await asyncio.sleep(0.005)
        b.cancel()
        await asyncio.gather(a, b, return_exceptions=True)
        async with tp.gate("t", "u", tp.ACTION):
            pass

    asyncio.run(asyncio.wait_for(scenario(), 2))


def test_expiry_assessment():
    from datetime import datetime, timedelta, timezone
    from app import task_expiry as te
    now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
    ago = lambda **kw: (now - timedelta(**kw)).isoformat()
    assert te.assess({"status": "waiting", "created_at": ago(minutes=10)}, now)["stale"] is None
    assert te.assess({"status": "waiting", "created_at": ago(minutes=31)}, now)["stale"] == "nudge"
    assert te.assess({"status": "waiting", "created_at": ago(hours=25)}, now)["stale"] == "expired"
    assert te.assess({"status": "working", "started_at": ago(minutes=2)}, now)["stale"] is None
    assert te.assess({"status": "working", "started_at": ago(minutes=6)}, now)["stale"] == "stalled"
    assert te.assess({"status": "done", "created_at": ago(days=9)}, now) == {"age_seconds": None, "stale": None}


def test_public_row_carries_priority_and_staleness():
    from app import concierge_delegation as d
    row = {"id": "1", "tenant_id": "t", "title": "Block Friday", "instruction": "block off Friday",
           "status": "waiting", "created_at": "2020-01-01T00:00:00+00:00", "result": {"kind": "waiting", "summary": "ok"}}
    out = d.public(row)
    assert out["priority"] == 0 and out["stale"] == "expired"
    row.update(status="queued", result=None, created_at="2999-01-01T00:00:00+00:00")
    out = d.public(row)
    assert out["priority"] == tp.ACTION and out["stale"] is None


def test_scoped_public_tasks_reads_permissions_and_proposals_once(monkeypatch):
    from types import SimpleNamespace
    from app import concierge_delegation as d, concierge_routes, db
    ctx = SimpleNamespace(tenant_id="t", user_id="u", client=object())
    perms = d.TaskReadPermissions("t", "u", ctx.client, asker="asker", scope="S")
    calls = {"perms": 0, "proposals": 0, "single": 0}
    monkeypatch.setattr(d, "task_read_permissions", lambda c: calls.__setitem__("perms", calls["perms"] + 1) or perms)
    monkeypatch.setattr(db, "get_concierge_proposals",
                        lambda ids, t, client=None: calls.__setitem__("proposals", calls["proposals"] + 1) or
                        {i: {"id": i, "status": "pending", "kind": "k", "payload": {}} for i in ids if i != "gone"})
    monkeypatch.setattr(db, "get_concierge_proposal", lambda *a, **k: calls.__setitem__("single", calls["single"] + 1))
    monkeypatch.setattr(db, "get_tenant", lambda *a, **k: calls.__setitem__("tenant", calls.get("tenant", 0) + 1) or {"concierge_settings": {}})
    monkeypatch.setattr(concierge_routes, "_proposal_refusal",
                        lambda c, p, asker=None: "no" if p["id"] == "refused" else None)
    base = {"tenant_id": "t", "user_id": "u", "revision": 1, "status": "waiting", "created_at": "2999-01-01T00:00:00+00:00"}
    rows = [{**base, "id": "a", "title": "Check", "instruction": "what", "result": {"kind": "waiting", "scope": "S"}},
            {**base, "id": "b", "title": "Mine", "instruction": "x", "proposal_id": "p1", "result": {"kind": "waiting"}},
            {**base, "id": "c", "title": "Refused", "instruction": "x", "proposal_id": "refused", "result": {"kind": "waiting"}},
            {**base, "id": "d", "title": "Gone", "instruction": "x", "proposal_id": "gone", "result": {"kind": "waiting"}},
            {**base, "id": "e", "title": "Other scope", "instruction": "x", "result": {"kind": "lookup_answer", "scope": "OLD"}},
            {**base, "id": "f", "title": "Foreign", "instruction": "x", "user_id": "other"},
            {**base, "id": "g", "title": "Legacy", "instruction": "x", "revision": 0}]
    out = d.scoped_public_tasks(ctx, rows)
    assert [r["id"] for r in out] == ["a", "b"]
    assert calls == {"perms": 1, "proposals": 1, "single": 0, "tenant": 1}


def test_concierge_settings_clamp_and_fall_back():
    from app import concierge_settings as cs
    from app.config import settings
    assert cs.resolved(None) == {"task_nudge_minutes": settings.concierge_task_nudge_minutes,
                                 "task_expire_minutes": settings.concierge_task_expire_minutes,
                                 "task_stall_minutes": settings.concierge_task_stall_minutes}
    out = cs.resolved({"concierge_settings": {"task_nudge_minutes": 0, "task_expire_minutes": 999999, "task_stall_minutes": "x"}})
    assert out["task_nudge_minutes"] == 5 and out["task_expire_minutes"] == 7 * 24 * 60
    assert out["task_stall_minutes"] == settings.concierge_task_stall_minutes
    # A nudge at or after the expiry would never show: both fall back together.
    crossed = cs.resolved({"concierge_settings": {"task_nudge_minutes": 600, "task_expire_minutes": 120}})
    assert crossed["task_nudge_minutes"] == settings.concierge_task_nudge_minutes
    assert cs.resolved("junk") == cs.resolved(None) and cs.resolved({"concierge_settings": []}) == cs.resolved(None)
    parsed = cs.parse_form({"task_nudge_minutes": " 45 ", "task_expire_hours": "12", "task_stall_minutes": ""})
    assert parsed == {"task_nudge_minutes": 45, "task_expire_minutes": 720}
    assert cs.parse_form({"task_stall_minutes": "1"}) == {"task_stall_minutes": 2}
    assert cs.chosen({"concierge_settings": {"task_stall_minutes": 10, "bogus": 1}}) == {"task_stall_minutes": 10}


def test_business_timeouts_change_the_assessment():
    from datetime import datetime, timedelta, timezone
    from app import task_expiry as te, concierge_settings as cs
    now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
    row = {"status": "waiting", "created_at": (now - timedelta(hours=2)).isoformat()}
    assert te.assess(row, now)["stale"] == "nudge"
    quick = cs.resolved({"concierge_settings": {"task_nudge_minutes": 300, "task_expire_minutes": 90 * 1 + 60}})
    long = cs.resolved({"concierge_settings": {"task_nudge_minutes": 300}})
    assert te.assess(row, now, long)["stale"] is None
    assert te.assess(row, now, cs.resolved({"concierge_settings": {"task_expire_minutes": 90 + 30}}))["stale"] == "expired"
