"""Full-auto holds and flags (Kris 2026-09-26: "test Agent flagging during
Fullauto… including acting on custom rules from the business").

The live test on the plumber rig (nine emails, full-auto, Hartwell's own
rules) found replies going out that the owner needed to see:
  A4 a £6,000 refit under "check with Dan over £1,000" → sent, promising Dan
     would call;
  A6 heat pumps, not in the knowledge → sent with invented "we don't do that";
  A7 home dialysis + out-of-hours held back → sent, no Today flag, "closed on
     Sundays";
  A8 a photo → "I can see the boiler display in the image" with nothing seen;
  A1 every hold read "Draft ready" with no reason; A2 "complaining" missed
     the "complaint" keyword; A3 a Never rule applied and nobody knew;
  A9 a gas smell — the right fast reply, and nobody told.

Run: python -m pytest tests/test_full_auto_holds.py -q
"""
import asyncio
import json
from types import SimpleNamespace

from app import agent, pipeline, push, urgency
from tests.test_push import EMAIL, Env


def _result(**kw):
    return agent.DraftResult(text="Reply.", usage={"input_tokens": 1}, model="m", **kw)


def _with(monkeypatch, env, result):
    async def fake_draft(cfg, email, **k):
        env.draft_kwargs = k
        return result
    monkeypatch.setattr(pipeline.agent, "draft_reply", fake_draft)


def test_an_owner_rule_holds_the_auto_send_and_says_why(monkeypatch):
    env = Env(monkeypatch, auto_send=True)
    _with(monkeypatch, env, _result(owner_check="check with Dan before any job over £1,000"))
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1", auto_send_ok=True))
    assert env.sent == []
    assert env.pushes[0][1]["reason"] == "your rule: check with Dan before any job over £1,000"


def test_needs_attention_and_a_deferral_hold_too(monkeypatch):
    env = Env(monkeypatch, auto_send=True)
    _with(monkeypatch, env, _result(thread_state="needs_attention", category_reason="Weeping valve after our job"))
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1", auto_send_ok=True))
    assert env.sent == [] and env.pushes[0][1]["reason"] == "needs your attention"
    env = Env(monkeypatch, auto_send=True)
    _with(monkeypatch, env, _result(deferred=True, missing="whether we install heat pumps"))
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1", auto_send_ok=True))
    assert env.sent == [] and "couldn't answer: whether we install heat pumps" in env.pushes[0][1]["reason"]


def test_a_plain_reply_still_sends_and_a_rule_it_applied_is_noted(monkeypatch):
    env = Env(monkeypatch, auto_send=True)
    notes = []
    monkeypatch.setattr(push, "notify", lambda tid, **k: notes.append(k))
    monkeypatch.setattr(pipeline.db, "get_tenant_config", lambda *a, **k: {
        "auto_send": True, "never_do": "Never offer a discount, price match or a free callout."})
    _with(monkeypatch, env, _result(rule_applied="never price match",
                                    request_summary="Asked us to price match a £60 callout"))
    asyncio.run(pipeline.draft_for_email("t1", EMAIL, source_message_id="m1", auto_send_ok=True))
    assert env.sent and env.pushes == []
    assert notes[0]["kind"] == "owner_rule" and "never price match" in notes[0]["body"]


def test_the_job_flags_fire_on_an_auto_sent_reply(monkeypatch):
    env = Env(monkeypatch, auto_send=True)
    notes = []
    monkeypatch.setattr(push, "notify", lambda tid, **k: notes.append(k))
    monkeypatch.setattr(pipeline.db, "effective_config", lambda *a, **k: {
        "auto_send": True, "alert_words": "gas smell, smell of gas, carbon monoxide, CO alarm"})
    email = {**EMAIL, "body": "Strong smell of gas in the hallway near the meter."}
    _with(monkeypatch, env, _result())
    asyncio.run(pipeline.draft_for_email("t1", email, source_message_id="m1", auto_send_ok=True))
    assert env.sent and [n["kind"] for n in notes] == ["alert_words"]
    words = "gas smell, smell of gas, carbon monoxide, CO alarm"
    assert urgency.alert_flag("t1", contact="a@x.co", name="A", body="Our CO alarm is going off",
                              phrases=words) is True
    assert urgency.alert_flag("t1", contact="a@x.co", name="A", body="Gas Safe registered?",
                              phrases=words) is False
    # No words set (every business that hasn't chosen any): nothing fires.
    assert urgency.alert_flag("t1", contact="a@x.co", name="A", body="smell of gas", phrases="") is False


