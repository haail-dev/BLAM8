"""Page-data caches (conversation list, bookings) never outlive a write, never lose rows past
PostgREST's 1,000-row cap, and hand each caller a private copy."""
import threading
import time

import httpx
import pytest

from app import db, navcache


@pytest.fixture(autouse=True)
def cache_on(monkeypatch):
    monkeypatch.setattr(navcache, "_disabled", False)
    navcache._store.clear()
    navcache._inflight.clear()
    navcache._gen_tenant.clear()
    yield
    navcache._store.clear()
    navcache._inflight.clear()
    navcache._gen_tenant.clear()


# ---- navcache.get_or_fill_data -------------------------------------------------------------

def test_data_is_cached_until_a_write_to_that_tenant():
    calls = []
    fill = lambda: calls.append(1) or {"v": len(calls)}
    assert navcache.get_or_fill_data("t1", "t:t1:data:x", 60, fill) == {"v": 1}
    assert navcache.get_or_fill_data("t1", "t:t1:data:x", 60, fill) == {"v": 1}
    navcache.bump_data("t1")
    assert navcache.get_or_fill_data("t1", "t:t1:data:x", 60, fill) == {"v": 2}


def test_a_write_to_one_tenant_leaves_another_tenants_data_alone():
    navcache.get_or_fill_data("t1", "t:t1:data:x", 60, lambda: 1)
    navcache.get_or_fill_data("t2", "t:t2:data:x", 60, lambda: 2)
    navcache.bump_data("t1")
    assert navcache.peek("t:t1:data:x") == (False, None)
    assert navcache.peek("t:t2:data:x") == (True, 2)


def test_an_untargeted_write_drops_every_tenants_data_but_not_the_nav_chrome():
    navcache.get_or_fill_data("t1", "t:t1:data:x", 60, lambda: 1)
    navcache.get_or_fill_data("t2", "t:t2:data:x", 60, lambda: 2)
    navcache.put("t:t1:chrome", 60, "chrome")
    navcache.bump_data(None)
    assert navcache.peek("t:t1:data:x")[0] is False and navcache.peek("t:t2:data:x")[0] is False
    assert navcache.peek("t:t1:chrome") == (True, "chrome")


def test_a_fill_that_started_before_a_write_is_not_stored_after_it():
    started, release = threading.Event(), threading.Event()

    def slow_read():
        started.set()
        release.wait(5)
        return "old rows"
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("v", navcache.get_or_fill_data("t1", "t:t1:data:x", 60, slow_read)))
    t.start()
    assert started.wait(2)
    navcache.bump_data("t1")                 # the write lands while the read is in flight
    release.set()
    t.join(5)
    assert out["v"] == "old rows"            # the caller still gets its answer...
    assert navcache.peek("t:t1:data:x") == (False, None)   # ...but nobody else is handed it later


def test_a_global_write_during_a_fill_also_prevents_storing():
    def read():
        navcache.bump_data(None)
        return "x"
    navcache.get_or_fill_data("t1", "t:t1:data:x", 60, read)
    assert navcache.peek("t:t1:data:x") == (False, None)


# ---- the db client hooks: every write to email_log / bookings / an RPC invalidates --------------

def hit(method, path, query=""):
    return httpx.Request(method, f"https://x.supabase.co{path}" + (f"?{query}" if query else ""))


def test_writes_to_the_tracked_tables_invalidate_the_right_scope():
    tid = "11111111-2222-3333-4444-555555555555"
    for key in (f"t:{tid}:data:a", "t:other:data:a"):
        navcache.put(key, 60, 1)
    db._note_data_write(hit("PATCH", "/rest/v1/email_log", f"tenant_id=eq.{tid}&id=eq.5"))
    assert navcache.peek(f"t:{tid}:data:a")[0] is False and navcache.peek("t:other:data:a")[0] is True
    navcache.put(f"t:{tid}:data:a", 60, 1)
    db._note_data_write(hit("POST", "/rest/v1/bookings"))             # tenant not in the URL: everyone's
    assert navcache.peek(f"t:{tid}:data:a")[0] is False and navcache.peek("t:other:data:a")[0] is False
    navcache.put("t:other:data:a", 60, 1)
    db._note_data_write(hit("POST", "/rest/v1/rpc/claim_something"))
    assert navcache.peek("t:other:data:a")[0] is False
    navcache.put("t:other:data:a", 60, 1)
    db._note_data_write(hit("DELETE", "/rest/v1/email_log", "id=eq.9"))
    assert navcache.peek("t:other:data:a")[0] is False


def test_reads_and_unrelated_writes_leave_the_caches_alone():
    navcache.put("t:t1:data:a", 60, 1)
    db._note_data_write(hit("GET", "/rest/v1/email_log", "tenant_id=eq.x"))
    db._note_data_write(hit("HEAD", "/rest/v1/bookings"))
    db._note_data_write(hit("POST", "/rest/v1/concierge_tasks"))
    db._note_data_write(hit("PATCH", "/rest/v1/tenants", "id=eq.x"))
    assert navcache.peek("t:t1:data:a") == (True, 1)


