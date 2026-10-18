"""The intake receipt (src/app/concierge_intake.py, docs/review-agentic-concierge-2026-10-16.md R6):
when one message asks for several things, what became of EACH part is kept with the reply,
so "what happened to my fourth thing?" can be answered from the records later.

Run: python tests/run_offline.py tests/test_concierge_intake.py -q
Nothing here touches a database or a model: every read and write is a stub.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app import concierge, concierge_data, concierge_intake, concierge_routes, concierge_tasks, db


def _ctx():
    return SimpleNamespace(tenant_id="t1", user_id="u1", client=None, viewing_as=None)


# ── what the model declares is cleaned before it is kept ─────────────────────
def test_declared_parts_keep_only_a_known_how_and_short_text():
    raw = [{"said": "any email I haven't answered", "how": "answered", "because": ""},
           {"said": "ring Sam", "how": "Cannot", "because": "x " * 200},
           {"said": "tell the fleet people", "how": "asking", "because": "Which ones?"},
           {"said": "nothing", "how": "done"}, {"said": "", "how": "cannot"}, "junk", None] + [
        {"said": f"p{i}", "how": "cannot", "because": "b"} for i in range(20)]
    out = concierge_intake.clean_others(raw)
    assert [o["how"] for o in out[:3]] == ["answered", "cannot", "asking"]
    assert len(out) == 4                                                           # unknown hows, empties and junk dropped; only the first eight read
    assert len(concierge_intake.clean_others([{"said": f"p{i}", "how": "cannot"} for i in range(20)])) == concierge_intake.MAX_OTHERS
    assert len(out[1]["because"]) <= concierge_intake.BECAUSE_CHARS
    assert concierge_intake.clean_others(None) == [] and concierge_intake.clean_others("x") == []
    assert concierge_intake.declared({"others": raw[:1], "now": "n " * 400})["now"].count(" ") < 200


def test_the_receipt_credits_only_read_tools_that_ran_and_needs_a_started_list():
    batch = {"batch_id": "b1", "tasks": [{"id": "k1", "title": "Block Sunday"}, {"id": "k2", "title": "Chase Meg"}, {"title": "no id"}]}
    intake = {"others": [{"said": "email?", "how": "answered", "because": ""}], "now": "No unanswered emails."}
    rec = concierge_intake.receipt(batch, intake, question="a " * 400,
                                   tools_called=["get_inbox_overview", "start_tasks", "list_invoices", "get_inbox_overview", "control_task"])
    assert rec["name"] == "intake_receipt" and rec["batch_id"] == "b1"
    assert rec["lookups"] == ["get_inbox_overview", "list_invoices"]                  # read tools only, once each, from the route's record
    assert [t["id"] for t in rec["tasks"]] == ["k1", "k2"] and len(rec["asked"]) <= concierge_intake.ASKED_CHARS
    one = {"batch_id": "b1", "tasks": [{"id": "k1", "title": "Block Sunday"}]}
    assert concierge_intake.receipt(one, {"others": [], "now": ""}, question="q", tools_called=[]) is None   # one thing asked
    assert concierge_intake.receipt(one, intake, question="q", tools_called=[]) is not None                  # a task and a part that is not
    assert concierge_intake.receipt(None, intake, question="q", tools_called=[]) is None
    assert concierge_intake.receipt(batch, None, question="q", tools_called=[]) is None
    assert concierge_intake.from_trail([{"name": "x"}, rec]) is rec and concierge_intake.from_trail(None) is None


# ── the model is told about it ───────────────────────────────────────────────
def test_start_tasks_and_the_prompt_ask_for_every_other_part():
    props = concierge_tasks.TOOL["input_schema"]["properties"]
    assert set(props) == {"tasks", "others", "now"}
    assert props["others"]["items"]["properties"]["how"]["enum"] == ["answered", "cannot", "asking"]
    assert props["others"]["items"]["required"] == ["said", "how"]
    assert "listed in `others`" in concierge_tasks.TOOL["description"]
    prompt = " ".join(concierge.build_business_prompt(tasks_on=True).split())
    assert "list each part that is not a task in `others`" in prompt
    assert "listed in `others`" in concierge_tasks.foreground_tools()[0]["description"]


# ── the starter keeps what was declared ──────────────────────────────────────
def test_the_starter_keeps_the_declared_parts_beside_the_started_tasks(monkeypatch):
    monkeypatch.setattr(db, "create_concierge_tasks", lambda tid, uid, batch, tasks, surface, client=None:
                        [{"id": f"k{i}", "title": t["title"]} for i, t in enumerate(tasks)])
    monkeypatch.setattr(concierge_tasks, "launch", lambda *a: None)
    s = concierge_tasks.Starter(_ctx(), surface="typed", question="four things")
    out = asyncio.run(s.start({
        "tasks": [{"title": "Chase invoices", "instruction": "Chase unpaid invoices"}, {"title": "Ring Sam", "instruction": "Ring Sam Monday"}],
        "others": [{"said": "any email unanswered", "how": "answered", "because": ""},
                   {"said": "tell the fleet", "how": "asking", "because": "Which fleet people?"}],
        "now": "No unanswered emails stand out."}))
    assert out["ok"] and s.intake["now"] == "No unanswered emails stand out."
    assert [o["how"] for o in s.intake["others"]] == ["answered", "asking"]
    rec = concierge_intake.receipt(s.batch, s.intake, question="four things", tools_called=["get_inbox_overview"])
    assert [t["title"] for t in rec["tasks"]] == ["Chase invoices", "Ring Sam"] and rec["lookups"] == ["get_inbox_overview"]


def test_nothing_is_kept_when_no_list_started(monkeypatch):
    monkeypatch.setattr(db, "create_concierge_tasks", lambda *a, **k: pytest.fail("no rows"))
    s = concierge_tasks.Starter(_ctx(), surface="typed")
    out = asyncio.run(s.start({"tasks": [{"title": "One", "instruction": "one thing"}],
                               "others": [{"said": "x", "how": "answered"}]}))
    assert not out["ok"] and s.intake is None
    assert concierge_intake.receipt(s.batch, s.intake, question="q", tools_called=[]) is None


# ── the receipt is stored hidden with the reply, and history never shows it ──
def _finish(monkeypatch, *, tasks, intake, tools):
    saved = []
    monkeypatch.setattr(db, "add_concierge_message", lambda tenant, role, content, **k: saved.append((role, content, k)))
    monkeypatch.setattr(concierge_routes.usage, "record_anthropic", lambda *a, **k: None)
    monkeypatch.setattr(concierge_routes.trace, "emit", lambda *a, **k: None)
    result = SimpleNamespace(model="m", usage={}, reply="On it — two things. No unanswered emails stand out.", links=[], action=None)
    trail = [{"name": "get_inbox_overview", "label": "Checked your inbox", "detail": ""}]
    done = concierge_routes.finish_business_turn(
        _ctx(), result, question="chase, ring Sam, any email?", trail=trail, tools_called=tools, t0=0.0,
        full_qa=True, scheduling_on=False, tasks=tasks, intake=intake)
    return done, saved


def test_the_reply_is_stored_with_the_receipt_but_the_page_gets_only_the_lookups(monkeypatch):
    batch = {"batch_id": "b1", "tasks": [{"id": "k1", "title": "Chase invoices"}]}
    intake = {"others": [{"said": "any email unanswered", "how": "answered", "because": ""}], "now": "No unanswered emails."}
    done, saved = _finish(monkeypatch, tasks=batch, intake=intake, tools=["get_inbox_overview", "start_tasks"])
    assistant = [s for s in saved if s[0] == "assistant"][0]
    trail = assistant[2]["trail"]
    assert [e["name"] for e in trail] == ["get_inbox_overview", "intake_receipt"]
    assert trail[1]["batch_id"] == "b1" and trail[1]["lookups"] == ["get_inbox_overview"] and "[Tasks: b1]" in assistant[1]
    assert [e["name"] for e in done["trail"]] == ["get_inbox_overview"]                 # the page's trail is exactly as before


def test_a_turn_with_no_task_list_stores_no_receipt(monkeypatch):
    done, saved = _finish(monkeypatch, tasks=None, intake=None, tools=["get_inbox_overview"])
    assert [e["name"] for e in [s for s in saved if s[0] == "assistant"][0][2]["trail"]] == ["get_inbox_overview"]


def test_history_never_hands_the_receipt_to_the_page_or_the_model(monkeypatch):
    rows = [{"id": "a1", "role": "assistant", "content": "On it.", "links": [], "created_at": "2026-10-16T09:00:00+00:00", "user_id": "u1",
             "trail": [{"name": "get_inbox_overview", "label": "Checked your inbox"},
                       {"name": "intake_receipt", "asked": "secret words", "tasks": [], "others": []},
                       {"name": "message_origin", "origin": "typed"}]}]

    class Q:
        def __getattr__(self, name):
            return self if name == "not_" else (lambda *a, **k: self)

        def execute(self):
            return SimpleNamespace(data=[dict(r) for r in rows])
    out = db.list_concierge_messages("t1", user_id="u1", include_native=False, client=SimpleNamespace(table=lambda n: Q()))
    assert [e["name"] for e in out[0]["trail"]] == ["get_inbox_overview"] and out[0]["origin"] == "typed"
    assert "secret words" not in json.dumps(out)


def test_the_ledger_read_is_the_askers_and_refuses_an_empty_user_id():
    class Boom:
        def table(self, name):
            raise AssertionError("no query may be made without a user id")
    assert db.list_ledger_intakes("t1", "", days=7, client=Boom()) == []
    calls = []

    class Q:
        def __getattr__(self, name):
            def call(*a, **k):
                calls.append((name, a))
                return self
            return call

        def execute(self):
            return SimpleNamespace(data=[])
    db.list_ledger_intakes("t1", "u1", days=7, client=SimpleNamespace(table=lambda n: Q()))
    assert ("eq", ("user_id", "u1")) in calls and ("eq", ("role", "assistant")) in calls
    assert ("contains", ("trail", '[{"name":"intake_receipt"}]')) in calls


def test_both_chat_routes_hand_the_declared_parts_to_the_turn_that_stores_them():
    from pathlib import Path
    src = Path("src/app/concierge_routes.py").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert src.count("intake=starter.intake") == 2                                     # typed and live voice, one shared finish
