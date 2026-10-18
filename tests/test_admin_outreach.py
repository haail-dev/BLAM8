"""Outreach checks never call a live database, provider or mailbox."""
import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import admin_outreach as outreach
from app.admin_auth import AdminCtx, require_admin
from app.main import app
from app.security import require_csrf

NOW = datetime(2026, 10, 12, 10, tzinfo=timezone.utc)


@pytest.fixture
def cfg():
    return outreach.validate_config({
        "sender": "Example <sender@example.com>", "reply_to": ["reply@example.com"],
        "unsubscribe_email": "reply@example.com", "inbox_tenant_id": "11111111-1111-1111-1111-111111111111",
        "fallback_line": "You can review the details and reply with any questions.",
        "plan": {"2026-10-12": 2},
        "steps": [
            {"step_order": 0, "action_type": "send_email", "delay_days": 0, "template_subject": "An introduction",
             "template_body": "Hello {{contact.name}} at {{company.name}}."},
            {"step_order": 1, "action_type": "send_email", "delay_days": 7, "template_subject": "Following up",
             "template_body": "{{tailored_line}}"},
        ],
    })


@pytest.fixture
def prospect():
    return {"id": "p1", "business_name": "Example Ltd", "email": "owner@example.com", "kind": "partner",
            "status": "IDENTIFIED", "company_number": "12345678", "email_source": "https://example.com/contact",
            "email_verified": True, "soft_match": False, "contact_name": "Alex", "description": "Design services in Bath"}


class Query:
    def __init__(self, fake, table):
        self.fake, self.table = fake, table
        self.filters, self.action, self.fields, self.bounds = [], "select", {}, None
        self.ignore = False

    def select(self, *a): return self
    def eq(self, key, value):
        if key == "config_snapshot" and isinstance(value, str): value = json.loads(value)
        self.filters.append(lambda r: r.get(key) == value)
        return self
    def gte(self, key, value):
        self.filters.append(lambda r: (r.get(key) or "") >= value)
        return self
    def in_(self, key, values):
        self.filters.append(lambda r: r.get(key) in values)
        return self
    def order(self, *a, **kw): return self
    def limit(self, size): self.bounds = (0, size - 1); return self
    def range(self, start, end): self.bounds = (start, end); return self
    def update(self, fields): self.action, self.fields = "update", fields; return self
    def insert(self, fields): self.action, self.fields = "insert", fields; return self
    def upsert(self, fields, **kw):
        self.action, self.fields, self.ignore = "upsert", fields, kw.get("ignore_duplicates", False)
        return self

    def execute(self):
        table = self.fake.tables.setdefault(self.table, [])
        matched = [r for r in table if all(f(r) for f in self.filters)]
        if self.action in ("upsert", "insert"):
            fields = copy.deepcopy(self.fields)
            keys = {"admin_outreach_messages": ("email", "step"), "admin_outreach_suppressions": ("email",),
                    "admin_outreach_digests": ("day",), "ignored_contacts": ("tenant_id", "address")}.get(self.table, ("id",))
            found = next((r for r in table if all(r.get(k) == fields.get(k) for k in keys)), None)
            if found is not None:
                if not self.ignore: found.update(fields)
            else:
                fields.setdefault("id", f"id-{len(table)}")
                fields.setdefault("created_at", NOW.isoformat())
                table.append(fields)
            matched = [fields]
        elif self.action == "update":
            for r in matched: r.update(copy.deepcopy(self.fields))
        if self.bounds:
            matched = matched[self.bounds[0]:self.bounds[1]+1]
        return SimpleNamespace(data=copy.deepcopy(matched))


