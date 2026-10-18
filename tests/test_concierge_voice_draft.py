"""Drafting from the voice Concierge (Kris 2026-09-23).

"Unless the VC agent calls the Drafting agent to write the draft, then it's
just displayed in the VC view." The Concierge picks who and which kind and
passes the owner's words through; the Drafting agent writes it; the result is
an ordinary pending draft, shown on the voice screen for approval — or, with
full-auto on, sent straight away as an automated send.

Pinned here:
  * a reply goes through the inbox's own redraft, carrying the instruction;
  * a reply with nothing waiting is refused honestly, not drafted anyway;
  * a new email goes through compose_outbound and lands as an owner_message;
  * full-auto sends through the thumbs-up's send path, marked auto, with the
    AI disclosure footer; review mode never sends;
  * the thumbs-up route itself is unchanged by the refactor.

Run: python -m pytest tests/test_concierge_voice_draft.py -q
"""
import asyncio
import pathlib
from types import SimpleNamespace

import pytest

import app.concierge_draft as cd
import app.dashboard as dashboard
from app import agent, pipeline
from app.auth import TenantCtx

ROOT = pathlib.Path(__file__).parent.parent
CTX = TenantCtx(tenant_id="t1", client=None)


def _wire(monkeypatch, rows, *, auto_send=False):
    calls = {"redraft": [], "compose": [], "insert": [], "send": [], "status": []}
    monkeypatch.setattr("app.concierge_booking_claims.read_bookings", lambda *a, **k: [
        {"booking_ref": "BK-1", "booking_date": "2026-10-01", "status": "confirmed"},
        {"booking_ref": "BK-2", "booking_date": "2026-09-30", "status": "confirmed"}])

    async def _redraft(tenant_id, log_id, client=None, owner_instruction=""):
        calls["redraft"].append((log_id, owner_instruction))
        return "Hi Hannah, Thursday is booked."

    async def _compose(cfg, purpose, facts, **k):
        calls["compose"].append((purpose, facts, k))
        return SimpleNamespace(text="Hi Hannah, we're closed Friday.",
                               model="m", usage={"input_tokens": 1})

    def _insert(tenant_id, from_address, subject, body_in, draft_reply, **k):
        calls["insert"].append({"to": from_address, "subject": subject,
                                "body_in": body_in, "draft": draft_reply, **k})
        return "new-log", True

    async def _send(ctx, log_id, text, **k):
        calls["send"].append((log_id, text, k))
        return "sent"

    monkeypatch.setattr(cd.db, "get_conversation", lambda *a, **k: rows)
    monkeypatch.setattr(cd.db, "effective_config", lambda *a, **k: {"auto_send": auto_send})
    monkeypatch.setattr(cd.db, "insert_email_log", _insert)
    monkeypatch.setattr(cd.db, "set_email_log_status",
                        lambda log_id, status, *a, **k: calls["status"].append((log_id, status)))
    monkeypatch.setattr(cd.db, "log_agent_event", lambda *a, **k: None)
    # Hannah is a customer on file: a NEW email only goes to one (M21).
    monkeypatch.setattr(cd.db, "crm_contact_by_email", lambda *a, **k: {"email": "hannah@x.co"})
    monkeypatch.setattr(cd.events, "publish", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "redraft_email_log", _redraft)
    monkeypatch.setattr(agent, "compose_outbound", _compose)
    monkeypatch.setattr(dashboard, "send_approved", _send)
    monkeypatch.setattr("app.usage.record_anthropic", lambda *a, **k: None)
    return calls


WAITING = [{"id": "log-9", "kind": "inbound", "status": "pending", "subject": "Thursday?",
            "from_name": "Hannah Price", "mailbox_id": None}]
ANSWERED = [{**WAITING[0], "status": "sent"}]


def _run(args, member=None):
    return asyncio.run(cd.draft(CTX, args, member=member))


def test_a_reply_is_the_inbox_redraft_with_the_owners_words(monkeypatch):
    calls = _wire(monkeypatch, WAITING)
    result, card = _run({"contact": "Hannah@X.co", "mode": "reply",
                         "instruction": "say Thursday is fine"})
    assert calls["redraft"] == [("log-9", "say Thursday is fine")]
    assert calls["compose"] == [] and calls["send"] == []
    assert result["status"] == "pending" and "ask whether to send" in result["message"]
    assert card["status"] == "pending" and card["log_id"] == "log-9"
    assert card["name"] == "Hannah Price" and card["contact"] == "hannah@x.co"
    assert card["text"] == "Hi Hannah, Thursday is booked."


