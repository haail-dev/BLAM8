"""Job parties, run #5 (2026-09-04): "Bill everything to me — Jo just needs
the times" never bound Jo. An offered visit is live for parties, the pass
reads the bookings after the draft, and the extractor sees the earlier
emails where Jo's address was given."""
import asyncio

from app import party_extract


def test_an_offered_visit_is_live_for_parties():
    assert "offered" in party_extract.LIVE_STATUSES


HISTORY = [
    {"kind": "inbound", "body_in": "Hi, we've got two holiday cottages at Thorpe with a hot tub each."},
    {"kind": "outbound", "body_in": "", "draft_reply": "Great, here's what we need…"},
    {"kind": "inbound", "body_in": "Jo from Hilltop Housekeeping (jo@example.com) does the changeovers "
                                  "and can let you in.\n\nOn Thu, someone wrote:\n> quoted stuff"},
]


def test_thread_history_carries_the_customers_earlier_emails_only():
    text = party_extract.thread_history_text(HISTORY)
    assert "jo@example.com" in text
    assert "two holiday cottages" in text
    assert "quoted stuff" not in text            # quotes stripped
    assert "here's what we need" not in text     # our own replies are not evidence
    assert party_extract.thread_history_text([]) == ""


def test_a_long_thread_still_surfaces_the_email_with_the_address():
    """Live 2026-09-04: Jo's address was fourteen customer emails back."""
    early = {"kind": "inbound", "body_in": "Jo from Hilltop Housekeeping (jo@example.com) does the changeovers."}
    filler = [{"kind": "inbound", "body_in": f"Update number {i}, nothing new to add here."} for i in range(14)]
    text = party_extract.thread_history_text([early] + filler)
    assert "jo@example.com" in text
    assert "Update number 13" in text and "Update number 12" in text   # the last two ride
    assert "Update number 3" not in text                               # the rest do not


def test_the_prompt_shows_earlier_emails_for_addresses_only():
    b = {"id": "b1", "booking_ref": "BK-1001", "summary": "Drain and test", "booking_date": "2026-09-08",
         "contact": "kris@x.co", "status": "offered"}
    prompt = party_extract.build_user_prompt([b], "Re: tubs", "Bill everything to me — Jo just needs the times.",
                                             "Kris", "kris@x.co", primary_ids=["b1"],
                                             history_text="Jo from Hilltop Housekeeping (jo@example.com) can let you in.")
    assert "Earlier emails in this thread" in prompt
    assert "jo@example.com" in prompt
    assert "THIS EMAIL'S JOB" in prompt
    assert "for a name's address or phone only" in prompt
    assert "never to add people this email does not mention" in party_extract.SYSTEM


def test_apply_binds_jo_to_the_offered_visit(monkeypatch):
    rows = []
    monkeypatch.setattr(party_extract.db, "job_parties_for_bookings", lambda t, ids, client=None: {})
    monkeypatch.setattr(party_extract.db, "upsert_job_party",
                        lambda t, bid, row, client=None: rows.append((bid, row)) or {"id": "p1", **row})
    b = {"id": "b1", "booking_ref": "BK-1001", "status": "offered", "contact": "kris@x.co"}
    tally = party_extract.apply("t1", [b], [{
        "name": "Jo", "email": "jo@example.com", "phone": "", "label": "housekeeper",
        "pays": False, "approves": False, "on_site": True, "informed": False,
        "booking_ref": "", "evidence": "Jo just needs the times.", "confidence": 0.9}],
        email_log_id="log4", primary_ids=["b1"])
    assert tally["suggested"] == 1 and tally["refs"] == ["BK-1001"]
    assert rows[0][0] == "b1" and rows[0][1]["on_site"] is True and rows[0][1]["source"] == "extracted"


