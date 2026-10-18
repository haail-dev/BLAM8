"""Mobile-first inbox chips (Kris, 2026-07-02): the name owns the line.

Pins the rework (updated 2026-07-26 for auto-categorisation — the smart-label
pills replaced the old Attention/In-progress state pills, and one category chip
per row replaced ✓ Draft / ✓✓ Sent / Resolved):
  * "Needs you" chip is gone — an attention row lights up rose instead (whole-row
    tint); the filter pills are the six smart labels (Needs Reply first);
  * the big "✓ Booked" chip is gone;
  * the row's state chip is its category: "✓ Draft Ready" waiting, "✓✓ Quote
    Sent" for the latest reply (glyph-only on mobile);
  * the gather flow's green copy-out tick on a row is GONE (Kris 2026-09-21:
    every tier books now, so the manual bridge into the calendar came off the
    list) — the thread's pinned booking card keeps its copy-out;
  * the booking-card Scheduling upsell shows on every 5th manual confirm only.

Run: python -m pytest tests/test_inbox_mobile_chips.py -q
"""
import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app.auth import TenantCtx, require_onboarded
from app.main import app


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    yield TestClient(app)
    app.dependency_overrides.clear()


FIELDS = [{"label": "Name", "required": True, "hint": ""},
          {"label": "Phone", "required": True, "hint": ""}]

CONVOS = [
    # gather complete → green tick (both required labels present)
    {"contact": "done@x.com", "state": "pending", "subject": "MOT", "mailbox_id": None,
     "last_at": "2026-07-02T09:00:00+00:00", "thread_state": "needs_attention",
     "booking_details": [{"label": "Name", "value": "Sarah"}, {"label": "Phone", "value": "07700"}]},
    # still gathering → draft tick, and needs_attention → rose row
    {"contact": "partial@x.com", "state": "pending", "subject": "Service", "mailbox_id": None,
     "last_at": "2026-07-02T08:00:00+00:00", "thread_state": "needs_attention",
     "booking_details": [{"label": "Name", "value": "Dave"}]},
    # latest reply sent, ongoing → ✓✓ and NO state chip
    {"contact": "sent@x.com", "state": "sent", "subject": "Hours", "mailbox_id": None,
     "last_at": "2026-07-01T08:00:00+00:00", "thread_state": "in_progress"},
]


def _env(monkeypatch, convos=CONVOS, fields=FIELDS, modules=()):
    async def _no_messages(*a, **k):
        return []
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set(modules))
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"name": "T"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "ignored_addresses", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "list_conversations", lambda *a, **k: [dict(c) for c in convos])
    monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: {"intake_fields": fields})
    monkeypatch.setattr(dashboard.mailbox, "list_messages", _no_messages)


def test_attention_is_a_quiet_state_not_a_row_wash(client, monkeypatch):
    # Glow era (2026-08-31): Layer-1 state is the card's glow, keyed to the
    # coloured toolbar chips — no words on the row, never a whole-row tint.
    _env(monkeypatch)
    html = client.get("/app/conversations").text
    assert "Needs you" not in html
    assert "bg-rose-50/70" not in html                # the old row wash stays gone
    # the attention rows here derive to Needs Reply: the card glows rose and
    # keeps the old hint as its tooltip — no separate Attention marker
    assert "glow-needs_reply" in html
    assert 'title="Waiting for a response' in html
    assert ">Attention</span>" not in html
    assert ">Needs Reply <" in html                   # the key chip in the rail


def test_default_state_and_booked_chips_are_gone(client, monkeypatch):
    _env(monkeypatch)
    html = client.get("/app/conversations").text
    assert ">Draft Ready <" in html                   # the smart-label FILTER pills
    assert ">Replied <" in html
    assert '>In progress</span>' not in html          # the old row chip stays gone
    assert "✓ Booked" not in html