def test_a_reply_with_nothing_waiting_is_refused_not_drafted(monkeypatch):
    calls = _wire(monkeypatch, ANSWERED)
    result, card = _run({"contact": "hannah@x.co", "mode": "reply"})
    assert result["error"] == "nothing_waiting" and card is None
    assert "new email" in result["message"]
    assert calls["redraft"] == []


def test_a_skipped_row_comes_back_to_pending_when_redrafted(monkeypatch):
    calls = _wire(monkeypatch, [{**WAITING[0], "status": "skipped"}])
    _run({"contact": "hannah@x.co", "mode": "reply"})
    assert calls["status"] == [("log-9", "pending")]


def test_a_new_email_is_composed_and_lands_as_an_owner_message(monkeypatch):
    calls = _wire(monkeypatch, ANSWERED)
    result, card = _run({"contact": "hannah@x.co", "mode": "new",
                         "instruction": "tell her we're closed Friday",
                         "subject": "Closed Friday"})
    assert calls["redraft"] == []
    assert "tell her we're closed Friday" in calls["compose"][0][1]
    ins = calls["insert"][0]
    assert ins["kind"] == "owner_message" and ins["status"] == "pending"
    assert ins["subject"] == "Closed Friday"
    assert ins["body_in"].startswith("[owner message]")   # the thread shows no customer bubble
    assert card["log_id"] == "new-log" and card["mode"] == "new"
    assert result["status"] == "pending"


def test_a_new_email_needs_to_know_what_to_say(monkeypatch):
    calls = _wire(monkeypatch, ANSWERED)
    result, card = _run({"contact": "hannah@x.co", "mode": "new"})
    assert result["error"] == "need_instruction" and card is None
    assert calls["compose"] == []


def test_full_auto_sends_through_the_thumbs_up_path_as_automated(monkeypatch):
    calls = _wire(monkeypatch, WAITING, auto_send=True)
    result, card = _run({"contact": "hannah@x.co", "mode": "reply"})
    assert calls["send"] == [("log-9", "Hi Hannah, Thursday is booked.", {"auto": True, "concierge": True})]
    assert card["status"] == "sent" and card["auto"] is True
    assert result["status"] == "sent" and "SENT" in result["message"]


def test_a_failed_full_auto_send_says_so(monkeypatch):
    _wire(monkeypatch, WAITING, auto_send=True)

    async def _boom(*a, **k):
        raise RuntimeError("smtp down")
    monkeypatch.setattr(dashboard, "send_approved", _boom)
    result, card = _run({"contact": "hannah@x.co", "mode": "reply"})
    assert card["status"] == "failed" and result["status"] == "failed"


def test_it_needs_an_email_address_and_permission(monkeypatch):
    calls = _wire(monkeypatch, WAITING)
    result, _ = _run({"contact": "Hannah", "mode": "reply"})
    assert result["error"] == "need_email"
    monkeypatch.setattr("app.concierge_actions.may_do", lambda member, kind: False)
    result, card = _run({"contact": "hannah@x.co", "mode": "reply"}, member={"role": "staff"})
    assert result["error"] == "not_allowed" and card is None
    assert calls["redraft"] == []


# ── the send path: one function, the route and the voice both use it ────────
def _wire_send(monkeypatch, kind="owner_message"):
    seen = {}
    entry = {"id": "log-1", "from_address": "hannah@x.co", "subject": "Closed Friday",
             "draft_reply": "We're closed Friday.", "kind": kind, "mailbox_id": None,
             "source_message_id": None}

    async def _send_mail(tenant_id, to, subject, body, **k):
        seen["mail"] = (to, subject, body)

    async def _send_reply(tenant_id, to, subject, body, **k):
        seen["reply"] = (to, subject, body)

    monkeypatch.setattr(dashboard.db, "get_email_log_entry", lambda *a, **k: entry)
    monkeypatch.setattr(dashboard.db, "update_email_log_draft", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.db, "effective_config", lambda *a, **k: {})
    monkeypatch.setattr(dashboard.db, "mark_email_log_sent",
                        lambda log_id, tid, via, edited=None, client=None: seen.update(via=via))
    monkeypatch.setattr(dashboard.db, "resolve_holds_for_log", lambda *a, **k: 0)
    monkeypatch.setattr(dashboard.db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.mailbox, "send_mail", _send_mail)
    monkeypatch.setattr(dashboard.mailbox, "send_reply", _send_reply)
    monkeypatch.setattr(dashboard.suppression, "record_sent", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.trace, "emit", lambda *a, **k: None)
    monkeypatch.setattr(dashboard.events, "publish", lambda *a, **k: None)
    monkeypatch.setattr(dashboard, "_detach", lambda coro: coro.close())
    return seen


