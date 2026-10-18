"""Today's "before BLAM8" count is real mail only (first-walk finding, 1 Oct 2026:
the count said 2 and both were the provider's own security alerts caused by the
connect, filed category automated).

Run: python -m pytest tests/test_backlog_counts_real_mail.py -q
"""
from app import backlog, db


class _Chain:
    def __init__(self, rows):
        self._rows = rows

    def __getattr__(self, name):
        return lambda *a, **k: self

    def execute(self):
        return type("R", (), {"data": self._rows})()


class _Client:
    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        return _Chain(self._rows)


AUTO = {"from_address": "no-reply@accounts.example.com", "category": "automated"}
REAL = {"from_address": "sam@example.com", "category": "fyi"}


def test_only_automated_rows_count_zero(monkeypatch):
    monkeypatch.setattr(db, "ignored_addresses", lambda tid, client=None: set())
    rows = [AUTO, {**AUTO, "from_address": "security@example.net"}]
    assert backlog.count_rows("t1", client=_Client(rows)) == 0


def test_one_real_and_two_automated_count_one(monkeypatch):
    monkeypatch.setattr(db, "ignored_addresses", lambda tid, client=None: set())
    rows = [AUTO, REAL, {**AUTO, "from_address": "security@example.net"}]
    assert backlog.count_rows("t1", client=_Client(rows)) == 1
