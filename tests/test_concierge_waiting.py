"""Waiting cards: a card nobody answered waits as a chip until it's answered.

Kris 2026-09-23 (after "Put Bella's food back to 300g" was set up, then pushed
aside by room chatter before anyone said yes): "We should create a pending
chip, for times like that when something gets ignored. It can be resurfaced
at any time until confirmation."
"""
import asyncio
from proposal_receipts import execution_result
from types import SimpleNamespace

from app import concierge_actions, concierge_routes, db

ACTION = {"kind": "update_dependant", "name": "Bella", "title": "Feeding instructions",
          "content": "300g a day"}


class _Q:
    """Just enough of the supabase query chain for list_waiting_proposals."""
    def __init__(self, rows, seen):
        self.rows, self.seen = rows, seen

    def __getattr__(self, name):
        def call(*a, **k):
            self.seen.setdefault(name, []).append(a)
            return self
        return call

    def execute(self):
        return SimpleNamespace(data=self.rows)


def _rows(monkeypatch, rows):
    seen = {}
    client = SimpleNamespace(table=lambda name: _Q(rows, seen))
    return db.list_waiting_proposals("t1", "u1", client=client), seen


def _row(pid, summary, status, at, action=ACTION):
    return {"id": pid, "kind": "update_dependant", "summary": summary, "status": status,
            "created_at": at, "user_id": "u1", "payload": {"_action": action} if action else {}}


def test_unanswered_cards_wait_newest_first_one_per_change(monkeypatch):
    out, seen = _rows(monkeypatch, [
        _row("p3", "Bella back to 300g", "pending", "2026-09-23T17:27:19"),
        _row("p2", "Bella back to 300g", "superseded", "2026-09-23T17:20:00"),   # same change, older
        _row("p1", "Bruno: add a note", "superseded", "2026-09-23T17:10:00"),
    ])
    assert [w["proposal_id"] for w in out] == ["p3", "p1"]
    # only this person's cards, in the statuses that can still be waiting
    assert ("user_id", "u1") in seen["eq"]
    assert ("status", ["pending", "superseded", "executed", "executing", "unknown"]) in seen["in_"]


def test_a_change_confirmed_since_is_not_waiting(monkeypatch):
    out, _ = _rows(monkeypatch, [
        _row("p9", "Bella back to 300g", "executed", "2026-09-23T17:30:00"),
        _row("p3", "Bella back to 300g", "pending", "2026-09-23T17:27:19"),
    ])
    assert out == []


def test_a_card_without_its_request_cannot_come_back(monkeypatch):
    out, _ = _rows(monkeypatch, [_row("p1", "Old card", "superseded", "2026-09-22T10:00:00", action=None)])
    assert out == []


def test_at_most_five(monkeypatch):
    out, _ = _rows(monkeypatch, [_row(f"p{i}", f"Change {i}", "superseded", f"2026-09-23T10:0{i}:00")
                                 for i in range(8)])
    assert len(out) == 5


# ── the brain can ask what is awaiting approval (14 Oct, Kris: "outstanding" is the
#    day's work, "awaiting approval" is the cards) ────────────────────────────────
def unmark(text):
    return (text or "").replace("«", "").replace("»", "")


async def concierge_data_execute(member, user_id=None):
    from app import concierge_data
    return await concierge_data.execute("t1", None, "list_awaiting_approval", {}, full_qa=False,
                                        member=member, user_id=user_id)


def test_the_brain_can_list_the_cards_awaiting_approval(monkeypatch):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    cards = [_row("p3", "Block the diary on 16th Oct", "pending", (now - timedelta(minutes=1)).isoformat()),
             _row("p1", "Add a note to Pat's record", "superseded", (now - timedelta(hours=1)).isoformat())]
    seen = {}
    def ledger_cards(tenant_id, user, *, days, client=None):
        seen.update(tenant=tenant_id, user=user)
        return [dict(c) for c in cards]
    monkeypatch.setattr(db, "list_ledger_proposals", ledger_cards)
    monkeypatch.setattr(db, "list_ledger_concierge_tasks", lambda *a, **k: [])
    monkeypatch.setattr(db, "list_waiting_drafts", lambda *a, **k: [])
    out = asyncio.run(concierge_data_execute(member={"user_id": "u1", "role": "manager"}))
    assert out["total"] == 2
    assert [(unmark(c["title"]), c["state"]) for c in out["items"]] == [
        ("Block the diary on 16th Oct", "Needs you"), ("Add a note to Pat's record", "Parked")]
    assert "mean nothing has happened" in out["how_to_read"]
    assert seen == {"tenant": "t1", "user": "u1"}                      # their own cards only
    # An account that predates People has no member row: it is read as the signed-in user, never the whole account.
    seen.clear()
    out = asyncio.run(concierge_data_execute(member=None, user_id="u1"))
    assert out["total"] == 2 and seen["user"] == "u1"
    assert asyncio.run(concierge_data_execute(member=None))["error"] == "unknown_asker"
    # And nothing waiting says so plainly.
    cards.clear()
    empty = asyncio.run(concierge_data_execute(member={"user_id": "u1", "role": "owner"}))
    assert empty["items"] == [] and empty["total"] == 0