@pytest.mark.parametrize("modules", [(), {"scheduling"}])
def test_a_gathered_row_no_longer_wears_the_copy_out_tick(client, monkeypatch, modules):
    """Kris 2026-09-21: "we see a green tick, it's clickable to copy the booking
    info. That was built before we included Bookings in the lowest tier plan."
    The row still renders, with its category chip; the tick, its details and
    the modal it opened are off the inbox page — whatever modules are on."""
    _env(monkeypatch, modules=modules)
    html = client.get("/app/conversations").text
    assert "done@x.com" in html and ">Draft Ready <" in html
    assert "data-booking-details" not in html and "details-btn" not in html
    assert 'id="bd-modal"' not in html and "07700" not in html


def test_the_thread_keeps_its_copy_out():
    """Only the inbox pill went: the pinned booking card's gathered chip still
    opens the shared modal on the thread page."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    thread = (root / "src/app/templates/app/_thread.html").read_text(encoding="utf-8")
    assert 'id="bd-gathered-chip"' in thread and "details-btn" in thread
    page = (root / "src/app/templates/app/conversation.html").read_text(encoding="utf-8")
    assert '{% include "app/_booking_copy_modal.html" %}' in page
    inbox = (root / "src/app/templates/app/conversations.html").read_text(encoding="utf-8")
    assert "_booking_copy_modal" not in inbox and "details-btn" not in inbox


def test_sent_thread_wears_the_quote_sent_state(client, monkeypatch):
    # The glyphs left the rows with the key rework (2026-08-31): a sent thread
    # is its card's quote_sent glow and the rail's Quote Sent count. (The old
    # "✓✓" assertion only ever matched a comment in the copy-out modal's script,
    # which the inbox page stopped shipping on 2026-09-21.)
    _env(monkeypatch)
    html = client.get("/app/conversations").text
    assert "glow-quote_sent" in html
    assert '>Replied <span class="ml-1 text-slate-400 font-normal">1</span>' in html


# ── the every-5th upsell (booking card) ───────────────────────────────────────
@pytest.mark.parametrize("confirms,expected", [(0, False), (4, False), (5, True),
                                               (6, False), (10, True)])
def test_upsell_shows_on_every_fifth_confirm(monkeypatch, confirms, expected):
    monkeypatch.setattr(dashboard.db, "get_scheduling_config",
                        lambda *a, **k: {"intake_fields": FIELDS})
    monkeypatch.setattr(dashboard.db, "count_confirmed_booking_sends",
                        lambda *a, **k: confirms)
    ctx = TenantCtx(tenant_id="t1", client=None)
    pin = dashboard._booking_pin(ctx, [{"scheduling_gap": True, "status": "sent",
                                        "booking_details": [{"label": "Name", "value": "S"}]}])
    assert pin["is_request"] is True
    assert pin["upsell"] is expected


# ── The pill rail's width budget (Kris 2026-09-01) ──────────────────────────
def test_all_is_hidden_until_a_filter_is_on(client, monkeypatch):
    """An unfiltered rail already IS everything, so the All chip only repeated
    what the list said — and it cost the width that pushed Booked off a phone.
    The four working states must all be on the rail with All gone."""
    _env(monkeypatch)
    html = client.get("/app/conversations").text
    assert ">All <" not in html
    for label in ("Needs Reply", "Draft Ready", "Replied", "Booked"):
        assert label in html


def test_all_comes_back_as_the_way_out_of_a_filter(client, monkeypatch):
    """With a filter on, All is the only way back to the whole inbox — it has
    to be there, and it has to drop the cat parameter rather than set one."""
    _env(monkeypatch)
    html = client.get("/app/conversations?cat=draft_ready").text
    assert ">All <" in html
    assert 'href="/app/conversations"' in html


# ── The gist: the customer's ask, then the agent's answer (Kris 2026-09-01) ──
SUMMARISED = [
    {"contact": "sarah@x.com", "state": "pending", "subject": "Booking", "mailbox_id": None,
     "count": 2, "last_at": "2026-09-01T09:00:00+00:00", "thread_state": "in_progress",
     "request_summary": "Wants the Corsa booked in for an MOT on Friday",
     "action_summary": "Offered three Friday slots and asked them to pick one"},
]


def test_the_agent_answer_gets_its_own_line(client, monkeypatch):
    """Both sentences shared one truncated line, so the agent's half was the
    half that got cut — the row read '— AI ·' and then ran out of width."""
    _env(monkeypatch, convos=SUMMARISED)
    html = client.get("/app/conversations").text
    assert "— AI" not in html                      # the em dash join is gone
    assert "Wants the Corsa booked in for an MOT on Friday" in html
    # the agent's sentence is its own <p>, not a span trailing the customer's
    assert '<p class="text-sm leading-snug truncate text-slate-400">' in html
    assert "Offered three Friday slots and asked them to pick one" in html


def test_a_row_with_no_agent_summary_stays_one_line(client, monkeypatch):
    """Nothing to say, no line to say it on — a thread the agent hasn't acted
    on must not grow an empty second row."""
    row = dict(SUMMARISED[0])
    row.pop("action_summary")
    _env(monkeypatch, convos=[row])
    html = client.get("/app/conversations").text
    assert '<p class="text-sm leading-snug truncate text-slate-400">' not in html
    assert "Wants the Corsa booked in for an MOT on Friday" in html


# ── "It still takes a second sometimes" (Kris 2026-09-01) ────────────────────
def test_the_shell_serves_the_tapped_control_a_spinner(client, monkeypatch):
    """Installed as a PWA there is no browser chrome, so the tab's own loading
    spinner is not there to cover a slow navigation — the app has to say it
    heard the tap itself. Shell-wide wiring, checked on a real page."""
    _env(monkeypatch)
    html = client.get("/app/conversations").text
    assert "/static/nav.js" in html
    assert ".nav-spin" in html and "@keyframes nav-spin" in html


def test_a_row_that_says_what_it_is_waiting_for_keeps_its_own_words(client, monkeypatch):
    """A New row shows its own word while the page turns. The generic spinner
    must stand down there rather than dim the more informative thing — the
    contract between the two is the data-busy attribute.

    The word is 'Opening…' since 2026-09-21: the draft no longer happens on that
    request (dashboard.open_conversation detaches it), so the row is waiting on
    a page turn, and the "Agent writing reply" indicator lives on the thread it
    turns to. Claiming to draft here would be the row narrating someone else's
    work."""
    _env(monkeypatch)
    html = client.get("/app/conversations").text
    assert "data-busy" in html and "Opening" in html


def test_waiting_for_you_lists_every_pending_draft_whatever_its_label():
    # Kris 2026-09-24: Today's "N drafts waiting" opened the Draft Ready pill,
    # and Jess's pending booking draft (labelled Booking pending) wasn't there.
    src = open("src/app/dashboard.py", encoding="utf-8").read()
    assert 'if cat_sel in ("waiting", "draft_ready"):' in src
    assert 'convos = [c for c in convos if c.get("state") == "pending"]' in src
    tpl = open("src/app/templates/app/_conversation_list.html", encoding="utf-8").read()
    assert "{% if cat_sel == 'waiting' %}" in tpl and "Waiting for you" in tpl


def test_a_row_with_a_waiting_draft_says_draft_ready_whatever_its_label():
    tpl = open("src/app/templates/app/_conversation_list.html", encoding="utf-8").read()
    # Every waiting draft wears it, Draft Ready-labelled rows too (Kris 2026-09-26).
    assert "{% if c.state == 'pending' and not c.gap %}" in tpl
    assert "data-draft-waiting" in tpl



def test_the_draft_ready_pill_counts_and_shows_every_waiting_draft():
    """Kris 2026-09-26: the Draft Ready pill said 7 while 10 rows said Draft
    ready — it counted the label, the cards count the waiting drafts."""
    src = open("src/app/dashboard.py", encoding="utf-8").read()
    assert 'cat_counts["draft_ready"] = waiting_count' in src
    assert 'if cat_sel in ("waiting", "draft_ready"):' in src
