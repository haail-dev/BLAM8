"""A draft's send settles its "Draft ready" card on Today.

Live 2026-09-04 (farrier run): Today kept two "Draft ready — tap to review and
approve" cards for drafts already approved and sent. The feed row is keyed by
the thread it deep-links to, so the send resolves every live draft_ready row
pointing at that thread.

Run: python -m pytest tests/test_notification_settle.py -q
"""
from urllib.parse import quote

from app import db


class _Q:
    def __init__(self, sink, data):
        self.sink, self.data, self.calls = sink, data, []
    def __getattr__(self, name):
        def _f(*a, **k):
            self.calls.append((name, a, k))
            return self
        return _f
    def execute(self):
        self.sink.append(list(self.calls))
        return type("R", (), {"data": self.data})()


class _Client:
    def __init__(self, sink, by_table):
        self.sink, self.by_table = sink, by_table
    def table(self, name):
        q = _Q(self.sink, self.by_table.get(name, []))
        q.calls.append(("table", (name,), {}))
        return q


def test_the_send_stamp_settles_the_thread_s_draft_ready_rows():
    sink = []
    client = _Client(sink, {"email_log": [{"from_address": "kris@example.com"}],
                            "concierge_notifications": [{"id": "n1"}, {"id": "n2"}]})
    db.mark_email_log_sent("log1", "t1", via="human", edited=False, client=client)
    tables = [c[0][1][0] for c in sink]
    assert tables[:2] == ["email_log", "concierge_notifications"]   # read the sender, settle the cards, then stamp sent
    settle = sink[1]
    assert ("update", ({"dismissed_at": settle[1][1][0]["dismissed_at"]},), {}) == settle[1]
    filters = {c[0]: c[1] for c in settle if c[0] in ("eq", "is_")}
    assert ("kind", "draft_ready") in [c[1] for c in settle if c[0] == "eq"]
    assert ("url", "/app/conversations/view?contact=" + quote("kris@example.com")) in [c[1] for c in settle if c[0] == "eq"]
    assert ("dismissed_at", "null") in [c[1] for c in settle if c[0] == "is_"]


def test_resolve_needs_a_kind_and_a_url_and_never_raises():
    assert db.resolve_concierge_notifications("t1", kind="draft_ready", url="") == 0
    class Boom:
        def table(self, name):
            raise RuntimeError("down")
    assert db.resolve_concierge_notifications("t1", kind="draft_ready", url="/x", client=Boom()) == 0
