"""Build-request loop — dispatch an approved request to a GitHub issue and keep
its storyline (docs/build-request-loop.md).

  * `storyline(row)`   pure: the six-stage story the admin page draws
  * `create_issue`     /admin "Approve & build" -> a labelled GitHub issue
  * `approve_plan`     /admin "Approve plan"     -> adds the plan-approved label
  * `verify_signature` the workflow -> BLAM8 event hook (src/app/build_hooks.py)

Only sanitised request text leaves BLAM8: no email, tenant name or page URL.
"""
import hashlib
import hmac
import re
import time
from datetime import datetime, timezone

import httpx

from app.config import settings

SOURCES = ("tenant", "agent", "owner")
API = "https://api.github.com"
SIGNATURE_MAX_AGE_S = 600

# Stage order = the story the page tells.
STAGES = (
    ("filed", "Filed"), ("triage", "Triage"), ("plan", "Plan"),
    ("build", "Build"), ("review", "Review"), ("shipped", "Shipped"),
)
STAGE_KEYS = tuple(k for k, _ in STAGES)

# Events the workflows may report, and the row status each one moves it to.
EVENT_STATUS = {
    "plan_posted": None, "plan_approved": "approved", "building": "building",
    "pr_opened": "pr_open", "merged": "done", "pr_closed": "approved",
    "failed": None,
}

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<!\w)\+?\d[\d ()-]{8,}\d")
_URL = re.compile(r"https?://\S+")


class BuildRequestError(Exception):
    """A dispatch problem the operator should read (shown as a flash message)."""


def configured() -> bool:
    return bool(settings.github_build_token and settings.github_build_repo)


def sanitise(text: str) -> str:
    """Strip contact details from request text before it goes to GitHub."""
    t = _EMAIL.sub("[email]", text or "")
    t = _URL.sub("[link]", t)
    t = _PHONE.sub("[phone]", t)
    return t.strip()[:4000]


def default_source(row: dict) -> str:
    """Review depth follows who raised it: an explicit owner/agent `source` wins,
    then rows the concierge filed (submitted_by 'assistant chat'), else tenant."""
    src = (row.get("source") or "").strip()
    if src in ("owner", "agent"):
        return src
    return "agent" if row.get("submitted_by") == "assistant chat" else "tenant"


def labels_for(source: str) -> list[str]:
    stage = "plan-approved" if source == "owner" else "needs-plan"
    return ["build-request", stage, f"src:{source}"]


def issue_body(row: dict, source: str, notes: str) -> str:
    return "\n".join([
        "### Request reference", row.get("reference") or "(none)", "",
        "### Source", source, "",
        "### Request (sanitised, no personal data)", sanitise(row.get("body") or ""), "",
        "### Owner notes", (notes or "").strip()[:2000] or "_none_",
    ])


def _headers() -> dict:
    return {"Authorization": f"Bearer {settings.github_build_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def _gh(method: str, path: str, **kw) -> dict:
    if not configured():
        raise BuildRequestError("GitHub is not configured (set GITHUB_BUILD_TOKEN).")
    try:
        r = httpx.request(method, f"{API}/repos/{settings.github_build_repo}{path}",
                          headers=_headers(), timeout=15, **kw)
    except httpx.HTTPError as e:
        raise BuildRequestError(f"GitHub unreachable: {e.__class__.__name__}") from e
    if r.status_code >= 300:
        raise BuildRequestError(f"GitHub refused ({r.status_code}): {r.text[:200]}")
    return r.json()


def create_issue(row: dict, source: str, notes: str) -> dict:
    """-> {number, url}. Title carries the reference, never the request body."""
    if source not in SOURCES:
        raise BuildRequestError("Unknown source.")
    title = f"{row.get('reference') or row['id'][:8]}: {sanitise(row.get('body') or '')[:70]}"
    data = _gh("POST", "/issues", json={
        "title": title, "body": issue_body(row, source, notes),
        "labels": labels_for(source)})
    return {"number": data["number"], "url": data["html_url"]}


def approve_plan(issue_number: int) -> None:
    _gh("POST", f"/issues/{int(issue_number)}/labels", json={"labels": ["plan-approved"]})


def issue_url(row: dict) -> str | None:
    if row.get("github_issue"):
        return f"https://github.com/{settings.github_build_repo}/issues/{row['github_issue']}"
    return None


def pr_url(row: dict) -> str | None:
    if row.get("github_pr"):
        return f"https://github.com/{settings.github_build_repo}/pull/{row['github_pr']}"
    return None


# ── Signed events from the workflows ─────────────────────────────────────────
def sign(body: bytes, ts: str, secret: str | None = None) -> str:
    key = (secret if secret is not None else settings.build_request_secret).encode()
    return hmac.new(key, ts.encode() + b"." + body, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, ts: str, signature: str, now: float | None = None) -> bool:
    """HMAC over `ts.body`, and ts must be recent so a captured request can't be
    replayed later. False when no secret is configured."""
    if not settings.build_request_secret or not signature:
        return False
    try:
        age = abs((now if now is not None else time.time()) - float(ts))
    except (TypeError, ValueError):
        return False
    if age > SIGNATURE_MAX_AGE_S:
        return False
    return hmac.compare_digest(sign(body, ts), signature)


def make_event(stage: str, note: str = "", url: str | None = None) -> dict:
    return {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "stage": stage, "note": (note or "")[:300], "url": url}


# ── The storyline the page draws ─────────────────────────────────────────────
def storyline(row: dict) -> dict:
    """-> {stages: [{key,label,state,detail}], current, source, closed, failed}.
    state is done|current|pending. Derived from status + source + events, so it
    stays right for rows that never entered the loop (they sit at Triage)."""
    status = row.get("status") or "new"
    events = row.get("build_events") or []
    kinds = [e.get("stage") for e in events]
    source = default_source(row)
    closed = status == "closed"
    failed = bool(kinds) and kinds[-1] == "failed"
    plan_needed = source != "owner"
    plan_ready = "plan_posted" in kinds
    plan_ok = (not plan_needed) or "plan_approved" in kinds or status in (
        "building", "pr_open", "done")

    if status in ("done", "resolved"):
        current = "shipped"
    elif status == "pr_open":
        current = "review"
    elif status == "building" or (status == "approved" and plan_ok):
        current = "build"
    elif status == "approved":
        current = "plan"
    else:
        current = "triage"   # new / reviewing / planned / open / in_progress / closed

    idx = STAGE_KEYS.index(current)
    finished = status in ("done", "resolved")
    detail = {"triage": "Waiting for your decision" if current == "triage" else "",
              "plan": ("Plan ready: your call" if plan_ready else "Planner running")
              if current == "plan" else ("Skipped (owner)" if not plan_needed and idx > 2 else ""),
              "build": ("Queued" if status == "approved" else "Builder running")
              if current == "build" else "",
              "review": "PR open: review and merge" if current == "review" else "",
              "shipped": "Live" if finished else ""}
    stages = []
    for i, (key, label) in enumerate(STAGES):
        state = "done" if (i < idx or (finished and i <= idx)) else (
            "current" if i == idx and not closed else "pending")
        stages.append({"key": key, "label": label, "state": state, "detail": detail.get(key, "")})
    return {"stages": stages, "current": current, "source": source,
            "closed": closed, "failed": failed, "in_loop": bool(row.get("github_issue"))}


def stage_counts(rows: list[dict]) -> dict:
    counts = {k: 0 for k in STAGE_KEYS}
    counts["closed"] = 0
    for r in rows:
        s = storyline(r)
        counts["closed" if s["closed"] else s["current"]] += 1
    return counts
