"""Fix mode — thumbs-down on a draft opens the assistant (2026-07-02).

Engine (pure): the fix prompt injects the draft + current rules as data and
keeps the no-invented-facts rule; _parse_fix coerces malformed actions to None
(redraft is the only kind valid with empty content).

Routes: /concierge/fix/apply executes exactly ONE approved action — never rule /
knowledge ingest / booking requirement (deduped) / plain redraft — then
re-drafts the reply and returns it for the page to swap in. Tenant-scoped via
get_email_log_entry (a foreign log_id 404s).

Run: python -m pytest tests/test_concierge_fix.py -q
"""
import pytest
from fastapi.testclient import TestClient

import app.concierge_routes as routes
import app.dashboard as dashboard
import app.pipeline as pipeline_mod
from app.auth import TenantCtx, require_tenant
from app.concierge import FIX_SCHEMA, _parse_fix, build_fix_prompt
from app.main import app
from app.security import require_csrf_header


# ── engine ────────────────────────────────────────────────────────────────────
def test_fix_prompt_injects_context_and_pins_honesty():
    p = build_fix_prompt({"draft_reply": "We open Sundays."}, "Never offer discounts",
                         ["Vehicle registration"])
    assert "<draft_context>" in p and "We open Sundays." in p
    assert "<current_never_rules>" in p and "Never offer discounts" in p
    assert "<current_booking_requirements>" in p and "Vehicle registration" in p
    assert "NEVER invent" in p                       # knowledge content is operator-stated only
    assert "NOTHING changes" in p                    # approve-before-apply is explicit


def test_fix_prompt_renders_full_requirement_fields():
    # full intake fields (label + required + hint) — so "why didn't it ask for X?"
    # can be answered from wording, not just presence
    p = build_fix_prompt(requirement_labels=[
        {"label": "Phone", "required": True, "hint": "a daytime number"},
        {"label": "Colour", "required": False, "hint": ""}])
    assert "- Phone (required) — hint: a daytime number" in p
    assert "- Colour (optional)" in p
    assert "UPDATES that requirement" in p           # the reword path is offered


def test_fix_schema_action_kinds():
    assert FIX_SCHEMA["properties"]["action"]["properties"]["kind"]["enum"] == \
        ["", "never", "knowledge", "requirement", "redraft"]


# ── grounding trace ("why did it say this?") ──────────────────────────────────
def test_fix_prompt_has_the_trace_rule():
    p = build_fix_prompt()
    assert "search_grounding" in p
    assert "INFERRED" in p                            # the honest no-source answer
    from app.concierge import FIX_TOOLS
    (tool,) = FIX_TOOLS
    assert tool["name"] == "search_grounding"
    assert tool["input_schema"]["required"] == ["phrase"]


def test_settings_hits_matches_significant_words_only():
    tcfg = {"custom_instructions": "Always invite the customer to call us on 01482 555555.",
            "sign_off": "Cheers, The Team", "tone": ""}
    hits = routes._settings_hits(tcfg, "the number we have on file")
    # no significant (≥3-char, non-stopword) phrase word appears in any settings field
    assert [h["setting"] for h in hits] == []
    hits2 = routes._settings_hits(tcfg, "invite the customer to phone us")
    assert [h["setting"] for h in hits2] == ["custom_instructions"]
    assert "01482" in hits2[0]["excerpt"]


def test_settings_hits_empty_phrase_and_config():
    assert routes._settings_hits({}, "anything at all here") == []
    assert routes._settings_hits({"do_list": "Offer the brochure"}, "a of to") == []


def test_fix_prompt_offers_the_note_instead_of_the_knowledge_page():
    # Kris 2026-09-04: the fix panel WRITES the note; it never sends the operator
    # to the Knowledge page to type the fact in themselves.
    p = build_fix_prompt(knowledge_notes=["Out-of-hours rates", "Boiler servicing"])
    assert "NEVER send the operator off to the Knowledge page" in p
    assert "<current_knowledge_notes>" in p and "- Out-of-hours rates" in p
    assert "REWRITES that note in place" in p          # correcting beats appending


