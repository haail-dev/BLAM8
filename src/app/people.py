"""People — partners and staff on one BLAM8 (docs/plan-people.md).

Kris, 2026-09-13: "we need some way to add employees to BLAM8 and allow the
business to decide what they're able to see/access … seats/staff should be
free … this should be a universal core, part of the horizontal build, as most
SMBs have partners/employees of types."

The shape agreed that day: **roles open doors, delegation fills them.** This
module is the doors half — the roles, what each one may open, and the invite
that binds a new sign-in to the business. Delegation by kind (a person *does*
the desk, meet & greets, a mailbox) is slice 2 and rides `scopes["does"]`,
which is carried here and ignored for now.

Pure: config in, decisions out. No DB, no I/O — src/app/db.py does the reading and
writing, src/app/dashboard.py the guarding.

Roles, in the business's words rather than an access-control vocabulary:

  owner       the account. Everything, including billing and closing it.
  manager     everything except billing and deleting the account.
  staff       only the doors they are given — a desk worker sees the diary.
  accountant  one door, Money, and nothing they can change.

`member` is the legacy role every pre-2026-09-13 row carries: it was the only
non-owner value and nothing gated on it, so those people already saw
everything. Reading it as a manager keeps that true — a migration must never
quietly take access away from someone who had it yesterday.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

# The doors, in strip order. A page not listed here is not gated by People.
PAGES = ("today", "inbox", "diary", "crm", "money", "settings")

PAGE_LABELS = {
    "today": "Today", "inbox": "Inbox", "diary": "Diary",
    "crm": "CRM", "money": "Money", "settings": "Settings",
}

ROLES = ("owner", "manager", "staff", "accountant")

ROLE_LABELS = {
    "owner": "Owner", "manager": "Manager", "staff": "Staff", "accountant": "Accountant",
}

# How a role reads mid-sentence — "as staff", not "as a Staff".
ROLE_AS = {"owner": "the owner", "manager": "a manager",
           "staff": "staff", "accountant": "their accountant"}

ROLE_BLURBS = {
    "owner": "Everything, including billing and closing the account.",
    "manager": "Everything except billing and closing the account.",
    "staff": "Only the doors you give them.",
    "accountant": "Money only, and nothing they can change.",
}

# The same, said to the person being invited (the join page): ROLE_BLURBS speak to
# the owner ("the doors you give them"), which read wrongly to the invitee.
INVITEE_BLURBS = {
    "owner": "Everything, including billing and closing the account.",
    "manager": "Everything except billing and closing the account.",
    "staff": "You'll see only the parts they've opened for you.",
    "accountant": "Money only, and nothing you can change.",
}

# What a role opens before anyone touches its switches. Staff start with the
# two doors a person on the floor needs; everything else is given deliberately.
ROLE_PAGES: dict[str, tuple[str, ...]] = {
    "owner": PAGES,
    "manager": PAGES,
    "staff": ("today", "diary"),
    "accountant": ("money",),
}

# What a door lets someone DO, once it is open (Kris 2026-09-13: "each of
# these tick boxes should have more options to each of them once ticked. More
# control basically"). A door on its own is READING; each ability here is a
# way of changing something, and every one is off until the business turns it
# on. Owner and Manager have all of them by definition; an Accountant has
# none, which is what read-only means.
ABILITIES: dict[str, tuple[tuple[str, str, str], ...]] = {
    "today": (),
    "inbox": (("approve", "Approve and send drafts", "Otherwise they can read the inbox and the drafts, and leave them for you."),
              ("write", "Write and send emails", "A new message to a customer, in their own words.")),
    "diary": (("add", "Add and move bookings", "Take one in, put one somewhere else."),
              ("cancel", "Cancel bookings", "A cancellation reaches the customer, so this is its own decision.")),
    "crm": (("edit", "Change customer details", "Correct a phone number, add a note."),
            ("private", "See private details", "Access codes, keys, medical notes — anything the business marks private.")),
    "money": (("send", "Send invoices and quotes", "Put a bill in front of a customer."),
              ("chase", "Chase what's overdue", "Send a reminder before you've seen it.")),
    "settings": (("change", "Change the business's settings", "The diary's rules, prices, the agent's knowledge."),),
}


def abilities_for(page: str) -> tuple:
    return ABILITIES.get(page, ())


def ability_key(page: str, ability: str) -> str:
    return f"{page}:{ability}"


# Roles whose doors the business chooses. Owner and Manager see everything by
# definition; an Accountant who could be given the Inbox would not be an
# accountant.
TAILORABLE = ("staff",)

# Today is not a permission (Kris 2026-09-13: "Today shouldn't be a tickbox,
# it should just be the main screen for the employee"). Everyone who works
# here lands on it, so it is never offered as a choice and never taken away.
# An Accountant is the exception and deliberately so: they are not staff, and
# their first screen is the one door they have.
ALWAYS_OPEN = ("today",)

# Roles that may never write. The page renders, every control that changes
# something does not.
READ_ONLY = ("accountant",)

# Roles that may manage other people (and see this page at all).
ADMIN = ("owner", "manager")

INVITE_DAYS = 7
_TOKEN_BYTES = 24          # 32 url-safe characters
MAX_PEOPLE = 15            # a soft cap on free seats, raised on request


def normalise_role(role) -> str:
    """A stored role as one of ROLES. The legacy 'member' reads as a manager
    (it saw everything, and an upgrade must not take that away); anything
    unrecognised is the most cautious real role, staff."""
    r = str(role or "").strip().lower()
    if r == "member":
        return "manager"
    return r if r in ROLES else "staff"


def clean_scopes(scopes, role: str | None = None) -> dict:
    """A person's switches, tidied: the pages they may open, the kinds of work
    delegated to them (slice 2), and the mailboxes they may read. A role whose
    doors are not tailorable always carries its role's own pages, whatever is
    stored — so a role change cannot leave a stale door open."""
    role = normalise_role(role) if role is not None else None
    src = scopes if isinstance(scopes, dict) else {}
    if role is not None and role not in TAILORABLE:
        pages = list(ROLE_PAGES[role])
    else:
        raw = src.get("pages")
        if not isinstance(raw, list):
            pages = list(ROLE_PAGES.get(role or "staff", ()))
        else:
            want = {str(x).strip().lower() for x in raw} | set(ALWAYS_OPEN)
            pages = [p for p in PAGES if p in want]
    does = [" ".join(str(d).split())[:60] for d in (src.get("does") or []) if str(d).strip()][:20]
    # Which mailboxes are THEIRS. Only a tailorable role is ever restricted to
    # some — an owner, a manager and an accountant are not "given" a mailbox.
    boxes = ([str(m).strip()[:64] for m in (src.get("mailboxes") or []) if str(m).strip()][:20]
             if (role is None or role in TAILORABLE) else [])
    # Kris, 2026-09-14: "the inbox thing should only happen if it's a business
    # email for that person". So the Inbox is not a door with a filter behind
    # it — it IS a mailbox, and without one there is nothing to open. Dropped
    # HERE rather than guarded at each page, so every read of a person agrees:
    # a tick with no mailbox behind it is not a half-open door, it is shut.
    #
    # The consequence worth knowing: a one-mailbox business can never give
    # staff an Inbox, because that mailbox is the owner's own.
    if role in TAILORABLE and not boxes:
        pages = [p for p in pages if p != "inbox"]
    # What they may CHANGE behind each door they can open. An ability whose
    # door is shut is dropped, so closing a door can never leave a write
    # behind it (and a role that is not tailorable carries none at all — the
    # roles that can do everything need no list, the one that can change
    # nothing must not have one).
    if role is not None and role not in TAILORABLE:
        can: list[str] = []
    else:
        want = {str(x).strip().lower() for x in (src.get("can") or []) if str(x).strip()}
        can = [ability_key(p, a) for p in pages for a, _l, _h in abilities_for(p)
               if ability_key(p, a) in want]
    return {"pages": pages, "does": does, "mailboxes": boxes, "can": can}


def member(row) -> dict:
    """One `tenant_members` row as the app reads it: role normalised, scopes
    cleaned against that role, and the display fields defaulted."""
    row = row if isinstance(row, dict) else {}
    role = normalise_role(row.get("role"))
    return {
        "user_id": str(row.get("user_id") or ""),
        "role": role,
        "role_label": ROLE_LABELS[role],
        "display_name": " ".join(str(row.get("display_name") or "").split())[:80],
        "email": str(row.get("email") or "").strip().lower(),
        "active": row.get("active") is not False,
        "last_seen_at": row.get("last_seen_at"),
        "scopes": clean_scopes(row.get("scopes"), role),
        "created_at": row.get("created_at"),
    }


def name_of(m: dict) -> str:
    """What to call this person on screen — their name, else their address,
    else the role. Never a user id."""
    return m.get("display_name") or m.get("email") or ROLE_LABELS.get(m.get("role"), "Someone")


def can_open(m: dict | None, page: str) -> bool:
    """May this person open that door? No member row at all is the account's
    own owner — every tenant predates People and its single user owns it."""
    if not m:
        return True
    if not m.get("active", True):
        return False
    role = normalise_role(m.get("role"))
    if role in ("owner", "manager"):
        return True
    if role in TAILORABLE and page in ALWAYS_OPEN:
        return True                      # where they land, never a choice
    return page in (m.get("scopes") or {}).get("pages", ())


def can_do(m: dict | None, page: str, ability: str) -> bool:
    """May this person do that? No member row is the account's own owner.
    Owner and Manager may do anything behind a door they can open; everyone
    else may do only what the business ticked — and never behind a shut door."""
    if not m:
        return True
    if not can_open(m, page):
        return False
    role = normalise_role(m.get("role"))
    if role in ("owner", "manager"):
        return True
    if role in READ_ONLY:
        return False
    return ability_key(page, ability) in ((m.get("scopes") or {}).get("can") or ())


def is_read_only(m: dict | None) -> bool:
    return bool(m) and normalise_role(m.get("role")) in READ_ONLY


def is_admin(m: dict | None) -> bool:
    """May they manage people? No row = the account's own owner."""
    return not m or normalise_role(m.get("role")) in ADMIN


