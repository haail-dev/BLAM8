"""People — the pages behind Settings › People, and the /join/{token} door.

Kris, 2026-09-13: "we need some way to add employees to BLAM8 and allow the
business to decide what they're able to see/access, even if it's just an
employee profile, added via invite or QR" — and, on the shape: a Settings row
that opens a list, with a page per person.

Three surfaces:

  /app/people             the business's people, and the invitations out.
                          Owner and Manager only.
  /app/people/{user_id}   one person: their name, role, the doors they open.
                          An admin edits; a person opening their own entry
                          sees their profile and may change only their name.
  /join/{token}           the invitation itself, signed out. Names the
                          business, who invited them and what they will be
                          able to see, then hands them to the normal SSO
                          sign-in; the callback binds them (app/auth_routes).

Writes go through the service role (src/app/db.py): membership IS the RLS key, so
a user-scoped client can never be the thing that grants access. Every write
here is behind require_onboarded, the admin check and CSRF.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from app import db, handoffs, mailbox, member_calendar, navcache, people
from app.auth import TenantCtx, require_onboarded
from app.security import require_csrf
from app.templating import templates
from fastapi.concurrency import run_in_threadpool

log = logging.getLogger(__name__)

router = APIRouter(prefix="/app/people", tags=["people"])
join_router = APIRouter(tags=["people"])

JOIN_COOKIE = "bl_join"


def set_join_cookie(response: Response, token: str) -> None:
    """The invitation, carried across the SSO round trip — the same trick the
    partner join uses. One hour: long enough to sign in, short enough that a
    shared computer doesn't inherit it."""
    response.set_cookie(JOIN_COOKIE, token, max_age=3600, httponly=True,
                        secure=True, samesite="lax", path="/")


def clear_join_cookie(response: Response) -> None:
    response.delete_cookie(JOIN_COOKIE, path="/")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def me(ctx: TenantCtx) -> dict | None:
    """The signed-in person's membership row, cleaned — or None for an account
    that predates People, whose single user owns it."""
    try:
        row = db.get_member(ctx.tenant_id, ctx.user_id or "")
    except Exception:
        # Fail CLOSED (M1): a failed read must never read as "the owner".
        log.warning("member read failed (tenant=%s)", ctx.tenant_id, exc_info=True)
        return people.member({"role": "staff", "active": False, "scopes": {}})
    return people.member(row) if row else None


def require_admin(ctx: TenantCtx = Depends(require_onboarded)) -> TenantCtx:
    """Managing people is the owner's and manager's job. Staff who follow a
    link land back on their own Today rather than a wall."""
    if not people.is_admin(me(ctx)):
        raise HTTPException(status_code=303, headers={"Location": "/app/"})
    return ctx


def _out_notes(ctx: TenantCtx) -> list[dict]:
    """What this person has asked of others and is still waiting on — the
    owner's half of the sharing slice. An answered note leaves the list; the
    booking it was about keeps it for good."""
    try:
        rows = db.list_staff_notes_sent(ctx.tenant_id, ctx.user_id or "")
    except Exception:
        log.warning("sent notes read failed (tenant=%s)", ctx.tenant_id, exc_info=True)
        return []
    return [r for r in rows if handoffs.is_open(r)]


def _mailboxes(ctx: TenantCtx) -> list[dict]:
    """The addresses that may be given to somebody — every connected mailbox
    but the oldest, which is the business's default and the owner's own."""
    try:
        boxes = db.list_mailboxes(ctx.tenant_id, client=ctx.client)
    except Exception:
        log.warning("mailbox read failed (tenant=%s)", ctx.tenant_id, exc_info=True)
        return []
    return [{"id": b["id"], "display": b.get("label") or b.get("address") or "Mailbox"}
            for b in people.assignable_mailboxes(boxes)]


def _kinds(ctx: TenantCtx) -> list[dict]:
    """What this business has to delegate — its desk and its services."""
    try:
        return people.kinds_for(db.get_scheduling_config(ctx.tenant_id, client=ctx.client) or {})
    except Exception:
        return []


