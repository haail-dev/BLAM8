"""Phase 5 — provider-native threading.

Unit coverage for the query branch that scopes a conversation by the provider thread id
(Graph conversationId / Gmail threadId) when one is present, and falls back to the
sender address otherwise. Uses a fake query client so it needs no database — it asserts
which `.eq(...)` filters get_conversation applies.

Run: python -m pytest tests/ -q   (from the project root)
"""
from app import db


class _FakeQuery:
    """Mimics the supabase query-builder chain just enough to record .eq() filters."""
    def __init__(self, rows):
        self._rows = rows
        self.eqs: list[tuple] = []

    def table(self, _name):
        return self

    def select(self, _cols):
        return self

    def eq(self, col, val):
        self.eqs.append((col, val))
        return self

    def order(self, _col):
        return self

    def execute(self):
        return type("Res", (), {"data": self._rows})()


def test_get_conversation_filters_by_thread_id_when_given():
    fake = _FakeQuery([{"id": "1"}])
    rows = db.get_conversation("t1", "dave@x.com", client=fake, thread_id="THREAD-A")
    assert ("thread_id", "THREAD-A") in fake.eqs
    assert ("from_address", "dave@x.com") not in fake.eqs   # thread id wins over contact
    assert rows == [{"id": "1"}]


def test_get_conversation_filters_by_contact_without_thread_id():
    fake = _FakeQuery([])
    db.get_conversation("t1", "dave@x.com", client=fake)
    assert ("from_address", "dave@x.com") in fake.eqs
    assert all(col != "thread_id" for col, _ in fake.eqs)


def test_get_conversation_always_scopes_to_tenant():
    fake = _FakeQuery([])
    db.get_conversation("tenant-9", "dave@x.com", client=fake, thread_id="T")
    assert ("tenant_id", "tenant-9") in fake.eqs