def pages_of(m: dict | None) -> tuple[str, ...]:
    """Every door this person may open, in strip order."""
    if not m:
        return PAGES
    return tuple(p for p in PAGES if can_open(m, p))


def home_page(m: dict | None) -> str:
    """The screen this person lands on. Today for anyone who works here; for
    an Accountant, the one door they have."""
    opens = pages_of(m)
    return "today" if "today" in opens else (opens[0] if opens else "today")


def default_scopes(role: str) -> dict:
    return clean_scopes({"pages": list(ROLE_PAGES[normalise_role(role)])}, role)


# ── delegation by kind (slice 2) ────────────────────────────────────────────
# Kris, 2026-09-13 (paraphrased): "the stays business could have someone that works there, only
# capable of accepting and releasing subjects, the higher ups deal with introductory
# visits." A person *does* kinds of work, and the kinds are the ones the
# business already configured — the front desk, and each service it offers.
# Nothing new to set up, and the same mechanism for every business: a services business's
# kinds are its job types, a classes business's its classes.
DESK = "desk"
SERVICE_PREFIX = "svc:"


def service_kind(name: str) -> str:
    return SERVICE_PREFIX + " ".join(str(name or "").split())


def kinds_for(cfg: dict | None) -> list[dict]:
    """What this business has to delegate. Empty for a diary with neither a
    desk nor services — then the chips say there is nothing yet."""
    from app import intake as _intake, services as _services
    cfg = cfg or {}
    out = []
    stays = _intake.stay_mode(cfg)
    if stays:
        out.append({"key": DESK, "label": "Drop-offs and collections",
                    "blurb": "Taking them in and handing them back."})
    for svc in _services.clean_service_types(cfg.get("service_types")):
        out.append({"key": service_kind(svc["name"]),
                    "label": _plural_kind(svc["name"]),
                    "blurb": f"{int(svc.get('minutes') or 0)} minutes each." if svc.get("minutes") else ""})
    return out