def _capacity_follows_people(ctx: TenantCtx) -> None:
    """Two people on the desk means two things can happen at it at once; two
    who do meet & greets means two at a time. The diary's own numbers follow
    the people (docs/plan-people.md slice 2) — best-effort, and only for the
    kinds somebody is actually assigned to."""
    try:
        cfg = db.get_scheduling_config(ctx.tenant_id, client=ctx.client) or {}
        members = [people.member(r) for r in db.list_members(ctx.tenant_id)]
        # People who don't sign in still take the subjects in (step 1).
        members += [people.as_worker(t) for t in _team(ctx) if not t["has_login"]]
        changes = people.capacity_from_people(members, cfg)
        if changes:
            db.upsert_scheduling_config(ctx.tenant_id, changes, client=ctx.client)
    except Exception:
        log.warning("capacity from people failed (tenant=%s)", ctx.tenant_id, exc_info=True)


def _team(ctx: TenantCtx) -> list[dict]:
    """Everyone on the team list, cleaned — best-effort, so a read failure
    shows the logged-in people alone rather than no page."""
    try:
        return [people.team_person(r) for r in db.list_team_people(ctx.tenant_id)]
    except Exception:
        log.warning("team read failed (tenant=%s)", ctx.tenant_id, exc_info=True)
        return []


def _mirror_member(ctx: TenantCtx, user_id: str) -> None:
    """A logged-in person's team row follows their member row: name, role,
    what they do, whether they still work here."""
    try:
        row = db.get_member(ctx.tenant_id, user_id)
        if not row:
            return
        m = people.member(row)
        db.sync_team_person_for_member(ctx.tenant_id, user_id, {
            "name": people.name_of(m), "role": m["role"], "email": m["email"] or None,
            "does": m["scopes"]["does"], "active": m["active"]})
    except Exception:
        log.warning("team mirror failed (tenant=%s)", ctx.tenant_id, exc_info=True)


def _people_ctx(ctx: TenantCtx, request: Request, **extra) -> dict:
    from app.dashboard import _ctx
    members = [people.member(r) for r in db.list_members(ctx.tenant_id)]
    invites = db.list_member_invites(ctx.tenant_id)
    rows = [{**i, "state": people.invite_state(i)} for i in invites]
    # "people" is deliberately not in dashboard._ACTIVE_DOORS: a staff member
    # with no Settings door still opens their own profile. The routes above
    # do the gating instead — admin for the list, own-or-admin for a person.
    return _ctx("people", ctx, members=members, invites=rows, out_notes=_out_notes(ctx),
                no_login=people.without_login(_team(ctx)),
                team_roles=[{"key": r, "label": people.ROLE_LABELS[r]} for r in people.TEAM_ROLES],
                seats_left=people.seats_left([m for m in members], invites),
                roles=[{"key": r, "label": people.ROLE_LABELS[r], "blurb": people.ROLE_BLURBS[r]}
                       for r in people.ROLES if r != "owner"],
                page_labels=people.PAGE_LABELS, pages=people.PAGES,
                role_blurbs=people.ROLE_BLURBS, kinds=_kinds(ctx),
                abilities=people.ABILITIES, always_open=people.ALWAYS_OPEN,
                tailorable=people.TAILORABLE, me=me(ctx),
                mailboxes=_mailboxes(ctx), **extra)


@router.get("/settings", response_class=HTMLResponse)
def people_settings(request: Request, ctx: TenantCtx = Depends(require_admin)):
    from app.dashboard import _ctx
    cfg = db.get_scheduling_config(ctx.tenant_id, client=ctx.client) or {}
    return templates.TemplateResponse(request, "app/people_settings.html", _ctx(
        "people", ctx, people_per_job=cfg.get("people_per_job", "one"),
        settings_saved=request.query_params.get("saved") == "1",
        can_change_people_settings=people.can_do(me(ctx), "settings", "change")))


@router.post("/settings", dependencies=[Depends(require_csrf)])
def save_people_settings(value: str = Form(...), ctx: TenantCtx = Depends(require_admin)):
    if not people.can_do(me(ctx), "settings", "change"):
        raise HTTPException(status_code=403, detail="You cannot change these settings.")
    if value not in ("one", "several"):
        raise HTTPException(status_code=422, detail="Choose one person or several people.")
    db.upsert_scheduling_config(ctx.tenant_id, {"people_per_job": value}, client=ctx.client)
    return RedirectResponse("/app/people/settings?saved=1", status_code=303)


