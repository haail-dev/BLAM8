"""Today's header row: this morning's brief scrolls where "Who needs you next
· N drafts waiting" was, and Today's order keeps the right-hand end.

Kris, 2026-09-25: "I had the notification come through on my phone but
there's nowhere in app that displays it. Feel like maybe it should be a chip
that's added to the Today page. Remove the 'Who needs you next · 4 Drafts
waiting'. Put a scrolling text brief in place. Small x on the left side of
the text to close it."

The words are the Concierge's spoken turn (debrief.speak); the × dismisses
the brief's feed row, so a closed brief stays closed. Today's only, the
owner's only (it talks money). Earlier: 2026-09-21 the drafts count was the
inbox door; that count is inside the brief now.

Run: python -m pytest tests/test_today_header_row.py -q
"""
from datetime import datetime, timezone

import app.dashboard as dashboard
from tests.test_home_dashboard import Env, client  # noqa: F401  (the fixture)
from tests.test_people import as_staff  # noqa: F401  (a person who isn't the owner)

SPOKEN = ("Morning.\n\nWhile you were out, Meg Ashworth went overdue.\n\n"
          "Four drafts are waiting on you — the oldest since yesterday.\n\n"
          "Want me to start with the drafts?")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _brief(monkeypatch, *, when=None, spoken=SPOKEN):
    when = when or _now()
    monkeypatch.setattr(dashboard.db, "list_deck_notifications", lambda *a, **k: [
        {"id": "n-brief", "kind": "morning_debrief", "title": "Morning brief",
         "body": "4 drafts waiting (oldest 1d)", "created_at": when}])
    monkeypatch.setattr(dashboard.db, "latest_concierge_assistant_since", lambda *a, **k:
                        [{"content": spoken, "created_at": when}] if spoken else [])


def _header(html):
    start = html.index('aria-label="Who needs you next">')
    return html[start:html.index('class="deck-card', start)]


def test_the_brief_scrolls_where_the_drafts_count_was(client, monkeypatch):
    Env(monkeypatch)
    _brief(monkeypatch)
    header = _header(client.get("/app/").text)
    assert 'data-brief-strip data-brief-id="n-brief"' in header
    assert ("Morning. While you were out, Meg Ashworth went overdue. Four drafts are "
            "waiting on you — the oldest since yesterday. Want me to start with the drafts?") in header
    assert "Who needs you next" not in header.split(">", 1)[1]        # only the section's label
    assert "data-drafts-waiting" not in header                           # the old count is gone


def test_the_close_sits_left_of_the_words_and_the_arrows_stay_right(client, monkeypatch):
    Env(monkeypatch)
    _brief(monkeypatch)
    html = client.get("/app/").text
    header = _header(html)
    assert header.index("data-brief-close") < header.index("data-brief-track") \
        < header.index("data-deck-order-root")
    assert 'aria-label="Close this morning\'s brief"' in header
    assert html.count(" data-deck-order-root>") == 1
    # × posts the feed row's own dismiss; a tap on the words opens the Concierge
    assert '"/concierge/notifications/" + encodeURIComponent(id) + "/dismiss"' in html
    assert 'document.getElementById("concierge-btn")' in html


def test_no_brief_today_means_an_empty_row_with_the_arrows(client, monkeypatch):
    Env(monkeypatch)
    _brief(monkeypatch, when="2026-01-02T06:30:00+00:00")             # an old one, not today's
    header = _header(client.get("/app/").text)
    assert " data-brief-strip " not in header
    assert " data-deck-order-root>" in header


def test_without_the_spoken_turn_the_push_words_stand_in(client, monkeypatch):
    Env(monkeypatch)
    _brief(monkeypatch, spoken=None)
    assert "4 drafts waiting (oldest 1d)" in _header(client.get("/app/").text)


def test_someone_who_isnt_the_owner_gets_no_brief(as_staff, monkeypatch):
    _brief(monkeypatch)
    html = as_staff.get("/app/").text
    assert " data-brief-strip " not in html


def test_with_the_concierge_a_tap_opens_her(client, monkeypatch):
    """Kris 2026-09-25: the Concierge answers the brief's offer — IF they have
    the Concierge tier (Pro + Concierge, the concierge module)."""
    Env(monkeypatch, modules={"crm", "concierge"})
    _brief(monkeypatch)
    header = _header(client.get("/app/").text)
    assert "data-brief-open" in header and "data-brief-inbox" not in header


def test_without_the_concierge_a_tap_goes_to_the_waiting_drafts(client, monkeypatch):
    """ "Clicking it should take you to the Inbox with drafts filtered so you
    can see the bulk being drafted." Pro without the Concierge, and Starter."""
    Env(monkeypatch, modules={"crm"})
    _brief(monkeypatch)
    header = _header(client.get("/app/").text)
    assert '<a href="/app/conversations?cat=waiting"' in header and "data-brief-inbox" in header
    assert "data-brief-open" not in header