def test_the_approvals_tool_is_last_in_the_catalogue_and_on_both_tiers(monkeypatch):
    from app import concierge_data
    # LAST: the render baselines key their debt by tool position.
    assert concierge_data.TOOLS[-1]["name"] == "list_awaiting_approval"
    for full_qa in (True, False):
        assert "list_awaiting_approval" in {t["name"] for t in concierge_data.tools_for(full_qa)}
    assert concierge_data.tool_door("list_awaiting_approval") is None
    assert "list_awaiting_approval" not in concierge_data.PRO_TOOLS
    assert "list_awaiting_approval" not in concierge_data.WHOLE_BOOK_TOOLS
    assert "list_awaiting_approval" in concierge_data._MEMBER_AWARE
    seen = {}
    def cards(tenant_id, user_id, *, days, client=None):
        seen.update(tenant=tenant_id, user=user_id)
        return [{**_row("p3", "Block the diary on 16th Oct", "pending", "2999-01-01T00:00:00+00:00"), "user_id": user_id}]
    monkeypatch.setattr(db, "list_ledger_proposals", cards)
    monkeypatch.setattr(db, "list_ledger_concierge_tasks", lambda *a, **k: [])
    monkeypatch.setattr(db, "list_waiting_drafts", lambda *a, **k: [])
    out = asyncio.run(concierge_data.execute("t1", None, "list_awaiting_approval", {},
                                             full_qa=False, member={"user_id": "u9", "role": "owner"}))
    assert out["total"] == 1 and unmark(out["items"][0]["title"]) == "Block the diary on 16th Oct"
    assert seen == {"tenant": "t1", "user": "u9"}


def test_every_new_card_keeps_its_request(monkeypatch):
    seen = {}
    monkeypatch.setitem(concierge_actions._STAGERS, "update_dependant",
                        lambda t, c, a: ("Update Bella", {"k": 1}, [], None))
    monkeypatch.setattr(concierge_actions.db, "create_concierge_proposal",
                        lambda t, kind, summary, payload, user_id=None, action=None, client=None:
                        seen.update(action=action) or "p-new")
    concierge_actions.stage("t1", None, dict(ACTION), full_qa=False, scheduling_on=False)
    assert seen["action"] == ACTION


# ── bringing one back ─────────────────────────────────────────────────────────
def _ctx():
    return SimpleNamespace(tenant_id="t1", client=None, user_id="u1")


def _stub_route(monkeypatch, row, stage):
    moved = []
    monkeypatch.setattr(concierge_routes.db, "get_concierge_proposal", lambda pid, t, client=None: row)
    monkeypatch.setattr(concierge_routes.db, "visible_modules", lambda t, client=None: {"crm", "scheduling"})
    monkeypatch.setattr(concierge_routes.db, "set_concierge_proposal_status",
                        lambda pid, t, status, from_statuses=None, client=None: moved.append((pid, status)) or True)
    monkeypatch.setattr(concierge_routes, "_asker", lambda ctx: None)
    monkeypatch.setattr(concierge_routes.trace, "emit", lambda *a, **k: None)
    monkeypatch.setattr(concierge_routes.concierge_actions, "stage", stage)
    return moved


def test_bringing_one_back_rebuilds_it_from_todays_data(monkeypatch):
    staged = {}

    def stage(t, c, action, **k):
        staged.update(action=action, **k)
        return {"proposal_id": "p-fresh", "kind": "update_dependant", "summary": "Update Bella…",
                "details": [], "expires_s": 300}
    moved = _stub_route(monkeypatch, _row("p3", "Bella back to 300g", "pending", "x"), stage)
    out = concierge_routes.business_resurface(
        concierge_routes.BusinessWaitingRequest(proposal_id="p3"), _ctx())
    assert out["ok"] is True and out["proposal"]["proposal_id"] == "p-fresh"
    assert staged["action"] == ACTION and staged["full_qa"] is True and staged["scheduling_on"] is True
    assert moved == [("p3", "resurfaced")]


