"""Events OS — Phase C (docs/os/events.md §6.3, §7, §8, §13, §14): run-of-show + post-event.

The run-of-show is the venue's daily deliverable: the assembled, minute-by-minute schedule
with GAPS FLAGGED LOUDLY. Assembly is DETERMINISTIC — no LLM (§14: a wrong carriage time is a
real liability) — and NEVER fabricates a timing/name/number.

Pins:
- assemble_run_of_show orders timings, annotates responsible stakeholders, flags each gap
  type (unassigned timing, unconfirmed supplier, missing headcount, open promise), and never
  invents a timing (empty timings → empty sheet, not phantom lines);
- assembly is deterministic (same input → same output, no LLM call);
- assemble_and_store persists the sheet + gaps, preserving the sign-off stamp across re-assembly;
- assemble_run_sheets.run() assembles due events + drafts the sign-off, deduped + draft-first;
- the assemble / get / send / PDF routes (PDF renderer mocked; CSRF + IDOR-safe);
- the post-event seed template exists.

No network: db.* + agent.* + the PDF renderer are monkeypatched; the autouse conftest guard
fails loudly on any un-stubbed live Supabase call.

Run: python -m pytest tests/test_events_runsheet.py -q
"""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import db, events_os
from app.scheduling import DIARY_TZ


def _now():
    return datetime(2026, 7, 8, 9, 0, tzinfo=DIARY_TZ)


# ══ 1. assemble_run_of_show — deterministic, ordered, annotated, gap-flagged ═══
def _event(**kw):
    base = {"id": "e1", "deal_id": "d1", "event_date": "2026-07-12",
            "guest_count": 118, "guest_count_final": True, "timings": [],
            "contingency_notes": None, "run_of_show": {}}
    base.update(kw)
    return base


def _sh(kind, role, **kw):
    base = {"id": f"{kind}-{role}", "kind": kind, "role": role, "name": None,
            "email": None, "confirmation": "confirmed", "outstanding": None}
    base.update(kw)
    return base


def test_assemble_orders_timings_by_time():
    ev = _event(timings=[{"label": "Carriages", "time": "23:30"},
                         {"label": "Arrival", "time": "13:00"},
                         {"label": "Dinner", "time": "16:00"}])
    sheet = events_os.assemble_run_of_show(ev, stakeholders=[], now=_now())
    assert [l["label"] for l in sheet["lines"]] == ["Arrival", "Dinner", "Carriages"]


def test_assemble_annotates_responsible_stakeholder_by_role_keyword():
    ev = _event(timings=[{"label": "Dinner service", "time": "16:00"},
                         {"label": "Bar", "time": "18:00"}])
    stk = [_sh("supplier", "caterer", name="Feast Co"),
           _sh("supplier", "bar", name="Vine")]
    sheet = events_os.assemble_run_of_show(ev, stakeholders=stk, now=_now())
    lines = {l["label"]: l for l in sheet["lines"]}
    assert lines["Dinner service"]["responsible"]["role"] == "caterer"   # keyword hint
    assert lines["Bar"]["responsible"]["role"] == "bar"                  # direct label match
    # both annotated → no unassigned-timing gap
    assert not any(g["kind"] == events_os.GAP_UNASSIGNED_TIMING for g in sheet["gaps"])


def test_assemble_flags_unassigned_timing_gap_and_never_invents_a_name():
    ev = _event(timings=[{"label": "Fireworks", "time": "22:00"}])   # no matching stakeholder
    sheet = events_os.assemble_run_of_show(ev, stakeholders=[], now=_now())
    line = sheet["lines"][0]
    assert line["responsible"] is None                                # NOT fabricated
    assert any(g["kind"] == events_os.GAP_UNASSIGNED_TIMING for g in sheet["gaps"])


def test_assemble_flags_unconfirmed_supplier_gap():
    ev = _event()
    stk = [_sh("supplier", "florist", confirmation="pending", name="Bloom")]
    sheet = events_os.assemble_run_of_show(ev, stakeholders=stk, now=_now())
    gaps = [g for g in sheet["gaps"] if g["kind"] == events_os.GAP_UNCONFIRMED_SUPPLIER]
    assert len(gaps) == 1 and "florist" in gaps[0]["detail"]