@router.get("", response_class=HTMLResponse)
def people_page(request: Request, ctx: TenantCtx = Depends(require_admin)):
    """Everyone on this business, and the invitations out. A token just minted
    comes back on the query string so the link and its QR can be shown once —
    the QR is drawn here, never by an image service (a one-time credential
    does not travel to a third party to become a picture)."""
    token = (request.query_params.get("t") or "").strip()
    msg = request.query_params.get("msg") or ""
    link = qr = invite_to = ""
    if token and msg in ("invited", "sendfailed", "noemail"):
        link = people.invite_url(str(request.base_url), token)
        qr = people.qr_svg(link)
        row = db.get_member_invite(token)
        if row and row.get("tenant_id") == ctx.tenant_id:
            invite_to = (row.get("email") or "").strip()
    return templates.TemplateResponse(request, "app/people.html", _people_ctx(
        ctx, request, msg=msg, invite_token=token, invite_to=invite_to,
        sent_to=(request.query_params.get("to") or "").strip(),
        invite_link=link, invite_qr=qr))


@router.post("/invite", dependencies=[Depends(require_csrf)])
def invite(request: Request, display_name: str = Form(""), email: str = Form(""),
           role: str = Form("staff"), page: list[str] = Form([]), can: list[str] = Form([]),
           mailbox: list[str] = Form([]),
           ctx: TenantCtx = Depends(require_admin)):
    """Mint one invitation. The link is shown once on the page it returns to —
    the business sends it however it likes, or holds up the QR."""
    members = [people.member(r) for r in db.list_members(ctx.tenant_id)]
    if people.seats_left(members, db.list_member_invites(ctx.tenant_id)) <= 0:
        return RedirectResponse("/app/people?msg=full", status_code=303)
    role = people.normalise_role(role)
    if role == "owner":
        role = "manager"                      # an account has one owner; transfer is not an invite
    # Their own address, checked against the ones this business may actually
    # hand out — never trusted from the form, so a crafted post can't give
    # somebody the owner's inbox.
    givable = {b["id"] for b in _mailboxes(ctx)}
    scopes = (people.clean_scopes(
        {"pages": page, "can": can, "mailboxes": [m for m in mailbox if m in givable]}, role)
        if role in people.TAILORABLE else people.default_scopes(role))
    token = people.new_token()
    try:
        db.create_member_invite({
            "tenant_id": ctx.tenant_id, "token": token,
            "email": (email or "").strip().lower() or None,
            "display_name": " ".join(str(display_name).split())[:80] or None,
            "role": role, "scopes": scopes,
            "created_by": ctx.user_id,
            "expires_at": people.invite_expiry().isoformat(),
        })
    except Exception:
        log.warning("invite create failed (tenant=%s)", ctx.tenant_id, exc_info=True)
        return RedirectResponse("/app/people?msg=failed", status_code=303)
    try:
        db.log_agent_event(ctx.tenant_id, "people",
                           f"Invited {display_name or email or 'someone'} as "
                           f"{people.ROLE_LABELS[role]}", client=ctx.client)
    except Exception:
        pass
    return RedirectResponse(f"/app/people?msg=invited&t={token}", status_code=303)


@router.post("/invite/send", dependencies=[Depends(require_csrf)])
async def send_invite(request: Request, token: str = Form(""),
                      ctx: TenantCtx = Depends(require_admin)):
    """Email the invitation from the business's own mailbox (Kris 2026-09-13:
    "it should also show a send email invite button"). From them, not from us
    — the person being invited knows that address, and an invitation arriving
    from a stranger is one nobody opens. A send that fails leaves the link and
    the QR exactly where they were."""
    row = db.get_member_invite((token or "").strip())
    if not row or row.get("tenant_id") != ctx.tenant_id or not people.usable(row):
        return RedirectResponse("/app/people?msg=stale", status_code=303)
    to = (row.get("email") or "").strip()
    if not to:
        return RedirectResponse(f"/app/people?msg=noemail&t={token}", status_code=303)
    t = db.get_tenant(ctx.tenant_id, client=ctx.client) or {}
    role = people.normalise_role(row.get("role"))
    scopes = people.clean_scopes(row.get("scopes"), role)
    subject, body = people.invite_email(
        t.get("name") or "", (me(ctx) or {}).get("display_name") or "",
        people.ROLE_LABELS[role], [people.PAGE_LABELS[p] for p in scopes["pages"]],
        people.invite_url(str(request.base_url), token))
    try:
        await mailbox.send_mail(ctx.tenant_id, to, subject, body)
    except Exception:
        log.warning("invite send failed (tenant=%s)", ctx.tenant_id, exc_info=True)
        return RedirectResponse(f"/app/people?msg=sendfailed&t={token}", status_code=303)
    def _blocking():
        try:
            db.log_agent_event(ctx.tenant_id, "people",
                               f"Emailed {to} their invitation", client=ctx.client)
        except Exception:
            pass
        return RedirectResponse(f"/app/people?msg=sent&to={to}", status_code=303)
    return await run_in_threadpool(_blocking)