def test_escalation_words_catch_their_variants():
    cfg = {"escalation": "complaint, refund, solicitor"}
    hit = lambda t: agent.escalation_match({"subject": "", "body": t}, cfg)  # noqa: E731
    assert hit("thinking of complaining to Checkatrade") == "complaint"
    assert hit("our solicitors will be in touch") == "solicitor"
    assert hit("uncomplainingly patient") is None


def test_invented_we_dont_do_that_is_caught_unless_the_knowledge_says_it():
    bits = agent._unsourced_bits(
        "Heat pump installations aren't something we offer. We're also not MCS certified.", "")
    assert len(bits) == 2
    kb = "We don't install heat pumps."
    assert agent._unsourced_bits("We don't install heat pumps, sorry.", kb) == []


def test_closed_is_caught_when_out_of_hours_were_held_back(monkeypatch):
    seen = []
    turns = [
        SimpleNamespace(content=[SimpleNamespace(type="tool_use", name="check_availability", input={}, id="t1")],
                        usage=None, stop_reason="tool_use", model="m"),
        SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(
            {"reply": "We're closed on Sundays, so Monday afternoon is the soonest.",
             "deferred": False, "missing": "", "thread_state": "open"}))],
            usage=None, stop_reason="end_turn", model="m"),
        SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(
            {"reply": "The soonest we can come is Monday afternoon.", "deferred": False,
             "missing": "", "thread_state": "open"}))], usage=None, stop_reason="end_turn", model="m"),
    ]

    async def fake_create(**kw):
        seen.append(kw)
        return turns.pop(0)
    monkeypatch.setattr(agent.llm, "create", fake_create)
    tools = [{"name": "check_availability", "description": "", "input_schema": {"type": "object"}}]
    out = asyncio.run(agent.draft_reply(
        {"business_name": "Hartwell"}, {"subject": "Leak", "body": "Tomorrow?"}, tools=tools,
        tool_executor=lambda n, a: {"available": [{"date": "2026-09-28", "window": "pm"}],
                                    "out_of_hours_held_back": "…"}, scheduling=True))
    assert out.text == "The soonest we can come is Monday afternoon."


def test_photos_reach_the_model_as_images(monkeypatch):
    from app import photos
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (3000, 2000), (10, 20, 30)).save(buf, format="PNG")
    im = photos.for_model(buf.getvalue(), "display.png")
    assert im["media_type"] == "image/jpeg" and im["data"]
    assert photos.is_photo("IMG_0012.HEIC", None) and not photos.is_photo("quote.pdf", "application/pdf")
    seen = []

    async def fake_create(**kw):
        seen.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(
            {"reply": "That's an F28 — no ignition.", "deferred": False, "missing": ""}))],
            usage=None, stop_reason="end_turn", model="m")
    monkeypatch.setattr(agent.llm, "create", fake_create)
    asyncio.run(agent.draft_reply({"business_name": "H"}, {"subject": "Code", "body": "See photo"},
                                  images=[im]))
    last = seen[0]["messages"][-1]["content"]
    assert last[0]["type"] == "image" and "attached 1 photo(s)" in last[-1]["text"]


def test_the_rerun_findings():
    """The second live run (5fe8220): "aren't services we offer" still got
    through, and "your rule applied" fired for knowledge notes and Always steps."""
    assert agent._unsourced_bits("Heat pump installations and MCS certification aren't services "
                                 "we offer.", "")
    nd = "Never offer a discount, price match or a free callout."
    assert pipeline._is_never_rule("no price match (never rule)", nd)
    assert not pipeline._is_never_rule("Warranty rule applied — asked boiler age", nd)
    assert not pipeline._is_never_rule("Always ask for a photo of the boiler's display", nd)