def test_assemble_no_gap_for_confirmed_supplier():
    ev = _event()
    stk = [_sh("supplier", "caterer", confirmation="confirmed")]
    sheet = events_os.assemble_run_of_show(ev, stakeholders=stk, now=_now())
    assert not any(g["kind"] == events_os.GAP_UNCONFIRMED_SUPPLIER for g in sheet["gaps"])


def test_assemble_flags_missing_headcount_when_not_final():
    ev = _event(guest_count=118, guest_count_final=False)
    sheet = events_os.assemble_run_of_show(ev, stakeholders=[], now=_now())
    assert any(g["kind"] == events_os.GAP_MISSING_HEADCOUNT for g in sheet["gaps"])


def test_assemble_no_headcount_gap_when_final():
    ev = _event(guest_count=118, guest_count_final=True)
    sheet = events_os.assemble_run_of_show(ev, stakeholders=[], now=_now())
    assert not any(g["kind"] == events_os.GAP_MISSING_HEADCOUNT for g in sheet["gaps"])


def test_assemble_zero_headcount_is_not_missing_only_the_final_flag_matters():
    """A real 0 (odd but legitimate) with the final flag set is NOT a missing-headcount gap —
    the gap is the absence of the FINAL flag, never a falsy count."""
    ev = _event(guest_count=0, guest_count_final=True)
    sheet = events_os.assemble_run_of_show(ev, stakeholders=[], now=_now())
    assert not any(g["kind"] == events_os.GAP_MISSING_HEADCOUNT for g in sheet["gaps"])


def test_assemble_flags_open_they_owe_promise_gap():
    ev = _event()
    promises = [{"direction": "they_owe", "description": "the drink licence", "deal_id": "d1"}]
    sheet = events_os.assemble_run_of_show(ev, stakeholders=[], open_promises=promises,
                                           now=_now())
    gaps = [g for g in sheet["gaps"] if g["kind"] == events_os.GAP_OPEN_PROMISE]
    assert len(gaps) == 1 and "drink licence" in gaps[0]["detail"]


def test_assemble_ignores_we_owe_promise():
    """A we_owe promise is OUR obligation, not a run-sheet gap — only they_owe items flag."""
    ev = _event()
    promises = [{"direction": "we_owe", "description": "the quote", "deal_id": "d1"}]
    sheet = events_os.assemble_run_of_show(ev, stakeholders=[], open_promises=promises,
                                           now=_now())
    assert not any(g["kind"] == events_os.GAP_OPEN_PROMISE for g in sheet["gaps"])


def test_assemble_empty_timings_yields_empty_sheet_not_fabricated_lines():
    """The liability guardrail (§14): given NO timings, the sheet has NO lines — never a
    single phantom row — and still flags the non-timing gaps."""
    ev = _event(timings=[], guest_count_final=False)
    sheet = events_os.assemble_run_of_show(ev, stakeholders=[], now=_now())
    assert sheet["lines"] == []                                        # not fabricated
    assert any(g["kind"] == events_os.GAP_MISSING_HEADCOUNT for g in sheet["gaps"])


def test_assemble_drops_malformed_timing_never_coerces_a_slot():
    ev = _event(timings=[{"label": "Arrival", "time": "13:00"},
                         {"label": "", "time": "14:00"},           # no label → dropped
                         {"label": "Speeches"},                    # no time → dropped
                         "not a dict"])                            # junk → dropped
    sheet = events_os.assemble_run_of_show(ev, stakeholders=[], now=_now())
    assert [l["label"] for l in sheet["lines"]] == ["Arrival"]


def test_assemble_collects_staff_call_times():
    ev = _event()
    stk = [_sh("staff", "waiting_staff", outstanding="call time", confirmation="pending"),
           _sh("supplier", "caterer")]
    sheet = events_os.assemble_run_of_show(ev, stakeholders=stk, now=_now())
    assert len(sheet["staff_call_times"]) == 1
    assert sheet["staff_call_times"][0]["role"] == "waiting_staff"


def test_assemble_carries_dietary_summary_and_contingency():
    ev = _event(contingency_notes="  Wet-weather: marquee.  ")
    sheet = events_os.assemble_run_of_show(
        ev, stakeholders=[], dietary_matrix={"vegan": 6}, now=_now())
    assert sheet["dietary_summary"] == {"vegan": 6}
    assert sheet["contingency_notes"] == "Wet-weather: marquee."