@router.post("/invite/{invite_id}/revoke", dependencies=[Depends(require_csrf)])
def revoke(invite_id: str, ctx: TenantCtx = Depends(require_admin)):
    try:
        db.revoke_member_invite(ctx.tenant_id, invite_id, _now_iso())
    except Exception:
        log.warning("invite revoke failed (tenant=%s)", ctx.tenant_id, exc_info=True)
    return RedirectResponse("/app/people?msg=revoked", status_code=303)


# ── telling a person something (slice 3) ───────────────────────────────────
# Declared BEFORE the "/{user_id}" routes below: FastAPI matches in order, and
# a bare {user_id} swallows "say" and "note" whole.
@router.post("/say", dependencies=[Depends(require_csrf)])
def say(request: Request, to_user: str = Form(""), body: str = Form(""),
        about_kind: str = Form(""), about_id: str = Form(""), about_label: str = Form(""),
        return_to: str = Form("/app/"),
        ctx: TenantCtx = Depends(require_onboarded)):
    """Say something to someone, fastened to the work it is about. Anyone on
    the business may do this — a staff member telling the owner a subject has a
    cough is the same act as the owner telling them to ring a customer.

    The `about_*` fields are stored as words as well as ids, so the record
    still reads properly when the booking is long cancelled."""
    mine = me(ctx)
    to = {people.member(r)["user_id"]: people.member(r)
          for r in db.list_members(ctx.tenant_id)}.get((to_user or "").strip())
    note = handoffs.build(
        body=body,
        from_user=ctx.user_id, from_name=(mine or {}).get("display_name") or (mine or {}).get("email"),
        to_user=(to or {}).get("user_id"), to_name=(to or {}).get("display_name") or (to or {}).get("email"),
        about_kind=about_kind, about_id=about_id, about_label=about_label)
    if not note or not to:
        return RedirectResponse(_back(return_to, "msg=saynothing"), status_code=303)
    try:
        db.create_staff_note({"tenant_id": ctx.tenant_id, **note})
        navcache.invalidate("sn:" + ctx.tenant_id + ":" + (to.get("user_id") or ""))
    except Exception:
        log.warning("staff note failed (tenant=%s)", ctx.tenant_id, exc_info=True)
        return RedirectResponse(_back(return_to, "msg=sayfailed"), status_code=303)
    try:
        db.log_agent_event(ctx.tenant_id, "people",
                           f"{note['from_name'] or 'Someone'} → {note['to_name']}: {note['body'][:120]}",
                           client=ctx.client)
    except Exception:
        pass
    return RedirectResponse(_back(return_to, "msg=said"), status_code=303)


@router.post("/note/{note_id}/{action}", dependencies=[Depends(require_csrf)])
def note_action(note_id: str, action: str, return_to: str = Form("/app/"),
                ctx: TenantCtx = Depends(require_onboarded)):
    """seen / done by the person it was asked of; cancel by whoever asked.
    Marking it seen is the whole point — it is the thing a group chat cannot
    tell you."""
    row = db.get_staff_note(ctx.tenant_id, note_id)
    if not row:
        return RedirectResponse(_back(return_to, ""), status_code=303)
    mine_id = ctx.user_id or ""
    theirs = row.get("to_user") == mine_id
    asked = row.get("from_user") == mine_id or people.is_admin(me(ctx))
    now = handoffs.now_iso()
    fields: dict = {}
    if action == "seen" and theirs:
        fields = {"seen_at": row.get("seen_at") or now}
    elif action == "done" and theirs:
        fields = {"seen_at": row.get("seen_at") or now, "done_at": now}
    elif action == "cancel" and asked:
        fields = {"cancelled_at": now}
    if fields:
        try:
            db.update_staff_note(ctx.tenant_id, note_id, fields)
            navcache.invalidate("sn:" + ctx.tenant_id + ":" + str(row.get("to_user") or ""))
        except Exception:
            log.warning("note action failed (tenant=%s)", ctx.tenant_id, exc_info=True)
    return RedirectResponse(_back(return_to, ""), status_code=303)