def _plural_kind(name: str) -> str:
    """The service's name as the owner wrote it (run log F11: an "s" glued on
    read malformed plurals and an extra "s" after a parenthesised service code)."""
    return " ".join(str(name or "").split())


def does(m: dict | None) -> list[str]:
    return list((m or {}).get("scopes", {}).get("does") or [])


# ── whose mailbox (Kris 2026-09-14) ────────────────────────────────────────
def mailboxes_of(m: dict | None) -> list[str]:
    """The mailboxes that are this person's own."""
    return list((m or {}).get("scopes", {}).get("mailboxes") or [])


def inbox_is_theirs_only(m: dict | None) -> bool:
    """Is this person's Inbox their own mailbox rather than the business's?

    True only for a role that is given mailboxes AND has one. Everybody else —
    owner, manager, and an account that predates People — reads the whole
    inbox, which is what an owner's inbox IS."""
    if not m or normalise_role(m.get("role")) not in TAILORABLE:
        return False
    return bool(mailboxes_of(m))


def may_read_mailbox(m: dict | None, mailbox_id, default_id=None) -> bool:
    """May this person read mail that arrived in that mailbox? A row with no
    mailbox on it belongs to the DEFAULT one — the oldest, which is the
    owner's — so legacy mail is never anybody else's by accident."""
    if not inbox_is_theirs_only(m):
        return True
    box = str(mailbox_id or default_id or "")
    return bool(box) and box in set(mailboxes_of(m))


