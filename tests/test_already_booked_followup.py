"""A follow-up that changes nothing must not re-hold or re-quote the customer's
confirmed bookings (kennels rig, Little Paws, 2026-09-20).

Live, thread seed:kennels:price: two rounds sent, two CONFIRMED stays on the
thread (BK-1003 trial night 7 Oct, BK-1004 boarding 9–12 Oct). The third
inbound was details only — "my mobile is…, we're at…, emergency contact…,
vet…, Biscuit was born on 2 May 2023. See you both on the 7th for the meet
and greet." The pipeline held BOTH stays again as BK-1018/BK-1019, drafted a
duplicate quote (£122, identical to QUO-1002), told the customer "both stays
are now booked in" with the NEW refs, and set the thread booked /
booking_pending. The CRM autofill was the only part that was right.

Three layers now say no, each on its own:
  1. scheduling.make_executor — the live-diary gate: a hold_booking for a
     span the diary already holds for this contact (proposed or confirmed)
     is refused `already_booked` with the standing reference; offered rows
     never block, a different dependant on the same dates is its own visit.
  2. agent.draft_reply — the reservation guard's exception in code: a reply
     naming only the LISTED live references is not sent back with a forced
     tool call (the forced redo is how the live run re-held).
  3. agent.build_system_prompt — a stay shows its whole span, and the
     existing_bookings block says a details-only follow-up is not a booking.

Run: python -m pytest tests/test_already_booked_followup.py -q
"""
from tests.booking_fixtures import carried_config
import asyncio
import json
from datetime import date, datetime
from types import SimpleNamespace

from app import agent, intake, pipeline, scheduling
from app.scheduling import DIARY_TZ

HANNAH = "business.blam8+hannah.price@gmail.com"
NOW = datetime(2026, 9, 20, 19, 0, tzinfo=DIARY_TZ)
STAY_CFG = carried_config({"capacity_per_window": 10, "open_time": "07:00", "close_time": "18:45",
            "working_days": "mon,tue,wed,thu,fri,sat,sun",
            "intake_configured_at": "2026-09-13T12:25:14+00:00",
            "intake_fields": [dict(intake.STAY_FIELD)], "service_types": []})
# The two confirmed stays exactly as the rig held them (grid columns included):
# 7 Oct 13:00 → 8 Oct 09:30 (block 52, 82 blocks) and 9 Oct 08:30 → 12 Oct 17:00
# (block 34, 322 blocks).
TRIAL = {"id": "cf209e58", "booking_ref": "BK-1003", "booking_date": "2026-10-07",
         "window": "day", "status": "confirmed", "summary": "Trial night — Biscuit",
         "start_block": 52, "length_blocks": 82, "contact": HANNAH}
BOARDING = {"id": "c10ffd6d", "booking_ref": "BK-1004", "booking_date": "2026-10-09",
            "window": "day", "status": "confirmed", "summary": "Boarding — Biscuit",
            "start_block": 34, "length_blocks": 322, "contact": HANNAH}
TRIAL_ARGS = {"arrival": "2026-10-07T13:00", "departure": "2026-10-08T09:30",
              "summary": "Trial night — Biscuit",
              "details": {intake.STAY_FIELD["label"]: "2026-10-08 09:30"}}
BOARDING_ARGS = {"arrival": "2026-10-09T08:30", "departure": "2026-10-12T17:00",
                 "summary": "Boarding — Biscuit",
                 "details": {intake.STAY_FIELD["label"]: "2026-10-12 17:00"}}


# ── 1. the executor's live-diary gate ────────────────────────────────────────
def _stay_exec(monkeypatch, inserted, existing=None, groups=None, **kw):
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(STAY_CFG))
    monkeypatch.setattr(scheduling.db, "list_active_bookings", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "set_booking_quote", lambda *a, **k: None)
    monkeypatch.setattr(scheduling.db, "subjects_for_bookings", lambda *a, **k: {})

    def fake_insert(tenant_id, date_iso, sb, length, summary, **k):
        row = {"id": f"new-{len(inserted) + 1}", "booking_ref": f"BK-101{8 + len(inserted)}",
               "booking_date": date_iso, "start_block": sb, "length_blocks": length,
               "summary": summary, "status": "proposed", **k}
        inserted.append(row)
        return row
    monkeypatch.setattr(scheduling.db, "insert_stay_booking", fake_insert)
    return scheduling.make_executor("t1", contact=HANNAH, stay=True,
                                    intake_fields=STAY_CFG["intake_fields"],
                                    existing_bookings=existing, groups=groups, now=NOW, **kw)