def test_parse_fix_actions():
    _, a = _parse_fix('{"reply":"Prepared.","action":{"kind":"never","content":"Never offer Sundays"}}')
    assert a == {"kind": "never", "content": "Never offer Sundays", "label": ""}
    # a knowledge note carries its name; every other kind drops one it shouldn't have
    assert _parse_fix('{"reply":"x","action":{"kind":"knowledge","content":"£85 call-out",'
                      '"label":"Call-out rates"}}')[1] == \
        {"kind": "knowledge", "content": "£85 call-out", "label": "Call-out rates"}
    assert _parse_fix('{"reply":"x","action":{"kind":"never","content":"No Sundays",'
                      '"label":"stray"}}')[1]["label"] == ""
    # redraft is valid with empty content; other kinds are not
    assert _parse_fix('{"reply":"x","action":{"kind":"redraft","content":""}}')[1] == \
        {"kind": "redraft", "content": "", "label": ""}
    assert _parse_fix('{"reply":"x","action":{"kind":"knowledge","content":"  "}}')[1] is None
    assert _parse_fix('{"reply":"x","action":{"kind":"nonsense","content":"y"}}')[1] is None
    assert _parse_fix("not json") == ("not json", None)


# ── /concierge/fix/apply ──────────────────────────────────────────────────────
@pytest.fixture
def client():
    from app import security
    security._BUCKETS.clear()   # apply is rate-limited 10/5min — per TEST, not per module
    app.dependency_overrides[require_tenant] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf_header] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


class Env:
    def __init__(self, monkeypatch, *, entry=True, labels=("Phone",), sources=()):
        self.never, self.ingested, self.saved_fields, self.redrafted = [], [], [], []

        self.instructions = []

        async def _redraft(tid, log_id, client=None, owner_instruction=""):
            self.redrafted.append(log_id)
            self.instructions.append(owner_instruction)
            return "Fresh draft."

        async def _ingest(tid, source, text, client=None, **kw):
            self.ingested.append((source, text, kw.get("mailbox_id")))
            return 1

        monkeypatch.setattr(routes.db, "list_knowledge_sources",
                            lambda tid, **k: [dict(s) for s in sources])
        self.stored: dict[str, list[str]] = {}
        monkeypatch.setattr(routes.db, "knowledge_source_chunks",
                            lambda tid, source, client=None, mailbox_id=None: list(self.stored.get(source, [])))

        monkeypatch.setattr(routes.db, "get_email_log_entry",
                            lambda lid, tid, **k: ({"id": lid, "from_address": "a@x.com",
                                                    "subject": "s", "body_in": "b",
                                                    "draft_reply": "d"} if entry else None))
        monkeypatch.setattr(routes.db, "get_tenant_config", lambda *a, **k: {"never_do": "Old rule"})
        monkeypatch.setattr(routes.db, "get_scheduling_config", lambda *a, **k: {
            "intake_fields": [{"label": l, "required": True, "hint": ""} for l in labels]})
        monkeypatch.setattr(routes.db, "save_scheduling_intake",
                            lambda tid, fields, **k: self.saved_fields.append(fields))
        monkeypatch.setattr(routes.kb, "is_configured", lambda: True)
        monkeypatch.setattr(routes.kb, "ingest", _ingest)
        monkeypatch.setattr(dashboard, "_append_never_rule",
                            lambda tid, rule, client=None: self.never.append(rule) or True)
        monkeypatch.setattr(pipeline_mod, "redraft_email_log", _redraft)


def _apply(client, **body):
    return client.post("/concierge/fix/apply", json={"log_id": "log1", **body})


def test_apply_never_rule_then_redrafts(client, monkeypatch):
    env = Env(monkeypatch)
    r = _apply(client, kind="never", content="Never promise same-day")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] and data["draft"] == "Fresh draft."
    assert env.never == ["Never promise same-day"]
    assert env.redrafted == ["log1"]


def test_apply_knowledge_ingests_under_a_fix_source(client, monkeypatch):
    env = Env(monkeypatch)
    r = _apply(client, kind="knowledge", content="MOTs are £54.85, book 3 days ahead.")
    assert r.status_code == 200 and r.json()["applied"] == "Knowledge added"
    (source, text, _), = env.ingested
    assert source.startswith("Assistant fix ·") and "£54.85" in text
    assert env.redrafted == ["log1"]


def test_apply_knowledge_names_the_note(client, monkeypatch):
    # The card carries the note's name — the operator can see and edit it before saving.
    env = Env(monkeypatch)
    r = _apply(client, kind="knowledge", label="Out-of-hours rates",
               content="£140 first hour, then £65/hr.")
    assert r.json()["applied"] == "Knowledge added"
    assert env.ingested == [("Out-of-hours rates", "£140 first hour, then £65/hr.", None)]