def test_assemble_is_deterministic_same_input_same_output_no_llm(monkeypatch):
    """Same input → identical sheet (minus the timestamp), and NO agent/LLM call anywhere."""
    import app.agent as agent
    monkeypatch.setattr(agent, "compose_outbound",
                        lambda *a, **k: pytest.fail("assembly must not call the LLM"))
    ev = _event(timings=[{"label": "Dinner", "time": "16:00"},
                         {"label": "Arrival", "time": "13:00"}])
    stk = [_sh("supplier", "caterer", confirmation="pending")]
    a = events_os.assemble_run_of_show(ev, stakeholders=stk, now=_now())
    b = events_os.assemble_run_of_show(ev, stakeholders=stk, now=_now())
    a.pop("assembled_at"); b.pop("assembled_at")
    assert a == b


# ══ 2. assemble_and_store — persists + preserves the sign-off stamp ════════════
def test_assemble_and_store_persists_sheet_and_gaps(monkeypatch):
    saved = {}
    monkeypatch.setattr(events_os.db, "crm_list_event_stakeholders",
                        lambda *a, **k: [_sh("supplier", "bar", confirmation="pending")])
    monkeypatch.setattr(events_os.db, "crm_event_dietary_matrix", lambda *a, **k: {})
    monkeypatch.setattr(events_os.db, "crm_list_event_guests", lambda *a, **k: [])
    monkeypatch.setattr(events_os.db, "crm_open_promises", lambda *a, **k: [])
    monkeypatch.setattr(events_os.db, "crm_update_event",
                        lambda eid, t, upd, **k: saved.update(upd) or upd)
    ev = _event(guest_count_final=False)
    out = events_os.assemble_and_store("t1", ev, now=_now())
    assert out["gaps"] == out["run_of_show"]["gaps"]
    # persisted onto run_of_show; gaps: unconfirmed bar + missing headcount
    assert "run_of_show" in saved
    kinds = {g["kind"] for g in saved["run_of_show"]["gaps"]}
    assert events_os.GAP_UNCONFIRMED_SUPPLIER in kinds
    assert events_os.GAP_MISSING_HEADCOUNT in kinds


def test_assemble_and_store_preserves_signoff_stamp(monkeypatch):
    saved = {}
    monkeypatch.setattr(events_os.db, "crm_list_event_stakeholders", lambda *a, **k: [])
    monkeypatch.setattr(events_os.db, "crm_event_dietary_matrix", lambda *a, **k: {})
    monkeypatch.setattr(events_os.db, "crm_list_event_guests", lambda *a, **k: [])
    monkeypatch.setattr(events_os.db, "crm_open_promises", lambda *a, **k: [])
    monkeypatch.setattr(events_os.db, "crm_update_event",
                        lambda eid, t, upd, **k: saved.update(upd) or upd)
    ev = _event(run_of_show={"signoff_requested_at": "2026-07-01T00:00:00+00:00"})
    events_os.assemble_and_store("t1", ev, now=_now())
    assert saved["run_of_show"]["signoff_requested_at"] == "2026-07-01T00:00:00+00:00"


# ══ 3. the sign-off draft — facts-only, draft-first, stamped ═══════════════════
class _FakeDraft:
    def __init__(self, text="Please review your run sheet."):
        self.text = text
        self.usage = {"input_tokens": 10, "output_tokens": 5}
        self.model = "claude-haiku"


def _patch_signoff_io(monkeypatch, cap, *, sent_box=None):
    from app import agent, mailbox, usage

    async def fake_compose(cfg, purpose, facts, channel="email", **kw):
        cap["purpose"] = purpose
        cap["facts"] = facts
        return _FakeDraft()

    async def fake_send(tenant_id, to, subject, text):
        if sent_box is not None:
            sent_box.append((to, subject))

    monkeypatch.setattr(agent, "compose_outbound", fake_compose)
    monkeypatch.setattr(mailbox, "send_mail", fake_send)
    monkeypatch.setattr(usage, "record_anthropic", lambda *a, **k: None)
    monkeypatch.setattr(events_os.db, "insert_email_log",
                        lambda *a, **k: (cap.setdefault("logs", []).append(k) or "log1", True))
    monkeypatch.setattr(events_os.db, "log_agent_event", lambda *a, **k: None)
    monkeypatch.setattr(events_os.db, "mark_email_log_sent", lambda *a, **k: None)
    # _stamp_signoff re-reads the event + writes the stamp.
    monkeypatch.setattr(events_os.db, "crm_get_event",
                        lambda eid, t, **k: {"id": eid, "run_of_show": {}})
    monkeypatch.setattr(events_os.db, "crm_update_event",
                        lambda eid, t, upd, **k: cap.setdefault("stamped", []).append(upd) or upd)