@router.post("/calendar/off", dependencies=[Depends(require_csrf)])
async def calendar_off(user: str = Form(""), ctx: TenantCtx = Depends(require_onboarded)):
    """Disconnect a person's own calendar (People slice 5). Theirs to switch
    off; an admin may also do it for someone who has left, which is the case
    that matters — nobody should keep receiving a business's work after they
    have gone.

    The work BLAM8 put there goes with it. A calendar left holding events that
    have stopped being kept up to date is worse than an empty one."""
    target = (user or "").strip() or (ctx.user_id or "")
    if target != (ctx.user_id or "") and not people.is_admin(me(ctx)):
        return RedirectResponse("/app/", status_code=303)
    try:
        await calendar_sync_remove(ctx.tenant_id, target)
        db.delete_member_calendar(ctx.tenant_id, target)
    except Exception:
        log.warning("calendar disconnect failed (tenant=%s)", ctx.tenant_id, exc_info=True)
        return RedirectResponse(f"/app/people/{target}?cal=offfailed", status_code=303)
    return RedirectResponse(f"/app/people/{target}?cal=off", status_code=303)


async def calendar_sync_remove(tenant_id: str, user_id: str) -> None:
    """Imported here rather than at module scope: app/calendar_sync pulls in
    the scheduling stack, and People must stay cheap to import."""
    from app import calendar_sync
    await calendar_sync.remove_from_people(tenant_id, user_id=user_id)


def _back(return_to: str, q: str) -> str:
    """Back where they pressed it, never somewhere else — these controls sit on
    a booking, a thread and a contact card as much as on Today."""
    path = return_to if str(return_to or "").startswith("/app/") else "/app/"
    if not q:
        return path
    return path + ("&" if "?" in path else "?") + q


# ── people who don't sign in (docs/plan-people-team-and-jobs.md step 1) ─────
# Declared before /{user_id} so "/team/…" is never read as a user id.
def _clean_person_form(ctx: TenantCtx, name: str, role: str, kind: list[str],
                       phone: str, email: str) -> dict:
    role = people.normalise_role(role)
    known = {k["key"] for k in _kinds(ctx)}
    return {
        "name": " ".join(str(name).split())[:80],
        "role": role if role in people.TEAM_ROLES else "staff",
        "does": [k for k in kind if k in known],
        "phone": " ".join(str(phone).split())[:30] or None,
        "email": (email or "").strip().lower()[:200] or None,
    }


@router.post("/team/add", dependencies=[Depends(require_csrf)])
def add_team_person(name: str = Form(""), role: str = Form("staff"), kind: list[str] = Form([]),
                    phone: str = Form(""), email: str = Form(""),
                    ctx: TenantCtx = Depends(require_admin)):
    """Put someone on the team who won't sign in. They open nothing — there
    is nothing to sign in with — but they can be given work, and what they
    do counts towards how many things the diary lets happen at once."""
    row = _clean_person_form(ctx, name, role, kind, phone, email)
    if not row["name"]:
        return RedirectResponse("/app/people?msg=noname", status_code=303)
    try:
        db.create_team_person({"tenant_id": ctx.tenant_id, **row})
    except Exception:
        log.warning("team add failed (tenant=%s)", ctx.tenant_id, exc_info=True)
        return RedirectResponse("/app/people?msg=failed", status_code=303)
    try:
        db.log_agent_event(ctx.tenant_id, "people",
                           f"Added {row['name']} to the team (doesn't sign in)", client=ctx.client)
    except Exception:
        pass
    if row["does"]:
        _capacity_follows_people(ctx)
    return RedirectResponse("/app/people?msg=added", status_code=303)


