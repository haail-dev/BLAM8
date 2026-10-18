"""Telling a person something, attached to the work it is about.

Kris, 2026-09-13, settling what People is for: "work delegation and record
keeping & quick sharing info". Delegation shipped in slices 1–2. This is the
sharing half, and it is the one thing none of the competitors in
`docs/research-people-tools-2026-09-13.md` do at all.

The research gave this module two hard rules and one shape.

**The shape.** A note is always ABOUT something — a booking, a customer, a
thread. That is the whole difference from a WhatsApp group, where the message
and the work live in different places and only the owner's memory joins them.
A note with nothing attached has no business existing here: the reason to read
it in BLAM8 is that the job is in BLAM8 too.

**Rule one: no app to install.** Staff organise hard against apps on personal
phones (416 upvotes for "Dont install it on your personal phone") while
accepting a browser sign-in. A note lands on their Today, in the browser they
already signed into. Nothing else is ever required of them.

**Rule two: the record outlives the people.** Names are carried on the row
beside the user ids, so "Kris asked Em to…" still reads properly after somebody
leaves — People deactivates, it never deletes.

Pure: no DB, no I/O. src/app/db.py reads and writes, src/app/people_routes.py serves.
"""
from __future__ import annotations

from datetime import datetime, timezone

MAX_BODY = 2000

# What a note can be about, and how the page it belongs to is reached.
ABOUT_KINDS = ("booking", "contact", "thread")

ABOUT_LABELS = {"booking": "booking", "contact": "customer", "thread": "conversation"}


def clean_body(text) -> str:
    """One note's words. Collapsed, capped, and empty means no note."""
    return " ".join(str(text or "").split())[:MAX_BODY]


def about_href(kind: str, about_id: str) -> str:
    """Where to open the thing this note is about. Empty when we cannot say —
    a note still stands without a door, it just cannot be followed."""
    kind = (kind or "").strip().lower()
    ident = str(about_id or "").strip()
    if not ident:
        return ""
    if kind == "booking":
        return f"/app/diary/{ident}"
    if kind == "contact":
        from urllib.parse import quote
        return f"/app/crm/open?email={quote(ident)}"
    if kind == "thread":
        from urllib.parse import quote
        return f"/app/conversations/view?contact={quote(ident)}"
    return ""


def build(*, body, from_user, from_name, to_user, to_name,
          about_kind="", about_id="", about_label="") -> dict | None:
    """A note ready to store, or None when there is nothing to say. The
    `about_*` fields are denormalised on purpose: a booking can be cancelled
    and a customer renamed, and the record of what was asked should still make
    sense a year later."""
    text = clean_body(body)
    if not text:
        return None
    kind = (about_kind or "").strip().lower()
    if kind not in ABOUT_KINDS:
        kind = ""
    return {
        "body": text,
        "from_user": from_user or None,
        "from_name": " ".join(str(from_name or "").split())[:80] or None,
        "to_user": to_user or None,
        "to_name": " ".join(str(to_name or "").split())[:80] or None,
        "about_kind": kind or None,
        "about_id": (str(about_id).strip()[:200] or None) if kind else None,
        "about_label": (" ".join(str(about_label or "").split())[:120] or None) if kind else None,
        "about_href": (about_href(kind, about_id) or None) if kind else None,
    }


def state(row) -> str:
    """'done' | 'seen' | 'new' | 'cancelled' — what the reader is looking at."""
    row = row if isinstance(row, dict) else {}
    if row.get("cancelled_at"):
        return "cancelled"
    if row.get("done_at"):
        return "done"
    if row.get("seen_at"):
        return "seen"
    return "new"


def is_open(row) -> bool:
    return state(row) in ("new", "seen")


def said_by(row) -> str:
    return (row or {}).get("from_name") or "Someone"


def said_to(row) -> str:
    return (row or {}).get("to_name") or "someone"


def line(row) -> str:
    """The note as one sentence for a feed or a card title."""
    row = row if isinstance(row, dict) else {}
    about = row.get("about_label")
    return f"{said_by(row)} asked you about {about}" if about else f"{said_by(row)} asked you"


def unseen(rows) -> list[dict]:
    return [r for r in rows or [] if state(r) == "new"]


def for_deck(rows, now: datetime | None = None) -> list[dict]:
    """Open notes as Today cards — the newest first, because a thing somebody
    just asked you is the thing most likely to matter."""
    out = [r for r in rows or [] if is_open(r)]
    out.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return out


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