def test_a_confirmed_stay_is_never_held_again(monkeypatch):
    inserted = []
    execute, state = _stay_exec(monkeypatch, inserted, existing=[TRIAL, BOARDING])
    trial = execute("hold_booking", dict(TRIAL_ARGS))
    boarding = execute("hold_booking", dict(BOARDING_ARGS))
    assert trial["error"] == "already_booked" and trial["booking_ref"] == "BK-1003"
    assert boarding["error"] == "already_booked" and boarding["booking_ref"] == "BK-1004"
    assert boarding["dates"] == "2026-10-09 to 2026-10-12"
    assert "it is confirmed" in boarding["message"]
    assert "NEVER say it is now booked" in boarding["message"]
    assert inserted == [] and state["bookings"] == []


def test_an_overlapping_span_is_the_same_visit(monkeypatch):
    # A hold that starts inside the boarding stay (10 Oct → 14 Oct) is still that stay.
    inserted = []
    execute, _ = _stay_exec(monkeypatch, inserted, existing=[BOARDING])
    out = execute("hold_booking", {**BOARDING_ARGS, "arrival": "2026-10-10T09:00",
                                   "departure": "2026-10-14T17:00"})
    assert out["error"] == "already_booked" and out["booking_ref"] == "BK-1004"
    assert inserted == []


def test_a_separate_visit_still_holds(monkeypatch):
    inserted = []
    execute, state = _stay_exec(monkeypatch, inserted, existing=[TRIAL, BOARDING])
    out = execute("hold_booking", {**BOARDING_ARGS, "arrival": "2026-10-20T08:30",
                                   "departure": "2026-10-22T17:00",
                                   "summary": "Boarding — Biscuit, half term"})
    assert out["held"] is True and out["booking_ref"] == "BK-1018"
    assert len(inserted) == 1 and state["bookings"] == inserted


def test_a_proposed_hold_from_an_earlier_round_blocks_too(monkeypatch):
    # The customer writes again before the owner has approved the first reply:
    # the pending hold is still the visit — refused, and the wording says so.
    inserted = []
    execute, _ = _stay_exec(monkeypatch, inserted, existing=[{**TRIAL, "status": "proposed"}])
    out = execute("hold_booking", dict(TRIAL_ARGS))
    assert out["error"] == "already_booked" and out["status"] == "proposed"
    assert "awaiting the business's confirmation" in out["message"]
    assert inserted == []


def test_an_offered_slot_never_blocks_its_own_acceptance(monkeypatch):
    inserted = []
    execute, _ = _stay_exec(monkeypatch, inserted, existing=[{**TRIAL, "status": "offered"}])
    out = execute("hold_booking", dict(TRIAL_ARGS))
    assert out.get("error") != "already_booked" and out["held"] is True


def test_a_different_dependant_on_the_same_dates_is_its_own_visit(monkeypatch):
    # The two-cars rule, against the diary: Bramble's boarding over Biscuit's
    # dates is a second visit; Biscuit's again is the same one.
    groups = [{"id": "g-dog", "label": "Dog", "label_plural": "Dogs", "cardinality": "many",
               "fields": []}]
    inserted = []
    execute, _ = _stay_exec(monkeypatch, inserted, existing=[BOARDING], groups=groups)
    monkeypatch.setattr(scheduling.db, "subjects_for_bookings",
                        lambda t, ids, **k: {"c10ffd6d": [{"id": "i-1", "name": "Biscuit"}]})
    monkeypatch.setattr(scheduling.db, "crm_contact_by_email", lambda *a, **k: {"id": "c-h"})
    monkeypatch.setattr(scheduling.db, "list_requirement_instances", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "upsert_requirement_instance",
                        lambda t, g, c, name, data, meta: {"id": "i-" + name.lower()})
    monkeypatch.setattr(scheduling.db, "link_booking_subjects", lambda *a, **k: None)
    again = execute("hold_booking", {**BOARDING_ARGS,
                                     "subjects": [{"name": "Biscuit", "group": "Dog", "fields": {}}]})
    assert again["error"] == "already_booked" and again["about"] == ["Biscuit"]
    assert "It is for Biscuit." in again["message"]
    other = execute("hold_booking", {**BOARDING_ARGS, "summary": "Boarding — Bramble",
                                     "subjects": [{"name": "Bramble", "group": "Dog", "fields": {}}]})
    assert other["held"] is True
    assert len(inserted) == 1


