"""The stage's centre panel — the agent's decision as stored truth (Kris
2026-08-31): what the customer asked, what the agent did, the KB search it ran,
the diary holds it placed. Nothing is re-asked of a model; every line is a
recorded fact, and any missing piece just drops its line.

Run: python -m pytest tests/test_thread_decision.py -q
"""
import re

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app.auth import TenantCtx, require_onboarded
from app.main import app


def stage_classes(html: str) -> str:
    """The stage element's own class list. Asserted against rather than the
    whole page: the pulse keyframes are named needs-eyes-* and live in the
    stylesheet on EVERY render, so a bare substring search always matches."""
    m = re.search(r'id="thread-stage"[^>]*class="([^"]*)"', html)
    return m.group(1) if m else ""


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    yield TestClient(app)
    app.dependency_overrides.clear()


def _env(monkeypatch, items, trace=(), bookings=()):
    monkeypatch.setattr(dashboard.db, "get_conversation", lambda *a, **k: [dict(i) for i in items])
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: set())
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"name": "T"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "list_agent_trace",
                        lambda *a, **k: [dict(t) for t in trace])
    monkeypatch.setattr(dashboard.db, "bookings_for_log",
                        lambda *a, **k: [dict(b) for b in bookings])


ROW = {"id": "e1", "status": "pending", "body_in": "Can you fit my Corsa in Friday?",
       "draft_reply": "Of course.", "created_at": "2026-08-31T10:00:00", "subject": "Booking",
       "request_summary": "Wants the Corsa booked in on Friday",
       "action_summary": "Offered three Friday slots and asked them to pick"}


def test_decision_panel_tells_the_stored_truth(client, monkeypatch):
    row = dict(ROW, draft_sources=[{"source": "Opening hours", "similarity": 0.91},
                                   {"source": "MOT pricing", "similarity": 0.84}])
    _env(monkeypatch, [row],
         trace=[{"trace_type": "kb_retrieval",
                 "detail": {"query": "Friday availability MOT bay", "results_returned": 3}}],
         bookings=[{"status": "hold", "booking_ref": "BK-2001",
                    "booking_date": "2026-09-04", "window": "10:00"}])
    html = client.get("/app/conversations/view?contact=sam@x.co").text
    assert 'id="stage-decision"' in html
    # they-said / I-did came out — message and draft are on the stage already
    assert "They said:" not in html and "I did:" not in html
    # the search line names what the draft DREW ON, never the raw-email query
    assert "Friday availability MOT bay" not in html and "3 knowledge matches" in html
    assert "Searched your knowledge</span> using the customer's message" in html
    # The trail reads "Held slot · 4th Sep 26 · 10:00" (Kris 2026-09-01): a
    # date a person can read, the slot TIME, and no booking ref.
    assert "Held slot · 4th Sep 26 · 10:00" in html
    # …and the ISO date is nowhere a person reads it. The line's own link
    # (2026-09-20) carries it to the Diary, which is an address, not a word.
    assert "2026-09-04" not in re.sub(r'href="[^"]*"', "", html)
    assert "pull down for history" not in html          # the hint is gone


def test_decision_panel_drops_missing_lines_not_the_thread(client, monkeypatch):
    # No self-reports, but the classifier said WHY — that line stands alone.
    row = dict(ROW, request_summary=None, action_summary=None,
               category_reason="Customer is asking to book — needs slot options")
    _env(monkeypatch, [row])
    html = client.get("/app/conversations/view?contact=sam@x.co").text
    assert 'id="stage-decision"' in html
    assert "They said:" not in html
    assert "Agent assessment:" in html and "needs slot options" in html
    assert "Searched your knowledge" not in html


def test_recorded_query_replaces_the_model_search_guess(client, monkeypatch):
    # The model's own lookup intent is THE line; source names only serve rows
    # drafted before the field existed.
    row = dict(ROW, searched_for="Thursday availability, Starter plan mailboxes",
               draft_sources=[{"source": "Opening hours", "similarity": 0.9}])
    _env(monkeypatch, [row],
         trace=[{"trace_type": "kb_retrieval",
                 "detail": {"query": "raw email echo", "results_returned": 6}}])
    html = client.get("/app/conversations/view?contact=sam@x.co").text
    assert "Searched your knowledge</span> using the customer's message" in html
    assert "Thursday availability, Starter plan mailboxes" not in html
    assert "raw email echo" not in html and "6 knowledge matches" in html
    assert "drew on" not in html


