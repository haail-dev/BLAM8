"""An offer's follow-up closes with the offer (src/app/offer_promises.py).

Live 2026-09-23: BK-1066 (9–11 Nov, offered to Daniel, held to 10:38 next
day) was released at 13:12 with no event; the follow-up "Daniel confirms
whether the alternative 9–11 November slot works" stayed open and the
Concierge read it back at 20:32 as if the offer still stood.
"""
from types import SimpleNamespace

import pytest

from app import db, offer_promises

BK = {"id": "b-1066", "tenant_id": "t1", "booking_ref": "BK-1066", "booking_date": "2026-11-09",
      "start_block": 28, "length_blocks": 228, "email_log_id": "log-1", "contact": "dan@x.com",
      "status": "cancelled", "expires_at": "2026-09-24T09:38:23+00:00"}
DECIDE = {"id": "p-1", "direction": "they_owe", "email_log_id": "log-1", "contact_id": "c1",
          "description": "Daniel confirms whether the alternative 9–11 November slot works or if he wants other options."}
CARD = {"id": "p-2", "direction": "they_owe", "email_log_id": "log-1", "contact_id": "c1",
        "description": "Daniel will send Mabel's vaccination card."}
OURS = {"id": "p-3", "direction": "we_owe", "email_log_id": "log-1", "contact_id": "c1",
        "description": "We will confirm whether the 9–11 November slot is available."}


@pytest.mark.parametrize("text,yes", [
    ("Daniel confirms whether the alternative 9–11 November slot works or if he wants other options.", True),
    ("Ellen to let us know which of the offered dates suits.", True),
    ("Customer to get back to us about the Friday 10am slot.", True),
    ("Daniel will send Mabel's vaccination card.", False),
    ("Hannah will pay the deposit today.", False),
    ("Meg to confirm Rufus's food brand.", False),
])
def test_only_a_choice_of_dates_counts(text, yes):
    assert offer_promises.is_offer_decision(text) is yes


def _stub(monkeypatch, promises):
    resolved = []

    class Q:
        def __getattr__(self, name):
            return lambda *a, **k: self

        def execute(self):
            return SimpleNamespace(data=promises)
    client = SimpleNamespace(table=lambda n: Q())
    monkeypatch.setattr(db, "crm_resolve_promise_auto",
                        lambda pid, t, evidence=None, by_kind=None, by_id=None, by_label=None:
                        resolved.append((pid, by_kind, by_id, by_label, evidence)))
    return client, resolved


def test_releasing_the_offer_closes_only_its_decision_followup(monkeypatch):
    client, resolved = _stub(monkeypatch, [DECIDE, CARD, OURS])
    n = offer_promises.close_for_bookings("t1", [BK], "released", client=client)
    assert n == 1
    assert resolved == [("p-1", "booking", "b-1066",
                         "offer of 9th Nov 26 to 11th Nov 26 released (BK-1066)",
                         "The offer of 9th Nov 26 to 11th Nov 26 released (BK-1066) — nothing left for them to decide.")]


def test_every_closed_offer_logs_an_event(monkeypatch):
    client, resolved = _stub(monkeypatch, [DECIDE])
    events = []
    monkeypatch.setattr(db, "log_agent_event", lambda t, kind, summary, **k: events.append((kind, summary, k.get("booking_ref"))))
    offer_promises.note_closed("t1", [BK], "expired", client=client)
    assert events == [("offer_closed", "Offered dates 9th Nov 26 to 11th Nov 26 (BK-1066) expired", "BK-1066")]
    assert resolved and resolved[0][3].endswith("expired (BK-1066)")


def test_the_release_paths_all_go_through_it():
    src = open("src/app/db.py", encoding="utf-8").read()
    for fn, outcome in (("def release_offered_for_contact", "released"),
                        ("def expire_offered_bookings", "expired"),
                        ("def resolve_holds_for_log", "released"),
                        ("def accept_offered_booking", "accepted")):
        body = src[src.index(fn):][:2600]
        assert f'"{outcome}"' in body and "_offers_closed(" in body, fn