def test_the_gate_reads_the_diary_itself_when_not_handed_the_list(monkeypatch):
    # The portal builds the executor without the pipeline's list: same answer.
    inserted = []
    monkeypatch.setattr(scheduling.db, "bookings_for_contact",
                        lambda t, c, client=None: [TRIAL] if c == HANNAH else [])
    execute, _ = _stay_exec(monkeypatch, inserted, existing=None)
    out = execute("hold_booking", dict(TRIAL_ARGS))
    assert out["error"] == "already_booked" and out["booking_ref"] == "BK-1003"


def test_a_diary_read_failure_never_blocks_the_hold(monkeypatch):
    inserted = []

    def boom(*a, **k):
        raise RuntimeError("diary down")
    monkeypatch.setattr(scheduling.db, "bookings_for_contact", boom)
    execute, _ = _stay_exec(monkeypatch, inserted, existing=None)
    out = execute("hold_booking", dict(TRIAL_ARGS))
    assert out["held"] is True


def test_windows_diary_same_date_and_window_is_the_same_visit(monkeypatch):
    cfg = carried_config({"windows": "am,pm", "open_time": "08:00", "close_time": "17:00",
           "capacity_per_window": 3, "min_notice_hours": 0,
           "working_days": "mon,tue,wed,thu,fri", "intake_fields": []})
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(cfg))
    monkeypatch.setattr(scheduling.db, "offered_bookings_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "release_offered_for_contact", lambda *a, **k: 0)
    placed = []

    def fake_place_hold(tenant_id, day, window, summary, **k):
        row = {"id": f"w-{len(placed) + 1}", "booking_ref": "BK-2001", "booking_date": str(day),
               "window": window, "summary": summary, "status": "proposed"}
        placed.append(row)
        return row, None
    monkeypatch.setattr(scheduling, "place_hold", fake_place_hold)
    existing = [{"id": "w-old", "booking_ref": "BK-1500", "booking_date": "2026-09-22",
                 "window": "am", "status": "confirmed", "summary": "Boiler service"}]
    execute, _ = scheduling.make_executor("t1", contact="kris@x.co", now=NOW,
                                          existing_bookings=existing)
    same = execute("hold_booking", {"date": "2026-09-22", "window": "am", "summary": "Boiler"})
    assert same["error"] == "already_booked" and same["booking_ref"] == "BK-1500"
    whole_day = execute("hold_booking", {"date": "2026-09-22", "window": "day", "summary": "Boiler"})
    assert whole_day["error"] == "already_booked"
    other = execute("hold_booking", {"date": "2026-09-23", "window": "am", "summary": "Boiler"})
    assert other["held"] is True and len(placed) == 1


APPT_CFG = carried_config({**STAY_CFG, "windows": "day", "slot_minutes": 60, "horizon_days": 60,
            "min_notice_hours": 0,
            "service_types": [{"name": "Meet & greet", "minutes": 30, "resource": "Meet & greet"}],
            "resource_types": [{"name": "Meet & greet", "count": 1}]})
GROOM = {"id": "a1", "booking_ref": "BK-1020", "booking_date": "2026-10-10", "window": "10:00",
         "status": "confirmed", "summary": "Meet & greet — Biscuit", "start_block": 40,
         "length_blocks": 2, "contact": HANNAH, "details": {"Service": "Meet & greet"}}


def test_an_appointment_during_a_stay_is_not_the_stay(monkeypatch):
    # A stays diary with appointment services (Kris 2026-09-13): the customer's
    # meet & greet on the 10th sits inside their 9–12 Oct boarding — neither is
    # a duplicate of the other, in either direction.
    monkeypatch.setattr(scheduling.db, "get_scheduling_config", lambda *a, **k: carried_config(APPT_CFG))
    monkeypatch.setattr(scheduling.db, "list_active_bookings", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "set_booking_quote", lambda *a, **k: None)
    monkeypatch.setattr(scheduling.db, "subjects_for_bookings", lambda *a, **k: {})
    monkeypatch.setattr(scheduling.db, "offered_bookings_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(scheduling.db, "release_offered_for_contact", lambda *a, **k: 0)
    inserted = []

    def fake_insert(tenant_id, date_iso, sb, length, summary, **k):
        row = {"id": f"new-{len(inserted) + 1}", "booking_ref": f"BK-103{len(inserted)}",
               "booking_date": date_iso, "start_block": sb, "length_blocks": length,
               "summary": summary, "status": "proposed", **k}
        inserted.append(row)
        return row
    monkeypatch.setattr(scheduling.db, "insert_stay_booking", fake_insert)

    def fresh():   # one executor per email — the in-memory one-hold rule is per email
        return scheduling.make_executor(
            "t1", contact=HANNAH, stay=True, intake_fields=APPT_CFG["intake_fields"],
            service_types=APPT_CFG["service_types"], now=NOW,
            existing_bookings=[BOARDING, GROOM])[0]
    # The meet & greet on a day of the stay: its own booking.
    appt = fresh()("hold_appointment", {"date": "2026-10-10", "window": "11:00",
                                        "service": "Meet & greet", "summary": "Meet & greet",
                                        "details": {intake.STAY_FIELD["label"]: "2026-10-10 11:30"}})
    assert appt.get("error") != "already_booked", appt
    assert appt["held"] is True
    # The same meet & greet again IS the one they have.
    again = fresh()("hold_appointment", {"date": "2026-10-10", "window": "10:00",
                                         "service": "Meet & greet", "summary": "Meet & greet",
                                         "details": {intake.STAY_FIELD["label"]: "2026-10-10 10:30"}})
    assert again["error"] == "already_booked" and again["booking_ref"] == "BK-1020"
    # And the stay itself is still refused for its own dates, appointment or no.
    stay_again = fresh()("hold_booking", dict(BOARDING_ARGS))
    assert stay_again["error"] == "already_booked" and stay_again["booking_ref"] == "BK-1004"
    assert len(inserted) == 1


def test_a_stay_over_an_existing_appointment_still_holds(monkeypatch):
    inserted = []
    execute, _ = _stay_exec(monkeypatch, inserted, existing=[GROOM])
    out = execute("hold_booking", dict(BOARDING_ARGS))
    assert out.get("error") != "already_booked", out
    assert out["held"] is True and len(inserted) == 1


# ── 2. the reservation guard's exception, in code ────────────────────────────
def _msg(envelope: dict):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(envelope))])