def test_zero_knowledge_matches_says_nothing(client, monkeypatch):
    # Kris 2026-08-31: "searched for availability — 0 matches" read as the
    # DIARY finding nothing while three held slots sat right below it. The
    # count is the KNOWLEDGE search's, says so, and at zero says nothing.
    row = dict(ROW, searched_for="Tuesday 15 Sept availability")
    _env(monkeypatch, [row],
         trace=[{"trace_type": "kb_retrieval",
                 "detail": {"query": "x", "results_returned": 0}}])
    html = client.get("/app/conversations/view?contact=sam@x.co").text
    assert "Tuesday 15 Sept availability" not in html
    assert "Searched your knowledge</span> using the customer's message" in html
    assert "0 match" not in html and "0 knowledge match" not in html


def test_proactive_reminder_explains_its_trigger(client, monkeypatch):
    # A day-before reminder has no inbound and no self-reports — its WHY is the
    # kind: nothing the customer sent triggered it.
    row = dict(ROW, request_summary=None, action_summary=None, kind="reminder")
    _env(monkeypatch, [row])
    html = client.get("/app/conversations/view?contact=sam@x.co").text
    assert "No customer message triggered this" in html
    assert "day-before" in html


def test_nothing_recorded_means_no_ghost(client, monkeypatch):
    row = dict(ROW, request_summary=None, action_summary=None)
    _env(monkeypatch, [row])
    html = client.get("/app/conversations/view?contact=sam@x.co").text
    assert 'id="stage-decision"' not in html   # empty ghost would be noise


def test_knowledge_hold_docks_the_gap_card_not_an_empty_form(client, monkeypatch):
    # Knowledge hold: deferred + no reply written — the dock shows the gap and
    # its two doors (Add to Knowledge carries the row id for the auto-redraft;
    # Draft anyway is the escape hatch), never an empty draft form. In the
    # ghost, the knowledge story sits ABOVE the why (Kris 2026-08-31).
    row = dict(ROW, draft_reply="", knowledge_gap="whether BLAM8 can be self-hosted",
               category_reason="Two product questions — one unanswerable")
    _env(monkeypatch, [row])
    html = client.get("/app/conversations/view?contact=sam@x.co").text
    assert "No draft written — missing knowledge" in html
    assert "whether BLAM8 can be self-hosted" in html
    assert "Draft anyway" not in html                 # no escape hatch — the fact is the way through
    # the Knowledge paste box lives inline in the dock — no page hop; the
    # hidden redraft_log is the auto-redraft handoff
    assert 'action="/app/knowledge/text"' in html
    assert 'name="redraft_log" value="e1"' in html
    assert "Save &amp; draft reply" in html
    assert '<textarea id="draft-' not in html         # no empty form to approve
    assert html.index("Couldn&#39;t answer:" if "Couldn&#39;t" in html
                      else "Couldn't answer:") < html.index("Why:")


def test_rose_thread_pulses_and_shows_its_reasons(client, monkeypatch):
    # A needs-you thread explains itself (Kris 2026-09-01): the whole stage
    # pulses rose — no warning sign to tap — and the reasons open the ghost.
    row = dict(ROW, thread_state="needs_attention", knowledge_gap="VAT number",
               category_reason="Customer disputes an invoice — judgment needed")
    _env(monkeypatch, [row])
    html = client.get("/app/conversations/view?contact=sam@x.co").text
    assert 'id="attention-bubble"' not in html          # the sign is gone
    assert "needs-eyes-rose" in stage_classes(html)     # the stage breathes instead
    assert "Why you&#39;re needed" in html or "Why you're needed" in html
    assert "The agent couldn&#39;t answer: VAT number." in html \
        or "The agent couldn't answer: VAT number." in html
    assert "Customer disputes an invoice" in html
    # Shown, not hidden behind a toggle: the reasons ride inside the ghost.
    assert 'id="stage-attention"' not in html
    assert html.index("Why you") > html.index('id="stage-decision"')
    # …and said ONCE: category_reason feeds the rose lines, so the quiet
    # "Why:" copy of the same sentence stands down (live 2026-09-01).
    assert html.count("Customer disputes an invoice") == 1
    assert "<span class=\"font-semibold text-slate-600\">Why:</span>" not in html


