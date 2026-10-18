"""Thread view: the history is pages, one round per page (Kris, 2026-09-20).

"The expanded history isn't clear on message rounds." So the blind that pulls
down over the stage now shows one exchange per page, laid out like the stage
— the customer top-left, the agent's working in the middle, the reply
bottom-right — with a marker "Round n of N · when", snapping page to page. The
newest exchange is the last page. Every page is locked to the blind's height
and long messages scroll inside their box, as the stage's own bubble does.
The pending draft's form stays on the stage; its page only points at it.

Run: python -m pytest tests/test_thread_collapse.py -q
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app.auth import TenantCtx, require_onboarded
from app.main import app

PAGE = '<section class="round-page"'


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    yield TestClient(app)
    app.dependency_overrides.clear()


def _env(monkeypatch, items):
    monkeypatch.setattr(dashboard.db, "get_conversation", lambda *a, **k: [dict(i) for i in items])
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"name": "T"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: False)


def _row(status, **kw):
    return {"id": kw.pop("id", "x"), "status": status, "body_in": "Hi, can I book my Corsa in?",
            "draft_reply": "Of course — when suits?", "created_at": "2026-07-01T10:00:00",
            "subject": "Booking", **kw}


def test_every_round_is_a_page_in_the_stage_layout(client, monkeypatch):
    _env(monkeypatch, [
        _row("sent", id="a", request_summary="Asked for full service and MOT",
             action_summary="Sent service info, asked for booking details",
             category_reason="Customer asked for a service"),
        _row("skipped", id="b"),
        _row("pending", id="c", draft_reply="Draft to approve"),
    ])
    html = client.get("/app/conversations/view?contact=sarah@x.co").text
    assert html.count(PAGE) == 3                            # one page per exchange, the newest included
    assert "Round 1 of 3" in html and "Round 3 of 3" in html
    assert 'id="log-a"' in html and 'id="log-c"' in html    # the diary's deep links still land on a round
    # the stage's shapes, per page: the customer's glass card, the ghost, the reply
    assert html.count('class="glass-card min-w-0 max-w-[85%] p-4 rounded-tl-sm"') == 3
    assert "Agent assessment:</span> Customer asked for a service" in html   # a past round carries its working
    assert html.count("Agent assessment:</span>") == 1                              # the newest leaves it to the stage
    assert "You · Skipped" in html                          # the skipped round tells its story
    assert "Draft to approve" not in html.split(PAGE, 1)[0]  # the draft belongs to the stage's form…
    assert "Editable before approval" in html                # …its page only points at it
    assert '<details class="group rnd"' not in html          # the three-state collapse is gone


def test_long_messages_scroll_inside_their_box_like_the_stage(client, monkeypatch):
    _env(monkeypatch, [_row("sent", id="a", body_in="line\n" * 80)])
    html = client.get("/app/conversations/view?contact=sarah@x.co").text
    assert html.count('class="round-body') == 2                 # the customer's box and the reply's
    assert 'overscroll-behavior: contain;' in html               # a box's scroll never bleeds into the pager
    assert "Read the rest" not in html and "data-clamp" not in html


def test_a_single_exchange_is_one_page_that_still_carries_the_sent_reply(client, monkeypatch):
    _env(monkeypatch, [_row("sent", id="a", request_summary="Only ask")])
    html = client.get("/app/conversations/view?contact=sarah@x.co").text
    assert html.count(PAGE) == 1 and "Round 1 of 1" in html
    assert "AI · Sent" in html and "Of course — when suits?" in html
    assert "Ignore this sender" not in html                  # ignoring lives on Today and the inbox row (Kris 2026-09-20)


def test_the_customer_bubble_is_capped_shorter_than_the_reply(client, monkeypatch):
    """Kris, 2026-09-21: "the customer message bubble should be a little
    smaller, it's already scrollable. There's also a divider that can be
    removed." The inbound box takes the shorter cap, and the round's working
    no longer wears a rule across the page above it."""
    _env(monkeypatch, [_row("sent", id="a", body_in="line\n" * 80,
                            category_reason="Customer asked for a service")])
    html = client.get("/app/conversations/view?contact=sarah@x.co").text
    page = html[html.index(PAGE):]
    assert page.count('class="round-body round-body-in') == 1   # the customer's box…
    assert page.count('class="round-body mt-') == 1             # …and the reply's keeps the taller cap
    css = (Path(__file__).resolve().parents[1] / "src/app/templates/app/conversation.html").read_text(encoding="utf-8")
    assert ".round-body-in { max-height: 17vh; }" in css
    ghost = page[page.index("Agent assessment:</span>") - 400:page.index("Agent assessment:</span>")]
    assert "border-t" not in ghost                            # the rule above the working is gone


def test_the_page_script_snaps_pages_and_pages_the_wheel():
    src = (Path(__file__).resolve().parents[1] / "src/app/templates/app/conversation.html").read_text(encoding="utf-8")
    assert "scroll-snap-type: y mandatory" in src and "scroll-snap-align: start" in src
    assert ".thread-pages { height: 100%; }" in src                # the container is definite…
    assert ".thread-pages > .round-page { height: 100%;" in src   # …so each page is the blind's height
    assert ".round-body { max-height: 27vh; overflow-y: auto; }" in src
    assert "function bindHistoryPages()" in src and "bindHistoryPages();" in src
    assert "scrollBy({ top: dir * history.clientHeight" in src
    assert "details.rnd" not in src                           # the old collapse's CSS and handler are gone


def test_the_masthead_names_the_customer_and_opens_their_card(client, monkeypatch):
    """Kris 2026-09-21: the name at the top of a thread is the way to the CRM card
    — the name when the CRM has one, the address under the pointer."""
    _env(monkeypatch, [_row("sent", id="a")])
    monkeypatch.setattr(dashboard.db, "crm_contact_by_email",
                        lambda *a, **k: {"id": "ct1", "name": "Sarah Price"})
    html = client.get("/app/conversations/view?contact=sarah@x.co").text
    assert 'href="/app/crm/contacts/ct1" title="sarah@x.co"' in html
    assert ">Sarah Price</a>" in html
    assert "Sarah Price · BLAM8" in html                     # the tab's title too


def test_a_thread_with_no_card_yet_still_links_to_the_crm_door(client, monkeypatch):
    _env(monkeypatch, [_row("sent", id="a")])
    monkeypatch.setattr(dashboard.db, "crm_contact_by_email", lambda *a, **k: None)
    html = client.get("/app/conversations/view?contact=sarah@x.co").text
    assert 'href="/app/crm/open?email=sarah%40x.co" title="sarah@x.co"' in html
    assert ">sarah@x.co</a>" in html