def test_a_reply_naming_only_listed_bookings_is_backed():
    m = _msg({"reply": "Both stays are confirmed — BK-1003 for the trial night on the 7th and "
                       "BK-1004 for 9–12 October. Drop-off 08:30, collection by 17:00.",
              "action_summary": "Noted contact details; both stays already confirmed",
              "thread_state": "booked"})
    assert agent._unreserved_booking_claim(m) is True          # it would have fired…
    assert agent._claim_is_backed(m, [TRIAL, BOARDING]) is True  # …but the diary backs it


def test_a_new_reference_beside_a_listed_one_is_not_backed():
    m = _msg({"reply": "BK-1003 stands, and I've pencilled the second dog in as BK-1018.",
              "action_summary": "Held a second stay", "thread_state": "booked"})
    assert agent._claim_is_backed(m, [TRIAL, BOARDING]) is False


def test_offered_slots_and_empty_lists_never_back_a_claim():
    m = _msg({"reply": "BK-1003 is yours.", "action_summary": "", "thread_state": "booked"})
    assert agent._claim_is_backed(m, [{**TRIAL, "status": "offered"}]) is False
    assert agent._claim_is_backed(m, []) is False
    assert agent._claim_is_backed(m, None) is False


def test_the_diary_saying_already_booked_backs_a_ref_less_reply():
    m = _msg({"reply": "Both stays are booked in for 7 Oct and 9–12 Oct — see you at 13:00 "
                       "on the 7th and 08:30 on the 9th.",
              "action_summary": "Confirmed both stays", "thread_state": "booked"})
    assert agent._claim_is_backed(m, [TRIAL, BOARDING]) is False
    assert agent._claim_is_backed(m, [TRIAL, BOARDING], diary_backed=True) is True
    ghost = _msg({"reply": "All booked — BK-1018.", "action_summary": "", "thread_state": "booked"})
    assert agent._claim_is_backed(ghost, [TRIAL, BOARDING], diary_backed=True) is False


