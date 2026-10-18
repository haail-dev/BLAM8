"""What is owed opens a pop-up on Today, not the CRM.

Kris, 2026-09-20: "on a contact chip, it displays the Owed stats as clickable,
which takes you to the contacts CRM. I want to change that, so that, when
clicked it opens a pop-up holding the info for that promise, and what caused
the creation for that promise. The pop-up should have a cross to close it."

Pins the four things that make it hold:

  * the "You owe" / "They owe" row is a button that opens the pop-up, not a
    link into the CRM — the CRM stays one click away, inside the pop-up;
  * the pop-up carries the promise (who owes what, by when, since when) AND
    where it came from: the exchange it was read out of — subject, day, what
    they asked, what you replied, their own words — with the exact email one
    click away;
  * the source emails are one read for the whole deck, and a read that fails
    costs the pop-up its source, never the page;
  * a promise with no email behind it still opens and says so.

Run: python -m pytest tests/test_today_promise_popup.py -q
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import crm
from app.auth import TenantCtx, require_onboarded
from app.main import app
from tests.test_home_dashboard import Env


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = \
        lambda: TenantCtx(tenant_id="t1", client=None)
    yield TestClient(app)
    app.dependency_overrides.clear()


SARAH = {"id": "c1", "name": "Sarah Vance", "email": "sarah@acme.co.uk",
         "crm_companies": {"id": "co1", "name": "Acme Ltd"}}
TOM = {"id": "c2", "name": "Tom Reed", "email": "tom@reed.co.uk", "crm_companies": None}

PROMISE = {"id": "p1", "contact_id": "c1", "deal_id": "d1", "direction": "we_owe",
           "description": "Send the slab measurements", "due_hint": "by Friday",
           "status": "open", "email_log_id": "e1",
           "created_at": "2026-09-04T10:00:00+00:00",
           "last_seen_at": "2026-09-06T08:00:00+00:00", "mention_count": 2}

EMAIL = {"id": "e1", "subject": "Re: Patio quote", "from_address": "sarah@acme.co.uk",
         "from_name": "Sarah Vance", "kind": "inbound", "channel": "email",
         "status": "sent", "created_at": "2026-09-04T09:58:00+00:00",
         "request_summary": "Wants the patio laid before October",
         "action_summary": "Offered two dates and asked for the measurements",
         "body_in": ("Hi,\n\nCould you send over the slab measurements by Friday so we "
                     "can get the order in?\n\nThanks,\nSarah\n\n"
                     "On Mon, 1 Sep 2026, Koth Landscapes wrote:\n> Here is the quote")}

DEAL = {"id": "d1", "title": "Back patio, 40m²", "status": "open"}


def _crm(monkeypatch, *, promises, contacts=(SARAH, TOM), deals=(DEAL,),
         emails=(EMAIL,), reads=None, read_fails=False):
    """A CRM tenant with nothing else on Today, so each card is a promise's."""
    Env(monkeypatch, modules={"scheduling", "crm"}, convos=[])
    monkeypatch.setattr(dashboard.db, "list_deck_notifications", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "crm_list_contacts", lambda *a, **k: list(contacts))
    monkeypatch.setattr(dashboard.db, "crm_list_deals", lambda *a, **k: list(deals))
    monkeypatch.setattr(dashboard.db, "crm_open_promises", lambda *a, **k: list(promises))

    def read(tenant_id, log_ids, client=None):
        if reads is not None:
            reads.append(list(log_ids))
        if read_fails:
            raise RuntimeError("supabase down")
        return [e for e in emails if e["id"] in log_ids]
    monkeypatch.setattr(dashboard.db, "get_email_log_entries", read)


def _card(html, name="Sarah Vance"):
    return next(c for c in html.split("<article") if name in c)


def _popup(card, pid="p1"):
    start = card.index('<template data-promise-card="%s"' % pid)
    return card[start:card.index("</template>", start)]


# ── The row ────────────────────────────────────────────────────────────────
def test_what_is_owed_is_a_button_that_opens_the_pop_up_not_a_link_to_the_crm(client, monkeypatch):
    _crm(monkeypatch, promises=[PROMISE])
    card = _card(client.get("/app/").text)
    # The fact rows only: the first card's corner also holds the order list
    # and, since 2026-09-20, the CRM mark — neither is the promise row.
    facts = card.index("data-facts")
    row = card[facts:card.index("You owe", facts)]
    assert '<button type="button" data-promise-open="p1"' in row
    assert 'data-promise-title="Sarah Vance"' in row
    assert 'href="/app/crm/contacts/c1"' not in row
    # …and the row still reads as it did.
    assert "Send the slab measurements · by Friday" in card