def test_a_resurfaced_task_card_names_its_approval_mode_like_any_other(monkeypatch):
    # The card of a background task comes back from its prepared card, not from stage():
    # it must say how it is authorised, as every staged card does.
    from app import concierge_delegation
    row = dict(_row("p3", "Block the diary on 16th Oct", "superseded", "x"), kind="block_date")
    saved = {"id": "p3", "kind": "block_date", "summary": "Block the diary on 16th Oct",
             "payload": {"_prepared_card": {"details": ["Date: 2026-10-16"], "draft_text": None}}}
    monkeypatch.setattr(concierge_routes.db, "get_concierge_proposal", lambda pid, t, client=None: row)
    monkeypatch.setattr(concierge_delegation, "binding", lambda r: {"task_id": "k1", "revision": 1})
    monkeypatch.setattr(concierge_delegation, "enabled", lambda: True)
    monkeypatch.setattr(concierge_delegation, "transition", lambda *a, **k: {"ok": True, "proposal": saved})
    monkeypatch.setattr(concierge_routes, "_proposal_refusal", lambda ctx, r: None)
    out = concierge_routes.business_resurface(
        concierge_routes.BusinessWaitingRequest(proposal_id="p3"), _ctx())
    assert out["ok"] is True
    assert out["proposal"] == {"proposal_id": "p3", "kind": "block_date", "summary": "Block the diary on 16th Oct",
                               "details": ["Date: 2026-10-16"], "draft_text": None,
                               "expires_s": concierge_actions.PROPOSAL_TTL_S, "approval": "confirm"}


def test_when_today_says_no_it_says_why_and_the_chip_goes(monkeypatch):
    def stage(*a, **k):
        raise concierge_actions.ActionError("Bella's Feeding instructions is already '300g a day'.")
    moved = _stub_route(monkeypatch, _row("p3", "Bella back to 300g", "superseded", "x"), stage)
    out = concierge_routes.business_resurface(
        concierge_routes.BusinessWaitingRequest(proposal_id="p3"), _ctx())
    assert out == {"ok": False, "error": "That can't be done now: Bella's Feeding instructions is already '300g a day'."}
    assert moved == [("p3", "resurfaced"), ("p3", "stale")]


def test_an_answered_card_cannot_come_back(monkeypatch):
    for status in ("executed", "cancelled", "dismissed", "resurfaced"):
        moved = _stub_route(monkeypatch, _row("p3", "x", status, "x"),
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("staged")))
        out = concierge_routes.business_resurface(
            concierge_routes.BusinessWaitingRequest(proposal_id="p3"), _ctx())
        assert out["ok"] is False and moved == []


def test_someone_elses_card_cannot_come_back(monkeypatch):
    row = dict(_row("p3", "x", "pending", "x"), user_id="someone-else")
    moved = _stub_route(monkeypatch, row, lambda *a, **k: (_ for _ in ()).throw(AssertionError("staged")))
    out = concierge_routes.business_resurface(
        concierge_routes.BusinessWaitingRequest(proposal_id="p3"), _ctx())
    assert out["ok"] is False and moved == []


def test_the_panel_shows_and_refreshes_the_chips():
    js = open("src/app/static/concierge_panel.js", encoding="utf-8").read()
    tpl = open("src/app/templates/app/_concierge_panel.html", encoding="utf-8").read()
    assert 'id="cz-waiting"' in tpl
    assert "fetch('/concierge/business/waiting'" in js
    assert "postJson('/concierge/business/resurface'" in js
    assert "postJson('/concierge/business/dismiss'" in js
    # the voice screen shows them when no card is up; a set-aside card waits at once
    assert "var wrow = waitingRow(true);" in js   # the badge opens the list
    assert "delete justShown[voiceStage.proposal.proposal_id]" in js
    # answered (tap or voice) → the list refreshes
    assert "loadWaiting();   // answered: it isn't waiting any more" in js


# ── drafts wait too (Kris 2026-09-23: "it didn't put the draft into pending?") ──
def test_a_voice_draft_still_awaiting_approval_waits(monkeypatch):
    rows = [{"id": "log-9", "from_address": "dan@x.com", "contact_name": "Daniel Okafor",
             "subject": "Thanks for the vaccination info", "draft_reply": "Hi Daniel…",
             "created_at": "2026-09-23T19:58:49"}]
    seen = {}
    client = SimpleNamespace(table=lambda name: _Q(rows, seen))
    out = db.list_waiting_drafts("t1", client=client)
    assert out == [{"type": "draft", "log_id": "log-9", "contact": "dan@x.com", "name": "Daniel Okafor",
                    "subject": "Thanks for the vaccination info", "text": "Hi Daniel…", "status": "pending",
                    "created_at": "2026-09-23T19:58:49",
                    "href": "/app/conversations/view?contact=dan%40x.com",
                    "summary": "Email to Daniel Okafor — Thanks for the vaccination info"}]
    assert ("kind", "owner_message") in seen["eq"] and ("status", "pending") in seen["eq"]