def test_apply_knowledge_rewrites_an_existing_note_in_its_own_scope(client, monkeypatch):
    # Naming a note that exists CORRECTS it (kb.ingest replaces a source) instead of
    # leaving the agent two contradictory facts — and keeps that note's mailbox scope.
    env = Env(monkeypatch, sources=[{"source": "Out-of-hours rates", "mailbox_id": "mb1"}])
    r = _apply(client, kind="knowledge", label="Out-of-hours rates",
               content="£140 first hour, then £65/hr.")
    assert r.json()["applied"] == "Knowledge rewritten"
    assert env.ingested == [("Out-of-hours rates", "£140 first hour, then £65/hr.", "mb1")]


def test_knowledge_note_names_survive_a_listing_failure(monkeypatch):
    # Prompt context is best-effort: the diagnosis still runs without the note list.
    def boom(*a, **k):
        raise RuntimeError("postgrest down")
    monkeypatch.setattr(routes.db, "list_knowledge_sources", boom)
    assert routes._knowledge_note_names(TenantCtx(tenant_id="t1", client=None)) == []


def test_apply_requirement_appends_and_dedupes(client, monkeypatch):
    env = Env(monkeypatch)
    r = _apply(client, kind="requirement", content="Vehicle registration")
    assert r.status_code == 200 and r.json()["applied"] == "Booking requirement added"
    assert env.saved_fields[0][-1]["label"] == "Vehicle registration"
    # duplicate (case-insensitive, no new hint) → no second save
    env2 = Env(monkeypatch, labels=("Vehicle registration",))
    r2 = _apply(client, kind="requirement", content="vehicle REGISTRATION")
    assert r2.json()["applied"] == "Requirement already existed"
    assert env2.saved_fields == []


def test_apply_requirement_label_hint_form_adds_or_rewords(client, monkeypatch):
    # 'Label — hint' on a NEW label → added with the guidance attached
    env = Env(monkeypatch)
    r = _apply(client, kind="requirement", content="Vehicle registration — the full number plate")
    assert r.json()["applied"] == "Booking requirement added"
    added = env.saved_fields[0][-1]
    assert added["label"] == "Vehicle registration" and added["hint"] == "the full number plate"
    # 'Label — hint' on an EXISTING label → that field's hint is updated in place
    env2 = Env(monkeypatch, labels=("Phone", "Vehicle registration"))
    r2 = _apply(client, kind="requirement", content="vehicle registration — reg plate as on the V5C")
    assert r2.json()["applied"] == "Booking requirement reworded"
    (saved,) = env2.saved_fields
    assert [f["label"] for f in saved] == ["Phone", "Vehicle registration"]   # no duplicate row
    assert saved[1]["hint"] == "reg plate as on the V5C" and saved[0]["hint"] == ""


def test_apply_redraft_only_and_bad_kind(client, monkeypatch):
    env = Env(monkeypatch)
    assert _apply(client, kind="redraft", content="").json()["applied"] == "Re-drafted"
    assert env.redrafted == ["log1"]
    assert _apply(client, kind="explode", content="x").status_code == 400
    assert _apply(client, kind="never", content="   ").status_code == 400


def test_a_redraft_carries_the_owners_words(client, monkeypatch):
    """Rehearsal 2026-09-26: "offer him Sunday morning out of hours at £140"
    became a bare redraft and the new draft ignored it."""
    from app import concierge
    act = concierge._coerce_fix_action({"kind": "redraft",
                                        "content": "Offer him Sunday morning out of hours at £140"})
    assert act["content"] == "Offer him Sunday morning out of hours at £140"
    env = Env(monkeypatch)
    _apply(client, kind="redraft", content=act["content"])
    assert env.instructions == ["Offer him Sunday morning out of hours at £140"]
    assert "the instruction is lost" in concierge.FIX_SYSTEM if hasattr(concierge, "FIX_SYSTEM") else True


def test_apply_404s_on_a_foreign_draft(client, monkeypatch):
    Env(monkeypatch, entry=False)
    assert _apply(client, kind="redraft", content="").status_code == 404