def test_every_other_fact_keeps_its_link(client, monkeypatch):
    """Only what is owed opens a pop-up; a draft still opens the thread."""
    _crm(monkeypatch, promises=[])
    Env(monkeypatch, modules={"scheduling", "crm"})     # the default CONVOS back
    html = client.get("/app/").text
    assert 'href="/app/conversations/view?contact=a%40x.com"' in html
    assert 'data-promise-open="' not in html          # (the script names the attribute; markup does not)


# ── The pop-up ─────────────────────────────────────────────────────────────
def test_the_pop_up_holds_the_promise(client, monkeypatch):
    _crm(monkeypatch, promises=[PROMISE])
    pop = _popup(_card(client.get("/app/").text))
    assert "You owe" in pop and "by Friday" in pop
    assert "Send the slab measurements" in pop
    assert "Sarah Vance" in pop and "Acme Ltd" in pop
    assert "Back patio, 40m²" in pop
    assert "4th Sep 26" in pop                                   # recorded
    assert "brought up 2 times" in pop and "6th Sep 26" in pop
    assert "Open in the CRM" not in pop                          # the card's corner carries the CRM mark (2026-09-20)


def test_the_pop_up_says_what_caused_it(client, monkeypatch):
    _crm(monkeypatch, promises=[PROMISE])
    pop = _popup(_card(client.get("/app/").text))
    assert "Where it came from" in pop
    assert "Read out of an email exchange with Sarah Vance" in pop
    assert "Re: Patio quote" in pop
    assert "They asked" in pop and "Wants the patio laid before October" in pop
    assert "You replied" in pop and "Offered two dates and asked for the measurements" in pop
    assert "Could you send over the slab measurements by Friday" in pop   # their own words
    assert "Koth Landscapes wrote" not in pop                              # not the quoted chain
    assert 'href="/app/conversations/view?contact=sarah%40acme.co.uk#log-e1"' in pop
    assert "Open the email" in pop


def test_the_pop_up_has_a_cross_to_close_it(client, monkeypatch):
    _crm(monkeypatch, promises=[PROMISE])
    html = client.get("/app/").text
    assert html.count("<div data-promise-modal") == 1
    shell = html[html.index("<div data-promise-modal"):]
    assert 'role="dialog"' in shell and 'aria-modal="true"' in shell
    assert "data-promise-close" in shell and 'aria-label="Close"' in shell and "&times;" in shell


def test_you_owe_reads_rose_and_says_you(client, monkeypatch):
    _crm(monkeypatch, promises=[dict(PROMISE, direction="we_owe",
                                     description="Send the revised quote")])
    pop = _popup(_card(client.get("/app/").text))
    assert "You owe" in pop and "text-rose-700" in pop and "Send the revised quote" in pop


def test_a_promise_that_looks_cleared_says_so(client, monkeypatch):
    _crm(monkeypatch, promises=[dict(PROMISE, resolution_type="suggested",
                                     resolution_evidence="measurements attached")])
    pop = _popup(_card(client.get("/app/").text))
    assert "Looks cleared" in pop and "measurements attached" in pop


# ── The read behind it ─────────────────────────────────────────────────────
def test_the_source_emails_are_one_read_for_the_whole_deck(client, monkeypatch):
    reads = []
    tom = dict(PROMISE, id="p2", contact_id="c2", email_log_id="e2", deal_id=None)
    nobody = dict(PROMISE, id="p3", contact_id="ghost")      # not a contact the CRM knows
    unsourced = dict(PROMISE, id="p4", contact_id="c2", email_log_id=None)
    _crm(monkeypatch, promises=[PROMISE, tom, nobody, unsourced], reads=reads)
    html = client.get("/app/").text
    assert reads == [["e1", "e2"]]
    assert 'data-promise-card="p1"' in html and 'data-promise-card="p2"' in html
    assert 'data-promise-card="p4"' in html
    assert 'data-promise-card="p3"' not in html


