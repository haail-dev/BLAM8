"""The inbox row's summary for an outbound-only round.

An invoice, a quote or a reminder the business sent is logged as one email_log
row with a "[invoice]"-style placeholder where a customer message would be.
The thread already names such a round for what it is; the inbox row printed
the placeholder — "Invoice INV-1001 — [invoice] · 3 messages" (kennels film,
2026-09-20). One rule now serves both: db.conversation_preview.

Run: python -m pytest tests/test_inbox_row_preview.py -q
"""
from app import db
import app.dashboard as dashboard


def test_an_outbound_only_round_previews_as_what_was_sent():
    assert db.conversation_preview("[invoice]", "invoice") == "the invoice you sent"
    assert db.conversation_preview("[day-before reminder] BK-1001 …", "reminder") == "the booking reminder you sent"
    assert db.conversation_preview("[on my way] We are on our way", "on_my_way") == "the on-my-way message you sent"


def test_a_customer_message_previews_as_its_first_140_characters():
    assert db.conversation_preview("Hi, could Biscuit board from the 9th?", None) == "Hi, could Biscuit board from the 9th?"
    assert len(db.conversation_preview("x" * 500, "reply")) == 140
    assert db.conversation_preview(None, "invoice") == ""
    # a bracketed opening on a kind the rule does not know stays as written
    assert db.conversation_preview("[urgent] the gate is broken", None) == "[urgent] the gate is broken"


def test_the_thread_and_the_row_share_one_rule():
    assert dashboard._PLACEHOLDER_BODY is db.PLACEHOLDER_BODY
    assert dashboard._OUTBOUND_KIND_WORDS is db.OUTBOUND_KIND_WORDS


def test_the_preview_lookup_reads_the_kind_and_applies_the_rule():
    class _Q:
        def __init__(self):
            self.cols = None

        def table(self, _n):
            return self

        def select(self, cols):
            self.cols = cols; return self

        def in_(self, *_a):
            return self

        def execute(self):
            return type("Res", (), {"data": [
                {"id": "r1", "body_in": "[invoice]", "kind": "invoice"},
                {"id": "r2", "body_in": "Thanks, booked!", "kind": None},
            ]})()

    q = _Q()
    out = db._conversation_previews(["r1", "r2"], q)
    assert "kind" in q.cols
    assert out == {"r1": "the invoice you sent", "r2": "Thanks, booked!"}