class FakeDB:
    def __init__(self, cfg, prospect):
        self.tables = {"admin_outreach_config": [{"id": 1, "config": cfg, "enabled": True, "last_result": {}}],
                       "vcrm_prospects": [prospect],
                       "email_connections": [{"id": "mailbox", "tenant_id": cfg["inbox_tenant_id"], "sync_failing_since": None}]}
        self.claims, self.claim_result = [], False
    def table(self, name): return Query(self, name)
    def rpc(self, name, params):
        if name == "admin_outreach_lock":
            self.tables["admin_outreach_config"][0]["lease_token"] = params["p_token"]
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=True))
        if name == "admin_outreach_claim":
            self.claims.append(params)
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=self.claim_result))
        if name == "admin_outreach_complete":
            for row in self.tables["admin_outreach_messages"]:
                if row["id"] == params["p_id"]:
                    row.update(status="sent", sent_at=NOW.isoformat())
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=None))
        raise AssertionError(name)


@pytest.fixture
def fake(monkeypatch, cfg, prospect):
    f = FakeDB(cfg, prospect)
    monkeypatch.setattr(outreach.db, "db", lambda: f)
    monkeypatch.setattr(outreach, "utcnow", lambda: NOW)
    monkeypatch.setenv("RESEND_API_KEY", "test-only")
    monkeypatch.setenv("RESEND_FULL_KEY", "test-only")
    monkeypatch.setattr(outreach.httpx, "get", lambda *a, **kw: pytest.fail("Unexpected network request"))
    monkeypatch.setattr(outreach.httpx, "post", lambda *a, **kw: pytest.fail("Unexpected network request"))
    return f


@pytest.mark.parametrize("changes", [
    {"daily_cap": 0}, {"min_gap_seconds": 0}, {"working_days": []}, {"working_days": [7]},
    {"timezone": "invalid"}, {"start_time": "18:00"}, {"plan": {"bad": 3}},
    {"plan": {"2026-10-12": -1}}, {"sender": "x@example.com\nBcc:x@y.com"}, {"stop_line": ""},
])
def test_reject_invalid_config(cfg, changes):
    with pytest.raises(ValueError): outreach.validate_config(cfg | changes)


def test_dst_window_holidays_and_catchup(cfg):
    assert outreach.in_window(cfg, datetime(2026, 10, 12, 8, tzinfo=timezone.utc))
    assert not outreach.in_window(cfg, datetime(2026, 10, 26, 8, tzinfo=timezone.utc))
    assert not outreach.in_window(cfg, datetime(2026, 10, 12, 16, tzinfo=timezone.utc))
    assert not outreach.in_window(cfg, datetime(2026, 10, 11, 10, tzinfo=timezone.utc))
    assert not outreach.in_window(cfg | {"holidays": ["2026-10-12"]}, NOW)
    assert outreach.target(cfg | {"plan": {"2026-10-09": 5, "2026-10-12": 10, "2026-10-13": 20}}, NOW) == 15


def test_composition_is_grounded_and_always_has_stop(cfg, prospect):
    assert outreach.compose(prospect | {"contact_name": ""}, cfg, 0)["text"].startswith("Hello there at")
    payload = outreach.compose(prospect, cfg, 1)
    assert prospect["description"] in payload["text"]
    assert payload["text"].startswith("You describe")
    assert cfg["stop_line"] in payload["text"]
    assert payload["headers"]["List-Unsubscribe"] == "<mailto:reply@example.com?subject=stop>"
    assert outreach.tailored_line(prospect | {"description": ""}, cfg) == cfg["fallback_line"]


@pytest.mark.parametrize("changes", [{"email_verified": False}, {"soft_match": True}, {"company_number": ""}, {"email_source": ""}, {"next_action": "BOUNCED"}])
def test_unverified_or_blocked_prospects_ineligible(prospect, changes):
    assert not outreach.eligible(prospect | changes, set())


def test_prepare_dedupes_email_and_requires_fresh_approval(fake, cfg, prospect):
    fake.tables["vcrm_prospects"].append(prospect | {"id": "p2", "email": "OWNER@example.com"})
    assert outreach.prepare(cfg, NOW) == 1
    assert outreach.prepare(cfg, NOW) == 0
    msg = fake.tables["admin_outreach_messages"][0]
    msg["status"] = "approved"
    changed = cfg | {"stop_line": "Reply stop to unsubscribe."}
    outreach.prepare(changed, NOW)
    assert msg["status"] == "draft"
    assert changed["stop_line"] in msg["payload"]["text"]


