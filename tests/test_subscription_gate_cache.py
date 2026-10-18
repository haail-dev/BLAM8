"""The access gate shares one tenant-row read per 10 s but still decides on every request."""
import pytest
from starlette.requests import Request  # noqa: F401

from app import auth, db, navcache
from app.auth import SubscriptionRequired, TenantCtx, TenantGone


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.setattr(navcache, "_disabled", False)
    navcache._store.clear()
    navcache._inflight.clear()
    monkeypatch.setattr(auth.settings, "require_subscription_gate", True)
    rows = {"t1": {"id": "t1", "subscription_status": "active"}}
    reads = []
    monkeypatch.setattr(db, "get_tenant", lambda tid, client=None: reads.append(tid) or (dict(rows[tid]) if tid in rows else None))
    yield rows, reads
    navcache._store.clear()
    navcache._inflight.clear()


def ctx(tid="t1"):
    return TenantCtx(tenant_id=tid, client=object(), user_id="u1")


@pytest.mark.asyncio
async def test_two_requests_inside_ten_seconds_read_the_tenant_once(gate):
    rows, reads = gate
    assert await auth.require_subscription(ctx()) is not None
    assert await auth.require_subscription(ctx()) is not None
    assert reads == ["t1"]


@pytest.mark.asyncio
async def test_a_stripe_write_drops_the_cached_row_at_once(gate, monkeypatch):
    rows, reads = gate
    await auth.require_subscription(ctx())
    rows["t1"]["subscription_status"] = "canceled"
    # the write path the webhook uses invalidates the gate's key
    class Table:
        def update(self, *a, **k): return self
        def eq(self, *a, **k): return self
        def execute(self): return None
    class Fake:
        def table(self, name): return Table()
    monkeypatch.setattr(db, "db", lambda: Fake())
    db.stripe_set_subscription_status("t1", "canceled")
    with pytest.raises(SubscriptionRequired):
        await auth.require_subscription(ctx())
    assert reads == ["t1", "t1"]


@pytest.mark.asyncio
async def test_a_missing_tenant_is_never_cached(gate):
    rows, reads = gate
    with pytest.raises(TenantGone):
        await auth.require_subscription(ctx("ghost"))
    rows["ghost"] = {"id": "ghost", "subscription_status": "active"}      # re-created
    assert await auth.require_subscription(ctx("ghost")) is not None
    assert reads == ["ghost", "ghost"]


@pytest.mark.asyncio
async def test_gate_off_reads_nothing(gate, monkeypatch):
    rows, reads = gate
    monkeypatch.setattr(auth.settings, "require_subscription_gate", False)
    assert await auth.require_subscription(ctx()) is not None
    assert reads == []