def test_compose_signoff_drafts_first_by_default(monkeypatch):
    import asyncio
    cap, sent = {}, []
    _patch_signoff_io(monkeypatch, cap, sent_box=sent)
    ev = _event()
    deal = {"title": "Harper Wedding", "crm_contacts": {"email": "couple@x.co"}}
    sheet = events_os.assemble_run_of_show(ev, stakeholders=[], now=_now())
    out = asyncio.run(events_os.compose_signoff("t1", {}, ev, deal, sheet, now=_now(),
                                                auto=False))
    assert out == "drafted"
    assert sent == []                                                 # nothing auto-sent
    # kind="run_sheet" on the log row + the sign-off stamp written to the sheet
    assert cap["logs"][0].get("kind") == "run_sheet"
    assert any("signoff_requested_at" in (u.get("run_of_show") or {})
               for u in cap["stamped"])


def test_compose_signoff_auto_sends_on_opt_in(monkeypatch):
    import asyncio
    cap, sent = {}, []
    _patch_signoff_io(monkeypatch, cap, sent_box=sent)
    ev = _event()
    deal = {"title": "Harper Wedding", "crm_contacts": {"email": "couple@x.co"}}
    sheet = events_os.assemble_run_of_show(ev, stakeholders=[], now=_now())
    out = asyncio.run(events_os.compose_signoff("t1", {}, ev, deal, sheet, now=_now(),
                                                auto=True))
    assert out == "sent" and sent == [("couple@x.co", events_os.signoff_subject(deal, ev))]


def test_signoff_facts_lists_gaps_and_never_invents():
    ev = _event(guest_count_final=False)
    deal = {"title": "Harper Wedding"}
    sheet = events_os.assemble_run_of_show(
        ev, stakeholders=[_sh("supplier", "bar", confirmation="pending")], now=_now())
    facts = events_os.signoff_facts(deal, ev, sheet)
    assert "Harper Wedding" in facts
    assert "resolve" in facts.lower()                                # the open items are listed
    assert "bar" in facts.lower()


# ══ 4. assemble_run_sheets.run() — assembles due, drafts sign-off, deduped ═════
def _run_job(monkeypatch, *, events, cfg, store_calls):
    import asyncio
    from app.jobs import assemble_run_sheets

    monkeypatch.setattr(assemble_run_sheets.db, "tenants_with_modules", lambda m: ["t1"])
    monkeypatch.setattr(assemble_run_sheets.db, "crm_events_between", lambda *a, **k: events)
    monkeypatch.setattr(assemble_run_sheets.db, "effective_config", lambda *a, **k: cfg)
    monkeypatch.setattr(assemble_run_sheets.db, "crm_get_deal",
                        lambda *a, **k: {"title": "Harper", "crm_contacts": {"email": "c@x.co"}})

    def fake_store(tid, event, *, now=None, client=None):
        store_calls.append(event["id"])
        # Return a sheet whose signoff stamp reflects the event's stored one (dedupe test).
        prior = (event.get("run_of_show") or {})
        sheet = {"lines": [], "gaps": [], "assembled_at": "x"}
        if prior.get("signoff_requested_at"):
            sheet["signoff_requested_at"] = prior["signoff_requested_at"]
        return {"run_of_show": sheet, "gaps": []}
    monkeypatch.setattr(assemble_run_sheets.events_os, "assemble_and_store", fake_store)
    return asyncio.run(assemble_run_sheets.run())


def test_job_assembles_and_drafts_signoff(monkeypatch):
    signoffs = []

    async def fake_signoff(tid, cfg, ev, deal, sheet, *, now, auto=False):
        signoffs.append((ev["id"], auto))
        return "sent" if auto else "drafted"
    monkeypatch.setattr("app.jobs.assemble_run_sheets.events_os.compose_signoff", fake_signoff)
    store = []
    ev = _event(id="e1", run_of_show={})
    out = _run_job(monkeypatch, events=[ev], cfg={"auto_send": True}, store_calls=store)
    assert store == ["e1"]                                            # assembled
    assert out["assembled"] == 1 and out["drafted"] == 1             # drafted (draft-first)
    assert signoffs == [("e1", False)]                               # auto False (no opt-in)