def _team_person_or_back(ctx: TenantCtx, person_id: str):
    row = db.get_team_person(ctx.tenant_id, person_id)
    if not row:
        return None, RedirectResponse("/app/people", status_code=303)
    t = people.team_person(row)
    if t["has_login"]:
        # A person with a login is edited where their doors are.
        return None, RedirectResponse(f"/app/people/{t['user_id']}", status_code=303)
    return t, None


@router.get("/team/{person_id}", response_class=HTMLResponse)
def team_person_page(person_id: str, request: Request, ctx: TenantCtx = Depends(require_admin)):
    t, back = _team_person_or_back(ctx, person_id)
    if back:
        return back
    return templates.TemplateResponse(request, "app/team_person.html", _people_ctx(
        ctx, request, t=t, saved=request.query_params.get("msg") == "saved"))


@router.post("/team/{person_id}", dependencies=[Depends(require_csrf)])
def save_team_person(person_id: str, name: str = Form(""), role: str = Form("staff"),
                     kind: list[str] = Form([]), phone: str = Form(""), email: str = Form(""),
                     active: str = Form(""), ctx: TenantCtx = Depends(require_admin)):
    t, back = _team_person_or_back(ctx, person_id)
    if back:
        return back
    fields = _clean_person_form(ctx, name, role, kind, phone, email)
    fields["name"] = fields["name"] or t["name"]
    fields["active"] = active == "1"
    try:
        db.update_team_person(ctx.tenant_id, person_id, fields)
    except Exception:
        log.warning("team save failed (tenant=%s)", ctx.tenant_id, exc_info=True)
    _capacity_follows_people(ctx)
    return RedirectResponse(f"/app/people/team/{person_id}?msg=saved", status_code=303)


@router.post("/team/{person_id}/login", dependencies=[Depends(require_csrf)])
def give_login(person_id: str, ctx: TenantCtx = Depends(require_admin)):
    """Give them a login: a normal invitation that names this team person, so
    accepting it joins the sign-in to them and their jobs and history stay.
    An account has one owner who signs in, so a co-owner signs in as a
    manager (their role on the team list stays theirs)."""
    t, back = _team_person_or_back(ctx, person_id)
    if back:
        return back
    members = [people.member(r) for r in db.list_members(ctx.tenant_id)]
    if people.seats_left(members, db.list_member_invites(ctx.tenant_id)) <= 0:
        return RedirectResponse("/app/people?msg=full", status_code=303)
    role = "manager" if t["role"] == "owner" else t["role"]
    token = people.new_token()
    try:
        db.create_member_invite({
            "tenant_id": ctx.tenant_id, "token": token, "team_person_id": person_id,
            "email": t["email"] or None, "display_name": t["name"],
            "role": role, "scopes": {**people.default_scopes(role), "does": t["does"]},
            "created_by": ctx.user_id,
            "expires_at": people.invite_expiry().isoformat(),
        })
    except Exception:
        log.warning("login invite failed (tenant=%s)", ctx.tenant_id, exc_info=True)
        return RedirectResponse("/app/people?msg=failed", status_code=303)
    return RedirectResponse(f"/app/people?msg=invited&t={token}", status_code=303)


