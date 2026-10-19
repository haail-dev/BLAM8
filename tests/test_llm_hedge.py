"""The hedged stream (Kris 2026-09-24: "It took a very long time Thinking").

About 3% of Concierge calls sat 9-27 s before their first word. llm.stream now
races a second identical request when the first hasn't started after a few
seconds, and uses whichever produces content first.
"""
import asyncio
from types import SimpleNamespace

from app import llm


class _Stream:
    def __init__(self, delay, words, tag, log):
        self.delay, self.words, self.tag, self.log = delay, words, tag, log

    async def __aenter__(self):
        self.log.append(("open", self.tag))
        return self

    async def __aexit__(self, *exc):
        self.log.append(("close", self.tag))
        return False

    def __aiter__(self):
        return self._events()

    async def _events(self):
        await asyncio.sleep(self.delay)
        yield SimpleNamespace(type="content_block_start")
        for w in self.words:
            yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=w))

    async def get_final_message(self):
        return SimpleNamespace(tag=self.tag, usage=None, stop_reason="end_turn", content=[])


def _client(delays, log):
    calls = iter(range(10))

    def stream(**kw):
        i = next(calls)
        return _Stream(delays[i], [f"w{i}a", f"w{i}b"], i, log)
    return SimpleNamespace(messages=SimpleNamespace(stream=stream))


def _run(delays, hedge_after):
    log, heard = [], []
    msg = asyncio.run(llm._stream_hedged(_client(delays, log), {}, heard.append, hedge_after))
    return msg, heard, log


def test_a_healthy_call_is_never_hedged():
    msg, heard, log = _run([0.01], hedge_after=0.2)
    assert msg.tag == 0 and heard == ["w0a", "w0b"]
    assert [e for e in log if e[0] == "open"] == [("open", 0)]


def test_a_stalled_call_is_raced_and_the_first_to_speak_wins():
    msg, heard, log = _run([2.0, 0.01], hedge_after=0.1)
    assert msg.tag == 1
    assert heard == ["w1a", "w1b"]          # only the winner's words reach the voice
    assert ("open", 1) in log


def test_the_first_call_can_still_win_after_the_hedge_fires():
    msg, heard, _ = _run([0.15, 1.0], hedge_after=0.1)
    assert msg.tag == 0 and heard == ["w0a", "w0b"]


