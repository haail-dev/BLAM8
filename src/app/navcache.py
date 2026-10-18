"""Process-local TTL cache for the per-navigation hot path.

Every /app page pays the same chain before it renders anything: resolve the
JWT to a user, the user to a tenant, check onboarding, then read the nav
chrome (modules, tenant row, draft count). Each read is an HTTPS round trip
to Supabase and they run one after another — that chain, not the page's own
data, was the bulk of "navigating takes actual seconds" (Kris 2026-08-31).

This cache holds exactly those reads, briefly. The service runs as ONE
uvicorn process (render.yaml), so a dict is the whole story; if workers are
ever added each process holds its own copy and the TTLs still bound
staleness.

Contract for writers: anything that CHANGES what a key answers must call
invalidate_tenant()/invalidate() (module toggles and plan flips do, via
src/app/db.py; onboarding needs nothing because only True is ever cached).
"""
import threading
import time

_lock = threading.Lock()
_store: dict[str, tuple[float, object]] = {}
_MAX_KEYS = 4096
# Tests flip this True (conftest _fresh_navcache): every lookup misses, so a
# test that re-renders a page after re-patching its Env reads fresh state —
# the caching itself is timing, never semantics.
_disabled = False


def peek(key: str):
    """(hit, value) — hit False on miss or expiry."""
    if _disabled:
        return False, None
    with _lock:
        entry = _store.get(key)
        if entry and entry[0] > time.monotonic():
            return True, entry[1]
    return False, None


def put(key: str, ttl: float, value) -> None:
    if _disabled:
        return
    now = time.monotonic()
    with _lock:
        if len(_store) >= _MAX_KEYS:
            # Bound memory: drop the expired, then oldest-inserted until it fits.
            for k in [k for k, (exp, _) in _store.items() if exp <= now]:
                del _store[k]
            while len(_store) >= _MAX_KEYS:
                del _store[next(iter(_store))]
        _store[key] = (now + ttl, value)


class _Flight:
    """One fill in progress for a key: followers wait on it instead of repeating the read."""
    __slots__ = ("done", "ok", "value")

    def __init__(self):
        self.done = threading.Event()
        self.ok = False
        self.value = None


_inflight: dict[str, _Flight] = {}
_FLIGHT_WAIT = 25.0


def get_or_fill(key: str, ttl: float, fill, cache_if=None):
    """Cached value for key, calling fill() on miss/expiry. fill runs outside
    the lock. cache_if(value) can veto storing — e.g. never cache a None
    tenant, so a failed lookup stays a live question.

    Single flight: when several requests miss the same key together (a page load fires the
    page, the Concierge panel, the setup status and history at once, all needing the same chrome
    and tenant config), ONE of them fills it and the rest wait for that answer instead of each
    paying the same Supabase round trips. A follower never trusts a leader that failed or
    declined to cache (cache_if): it runs its own fill, exactly as before. Off when the cache is
    disabled (tests), where every lookup fills."""
    hit, val = peek(key)
    if hit:
        return val
    if _disabled:
        val = fill()
        return val
    with _lock:
        flight = _inflight.get(key)
        leader = flight is None
        if leader:
            flight = _inflight[key] = _Flight()
    if not leader:
        if flight.done.wait(_FLIGHT_WAIT) and flight.ok:
            return flight.value
        hit, val = peek(key)          # the leader may have cached it just as we woke
        if hit:
            return val
        val = fill()                  # leader failed, declined to cache, or was slow: our own read
        if cache_if is None or cache_if(val):
            put(key, ttl, val)
        return val
    try:
        val = fill()
        stored = cache_if is None or cache_if(val)
        if stored:
            put(key, ttl, val)
        flight.value, flight.ok = val, stored
        return val
    finally:
        with _lock:
            if _inflight.get(key) is flight:
                del _inflight[key]
        flight.done.set()


def invalidate(prefix: str) -> None:
    """Drop every key under prefix (keys are namespaced 't:<tenant>:…',
    'u:<token-hash>', 'ut:<user>', 'pa:<user>')."""
    with _lock:
        for k in [k for k in _store if k.startswith(prefix)]:
            del _store[k]


def invalidate_tenant(tenant_id: str) -> None:
    invalidate("t:" + tenant_id)


def invalidate_containing(fragment: str) -> None:
    """Drop every key that contains fragment (used for the ':data:' family on an untargeted write)."""
    with _lock:
        for k in [k for k in _store if fragment in k]:
            del _store[k]


# -- Data caches that must never outlive a write ------------------------------------------------
# The nav-chrome keys above hold for a fixed few seconds. Page DATA (the conversation list, today's
# bookings) is different: the owner acts on it and expects the next page to show the result. Every
# write the database client sends to email_log, bookings or an RPC (src/app/db.py hooks) calls
# bump_data(); that drops the affected ':data:' keys AND moves a generation counter, so a fill that
# STARTED before a write is never stored after it (it may have read the old rows). The TTL is only
# the backstop for writes this process cannot see (another process, direct SQL).
_gen_global = 0
_gen_tenant: dict[str, int] = {}


def data_generation(tenant_id: str) -> tuple[int, int]:
    return (_gen_global, _gen_tenant.get(tenant_id, 0))


def bump_data(tenant_id: str | None = None) -> None:
    """A write landed. With a tenant id only that tenant's data keys go; without one (a write whose
    tenant is not in the URL) every tenant's do. Safe on any thread, any number of times."""
    global _gen_global
    with _lock:
        if tenant_id:
            _gen_tenant[tenant_id] = _gen_tenant.get(tenant_id, 0) + 1
        else:
            _gen_global += 1
    if tenant_id:
        invalidate("t:" + tenant_id + ":data:")
    else:
        invalidate_containing(":data:")


def get_or_fill_data(tenant_id: str, key: str, ttl: float, fill):
    """get_or_fill for write-sensitive data: the value is stored only if no write to this tenant's
    data (or an untargeted write) landed while it was being read."""
    state: dict = {}

    def run():
        state["gen"] = data_generation(tenant_id)
        return fill()
    return get_or_fill(key, ttl, run, cache_if=lambda _v: state.get("gen") == data_generation(tenant_id))