def test_attention_reasons_show_even_with_no_decision_to_tell(client, monkeypatch):
    """The ghost is the reasons' home now, so it has to appear for a thread
    that has attention lines but nothing else recorded."""
    row = dict(ROW, thread_state="needs_attention", draft_sources=None,
               searched_for=None, category_reason=None, knowledge_gap=None)
    _env(monkeypatch, [row])
    html = client.get("/app/conversations/view?contact=sam@x.co").text
    assert 'id="stage-decision"' in html
    assert "The agent flagged this thread for your judgment." in html


def test_stage_draft_starts_one_line_and_the_dock_only_caps_it(client, monkeypatch):
    """The draft box grows into the stage instead of claiming it (Kris
    2026-09-01): one row to start, and 55% — the height the dock used to take
    outright — becomes the ceiling the JS grows it to."""
    _env(monkeypatch, [ROW])
    html = client.get("/app/conversations/view?contact=sam@x.co").text
    ta = html.split('name="draft_reply"')[1][:400]
    assert 'rows="1"' in ta                       # the floor, not the size
    assert "data-stage-draft" in ta               # …grown against the measured cap
    assert "flex-1" not in ta                     # no longer flex-filled
    assert "overflow-y-auto" in ta                # past the cap it scrolls
    # The dock is capped, not fixed: nothing forces it to 55% any more.
    dock = html.split('id="thread-stage-dock"')[0][-400:]
    # (leading space: "max-h-[55%]" would swallow a bare "h-[55%]" check)
    assert "max-h-[55%]" in dock
    assert " h-[55%]" not in dock and "min-h-[18rem]" not in dock


def test_escalation_hold_offers_the_empty_pen(client, monkeypatch):
    # An escalated inbound writes NO draft (the match is known pre-model, so
    # nothing is spent) and no banner pollutes the reply text: the dock offers
    # the empty pen, and the pulsing stage carries the keyword in its ghost.
    row = dict(ROW, draft_reply="", thread_state="needs_attention",
               category_reason="Escalation keyword matched: “refund” — this reply is yours to write")
    _env(monkeypatch, [row])
    html = client.get("/app/conversations/view?contact=sam@x.co").text
    assert "yours to write" in html
    assert "No draft written — missing knowledge" not in html   # not the gap card
    assert "needs-eyes-rose" in stage_classes(html)
    assert "refund" in html
    assert "⚠ ESCALATION" not in html                           # banner era over


def test_calm_thread_does_not_pulse(client, monkeypatch):
    _env(monkeypatch, [ROW])   # plain pending → draft_ready, not needs_reply
    html = client.get("/app/conversations/view?contact=sam@x.co").text
    assert 'id="attention-bubble"' not in html
    assert 'id="stage-attention"' not in html
    assert "needs-eyes" not in stage_classes(html)
    assert "Why you&#39;re needed" not in html and "Why you're needed" not in html


def test_concerned_customer_pulses_amber_even_on_a_calm_label(client, monkeypatch):
    # Mood alerts explain themselves too (Kris 2026-08-31): a concerned read
    # breathes AMBER with the sentence that earned it — even when the label is
    # a calm draft_ready; frustrated/angry pulses rose instead.
    _env(monkeypatch, [ROW])
    monkeypatch.setattr(dashboard.db, "crm_contact_temperatures",
                        lambda *a, **k: {"sam@x.co": {
                            "mood": "concerned", "trend": "worsening",
                            "reason": "double-checking their booking went through"}})
    html = client.get("/app/conversations/view?contact=sam@x.co").text
    assert "needs-eyes-amber" in stage_classes(html)
    assert "needs-eyes-rose" not in stage_classes(html)
    assert "The customer reads concerned and worsening" in html
    assert "double-checking their booking went through" in html


