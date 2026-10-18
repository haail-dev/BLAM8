"""The job page a person who doesn't sign in opens — /j/{token} (src/app/job_sheets.py).

The token is the capability (the /pay and /q doctrine): no login, not tenant
scoped, rate-limited, noindex. It shows ONE job to the ONE person it was sent
to — when, what, where (a map), who's there (tap to call) — and two taps that
tell the office: On my way (which also tells the customer, the job page's own
on_my_way.send) and Done — and a way back: photos and a note from the job
(Kris 2026-09-26: "a way for the Freelancer to return info/outcome"), kept on
the booking and the customer's record. The link dies when they come off the job, the job is
cancelled, or its last day has passed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from starlette.datastructures import UploadFile as FormUploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from app import db, job_sheets, on_my_way, people, push
from app.security import rate_limit
from app.templating import templates

log = logging.getLogger(__name__)

router = APIRouter(tags=["job-sheets"])


def _load(token: str) -> tuple[dict | None, dict | None, dict | None]:
    """(sheet, booking, person) while the link is live, else Nones."""
    s = db.get_job_sheet_by_token(token or "")
    if not s or s.get("closed_at"):
        return None, None, None
    b = db.get_booking(s["booking_id"], s["tenant_id"])
    if not b or b.get("status") == "cancelled" or job_sheets.ended(b):
        return None, None, None
    row = next((r for r in db.list_team_people(s["tenant_id"]) if str(r.get("id")) == str(s["person_id"])), None)
    return s, b, (people.team_person(row) if row else None)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.get("/j/{token}", response_class=HTMLResponse,
            dependencies=[Depends(rate_limit("job_sheet_page", 30, 60))])
def job_sheet_page(request: Request, token: str, msg: str = ""):
    s, b, person = _load(token)
    if not s:
        return templates.TemplateResponse(request, "job_sheet.html", {"closed": True}, status_code=404)
    if not s.get("opened_at"):
        try:
            db.update_job_sheet(s["id"], {"opened_at": _now()})
        except Exception:
            log.warning("job sheet opened stamp failed", exc_info=True)
    f = job_sheets.facts(s["tenant_id"], b)
    try:
        sched = db.get_scheduling_config(s["tenant_id"]) or {}
    except Exception:
        sched = {}
    where = f.get("where") or ""
    return templates.TemplateResponse(request, "job_sheet.html", {
        "closed": False, "token": token, "f": f, "person": person or {"name": ""},
        "biz": job_sheets._biz_name(s["tenant_id"]), "s": s, "msg": msg,
        "map_url": ("https://www.google.com/maps/search/?api=1&query=" + quote(where)) if where else "",
        "goes": on_my_way.goes_to_customer(sched.get("intake_fields"), b),
        "etas": on_my_way.ETA_CHOICES,
        "omw_action": on_my_way.page_state(s["tenant_id"], b["id"], b),
    })


def _tell_owner(s: dict, b: dict, person: dict | None, kind: str, title: str, body: str) -> None:
    try:
        push.notify(s["tenant_id"], title=title, body=body, url=f"/app/diary/{b['id']}",
                    tag=f"{kind}-{s['id']}", kind=kind, contact=b.get("contact"))
    except Exception:
        log.warning("job sheet owner push failed", exc_info=True)
    db.log_agent_event(s["tenant_id"], kind, title, direction="inbound",
                       contact=b.get("contact"), booking_id=b.get("id"),
                       booking_ref=b.get("booking_ref"),
                       event_date=str(b.get("booking_date") or "")[:10] or None,
                       detail={"person_id": s["person_id"]})


@router.post("/j/{token}/on-my-way", dependencies=[Depends(rate_limit("job_sheet_omw", 6, 60))])
async def job_sheet_on_my_way(token: str, eta: str = Form(""), action_key: str = Form("initial")):
    s, b, person = _load(token)
    if not s:
        return RedirectResponse(f"/j/{token}", status_code=303)
    name = (person or {}).get("name") or "Someone"
    ref = b.get("booking_ref") or job_sheets.what_text(b)
    told = None
    try:
        sched = db.get_scheduling_config(s["tenant_id"]) or {}
    except Exception:
        sched = {}
    goes = on_my_way.goes_to_customer(sched.get("intake_fields"), b)
    if goes and not on_my_way.eta_phrase(eta):
        return RedirectResponse(f"/j/{token}?msg=bad_eta", status_code=303)
    if goes:
        try:
            told = await on_my_way.send(s["tenant_id"], b, eta, engineer=name,
                                       action_key=action_key, sheet_id=s["id"])
        except Exception:
            told = {"ok": False, "error": "unknown"}
        # The customer claim is shared with the owner route. The office notice
        # has its own durable marker: a lost notice response is never retried.
        if told.get("state") in ("sent", "unknown", "pre_send_failure"):
            _on_way_notice(s, b, told, name, ref)
        if told.get("ok"):
            try:
                db.claim_job_sheet(s["id"], "on_my_way_at", _now())
            except Exception:
                pass  # The receipt, not this presentation stamp, is authoritative.
        msg = ("omw_already" if told.get("duplicate") else "omw") if told.get("ok") else (
            "omw_" + str(told.get("error") or "unknown"))
        return RedirectResponse(f"/j/{token}?msg={msg}", status_code=303)
    # A non-travelling booking only tells the office. Preserve that existing tap.
    if not db.claim_job_sheet(s["id"], "on_my_way_at", _now()):
        return RedirectResponse(f"/j/{token}?msg=omw_already", status_code=303)
    _tell_owner(s, b, person, "team_on_way", f"{name} is on the way — {ref}", job_sheets.what_text(b))
    return RedirectResponse(f"/j/{token}?msg=omw", status_code=303)


def _on_way_notice(s, b, told, name, ref):
    from app import on_my_way_store, events, notify_prefs
    key, attempt = told.get("action_key"), told.get("attempt")
    if not key or not attempt:
        return
    try:
        name = told.get("person_name") or name
        title = f"{name} is on the way — {ref}"
        url = f"/app/diary/{b['id']}"
        row = on_my_way_store.action(s["tenant_id"], b["id"], key, attempt, "notice",
            {"title": title, "what": job_sheets.what_text(b), "url": url, "contact": b.get("contact")})
        events.publish(s["tenant_id"], {"type": "notification", "kind": "team_on_way"})
        if not row.get("owned"):
            return
        body = row["notice_body"]
        # notify() would insert a second in-app notice. Use its existing push
        # policy/delivery functions after the atomic notice has been recorded.
        prefs = push._prefs(s["tenant_id"])
        if notify_prefs.should_push(prefs, "team_on_way"):
            push._deliver(s["tenant_id"], {"title": title, "body": body, "url": url,
                          "tag": f"team_on_way-{row['id']}"}, "team_on_way", prefs)
        db.log_agent_event(s["tenant_id"], "team_on_way", title, direction="inbound",
            contact=b.get("contact"), booking_id=b["id"], booking_ref=b.get("booking_ref"),
            event_date=str(b.get("booking_date") or "")[:10] or None,
            detail={"action_id": row["id"]})
        on_my_way_store.action(s["tenant_id"], b["id"], key, attempt, "notice_attempted")
    except Exception:
        log.warning("On-my-way office notice needs review (booking=%s)", b["id"])


MAX_SHEET_PHOTOS = 8


@router.post("/j/{token}/photos", dependencies=[Depends(rate_limit("job_sheet_photos", 6, 60))])
async def job_sheet_photos(request: Request, token: str):
    """Photos and a note back from the job — the fault found, the finished
    work, the part that's needed. Kept on the booking (the owner's job page)
    and the customer's record (so the thread can attach them to a reply)."""
    from app import documents, photos
    s, b, person = _load(token)
    if not s:
        return RedirectResponse(f"/j/{token}", status_code=303)
    form = await request.form()
    name = (person or {}).get("name") or "Someone"
    first = name.split(" ")[0]
    contact = (b.get("contact") or "").strip().lower() or None
    kept = 0
    for f in form.getlist("photo")[:MAX_SHEET_PHOTOS]:
        if not isinstance(f, FormUploadFile) or not f.filename:
            continue
        data = await f.read()
        if not data or len(data) > documents.MAX_BYTES * 3:
            continue
        fname, data, ct = photos.web_ready(data, f.filename, f.content_type)
        if not photos.is_photo(fname, ct) or len(data) > documents.MAX_BYTES:
            continue
        if documents.store(s["tenant_id"], filename=fname, data=data, content_type=ct,
                           source="upload", contact_email=contact, booking_id=b["id"],
                           label=f"From {first}"):
            kept += 1
    note = str(form.get("note") or "").strip()[:1000]
    if not kept and not note:
        return RedirectResponse(f"/j/{token}?msg=nothing#back", status_code=303)
    what = " and ".join(x for x in (f"{kept} photo{'s' if kept != 1 else ''}" if kept else "",
                                     "a note" if note else "") if x)
    _tell_owner(s, b, person, "team_photos",
                f"{name} sent {what} from {b.get('booking_ref') or job_sheets.what_text(b)}",
                (f"“{note}”" if note else job_sheets.what_text(b)))
    return RedirectResponse(f"/j/{token}?msg=sent_back#back", status_code=303)