def test_the_waiting_route_lists_drafts_beside_cards(monkeypatch):
    monkeypatch.setattr(concierge_routes.db, "get_member", lambda *a, **k: None)  # the owner
    monkeypatch.setattr(concierge_routes.db, "list_waiting_proposals",
                        lambda t, u, client=None, **k: [{"proposal_id": "p1", "summary": "A card"}])
    monkeypatch.setattr(concierge_routes.db, "list_waiting_drafts",
                        lambda t, client=None: [{"type": "draft", "log_id": "log-9", "summary": "Email to Dan"}])
    out = concierge_routes.business_waiting(_ctx())
    assert [w.get("summary") for w in out["waiting"]] == ["A card", "Email to Dan"]


def test_a_draft_chip_brings_the_card_back_and_x_skips_it():
    js = open("src/app/static/concierge_panel.js", encoding="utf-8").read()
    r = js[js.index("function resurface(w, btn) {"):][:1400]
    assert "if (w.type === 'draft') {" in r and "var node = draftCardNode(w);" in r
    assert "voiceStage.draft = node;" in r
    d = js[js.index("function dismissWaiting(w) {"):][:500]
    assert "'/app/drafts/' + encodeURIComponent(w.log_id) + '/skip'" in d
    # (The chip now names what it is: recovery, draft, link or approval.)
    assert "w.type === 'draft' ? '✉ Draft awaiting approval: ' : w.type === 'link' ? '£ Draft for review: ' : '⏳ Awaiting approval: '" in js
    v = open("src/app/static/voice-live.js", encoding="utf-8").read()
    assert 'const glue = (/[.!?]$/.test(spokenSoFar) && /^[A-Za-z]/.test(msg.text)) ? " " : "";' in v


def test_the_voice_screen_shows_a_count_and_opens_the_list_on_tap():
    # Kris 2026-10-14: put the pending count beside the same-size Debrief control
    # at the top right. Once tapped, the list stays at the bottom of the screen.
    js = open("src/app/static/concierge_panel.js", encoding="utf-8").read()
    tpl = open("src/app/templates/app/_concierge_panel.html", encoding="utf-8").read()
    assert "onMount: mountBadge," in js
    badge = js[js.index("function mountBadge(root) {"):][:1500]
    assert "if (root.id !== 'voice-gptlive-root') return;" in badge
    assert "waitingOpen = !waitingOpen;" in badge and "voiceLiveInstance.refreshStage();" in badge
    assert "waitBadge.hidden = !n;" in js                       # nothing waiting → no circle
    assert "else if (waitingOpen) {" in js                       # the list only once tapped
    assert "waitingOpen = false;" in js and "// A draft invoice waits" in js   # picking one closes it
    assert "#voice-gptlive-root .cz-badges { position: absolute; top: max(0.9rem, env(safe-area-inset-top)); right: 0.8rem; left: auto;" in tpl
    assert "width: 2.75rem; height: 2.75rem" in tpl
    assert "#voice-live-root .cz-debrief, #voice-gptlive-root .cz-debrief" in tpl
    assert "#voice-live-root .cz-wait-list { flex-direction: column;" in tpl


# ── the done list (Kris 2026-09-24: "another counter chip, for done actions …
#    a list of what's happened. Not a whole thread of chat.") ──────────────────
def test_done_cards_carry_their_outcome_and_where_they_landed():
    rows = [{"id": "p7", "kind": "change_stay", "summary": "Extend Bella's stay", "user_id": "u1",
             "executed_at": "2026-09-24T09:15:00", "created_at": "2026-09-24T09:14:00",
             "result": {"summary": "Done — Bella now leaves Fri.", "link": {"label": "Open the diary", "href": "/app/diary?date=2026-11-06"}}}]
    rows[0]["result"] = execution_result("p7", state="completed", **rows[0]["result"])
    seen = {}
    client = SimpleNamespace(table=lambda name: _Q(rows, seen))
    out = db.list_done_proposals("t1", "u1", client=client)
    assert out == [{"type": "card", "proposal_id": "p7", "kind": "change_stay", "summary": "Extend Bella's stay",
                    "outcome": "Done — Bella now leaves Fri.", "done_at": "2026-09-24T09:15:00",
                    "href": "/app/diary?date=2026-11-06"}]
    assert ("status", "executed") in seen["eq"] and ("user_id", "u1") in seen["eq"]