# ── 3. the prompt: a stay's whole span, and the details-only rule ────────────
def test_prompt_lists_a_stay_with_its_whole_span_and_the_details_rule():
    text = agent.build_system_prompt({"business_name": "Little Paws"}, scheduling=True,
                                     today=date(2026, 9, 20), cancel_tool=True,
                                     existing_bookings=[TRIAL, BOARDING])
    assert "- BK-1004: Boarding — Biscuit on 2026-10-09 to 2026-10-12 — confirmed" in text
    assert "- BK-1003: Trial night — Biscuit on 2026-10-07 to 2026-10-08 — confirmed" in text
    assert "only adds details" in text and "call no booking tool" in text
    assert "use move_booking / cancel_booking" in text
    assert "you cannot edit bookings yet" not in text


def test_prompt_without_the_diary_tools_keeps_the_colleague_line():
    text = agent.build_system_prompt({"business_name": "Little Paws"}, cancel_tool=False,
                                     existing_bookings=[{**TRIAL, "start_block": None,
                                                         "length_blocks": None}])
    assert "- BK-1003: Trial night — Biscuit on 2026-10-07 — confirmed" in text
    assert "you cannot edit bookings yet" in text


# ── 4. the whole pipeline, on the live round ─────────────────────────────────
DETAILS_EMAIL = {
    "from_address": HANNAH, "from_name": "Hannah Price", "thread_id": "seed:kennels:price",
    "subject": "Re: Biscuit — trial night and October boarding",
    "body": ("Hi both,\n\nAs promised: my mobile is 07700 900123, we're at 14 Orchard Close, "
             "Frome BA11 2QT. Emergency contact is my sister Claire on 07700 900456, and "
             "Biscuit's vet is Frome Vets on Wallbridge. Biscuit was born on 2 May 2023.\n\n"
             "See you both on the 7th for the meet and greet.\n\nHannah"),
}
PRIOR_ROUNDS = [
    {"id": "r1", "from_address": HANNAH, "subject": "Biscuit", "status": "sent",
     "kind": "inbound", "body_in": "Could Biscuit come for a trial night then three nights in October?",
     "draft_reply": "Yes — the trial night is £30.50 and 9–12 Oct is £91.50. Could you send "
                    "vaccination expiry and drop-off/collection times?",
     "created_at": "2026-09-20T12:29:19+00:00"},
    {"id": "r2", "from_address": HANNAH, "subject": "Re: Biscuit", "status": "sent",
     "kind": "inbound", "body_in": "Vaccinated until March 2027; drop off 1pm on the 7th, then 8.30 on the 9th "
                "and collect 5pm on the 12th.",
     "draft_reply": "Both held — BK-1003 for the trial night and BK-1004 for the 9th to the "
                    "12th. £122 in total.",
     "created_at": "2026-09-20T13:53:10+00:00"},
]