def test_no_email_read_when_nothing_on_the_deck_has_one(client, monkeypatch):
    reads = []
    _crm(monkeypatch, promises=[dict(PROMISE, email_log_id=None)], reads=reads)
    client.get("/app/")
    assert reads == []


def test_a_source_read_that_fails_costs_the_source_never_the_page(client, monkeypatch):
    _crm(monkeypatch, promises=[PROMISE], read_fails=True)
    r = client.get("/app/")
    assert r.status_code == 200
    pop = _popup(_card(r.text))
    assert "Send the slab measurements" in pop
    assert "Read out of an email exchange with Sarah Vance" in pop
    assert "Re: Patio quote" not in pop
    # …and the thread is still one click away, at the exact exchange.
    assert 'href="/app/conversations/view?contact=sarah%40acme.co.uk#log-e1"' in pop


def test_a_promise_with_no_email_behind_it_still_opens_and_says_so(client, monkeypatch):
    _crm(monkeypatch, promises=[dict(PROMISE, email_log_id=None)])
    pop = _popup(_card(client.get("/app/").text))
    assert "Not read out of an email" in pop
    assert "#log-" not in pop and "Open the email" not in pop


# ── The card itself (src/app/crm.py — pure) ────────────────────────────────────
def test_a_text_message_reads_as_a_text_not_an_email():
    o = crm.promise_card(PROMISE, SARAH, email=dict(EMAIL, channel="sms"))["origin"]
    assert o["label"] == "Read out of a text exchange with Sarah Vance"
    assert o["sms"] is True


def test_a_round_that_started_with_an_invoice_reads_as_the_invoice():
    email = dict(EMAIL, kind="invoice", body_in="[Invoice]")
    o = crm.promise_card(PROMISE, SARAH, email=email)["origin"]
    assert o["label"] == "Read out of the invoice sent to Sarah Vance"
    assert o["snippet"] == ""                       # a placeholder body is not their words


def test_their_words_are_clipped_on_a_word_never_mid_word():
    long = "word " * 100
    s = crm.promise_snippet(long)
    assert s.endswith("…") and len(s) <= crm.SNIPPET_CHARS + 1
    assert not s[:-1].endswith(" ")
    assert crm.promise_snippet("short note") == "short note"
    assert crm.promise_snippet(None) == ""


def test_the_card_needs_no_email_and_no_deal():
    card = crm.promise_card(dict(PROMISE, email_log_id=None, deal_id=None), TOM)
    assert card["name"] == "Tom Reed" and card["company"] is None and card["deal"] is None
    assert card["origin"]["kind"] == "none" and card["origin"]["href"] is None
    assert card["crm_href"] == "/app/crm/contacts/c2"


def test_a_contact_with_no_address_gets_no_thread_link_rather_than_a_broken_one():
    card = crm.promise_card(PROMISE, dict(SARAH, email=None), email=EMAIL)
    assert card["origin"]["href"] is None
    assert card["origin"]["subject"] == "Re: Patio quote"


# ── Sideways through the card's promises (Kris 2026-09-20) ─────────────────
# "Currently it opens the link you click, which I want to keep. But when
# open, I would like it to show X amount of dots for the user to scroll
# sideways through the displayed links." The tapped promise opens in view;
# every other promise on the same card is a swipe away, one dot each.
POPUP_SRC = Path("src/app/templates/app/_promise_popup.html").read_text(encoding="utf-8")


def test_the_shell_has_a_snapping_track_and_a_row_of_dots(client, monkeypatch):
    _crm(monkeypatch, promises=[PROMISE])
    html = client.get("/app/").text
    shell = html[html.index("<div data-promise-modal"):]
    assert "data-promise-track" in shell and "snap-x snap-mandatory" in shell
    assert "data-promise-dots" in shell


def test_the_script_pages_through_this_cards_promises_only():
    assert 'card.querySelectorAll("[data-facts] template[data-promise-card]")' in POPUP_SRC
    assert "goTo(all.indexOf(mine), false)" in POPUP_SRC              # the tapped one opens in view
    assert '"Promise " + (i + 1) + " of " + all.length' in POPUP_SRC
    assert 'dots.classList.toggle("hidden", count < 2)' in POPUP_SRC   # one promise, no dots
    assert 'track.addEventListener("scroll"' in POPUP_SRC              # a swipe moves the dot
    assert 'e.key === "ArrowRight"' in POPUP_SRC and 'e.key === "ArrowLeft"' in POPUP_SRC
