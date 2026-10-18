"""Build-request loop: storyline, sanitising, dispatch, signed event hook and the
admin page. No network, no database."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import build_hooks, build_requests as br, db
from app.admin_auth import AdminCtx, require_admin
from app.main import app
from app.security import require_csrf


def row(**kw):
    base = {"id": "r1", "reference": "BLAM8-1001", "kind": "feature", "status": "new",
            "body": "Please email dave@x.co.uk or call +44 7700 900123 https://x.co/p",
            "submitted_by": "kim@firm.co.uk", "created_at": "2026-10-16T09:00:00+00:00",
            "build_events": [], "tenant_id": None}
    base.update(kw)
    return base


def states(r):
    return {s["key"]: s["state"] for s in br.storyline(r)["stages"]}


# ── storyline ────────────────────────────────────────────────────────────────
def test_new_request_sits_at_triage():
    s = br.storyline(row())
    assert s["current"] == "triage" and not s["in_loop"]
    assert states(row())["filed"] == "done" and states(row())["triage"] == "current"


def test_tenant_request_waits_at_plan_until_approved():
    r = row(status="approved", github_issue=7, source="tenant",
            build_events=[{"stage": "dispatched"}])
    assert br.storyline(r)["current"] == "plan"
    r["build_events"].append({"stage": "plan_posted"})
    assert "Plan ready" in br.storyline(r)["stages"][2]["detail"]
    r["build_events"].append({"stage": "plan_approved"})
    assert br.storyline(r)["current"] == "build"


def test_owner_request_skips_the_plan():
    r = row(status="approved", github_issue=7, source="owner")
    s = br.storyline(r)
    assert s["current"] == "build"
    assert [x["state"] for x in s["stages"]][:3] == ["done", "done", "done"]


def test_review_then_shipped():
    assert br.storyline(row(status="pr_open", github_issue=7, source="owner"))["current"] == "review"
    done = row(status="done", github_issue=7, source="owner")
    assert set(states(done).values()) == {"done"}


def test_failed_and_closed_flags():
    assert br.storyline(row(status="building", github_issue=7, source="owner",
                            build_events=[{"stage": "failed"}]))["failed"]
    assert br.storyline(row(status="closed"))["closed"]


def test_agent_source_inferred_from_concierge_filing():
    assert br.default_source(row(submitted_by="assistant chat")) == "agent"
    assert br.default_source(row(source="owner")) == "owner"
    assert br.default_source(row()) == "tenant"


# ── dispatch ─────────────────────────────────────────────────────────────────
def test_sanitise_strips_contact_details():
    out = br.sanitise(row()["body"])
    assert "dave@x.co.uk" not in out and "7700" not in out and "x.co/p" not in out
    assert "[email]" in out and "[phone]" in out and "[link]" in out


def test_issue_labels_follow_source():
    assert "plan-approved" in br.labels_for("owner") and "needs-plan" not in br.labels_for("owner")
    assert "needs-plan" in br.labels_for("agent") and "src:agent" in br.labels_for("agent")


def test_create_issue_posts_sanitised_text(monkeypatch):
    sent = {}

    def fake(method, path, **kw):
        sent.update(method=method, path=path, **kw)
        return {"number": 12, "html_url": "https://github.com/o/r/issues/12"}
    monkeypatch.setattr(br, "_gh", fake)
    out = br.create_issue(row(), "tenant", "keep it small")
    assert out == {"number": 12, "url": "https://github.com/o/r/issues/12"}
    payload = sent["json"]
    assert "dave@x.co.uk" not in payload["title"] + payload["body"]
    assert payload["title"].startswith("BLAM8-1001")
    assert payload["labels"] == ["build-request", "needs-plan", "src:tenant"]


def test_unconfigured_github_is_a_readable_error(monkeypatch):
    monkeypatch.setattr(br.settings, "github_build_token", "")
    with pytest.raises(br.BuildRequestError):
        br.create_issue(row(), "owner", "")


# ── signed hook ──────────────────────────────────────────────────────────────
@pytest.fixture
def hook(monkeypatch):
    monkeypatch.setattr(br.settings, "build_request_secret", "s3cret")
    monkeypatch.setattr(build_hooks.settings, "build_request_secret", "s3cret")
    state = {"row": row(status="approved", github_issue=7, source="tenant"), "writes": []}
    monkeypatch.setattr(db, "get_feature_request_by_issue",
                        lambda n: state["row"] if n == 7 else None)
    monkeypatch.setattr(db, "update_feature_request_build",
                        lambda rid, patch, event=None: state["writes"].append((patch, event)))
    return TestClient(app), state


def post(client, payload, secret="s3cret", ts=None):
    body = json.dumps(payload).encode()
    ts = ts or str(int(time.time()))
    return client.post("/hooks/build-request", content=body, headers={
        "X-Build-Timestamp": ts, "X-Build-Signature": br.sign(body, ts, secret)})


def test_hook_moves_status_and_records_event(hook):
    client, state = hook
    r = post(client, {"issue": 7, "event": "pr_opened", "pr": 31, "url": "https://github.com/o/r/pull/31"})
    assert r.status_code == 200 and r.json()["matched"]
    patch, event = state["writes"][0]
    assert patch == {"status": "pr_open", "github_pr": 31} and event["stage"] == "pr_opened"


def test_hook_rejects_bad_signature_and_stale_timestamp(hook):
    client, state = hook
    assert post(client, {"issue": 7, "event": "building"}, secret="wrong").status_code == 401
    assert post(client, {"issue": 7, "event": "building"}, ts=str(int(time.time()) - 3600)).status_code == 401
    assert not state["writes"]


def test_hook_ignores_unknown_issue_and_unknown_event(hook):
    client, state = hook
    assert post(client, {"issue": 99, "event": "building"}).json()["matched"] is False
    assert post(client, {"issue": 7, "event": "nope"}).status_code == 400


def test_hook_never_moves_a_row_backwards(hook):
    client, state = hook
    state["row"]["status"] = "pr_open"
    post(client, {"issue": 7, "event": "building"})
    assert "status" not in state["writes"][0][0]
    post(client, {"issue": 7, "event": "pr_closed"})      # unmerged: back to approved
    assert state["writes"][1][0]["status"] == "approved"


def test_hook_503_without_secret(monkeypatch):
    monkeypatch.setattr(build_hooks.settings, "build_request_secret", "")
    assert TestClient(app).post("/hooks/build-request", content=b"{}").status_code == 503


# ── admin page and actions ───────────────────────────────────────────────────
@pytest.fixture
def admin(monkeypatch):
    app.dependency_overrides[require_admin] = lambda: AdminCtx(user_id="a", email="a@example.com", role="owner")
    app.dependency_overrides[require_csrf] = lambda: None
    rows = [row(),
            row(id="r2", reference="BLAM8-1002", status="approved", github_issue=7, source="tenant",
                build_events=[{"at": "2026-10-16T10:00:00+00:00", "stage": "plan_posted", "note": "", "url": None}]),
            row(id="r3", reference="BLAM8-1003", status="pr_open", github_issue=8, github_pr=31, source="owner",
                body="<script>alert(1)</script>")]
    monkeypatch.setattr(db, "list_all_feature_requests", lambda: rows)
    monkeypatch.setattr(db, "mark_feature_requests_seen", lambda: None)
    monkeypatch.setattr(db, "count_unseen_feature_requests", lambda: 0)
    monkeypatch.setattr(db, "get_tenant", lambda *a, **k: {"name": "Acme"})
    yield TestClient(app), rows
    app.dependency_overrides.clear()


def test_page_shows_storyline_and_actions(admin, monkeypatch):
    client, _ = admin
    monkeypatch.setattr(br.settings, "github_build_token", "tok")
    html = client.get("/admin/requests").text
    assert "Approve &amp; build" in html                  # triage row
    assert "Approve plan" in html and "The plan is ready" in html   # plan row
    assert "Review PR #31" in html                        # review row
    assert "<script>alert(1)</script>" not in html        # escaped


def test_stage_filter(admin):
    client, _ = admin
    html = client.get("/admin/requests?stage=review").text
    assert "BLAM8-1003" in html and "BLAM8-1001" not in html


def test_build_button_disabled_without_token(admin, monkeypatch):
    client, _ = admin
    monkeypatch.setattr(br.settings, "github_build_token", "")
    assert "GITHUB_BUILD_TOKEN" in client.get("/admin/requests").text


def test_dispatch_creates_issue_once(admin, monkeypatch):
    client, rows = admin
    writes = []
    monkeypatch.setattr(db, "get_feature_request", lambda rid: next((r for r in rows if r["id"] == rid), None))
    monkeypatch.setattr(db, "update_feature_request_build", lambda rid, patch, event=None: writes.append((rid, patch, event)))
    monkeypatch.setattr(br, "create_issue", lambda r, s, n: {"number": 40, "url": "https://github.com/o/r/issues/40"})
    r = client.post("/admin/requests/r1/build", data={"source": "owner", "notes": "n"}, follow_redirects=False)
    assert r.status_code == 303 and "msg=built" in r.headers["location"]
    rid, patch, event = writes[0]
    assert patch == {"github_issue": 40, "source": "owner", "status": "approved"} and event["stage"] == "dispatched"
    # already dispatched -> refused, nothing written
    again = client.post("/admin/requests/r2/build", data={"source": "owner"}, follow_redirects=False)
    assert "err:" in again.headers["location"] and len(writes) == 1


def test_github_failure_is_shown_not_raised(admin, monkeypatch):
    client, rows = admin
    monkeypatch.setattr(db, "get_feature_request", lambda rid: rows[0])

    def boom(*a):
        raise br.BuildRequestError("GitHub refused (401)")
    monkeypatch.setattr(br, "create_issue", boom)
    r = client.post("/admin/requests/r1/build", data={"source": "owner"}, follow_redirects=False)
    assert r.status_code == 303 and "err:" in r.headers["location"]


def test_approve_plan_adds_label_and_event(admin, monkeypatch):
    client, rows = admin
    writes, labelled = [], []
    monkeypatch.setattr(db, "get_feature_request", lambda rid: rows[1])
    monkeypatch.setattr(db, "update_feature_request_build", lambda rid, patch, event=None: writes.append(event))
    monkeypatch.setattr(br, "approve_plan", lambda n: labelled.append(n))
    r = client.post("/admin/requests/r2/approve-plan", follow_redirects=False)
    assert r.status_code == 303 and labelled == [7] and writes[0]["stage"] == "plan_approved"