class Env:
    """draft_for_email with the REAL scheduling set-up and stay executor; only
    the database, the knowledge base and the model are stubbed. The fake model
    does what the live one did — calls hold_booking for both stays — and then
    writes whatever the tool results let it say, as a model would."""

    def __init__(self, monkeypatch):
        self.inserted, self.linked, self.events, self.quotes, self.captured = [], [], [], [], {}
        self.seen = {}
        db = pipeline.db
        mp = monkeypatch.setattr
        mp(db, "get_tenant", lambda *a, **k: {"mailbox": "hello@littlepaws.co", "industry": "kennels"})
        mp(db, "get_tenant_config", lambda *a, **k: {})
        mp(db, "get_mailbox_config", lambda *a, **k: None)
        mp(db, "is_ignored", lambda *a, **k: False)
        mp(db, "mailbox_self_addresses", lambda *a, **k: set())
        mp(db, "own_mailbox_addresses", lambda *a, **k: {"hello@littlepaws.co"})
        mp(db, "get_contact_draft_stats", lambda *a, **k: None)
        mp(db, "active_modules", lambda *a, **k: {"scheduling"})
        mp(db, "module_active", lambda *a, **k: False)
        mp(db, "get_scheduling_config", lambda *a, **k: STAY_CFG)
        mp(db, "list_requirement_groups", lambda *a, **k: [])
        mp(db, "bookings_for_contact", lambda t, c, client=None: [dict(TRIAL), dict(BOARDING)] if c == HANNAH else [])
        mp(db, "get_conversation", lambda *a, **k: [dict(r) for r in PRIOR_ROUNDS])
        mp(db, "list_portal_messages", lambda *a, **k: [])
        mp(db, "list_documents", lambda *a, **k: [])
        mp(db, "job_parties_for_bookings", lambda *a, **k: {})
        mp(db, "list_active_bookings", lambda *a, **k: [])
        mp(db, "set_booking_quote", lambda *a, **k: None)
        mp(db, "release_offered_for_contact", lambda *a, **k: 0)
        mp(db, "offered_bookings_for_contact", lambda *a, **k: [])
        mp(db, "subjects_for_bookings", lambda *a, **k: {})
        mp(db, "crm_contact_by_email", lambda *a, **k: None)
        mp(db, "insert_stay_booking", self._insert)
        mp(db, "insert_email_log", self._insert_log)
        mp(db, "link_bookings_to_log", lambda ids, log_id, t: self.linked.extend(ids))
        mp(db, "set_booking_status", lambda *a, **k: self.events.append(("cancel_one", a)))
        mp(db, "resolve_holds_for_log", lambda *a, **k: self.events.append(("resolve", a)))
        mp(db, "log_agent_event", lambda t, kind, text, **k: self.events.append((kind, text)))
        mp(db, "bookings_for_log", lambda log_id, t, client=None: [
            b for b in self.inserted if b["id"] in self.linked])
        mp(db, "quote_for_log", lambda *a, **k: None)
        mp(db, "open_quote_for_customer", lambda *a, **k: None)
        mp(db, "list_job_parties", lambda *a, **k: [])
        mp(db, "create_quote", self._create_quote)
        mp(pipeline.crm, "ingest_email_log_row",
           lambda *a, **k: {"activities": 1, "contact_id": None, "company_id": None})

        async def no_parties(*a, **k):
            return None
        mp(pipeline, "_read_parties", no_parties)

        async def fake_retrieve(*a, **k):
            return []
        mp(pipeline.kb, "retrieve", fake_retrieve)
        mp(pipeline.kb, "render_for_prompt", lambda *a: "")
        mp(pipeline.kb, "is_configured", lambda: False)
        mp(pipeline.usage, "record_anthropic", lambda *a, **k: None)
        mp(pipeline.agent, "draft_reply", self._model)

    def _insert(self, tenant_id, date_iso, sb, length, summary, **k):
        row = {"id": f"new-{len(self.inserted) + 1}",
               "booking_ref": f"BK-101{8 + len(self.inserted)}", "booking_date": date_iso,
               "start_block": sb, "length_blocks": length, "summary": summary,
               "status": "proposed", "quoted_amount": 30.5, **k}
        self.inserted.append(row)
        return row

    def _insert_log(self, *a, **k):
        self.captured.update(k)
        self.captured["draft"] = a[4]
        return "log-3", True

    def _create_quote(self, *a, **k):
        self.quotes.append((a, k))
        return {"id": "q-9", "quote_ref": "QUO-1009", "total": 122.0}

    async def _model(self, cfg, email, knowledge=None, history=None, tools=None,
                     tool_executor=None, **kw):
        self.seen = {"tools": [t["name"] for t in (tools or [])],
                     "existing": kw.get("existing_bookings"), "history": history}
        results = [tool_executor("hold_booking", dict(TRIAL_ARGS)),
                   tool_executor("hold_booking", dict(BOARDING_ARGS))]
        self.seen["results"] = results
        refs = [r.get("booking_ref") for r in results]
        if all(r.get("held") for r in results):
            # The live defect's reply, as the model wrote it from two fresh holds.
            return agent.DraftResult(
                text=(f"Thanks Hannah — all noted. Both stays are now booked in: {refs[0]} "
                      f"for the trial night and {refs[1]} for the boarding."),
                usage={"input_tokens": 1}, model="m", thread_state="booked",
                action_summary="Held both stays", category="needs_reply")
        return agent.DraftResult(
            text=(f"Thanks Hannah — your mobile, address, emergency contact and vet are on "
                  f"Biscuit's record, and his date of birth too. Both stays stand as booked: "
                  f"{refs[0]} for the trial night on the 7th and {refs[1]} for the 9th to the "
                  f"12th. See you both on the 7th."),
            usage={"input_tokens": 1}, model="m", thread_state="in_progress",
            action_summary="Noted contact details and Biscuit's date of birth; both stays "
                           "already confirmed",
            category="needs_reply")