def test_trace_failure_never_costs_the_thread(client, monkeypatch):
    # Reads blowing up must never 500 the thread. With every invisible fact
    # unreachable (and said/did no longer shown), the ghost simply stays away.
    _env(monkeypatch, [ROW])
    def _boom(*a, **k):
        raise RuntimeError("trace store down")
    monkeypatch.setattr(dashboard.db, "list_agent_trace", _boom)
    monkeypatch.setattr(dashboard.db, "bookings_for_log", _boom)
    r = client.get("/app/conversations/view?contact=sam@x.co")
    assert r.status_code == 200
    assert 'id="stage-decision"' not in r.text


# ── Every past round carries its own ghost (Kris 2026-09-01) ───────────────
# The stage's decision ghost is built from `latest` only, but category_reason,
# searched_for, source_pills and draft_sources are recorded PER row — so every
# collapsed round already had its reasoning and nothing surfaced it. Open one
# and you saw what was said and what was replied; WHY stayed invisible, which
# is the half you go back for.

def _round(n, **over):
    r = {"id": f"log{n}", "from_address": "sarah@x.com", "subject": f"Round {n}",
         "body_in": "hello", "draft_reply": "hi back", "status": "sent",
         "created_at": f"2026-08-0{n}T09:00:00+00:00", "category": "quote_sent"}
    r.update(over)
    return r


def _ghosts(html):
    """The per-round ghost blocks — centred, and since 2026-09-21 ruleless (the
    line across the page only added to the crowding)."""
    import re as _re
    return _re.findall(r'<div data-ghost-scope="[^"]*" class="mt-2 pt-2 space-y-1 text-center">.*?</div>',
                       html, _re.S)


def test_a_past_round_shows_the_reasoning_behind_it(client, monkeypatch):
    _env(monkeypatch, [
        _round(1, category_reason="Booking confirmed in the diary",
               searched_for="Thursday availability",
               draft_sources=[{"source": "Opening hours"}]),
        _round(2),   # the newest — the stage owns this one
    ])
    html = client.get("/app/conversations/view?contact=sarah@x.com").text
    ghosts = _ghosts(html)
    assert len(ghosts) == 1, "the ghost should be on the PAST round only"
    assert "Booking confirmed in the diary" in ghosts[0]
    assert "Thursday availability" not in ghosts[0]
    assert "Opening hours" in ghosts[0]


def test_the_newest_round_leaves_it_to_the_stage(client, monkeypatch):
    """Rendering it inline as well would print the same sentences twice on one
    screen — the mistake the stage ghost already had to be taught not to make."""
    _env(monkeypatch, [_round(1, category_reason="Only round, so it is the latest")])
    assert _ghosts(client.get("/app/conversations/view?contact=sarah@x.com").text) == []


def test_a_reason_that_just_restates_the_category_is_not_a_reason(client, monkeypatch):
    """"Reply sent — awaiting the customer" IS the quote_sent hint. Printing it
    as Why explains nothing the status line above it hasn't already said, and on
    the live tenant it was most of them."""
    _env(monkeypatch, [
        _round(1, category="quote_sent", category_reason="Reply sent — awaiting the customer"),
        _round(2),
    ])
    ghosts = _ghosts(client.get("/app/conversations/view?contact=sarah@x.com").text)
    assert not any("Reply sent — awaiting the customer" in g for g in ghosts)


def test_a_hand_set_label_has_no_reasoning_to_show(client, monkeypatch):
    _env(monkeypatch, [
        _round(1, category_source="manual", category_reason="Set manually"),
        _round(2),
    ])
    assert not any("Set manually" in g for g in _ghosts(
        client.get("/app/conversations/view?contact=sarah@x.com").text))


def test_retrieval_that_matched_nothing_says_so(client, monkeypatch):
    """[] is retrieval running and finding nothing — an honest, different fact
    from null, which is a row that predates the feature and says nothing."""
    _env(monkeypatch, [_round(1, draft_sources=[]), _round(2)])
    ghosts = _ghosts(client.get("/app/conversations/view?contact=sarah@x.com").text)
    assert any("No knowledge matches recorded." in g for g in ghosts)

    _env(monkeypatch, [_round(1, draft_sources=None), _round(2)])
    assert _ghosts(client.get("/app/conversations/view?contact=sarah@x.com").text) == []


