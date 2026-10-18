"""After the job (docs/plan-after-job-tasks.md, Kris 2026-09-26): a service can
carry tasks the job leaves for the business, each with a deadline from the job
being done. Agnostic by rule — nothing here knows a trade.

Run: python -m pytest tests/test_after_job_tasks.py -q
"""
import asyncio
from datetime import date, datetime, timezone

from fastapi.testclient import TestClient

from app import after_job, services

SVC = {"name": "Installation", "minutes": 480, "after_job": [
    {"title": "Register the warranty", "days": 30, "unit": "calendar"},
    {"title": "Notify building control", "days": 5, "unit": "working"}]}
PLAIN = {"name": "Check-up", "minutes": 30}


def test_tasks_from_the_settings_text_and_back():
    t = after_job.clean_tasks("Send the stay report — 1 day; Register the warranty - 30 days;"
                              " Notify building control 30 working days; nonsense; Bad — 0 days")
    assert t == [{"title": "Send the stay report", "days": 1, "unit": "calendar"},
                 {"title": "Register the warranty", "days": 30, "unit": "calendar"},
                 {"title": "Notify building control", "days": 30, "unit": "working"}]
    assert after_job.as_text(t) == ("Send the stay report — 1 day; Register the warranty — 30 days; "
                                    "Notify building control — 30 working days")
    assert len(after_job.clean_tasks([{"title": f"T{i}", "days": 1} for i in range(9)])) == 5


def test_a_service_without_tasks_is_byte_identical():
    assert services.clean_service_types([PLAIN]) == [
        {"name": "Check-up", "minutes": 30, "price": None, "resource": None, "buffer": None}]
    rows = services.parse_form(["Installation", "Check-up"], [480, 30], [None, None],
                               after_job_list=["Register the warranty — 30 days", ""])
    assert rows[0]["after_job"][0]["title"] == "Register the warranty" and "after_job" not in rows[1]
    assert not after_job.tenant_uses([PLAIN]) and after_job.tenant_uses([SVC])


def test_working_days_skip_weekends_and_the_business_bank_holidays(monkeypatch):
    from app import bank_holidays
    monkeypatch.setattr(bank_holidays, "days_for", lambda nation: {date(2026, 12, 25): "Christmas Day",
                                                                   date(2026, 12, 28): "Boxing Day (sub)"})
    fri = date(2026, 12, 18)
    assert after_job.due_date(fri, 5, "working", "england-and-wales") == date(2026, 12, 29)
    assert after_job.due_date(fri, 5, "calendar") == date(2026, 12, 23)


class Rig:
    def __init__(self, monkeypatch, done_at=None):
        self.tasks, self.pushes, self.done = [], [], done_at
        from app import db, push
        monkeypatch.setattr(after_job, "_claim_done", self.claim)
        monkeypatch.setattr(after_job, "_insert_task", self.insert)
        monkeypatch.setattr(after_job, "_tasks_for_bookings",
                            lambda tid, ids: [t for t in self.tasks if t["booking_id"] in ids])
        monkeypatch.setattr(db, "crm_contact_by_email", lambda *a, **k: {"id": "c1"})
        monkeypatch.setattr(db, "bookings_for_contact",
                            lambda tid, c, **k: [{"id": "b1", "booking_ref": "BK-1"}])
        monkeypatch.setattr(push, "notify", lambda tid, **k: self.pushes.append(k))

    def claim(self, tid, bid, by, at):
        if self.done:
            return None
        self.done = at
        return {"id": bid}

    def insert(self, row):
        if any(t["booking_id"] == row["booking_id"] and t["task_key"] == row["task_key"] for t in self.tasks):
            return None
        row = {**row, "id": f"t{len(self.tasks) + 1}", "status": "open"}
        self.tasks.append(row)
        return row


BOOKING = {"id": "b1", "booking_ref": "BK-1", "contact": "sam@x.co",
           "details": {"Service": "Installation"}}


def test_marking_a_job_done_starts_its_deadlines_once(monkeypatch):
    rig = Rig(monkeypatch)
    cfg = {"service_types": [SVC, PLAIN], "bank_holiday_nation": None}
    now = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)   # a Thursday
    made = after_job.mark_done("t1", BOOKING, sched_cfg=cfg, now=now)
    assert [(t["title"], t["due_date"]) for t in made] == [
        ("Register the warranty", "2026-10-31"), ("Notify building control", "2026-10-08")]
    assert made[0]["source"] == "after_job" and made[0]["contact_id"] == "c1"
    assert after_job.mark_done("t1", BOOKING, sched_cfg=cfg, now=now) == []     # second tap
    assert len(rig.tasks) == 2
    # a service with none: the stamp, and nothing else
    rig2 = Rig(monkeypatch)
    assert after_job.mark_done("t1", {**BOOKING, "details": {"Service": "Check-up"}},
                               sched_cfg=cfg, now=now) == [] and rig2.done


def test_the_agent_hears_only_the_business_own_list(monkeypatch):
    rig = Rig(monkeypatch)
    assert after_job.context_lines("t1", "sam@x.co", [PLAIN]) == ""        # business uses none
    assert after_job.context_lines("t1", "sam@x.co", [SVC]) == ""          # customer has none
    after_job.mark_done("t1", BOOKING, sched_cfg={"service_types": [SVC]},
                        now=datetime(2026, 10, 1, 15, tzinfo=timezone.utc))
    rig.tasks[1].update(status="done", completed_at="2026-10-05T10:00:00+00:00")
    text = after_job.context_lines("t1", "sam@x.co", [SVC])
    assert "“Register the warranty” — NOT done yet — due by" in text
    assert "“Notify building control” — done" in text
    assert "Never say a task is done unless the list says done" in text