def test_details_only_followup_holds_nothing_quotes_nothing_claims_nothing(monkeypatch):
    env = Env(monkeypatch)
    log_id = asyncio.run(pipeline.draft_for_email("t1", DETAILS_EMAIL, source_message_id="m3",
                                                  mailbox_id="mb-1"))
    assert log_id == "log-3"
    # The drafter had the diary tools AND the two confirmed stays in front of it.
    assert "hold_booking" in env.seen["tools"]
    assert [b["booking_ref"] for b in env.seen["existing"]] == ["BK-1003", "BK-1004"]
    # Both re-holds were refused with the standing references — nothing was inserted,
    # linked, or logged as a hold.
    assert [r["error"] for r in env.seen["results"]] == ["already_booked", "already_booked"]
    assert [r["booking_ref"] for r in env.seen["results"]] == ["BK-1003", "BK-1004"]
    assert env.inserted == [] and env.linked == []
    assert not [e for e in env.events if e[0] in ("office_booking", "quote_drafted", "cancel_one")]
    # No quote was drafted: nothing this round priced, so quote_autodraft had nothing.
    assert env.quotes == []
    # The reply names only the bookings that stand, and the thread is not "booked" again.
    draft = env.captured["draft"]
    assert "BK-1003" in draft and "BK-1004" in draft
    assert pipeline._unbacked_refs(draft, None, [TRIAL, BOARDING]) == []
    assert "now booked" not in draft
    assert env.captured["thread_state"] == "in_progress"
    assert env.captured["category"] == "draft_ready"
    assert env.captured["status"] == "pending"


def test_without_the_gate_the_live_defect_reproduces(monkeypatch):
    """The control: the same round with the executor's gate stubbed out lands
    exactly where the rig did — two fresh holds, a quote, 'now booked in',
    thread booked, booking_pending. Proves the test would catch a regression."""
    env = Env(monkeypatch)
    real = scheduling.make_executor

    def ungated(*a, **k):
        k.pop("existing_bookings", None)
        return real(*a, existing_bookings=[], **k)
    monkeypatch.setattr(pipeline.scheduling, "make_executor", ungated)
    asyncio.run(pipeline.draft_for_email("t1", DETAILS_EMAIL, source_message_id="m3"))
    assert [b["booking_ref"] for b in env.inserted] == ["BK-1018", "BK-1019"]
    assert env.linked == ["new-1", "new-2"]
    assert len(env.quotes) == 1
    assert "now booked in" in env.captured["draft"]
    assert env.captured["thread_state"] == "booked"
    assert env.captured["category"] == "booking_pending"