# ── /concierge/fix streams the trace live ─────────────────────────────────────
def test_fix_streams_tool_events_then_done(client, monkeypatch):
    import json as _json
    from app.concierge import FixReply

    Env(monkeypatch)

    async def fake_answer_fix(question, history, draft_ctx=None, never_rules=None,
                              requirement_labels=None, business_profile=None,
                              tool_executor=None, on_tool=None, **kw):
        # the engine pings on_tool as each grounding search starts
        await on_tool({"phrase": "the number we have on file"})
        return FixReply(reply="That was inferred — it's not in your data.",
                        action={"kind": "never", "content": "Never mention phone numbers"},
                        usage={"input_tokens": 1, "output_tokens": 1}, model="m")

    monkeypatch.setattr(routes.concierge, "answer_fix", fake_answer_fix)
    monkeypatch.setattr(routes.usage, "record_anthropic", lambda *a, **k: None)

    r = client.post("/concierge/fix", json={"log_id": "log1", "question": "why the number?"})
    assert r.status_code == 200
    events = [_json.loads(l) for l in r.text.splitlines() if l.strip()]
    assert events[0]["type"] == "tool"
    assert events[0]["phrase"] == "the number we have on file"
    # Ghost-trail fields (docs/modules/module-concierge-transparency.md §A).
    assert events[0]["label"] == "Checked knowledge & settings"
    assert events[0]["detail"] == "the number we have on file"
    assert events[-1]["type"] == "done"
    assert "inferred" in events[-1]["reply"]
    assert events[-1]["action"]["kind"] == "never"
    assert events[-1]["trail"] == [{"name": "search_grounding",
                                    "label": "Checked knowledge & settings",
                                    "detail": "the number we have on file"}]


def test_fix_stream_degrades_to_a_done_event_on_error(client, monkeypatch):
    import json as _json

    Env(monkeypatch)

    async def boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr(routes.concierge, "answer_fix", boom)
    r = client.post("/concierge/fix", json={"log_id": "log1", "question": "help"})
    events = [_json.loads(l) for l in r.text.splitlines() if l.strip()]
    assert len(events) == 1 and events[0]["type"] == "done"
    assert events[0]["action"] is None and "Sorry" in events[0]["reply"]


def test_a_fix_can_queue_further_actions_shown_one_at_a_time():
    # Kris 2026-09-04: "I can only propose one at a time — which first?" was an
    # extra turn. The first fix is `action`; the rest ride in `queue`, in order.
    from app.concierge import _parse_fix_queue, FixReply
    text = ('{"reply":"Two fixes.","action":{"kind":"knowledge","content":"trim £35","label":"Prices"},'
            '"queue":[{"kind":"never","content":"Never volunteer the multi-horse rate","label":""},'
            '{"kind":"nonsense","content":"x","label":""},{"kind":"redraft","content":"","label":""}]}')
    assert _parse_fix(text)[1] == {"kind": "knowledge", "content": "trim £35", "label": "Prices"}
    # a queued "redraft" behind an action that redrafts anyway is dropped
    # (Kris 2026-09-04: two offers at once)
    assert _parse_fix_queue(text) == [
        {"kind": "never", "content": "Never volunteer the multi-horse rate", "label": ""}]
    # still diagnosing: no first action means no queue either
    assert _parse_fix_queue('{"reply":"?","action":{"kind":"","content":"","label":""},'
                            '"queue":[{"kind":"never","content":"x","label":""}]}') == []
    assert _parse_fix_queue("not json") == []
    assert FIX_SCHEMA["required"] == ["reply", "action", "queue"]
    assert FixReply("r", None, {}, "m").queue == []


def test_the_fix_prompt_asks_for_a_queue_not_a_which_first_question():
    p = build_fix_prompt()
    assert "the rest in `queue`, in order" in p
    assert "NEVER ask which fix to do first" in p


def test_a_redraft_proposed_first_is_moved_behind_the_real_fix():
    """Run #7 (Kris, 2026-09-05): 'Redraft, then Never rule' burned a draft —
    the never rule applied AFTER the reply it was meant to shape. The redraft
    is implied by whichever fix is applied last."""
    from app.concierge import _order_fix
    never = {"kind": "never", "content": "Never say a colleague will arrange it", "label": ""}
    redraft = {"kind": "redraft", "content": "", "label": ""}
    assert _order_fix(redraft, [never]) == (never, [])
    assert _order_fix(never, []) == (never, [])
    assert _order_fix(redraft, []) == (redraft, [])
    assert _order_fix(None, []) == (None, [])


