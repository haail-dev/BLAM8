"""Who did it — the signed-in person behind the current request.

People slice 4, the last of it: "by-hand actions already log to the activity
feed; they carry the person's name." With more than one person on a business,
"someone cancelled BK-1042" is not a record, and record keeping is a third of
what this product said it was for.

The problem with stamping a name is that there are 149 places that write to the
Agent Log, and only some of them are a person. Threading an `actor` argument
through all of them would be 149 chances to forget one, and forgetting one
means an action silently attributed to nobody — or worse, to the wrong person
if a default were ever guessed.

So the actor is ambient, and it is set exactly once, in the auth dependency
that every /app request already passes through. A request with a signed-in
human carries one; a nightly job, a webhook and the drafting pipeline do not,
because nothing set it. That is not a convention anyone has to remember — it
is what "by hand" MEANS.

Two safeguards, because ambient state is easy to get wrong:

  * Every stamp is checked against the tenant it is being written for. A
    background task that inherited a request's context can never attribute one
    business's action to another business's person.
  * `forget()` clears it, for work that is deliberately detached from the
    request that started it (the backlog import behind a mailbox connect is
    BLAM8 filing old mail, not the owner doing it by hand).

Nothing here is an authorisation. The actor is a label on a record; every
permission decision is made by src/app/people.py against the membership row.
"""
from __future__ import annotations

import functools
import logging
from contextvars import ContextVar

log = logging.getLogger(__name__)

# Set per request by app.auth.require_tenant. Each ASGI request runs in its own
# task with its own context, so there is no bleed between concurrent requests.
_ACTOR: ContextVar[dict | None] = ContextVar("blam8_actor", default=None)

# How long a person's display name is held. Names change by hand, rarely.
_NAME_TTL = 300.0


def remember(tenant_id: str, user_id: str | None, email: str | None = None) -> None:
    """Note who is driving this request. Called once, from the auth dependency."""
    if not tenant_id or not user_id:
        return
    _ACTOR.set({"tenant_id": str(tenant_id), "user_id": str(user_id),
                "email": (email or "").strip().lower()})


def forget() -> None:
    """Detach from whoever started this. For work that is BLAM8's own, kicked
    off behind a request — it must not wear their name."""
    _ACTOR.set(None)


def current() -> dict | None:
    return _ACTOR.get()


def agent_work(fn):
    """Mark an async function as BLAM8's own work, even when a person started
    it. Pressing "draft a reply" is a human act; the reply is still the agent's,
    and a feed that says "Sam drafted a reply to Jo" would be a lie about the
    one thing this product has to be honest about.

    The actor is cleared for the duration and RESTORED after, so an approve
    route that redrafts and then sends still records the send as theirs."""
    @functools.wraps(fn)
    async def run(*args, **kwargs):
        token = _ACTOR.set(None)
        try:
            return await fn(*args, **kwargs)
        finally:
            _ACTOR.reset(token)
    return run


def name_for(tenant_id: str, user_id: str, email: str = "") -> str:
    """What to call them on the record: their name on this business, else the
    address they signed in with. Never a user id — an id on a page is noise
    nobody can act on."""
    from app import db as _db, navcache
    try:
        row = navcache.get_or_fill(
            "an:" + tenant_id + ":" + user_id, _NAME_TTL,
            lambda: (_db.get_member(tenant_id, user_id) or {}),
            cache_if=lambda r: r is not None)   # {} caches too: a pre-People
        #                                         account must not re-read forever
    except Exception:
        log.warning("actor name read failed (tenant=%s)", tenant_id, exc_info=True)
        row = {}
    name = " ".join(str((row or {}).get("display_name") or "").split())
    return name or (row or {}).get("email") or email or ""


def stamp(tenant_id: str) -> dict:
    """The columns an agent_events row should carry for this request:
    {'actor_user_id', 'actor_name'} when a person is driving it, {} when BLAM8
    is. Empty is the honest answer for the majority of the feed, and far better
    than a guess — a nightly chase is not something anybody did.

    The tenant check is the important line: a task that inherited a request's
    context must never put one business's person on another's record."""
    a = _ACTOR.get()
    if not a or not tenant_id or a.get("tenant_id") != str(tenant_id):
        return {}
    return {"actor_user_id": a["user_id"],
            "actor_name": name_for(a["tenant_id"], a["user_id"], a.get("email") or "") or None}


def did_it(row: dict | None) -> str:
    """How one feed row names its author: the person, or BLAM8 itself. Rows
    written before this existed have no actor and stay silent rather than
    claiming either — nobody knows who did those."""
    return " ".join(str((row or {}).get("actor_name") or "").split())
