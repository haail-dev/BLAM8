"""Who's on the job (docs/plan-people-team-and-jobs.md step 2).

Kris, 2026-09-24: "more than one per job (should be configurable —
agnostic)", and the first test, said to the Concierge: "delegate the next
introductory visit to Em". Until now a job was nobody's in particular: kinds said
who SEES a kind of work, never who is DOING this one.

A person on a booking is a team_people row — with or without a login, so a
partner who never signs in can be on the drop-off. `part` is the business's
own word for their share ("drop-off", "collection", "on site"), optional.

The setting (scheduling_config.people_per_job):
  one      — putting someone on a job replaces whoever was on it (default)
  several  — people are added; the stays preset (Kristie on the drop-off,
             Em on the collection)

Being on a job always makes it theirs: their Today, their Diary, their own
calendar, and the Concierge's "my jobs" (app/people.owns_booking reads the
`on_it_users` this module puts on each booking row).
"""
from __future__ import annotations

import logging

from app import db, people

log = logging.getLogger(__name__)

ONE, SEVERAL = "one", "several"


def mode(cfg: dict | None) -> str:
    return SEVERAL if str((cfg or {}).get("people_per_job") or "") == SEVERAL else ONE


def team(tenant_id: str) -> list[dict]:
    """Everyone who can be put on a job — active people, login or not."""
    try:
        return [t for t in (people.team_person(r) for r in db.list_team_people(tenant_id))
                if t["active"]]
    except Exception:
        log.warning("team read failed (tenant=%s)", tenant_id, exc_info=True)
        return []


def by_booking(tenant_id: str, booking_ids: list[str],
               roster: list[dict] | None = None) -> dict[str, list[dict]]:
    """{booking_id: [{person_id, name, user_id, has_login, part}]}, oldest first."""
    ids = [str(b) for b in booking_ids if b]
    if not ids:
        return {}
    try:
        rows = db.list_booking_people(tenant_id, ids)
    except Exception:
        log.warning("booking people read failed (tenant=%s)", tenant_id, exc_info=True)
        return {}
    if not rows:
        return {}
    who = {t["id"]: t for t in (roster if roster is not None else team(tenant_id))}
    out: dict[str, list[dict]] = {}
    for r in sorted(rows, key=lambda r: str(r.get("created_at") or "")):
        t = who.get(str(r.get("person_id")))
        if not t:
            continue          # someone who has left: their row stays, the list doesn't show them
        out.setdefault(str(r["booking_id"]), []).append({
            "person_id": t["id"], "name": t["name"], "user_id": t["user_id"],
            "has_login": t["has_login"], "part": (r.get("part") or "").strip()})
    return out


def annotate(tenant_id: str, rows: list[dict]) -> list[dict]:
    """Put who's on each booking onto its row: `on_it` (names) and
    `on_it_users` (the logins among them) — what owns_booking reads."""
    if not rows:
        return rows
    on = by_booking(tenant_id, [r.get("id") for r in rows])
    for r in rows:
        people_on = on.get(str(r.get("id")), [])
        r["on_it"] = [p["name"] + (f" ({p['part']})" if p["part"] else "") for p in people_on]
        r["on_it_users"] = [p["user_id"] for p in people_on if p["user_id"]]
    return rows


# A short name said aloud comes back as the letter it sounds like (Kris
# 2026-09-24: "Em" was transcribed "M"). A lone letter is read as its sound.
_LETTER_SOUNDS = {"m": "em", "l": "el", "n": "en", "s": "es", "f": "ef", "r": "ar",
                  "k": "kay", "j": "jay", "d": "dee", "t": "tee", "b": "bee", "v": "vee"}


def find_person(roster: list[dict], name: str) -> dict | None:
    """A team person by what someone called them: the full name, else the
    first name, else a unique start ("Kris" → Kristie only if nobody else),
    and a lone letter by its sound ("M" → Em)."""
    want = " ".join(str(name or "").lower().replace(".", " ").split())
    if not want:
        return None
    want = _LETTER_SOUNDS.get(want, want)
    for t in roster:
        if t["name"].lower() == want:
            return t
    firsts = [t for t in roster if t["name"].lower().split(" ")[0] == want]
    if len(firsts) == 1:
        return firsts[0]
    starts = [t for t in roster if t["name"].lower().startswith(want)]
    return starts[0] if len(starts) == 1 else None


def put_on(tenant_id: str, booking_id: str, person_id: str, *, cfg: dict | None,
           part: str = "", by: str | None = None) -> list[str]:
    """Put one person on a job. With 'one person per job' whoever was on it
    comes off; returns the names of those replaced."""
    replaced: list[str] = []
    if mode(cfg) == ONE:
        current = by_booking(tenant_id, [booking_id]).get(str(booking_id), [])
        others = [p for p in current if p["person_id"] != person_id]
        if others:
            db.remove_booking_people(tenant_id, booking_id, [p["person_id"] for p in others])
            replaced = [p["name"] for p in others]
    db.add_booking_person({"tenant_id": tenant_id, "booking_id": booking_id,
                           "person_id": person_id, "part": (part or "").strip()[:60] or None,
                           "added_by": by})
    return replaced


def set_people(tenant_id: str, booking_id: str, person_ids: list[str], parts: dict[str, str],
               *, cfg: dict | None, by: str | None = None) -> None:
    """The job page's "Who's on it" save: exactly these people (the first
    only, with 'one person per job'), each with their part."""
    known = {t["id"] for t in team(tenant_id)}
    want = [p for p in dict.fromkeys(person_ids) if p in known]
    if mode(cfg) == ONE:
        want = want[:1]
    current = [p["person_id"] for p in by_booking(tenant_id, [booking_id]).get(str(booking_id), [])]
    gone = [p for p in current if p not in want]
    if gone:
        db.remove_booking_people(tenant_id, booking_id, gone)
    for pid in want:
        db.add_booking_person({"tenant_id": tenant_id, "booking_id": booking_id, "person_id": pid,
                               "part": (parts.get(pid) or "").strip()[:60] or None,
                               "added_by": by})


def job_context(tenant_id: str, b: dict, cfg: dict | None) -> dict:
    """The job page's "Who's on it"."""
    roster = team(tenant_id)
    on = by_booking(tenant_id, [b.get("id")], roster).get(str(b.get("id")), [])
    on_ids = {p["person_id"] for p in on}
    return {"on_it": on, "people_several": mode(cfg) == SEVERAL,
            "team_choices": [{**t, "on": t["id"] in on_ids,
                              "part": next((p["part"] for p in on if p["person_id"] == t["id"]), "")}
                             for t in roster]}