def test_voice_uses_the_hedge_and_the_hour_long_cache():
    src = open("src/app/concierge.py", encoding="utf-8").read()
    assert 'msg = await llm.stream(on_delta=_forward, system_ttl="1h", **request)' in src
    assert llm.HEDGE_AFTER_S == 5.0
    kw = llm._cached_kwargs({"system": "S", "messages": [{"role": "user", "content": "q"}]}, system_ttl="1h")
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    # the conversation tail keeps the default 5 minutes (1h entries come first)
    assert kw["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}


# ── fewer rounds (same pass) ─────────────────────────────────────────────────
def test_the_voice_note_names_the_drafts_already_waiting(monkeypatch):
    from app import concierge_routes
    monkeypatch.setattr(concierge_routes.db, "list_waiting_drafts", lambda t, client=None: [
        {"name": "Laura Finch", "contact": "laura@x.com", "subject": "Meet & Greet Confirmation"}])
    ctx = SimpleNamespace(tenant_id="t1", client=None)
    note = concierge_routes._voice_note(ctx, clipped=False)
    assert "Laura Finch <laura@x.com>" in note and "call show_draft with that address (no lookup" in note
    assert concierge_routes.concierge.CLIPPED_NOTE in concierge_routes._voice_note(ctx, clipped=True)
    monkeypatch.setattr(concierge_routes.db, "list_waiting_drafts", lambda t, client=None: [])
    assert concierge_routes._voice_note(ctx, clipped=False) is None


def test_lookups_in_one_round_run_together():
    src = open("src/app/concierge.py", encoding="utf-8").read()
    assert "outs = await asyncio.gather(*(_one(tu) for tu in tool_uses))" in src
    assert "for tu, out in zip(tool_uses, outs)]})" in src


def test_a_thank_you_is_answered_without_the_model():
    js = open("src/app/static/voice-live.js", encoding="utf-8").read()
    heard = js[js.index("function heardUtterance(u) {"):][:2000]
    assert heard.index("deps.expectsAnswer(u.text)") < heard.index("isCourtesy(u.text)")
    assert 'sayLocally("You\'re welcome.");' in heard
    court = js[js.index("const COURTESY_WORDS"):][:700]
    assert '"cheers"' not in court          # "cheers" is a goodbye


def test_an_answer_without_an_action_writes_no_empty_action():
    # 24 Sep (phrase 1.1): 146 of a 224-token spoken answer was an empty action
    # of 27 blank fields. The action is optional; present, its fields are required.
    from app import concierge
    schema = concierge.BUSINESS_SCHEMA
    assert schema["required"] == ["reply", "links", "action"]   # tiny now: kind "" + fields "{}" when idle
    action = schema["properties"]["action"]
    # kind + ONE fields string (only the fields that kind uses) — not 27 properties
    assert action["required"] == ["kind", "fields"] and set(action["properties"]) == {"kind", "fields"}

    # The grammar allows at most 24 optional parameters in a schema (a 400 otherwise).
    def optional(node):
        if not isinstance(node, dict):
            return 0
        props = node.get("properties") or {}
        n = len(set(props) - set(node.get("required") or []))
        return n + sum(optional(v) for v in props.values()) + optional(node.get("items"))
    assert optional(schema) <= 24
    # …and the model is told not to re-show the draft it just made
    assert "don't call show_draft for it" in open("src/app/concierge_draft.py", encoding="utf-8").read()


# ── what the model remembers (phrase 1.2, 24 Sep) ────────────────────────────
def test_an_exchange_the_phone_answered_is_saved(monkeypatch):
    import asyncio as _a
    from app import concierge_routes
    saved = []
    monkeypatch.setattr(concierge_routes.db, "add_concierge_message",
                        lambda t, role, content, **k: saved.append((role, content)))
    ctx = SimpleNamespace(tenant_id="t1", client=None, user_id="u1")
    concierge_routes.voice_local(
        concierge_routes.VoiceLocalRequest(question="Yes.", answer="Sent."), ctx)
    assert saved == [("user", "Yes."), ("assistant", "Sent.")]
    js = open("src/app/static/voice-live.js", encoding="utf-8").read()
    assert 'deps.apiFetch("local", { question: text, answer: outcome.ack, display_turn_id: turnId })' in js
    assert 'deps.apiFetch("local", { question: u.text, answer: "You\'re welcome." })' in js


def test_new_task_sets_the_earlier_chat_aside():
    js = open("src/app/static/concierge_panel.js", encoding="utf-8").read()
    nt = js[js.index("function newTask() {"):][:700]
    assert "history.length = 0;" in nt and "localStorage.setItem(CONTEXT_FROM_KEY, String(Date.now()))" in nt
    assert "var cutoff = Math.max(Date.now() - HISTORY_RECENT_MS, contextFrom());" in js
    # The Context button and its hold-to-clear handler were removed from the voice screen.
    assert "cz-context-chip" not in js and "function holdStart(e) {" not in js


# ── test run section 2 (24 Sep) ──────────────────────────────────────────────
def test_one_unrecognised_word_while_a_card_waits_is_asked_again():
    # "Yes." came back as "Jess." (a customer's name) and cost two model turns.
    js = open("src/app/static/concierge_panel.js", encoding="utf-8").read()
    turn = js[js.index("async function voiceUserTurn(text, meta) {"):]
    ask = turn.index("if (stillWaiting && voiceWords(text).length === 1 && !isVoiceFarewell(text)) {")
    assert ask < turn.index("if (voiceStage.cardNode || voiceStage.links.length) clearVoiceCard();")
    assert 'var SAY_AGAIN = "Sorry — was that a yes? Say yes or no.";' in js
    assert '"Sorry — was that a yes? Say yes or no."' in open("src/app/static/voice-live.js", encoding="utf-8").read()
    src = open("src/app/concierge_routes.py", encoding="utf-8").read()
    assert "They often answer with a single word: yes or no." in src


def test_the_draft_card_puts_its_buttons_on_the_to_line():
    js = open("src/app/static/concierge_panel.js", encoding="utf-8").read()
    card = js[js.index("function draftCardNode(d) {"):][:2200]
    assert card.index("head.appendChild(to);") < card.index("head.appendChild(acts);")
    assert "footRow.appendChild(foot);\n      footRow.appendChild(label);" in card


# ── test run section 3 (24 Sep) ──────────────────────────────────────────────
def test_a_confirmed_stay_change_does_not_wait_for_the_customer_note(monkeypatch):
    # 3.1: the owner's "yes" waited 3.5 s of 5.7 s on the model writing the note.
    import asyncio as _a
    from app import concierge_actions, record_edits
    started = []

    async def slow_note(*a, **k):
        started.append("note")
        await _a.sleep(0.5)
        return True
    monkeypatch.setattr(concierge_actions, "_queue_stay_note", slow_note)
    monkeypatch.setattr(concierge_actions.db, "get_booking", lambda *a, **k: {"id": "b1", "contact": "h@x.com"})
    monkeypatch.setattr(concierge_actions, "_stay_note_needed", lambda *a, **k: True)
    monkeypatch.setattr(record_edits, "plan_stay_change", lambda *a, **k: {"new_date": "2026-10-12"})

    async def apply(*a, **k):
        return "Done — the stay now runs to the 27th."
    monkeypatch.setattr(record_edits, "apply_stay_change", apply)

    async def go():
        import time
        t0 = time.monotonic()
        out = await concierge_actions._exec_change_stay(
            "t1", None, {"booking_id": "b1", "arrival": "a", "departure": "d", "notify": True}, False)
        took = time.monotonic() - t0
        await _a.sleep(0.6)          # the note still gets written
        return out, took
    out, took = _a.run(go())
    assert took < 0.3
    assert out["summary"].endswith("A note to the customer is being written for your approval.")
    assert started == ["note"]


def test_the_lookup_chip_never_shows_an_email_address():
    js = open("src/app/static/voice-live.js", encoding="utf-8").read()
    assert 'const detail = msg.detail && !String(msg.detail).includes("@") ? msg.detail : "";' in js


def test_a_stay_put_back_to_what_the_customer_knows_sends_no_note(monkeypatch):
    # 3.2 (24 Sep): extended then shortened back before any note went — the
    # "back to the 26th" note would tell Hannah about a change she never heard
    # of. Decided BEFORE "Done" is worded, so Done never promises a note.
    from app import concierge_actions
    monkeypatch.setattr(concierge_actions.db, "retire_pending_owner_messages",
                        lambda t, c, subj, client=None: [{"body_in": "[stay change] was 12th Oct → 26th Oct; now 12th Oct → 27th Oct"}])
    plan = {"old_when": "12th Oct → 27th Oct", "when": "12th Oct → 26th Oct", "nights": 14}
    assert concierge_actions._stay_note_needed("t1", None, {"contact": "h@x.com", "booking_ref": "BK-1"}, plan) is False
    monkeypatch.setattr(concierge_actions.db, "retire_pending_owner_messages", lambda *a, **k: [])
    assert concierge_actions._stay_note_needed("t1", None, {"contact": "h@x.com", "booking_ref": "BK-1"}, plan) is True
    src = open("src/app/concierge_actions.py", encoding="utf-8").read()
    assert 'tail = " No note needed — they still have these dates."' in src


def test_a_change_comes_with_its_card_in_voice():
    # 3.2: "Want me to change that now?" with no card — "Please." had nothing to confirm.
    from app import concierge
    p = concierge.build_business_prompt(business_name="X", today="2026-09-24", full_qa=True,
                                        voice_mode=True, actions=True)
    assert "THE CARD IS THE QUESTION." in p and "a yes with no card on screen does nothing" in p
    # Wave 3a: GPT-Live confirms only by a tap, Standard by a yes or a tap; amounts in digits.
    assert "ONLY a tap on the card counts" in p and "tap Confirm" in p and "Every card today is approve-first" in p
    assert "MONEY AND REFERENCES" in p and "'£252.50'" in p
    assert "asking for a spoken yes ('Shall I?'" not in p


def test_the_waiting_drafts_note_is_only_for_sending_or_seeing_one(monkeypatch):
    from app import concierge_routes
    monkeypatch.setattr(concierge_routes.db, "list_waiting_drafts", lambda t, client=None: [
        {"name": "Hannah", "contact": "h@x.com", "subject": "Your stay — new dates (BK-1038)"}])
    note = concierge_routes._voice_note(SimpleNamespace(tenant_id="t1", client=None), clipped=False)
    assert "ONLY if the owner asks to send, see or check one of these" in note
    assert "ignore this list and do what they asked" in note


def test_the_live_note_says_only_a_tap_counts(monkeypatch):
    # GPT-Live: a spoken yes never runs a card, so the per-turn note must not leave the
    # brain asking for one; Standard voice (a yes works there) and typed get no such line.
    from app import concierge_routes
    monkeypatch.setattr(concierge_routes.db, "list_waiting_drafts", lambda t, client=None: [])
    ctx = SimpleNamespace(tenant_id="t1", client=None)
    live = concierge_routes._voice_note(ctx, False, live=True)
    assert "only a tap on the Confirm card" in live and "never ask 'Shall I?'" in live
    assert concierge_routes._voice_note(ctx, False) is None
    assert concierge_routes._voice_note(ctx, False, live=False) is None
    # It rides with the other notes rather than replacing them.
    monkeypatch.setattr(concierge_routes.db, "list_waiting_drafts", lambda t, client=None: [
        {"name": "Hannah", "contact": "h@x.com", "subject": "Your new dates"}])
    both = concierge_routes._voice_note(ctx, True, live=True)
    from app import concierge
    assert "only a tap on the Confirm card" in both and "Hannah" in both and concierge.CLIPPED_NOTE in both


# ── test run 4.2b (24 Sep) ───────────────────────────────────────────────────
def test_what_a_lookup_found_rides_into_the_next_turn():
    # "yes, anyway" re-ran the contact search and the record lookup for Graham.
    from app import concierge, concierge_routes
    assert concierge_routes._found_in({"contacts": [{"name": "Graham Pryce", "email": "g@x.com"}]}) == "Graham Pryce <g@x.com>"
    assert "BK-1038" in concierge_routes._found_in({"bookings": [{"summary": "Boarding (BK-1038)"}]})
    src = open("src/app/concierge_routes.py", encoding="utf-8").read()
    assert src.count("found = _found_in(out)") == 2          # typed and voice
    js = open("src/app/static/concierge_panel.js", encoding="utf-8").read()
    assert "return t.found ? line + ' → found ' + t.found : line;" in js
    p = concierge.build_business_prompt(business_name="X", today="2026-09-24", full_qa=True,
                                        voice_mode=True, actions=True)
    assert "USE it for the next step" in p


def test_only_the_email_box_scrolls_on_the_voice_screen():
    # "The whole card & suggested nav button is in a scrollable container,
    # that's wrong. Only the text area inside the draft box should be scrollable."
    js = open("src/app/static/voice-overlay-css.js", encoding="utf-8").read()
    stage = js[js.index("#voice-live-root .vl-stage-scroll {"):][:200]
    assert "overflow-y" not in stage and "max-height" not in stage
    assert "#voice-live-root .vl-stage-scroll textarea {\n  height: 12vh; min-height: 4.5rem; max-height: 12vh;\n  overflow-y: auto;" in js
    tpl = open("src/app/templates/app/_concierge_panel.html", encoding="utf-8").read()
    assert "overflow-x: hidden; overflow-y: auto; overscroll-behavior: contain; isolation: isolate; }" in tpl   # the done deck still scrolls
    panel = open("src/app/static/concierge_panel.js", encoding="utf-8").read()
    assert "Edit the text here if you like" not in panel and "Edit the draft here if you like" not in panel



# ── Context and invented speech (24 Sep) ────────────────────────────────────
def test_voice_context_api_remains_separate_from_removed_context_button(monkeypatch):
    from app import concierge_routes
    monkeypatch.setattr(concierge_routes, "_voice_note", lambda ctx, clipped, member=None: "[for reference only — …]")
    out = concierge_routes.voice_context(SimpleNamespace(tenant_id="t1", client=None))
    assert out == {"note": "[for reference only — …]"}
    js = open("src/app/static/concierge_panel.js", encoding="utf-8").read()
    tpl = open("src/app/templates/app/_concierge_panel.html", encoding="utf-8").read()
    assert "cz-context-chip" not in js and "contextView()" not in js
    assert "cz-context-chip" not in tpl and "cz-context-details" not in js


def test_a_long_sentence_from_a_blip_is_held_not_sent():
    # Sat silent: "Yes, she's coming. Yes, I will confirm the booking for Mabel
    # with Jess" at peak 0.065 vs a 0.051 line — names from the hint, invented.
    js = open("src/app/static/voice-live.js", encoding="utf-8").read()
    f = js[js.index("function soundsInvented(u) {"):][:500]
    assert "if (words >= 6 && u.peak < speechGate() * 1.6) return true;" in f
    assert "if (secs > 0.2 && words >= 5 && words / secs > 6) return true;" in f
    heard = js[js.index("function heardUtterance(u) {"):][:2400]
    assert heard.index('if (u.peak < speechGate()) { holdWords(u, "quiet"); return; }') < heard.index('if (soundsInvented(u)) { holdWords(u, "unlikely"); return; }')
    assert heard.index("deps.expectsAnswer(u.text)") < heard.index("soundsInvented(u)")   # a yes to the card still goes



def test_an_action_carries_only_the_fields_it_uses():
    # Replaying real cards (24 Sep): a stay change 277 -> 156 tokens, 6.1 -> 3.8 s.
    import json as _j
    from app import concierge
    _, _, a = concierge._parse_business(_j.dumps({"reply": "Shall I?", "links": [], "action": {
        "kind": "change_stay", "fields": _j.dumps({"booking_id": "BK-1038", "end_date": "2026-10-27", "notify": "yes"})}}))
    assert (a["kind"], a["booking_id"], a["end_date"], a["notify"], a["name"]) == ("change_stay", "BK-1038", "2026-10-27", "yes", "")
    _, _, a = concierge._parse_business(_j.dumps({"reply": "x", "links": [], "action": {"kind": "block_date", "fields": "{bad"}}))
    assert a["kind"] == "block_date" and not a["new_date"]          # nothing guessed
    _, _, a = concierge._parse_business(_j.dumps({"reply": "x", "links": [], "action": {"kind": "bulk", "fields": _j.dumps(
        {"items": [{"kind": "invoice_reminder_draft", "invoice_id": "INV-1008"}]})}}))
    assert a["items"] == [dict({f: "" for f in concierge._ACTION_FIELDS}, kind="invoice_reminder_draft", invoice_id="INV-1008")]



def test_voice_context_has_no_button_or_progress_meter():
    js = open("src/app/static/concierge_panel.js", encoding="utf-8").read()
    tpl = open("src/app/templates/app/_concierge_panel.html", encoding="utf-8").read()
    # Context length remains part of the voice request; only its redundant UI control is removed.
    assert "CONTEXT_TURNS" in js and "HISTORY_SEED_TURNS = CONTEXT_TURNS;" in js
    assert "turns.slice(0, -1).slice(-CONTEXT_TURNS)" in js           # what's sent = what context counts
    assert "cz-context-chip" not in js and "contextView()" not in js
    assert "cz-context-chip" not in tpl and "cz-ctx-fill" not in tpl and "cz-ctx-hold" not in tpl


# ── test run 5.1 (24 Sep) ────────────────────────────────────────────────────
def test_a_block_card_lists_a_stay_already_running_through_the_day(monkeypatch):
    # The Concierge said Rufus's stay runs through the 11th; the card said
    # "Nothing is booked on those days" — it only looked at bookings STARTING then.
    from datetime import datetime as _dt
    from app import concierge_actions as ca, record_edits
    monkeypatch.setattr(ca.db, "get_scheduling_config", lambda t, client=None: {"blackout_dates": []})
    monkeypatch.setattr(ca.db, "list_active_bookings", lambda *a, **k: [])
    rufus = {"id": "b-r", "status": "confirmed", "booking_ref": "BK-1017", "summary": "Boarding for Rufus",
             "booking_date": "2026-10-09"}
    monkeypatch.setattr(ca.db, "stays_running_into", lambda t, day, client=None: [rufus])
    monkeypatch.setattr(ca.db, "get_booking", lambda bid, t, client=None: rufus)
    monkeypatch.setattr(record_edits, "stay_times", lambda b: (_dt(2026, 10, 9, 8), _dt(2026, 10, 12, 17)))
    monkeypatch.setattr(ca, "datetime", type("D", (_dt,), {"now": classmethod(lambda cls, tz=None: _dt(2026, 9, 24, tzinfo=tz))}))
    summary, payload, details, _ = ca._stage_block_date("t1", None, {"kind": "block_date", "new_date": "2026-10-11"})
    assert payload["stranded"] == 1 and "BK-1017" in details[1]
    # a stay that left before the day isn't listed
    monkeypatch.setattr(record_edits, "stay_times", lambda b: (_dt(2026, 10, 5, 8), _dt(2026, 10, 9, 17)))
    summary, payload, details, _ = ca._stage_block_date("t1", None, {"kind": "block_date", "new_date": "2026-10-11"})
    assert payload["stranded"] == 0 and details[1] == "Nothing is booked on those days"


def test_a_proposal_card_is_built_like_a_draft_card():
    # Kris 2026-09-24: "the Diary block UI doesn't match the drafts. Use the same
    # style and buttons (Thumb & Bin)".
    js = open("src/app/static/concierge_panel.js", encoding="utf-8").read()
    card = js[js.index("function proposalCardNode(p) {"):js.index("function proposalCard(p) {")]
    assert "card.className = 'cz-draft cz-proposal';" in card
    assert "ok.innerHTML = THUMB_UP;" in card and "no.innerHTML = SKIP_BIN;" in card
    assert "no.className = 'cz-draft-btn cz-draft-bin';" in card
    assert "ta.className = 'cz-draft-text';" in card
    assert "'Confirm';\n      var no" not in card          # no text buttons


def test_unblock_reopens_only_the_days_that_were_blocked(monkeypatch):
    # Test run 5 (Kris 2026-09-24): "then I will ask for it to unblock".
    import asyncio as _a
    from app import concierge, concierge_actions as ca
    saved = {}
    monkeypatch.setattr(ca.db, "get_scheduling_config", lambda t, client=None: {
        "blackout_dates": ["2026-10-04", "2026-10-05", "2026-10-06", "2026-12-25"]})
    summary, payload, details, _ = ca._stage_unblock_date("t1", None, {"new_date": "2026-10-04", "end_date": "2026-10-08"})
    assert payload["days"] == ["2026-10-04", "2026-10-05", "2026-10-06"]
    assert "2 of the days asked for weren't blocked" in details[1]
    try:
        ca._stage_unblock_date("t1", None, {"new_date": "2026-10-11"})
        assert False
    except ca.ActionError as e:
        assert "isn't blocked" in str(e)
    monkeypatch.setattr(ca.db, "upsert_scheduling_config", lambda t, fields, client=None: saved.update(fields))
    monkeypatch.setattr(ca.db, "log_agent_event", lambda *a, **k: None)
    out = _a.run(ca._exec_unblock_date("t1", None, payload, False))
    assert saved["blackout_dates"] == ["2026-12-25"] and out["summary"].startswith("Done — the diary is open again")
    # wired everywhere a kind has to be
    assert "unblock_date" in concierge.BUSINESS_ACTION_KINDS
    assert "unblock_date" in ca._STAGERS and "unblock_date" in ca._EXECUTORS
    assert "unblock_date" in ca.allowed_kinds(full_qa=True, scheduling_on=True)
    assert "unblock_date: 'Diary reopen'" in open("src/app/static/concierge_panel.js", encoding="utf-8").read()



def test_a_waiting_card_has_no_caption():
    # Kris 2026-09-24: "Remove the Diary open confirm to run text".
    js = open("src/app/static/concierge_panel.js", encoding="utf-8").read()
    card = js[js.index("function proposalCardNode(p) {"):js.index("function proposalCard(p) {")]
    assert "confirm to run" not in card and "var HEAD_READY = '';" in card
    assert "function setState(text) { head.textContent = text; foot.hidden = !text; }" in card
    assert ".cz-draft-foot[hidden] { display: none; }" in open("src/app/templates/app/_concierge_panel.html", encoding="utf-8").read()



def test_notifications_carry_no_trailing_ellipsis():
    # Kris 2026-09-24: "Remove the Ellipsis from the notification chips".
    js = open("src/app/static/voice-live.js", encoding="utf-8").read()
    n = js[js.index("function notify(text) {"):][:250]
    assert 'text = String(text).replace(/\s*…\s*$/, "");' in n


# ── test run 5.2 (24 Sep) ────────────────────────────────────────────────────
def test_overdue_invoices_are_the_late_ones_with_how_late_and_reminders(monkeypatch):
    # "Which invoices are overdue?" — Meg's INV-1008, 14 days late, one reminder;
    # a sent invoice past its due date counts even before the nightly job stamps it.
    from datetime import date, datetime, timedelta, timezone
    from app import concierge_data
    today = datetime.now(timezone.utc).date()   # the code counts in UTC; local date broke it 23:00-01:00 BST
    rows = [
        {"id": "i8", "invoice_ref": "INV-1008", "status": "overdue", "customer_name": "Meg Ashworth",
         "total": 30.5, "due_date": (today - timedelta(days=14)).isoformat(), "reminders_sent": 1},
        {"id": "i9", "invoice_ref": "INV-1009", "status": "sent", "customer_name": "Late but unstamped",
         "total": 10, "due_date": (today - timedelta(days=2)).isoformat()},
        {"id": "i10", "invoice_ref": "INV-1010", "status": "sent", "customer_name": "Not due",
         "total": 20, "due_date": (today + timedelta(days=5)).isoformat()},
        {"id": "i11", "invoice_ref": "INV-1011", "status": "paid", "customer_name": "Paid",
         "total": 5, "due_date": (today - timedelta(days=30)).isoformat()},
    ]
    monkeypatch.setattr(concierge_data.db, "list_invoices", lambda t, client=None: rows)
    out = concierge_data._list_invoices("t1", None, {"status": "overdue"})
    items = out.get("invoices") or out.get("items") or []
    assert [i["ref"] for i in items] == ["INV-1008", "INV-1009"]
    assert items[0]["days_overdue"] == 14 and items[0]["customer"] == "Meg Ashworth"


def test_a_stalled_draft_call_is_raced_too(monkeypatch):
    """Run log F16 (plumber take 2026-09-25): ~30 s of Sarah's 56 s draft was
    stall. llm.create takes the same hedge when asked, and drafting asks."""
    monkeypatch.setattr(llm, "_log_call", lambda *a, **k: None)
    log = []
    msg = asyncio.run(llm.create(client=_client([2.0, 0.01], log), hedge_after=0.1,
                                 model="m", max_tokens=10, messages=[]))
    assert msg.tag == 1 and ("open", 1) in log
    from app import agent
    assert agent.DRAFT_HEDGE_AFTER_S and agent.DRAFT_HEDGE_AFTER_S <= 10