@router.post("/j/{token}/done", dependencies=[Depends(rate_limit("job_sheet_done", 6, 60))])
def job_sheet_done(token: str):
    s, b, person = _load(token)
    if not s:
        return RedirectResponse(f"/j/{token}", status_code=303)
    name = (person or {}).get("name") or "Someone"
    if not db.claim_job_sheet(s["id"], "done_at", _now()):
        return RedirectResponse(f"/j/{token}?msg=done", status_code=303)
    _tell_owner(s, b, person, "team_job_done",
                f"{name} has finished — {b.get('booking_ref') or job_sheets.what_text(b)}",
                job_sheets.what_text(b) + " · marked done from the job sheet")
    # After the job (src/app/after_job.py): a helper's Done is the job being done
    # for a business that marks jobs done — its deadlines start now.
    try:
        from app import after_job
        cfg = db.get_scheduling_config(s["tenant_id"]) or {}
        if after_job.tenant_uses(cfg.get("service_types")) and \
                after_job.clean_done_when(cfg.get("job_done_when")) == "marked":
            after_job.mark_done(s["tenant_id"], b, by=f"helper:{name}", sched_cfg=cfg)
    except Exception:
        log.warning("job sheet after-job mark failed", exc_info=True)
    return RedirectResponse(f"/j/{token}?msg=done", status_code=303)