def test_a_voice_full_auto_send_carries_the_ai_footer_and_is_marked_auto(monkeypatch):
    seen = _wire_send(monkeypatch)
    out = asyncio.run(dashboard.send_approved(CTX, "log-1", "We're closed Friday.", auto=True))
    assert out == "sent" and seen["via"] == "auto"
    to, subject, body = seen["mail"]            # an owner_message is a fresh send, own subject
    assert subject == "Closed Friday"
    assert body.startswith("We're closed Friday.")
    assert "This automated message was written by an AI assistant" in body


def test_an_approved_send_is_human_and_carries_no_footer(monkeypatch):
    seen = _wire_send(monkeypatch)
    asyncio.run(dashboard.send_approved(CTX, "log-1", "We're closed Friday."))
    assert seen["via"] == "human"
    assert "AI assistant" not in seen["mail"][2]


def test_the_form_route_cannot_ask_for_an_automated_send():
    import inspect
    params = inspect.signature(dashboard.send_draft).parameters
    assert "auto" not in params
    src = inspect.getsource(dashboard.send_draft)
    assert "auto=" not in src


# ── the Drafting agent hears the owner, under the platform rules ────────────
def test_the_owners_instruction_reaches_the_drafting_prompt():
    prompt = agent.build_system_prompt({}, owner_instruction="say Thursday is fine")
    assert "<owner_instruction>" in prompt and "say Thursday is fine" in prompt
    assert "PLATFORM RULES above still win" in prompt
    assert "<owner_instruction>" not in agent.build_system_prompt({})


# ── the voice route and the screen ───────────────────────────────────────────
def test_voice_offers_the_drafting_tool_and_not_the_self_written_card():
    src = (ROOT / "src/app" / "concierge_routes.py").read_text(encoding="utf-8")
    voice = src[src.index("async def voice_chat("):src.index("# What the server has said")]
    assert "+ [concierge_draft.DRAFT_TOOL, concierge_draft.SHOW_TOOL," in voice
    assert "concierge_draft.show(ctx, args, member=asker)" in voice
    assert 'if (result.action or {}).get("kind") == "draft_reply":' in voice
    assert 'done["draft"] = drafted.get("card")' in voice


def test_the_voice_screen_sends_through_the_threads_own_endpoints():
    js = (ROOT / "src/app" / "static" / "concierge_panel.js").read_text(encoding="utf-8")
    card = js[js.index("function draftCardNode(d)"):][:6000]
    assert "'/app/drafts/' + encodeURIComponent(d.log_id) + '/send'" in card
    assert "'/app/drafts/' + encodeURIComponent(d.log_id) + '/skip'" in card
    assert "Draft ready · Editable before approval" in card
    turn = js[js.index("async function voiceUserTurn("):][:1600]
    # yes sends it, no leaves it in the thread
    assert "dr._draft.send()" in turn and "dr._draft.leave()" in turn


def test_the_voice_screen_clears_on_no_skip_and_the_next_message():
    # Kris 2026-09-23: "the UI state doesn't reset on the next message. Also
    # clicking a cross or saying no to the draft doesn't work/clear it". The
    # skip DID land (the row was skipped); the card just stayed on screen.
    js = (ROOT / "src/app" / "static" / "concierge_panel.js").read_text(encoding="utf-8")
    clear = js[js.index("function clearVoiceCard() {"):][:900]
    for step in ("parkVoiceCard();", "voiceStage.cardNode = null;", "voiceStage.proposal = null;",
                 "voiceStage.draft = null;", "voiceStage.links = [];",
                 "voiceLiveInstance.refreshStage();"):
        assert step in clear, step
    turn = js[js.index("async function voiceUserTurn("):]
    turn = turn[:turn.index("function parkVoiceCard()")]
    # no to a draft, no to a confirm card: both leave the screen
    assert turn.count("clearVoiceCard();") == 3
    assert "dr._draft.leave();\n          clearVoiceCard();" in turn
    # anything that isn't a yes/no to the card is a new message: clean screen
    tail = turn[turn.rindex("return {};") - 400:]
    assert "if (voiceStage.cardNode || voiceStage.links.length) clearVoiceCard();" in tail
    # the cross takes the card off; a send leaves "Sent ✓" up until the next message
    tapped = js[js.index("function tapped(said) {"):][:500]
    assert "if (state === 'skipped') clearVoiceCard();" in tapped