def test_pipeline_reads_the_bookings_after_the_draft(monkeypatch):
    """The pre-draft snapshot said 'offered'; the draft accepted it. The pass
    must see the accepted row and treat it as this email's own job."""
    from app import agent as agent_mod, crm, db as real_db, knowledge as kbmod, pipeline, usage

    async def fake_draft(cfg, email, **kw):
        return agent_mod.DraftResult(text="All confirmed.", usage={"input_tokens": 1}, model="m")

    async def fake_retrieve(*a, **k):
        return []
    monkeypatch.setattr(pipeline.agent, "draft_reply", fake_draft)
    monkeypatch.setattr(pipeline.agent, "escalation_match", lambda *a, **k: None)
    monkeypatch.setattr(kbmod, "retrieve", fake_retrieve)
    monkeypatch.setattr(kbmod, "render_for_prompt", lambda *a, **k: None)
    for name, value in (("is_ignored", False), ("mailbox_self_addresses", set()),
                        ("own_mailbox_addresses", set()), ("get_scheduling_config", None),
                        ("get_tenant", {"industry": "trades"}), ("effective_config", {}),
                        ("get_conversation", HISTORY), ("active_modules", set()),
                        ("insert_email_log", ("log-4", True))):
        monkeypatch.setattr(real_db, name, lambda *a, _v=value, **k: _v)
    offered = {"id": "b1", "booking_ref": "BK-1001", "status": "offered", "contact": "kris@x.co",
               "summary": "Drain and test", "booking_date": "2026-09-08"}
    accepted = {**offered, "status": "proposed"}
    reads = {"n": 0}

    def bookings_for_contact(*a, **k):
        reads["n"] += 1
        return [offered] if reads["n"] == 1 else [accepted]   # before the draft, then after
    monkeypatch.setattr(real_db, "bookings_for_contact", bookings_for_contact)
    monkeypatch.setattr(real_db, "bookings_for_log", lambda l, t: [])   # the hold belongs to the earlier email
    monkeypatch.setattr(usage, "record_anthropic", lambda *a, **k: None)
    monkeypatch.setattr(crm, "ingest_email_log_row", lambda *a, **k: {"contact_id": None, "company_id": None})
    monkeypatch.setattr(real_db, "log_agent_event", lambda *a, **k: None)
    calls = {}

    async def fake_extract(t, jobs, **kw):
        calls["jobs"] = jobs; calls["history"] = kw.get("history_text"); calls["primary"] = kw.get("primary_ids")
        return []
    monkeypatch.setattr(party_extract, "extract", fake_extract)
    monkeypatch.setattr(party_extract, "apply", lambda *a, **k: {"suggested": 0, "money_claims": 0, "refs": []})
    asyncio.run(pipeline.draft_for_email(
        "t1", {"from_address": "kris@x.co", "subject": "Re: tubs",
               "body": "Bill everything to me — Jo just needs the times."}, source_message_id="m4"))
    assert calls["jobs"] and calls["jobs"][0]["status"] == "proposed"
    assert "jo@example.com" in calls["history"]