def test_job_signoff_auto_sends_only_on_opt_in(monkeypatch):
    seen = []

    async def fake_signoff(tid, cfg, ev, deal, sheet, *, now, auto=False):
        seen.append(auto)
        return "sent" if auto else "drafted"
    monkeypatch.setattr("app.jobs.assemble_run_sheets.events_os.compose_signoff", fake_signoff)
    out = _run_job(monkeypatch, events=[_event(id="e1", run_of_show={})],
                   cfg={"auto_signoff_events": True}, store_calls=[])
    assert seen == [True] and out["sent"] == 1


def test_job_dedupes_already_signed_off(monkeypatch):
    """An event whose stored sheet already carries signoff_requested_at is assembled again
    (the sheet must stay current) but NOT re-drafted."""
    monkeypatch.setattr("app.jobs.assemble_run_sheets.events_os.compose_signoff",
                        lambda *a, **k: pytest.fail("must not re-draft an already-signed-off event"))
    store = []
    ev = _event(id="e1", run_of_show={"signoff_requested_at": "2026-07-01T00:00:00+00:00"})
    out = _run_job(monkeypatch, events=[ev], cfg={}, store_calls=store)
    assert store == ["e1"]                                            # re-assembled
    assert out["assembled"] == 1 and out["drafted"] == 0             # not re-drafted


def test_job_one_bad_event_does_not_stop_run(monkeypatch):
    async def ok_signoff(*a, **k):
        return "drafted"
    monkeypatch.setattr("app.jobs.assemble_run_sheets.events_os.compose_signoff", ok_signoff)

    import asyncio
    from app.jobs import assemble_run_sheets
    monkeypatch.setattr(assemble_run_sheets.db, "tenants_with_modules", lambda m: ["t1"])
    monkeypatch.setattr(assemble_run_sheets.db, "crm_events_between",
                        lambda *a, **k: [_event(id="bad"), _event(id="good")])
    monkeypatch.setattr(assemble_run_sheets.db, "effective_config", lambda *a, **k: {})
    monkeypatch.setattr(assemble_run_sheets.db, "crm_get_deal", lambda *a, **k: {})

    def flaky(tid, event, *, now=None, client=None):
        if event["id"] == "bad":
            raise RuntimeError("assembly blew up")
        return {"run_of_show": {"gaps": [], "assembled_at": "x"}, "gaps": []}
    monkeypatch.setattr(assemble_run_sheets.events_os, "assemble_and_store", flaky)
    out = asyncio.run(assemble_run_sheets.run())
    assert out["assembled"] == 1                                      # the good one still built


# ══ 5. the run-of-show routes (CSRF, IDOR-safe; PDF renderer mocked) ═══════════
@pytest.fixture
def events_client(monkeypatch):
    import app.dashboard as dashboard
    import app.events_routes as er
    from app.auth import TenantCtx, require_onboarded
    from app.main import app
    from app.security import require_csrf
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"events"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: False)
    monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"mailbox": "a@b.co"})
    monkeypatch.setattr(er.db, "module_active", lambda *a, **k: True)
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app), er
    app.dependency_overrides.clear()


def test_assemble_route_reassembles(events_client, monkeypatch):
    client, er = events_client
    monkeypatch.setattr(er.db, "crm_get_event",
                        lambda eid, t, **k: {"id": eid, "deal_id": "d1"})
    called = {}
    monkeypatch.setattr(er.events_os, "assemble_and_store",
                        lambda tid, ev, **k: called.update(ev) or {"run_of_show": {}, "gaps": []})
    r = client.post("/app/events/e1/run-of-show/assemble", follow_redirects=False)
    assert r.status_code == 303 and "msg=assembled" in r.headers["location"]
    assert called["id"] == "e1"


def test_assemble_route_404_for_foreign_event(events_client, monkeypatch):
    client, er = events_client
    monkeypatch.setattr(er.db, "crm_get_event", lambda *a, **k: None)
    r = client.post("/app/events/nope/run-of-show/assemble", follow_redirects=False)
    assert r.status_code == 404                                       # IDOR-safe


