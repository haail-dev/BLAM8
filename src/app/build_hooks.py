"""Event hook the build-request workflows call (scripts/ci/build_request_notify.py).

POST /hooks/build-request  {issue, event, pr?, note?, url?}
Signed: X-Build-Timestamp + X-Build-Signature = HMAC-SHA256(secret, ts.body).
No cookies, so it sits outside CSRF by design; the signature is the auth."""
import json
import logging

from fastapi import APIRouter, HTTPException, Request

from app import build_requests as br
from app import db
from app.config import settings

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/hooks/build-request")
async def build_request_event(request: Request):
    if not settings.build_request_secret:
        raise HTTPException(status_code=503, detail="Not configured.")
    body = await request.body()
    if len(body) > 8192 or not br.verify_signature(
            body, request.headers.get("x-build-timestamp", ""),
            request.headers.get("x-build-signature", "")):
        raise HTTPException(status_code=401, detail="Bad signature.")
    try:
        data = json.loads(body)
        issue = int(data["issue"])
        kind = str(data["event"])
    except (ValueError, KeyError, TypeError):
        raise HTTPException(status_code=400, detail="Bad payload.")
    if kind not in br.EVENT_STATUS:
        raise HTTPException(status_code=400, detail="Unknown event.")
    row = db.get_feature_request_by_issue(issue)
    if not row:
        return {"ok": True, "matched": False}      # an issue BLAM8 didn't dispatch
    patch: dict = {}
    status = br.EVENT_STATUS[kind]
    if status and (kind == "pr_closed" and row.get("status") == "pr_open"
                   or not _regresses(row.get("status"), status)):
        patch["status"] = status
    pr = data.get("pr")
    if pr is not None:
        try:
            patch["github_pr"] = int(pr)
        except (ValueError, TypeError):
            pass
    db.update_feature_request_build(
        row["id"], patch, br.make_event(kind, str(data.get("note") or ""),
                                        str(data["url"])[:300] if data.get("url") else None))
    return {"ok": True, "matched": True}


_ORDER = ["new", "reviewing", "approved", "building", "pr_open", "done"]


def _regresses(current: str | None, new: str) -> bool:
    """Late or replayed events never move a row backwards (pr_closed, an unmerged
    PR sending the row back to 'approved', is allowed explicitly above)."""
    if current not in _ORDER or new not in _ORDER:
        return False
    return _ORDER.index(new) < _ORDER.index(current)
