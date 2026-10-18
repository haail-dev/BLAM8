"""The fixed prefix of a Concierge call, the system prompt plus the tool list, has a
size budget, so it cannot grow unnoticed (docs/review-agentic-concierge-2026-10-16.md,
R3: the prefix grew about 37% in four days, 9.0k to 12.3k tokens, with nobody
deciding to).

The prefix is 85-93% of every call's tokens, so it is where the money goes. This is an
ESTIMATE, not the model's tokenizer (there is none offline): characters divided by
CHARS_PER_TOKEN, which was set so today's prompt lands where the rig measured it
(about 12.3k tokens on 15 Oct). It measures the heaviest real shapes: the typed path
(with the `answer` tool and settings guidance) and live voice (with the two drafting
tools), each with and without the delegated task tools.

If this fails: shorten the new text (a tool description or a prompt clause), do not
raise the number. Raising BUDGET_TOKENS is a cost decision for Kris.

Run: python tests/run_offline.py tests/test_concierge_prompt_budget.py -q
"""
import json
import math
from types import SimpleNamespace

from app import concierge, concierge_data, concierge_delegation, concierge_draft, concierge_tasks

BUDGET_TOKENS = 14_000
NOTE_BUDGET_TOKENS = 2_000     # the per-turn note (question_note) is not cached: it is paid for in full every call
CHARS_PER_TOKEN = 3.4


def estimate_tokens(system: str, tools: list) -> int:
    chars = len(system) + len(json.dumps(tools, ensure_ascii=False, separators=(",", ":")))
    return math.ceil(chars / CHARS_PER_TOKEN)


def shapes(monkeypatch):
    """(label, system prompt, tool list) for each heaviest real shape."""
    out = []
    for delegated in (False, True):
        monkeypatch.setattr(concierge_delegation, "enabled", lambda d=delegated: d)
        tools = concierge_data.tools_for(True, None) + concierge_tasks.foreground_tools()
        common = dict(business_name="Hartwell & Daughters", today="Friday 16 October 2026 (2026-10-16)",
                      full_qa=True, actions=True, tasks_on=True)
        out.append((f"typed, delegation {'on' if delegated else 'off'}",
                    concierge.build_business_prompt(**common, settings_guidance={"diary_setup": {}}),
                    tools + [concierge.answer_tool(True)]))
        out.append((f"live voice, delegation {'on' if delegated else 'off'}",
                    concierge.build_business_prompt(**common, voice_mode=True),
                    tools + [concierge_draft.DRAFT_TOOL, concierge_draft.SHOW_TOOL]))
    return out


def test_the_fixed_prefix_stays_inside_its_budget(monkeypatch):
    sizes = {label: estimate_tokens(system, tools) for label, system, tools in shapes(monkeypatch)}
    worst = max(sizes, key=sizes.get)
    assert sizes[worst] <= BUDGET_TOKENS, (
        f"The fixed prompt prefix is about {sizes[worst]} tokens for '{worst}' "
        f"(budget {BUDGET_TOKENS}; every shape: {sizes}). Shorten the text you added; "
        "raising the budget is Kris's call.")


def test_the_guard_notices_growth(monkeypatch):
    """A guard that cannot fail is no guard: pad the heaviest shape by what a new
    rule or two would add and it must go over."""
    label, system, tools = max(shapes(monkeypatch), key=lambda s: estimate_tokens(s[1], s[2]))
    assert estimate_tokens(system, tools) <= BUDGET_TOKENS
    assert estimate_tokens(system + "x" * 6000, tools) > BUDGET_TOKENS


def test_the_per_turn_note_is_capped_even_with_forty_long_open_tasks(monkeypatch):
    """question_note carries the delegated-task note, which used to grow with every task
    (up to 40 rows of up to 1,000 characters). It is bounded now, and the bound is budgeted."""
    from app import db
    rows = [{"id": f"t{i}", "tenant_id": "t", "user_id": "u", "status": "working", "revision": 1, "title": f"Task {i}",
             "instruction": "word " * 200, "commit_state": "preparation"} for i in range(40)]
    ctx = SimpleNamespace(tenant_id="t", user_id="u", client=None)
    monkeypatch.setattr(concierge_delegation, "enabled", lambda: True)
    monkeypatch.setattr(db, "list_session_concierge_tasks", lambda *a, **k: list(rows))
    monkeypatch.setattr(concierge_delegation, "scoped_public_tasks", lambda c, r: [{"id": x["id"]} for x in r])
    note = concierge_delegation.context_note(ctx, None, surface="typed")
    assert math.ceil(len(note) / CHARS_PER_TOKEN) <= NOTE_BUDGET_TOKENS, (
        f"The task note is about {math.ceil(len(note) / CHARS_PER_TOKEN)} tokens (budget {NOTE_BUDGET_TOKENS}); "
        "raising NOTE_MAX_CHARS is Kris's call.")