def test_a_rewrite_never_shrinks_a_note(client, monkeypatch):
    """Run #8: the trace clipped the 520-char Rules note to 400, the assistant
    read the clip as truncation and offered a 419-char rewrite that dropped the
    DVSA sentence. A rewrite shorter than the stored note is refused with the
    reason; one that carries the note plus the correction goes through."""
    env = Env(monkeypatch, sources=[{"source": "Rules", "mailbox_id": None}])
    env.stored["Rules"] = ["The car is not available for a test unless Jo has seen you driving "
                           "at test standard. The DVSA needs ten working days' notice to move a test. "
                           "Lessons are cancelled free with 48 hours' notice."]
    short = "The car is not available for a test unless Jo has seen you driving at test standard."
    r = _apply(client, kind="knowledge", label="Rules", content=short)
    assert r.status_code == 409
    assert "shorter than the note it replaces" in r.json()["detail"] and "Rules" in r.json()["detail"]
    assert env.ingested == [] and env.redrafted == []
    whole = env.stored["Rules"][0] + " Test-day hire is £120."
    r = _apply(client, kind="knowledge", label="Rules", content=whole)
    assert r.status_code == 200 and r.json()["applied"] == "Knowledge rewritten"
    assert env.ingested[0][1] == whole and env.redrafted == ["log1"]
    # a note whose text cannot be read never blocks the fix
    env.stored["Rules"] = []
    assert _apply(client, kind="knowledge", label="Rules", content=short).status_code == 200


def test_the_trace_carries_a_chunk_whole():
    from app.concierge_routes import _whole_chunk, _TRACE_CHUNK_CHARS
    note = "The car is not available for a test unless Jo has seen you driving. " * 8   # 552 chars
    assert _whole_chunk(note) == note                       # no 400-char clip (run #8)
    big = "x" * (_TRACE_CHUNK_CHARS + 50)
    assert _whole_chunk(big).endswith("[…the stored note continues for 50 more characters]")
    p = build_fix_prompt(knowledge_notes=["Rules"])
    assert "A traced note is shown WHOLE" in p and "never shorter, never dropping a sentence" in p


def test_an_empty_fix_reply_is_retried_then_answered_honestly(client, monkeypatch):
    """Run #8 live: the model returned {"reply": ""} with no action and no trace;
    the widget drew an empty bubble. One retry; if still empty, an honest line."""
    import json as _json
    from app.concierge import FixReply
    Env(monkeypatch)
    calls = []

    async def fake_answer_fix(question, history, **kw):
        calls.append(question)
        if len(calls) == 1:
            return FixReply(reply="", action={"kind": "", "content": "", "label": ""},
                            usage={"input_tokens": 1, "output_tokens": 1}, model="m")
        return FixReply(reply="It came from your Rules note.", action=None,
                        usage={"input_tokens": 1, "output_tokens": 1}, model="m")

    monkeypatch.setattr(routes.concierge, "answer_fix", fake_answer_fix)
    monkeypatch.setattr(routes.usage, "record_anthropic", lambda *a, **k: None)
    r = client.post("/concierge/fix", json={"log_id": "log1", "question": "where from?"})
    done = [_json.loads(l) for l in r.text.splitlines() if l.strip()][-1]
    assert len(calls) == 2 and done["reply"] == "It came from your Rules note."

    async def always_empty(question, history, **kw):
        return FixReply(reply="  ", action=None, usage={"input_tokens": 1, "output_tokens": 1}, model="m")
    monkeypatch.setattr(routes.concierge, "answer_fix", always_empty)
    r = client.post("/concierge/fix", json={"log_id": "log1", "question": "where from?"})
    done = [_json.loads(l) for l in r.text.splitlines() if l.strip()][-1]
    assert done["reply"].startswith("I couldn't work that one out just now")
    js = __import__("pathlib").Path(routes.__file__).resolve().parents[1].joinpath("app", "static", "concierge.js").read_text(encoding="utf-8")
    assert 'throw new Error("empty reply")' in js



def test_fix_prompt_asks_before_it_presumes():
    """Kris 2026-09-21: the assistant guessed at faults nobody had named. Until the
    operator says what's wrong, the only move is one question."""
    p = build_fix_prompt()
    assert "ASK, DON'T PRESUME" in p
    assert "reply with ONE short question" in p
    assert p.index("ASK, DON'T PRESUME") < p.index("TRACE before you blame")


def test_thumbs_down_opens_with_a_question_not_a_canned_complaint():
    js = __import__("pathlib").Path(__file__).resolve().parents[1].joinpath("src", "app", "static", "concierge.js").read_text(encoding="utf-8")
    assert "help me fix it" not in js                       # nothing posted in the operator's name
    assert 'FIX_GREETING = "What\'s wrong with it?' in js
    assert "return FIX_LOG_ID ? FIX_GREETING : (SCENARIO ? SCENARIO_GREETING : GREETING)" in js   # the fix greeting still wins; scenario mode is the next case
