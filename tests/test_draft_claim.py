"""One draft per inbound message, across processes (src/app/db.py, 2026-09-22).

`pipeline._drafts_in_flight` is an in-memory set whose comment reasons it is
atomic because "every caller shares one event loop". That holds inside one
process — nothing awaits between the check and the add — and production runs
more than one. A duplicate webhook delivery landed in a different worker and
both drafted: double the tokens, two trails of which only the winner's is
stored, and on 2026-09-22 a loser that inserted first with a reply reading
"Hi Tessa..." and every prose field "...".

The primary key on (tenant_id, message_id) is the lock. What is pinned here is
the behaviour that makes it safe to put on the hottest path in the app:

  * the second caller is turned away;
  * a claim left behind by a process that died is taken over, but only after
    the grace period, and only by ONE of two racing takers (the server-side
    `claimed_at < cutoff` filter decides);
  * and every failure mode drafts anyway. A duplicate draft costs money. A
    swallowed one is a customer whose email is never answered.

Run: python -m pytest tests/test_draft_claim.py -q
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import db as D

T = "11111111-1111-1111-1111-111111111111"
MSG = "1a0c93ce91f5f375"


class _Res:
    def __init__(self, data):
        self.data = data


class FakeTable:
    """Just enough PostgREST to hold a primary key and a `lt` filter."""

    def __init__(self, store: dict, fail: set[str] | None = None):
        self.store, self.fail = store, fail or set()
        self._filters: dict = {}
        self._lt: tuple[str, str] | None = None
        self._update: dict | None = None
        self._op = ""

    # -- builders ---------------------------------------------------------
    def insert(self, row):
        if "insert" in self.fail:
            raise RuntimeError("unreachable")
        key = (row["tenant_id"], row["message_id"])
        if key in self.store:
            raise RuntimeError("duplicate key value violates unique constraint")
        self.store[key] = row.get("claimed_at") or datetime.now(timezone.utc).isoformat()
        self._op = "insert"
        return self

    def update(self, values):
        if "update" in self.fail:
            raise RuntimeError("unreachable")
        self._update, self._op = values, "update"
        return self

    def delete(self):
        if "delete" in self.fail:
            raise RuntimeError("unreachable")
        self._op = "delete"
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def lt(self, col, val):
        self._lt = (col, val)
        return self

    # -- terminal ---------------------------------------------------------
    def execute(self):
        key = (self._filters.get("tenant_id"), self._filters.get("message_id"))
        if self._op == "insert":
            return _Res([{}])
        if self._op == "delete":
            self.store.pop(key, None)
            return _Res([{}])
        held = self.store.get(key)
        if held is None:
            return _Res([])
        if self._lt and not (held < self._lt[1]):
            return _Res([])                      # not stale enough — no row matched
        self.store[key] = self._update["claimed_at"]
        return _Res([{"tenant_id": key[0], "message_id": key[1]}])


class FakeClient:
    def __init__(self, store=None, fail=None):
        self.store = {} if store is None else store
        self.fail = fail or set()

    def table(self, _name):
        return FakeTable(self.store, self.fail)


def test_the_second_process_is_turned_away():
    c = FakeClient()
    assert D.claim_inbound_draft(T, MSG, client=c) is True
    assert D.claim_inbound_draft(T, MSG, client=c) is False
    # a DIFFERENT message is nothing to do with it
    assert D.claim_inbound_draft(T, "another-message", client=c) is True


def test_releasing_lets_a_genuine_retry_straight_back_in():
    c = FakeClient()
    assert D.claim_inbound_draft(T, MSG, client=c) is True
    D.release_inbound_draft(T, MSG, client=c)
    assert D.claim_inbound_draft(T, MSG, client=c) is True


def test_a_claim_from_a_process_that_died_is_taken_over_but_only_once():
    stale = (datetime.now(timezone.utc)
             - timedelta(seconds=D.DRAFT_CLAIM_GRACE_S + 60)).isoformat()
    c = FakeClient(store={(T, MSG): stale})
    assert D.claim_inbound_draft(T, MSG, client=c) is True      # taken over
    # the takeover moved claimed_at to now, so a second taker finds it fresh
    assert D.claim_inbound_draft(T, MSG, client=c) is False


def test_a_claim_still_inside_the_grace_period_is_left_alone():
    recent = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    c = FakeClient(store={(T, MSG): recent})
    assert D.claim_inbound_draft(T, MSG, client=c) is False


@pytest.mark.parametrize("broken", [{"insert", "update"}, {"insert", "update", "delete"}])
def test_an_unreachable_claim_table_drafts_anyway(broken):
    """Fail open, always. The alternative is an unanswered customer."""
    c = FakeClient(fail=broken)
    assert D.claim_inbound_draft(T, MSG, client=c) is True
    D.release_inbound_draft(T, MSG, client=c)        # must not raise


def test_a_client_that_cannot_even_be_built_drafts_anyway(monkeypatch):
    """The first version got this wrong: `client or db()` sat OUTSIDE the try,
    so a client factory that raised took the whole draft down with it — the
    one failure the fail-open exists for. 44 tests caught it."""
    def _blocked(*a, **k):
        raise RuntimeError("no client")
    monkeypatch.setattr(D, "db", _blocked)
    assert D.claim_inbound_draft(T, MSG) is True
    D.release_inbound_draft(T, MSG)                  # must not raise either


def test_release_never_raises_even_when_nothing_is_held():
    D.release_inbound_draft(T, "never-claimed", client=FakeClient())


def test_the_fingerprint_is_the_same_however_the_message_arrives():
    """The claim keyed on the provider id was not enough: 2026-09-22 two runs
    drafted one email anyway, so the id each path carried was not the same id.
    A lock is only as reliable as the key it turns on, and the sender, subject
    and opening of the body are identical whichever path delivers it."""
    a = D.draft_fingerprint("Daniel@Example.com ", "Mabel 17th-19th October",
                            "Hi,\n\nCould we book Mabel in?\n\nThanks,\nDaniel")
    # same message, different casing/whitespace — the poll path and the push
    # path do not normalise identically
    b = D.draft_fingerprint("daniel@example.com", "  Mabel   17th-19th October ",
                            "Hi,\n Could we book Mabel in?\n Thanks, Daniel")
    assert a == b and a.startswith("fp:")

    # a different customer, or a different ask, is a different email
    assert a != D.draft_fingerprint("someone@example.com", "Mabel 17th-19th October",
                                    "Hi,\n\nCould we book Mabel in?\n\nThanks,\nDaniel")
    assert a != D.draft_fingerprint("daniel@example.com", "Mabel 24th-26th October",
                                    "Hi,\n\nCould we book Mabel in?\n\nThanks,\nDaniel")

    # and it never blows up on nothing
    assert D.draft_fingerprint(None, None, None).startswith("fp:")


def test_a_second_delivery_is_turned_away_on_the_fingerprint_alone():
    """The case that matters: the two deliveries disagree about the id, so the
    id claim succeeds for both — and the fingerprint still stops the second."""
    c = FakeClient()
    fp = D.draft_fingerprint("daniel@example.com", "Mabel", "body")
    assert D.claim_inbound_draft(T, "push-id-1", client=c) is True
    assert D.claim_inbound_draft(T, fp, client=c) is True
    # the twin, arriving with a DIFFERENT provider id
    assert D.claim_inbound_draft(T, "poll-id-2", client=c) is True    # id says nothing
    assert D.claim_inbound_draft(T, fp, client=c) is False            # the fingerprint does