def test_followup_requires_recorded_first_send(fake, cfg, prospect):
    prospect.update(status="EMAILED", next_action_date="2026-10-12")
    assert outreach.prepare(cfg, NOW) == 0
    fake.tables["admin_outreach_messages"] = [{"id": "old", "email": prospect["email"], "step": 0, "status": "sent", "sent_at": "2026-10-05T10:00:00+00:00"}]
    assert outreach.prepare(cfg, NOW) == 1
    assert outreach.prepare(cfg, NOW) == 0


def test_sync_failure_blocks_claim_and_send(fake, monkeypatch):
    monkeypatch.setattr(outreach, "sync", lambda *a: (_ for _ in ()).throw(RuntimeError("unavailable")))
    assert "Job failed" in outreach.run()["blocked"]
    assert not fake.claims
    assert fake.tables["admin_outreach_config"][0]["lease_token"] is None


def test_paused_previews_and_draft_mode_cannot_send(fake, monkeypatch):
    monkeypatch.setattr(outreach, "sync", lambda *a: [])
    assert "Awaiting approval" in outreach.run()["blocked"]
    assert not fake.claims
    fake.tables["admin_outreach_config"][0]["enabled"] = False
    assert outreach.run()["blocked"] == "Paused"
    assert outreach.run(preview=True)["blocked"] == "Preview only"
    assert not fake.claims


def test_one_send_per_tick_and_timeout_is_not_retried(fake, monkeypatch, cfg):
    monkeypatch.setattr(outreach, "sync", lambda *a: [])
    outreach.prepare(cfg, NOW)
    fake.tables["admin_outreach_messages"][0]["status"] = "approved"
    fake.claim_result = True
    attempted = []
    def timeout(self, message):
        attempted.append(message)
        raise TimeoutError()
    monkeypatch.setattr(outreach.Provider, "send", timeout)
    assert "Job failed" in outreach.run()["blocked"]
    assert fake.tables["admin_outreach_messages"][0]["status"] == "unknown"
    assert "Reconcile" in outreach.run()["blocked"]
    assert len(attempted) == 1


def test_stop_after_reply_and_quoted_stop_line(fake, cfg, prospect):
    prospect["status"] = "REPLIED"
    fake.tables["email_log"] = [{"id": "in1", "tenant_id": cfg["inbox_tenant_id"], "kind": "inbound",
                                "from_address": prospect["email"], "subject": "Re: introduction", "body_in": "Please stop", "created_at": NOW.isoformat()}]
    provider = SimpleNamespace(list=lambda path: [])
    outreach.sync(cfg, provider, NOW)
    assert fake.tables["admin_outreach_suppressions"][0]["email"] == prospect["email"]
    assert prospect["status"] == "NOT_NOW"
    assert outreach.fresh_text('Thanks, interested.\nOn Monday someone wrote:\nReply "stop"') == "Thanks, interested."


def test_no_domain_guessing(fake, cfg, prospect):
    fake.tables["email_log"] = [{"id": "in1", "tenant_id": cfg["inbox_tenant_id"], "kind": "inbound",
                                "from_address": "other@example.com", "subject": "An introduction", "body_in": "Please stop", "created_at": NOW.isoformat()}]
    outreach.sync(cfg, SimpleNamespace(list=lambda path: []), NOW)
    assert not fake.tables.get("admin_outreach_suppressions")


def test_changed_reply_subject_still_stops_followup(fake, cfg, prospect):
    prospect["status"] = "EMAILED"
    fake.tables["email_log"] = [{"id": "in1", "tenant_id": cfg["inbox_tenant_id"], "kind": "inbound",
                                "from_address": prospect["email"], "subject": "A different topic", "body_in": "Let's talk",
                                "created_at": NOW.isoformat()}]
    outreach.sync(cfg, SimpleNamespace(list=lambda path: []), NOW)
    assert prospect["status"] == "REPLIED"