def assignable_mailboxes(boxes) -> list[dict]:
    """Which of a business's mailboxes may be handed to a person: any but the
    first. list_mailboxes returns them oldest first, and the oldest is the
    tenant's default — the owner's own inbox, which is never given away
    (Kris 2026-09-14, choosing "any but the owner's")."""
    return list(boxes or [])[1:]


def kind_of(booking, cfg: dict | None) -> str:
    """Which delegatable kind a booking IS — a named service, or the desk (a
    stay: taking them in and handing them back). The one place that decision is
    made, so a page and a calendar can never disagree about whose work it is."""
    from app import services as _services
    get = booking.get if isinstance(booking, dict) else lambda k, d=None: getattr(booking, k, d)
    rt = str(get("resource_type") or "").strip()
    if rt and _services.is_appointment_row(booking, (cfg or {}).get("service_types")):
        return service_kind(rt)
    return DESK


def on_booking(m: dict | None, booking) -> bool:
    """Is this person ON this job (src/app/job_people.py)? Reads the logins
    job_people.annotate put on the row — a row nobody annotated reads False."""
    uid = (m or {}).get("user_id")
    return bool(uid) and uid in ((booking or {}).get("on_it_users") or ())


def owns_booking(m: dict | None, booking, cfg: dict | None) -> bool:
    """Is this booking this person's work? Everyone who is not Staff sees the
    whole diary; a Staff member with nothing delegated sees the whole diary
    too (delegation narrows, it never blanks a page by omission). A job they
    are ON is always theirs, whatever its kind."""
    if not m or normalise_role(m.get("role")) != "staff":
        return True
    if on_booking(m, booking):
        return True
    mine = set(does(m))
    if not mine:
        return True
    return kind_of(booking, cfg) in mine


def does_booking(m: dict | None, booking, cfg: dict | None) -> bool:
    """Is this booking work delegated to THIS person, by kind?

    Deliberately stricter than owns_booking, and it applies to every role.
    Showing somebody a page they were never narrowed out of is one thing;
    writing into the calendar on their phone is another, so an empty delegation
    means NOTHING here rather than EVERYTHING. A person with no kinds ticked has
    had nothing handed to them, which is exactly the reason not to write to
    their calendar (docs/plan-people.md slice 5)."""
    if not m or not m.get("active", True):
        return False
    if on_booking(m, booking):
        return True
    mine = set(does(m))
    if not mine:
        return False
    return kind_of(booking, cfg) in mine


