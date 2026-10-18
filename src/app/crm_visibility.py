"""What a staff member sees in the CRM (docs/plan-people.md slice 3).

Two narrowings, and they answer two different questions.

**Whose customers.** "The Diary" is every booking and "the CRM" is every
contact — a door is a coarse thing. A person who does the drop-offs needs the
people whose subjects they receive, not the whole book. So a staff member with
kinds delegated sees the contacts behind bookings OF THOSE KINDS, and search
finds only those. Somebody with nothing delegated is not narrowed, for the same
reason the diary isn't: delegation narrows, it never blanks a page by omission.

**Which fields.** Kris, when the roles were settled: default *Staff can see*
off "for anything onboarding marked as access information (codes, keys), on for
name, phone, the dependants' names and notes." There is no such marker on a
field definition — the tenant types a label and a hint in their own words — so
the DEFAULT is read from those words and the owner can flip any of them.

The word list is deliberately tight. It matches "alarm code" and "key safe",
not the bare words "key" or "code", because a field called "Key facts" that
silently hides itself is a worse failure than one the owner has to tick shut:
the first is invisible, the second is on a page in front of them.

Nothing here is a permission boundary on its own. The door itself is
app/people.can_open; this decides what is behind it once it is open.
"""
from __future__ import annotations

import re

# What reads as access information. Whole phrases and unambiguous single words
# only — see the module docstring for why this list is not longer.
SENSITIVE_PATTERNS = (
    r"alarm",
    r"pass[\s-]?code", r"pass[\s-]?word",
    r"\bpin\b", r"\bpin[\s-]?numbers?\b",
    r"key[\s-]?safe", r"key[\s-]?code", r"key[\s-]?box",
    r"(?:gate|door|entry|access|lock|security|safe|meter|garage|barrier)[\s-]*codes?",
    r"code\s+for\s+the\s+(?:gate|door|alarm|safe|lock|box)",
    r"combination",
    # "Access notes" is the label onboarding itself writes (src/app/intake.py), and
    # its hint is "Parking, keys, gate codes" — the canonical example of the
    # thing Kris named when he said this should default off.
    r"\baccess\b[\s-]*(?:notes?|details?|info\w*|arrangements?|instructions?)",
    r"\bcvv\b", r"sort[\s-]?code", r"account\s+number", r"card\s+number",
)
_SENSITIVE = re.compile("|".join(SENSITIVE_PATTERNS), re.IGNORECASE)


def looks_sensitive(*words) -> bool:
    """Does any of this read as a way INTO somebody's home or money? Used only
    to pick a default the owner can change."""
    return bool(_SENSITIVE.search(" ".join(str(w or "") for w in words)))


def default_staff_can_see(defn: dict | None) -> bool:
    """What a new field definition starts as. Closed for access information,
    open for everything else — a wrong guess in the open direction leaks an
    alarm code, and a wrong guess in the closed direction is a tick box."""
    d = defn or {}
    return not looks_sensitive(d.get("label"), d.get("extraction_hint"),
                               d.get("hint"), d.get("key"))


def _narrowed(member: dict | None) -> bool:
    """Only Staff are narrowed. An owner, a manager and an account that
    predates People see their own CRM whole."""
    if not member:
        return False
    from app import people
    return people.normalise_role(member.get("role")) == "staff"


def can_see_field(member: dict | None, defn: dict | None) -> bool:
    """A field marked private is still open to a staff member who has been
    given "See private details" — the tick People already offers, whose words
    are literally "Access codes, keys, medical notes". So this is not a wall
    between staff and the truth; it is the business deciding, per person, who
    holds the keys."""
    if not _narrowed(member):
        return True
    d = defn or {}
    stored = d.get("staff_can_see")
    if (default_staff_can_see(d) if stored is None else bool(stored)):
        return True
    from app import people
    return people.can_do(member, "crm", "private")


def hides_private(member: dict | None) -> bool:
    """Does anything private stay shut for this person? The FIELD lens, and
    separate from the customer lens (narrows_contacts): a staff member with
    nothing delegated sees every customer but still not the alarm code
    (security review 2026-09, M33). Owner, manager and a pre-People account:
    no. Staff given "See private details": no."""
    if not _narrowed(member):
        return False
    from app import people
    return not people.can_do(member, "crm", "private")


def visible_defs(defs, member: dict | None) -> list[dict]:
    return [d for d in (defs or []) if can_see_field(member, d)]


def redact(custom_data: dict | None, defs, member: dict | None) -> dict:
    """The values of a record, minus the ones this person's fields don't
    include. Keys are DROPPED rather than blanked — a row reading "Alarm code:
    hidden" tells somebody there is one to ask for."""
    data = dict(custom_data or {})
    if not _narrowed(member):
        return data
    shut = {str(d.get("key")) for d in (defs or []) if not can_see_field(member, d)}
    return {k: v for k, v in data.items() if k not in shut}


# ── dependants ─────────────────────────────────────────────────────────────
# A dependant's own fields (requirement_groups.fields — a subject, an asset, a
# property) are the same kind of thing and carry the same risk: a service
# business's "Property" group is exactly where a key-safe number would live.
# They read the SAME switch when one is stored on the field, and the same
# heuristic when it is not. Storing one needs an editor on the dependants
# settings page, which is the remaining piece — until then the heuristic is
# what a group's fields get, and it only ever hides.
def visible_group_fields(group: dict | None, member: dict | None) -> list[dict]:
    return [f for f in ((group or {}).get("fields") or []) if can_see_field(member, f)]


def redact_dependant(data: dict | None, group: dict | None, member: dict | None) -> dict:
    out = dict(data or {})
    if not _narrowed(member):
        return out
    shut = {str(f.get("key")) for f in ((group or {}).get("fields") or [])
            if not can_see_field(member, f)}
    return {k: v for k, v in out.items() if k not in shut}


# ── whose customers ────────────────────────────────────────────────────────
def narrows_contacts(member: dict | None) -> bool:
    """Is this person's CRM a subset? Staff with kinds delegated, yes. Staff
    with nothing delegated, no — the same rule the diary uses, so a person is
    never shown an empty page because a box was left unticked."""
    if not _narrowed(member):
        return False
    from app import people
    return bool(people.does(member))


def theirs(member: dict | None, bookings, cfg: dict | None) -> set[str]:
    """The addresses whose work belongs to this person, from the bookings they
    do. Lowercased, because an address is the CRM's identity key."""
    from app import people
    mine = set(people.does(member))
    if not mine:
        return set()
    out = set()
    for b in bookings or []:
        addr = str((b or {}).get("contact") or "").strip().lower()
        if addr and people.kind_of(b, cfg) in mine:
            out.add(addr)
    return out


def keep_contacts(contacts, member: dict | None, allowed: set[str]):
    """The contact list as this person should read it. Untouched for anyone
    not narrowed."""
    if not narrows_contacts(member):
        return list(contacts or [])
    return [c for c in (contacts or [])
            if str((c or {}).get("email") or "").strip().lower() in allowed]


def may_open(contact: dict | None, member: dict | None, allowed: set[str]) -> bool:
    if not narrows_contacts(member):
        return True
    return str((contact or {}).get("email") or "").strip().lower() in allowed