@router.get("/{user_id}", response_class=HTMLResponse)
def person_page(user_id: str, request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    """One person. An admin edits them; anyone may open their own entry and
    see what they can see — their name theirs to change, their role and doors
    the business's."""
    mine = me(ctx)
    own = bool(ctx.user_id and ctx.user_id == user_id)
    if not own and not people.is_admin(mine):
        return RedirectResponse("/app/", status_code=303)
    row = db.get_member(ctx.tenant_id, user_id)
    if not row:
        return RedirectResponse("/app/people", status_code=303)
    person = people.member(row)
    try:
        asked = [r for r in db.list_staff_notes_for(ctx.tenant_id, user_id) if handoffs.is_open(r)]
    except Exception:
        asked = []
    try:
        cal = member_calendar.face(db.get_member_calendar(ctx.tenant_id, user_id))
    except Exception:
        log.warning("member calendar read failed (tenant=%s)", ctx.tenant_id, exc_info=True)
        cal = member_calendar.face(None)
    return templates.TemplateResponse(request, "app/person.html", _people_ctx(
        ctx, request, person=person, own=own, can_edit=people.is_admin(mine), asked=asked,
        cal=cal, cal_msg=(request.query_params.get("cal") or ""),
        home=people.PAGE_LABELS[people.home_page(person)],
        doors=[{"key": p, "label": people.PAGE_LABELS[p], "open": people.can_open(person, p),
                "can": [label.lower() for a, label, _h in people.abilities_for(p)
                        if people.can_do(person, p, a)]}
               for p in people.PAGES],
        saved=request.query_params.get("msg") == "saved"))


@router.post("/{user_id}", dependencies=[Depends(require_csrf)])
def save_person(user_id: str, display_name: str = Form(""), role: str = Form(""),
                page: list[str] = Form([]), active: str = Form(""),
                kind: list[str] = Form([]), can: list[str] = Form([]),
                mailbox: list[str] = Form([]),
                ctx: TenantCtx = Depends(require_onboarded)):
    """Save a person. Their own name is theirs; role, doors and whether they
    are still here belong to the business — and the owner is neither
    demoted nor switched off from this page."""
    mine = me(ctx)
    own = bool(ctx.user_id and ctx.user_id == user_id)
    if not own and not people.is_admin(mine):
        return RedirectResponse("/app/", status_code=303)
    row = db.get_member(ctx.tenant_id, user_id)
    if not row:
        return RedirectResponse("/app/people", status_code=303)
    fields: dict = {"display_name": " ".join(str(display_name).split())[:80] or None}
    if people.is_admin(mine) and people.normalise_role(row.get("role")) != "owner":
        new_role = people.normalise_role(role or row.get("role"))
        if new_role == "owner":
            new_role = "manager"
        fields["role"] = new_role
        givable = {b["id"] for b in _mailboxes(ctx)}
        base = (people.clean_scopes(
            {"pages": page, "can": can, "mailboxes": [m for m in mailbox if m in givable]},
            new_role)
            if new_role in people.TAILORABLE else people.default_scopes(new_role))
        # What they DO (slice 2). Kept for every role that works the floor: an
        # accountant does not take the subjects in, but a manager does.
        known = {k["key"] for k in _kinds(ctx)}
        base["does"] = ([k for k in kind if k in known]
                        if new_role not in people.READ_ONLY else [])
        fields["scopes"] = base
        fields["active"] = active == "1"
    elif people.is_admin(mine) and people.normalise_role(row.get("role")) == "owner":
        # The owner's row: their role, doors and whether they still work here
        # are not editable by anybody — but WHAT THEY DO is not a permission,
        # it is a fact about who takes the subjects in. Without this the owner
        # could never be given a kind, and their own calendar (slice 5) could
        # never receive a thing (found on the rig, 2026-09-14).
        known = {k["key"] for k in _kinds(ctx)}
        fields["scopes"] = {**people.clean_scopes(row.get("scopes"), "owner"),
                            "does": [k for k in kind if k in known]}
    try:
        db.update_member(ctx.tenant_id, user_id, fields)
    except Exception:
        log.warning("member save failed (tenant=%s)", ctx.tenant_id, exc_info=True)
    # A change of role, doors or switch-off bites on their NEXT request, not
    # after the caches expire (security review M1).
    from app import navcache as _navcache
    _navcache.invalidate("ut:" + user_id)
    _navcache.invalidate("tm:" + ctx.tenant_id + ":" + user_id)
    if fields.get("active") is False:
        try:
            db.delete_push_subscriptions_for_user(ctx.tenant_id, user_id)   # M30
        except Exception:
            log.warning("push unsubscribe on switch-off failed (tenant=%s)", ctx.tenant_id, exc_info=True)
    _mirror_member(ctx, user_id)
    if "scopes" in fields:
        _capacity_follows_people(ctx)
    return RedirectResponse(f"/app/people/{user_id}?msg=saved", status_code=303)


# ── the invitation itself, signed out ──────────────────────────────────────
@join_router.get("/join/{token}", response_class=HTMLResponse)
def join(token: str, request: Request):
    """What the invited person opens. Says which business, who invited them
    and what they will be able to see BEFORE any sign-in — an invitation that
    asks you to sign in first and explains afterwards is a phishing lesson."""
    invite_row = db.get_member_invite(token)
    state = people.invite_state(invite_row) if invite_row else "unknown"
    business = ""
    if invite_row and state == "pending":
        try:
            business = (db.get_tenant(invite_row["tenant_id"]) or {}).get("name") or ""
        except Exception:
            business = ""
    role = people.normalise_role((invite_row or {}).get("role"))
    scopes = people.clean_scopes((invite_row or {}).get("scopes"), role)
    return templates.TemplateResponse(request, "join.html", {
        "request": request, "token": token, "state": state, "business": business,
        "role": role, "role_label": people.ROLE_LABELS[role],
        "role_blurb": people.INVITEE_BLURBS[role],
        "role_as": people.ROLE_AS[role],
        "invited_name": (invite_row or {}).get("display_name") or "",
        "doors": [people.PAGE_LABELS[p] for p in scopes["pages"]],
    })


@join_router.post("/join/{token}")
def join_start(token: str):
    """Accepted: stash the token and hand them to the normal SSO sign-in. The
    callback (app/auth_routes) binds them to the business the token names."""
    invite_row = db.get_member_invite(token)
    if not invite_row or not people.usable(invite_row):
        return RedirectResponse(f"/join/{token}", status_code=303)
    resp = RedirectResponse("/login?join=1", status_code=303)
    set_join_cookie(resp, token)
    return resp


# ── View as (src/app/viewas.py; Kris 2026-09-24) ────────────────────────────────
# From the owner's account menu for now; it moves into People once People is
# rebuilt. Owner only — a manager viewing as the owner would be a way round the
# doors the owner set.
viewas_router = APIRouter(prefix="/app/view-as", tags=["people"])


def _real_owner(ctx: TenantCtx) -> bool:
    """Is the person actually signed in this tenant's owner? While viewing,
    ctx.user_id is the person being viewed; the real one is ctx.viewing_as."""
    real_id = getattr(ctx, "viewing_as", None) or ctx.user_id or ""
    try:
        row = db.get_member(ctx.tenant_id, real_id)
    except Exception:
        return False
    return (not row) or people.normalise_role(row.get("role")) == "owner"


@viewas_router.get("", response_class=HTMLResponse)
def view_as_page(request: Request, ctx: TenantCtx = Depends(require_onboarded)):
    from app.dashboard import _ctx
    if not _real_owner(ctx):
        raise HTTPException(status_code=303, headers={"Location": "/app/"})
    real_id = getattr(ctx, "viewing_as", None) or ctx.user_id or ""
    others = [people.member(r) for r in db.list_members(ctx.tenant_id)]
    others = [m for m in others if m.get("user_id") != real_id and m.get("active")]
    return templates.TemplateResponse(request, "app/view_as.html", _ctx(
        "view_as", ctx, others=others, viewing=bool(getattr(ctx, "viewing_as", None)),
        role_labels=people.ROLE_LABELS))


@viewas_router.post("/start", dependencies=[Depends(require_csrf)])
def view_as_start(user_id: str = Form(...), ctx: TenantCtx = Depends(require_onboarded)):
    from app import viewas
    if not _real_owner(ctx):
        raise HTTPException(status_code=403, detail="Only the account's owner can view as someone.")
    real_id = getattr(ctx, "viewing_as", None) or ctx.user_id or ""
    target = db.get_member(ctx.tenant_id, user_id)
    if not target or target.get("active") is False or user_id == real_id:
        return RedirectResponse("/app/view-as", status_code=303)
    resp = RedirectResponse("/app/", status_code=303)
    resp.set_cookie(viewas.COOKIE, viewas.sign(real_id, user_id, ctx.tenant_id),
                    max_age=viewas.MAX_AGE_S, httponly=True, secure=True, samesite="lax", path="/")
    log.info("view-as start tenant=%s by=%s as=%s", ctx.tenant_id, real_id, user_id)
    return resp


@viewas_router.post("/exit", dependencies=[Depends(require_csrf)])
def view_as_exit(ctx: TenantCtx = Depends(require_onboarded)):
    from app import viewas
    resp = RedirectResponse("/app/view-as", status_code=303)
    resp.delete_cookie(viewas.COOKIE, path="/")
    return resp