def test_sent_voice_emails_are_done_too():
    rows = [{"id": "log-9", "from_address": "dan@x.com", "contact_name": "Daniel Okafor",
             "subject": "Thanks", "sent_at": "2026-09-24T09:20:00", "created_at": "2026-09-24T09:19:00"}]
    seen = {}
    client = SimpleNamespace(table=lambda name: _Q(rows, seen))
    out = db.list_sent_drafts("t1", client=client)
    assert out[0]["type"] == "sent" and out[0]["done_at"] == "2026-09-24T09:20:00"
    assert out[0]["summary"] == "Email to Daniel Okafor — Thanks"
    assert ("status", "sent") in seen["eq"]


def test_the_done_route_merges_newest_first(monkeypatch):
    monkeypatch.setattr(concierge_routes.db, "get_member", lambda *a, **k: None)  # the owner
    monkeypatch.setattr(concierge_routes.db, "list_done_proposals",
                        lambda t, u, client=None, **k: [{"summary": "A card", "done_at": "2026-09-24T09:15:00"}])
    monkeypatch.setattr(concierge_routes.db, "list_sent_drafts",
                        lambda t, client=None: [{"summary": "Email to Dan", "done_at": "2026-09-24T09:20:00"}])
    out = concierge_routes.business_done(_ctx())
    assert [d["summary"] for d in out["done"]] == ["Email to Dan", "A card"]


def test_a_sent_card_leaves_the_stage_and_the_done_badge_opens_its_list():
    # Kris 2026-09-24: "There's no option to close the sent email, which is also
    # blocking the Pending from opening. Once a draft is sent, it should auto
    # close."
    js = open("src/app/static/concierge_panel.js", encoding="utf-8").read()
    tpl = open("src/app/templates/app/_concierge_panel.html", encoding="utf-8").read()
    yes = js[js.index("var sent = await dr._draft.send();"):][:600]
    assert "if (sent) settledCardGoes(dr);" in yes
    assert "if (card && data.ok && !actOn) settledCardGoes(card);" in js      # a confirmed card goes the same way…
    assert r"var actOn = data.link && /^\/app\/invoices\//.test(data.link.href || '');" in js   # …unless it hands you an invoice
    gone = js[js.index("function settledCardGoes(card) {"):][:250]
    assert "loadDone();" in gone and "if (voiceStage.cardNode === card) clearVoiceCard();" in gone
    badge = js[js.index("function mountBadge(root) {"):][:1400]
    assert "doneOpen = !doneOpen;" in badge and "stageForList();" in badge
    # any card on screen steps aside for a list, waiting or not (Kris 2026-09-24)
    lst = js[js.index("function stageForList() {"):][:120]
    assert "if (voiceStage.cardNode) clearVoiceCard();" in lst
    assert "} else if (doneOpen) {" in js and "fetch('/concierge/business/done'" in js
    assert "#voice-live-root .cz-done-badge {" in tpl


def test_the_done_list_is_a_deck_in_the_bottom_third():
    # Kris 2026-09-24: "the same stacking effect/style as the Today page. Just
    # use the bottom third of the screen for the whole stack."
    js = open("src/app/static/concierge_panel.js", encoding="utf-8").read()
    tpl = open("src/app/templates/app/_concierge_panel.html", encoding="utf-8").read()
    deck = js[js.index("function doneRow() {"):][:5600]
    assert "card.className = 'cz-done-card glass-card';" in deck
    assert "card.style.top = (DONE_TOP + ti * DONE_TOP_STEP) + 'px';" in deck and "card.style.bottom = (ri * DONE_BOT_STEP) + 'px';" in deck
    assert "var face = Math.min(n - 1, Math.max(0, Math.round(stage.scrollTop / Math.max(1, step))));" in deck
    assert "c.style.zIndex = String(i === face ? 600 : (i < face ? i + 1 : 300 - i));" in deck
    assert "c.classList.toggle('is-buried', i !== face);" in deck                # one sharp card at a time
    assert "stage.addEventListener('scroll', function () { if (deck.isConnected) schedule(); }, { passive: true });" in deck
    assert "#voice-live-root .vl-stage-scroll:has(.cz-done-deck) { height: var(--cz-deck-h, 33vh); max-height: var(--cz-deck-h, 33vh);" in tpl
    assert "#voice-live-root .vl-stage-scroll .cz-done-card { position: sticky;" in tpl
    assert "#voice-live-root .cz-done-card.is-buried .cz-done-face { filter: blur(3px); }" in tpl