# Which door a Today card belongs behind: a draft is the Inbox's, an overdue
# invoice is Money's. A card nobody can act on is noise on someone's first
# screen (Kris 2026-09-13: "they should have their own Today page for what
# they've got on").
TOPIC_DOORS = {"drafts": "inbox", "unanswered": "inbox", "money": "money",
               "quiet": "crm", "unhappy": "crm", "promises": "crm",
               "bookings": "diary", "diary": "diary"}


def card_is_theirs(m: dict | None, card: dict, cfg: dict | None = None) -> bool:
    """Does this Today card belong on their screen? A card survives when any
    fact on it sits behind a door they can open — and, for a booking, when the
    work is theirs. Cards with no known topic always survive: Today must never
    go quiet because a new kind of card wasn't listed here."""
    if not m or normalise_role(m.get("role")) != "staff":
        return True
    facts = [f for f in (card.get("facts") or []) if isinstance(f, dict)]
    if not facts:
        return True
    kept = []
    for f in facts:
        door = TOPIC_DOORS.get(str(f.get("topic") or ""))
        if door and not can_open(m, door):
            continue
        kept.append(f)
    if not kept:
        return False
    card["facts"] = kept
    return True


def capacity_from_people(members: list[dict], cfg: dict | None) -> dict:
    """What the people say the capacity is. Two people on the desk means two
    things can happen at it at once; two people who do meet & greets means two
    meet & greets at once. Returns only what People actually decides — a kind
    nobody is assigned to keeps whatever the business typed in, so switching
    People on never quietly re-capacities a diary."""
    from app import diary_config, services as _services
    cfg = cfg or {}
    live = [m for m in members or [] if m.get("active") is not False]
    counts: dict[str, int] = {}
    for m in live:
        for k in does(m):
            counts[k] = counts.get(k, 0) + 1
    out: dict = {}
    if counts.get(DESK):
        out["front_desk"] = {**diary_config.front_desk_of(cfg), "hands": counts[DESK]}
    pools = diary_config.clean_resource_types(cfg.get("resource_types"))
    changed = False
    for svc in _services.clean_service_types(cfg.get("service_types")):
        n = counts.get(service_kind(svc["name"]))
        if not n:
            continue
        want = (svc.get("resource") or svc["name"]).strip().lower()
        for pool in pools:
            if pool["name"].strip().lower() == want and int(pool["count"]) != n:
                pool["count"] = n
                changed = True
    if changed:
        out["resource_types"] = pools
    return out


# ── the team list (docs/plan-people-team-and-jobs.md step 1) ───────────────
# Everyone who works here, whether or not they sign in. Kris, 2026-09-24: a
# partner who works the floor but never opens BLAM8, or a team member the diary
# sends out, is still a named person who can be given work. A person without
# a login opens no door — there is nothing to sign in with — so they carry no
# pages and no abilities, only what they DO and how to reach them.
TEAM_ROLES = ("owner", "manager", "staff")


def team_person(row) -> dict:
    """One `team_people` row as the app reads it."""
    row = row if isinstance(row, dict) else {}
    role = normalise_role(row.get("role"))
    if role not in TEAM_ROLES:
        role = "staff"
    return {
        "id": str(row.get("id") or ""),
        "name": " ".join(str(row.get("name") or "").split())[:80] or "Someone",
        "role": role,
        "role_label": ROLE_LABELS[role],
        "user_id": str(row.get("user_id") or ""),
        "has_login": bool(row.get("user_id")),
        "engineer_id": str(row.get("engineer_id") or ""),
        "email": str(row.get("email") or "").strip().lower(),
        "phone": " ".join(str(row.get("phone") or "").split())[:30],
        "does": clean_scopes({"does": row.get("does") or []})["does"],
        "active": row.get("active") is not False,
    }