def test_a_redraft_reads_the_parties_too(monkeypatch):
    """Live 2026-09-04: the email naming Jo was filed by draft suppression and
    redrafted from the thread — the party pass must run on that path."""
    from app import pipeline, party_extract
    from types import SimpleNamespace
    seen = {}

    async def fake_read(tenant_id, log_id, email, from_address, existing_bookings, sched_state, history, mailbox_id):
        seen.update(log_id=log_id, from_address=from_address, history=history)
    monkeypatch.setattr(pipeline, "_read_parties", fake_read)
    entry = {"id": "log4", "kind": "inbound", "from_address": "kris@x.co", "subject": "s", "body_in": "Jo lets you in.",
             "status": "pending", "mailbox_id": None, "thread_id": "t"}
    monkeypatch.setattr(pipeline.db, "get_email_log_entry", lambda l, t, client=None: entry)
    monkeypatch.setattr(pipeline.db, "effective_config", lambda t, m, client=None: {})
    monkeypatch.setattr(pipeline.db, "get_scheduling_config", lambda *a, **k: {})
    monkeypatch.setattr(pipeline.db, "get_tenant", lambda t, client=None: {"industry": "trades"})
    thread = [{"id": f"h{i}", **row} for i, row in enumerate(HISTORY)]
    monkeypatch.setattr(pipeline.db, "get_conversation", lambda *a, **k: thread)
    monkeypatch.setattr(pipeline.db, "active_modules", lambda t, client=None: set())
    monkeypatch.setattr(pipeline.db, "bookings_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(pipeline.db, "update_email_log_draft", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.db, "link_bookings_to_log", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_merge_portal_history", lambda t, f, h, client=None: h)
    monkeypatch.setattr(pipeline, "_party_context", lambda *a, **k: "")
    monkeypatch.setattr(pipeline, "_documents_context", lambda *a, **k: "")
    monkeypatch.setattr(pipeline, "_merge_handoff_usage", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_unbacked_refs", lambda *a, **k: [])
    monkeypatch.setattr(pipeline, "_draft_sources", lambda r: [])
    monkeypatch.setattr(pipeline, "_trace_kb_retrieval", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_log_agent_activity", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_scheduling_setup",
                        lambda *a, **k: (None, None, None, "", {"scheduling": False, "transport": False}))
    monkeypatch.setattr(pipeline.kb, "retrieve", _async([]))
    monkeypatch.setattr(pipeline.kb, "render_for_prompt", lambda r: "")
    monkeypatch.setattr(pipeline.kb, "is_configured", lambda: False)
    monkeypatch.setattr(pipeline.events, "drafting_step", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.trace, "emit", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.usage, "record_anthropic", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.agent, "escalation_match", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.agent, "strip_quoted", lambda b: b)
    monkeypatch.setattr(pipeline.textfmt, "to_plain_text", lambda t: t)
    monkeypatch.setattr(pipeline.crm, "ingest_email_log_row", lambda *a, **k: {"contact_id": None, "company_id": None})

    async def draft_reply(cfg, email, **kw):
        return SimpleNamespace(text="Reply.", missing=None, deferred=False, thread_state="booked",
                               request_summary="", action_summary="", contact_name=None, contact_company=None,
                               usage=None, searched_for=[], booking_details=[], category_reason=None,
                               reasoning=None, request_id=None, category="draft_ready", model="m")
    monkeypatch.setattr(pipeline.agent, "draft_reply", draft_reply)
    asyncio.run(pipeline.redraft_email_log("t1", "log4"))
    assert seen["log_id"] == "log4" and seen["from_address"] == "kris@x.co"
    assert seen["history"] == thread


def _async(value):
    async def f(*a, **k):
        return value
    return f


def test_the_sender_is_never_bound_as_a_party_of_their_own_booking(monkeypatch):
    rows = []
    monkeypatch.setattr(party_extract.db, "job_parties_for_bookings", lambda t, ids, client=None: {})
    monkeypatch.setattr(party_extract.db, "upsert_job_party",
                        lambda t, bid, row, client=None: rows.append(row) or {"id": "p", **row})
    b = {"id": "b1", "booking_ref": "BK-1001", "status": "confirmed"}      # no contact on the row
    tally = party_extract.apply("t1", [b], [
        {"name": "Kris", "email": "kris.deane93@gmail.com", "phone": "", "label": "invoice recipient",
         "pays": True, "approves": False, "on_site": False, "informed": False,
         "booking_ref": "BK-1001", "evidence": "All the invoices come to me.", "confidence": 1.0},
        {"name": "Jo", "email": "jo@example.com", "phone": "", "label": "site contact",
         "pays": False, "approves": False, "on_site": True, "informed": False,
         "booking_ref": "BK-1001", "evidence": "Jo will be there to let you in.", "confidence": 1.0},
    ], email_log_id="log", primary_ids=["b1"], sender="Kris.Deane93@gmail.com")
    assert tally["suggested"] == 1 and rows[0]["email"] == "jo@example.com"


def test_an_unreferenced_on_site_claim_binds_to_the_jobs_on_the_day_it_names(monkeypatch):
    """Live 2026-09-04 18:12: "Jo will be there… on Tuesday", two visits that
    Tuesday, a third in October — Jo belongs to the two, not dropped."""
    rows = []
    monkeypatch.setattr(party_extract.db, "job_parties_for_bookings", lambda t, ids, client=None: {})
    monkeypatch.setattr(party_extract.db, "upsert_job_party",
                        lambda t, bid, row, client=None: rows.append((bid, row["email"])) or {"id": bid + "-p", **row})
    live = [{"id": "b1", "booking_ref": "BK-1001", "status": "confirmed", "booking_date": "2026-09-08", "contact": "kris@x.co"},
            {"id": "b2", "booking_ref": "BK-1002", "status": "confirmed", "booking_date": "2026-09-08", "contact": "kris@x.co"},
            {"id": "b3", "booking_ref": "BK-1003", "status": "confirmed", "booking_date": "2026-10-06", "contact": "kris@x.co"}]
    claim = {"name": "Jo", "email": "jo@example.com", "phone": "", "label": "Hilltop Housekeeping",
             "pays": False, "approves": False, "on_site": True, "informed": False, "booking_ref": "",
             "evidence": "Just to be clear for Tuesday: Jo will be there to let you in at both cottages.", "confidence": 1.0}
    tally = party_extract.apply("t1", live, [claim], email_log_id="log", primary_ids=[], sender="kris@x.co")
    assert sorted(r[0] for r in rows) == ["b1", "b2"]
    assert tally["suggested"] == 2 and tally["refs"] == ["BK-1001", "BK-1002"]


def test_an_unreferenced_payer_claim_on_a_multi_job_thread_lands_nowhere(monkeypatch):
    rows = []
    monkeypatch.setattr(party_extract.db, "job_parties_for_bookings", lambda t, ids, client=None: {})
    monkeypatch.setattr(party_extract.db, "upsert_job_party", lambda t, bid, row, client=None: rows.append(bid) or {"id": "p"})
    live = [{"id": "b1", "booking_ref": "BK-1", "status": "confirmed", "booking_date": "2026-09-08"},
            {"id": "b2", "booking_ref": "BK-2", "status": "confirmed", "booking_date": "2026-10-06"}]
    claim = {"name": "John", "email": "john@x.co", "phone": "", "label": "landlord", "pays": True,
             "approves": False, "on_site": False, "informed": False, "booking_ref": "",
             "evidence": "John pays for everything.", "confidence": 1.0}
    assert party_extract.apply("t1", live, [claim], email_log_id="log")["suggested"] == 0 and rows == []
    # an on-site claim with no day named goes to the earliest upcoming job
    onsite = {**claim, "pays": False, "on_site": True, "evidence": "John will let you in."}
    party_extract.apply("t1", live, [onsite], email_log_id="log")
    assert rows == ["b1"]
