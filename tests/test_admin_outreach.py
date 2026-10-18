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
        self.columns = "*"

    def select(self, columns="*"):
        self.columns = columns
        return self
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
        if self.action == "select":
            # Tenant outreach and scheduled-plan tests reuse this query helper;
            # egress instrumentation is opt-in on the founder fake database.
            if hasattr(self.fake, "reads"):
                self.fake.reads.append((self.table, self.columns, [r.get("id") for r in matched]))
            if self.columns != "*":
                def project(row):
                    result = {}
                    for field in self.columns.split(","):
                        if field == "subject:payload->>subject":
                            result["subject"] = (row.get("payload") or {}).get("subject")
                        else:
                            result[field] = row.get(field)
                    return result
                matched = [project(row) for row in matched]
            if hasattr(self.fake, "read_bytes"):
                self.fake.read_bytes += len(json.dumps(matched))
        return SimpleNamespace(data=copy.deepcopy(matched))


class FakeDB:
    def __init__(self, cfg, prospect):
        self.tables = {"admin_outreach_config": [{"id": 1, "config": cfg, "enabled": True, "last_result": {}}],
                       "vcrm_prospects": [prospect],
                       "email_connections": [{"id": "mailbox", "tenant_id": cfg["inbox_tenant_id"], "sync_failing_since": None}]}
        self.claims, self.claim_result = [], False
        self.reads, self.read_bytes = [], 0
    def table(self, name): return Query(self, name)
    def rpc(self, name, params):
        if name == "admin_outreach_stale_drafts":
            ids = [{"id": r["id"]} for r in self.tables.get("admin_outreach_messages", [])
                   if r["status"] in ("draft", "approved") and r.get("config_snapshot") != params["p_config"]]
            result = SimpleNamespace(order=lambda *a: result,
                                     range=lambda start, end: SimpleNamespace(execute=lambda: SimpleNamespace(data=ids[start:end+1])))
            return result
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


def inbound(cfg, prospect, **changes):
    return {"id": "reply", "tenant_id": cfg["inbox_tenant_id"], "kind": "inbound",
            "from_address": prospect["email"], "subject": "An introduction", "body_in": "Let's talk",
            "created_at": NOW.isoformat()} | changes


def test_idle_scan_does_not_download_history_bodies_or_template_copies(fake, cfg, prospect):
    # Deliberately exceed one page and use large bodies/configuration snapshots.
    fake.tables["email_log"] = [inbound(cfg, prospect, id=f"old-{i}", body_in="x" * 20000) for i in range(600)]
    fake.tables["admin_outreach_events"] = [
        {"id": f"inbox:old-{i}", "email": prospect["email"], "kind": "replied", "created_at": NOW.isoformat()}
        for i in range(600)]
    fake.tables["admin_outreach_messages"] = [
        {"id": f"sent-{i}", "email": f"p{i}@example.org", "step": 0, "status": "sent",
         "sent_at": NOW.isoformat(), "payload": {"subject": "An introduction", "text": "x" * 20000},
         "config_snapshot": {"template": "x" * 20000}} for i in range(600)]
    full_history_bytes = len(json.dumps(fake.tables["email_log"])) + len(json.dumps(fake.tables["admin_outreach_messages"]))
    outreach.sync(cfg, SimpleNamespace(list=lambda path: []), NOW)
    outreach.prepare(cfg, NOW)
    outreach.digest(cfg, NOW, {})
    assert not any(table == "email_log" and "body_in" in columns for table, columns, _ in fake.reads)
    assert not any(table == "admin_outreach_messages" and (columns == "*" or "config_snapshot" in columns or columns == "payload")
                   for table, columns, _ in fake.reads)
    assert fake.read_bytes < full_history_bytes / 100


def test_only_unseen_known_inbound_bodies_are_fetched_and_replay_is_safe(fake, cfg, prospect):
    fake.tables["email_log"] = [
        inbound(cfg, prospect, id="seen"),
        inbound(cfg, prospect, id="unknown", from_address="other@example.org"),
        inbound(cfg, prospect, id="outbound", kind="outbound"),
        inbound(cfg, prospect, id="other-tenant", tenant_id="other"),
        inbound(cfg, prospect, id="new", kind=None, from_address="OWNER@example.com")]
    fake.tables["admin_outreach_events"] = [{"id": "inbox:seen", "email": prospect["email"], "kind": "auto"}]
    provider = SimpleNamespace(list=lambda path: [])
    outreach.sync(cfg, provider, NOW)
    bodies = [ids for table, columns, ids in fake.reads if table == "email_log" and columns == "id,body_in"]
    assert bodies == [["new"]]
    assert prospect["status"] == "REPLIED"
    fake.reads.clear()
    # No process-local cursor/cache: an independent run uses durable events.
    outreach.sync(cfg, provider, NOW)
    assert not any(table == "email_log" and "body_in" in columns for table, columns, _ in fake.reads)


def test_late_import_with_old_timestamp_is_detected(fake, cfg, prospect):
    provider = SimpleNamespace(list=lambda path: [])
    outreach.sync(cfg, provider, NOW)
    fake.tables["email_log"] = [inbound(cfg, prospect, created_at="2026-10-01T00:00:00+00:00", body_in="Please stop")]
    outreach.sync(cfg, provider, NOW)
    assert prospect["status"] == "NOT_NOW"