def test_csv_prevalidates_entire_file_and_cannot_reset_existing(fake):
    header = "business_name,email,company_number,email_source,email_verified,soft_match\n"
    with pytest.raises(ValueError):
        outreach.import_csv(header + "First,a@example.org,123,site,true,false\nBad,bad,123,site,true,false\n")
    assert len(fake.tables["vcrm_prospects"]) == 1
    assert outreach.import_csv(header + "Duplicate,OWNER@example.com,123,site,true,false\n") == 0


def test_day_failure_and_plan_start_do_not_count_older_campaign(fake, cfg):
    fake.tables["admin_outreach_messages"] = [{"step": 0, "status": "sent", "sent_at": "2026-10-01T10:00:00+00:00"}]
    summary = outreach.digest(cfg, NOW.replace(hour=18), {"blocked": "cap"})
    assert summary["failed_day"] and summary["shortfall"] == 2


@pytest.fixture
def client(fake):
    app.dependency_overrides[require_admin] = lambda: AdminCtx(user_id="admin", email="admin@example.com", role="owner")
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_admin_page_escapes_records_and_shows_controls(client, fake):
    fake.tables["vcrm_prospects"][0]["business_name"] = "<script>alert(1)</script>"
    response = client.get("/admin/outreach")
    assert response.status_code == 200
    assert "Pause all sending" in response.text
    assert "&lt;script&gt;" in response.text


def test_settings_pause_and_invalid_settings_do_not_write(client, fake, cfg):
    assert client.post("/admin/outreach/settings", data={"config_json": json.dumps(cfg)}, follow_redirects=False).status_code == 303
    assert fake.tables["admin_outreach_config"][0]["enabled"] is False
    assert client.post("/admin/outreach/settings", data={"config_json": '{"daily_cap": -1}'}).status_code == 400


def test_settings_form_roundtrip(client, fake, cfg):
    fields = {k: str(v) for k, v in cfg.items() if isinstance(v, (str, int))}
    fields.update(working_days=["0", "1", "2", "3", "4"], plan_text="2026-10-12, 3\n2026-10-13, 5",
                  reply_to="reply@example.com\narchive@example.com", holidays="2026-12-25", legacy_subjects="Earlier subject",
                  followup_days="7")
    for i, step in enumerate(cfg["steps"]):
        fields[f"subject_{i}"] = step["template_subject"]
        fields[f"body_{i}"] = step["template_body"]
    response = client.post("/admin/outreach/settings", data=fields, follow_redirects=False)
    assert response.status_code == 303
    saved = fake.tables["admin_outreach_config"][0]["config"]
    assert saved["plan"] == {"2026-10-12": 3, "2026-10-13": 5}
    assert saved["reply_to"] == ["reply@example.com", "archive@example.com"]


def test_approval_requires_current_configuration(client, fake, cfg):
    outreach.prepare(cfg, NOW)
    message = fake.tables["admin_outreach_messages"][0]
    fake.tables["admin_outreach_config"][0]["config"] = cfg | {"name": "Revised campaign"}
    client.post(f'/admin/outreach/approve/{message["id"]}', follow_redirects=False)
    assert message["status"] == "draft"


def test_disconnected_inbox_blocks_sync(fake, cfg):
    fake.tables["email_connections"] = []
    with pytest.raises(RuntimeError):
        outreach.sync(cfg, SimpleNamespace(list=lambda path: pytest.fail("Should block before provider read")), NOW)


def test_restart_backfills_failed_planned_day(fake, cfg):
    summary = outreach.digest(cfg | {"plan": {"2026-10-09": 5, "2026-10-12": 2}}, NOW, {})
    previous = next(d for d in fake.tables["admin_outreach_digests"] if d["day"] == "2026-10-09")
    assert previous["summary"]["failed_day"] and previous["summary"]["shortfall"] == 5
    assert summary["shortfall"] == 7


def test_csrf_and_auth_are_required(client):
    del app.dependency_overrides[require_csrf]
    assert client.post("/admin/outreach/control", data={"action": "resume"}).status_code == 403
    del app.dependency_overrides[require_admin]
    assert client.get("/admin/outreach", follow_redirects=False).status_code in (302, 303, 401, 403, 307)
