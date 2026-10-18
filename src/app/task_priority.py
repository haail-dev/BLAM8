"""Priority tiers for Concierge tasks, shared by every batch of one user.

Tier 0 needs the owner, 1 is a quick read, 2 is an action, 3 is a long draft.
Reads (0-1) get their own lane so they never queue behind an action or draft;
actions and drafts share one lane, served lowest tier first, then first come.
"""
import asyncio
import heapq
import itertools
import re

NEEDS_YOU, LOOKUP, ACTION, DRAFT = 0, 1, 2, 3

_DRAFT = re.compile(r"\b(draft|write|compose|prepare (?:a|an|the) (?:reply|email|letter|quote|proposal|report)|summari[sz]e|report on)\b")
_ACTION = re.compile(r"\b(block|book|move|cancel|reschedul\w*|send|create|add|set|mark|invoice|assign|delete|remove|update|change|confirm|reply)\b")
_LOOKUP = re.compile(r"\b(check|what|when|who|which|how many|how much|find|show|look(?:\s|-)?up|list|is there|are there|any|tell me)\b")


def classify(title: str = "", instruction: str = "") -> int:
    """Quick keyword read of the request; the first matching tier wins."""
    text = f"{title} {instruction}".lower()
    if _DRAFT.search(text):
        return DRAFT
    if _ACTION.search(text):
        return ACTION
    if _LOOKUP.search(text):
        return LOOKUP
    return ACTION


def lane(tier: int) -> str:
    return "fast" if tier <= LOOKUP else "slow"


class _Lane:
    def __init__(self):
        self.busy = False
        self.waiting: list = []


_LANES: dict[tuple, _Lane] = {}
_SEQ = itertools.count()


class gate:
    """`async with gate(tenant, user, tier):` runs one task at a time per lane."""

    def __init__(self, tenant_id, user_id, tier: int):
        self.key = (tenant_id, user_id, lane(tier))
        self.tier = tier

    async def __aenter__(self):
        state = _LANES.setdefault(self.key, _Lane())
        if not state.busy and not state.waiting:
            state.busy = True
            return self
        future = asyncio.get_running_loop().create_future()
        entry = (self.tier, next(_SEQ), future)
        heapq.heappush(state.waiting, entry)
        try:
            await future
        except BaseException:
            if future.done() and not future.cancelled():
                self._release(state)   # we were handed the lane just as we were cancelled
            raise
        return self

    async def __aexit__(self, *exc):
        self._release(_LANES[self.key])

    def _release(self, state: _Lane) -> None:
        while state.waiting:
            _tier, _seq, future = heapq.heappop(state.waiting)
            if not future.done():
                future.set_result(None)   # lane stays busy for the next owner
                return
        state.busy = False
        if not state.waiting:
            _LANES.pop(self.key, None)


def rank(task: dict) -> int:
    """Tier of a public task row: its stored priority, else the keyword read."""
    value = task.get("priority")
    if isinstance(value, int) and 0 <= value <= DRAFT:
        return value
    return classify(task.get("title") or "", task.get("instruction") or "")