@pytest.mark.parametrize("paused,now", [(True, NOW), (False, NOW.replace(hour=22))])
def test_paused_and_overnight_detect_stop_without_preparing_or_sending(fake, cfg, prospect, monkeypatch, paused, now):
    fake.tables["admin_outreach_config"][0]["enabled"] = not paused
    fake.tables["email_log"] = [inbound(cfg, prospect, body_in="Please stop")]
    monkeypatch.setattr(outreach, "utcnow", lambda: now)
    monkeypatch.setattr(outreach.Provider, "list", lambda *a: [])
    monkeypatch.setattr(outreach, "prepare", lambda *a: pytest.fail("Idle tick prepared drafts"))
    assert outreach.run()["blocked"] == ("Paused" if paused else "Outside working window")
    assert prospect["status"] == "NOT_NOW"
    assert not fake.claims


def test_missing_reply_body_blocks_sending_and_does_not_mark_seen(fake, cfg, prospect, monkeypatch):
    fake.tables["email_log"] = [inbound(cfg, prospect)]
    execute = Query.execute
    def missing(query):
        if query.table == "email_log" and query.columns == "id,body_in":
            return SimpleNamespace(data=[])
        return execute(query)
    monkeypatch.setattr(Query, "execute", missing)
    monkeypatch.setattr(outreach.Provider, "list", lambda *a: [])
    assert "Job failed" in outreach.run()["blocked"]
    assert not fake.claims
    assert not fake.tables.get("admin_outreach_events")


def test_auto_reply_delays_followup_and_later_stop_still_wins(fake, cfg, prospect):
    prospect.update(status="EMAILED", next_action_date="2026-10-13")
    fake.tables["email_log"] = [inbound(cfg, prospect, subject="Automatic reply", body_in="Out of office")]
    provider = SimpleNamespace(list=lambda path: [])
    outreach.sync(cfg, provider, NOW)
    assert prospect["status"] == "EMAILED"
    assert prospect["next_action_date"] == "2026-10-19"
    fake.tables["email_log"].append(inbound(cfg, prospect, id="stop", body_in="Please unsubscribe"))
    outreach.sync(cfg, provider, NOW)
    assert prospect["status"] == "NOT_NOW"


def test_historical_rendered_subject_is_available_without_full_payload(fake, cfg, prospect):
    fake.tables["admin_outreach_messages"] = [{"id": "first", "email": prospect["email"], "step": 0,
        "status": "sent", "payload": {"subject": "Earlier personalised subject", "text": "large template"}}]
    fake.tables["email_log"] = [inbound(cfg, prospect, subject="Re: Earlier personalised subject")]
    outreach.sync(cfg, SimpleNamespace(list=lambda path: []), NOW)
    assert prospect["status"] == "REPLIED"


def test_send_loads_one_payload_and_preserves_idempotent_claim(fake, cfg, monkeypatch):
    monkeypatch.setattr(outreach, "sync", lambda *a: [])
    outreach.prepare(cfg, NOW)
    message = fake.tables["admin_outreach_messages"][0]
    message["status"] = "approved"
    fake.claim_result = True
    sent = []
    monkeypatch.setattr(outreach.Provider, "send", lambda self, row: sent.append(row) or "receipt")
    result = outreach.run()
    assert result["sent"] == message["id"]
    assert len(sent) == len(fake.claims) == 1
    assert sent[0]["payload"] == message["payload"]
    assert message["status"] == "sent"
    assert [ids for table, columns, ids in fake.reads if table == "admin_outreach_messages" and columns == "id,payload,step"] == [[message["id"]]]


def test_provider_bounce_still_cancels_pending_messages(fake, cfg, prospect):
    outreach.prepare(cfg, NOW)
    provider = SimpleNamespace(
        list=lambda path: ([{"id": "sent", "last_event": "bounced", "to": [prospect["email"]]}]
                           if path == "/emails" else []))
    outreach.sync(cfg, provider, NOW)
    assert prospect["status"] == "NOT_NOW"
    assert fake.tables["admin_outreach_messages"][0]["status"] == "cancelled"


def test_provider_received_reply_still_uses_detail_and_stops_followup(fake, cfg, prospect):
    outreach.prepare(cfg, NOW)
    received = {"id": "received", "from": "OWNER@example.com", "subject": "Re: An introduction"}
    details = []
    def detail(path):
        details.append(path)
        return {"html": "<p>Interested, let's talk.</p>"}
    provider = SimpleNamespace(list=lambda path: [] if path == "/emails" else [received], get=detail)
    outreach.sync(cfg, provider, NOW)
    assert prospect["status"] == "REPLIED"
    assert fake.tables["admin_outreach_messages"][0]["status"] == "cancelled"
    outreach.sync(cfg, provider, NOW)
    assert details == ["/emails/receiving/received"]


def test_inbox_body_batches_keep_all_replies_across_pages(fake, cfg, prospect):
    fake.tables["email_log"] = [inbound(cfg, prospect, id=f"new-{i}") for i in range(501)]
    outreach.sync(cfg, SimpleNamespace(list=lambda path: []), NOW)
    fetched = [ids for table, columns, ids in fake.reads if table == "email_log" and columns == "id,body_in"]
    assert [len(ids) for ids in fetched] == [100, 100, 100, 100, 100, 1]
    assert len(fake.tables["admin_outreach_events"]) == 501


def test_stale_draft_ids_are_not_truncated_at_api_row_limit(fake, cfg, prospect):
    fake.tables["vcrm_prospects"] = [prospect | {"id": f"p{i}", "email": f"p{i}@example.org"} for i in range(1100)]
    fake.tables["admin_outreach_messages"] = [
        {"id": f"m{i}", "email": f"p{i}@example.org", "step": 0, "status": "approved",
         "created_at": NOW.isoformat(), "config_snapshot": {}} for i in range(1100)]
    outreach.prepare(cfg, NOW)
    assert all(m["status"] == "draft" and m["config_snapshot"] == cfg for m in fake.tables["admin_outreach_messages"])
