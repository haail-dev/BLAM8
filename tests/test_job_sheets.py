"""Job sheets (src/app/job_sheets.py; Kris 2026-09-25: "if they never touch the
app, how do they get the info to do the job?"). A person on a job who doesn't
sign in is sent it, told when it changes, told when they're off it or it's
cancelled — and taps On my way / Done from a private link."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app import db, job_sheets, on_my_way, push
from app.main import app

T = "t1"
TEAM = [
    {"id": "p-dan", "name": "Dan Hartwell", "role": "owner", "user_id": "u-dan", "active": True},
    {"id": "p-jake", "name": "Jake Morris", "role": "staff", "user_id": None, "active": True,
     "phone": "07700 900123", "email": "jake@example.com"},
    {"id": "p-quiet", "name": "Sam", "role": "staff", "user_id": None, "active": True},
]
JOB = {"id": "b1", "tenant_id": T, "booking_ref": "BK-1003", "status": "confirmed",
       "summary": "Emergency callout — no hot water", "booking_date": "2099-09-28",
       "start_block": 32, "length_blocks": 8, "window": "am", "contact": "sarah@example.com",
       "details": {"Service": "Emergency callout", "Job address": "14 Sydney Buildings, Bath",
                   "Boiler": "Worcester Bosch, EA code", "Phone": "07700 900999"}}


@pytest.fixture
def rig(monkeypatch):
    from app import security
    monkeypatch.setattr(security, "_BUCKETS", {})
    state = {"on": [], "sheets": [], "sent": [], "sms_live": False, "booking": dict(JOB),
             "events": [], "pushes": []}
    monkeypatch.setattr(db, "list_team_people", lambda tid, client=None: [dict(r) for r in TEAM])
    monkeypatch.setattr(db, "list_booking_people",
                        lambda tid, ids: [r for r in state["on"] if r["booking_id"] in ids])
    monkeypatch.setattr(db, "get_booking", lambda bid, tid, client=None:
                        dict(state["booking"]) if state["booking"] and bid == "b1" else None)
    monkeypatch.setattr(db, "list_job_parties", lambda tid, bid, client=None: [])
    monkeypatch.setattr(db, "crm_contact_by_email", lambda tid, e, client=None:
                        {"name": "Sarah Bennett", "phone": "07700 900555"})
    monkeypatch.setattr(db, "get_invoice_settings", lambda tid, client=None:
                        {"business_name": "Hartwell & Daughters"})
    monkeypatch.setattr(db, "list_job_sheets", lambda tid, ids: [dict(s) for s in state["sheets"]
                                                                if s["booking_id"] in ids])

    def insert(row):
        state["sheets"][:] = [s for s in state["sheets"]
                              if not (s["booking_id"] == row["booking_id"] and s["person_id"] == row["person_id"])]
        state["sheets"].append({**row, "id": f"s{len(state['sheets']) + 1}"})
        return row

    def update(sid, fields):
        for s in state["sheets"]:
            if s["id"] == sid:
                s.update(fields)
    def claim(sid, col, at):
        for s in state["sheets"]:
            if s["id"] == sid and not s.get(col):
                s[col] = at
                return True
        return False
    monkeypatch.setattr(db, "claim_job_sheet", claim)
    monkeypatch.setattr(db, "insert_job_sheet", insert)
    monkeypatch.setattr(db, "update_job_sheet", update)
    monkeypatch.setattr(db, "get_job_sheet_by_token", lambda tok: next(
        (dict(s) for s in state["sheets"] if s["token"] == tok), None))
    monkeypatch.setattr(db, "log_agent_event", lambda *a, **k: state["events"].append((a, k)))
    monkeypatch.setattr(job_sheets, "_sms_live", lambda tid: state["sms_live"])

    async def fake_deliver(tid, person, msg, mailbox_id=None):
        if state["sms_live"] and person.get("phone"):
            state["sent"].append(("sms", person["name"], msg))
            return ("sms", "+447700900123")
        if person.get("email"):
            state["sent"].append(("email", person["name"], msg))
            return ("email", person["email"])
        return None
    monkeypatch.setattr(job_sheets, "deliver", fake_deliver)
    monkeypatch.setattr(push, "notify", lambda tid, **k: state["pushes"].append(k))
    monkeypatch.setattr(push, "_prefs", lambda *a: {})
    monkeypatch.setattr(push, "_deliver", lambda t, payload, kind, cfg: state["pushes"].append({**payload, "kind": kind}))
    return state


def put(state, pid):
    state["on"].append({"booking_id": "b1", "person_id": pid, "created_at": str(len(state["on"]))})


def run(coro):
    return asyncio.run(coro)


def test_the_sheet_says_when_where_who_and_links_the_job():
    f = {"ref": "BK-1003", "what": "Emergency callout", "when": "Mon 28 Sep, 08:00–10:00",
         "where": "14 Sydney Buildings, Bath", "who": {"name": "Sarah Bennett", "phone": "07700 900555"},
         "notes": ["Boiler: Worcester Bosch, EA code"]}
    m = job_sheets.compose(job_sheets.NEW, "Hartwell & Daughters", {"name": "Jake Morris"}, f,
                           "https://blam8.com/j/abc")
    assert m["sms"].startswith("Hartwell & Daughters: job for you, Jake.")
    assert "Mon 28 Sep, 08:00–10:00" in m["sms"] and "14 Sydney Buildings" in m["sms"]
    assert m["sms"].endswith("https://blam8.com/j/abc")
    assert "Sarah Bennett · 07700 900555" in m["email"] and "Worcester Bosch" in m["email"]
    assert m["subject"] == "New job: Mon 28 Sep, 08:00–10:00 — Emergency callout (BK-1003)"


def test_the_facts_come_from_the_booking():
    assert job_sheets.when_text(JOB) == "Mon 28 Sep, 08:00–10:00"
    assert job_sheets.when_text({**JOB, "start_block": None}) == "Mon 28 Sep, morning"
    assert job_sheets.where_text(JOB) == "14 Sydney Buildings, Bath"
    assert job_sheets.what_text(JOB) == "Emergency callout"
    # the customer's phone sits with who's there, not in the notes
    assert job_sheets.notes(JOB) == ["Boiler: Worcester Bosch, EA code"]


def test_someone_without_a_login_is_sent_the_job_once(rig):
    put(rig, "p-jake")
    assert run(job_sheets.reconcile(T, "b1")) == ["new:Jake Morris"]
    assert rig["sent"][0][0] == "email"            # no SMS number yet → email
    assert run(job_sheets.reconcile(T, "b1")) == []   # nothing changed, nothing sent
    assert len(rig["sent"]) == 1
    assert rig["sheets"][0]["token"] and rig["sheets"][0]["sent_at"]


def test_a_text_when_the_business_has_sms(rig):
    rig["sms_live"] = True
    put(rig, "p-jake")
    run(job_sheets.reconcile(T, "b1"))
    assert rig["sent"][0][0] == "sms"


def test_a_login_or_no_way_to_reach_them_sends_nothing(rig):
    put(rig, "p-dan")      # signs in: it's on their Today already
    put(rig, "p-quiet")    # no mobile, no email
    assert run(job_sheets.reconcile(T, "b1")) == []
    lines = job_sheets.status_lines(T, JOB, [{"person_id": "p-quiet", "has_login": False},
                                             {"person_id": "p-dan", "has_login": True}])
    assert "add one in People" in lines["p-quiet"] and "p-dan" not in lines


def test_an_offer_waits_until_it_is_confirmed(rig):
    rig["booking"]["status"] = "offered"
    put(rig, "p-jake")
    assert run(job_sheets.reconcile(T, "b1")) == []
    rig["booking"]["status"] = "confirmed"
    assert run(job_sheets.reconcile(T, "b1")) == ["new:Jake Morris"]


def test_a_move_sends_the_new_details(rig):
    put(rig, "p-jake")
    run(job_sheets.reconcile(T, "b1"))
    rig["booking"]["start_block"] = 52
    assert run(job_sheets.reconcile(T, "b1")) == ["changed:Jake Morris"]
    assert "13:00" in rig["sent"][-1][2]["sms"] and "CHANGED" in rig["sent"][-1][2]["sms"]
    assert run(job_sheets.reconcile(T, "b1")) == []


def test_taken_off_or_cancelled_they_are_told_and_the_link_closes(rig):
    put(rig, "p-jake")
    run(job_sheets.reconcile(T, "b1"))
    rig["on"].clear()
    assert run(job_sheets.reconcile(T, "b1")) == ["off:Jake Morris"]
    assert rig["sheets"][0]["closed_at"]
    put(rig, "p-jake")                    # back on: a fresh sheet, a fresh link
    assert run(job_sheets.reconcile(T, "b1")) == ["new:Jake Morris"]
    rig["booking"]["status"] = "cancelled"
    assert run(job_sheets.reconcile(T, "b1")) == ["cancelled:Jake Morris"]
    assert "CANCELLED" in rig["sent"][-1][2]["sms"]


def test_the_link_shows_the_job_and_the_taps_tell_the_office(rig, monkeypatch):
    put(rig, "p-jake")
    run(job_sheets.reconcile(T, "b1"))
    token = rig["sheets"][0]["token"]
    monkeypatch.setattr(db, "get_scheduling_config", lambda tid, client=None: {
        "intake_fields": [{"label": "Job address", "required": True}]})
    from tests.test_on_my_way import _env
    sent, logged = _env(monkeypatch, parties=[], booking=rig["booking"])
    monkeypatch.setattr(db, "log_agent_event", lambda *a, **k: rig["events"].append((a, k)))
    c = TestClient(app)
    r = c.get(f"/j/{token}")
    assert r.status_code == 200
    assert "14 Sydney Buildings, Bath" in r.text and "tel:07700900555" in r.text
    assert "google.com/maps" in r.text and "noindex" in r.text
    assert rig["sheets"][0]["opened_at"]
    r = c.post(f"/j/{token}/on-my-way", data={"eta": "30"}, follow_redirects=False)
    assert r.status_code == 303 and len(sent["deliveries"]) == 1
    assert "Jake Morris is the one coming" in sent["facts"]
    assert rig["sheets"][0]["on_my_way_at"]
    assert rig["pushes"][-1]["kind"] == "team_on_way" and "Jake Morris" in rig["pushes"][-1]["title"]
    c.post(f"/j/{token}/done", follow_redirects=False)
    assert rig["sheets"][0]["done_at"] and rig["pushes"][-1]["kind"] == "team_job_done"
    lines = job_sheets.status_lines(T, JOB, [{"person_id": "p-jake", "has_login": False}])
    assert "by email" in lines["p-jake"] and "on the way" in lines["p-jake"] and "done" in lines["p-jake"]


def test_a_closed_or_unknown_link_shows_nothing_of_the_job(rig):
    put(rig, "p-jake")
    run(job_sheets.reconcile(T, "b1"))
    token = rig["sheets"][0]["token"]
    rig["booking"]["status"] = "cancelled"
    c = TestClient(app)
    r = c.get(f"/j/{token}")
    assert r.status_code == 404 and "Sydney" not in r.text
    assert c.get("/j/nope").status_code == 404


def test_an_arrival_window_reads_as_the_window_and_a_rewording_is_not_a_change(rig, monkeypatch):
    """Kris 2026-09-26: BK-1003's sheet said 08:00–10:00; Sarah was told
    morning, 08:00–12:00."""
    cfg = {"working_days": "mon,tue,wed,thu,fri,sat", "open_time": "08:00:00", "close_time": "18:00:00"}
    assert job_sheets.when_text(JOB, cfg) == "Mon 28 Sep, morning (arrive 08:00–12:00)"
    put(rig, "p-jake")
    run(job_sheets.reconcile(T, "b1"))                      # sent with no diary config
    monkeypatch.setattr(db, "get_scheduling_config", lambda tid, client=None: cfg)
    assert run(job_sheets.reconcile(T, "b1")) == []          # better words, same slot: no CHANGED


def test_a_second_tap_sends_nothing(rig, monkeypatch):
    """Kris 2026-09-26: six taps on 30m sent Sarah six emails."""
    put(rig, "p-jake")
    run(job_sheets.reconcile(T, "b1"))
    token = rig["sheets"][0]["token"]

    monkeypatch.setattr(db, "get_scheduling_config", lambda tid, client=None: {
        "intake_fields": [{"label": "Job address", "required": True}]})
    from tests.test_on_my_way import _env
    sent, logged = _env(monkeypatch, parties=[], booking=rig["booking"])
    monkeypatch.setattr(db, "log_agent_event", lambda *a, **k: rig["events"].append((a, k)))
    c = TestClient(app)
    first = c.post(f"/j/{token}/on-my-way", data={"eta": "30"}, follow_redirects=False)
    again = c.post(f"/j/{token}/on-my-way", data={"eta": "45"}, follow_redirects=False)
    assert len(sent["deliveries"]) == 1 and "omw_already" in again.headers["location"] and first.status_code == 303
    assert len([p for p in rig["pushes"] if p["kind"] == "team_on_way"]) == 1
    c.post(f"/j/{token}/done", follow_redirects=False)
    c.post(f"/j/{token}/done", follow_redirects=False)
    assert len([p for p in rig["pushes"] if p["kind"] == "team_job_done"]) == 1
    page = c.get(f"/j/{token}?msg=omw_already").text
    assert "already on the way" in page and "Telling them" in page   # the one-tap script ships


def test_unknown_delivery_never_says_customer_knows_or_offers_correction(rig, monkeypatch):
    from tests.test_on_my_way import _env
    put(rig, "p-jake")
    run(job_sheets.reconcile(T, "b1"))
    token = rig["sheets"][0]["token"]
    sent, _ = _env(monkeypatch, parties=[], booking=rig["booking"])
    async def timeout(*a, **k):
        sent["deliveries"].append("accepted then timeout")
        raise TimeoutError()
    monkeypatch.setattr(on_my_way.mailbox, "send_mail", timeout)
    c = TestClient(app)
    first = c.post(f"/j/{token}/on-my-way", data={"eta":"30"}, follow_redirects=False)
    assert "omw_unknown" in first.headers["location"]
    again = c.post(f"/j/{token}/on-my-way", data={"eta":"45"}, follow_redirects=False)
    assert "omw_unknown" in again.headers["location"]
    assert sent["deliveries"] == ["accepted then timeout"]
    uncertain = sent["claims"][(T,"b1","initial")]
    c.post(f"/j/{token}/on-my-way", data={"eta":"45", "resend_attempt":uncertain["attempt"]}, follow_redirects=False)
    assert sent["deliveries"] == ["accepted then timeout"]
    page = c.get(f"/j/{token}").text
    assert "couldn't confirm the customer got it" in page and "Send ETA correction" not in page
    assert "The office has been told" in page and "Send again" not in page
    assert 'name="resend_attempt"' not in page
    assert "customer know" not in page
    assert len(rig["pushes"]) == 1 and "delivery is unknown" in rig["pushes"][0]["body"]
    assert not rig["sheets"][0].get("on_my_way_at")


def test_prepare_failure_can_retry_and_sent_page_separates_correction(rig, monkeypatch):
    from tests.test_on_my_way import _env
    put(rig, "p-jake")
    run(job_sheets.reconcile(T,"b1"))
    token=rig["sheets"][0]["token"]
    sent,_=_env(monkeypatch,parties=[],booking=rig["booking"])
    original=on_my_way.agent.compose_outbound
    async def failed(*a,**k):
        raise RuntimeError("composer unavailable")
    monkeypatch.setattr(on_my_way.agent,"compose_outbound",failed)
    c=TestClient(app)
    assert "pre_send_failure" in c.post(f"/j/{token}/on-my-way",data={"eta":"30"},follow_redirects=False).headers["location"]
    assert not sent["deliveries"] and not rig["sheets"][0].get("on_my_way_at")
    monkeypatch.setattr(on_my_way.agent,"compose_outbound",original)
    assert c.post(f"/j/{token}/on-my-way",data={"eta":"30"},follow_redirects=False).status_code==303
    page=c.get(f"/j/{token}").text
    assert "On my way sent</button>" in page and "<summary>Send ETA correction</summary>" in page
    assert 'name="action_key" value="after:' in page
    assert len(sent["deliveries"])==1 and len(rig["pushes"])==1


def test_notice_crash_does_not_repeat_customer_or_push(rig, monkeypatch):
    from tests.test_on_my_way import _env
    put(rig,"p-jake")
    run(job_sheets.reconcile(T,"b1"))
    token=rig["sheets"][0]["token"]
    sent,_=_env(monkeypatch,parties=[],booking=rig["booking"])
    attempted=[]
    def crash(*a,**k):
        attempted.append(True)
        raise RuntimeError("push outcome lost")
    monkeypatch.setattr(push,"_deliver",crash)
    c=TestClient(app)
    for _ in range(2):
        assert c.post(f"/j/{token}/on-my-way",data={"eta":"30"},follow_redirects=False).status_code==303
    assert attempted == [True] and len(sent["deliveries"])==1