def test_the_hooks_are_on_the_real_clients():
    hooks = db._http1_options().httpx_client.event_hooks
    assert db._stamp_request in hooks["request"] and db._count_response in hooks["response"]


# ---- pagination past the 1,000-row cap -----------------------------------------------------------

class FakeQuery:
    def __init__(self, rows, log):
        self.rows, self.log, self.want_count = rows, log, None
        self.start = self.end = None

    def range(self, a, b):
        self.start, self.end = a, b
        return self

    def execute(self):
        self.log.append((self.start, self.end, self.want_count))
        page = self.rows[self.start:self.end + 1]
        return type("R", (), {"data": page, "count": len(self.rows) if self.want_count else None})()


def builder(rows, log):
    def build(count):
        q = FakeQuery(rows, log)
        q.want_count = count
        return q
    return build


def test_all_rows_returns_everything_in_order_across_pages():
    rows = [{"id": i} for i in range(2350)]
    log = []
    assert db._all_rows(builder(rows, log)) == rows
    assert sorted(log) == [(0, 999, "exact"), (1000, 1999, None), (2000, 2999, None)]


def test_all_rows_is_one_request_when_it_fits():
    log = []
    assert len(db._all_rows(builder([{"id": i} for i in range(400)], log))) == 400
    assert log == [(0, 999, "exact")]
    log.clear()
    assert db._all_rows(builder([], log)) == []
    log.clear()
    exact = [{"id": i} for i in range(1000)]
    assert db._all_rows(builder(exact, log)) == exact and len(log) == 1   # exactly one full page, no extra call


# ---- list_conversations: the newest rows survive, results are private copies ------------------------

class FakeClient:
    """email_log with 1,300 rows (the 300 newest are past the cap), bookings empty."""
    def __init__(self):
        base = "2026-10-01T00:00:{:02d}"
        self.rows = [{"id": f"r{i:04d}", "from_address": f"c{i % 5}@example.invalid", "subject": f"s{i}",
                      "status": "sent", "created_at": f"2026-10-01T{(i // 60) % 24:02d}:{i % 60:02d}:00+00:00",
                      "mailbox_id": None} for i in range(1300)]
        self.reads = 0

    def table(self, name):
        client = self

        class T:
            def __init__(s):
                s.name = name
                s.start, s.end, s.count = 0, 10 ** 9, None

            def select(s, *a, count=None, **k):
                s.count = count
                return s

            def eq(s, *a, **k): return s
            def in_(s, *a, **k): return s
            def gte(s, *a, **k): return s
            def order(s, *a, **k): return s
            def range(s, a, b): s.start, s.end = a, b; return s

            def execute(s):
                if s.name == "email_log":
                    client.reads += 1
                    data = client.rows[s.start:s.end + 1]
                    return type("R", (), {"data": data, "count": len(client.rows) if s.count else None})()
                return type("R", (), {"data": [], "count": None})()
        return T()


def test_list_conversations_sees_rows_past_the_cap_and_caches(monkeypatch):
    monkeypatch.setattr(db, "_conversation_previews", lambda ids, client: {})
    fake = FakeClient()
    out = db.list_conversations("t1", client=fake)
    assert sum(c["count"] for c in out) == 1300                    # none dropped at 1,000
    newest = max(fake.rows, key=lambda r: r["created_at"])
    assert any(c["subject"] == newest["subject"] for c in out)    # the newest row won its conversation
    reads_after_first = fake.reads
    again = db.list_conversations("t1", client=fake)
    assert fake.reads == reads_after_first                         # cached: no more reads
    out[0]["subject"] = "MUTATED"
    assert db.list_conversations("t1", client=fake)[0]["subject"] != "MUTATED"
    navcache.bump_data("t1")
    db.list_conversations("t1", client=fake)
    assert fake.reads > reads_after_first                          # a write forces a fresh read


def test_list_bookings_filters_expired_offers_after_the_cache(monkeypatch):
    rows = [{"id": "b1", "status": "confirmed"}, {"id": "b2", "status": "offered"}]

    class C:
        def table(self, n):
            class T:
                def select(s, *a, **k): return s
                def eq(s, *a, **k): return s
                def in_(s, *a, **k): return s
                def gte(s, *a, **k): return s
                def order(s, *a, **k): return s
                def execute(s): return type("R", (), {"data": [dict(r) for r in rows]})()
            return T()
    drop = {"on": False}
    monkeypatch.setattr(db, "_drop_expired_offers", lambda r: [x for x in r if not (drop["on"] and x["status"] == "offered")])
    assert len(db.list_bookings("t1", "2026-10-14", client=C())) == 2
    drop["on"] = True                                               # the offer's deadline passes; no write happened
    assert [b["id"] for b in db.list_bookings("t1", "2026-10-14", client=C())] == ["b1"]