def test_send_route_blasts_draft_first(events_client, monkeypatch):
    client, er = events_client
    monkeypatch.setattr(er.db, "crm_get_event",
                        lambda eid, t, **k: {"id": eid, "deal_id": "d1"})
    monkeypatch.setattr(er.events_os, "assemble_and_store",
                        lambda *a, **k: {"run_of_show": {"lines": []}, "gaps": []})
    monkeypatch.setattr(er.db, "crm_get_deal", lambda *a, **k: {"title": "Harper"})
    monkeypatch.setattr(er.db, "crm_list_event_stakeholders", lambda *a, **k: [])
    monkeypatch.setattr(er.db, "effective_config", lambda *a, **k: {})
    seen = {}

    async def fake_blast(tid, cfg, ev, deal, sheet, *, stakeholders, now, auto=False):
        seen["auto"] = auto
        return {"drafted": 0, "sent": 0}
    monkeypatch.setattr(er.events_os, "blast_run_sheet", fake_blast)
    r = client.post("/app/events/e1/run-of-show/send", follow_redirects=False)
    assert r.status_code == 303 and "msg=blasted" in r.headers["location"]
    assert seen["auto"] is False                                     # draft-first (§14)


def test_export_pdf_route_returns_pdf(events_client, monkeypatch):
    client, er = events_client
    import app.events_pdf as events_pdf
    monkeypatch.setattr(er.db, "crm_get_event",
                        lambda eid, t, **k: {"id": eid, "deal_id": "d1"})
    monkeypatch.setattr(er.events_os, "assemble_and_store",
                        lambda *a, **k: {"run_of_show": {"lines": [], "gaps": []}, "gaps": []})
    monkeypatch.setattr(er.db, "crm_get_deal", lambda *a, **k: {"title": "Harper"})
    monkeypatch.setattr(events_pdf, "render", lambda *a, **k: b"%PDF-fake")
    r = client.get("/app/events/e1/run-of-show/export.pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content == b"%PDF-fake"


def test_get_run_of_show_route_renders(events_client, monkeypatch):
    client, er = events_client
    monkeypatch.setattr(er.events_os, "detail_view",
                        lambda t, eid, **k: {"event": {"id": eid}, "deal": {},
                                             "guests": [], "dietary_matrix": {},
                                             "stakeholders": [], "lights": {}, "max_chases": 3,
                                             "run_of_show": {"lines": [], "gaps": []}})
    r = client.get("/app/events/e1/run-of-show")
    assert r.status_code == 200


# ══ 6. the run-sheet PDF renderer (deterministic bytes) ════════════════════════
def test_events_pdf_render_produces_bytes():
    from app import events_pdf
    ev = _event()
    sheet = events_os.assemble_run_of_show(
        ev, stakeholders=[_sh("supplier", "bar", confirmation="pending")],
        dietary_matrix={"vegan": 6}, now=_now())
    out = events_pdf.render(ev, {"title": "Harper Wedding"}, sheet)
    assert isinstance(out, bytes) and out[:4] == b"%PDF"


def test_events_pdf_render_empty_sheet_no_crash():
    from app import events_pdf
    out = events_pdf.render(_event(timings=[]), {"title": "Harper"}, {"lines": [], "gaps": []})
    assert out[:4] == b"%PDF"


# ══ 7. the board card carries the run-sheet gap count ══════════════════════════
def test_card_reports_run_sheet_gaps(monkeypatch):
    monkeypatch.setattr(events_os.db, "crm_list_event_guests", lambda *a, **k: [])
    monkeypatch.setattr(events_os.db, "crm_list_event_stakeholders", lambda *a, **k: [])
    monkeypatch.setattr(events_os.db, "crm_event_dietary_matrix", lambda *a, **k: {})
    ev = {"id": "e1", "deal_id": "d1", "crm_deals": {"title": "Harper"},
          "run_of_show": {"assembled_at": "x", "gaps": [{"kind": "x"}, {"kind": "y"}]}}
    card = events_os._card("t1", ev)
    assert card["run_sheet_assembled"] is True and card["run_sheet_gaps"] == 2


# ══ 8. the post-event sequence seed template exists ════════════════════════════
def test_post_event_sequence_seed_exists():
    from app import crm_sequences
    seqs = crm_sequences.default_sequences("events")
    assert len(seqs) == 1
    seq = seqs[0]
    assert seq["name"] == "Post-event thank-you + feedback"
    assert len(seq["steps"]) == 2                                     # 2-touch
    assert seq["steps"][0]["delay_days"] == 1                        # thank-you day+1
    assert seq["steps"][1]["delay_days"] == 3                        # feedback day+3
    assert all(s["exit_on_reply"] for s in seq["steps"])            # exit-on-reply