def test_the_done_deck_measures_against_the_voice_scroller():
    # Kris 2026-09-24: "when revealing cards they stay blurred, it's only the
    # first & last card that isn't". The deck's parent is voiceRenderStage's
    # wrapper, as tall as the deck — the piles must be measured against, and
    # the scroll heard on, the .vl-stage-scroll itself.
    js = open("src/app/static/concierge_panel.js", encoding="utf-8").read()
    deck = js[js.index("function doneRow() {"):][:5600]
    assert "function stageOf() { return deck.closest('.vl-stage-scroll') || deck.parentNode; }" in deck
    assert "var stage = deck.parentNode;" not in deck
    assert deck.count("var stage = stageOf();") == 2


def test_the_done_deck_shows_one_card_at_a_time():
    # Kris 2026-09-24: "take less screen space, only show 1 unblurred card at
    # a time, compress the stack height a little".
    js = open("src/app/static/concierge_panel.js", encoding="utf-8").read()
    tpl = open("src/app/templates/app/_concierge_panel.html", encoding="utf-8").read()
    assert "var DONE_TOP = 4, DONE_TOP_STEP = 5, DONE_BOT_STEP = 5, DONE_LAYERS = 9;" in js
    # one edge per done action behind the face, up to nine (10 cards in all)
    assert "var ti = Math.min(i, DONE_LAYERS), ri = Math.min(n - 1 - i, DONE_LAYERS);" in js
    assert "piles = Math.min(n - 1, DONE_LAYERS);" in js
    assert "(DONE_TOP + cardH + piles * Math.max(DONE_TOP_STEP, DONE_BOT_STEP)) + 'px');" in js
    assert "#voice-live-root .cz-done-face { padding: 0.65rem 1rem;" in tpl



def test_the_corners_and_the_cross():
    # Kris 2026-09-24: waiting + done top right, New task top left; an open
    # list's circle shows a cross; New task empties the screen and says so.
    js = open("src/app/static/concierge_panel.js", encoding="utf-8").read()
    tpl = open("src/app/templates/app/_concierge_panel.html", encoding="utf-8").read()
    voice = open("src/app/static/voice-gptlive.js", encoding="utf-8").read()
    assert "#voice-live-root .cz-context-chip, #voice-gptlive-root .cz-context-chip { position: absolute; top: max(0.9rem, env(safe-area-inset-top)); left: 1rem;" in tpl
    assert "waitBadge.textContent = waitingOpen && n ? '✕' : String(n);" in js
    assert "doneBadge.textContent = doneOpen && n ? '✕' : String(n);" in js
    assert "activeVoiceMode === 'gptlive' && voiceLiveInstance.clearScreen" in js
    clear = voice[voice.index("clearScreen(line)"):][:180]
    assert "api.close('context_clear');api.open()" in clear


def test_an_extra_night_draft_invoice_waits_and_opens(monkeypatch):
    monkeypatch.setattr(concierge_routes.db, "get_member", lambda *a, **k: None)  # the owner
    # Kris 2026-09-24: "it says a draft invoice is ready but doesn't show that
    # as a draft or pending".
    monkeypatch.setattr(concierge_routes.db, "list_waiting_proposals", lambda t, u, client=None, **k: [])
    monkeypatch.setattr(concierge_routes.db, "list_waiting_drafts", lambda t, client=None: [])
    monkeypatch.setattr(concierge_routes.db, "list_waiting_invoices", lambda t, client=None: [
        {"type": "link", "href": "/app/invoices/i1", "summary": "Draft invoice INV-1019 — £30.50 extra night"}])
    out = concierge_routes.business_waiting(_ctx())
    assert out["waiting"][0]["type"] == "link"
    js = open("src/app/static/concierge_panel.js", encoding="utf-8").read()
    assert "if (w.type === 'link') { if (w.href && w.href.charAt(0) === '/') window.location.href = w.href; return; }" in js
    assert "if (w.type !== 'link' && w.type !== 'recovery') chip.appendChild(x);" in js


def test_a_newer_stay_note_retires_the_one_still_waiting():
    src = open("src/app/concierge_actions.py", encoding="utf-8").read()
    ex = src[src.index("async def _exec_change_stay("):][:2600]
    # the waiting notes are retired (and the need decided) before any new one is written
    assert ex.index("if _stay_note_needed(tenant_id, client, booking, plan):") < ex.index("_background(_queue_stay_note(")
    need = src[src.index("def _stay_note_needed("):][:900]
    assert "db.retire_pending_owner_messages(tenant_id, booking.get(\"contact\"), subject, client=client)" in need