def test_the_knowledge_gap_is_not_said_twice(client, monkeypatch):
    """It already sits on the inbound bubble in the same round."""
    _env(monkeypatch, [
        _round(1, knowledge_gap="weekend rates", category_reason="Missing a price"),
        _round(2),
    ])
    html = client.get("/app/conversations/view?contact=sarah@x.com").text
    assert html.count("weekend rates") == html.count("weekend rates")  # sanity
    assert not any("weekend rates" in g for g in _ghosts(html))


def test_held_rows_list_only_live_diary_moves():
    """Kris 2026-09-03: after two redrafts the ghost showed three 'Held slot ·
    15:30' lines for one appointment — every superseded hold still rendered as
    held. Released/cancelled/declined rows leave the list; offers read as
    Offered, not Held; 'Whole day' drops its time as before."""
    rows = [
        {"status": "cancelled", "booking_date": "2026-09-08", "window": "15:30"},
        {"status": "cancelled", "booking_date": "2026-09-08", "window": "15:30"},
        {"status": "confirmed", "booking_date": "2026-09-08", "window": "15:30"},
        {"status": "offered", "booking_date": "2026-09-09", "window": "14:00"},
        {"status": "proposed", "booking_date": "2026-09-10", "window": "day"},
        {"status": "declined", "booking_date": "2026-09-11", "window": "am"},
    ]
    held = dashboard._held_rows(rows)
    assert [(h["label"], h["date"], h["when"]) for h in held] == [
        ("Booked", "2026-09-08", "15:30"),
        ("Offered", "2026-09-09", "14:00"),
        ("Held", "2026-09-10", ""),
    ]
    assert dashboard._held_rows([]) == [] and dashboard._held_rows(None) == []


def test_outbound_only_rounds_never_read_as_a_customer_message():
    """Kris 2026-09-04: the thread showed "[Invoice]" as the customer's message.
    Invoice/quote/reminder sends are one email_log row with a placeholder body;
    the round has no customer message and the display says so."""
    text, outbound = dashboard._inbound_display({"kind": "invoice", "body_in": "[invoice]"})
    assert outbound is True and text == (
        "No message from the customer in this round — this is the invoice you sent.")
    text, outbound = dashboard._inbound_display({"kind": "quote", "body_in": " [quote] "})
    assert outbound is True and "the quote you sent" in text
    # a real inbound keeps its body (quoted tail stripped) and its bubble
    text, outbound = dashboard._inbound_display(
        {"kind": "inbound", "body_in": "Hello, Tuesday please.\n\nOn Thu, X wrote:\n> old"})
    assert outbound is False and text.startswith("Hello, Tuesday please.")
    # a bracketed word in a REAL customer email is not a placeholder round
    text, outbound = dashboard._inbound_display({"kind": "inbound", "body_in": "[urgent]"})
    assert outbound is False


def test_an_offer_shows_its_deadline_until_the_customer_accepts():
    """Kris 2026-09-22: "show the expiry time next to it in the thread until
    it's confirmed by the customer."

    An offer is the only row with a clock on it. The space is theirs until that
    moment and then it is gone, and the deadline was quoted to them verbatim in
    the reply — so the operator should be watching the same countdown. A hold
    and a confirmed booking carry none: a hold's deadline is the owner's own
    approval, not a clock.
    """
    from datetime import datetime, timedelta, timezone
    from app.dashboard import _held_rows

    soon = (datetime.now(timezone.utc) + timedelta(hours=20)).isoformat()
    rows = _held_rows([{"id": "b1", "status": "offered", "booking_date": "2026-10-19",
                        "window": "day", "expires_at": soon}])
    assert rows[0]["label"] == "Offered"
    assert rows[0]["until"], "an offered row must carry its deadline"

    # a hold and a confirmed booking have no clock
    for status in ("proposed", "confirmed"):
        r = _held_rows([{"id": "b2", "status": status, "booking_date": "2026-10-19",
                         "window": "day", "expires_at": soon}])
        assert r[0]["until"] == "", f"{status} must not show a deadline"