def test_today_hears_when_due_and_when_missed(monkeypatch):
    rig = Rig(monkeypatch)
    rows = [{"id": "a", "title": "Send the report", "due_date": "2026-10-03", "booking_id": "b1",
             "description": "After the job — BK-1", "reminded": None},
            {"id": "b", "title": "Book the check", "due_date": "2026-09-30", "booking_id": "b1",
             "description": "", "reminded": "soon"},
            {"id": "c", "title": "Later", "due_date": "2026-11-30", "booking_id": "b1", "reminded": None}]
    updates = []

    class Q:
        def __init__(self, data=None): self.data, self._upd = data, None
        def table(self, name): return self
        def select(self, *a): return self
        def eq(self, *a): return self
        def update(self, f): updates.append(f); return self
        def execute(self): return Q(rows)
    monkeypatch.setattr(after_job, "_db", lambda: Q())
    assert after_job.remind("t1", date(2026, 10, 1)) == 2
    titles = [p["title"] for p in rig.pushes]
    assert titles == ["Send the report — due in 2 days", "Book the check — overdue"]
    assert rig.pushes[0]["kind"] == "after_job_due" and updates == [{"reminded": "soon"}, {"reminded": "overdue"}]
    assert after_job.brief_counts("t1", date(2026, 10, 1)) == {"overdue": 1, "week": 1}


def test_shared_words_name_no_trade():
    """The hard rule (Kris 2026-09-26): the shared text an agent or a page
    shows reads right for any business."""
    import inspect
    from app import settings_recommend
    shared = (after_job.AGENT_RULE + inspect.getsource(after_job).split("# ── the job page")[0]
              + services.URGENT_PROMPT_LINE)
    for word in ("boiler", "gas", "plumb", "engineer", "cp12", "dog", "pet", "vehicle", "mot "):
        assert word not in after_job.AGENT_RULE.lower(), word
        assert word not in services.URGENT_PROMPT_LINE.lower(), word
    desc = settings_recommend.PAGES["diary_settings"]["fields"]["service_after_job"]
    assert "Service name | Task — N days; Task — N working days" in desc
    assert "exact service, task names and deadlines" in desc
    assert "never invent them" in desc
    assert shared


def test_the_job_page_and_routes(monkeypatch):
    from app import dashboard
    from app.auth import TenantCtx, require_onboarded
    from app.main import app
    from app.security import require_csrf
    rig = Rig(monkeypatch)
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None, user_id="u1")
    app.dependency_overrides[require_csrf] = lambda: None
    try:
        monkeypatch.setattr(dashboard.db, "get_booking", lambda bid, tid, client=None: dict(BOOKING))
        from app import db as _db
        monkeypatch.setattr(_db, "get_scheduling_config", lambda *a, **k: {"service_types": [SVC]})
        c = TestClient(app)
        r = c.post("/app/diary/b1/done", follow_redirects=False)
        assert r.status_code == 303 and "msg=done2" in r.headers["location"] and len(rig.tasks) == 2
        done = []
        monkeypatch.setattr(after_job, "complete", lambda tid, task_id, doc_id=None: done.append(task_id))
        r = c.post("/app/diary/b1/after-job/t1/done", follow_redirects=False)
        assert r.status_code == 303 and done == ["t1"]
        ctx = after_job.job_context("t1", {**BOOKING, "done_at": "2026-10-01T15:00:00+00:00"},
                                    {"service_types": [SVC]})
        assert ctx["after_job"]["manual"] and len(ctx["after_job"]["tasks"]) == 2
        assert after_job.job_context("t1", BOOKING, {"service_types": [PLAIN]}) == {"after_job": None}
    finally:
        app.dependency_overrides.clear()


def test_a_helper_done_starts_them_too(monkeypatch):
    from tests.test_job_sheets import put, run  # noqa: F401
    from app import db, job_sheets
    import app.job_sheet_public as jsp
    called = []
    monkeypatch.setattr(after_job, "mark_done", lambda tid, b, **k: called.append(k.get("by")) or [])
    src = open("src/app/job_sheet_public.py", encoding="utf-8").read()
    assert "after_job.mark_done(s[\"tenant_id\"], b, by=f\"helper:{name}\"" in src
    assert asyncio  # the route itself is exercised in test_job_sheets


def test_dates_are_the_business_own_and_the_card_has_its_customer(monkeypatch):
    """Rig 2026-09-27 00:45 BST: the job page said "Job done 26th Sep" (the
    UTC date), and the Today card never showed — a deck row needs a customer."""
    assert after_job.local_date("2026-09-26T23:45:00+00:00") == "2026-09-27"
    assert after_job.local_date("2026-01-10T23:45:00+00:00") == "2026-01-10"   # GMT in winter
    rig = Rig(monkeypatch)
    rows = [{"id": "a", "title": "Send the report", "due_date": "2026-10-03", "booking_id": "b1",
             "description": "", "reminded": None}]

    class Q:
        def __init__(self, data=None, table=None): self.data, self.t = data, table
        def table(self, name): return Q(None, name)
        def select(self, *a): return self
        def eq(self, *a): return self
        def in_(self, *a): return self
        def update(self, f): return self
        def execute(self):
            return Q([{"id": "b1", "contact": "Sam@X.co"}] if self.t == "bookings" else rows)
    monkeypatch.setattr(after_job, "_db", lambda: Q())
    from app import db
    monkeypatch.setattr(db, "crm_contact_by_email", lambda *a, **k: {"id": "c1", "name": "Sam Ruiz"})
    after_job.remind("t1", date(2026, 10, 1))
    assert rig.pushes[0]["contact"] == "sam@x.co" and rig.pushes[0]["contact_name"] == "Sam Ruiz"