def test_the_draft_is_binned_with_a_red_bin_not_a_cross():
    # Kris 2026-09-24: "it shows a cross to cancel it, but it looks more like
    # it closes the draft. Change that into a red Bin".
    js = open("src/app/static/concierge_panel.js", encoding="utf-8").read()
    tpl = open("src/app/templates/app/_concierge_panel.html", encoding="utf-8").read()
    assert "skip.innerHTML = SKIP_BIN;" in js and "SKIP_X" not in js
    assert "skip.className = 'cz-draft-btn cz-draft-bin';" in js
    assert ".cz-draft-bin { color: #dc2626;" in tpl and "#voice-live-root .cz-draft-bin { color: #f87171;" in tpl


# ── the typed thread's waiting circle (Kris 2026-09-29: "reuse the circle from VC, put it on
#    the bottom right next to the Agent bubbles… when the user sends a message move it to the
#    left, next to the user bubble. Clicked, it expands into the list, centred at the bottom") ──
def test_typed_thread_waiting_circle_sits_beside_the_concierge_and_hides_while_answering():
    js = open("src/app/static/concierge_panel.js", encoding="utf-8").read()
    tpl = open("src/app/templates/app/_concierge_panel.html", encoding="utf-8").read()
    # one circle, kept beside the newest message and re-placed as the thread grows
    assert "function mountWaitDot()" in js and "new MutationObserver(function () { placeWaitDot(); })" in js
    place = js[js.index("function placeWaitDot()"):][:3200]
    # Kris 2026-09-29 (revised): it does NOT move to the owner's side — it hides while their
    # message is unanswered (their bubble, the "Searching…" bubble, its pills) and returns with the answer
    assert "classList.contains('justify-end')" in place
    assert "newest.hasAttribute('data-cz-pending') || newest.hasAttribute('data-cz-trail')" in place
    assert "' is-hidden'" in place and "is-left" not in place
    assert "gutter >= 48 ? ' is-beside'" in place                # a gutter → beside; none → just below
    assert "anchor.parentNode.insertBefore(waitDotRow, anchor.nextSibling)" in place
    assert "el.setAttribute('data-cz-pending', '');" in js
    # hidden when nothing waits; tapped, the list opens (and the circle becomes the way out)
    paint = js[js.index("function paintWaitDot()"):][:900]
    assert "waitDot.hidden = !n;" in paint and "if (!n) dotOpen = false;" in paint
    assert "var row = dotOpen ? waitingRow(true) : null;" in js   # the old always-on strip is gone
    assert "waitingBox.classList.toggle('cz-waiting-pop', !!row);" in js
    # picking a line closes it
    assert "function resurface(w, btn) {\n      dotOpen = false;" in js
    # the circle is the voice screen's (amber dashed ring, count), the list a centred column
    assert ".cz-wait-dot { width: 2rem; height: 2rem; border-radius: 9999px; border: 1px dashed #f59e0b;" in tpl
    assert ".cz-waitdot-row.is-beside .cz-wait-dot { position: absolute; bottom: 0; right: 0; }" in tpl
    assert ".cz-waitdot-row.is-empty, .cz-waitdot-row.is-hidden { display: none !important; }" in tpl
    assert "is-left" not in tpl
    assert "#cz-waiting.cz-waiting-pop { display: flex; justify-content: center; }" in tpl


def test_circle_steps_up_beside_the_concierge_message_when_a_card_is_full_width():
    """Kris 2026-09-30: a draft/offer has no gutter, so the circle sits beside the Concierge
    message that introduced it, not below the card; it stops at the owner's own message."""
    js = open("src/app/static/concierge_panel.js", encoding="utf-8").read()
    place = js[js.index("function placeWaitDot()"):][:3200]
    assert "messages.clientWidth - newest.offsetWidth < 48" in place
    # Flatten mode sections before walking back to the preceding reply.
    assert "entries = transcriptItems()" in place
    assert "j = entries.indexOf(newest) - 1" in place and "j >= 0; j--" in place
    assert "var b = entries[j]" in place
    assert "b.getAttribute('data-cz-role') === 'user' || b.classList.contains('justify-end')" in place
    assert "if (messages.clientWidth - b.offsetWidth >= 48) { anchor = b; break; }" in place
    assert "var gutter = anchor && !waiting ? messages.clientWidth - anchor.offsetWidth : 0;" in place


def _yes_gate():
    import shutil, subprocess
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not installed")
    js = open("src/app/static/concierge_panel.js", encoding="utf-8").read()
    fn = js[js.index("function lastAssistantAskedToSend()"):js.index("function voiceLog(entry)")]

    def run(history, invited=False):
        import json
        script = ("var history=%s; var voiceStage={draftInvited:%s}; %s "
                  "console.log(lastAssistantAskedToSend());" % (json.dumps(history), str(invited).lower(), fn))
        r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip() == "true"
    return run