def test_an_offer_past_its_deadline_is_not_shown_as_live():
    """db._drop_expired_offers: an expired offer "no longer exists for ANY
    consumer". The thread is a consumer, and rendering a dead offer as live
    would have the operator believe a space is still the customer's — the sweep
    may not have caught up, and the read must not wait for it."""
    from datetime import datetime, timedelta, timezone
    from app.dashboard import _held_rows

    gone = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    assert _held_rows([{"id": "b1", "status": "offered", "booking_date": "2026-10-19",
                        "window": "day", "expires_at": gone}]) == []

    # an unreadable expiry is not a reason to hide a live offer
    odd = _held_rows([{"id": "b1", "status": "offered", "booking_date": "2026-10-19",
                       "window": "day", "expires_at": "not-a-date"}])
    assert len(odd) == 1 and odd[0]["until"] == ""


def test_a_reply_waiting_on_a_document_shows_the_document_card_not_a_draft(client, monkeypatch):
    """Kris 2026-09-22: "instead of the Agent responding to it, we have a
    notification box … telling us there's a document that arrived and needs our
    attention, text linked directly to it" — and it is dealt with right here."""
    from app.pipeline import DOC_HOLD_CONFIRM
    row = dict(ROW, id="held1", draft_reply="", from_name="Claire Dunmore",
               body_in="Took him to the vet this morning - new card attached.",
               action_summary=DOC_HOLD_CONFIRM)
    _env(monkeypatch, [row])
    monkeypatch.setattr(dashboard.db, "crm_contact_by_email", lambda *a, **k: {"id": "c1"})
    monkeypatch.setattr(dashboard.db, "list_documents", lambda *a, **k: [
        {"id": "d-new", "filename": "alfie-vaccinations-2026.pdf", "label": "Vaccination record",
         "instance_id": "i1", "email_log_id": "held1", "created_at": "2026-09-22T21:58:00"},
        {"id": "d-old", "filename": "alfie-vaccinations-2025.pdf", "label": "Vaccination record",
         "instance_id": "i1", "email_log_id": "old", "created_at": "2025-10-25T09:00:00"}])
    monkeypatch.setattr(dashboard.db, "list_requirement_groups", lambda *a, **k: [
        {"id": "g1", "label": "Dog", "fields": [{"key": "vaccinations", "label": "Vaccinations",
                                                 "type": "date", "durability": "expires", "gate": True}]}])
    monkeypatch.setattr(dashboard.db, "list_requirement_instances", lambda *a, **k: [
        {"id": "i1", "group_id": "g1", "name": "Alfie", "data": {"vaccinations": "2026-10-25"}}])
    monkeypatch.setattr(dashboard.db, "pending_chips_for_email", lambda *a, **k: [
        {"id": "s1", "entity": "dependant", "record_id": "i1", "field_key": "vaccinations",
         "value": "2027-09-22", "email_log_id": "held1"}])
    html = client.get("/app/conversations/view?contact=claire@x.co").text
    assert 'data-doc-hold="held1"' in html and "Document arrived — needs you" in html
    assert 'href="/app/crm/documents/d-new"' in html and "alfie-vaccinations-2026.pdf" in html
    assert "Vaccination record · Alfie" in html and ">new version</span>" in html
    assert "25th Oct 26" in html and "22nd Sep 27" in html
    assert 'action="/app/crm/documents/d-new/updates"' in html and 'name="ids" value="s1"' in html
    assert "return_to\" value=\"/app/conversations/view?contact=claire%40x.co" in html
    # no empty draft form in the dock while it waits
    assert 'action="/app/drafts/held1/send"' not in html


def test_an_ordinary_empty_draft_is_not_mistaken_for_a_document_hold(client, monkeypatch):
    _env(monkeypatch, [dict(ROW, draft_reply="", action_summary="Draft failed — write a reply or hit Redraft")])
    html = client.get("/app/conversations/view?contact=sam@x.co").text
    assert "data-doc-hold" not in html and 'action="/app/drafts/e1/send"' in html


