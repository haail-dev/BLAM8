"""A rule that turns on elapsed time is worked out on the real dates (run #7).

Live (swim school, 2026-09-05, a Saturday): "Finn was sick on Thursday night —
is he still OK for Saturday morning?" with the assessment booked for Saturday
12 September. The drafter did the 48-hour arithmetic right (clear by Saturday
night) and then applied it to the wrong Saturday — "his Saturday 08:30
assessment will need to move" — because "Saturday" in the parent's words was
read as the booking's date instead of the dated booking on the record. The
date anchor now tells the drafter to write the dates out and compare them.

Run: python -m pytest tests/test_date_anchor_elapsed_rules.py -q
"""
from datetime import date

from app.agent import date_anchor_line


def test_the_anchor_says_to_work_elapsed_time_rules_on_the_dated_booking():
    line = date_anchor_line(date(2026, 9, 5))
    assert line.startswith("Today is Saturday 05 September 2026 (2026-09-05).")
    assert "48 hours after an illness" in line
    assert "the dated booking on their record, not the next such day" in line
    assert "never your working" in line


def test_working_in_the_reply_field_is_not_a_draft():
    """The first version of the anchor ("write the dates out before concluding")
    had the model write its working INTO the reply field and end the turn:
    "...checking the rules...}  Let me work this out carefully before replying.
    Today is Saturday 05 September 2026… The customer says" — a valid envelope,
    stored as the draft. Working parses as an empty reply, so the corrective
    retry asks for the finished message."""
    import json
    from app.agent import _parse_draft, _working_reply
    working = ("...checking the rules...}  Let me work this out carefully before replying.  "
               "Today is Saturday 05 September 2026. Finn was sick on Thursday night. Wait — is the "
               "customer asking about THIS Saturday? The customer says ")
    assert _working_reply(working)
    assert _parse_draft(json.dumps({"reply": working, "thread_state": "resolved"}))["reply"] == ""
    real = ("Good news — Finn was sick on Thursday 3 September, so he is clear from Saturday evening "
            "the 5th; his assessment on Saturday 12 September stands. Best wishes, Hannah")
    assert not _working_reply(real)
    assert _parse_draft(json.dumps({"reply": real, "thread_state": "in_progress"}))["reply"] == real