def without_login(team: list[dict]) -> list[dict]:
    """The people who don't sign in — logged-in people are already listed
    from their member rows, which hold their doors."""
    return [t for t in team if not t.get("has_login")]


def as_worker(t: dict) -> dict:
    """A person without a login in the shape the capacity sum reads: what
    they do counts, whether or not they can open the app."""
    return {"active": t.get("active") is not False, "scopes": {"does": list(t.get("does") or [])}}


# ── invitations ─────────────────────────────────────────────────────────────
def new_token() -> str:
    """A one-time invite token. Long enough that guessing is not a strategy;
    url-safe so it rides a link and a QR unencoded."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def invite_expiry(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)) + timedelta(days=INVITE_DAYS)


def invite_url(base_url: str, token: str) -> str:
    return f"{str(base_url or '').rstrip('/')}/join/{token}"


def qr_svg(url: str, scale: int = 4) -> str:
    """The invitation as a scannable square, rendered here rather than fetched
    from an image service — a one-time credential must never travel to a third
    party just to become a picture. Empty string if it cannot be drawn, and
    the page falls back to the link alone."""
    try:
        import segno
        return segno.make(url, error="m").svg_inline(scale=scale, border=2)
    except Exception:
        return ""


def invite_email(business: str, inviter: str, role_label: str, doors: list[str],
                 url: str, days: int = INVITE_DAYS) -> tuple[str, str]:
    """The invitation as an email from the business (Kris 2026-09-13: "it
    should also show a send email invite button"). Plain text, because it goes
    out of their own mailbox and has to read like something a person sent —
    and because a link in a plain message is a link anyone can check before
    they click it."""
    biz = " ".join(str(business or "").split()) or "a business"
    who = " ".join(str(inviter or "").split())
    subject = f"{who} has invited you to {biz} on BLAM8" if who else f"You've been invited to {biz} on BLAM8"
    lines = [
        f"{who + ' has added you' if who else 'You have been added'} to {biz} on BLAM8 "
        f"as {ROLE_AS.get(role_label.lower(), role_label)}.",
        "",
    ]
    if doors:
        lines += ["You'll be able to open: " + ", ".join(doors) + ".", ""]
    lines += [
        "Accept here:",
        url,
        "",
        f"The link works once and lasts {days} days. You sign in with your own "
        "Google or Microsoft account — you never get a password of theirs, and "
        "it costs you nothing.",
        "",
        "If you weren't expecting this, ignore it; nothing happens until you sign in.",
    ]
    return subject, "\n".join(lines)


def invite_state(row, now: datetime | None = None) -> str:
    """'pending' | 'accepted' | 'expired' | 'revoked' — what the People page
    says beside an invitation."""
    row = row if isinstance(row, dict) else {}
    if row.get("revoked_at"):
        return "revoked"
    if row.get("accepted_at"):
        return "accepted"
    now = now or datetime.now(timezone.utc)
    exp = row.get("expires_at")
    if isinstance(exp, str):
        try:
            exp = datetime.fromisoformat(exp.replace("Z", "+00:00"))
        except ValueError:
            exp = None
    if isinstance(exp, datetime):
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp <= now:
            return "expired"
    return "pending"


def usable(row, now: datetime | None = None) -> bool:
    return invite_state(row, now) == "pending"


def seats_left(members: list[dict], invites: list[dict],
               now: datetime | None = None) -> int:
    """How many more people this business may add. Free seats, soft-capped so
    the allowance a business already pays for isn't shared with a town.

    `now` is threaded through to the expiry check rather than read from the
    wall clock inside it — without it this function could not be tested
    against a fixed date, and a test that froze the date everywhere else
    silently failed the next morning when a fixture's invitation expired in
    real time (caught 2026-09-14)."""
    used = sum(1 for m in members or [] if m.get("active") is not False)
    used += sum(1 for i in invites or [] if usable(i, now))
    return max(0, MAX_PEOPLE - used)