def test_a_new_voice_draft_replaces_the_one_still_waiting(monkeypatch):
    # Kris 2026-09-23: "Yes, send it" went to the model, which drafted again —
    # three identical drafts to Daniel waited in the queue.
    from types import SimpleNamespace
    from app import concierge_draft

    skipped, filters = [], {}

    class Q:
        def __getattr__(self, name):
            def call(*a, **k):
                filters.setdefault(name, []).append(a)
                return self
            return call

        def execute(self):
            return SimpleNamespace(data=[{"id": "old-1"}, {"id": "old-2"}])
    ctx = SimpleNamespace(tenant_id="t1", client=SimpleNamespace(table=lambda n: Q()))
    monkeypatch.setattr(concierge_draft.db, "set_email_log_status",
                        lambda lid, status, t, client=None: skipped.append((lid, status)))
    assert concierge_draft._retire_earlier_drafts(ctx, "dan@x.com") == 2
    assert skipped == [("old-1", "skipped"), ("old-2", "skipped")]
    # only this customer's pending voice drafts
    assert ("kind", "owner_message") in filters["eq"] and ("status", "pending") in filters["eq"]
    assert ("from_address", "dan@x.com") in filters["ilike"]
    src = open("src/app/concierge_draft.py", encoding="utf-8").read()
    assert src.index("_retire_earlier_drafts(ctx, contact)") < src.index('status="pending", kind="owner_message"')


def test_a_sent_draft_leaves_the_voice_screen_after_its_tick():
    # Kris 2026-09-23 20:05: "I clicked the thumbs up. It says sent but the
    # draft box is still showing on screen".
    js = open("src/app/static/concierge_panel.js", encoding="utf-8").read()
    tapped = js[js.index("function tapped(said) {"):][:900]
    assert "if (state === 'skipped') clearVoiceCard();" in tapped
    assert "else if (state === 'sent') settledCardGoes(card);" in tapped
    gone = js[js.index("function settledCardGoes(card) {"):][:250]
    assert "setTimeout(function () { if (voiceStage.cardNode === card) clearVoiceCard(); }, SETTLED_MS);" in gone


# ── a draft that's already written comes back on screen (Kris 2026-09-24) ────
# "It's already written … I say Yes, it then goes and searches the inbox
# again … then tells me it cannot send from here." The Laura draft sat pending
# while the stage had moved on; nothing could put it back for the yes to send.
LAURA = [{"id": "log-L", "kind": "owner_message", "status": "pending",
          "subject": "Meet & Greet Confirmation – Otis",
          "draft_reply": "Hi Laura, Otis is booked in for Wednesday at 1pm."}]


def _show(monkeypatch, rows, member=None):
    calls = _wire(monkeypatch, rows)
    monkeypatch.setattr(cd.db, "crm_contact_by_email", lambda *a, **k: {"name": "Laura Finch"})
    monkeypatch.setattr(dashboard, "_require_their_log", lambda ctx, log_id: None)
    return calls, asyncio.run(cd.show(CTX, {"contact": "Business.Blam8+Laura.Finch@gmail.com"},
                                      member=member))


def test_an_existing_draft_comes_back_on_screen_unchanged(monkeypatch):
    calls, (result, card) = _show(monkeypatch, LAURA)
    assert result["ok"] and result["status"] == "pending"
    assert card["log_id"] == "log-L" and card["status"] == "pending"
    assert card["text"] == LAURA[0]["draft_reply"] and card["name"] == "Laura Finch"
    assert card["mode"] == "new"
    # never rewritten, never sent, nothing retired
    assert calls["redraft"] == calls["compose"] == calls["send"] == calls["status"] == []


def test_the_newest_pending_draft_is_the_one_shown(monkeypatch):
    rows = [{**LAURA[0], "id": "old", "status": "sent"},
            {"id": "reply", "kind": "inbound", "status": "pending", "subject": "Otis",
             "draft_reply": "Hi Laura, see you Wednesday."}]
    _, (_, card) = _show(monkeypatch, rows)
    assert card["log_id"] == "reply" and card["mode"] == "reply"


def test_no_draft_waiting_says_so(monkeypatch):
    rows = [{**LAURA[0], "status": "sent"},
            {"id": "x", "kind": "inbound", "status": "pending", "draft_reply": ""}]
    _, (result, card) = _show(monkeypatch, rows)
    assert result["error"] == "no_draft" and card is None