def test_a_bare_yes_sends_the_draft_only_when_the_last_question_was_about_sending():
    run = _yes_gate()
    a = lambda t: [{"role": "user", "content": "x"}, {"role": "assistant", "content": t}, {"role": "user", "content": "Yes."}]
    # the 2026-09-30 reply: a draft was on screen, the question was about the diary
    assert not run(a("Hmm, the draft says we haven't fully confirmed the booking. You'll want to confirm it in the "
                     "diary first, and then I can get the right email sent to Ann. Want to do that?"))
    assert not run(a("It's ready. Want to confirm the booking first?"))
    assert not run(a("I've written it to Ann."))                      # no question at all
    assert run(a("That says it's pencilled in and asks her to confirm. Shall I send it?"))
    assert run(a("Want me to send that to Ann?"))
    assert run(a("Ready for you to approve — go ahead?"))
    assert not run([{"role": "user", "content": "Yes."}])             # nothing asked
    # "Back — say yes to send it" (a draft brought back from the waiting list) is an invitation
    assert run([{"role": "user", "content": "Yes."}], invited=True)


def test_the_yes_gate_guards_only_the_draft_send_and_is_reset_with_the_card():
    js = open("src/app/static/concierge_panel.js", encoding="utf-8").read()
    assert "if (isBareAffirmation(text) && lastAssistantAskedToSend()) {" in js
    assert "voiceStage.draftInvited = true;" in js          # resurface invites a yes
    assert js.count("voiceStage.draftInvited = false;") >= 2  # a new card, and clearVoiceCard


def test_a_delegated_question_parks_the_card_on_the_live_screen():
    """GPT-Live (live 2026-10-14, scenario 20): asking something else while a card
    was up left the card on the stage, where Standard voice parks it as a waiting
    chip. A yes/no to the card on screen is the one question that keeps it."""
    import json
    import shutil
    import subprocess
    import pytest
    js = open("src/app/static/concierge_panel.js", encoding="utf-8").read()
    start = js.index("onNativeQuestion: function (text, turnId) {")
    handler = js[start:js.index("\n          onNativeTasks:", start)].rstrip().rstrip(",")
    assert handler.count("clearVoiceCard();") == 1
    # the two invariants other tests pin: the badge mount and Standard voice's own parking
    assert "onMount: mountBadge," in js
    turn = js[js.index("async function voiceUserTurn("):js.index("function parkVoiceCard()")]
    assert turn.count("clearVoiceCard();") == 3
    node = shutil.which("node")
    if not node:
        pytest.fail("Node is required to verify the live question handler")
    cases = [
        # (stage, text, parked?)
        ({"cardNode": True, "proposal": True}, "What's in the diary tomorrow", True),
        ({"cardNode": True, "proposal": True}, "Run me through my debrief.", True),
        ({"cardNode": True, "proposal": True}, "yes please", False),
        ({"cardNode": True, "draft": True}, "no thanks", False),
        ({"cardNode": True}, "yes please", True),        # a card nobody awaits an answer on
        ({"links": 1}, "What's in the diary tomorrow", True),
        ({}, "What's in the diary tomorrow", False),
    ]
    script = ("const assert = require('node:assert/strict');"
              "const HANDLER = " + json.dumps(handler) + ";"
              "const cases = JSON.parse(" + json.dumps(json.dumps(cases)) + ");"
              "for (const [stage, text, parked] of cases) {"
              " let cleared = 0;"
              " const voiceStage = {cardNode: stage.cardNode ? {} : null, proposal: stage.proposal ? {} : null,"
              "   draft: stage.draft ? {_draft: {pending: () => true}} : null, links: stage.links ? [{}] : []};"
              " const ctx = {voiceStage, voiceUserBubbles: new Map(), voiceLog: () => ({}),"
              "   isBareAffirmation: t => /^(yes|yes please|yeah)$/i.test(t), isBareDecline: t => /^(no|no thanks)$/i.test(t),"
              "   clearVoiceCard: () => { cleared += 1; }};"
              " const handlers = require('node:vm').runInNewContext('({' + HANDLER + '})', ctx);"
              " handlers.onNativeQuestion(text, 'turn-1');"
              " assert.equal(cleared, parked ? 1 : 0, text + ' ' + JSON.stringify(stage));"
              " assert.ok(ctx.voiceUserBubbles.has('turn-1')); }")
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