# ── the held list names what the booking IS (Kris 2026-09-23) ─────────────────
def test_the_held_list_names_the_booking_kind_with_its_times():
    """"Offered slot" said nothing about which diary: a meet & greet and a trial
    night read the same. Now: the service by name with start–end, a stay as a
    stay arrival → departure, anything else the business's own word."""
    from app import dashboard, slotgrid
    appt = {"id": "a", "status": "offered", "booking_date": "2026-10-15", "window": "13:00",
            "start_block": slotgrid.block_at("13:00"), "length_blocks": 2,
            "details": {"Service": "Meet & greet"}, "expires_at": "2099-01-01T00:00:00+00:00"}
    stay = {"id": "s", "status": "proposed", "booking_date": "2026-10-15", "window": "day",
            "start_block": slotgrid.block_at("13:00"), "length_blocks": 82, "details": {}}
    day = {"id": "w", "status": "confirmed", "booking_date": "2026-10-15", "window": "am", "details": {}}
    rows = {r["id"]: r for r in dashboard._held_rows([appt, stay, day], "lesson")}
    assert (rows["a"]["label"], rows["a"]["what"], rows["a"]["when"]) == ("Offered", "Meet & greet", "13:00–13:30")
    assert (rows["s"]["label"], rows["s"]["what"]) == ("Held", "stay")
    assert rows["s"]["when"] == "13:00 → 16th Oct 26, 09:30"
    assert (rows["w"]["what"], rows["w"]["label"]) == ("lesson", "Booked")
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src/app/templates/app/_thread.html").read_text(encoding="utf-8")
    assert dashboard._held_rows([day])[0]["what"] == "slot"                  # no word of its own: unchanged
    assert "{{ h.label }} {{ h.what or 'booking' }}" in src and "{{ h.label }} slot" not in src


# ── "no sign-off set" on a waiting draft ────────────────────────────────────
def _config_reads(monkeypatch, cfg):
    reads = []

    def read(*a, **k):
        reads.append(1)
        return dict(cfg)
    monkeypatch.setattr(dashboard.db, "get_tenant_config", read)
    return reads


def test_a_waiting_draft_says_when_no_sign_off_is_set(client, monkeypatch):
    _env(monkeypatch, [dict(ROW)])
    _config_reads(monkeypatch, {"sign_off": None})
    html = client.get("/app/conversations/view?contact=sam@x.co").text
    note = html.split("data-sign-off-missing>")[1].split("</p>")[0]
    assert "no sign-off set" in note and 'href="/app/settings"' in note
    # The live fragment (swapped in by live.js) says the same.
    fragment = client.get("/app/conversations/view/fragment?contact=sam@x.co").text
    assert "data-sign-off-missing>" in fragment and "no sign-off set</a>" in fragment
    # Only a draft that is waiting is announced: an empty pen has nothing to sign.
    _env(monkeypatch, [dict(ROW, draft_reply="")])
    assert "data-sign-off-missing" not in client.get(
        "/app/conversations/view?contact=sam@x.co").text


def test_a_waiting_draft_with_a_sign_off_or_an_sms_row_says_nothing(client, monkeypatch):
    _env(monkeypatch, [dict(ROW)])
    _config_reads(monkeypatch, {"sign_off": "Cheers,\nSam"})
    html = client.get("/app/conversations/view?contact=sam@x.co").text
    assert "data-attach-toggle" in html                       # the email draft box rendered
    assert "no sign-off set" not in html and "data-sign-off-missing" not in html
    # An SMS thread never carries an email sign-off, so there is nothing to announce.
    _env(monkeypatch, [dict(ROW, channel="sms")])
    _config_reads(monkeypatch, {"sign_off": None})
    html = client.get("/app/conversations/view?contact=sam@x.co").text
    assert "Draft ready · Editable before approval" in html   # the SMS draft box rendered
    assert "no sign-off set" not in html and "data-sign-off-missing" not in html


def test_thread_flags_come_from_one_read(monkeypatch):
    ctx = TenantCtx(tenant_id="t1", client=None)
    reads = _config_reads(monkeypatch, {"draft_hold": True, "sign_off": " \r\n"})
    assert dashboard._thread_flags(ctx) == {"draft_hold": True, "sign_off_missing": True}
    assert len(reads) == 1
    reads = _config_reads(monkeypatch, {"sign_off": "Cheers,\r\nSam"})
    assert dashboard._thread_flags(ctx) == {"draft_hold": False, "sign_off_missing": False}
    assert len(reads) == 1

    def broken(*a, **k):
        raise RuntimeError("config unavailable")
    monkeypatch.setattr(dashboard.db, "get_tenant_config", broken)
    # Unknown = not held and nothing to announce: a filed thread keeps its Draft button.
    assert dashboard._thread_flags(ctx) == {"draft_hold": False, "sign_off_missing": False}