def test_showing_a_draft_needs_permission(monkeypatch):
    monkeypatch.setattr("app.concierge_actions.may_do", lambda member, kind: False)
    _, (result, card) = _show(monkeypatch, LAURA, member={"role": "staff"})
    assert result["error"] == "not_allowed" and card is None


def test_the_voice_prompt_sends_an_existing_draft_through_show_draft():
    from app import concierge
    src = concierge.build_business_prompt(voice_mode=True)
    assert "call show_draft" in src
    assert "never tell them you can't send from here" in src



def _drafted_details(monkeypatch, mode, rows, cfg, draft_text=None):
    """Run a voice draft and return the detail of its `drafted` agent event."""
    _wire(monkeypatch, rows)
    seen = []
    monkeypatch.setattr(cd.db, "log_agent_event",
                        lambda tenant_id, kind, summary, **k: seen.append((kind, k.get("detail"))))
    monkeypatch.setattr(cd.db, "effective_config", lambda *a, **k: cfg)
    if draft_text is not None:
        async def _redraft(*a, **k):
            return draft_text
        monkeypatch.setattr(pipeline, "redraft_email_log", _redraft)
    _run({"contact": "hannah@x.co", "mode": mode, "instruction": "say hello"})
    return [detail for kind, detail in seen if kind == "drafted"][0]


@pytest.mark.parametrize("mode,rows", [("reply", WAITING), ("new", ANSWERED)])
def test_the_voice_draft_event_flags_a_missing_sign_off(monkeypatch, mode, rows):
    """No sign-off set: the agent log says so (never blocks); with one, no flag."""
    assert _drafted_details(monkeypatch, mode, rows, {})["sign_off_missing"] is True
    assert _drafted_details(monkeypatch, mode, rows, {"sign_off": " \r\n "})["sign_off_missing"] is True
    set_detail = _drafted_details(monkeypatch, mode, rows, {"sign_off": "Cheers, Sam"})
    assert "sign_off_missing" not in set_detail
    assert set_detail["via"] == "concierge_voice" and set_detail["mode"] == mode


def test_an_empty_voice_draft_is_not_flagged_and_skips_the_config_read(monkeypatch):
    _wire(monkeypatch, WAITING)
    seen = []
    monkeypatch.setattr(cd.db, "log_agent_event",
                        lambda tenant_id, kind, summary, **k: seen.append((kind, k.get("detail"))))

    def forbidden(*a, **k):
        raise AssertionError("an empty draft needs no config read")
    monkeypatch.setattr(cd.db, "effective_config", forbidden)

    async def _redraft(*a, **k):
        return ""
    monkeypatch.setattr(pipeline, "redraft_email_log", _redraft)
    result, card = _run({"contact": "hannah@x.co", "mode": "reply"})
    assert card["status"] == "empty"
    assert "sign_off_missing" not in [d for kind, d in seen if kind == "drafted"][0]


@pytest.mark.parametrize("mode,rows", [("reply", WAITING), ("new", ANSWERED)])
def test_an_unreadable_config_never_loses_the_drafted_event_or_the_live_update(
        monkeypatch, mode, rows):
    """The sign-off badge reads the config after the draft is stored. If that read fails
    the agent-log event and the status update still happen (without the badge), and
    nothing is auto-sent: the draft waits for the owner."""
    calls = _wire(monkeypatch, rows, auto_send=True)
    seen, published = [], []
    monkeypatch.setattr(cd.db, "log_agent_event",
                        lambda tenant_id, kind, summary, **k: seen.append((kind, k.get("detail"))))
    monkeypatch.setattr(cd.events, "publish", lambda tenant_id, payload: published.append(payload))

    reads = []

    def broken(*a, **k):
        reads.append(1)
        if mode == "new" and len(reads) == 1:
            return {}            # composing a new email needs the config before the draft exists
        raise RuntimeError("config unavailable")
    monkeypatch.setattr(cd.db, "effective_config", broken)
    result, card = _run({"contact": "hannah@x.co", "mode": mode, "instruction": "say hello"})
    drafted =[detail for kind, detail in seen if kind == "drafted"]
    assert len(drafted) == 1 and "sign_off_missing" not in drafted[0]
    assert drafted[0]["via"] == "concierge_voice" and drafted[0]["mode"] == mode
    assert [p["status"] for p in published] == ["pending"]
    assert card["status"] == "pending" and result["status"] == "pending"
    assert calls["send"] == []
