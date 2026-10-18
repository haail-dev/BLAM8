"""Job parties Phase 1 (docs/modules/module-job-parties.md, src/app/job_parties.py).

Pins the spine: labels are not routing (four duty booleans do the work),
zero-config fallback to the booking's own contact, D4 (extracted rows never
resolve a duty), the informed-cc opt-in, the invoice-generate payer override,
and the new diary job page.

Run: python -m pytest tests/test_job_parties.py -q
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import invoicing, job_parties
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf

TENANT = "11111111-1111-1111-1111-111111111111"
BOOKING = {"id": "7b1e0000-0000-4000-8000-000000000001", "contact": "booker@x.co", "summary": "Rewire",
           "booking_date": "2026-09-10", "window": "am", "status": "confirmed",
           "booking_ref": "BK-77"}


# ── resolve ──────────────────────────────────────────────────────────────────
def test_no_parties_means_todays_behaviour():
    r = job_parties.resolve(BOOKING, [])
    for duty in ("pays", "approves", "on_site"):
        assert r[duty]["email"] == "booker@x.co" and r[duty]["is_fallback"]
    assert r["informed"] == []


def test_duties_route_to_their_holders_with_per_duty_fallback():
    parties = [
        {"label": "landlord", "name": "John Smith", "email": "JOHN@x.co",
         "pays": True, "source": "owner"},
        {"label": "tenant", "email": "tenant@x.co", "on_site": True,
         "source": "confirmed"},
    ]
    r = job_parties.resolve(BOOKING, parties)
    assert r["pays"]["email"] == "john@x.co" and r["pays"]["name"] == "John Smith"
    assert r["on_site"]["email"] == "tenant@x.co"
    # Nobody holds 'approves' → the booking's own contact still does.
    assert r["approves"]["email"] == "booker@x.co" and r["approves"]["is_fallback"]


def test_extracted_parties_never_resolve_a_duty():
    # D4: "invoice this other address instead" is the textbook redirection
    # scam — an unconfirmed extracted row must not take pays (or anything).
    parties = [{"label": "landlord", "email": "evil@x.co", "pays": True,
                "informed": True, "source": "extracted"}]
    r = job_parties.resolve(BOOKING, parties)
    assert r["pays"]["email"] == "booker@x.co" and r["pays"]["is_fallback"]
    assert r["informed"] == []


def test_informed_cc_is_opt_in_deduped_and_never_the_recipient():
    parties = [
        {"email": "agent@x.co", "informed": True, "source": "owner"},
        {"email": "AGENT@x.co", "informed": True, "source": "owner"},   # dupe
        {"email": "payer@x.co", "informed": True, "source": "owner"},
        {"email": "quiet@x.co", "source": "owner"},                     # not ticked
    ]
    cc = job_parties.informed_cc(BOOKING, parties, exclude="payer@x.co")
    assert cc == ["agent@x.co"]


# ── the invoice-generate payer override ──────────────────────────────────────
def test_generate_bills_the_pays_party_not_the_booker(monkeypatch):
    jobs = [dict(BOOKING, id="7b1e0000-0000-4000-8000-000000000001", engineer_id="e1", quoted_amount=100.0),
            dict(BOOKING, id="bk2", engineer_id="e1", quoted_amount=50.0,
                 contact="other@x.co")]
    monkeypatch.setattr(invoicing.db, "job_parties_for_bookings",
                        lambda t, ids, client=None: {
                            "7b1e0000-0000-4000-8000-000000000001": [{"label": "landlord", "name": "John Smith",
                                     "email": "john@x.co", "pays": True,
                                     "source": "owner"}]})
    out = invoicing._apply_party_overrides(TENANT, jobs)
    assert out[0]["contact"] == "john@x.co"
    assert out[0]["contact_name"] == "John Smith"
    assert out[1]["contact"] == "other@x.co"       # untouched
    drafts = invoicing.fsm_drafts(out, None)
    payer = next(d for d in drafts if d["customer_email"] == "john@x.co")
    assert payer["customer_name"] == "John Smith"


def test_no_party_rows_costs_nothing_and_changes_nothing(monkeypatch):
    jobs = [dict(BOOKING, id="7b1e0000-0000-4000-8000-000000000001", engineer_id="e1", quoted_amount=100.0)]
    monkeypatch.setattr(invoicing.db, "job_parties_for_bookings",
                        lambda t, ids, client=None: {})
    out = invoicing._apply_party_overrides(TENANT, jobs)
    assert out[0]["contact"] == "booker@x.co"


# ── the diary job page ───────────────────────────────────────────────────────
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"fsm"})
    monkeypatch.setattr(dashboard.db, "visible_modules", lambda *a, **k: {"fsm"})
    monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id=TENANT, client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app, follow_redirects=False)
    app.dependency_overrides.clear()


def test_job_page_states_the_routing_receipt(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_booking", lambda i, t, client=None: dict(BOOKING))
    monkeypatch.setattr(dashboard.db, "list_job_parties", lambda *a, **k: [
        {"id": "p1", "label": "landlord", "name": "John Smith",
         "email": "john@x.co", "pays": True, "approves": False,
         "on_site": False, "informed": False, "source": "owner"}])
    r = client.get("/app/diary/7b1e0000-0000-4000-8000-000000000001")
    assert r.status_code == 200
    assert "Quotes go to" in r.text
    assert "invoices to <span" in r.text and "John Smith" in r.text
    assert "landlord" in r.text and "pays" in r.text


def test_add_party_links_the_crm_and_saves(client, monkeypatch):
    saved, contacts = [], []
    monkeypatch.setattr(dashboard.db, "get_booking", lambda i, t, client=None: dict(BOOKING))
    monkeypatch.setattr(dashboard.db, "crm_get_or_create_contact",
                        lambda t, e, name=None, **k: contacts.append(e) or {"id": "c9"})
    monkeypatch.setattr(dashboard.db, "upsert_job_party",
                        lambda t, b, party, client=None: saved.append(party) or {"id": "p1"})
    r = client.post("/app/diary/7b1e0000-0000-4000-8000-000000000001/parties", data={
        "name": "John Smith", "label": "landlord", "email": "John@X.co",
        "pays": "1", "informed": "1"})
    assert r.status_code == 303 and "msg=saved" in r.headers["location"]
    assert contacts == ["john@x.co"]
    assert saved[0]["pays"] is True and saved[0]["approves"] is False
    assert saved[0]["contact_id"] == "c9"


def test_add_party_needs_an_identity(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_booking", lambda i, t, client=None: dict(BOOKING))
    r = client.post("/app/diary/7b1e0000-0000-4000-8000-000000000001/parties", data={"name": "Ghost", "label": "x"})
    assert r.status_code == 303 and "msg=noidentity" in r.headers["location"]


# ── compose-prompt party context (the doc's last Phase-1 line) ───────────────
PARTIES = [
    {"label": "landlord", "name": "John Smith", "email": "john@x.co",
     "pays": True, "approves": True, "source": "owner"},
    {"label": "tenant", "name": "Priya Nair", "phone": "07700 900456",
     "on_site": True, "source": "confirmed"},
    {"label": "letting agent", "name": "Sarah Vale",
     "email": "agent@lettings.example", "informed": True, "source": "owner"},
]


def test_prompt_context_names_each_role_and_the_sender():
    ctx = job_parties.prompt_context(
        "agent@lettings.example", [BOOKING],
        {BOOKING["id"]: PARTIES})
    assert ctx.startswith("<job_parties>") and ctx.endswith("</job_parties>")
    assert "- BK-77: John Smith (landlord — pays, approves quotes)" in ctx
    assert "Priya Nair (tenant — on site)" in ctx
    # The sender is identified in THEIR role, so the draft speaks to it.
    assert ("The person you are replying to is Sarah Vale "
            "(letting agent — kept informed) on BK-77.") in ctx
    # Context, never routing: the anti-redirection sentence rides along.
    assert "never promise to redirect" in ctx


def test_prompt_context_is_empty_when_no_parties_and_ignores_extracted():
    # The overwhelming case: no rows → "" → drafting byte-identical.
    assert job_parties.prompt_context("a@x.co", [BOOKING], {}) == ""
    assert job_parties.prompt_context("a@x.co", [], {}) == ""
    # D4 for language too: an unconfirmed "landlord pays" claim must not
    # shape money wording.
    scam = [{"label": "landlord", "email": "evil@x.co", "pays": True,
             "source": "extracted"}]
    assert job_parties.prompt_context("a@x.co", [BOOKING],
                                      {BOOKING["id"]: scam}) == ""


def test_party_context_reaches_the_system_prompt():
    from app.agent import build_system_prompt
    block = job_parties.prompt_context(
        "agent@lettings.example", [BOOKING], {BOOKING["id"]: PARTIES})
    assert block in build_system_prompt({}, party_context=block)
    assert "<job_parties>" not in build_system_prompt({})


# ── Phase 2: proactive extraction ────────────────────────────────────────────
def test_extract_skips_the_llm_entirely_without_a_live_booking(monkeypatch):
    import asyncio

    from app import party_extract

    async def boom(**kw):
        raise AssertionError("llm called with no live booking")
    monkeypatch.setattr(party_extract.llm, "create", boom)
    dead = dict(BOOKING, status="cancelled")
    assert asyncio.run(party_extract.extract(
        TENANT, [dead], subject="s", body="b")) == []
    assert asyncio.run(party_extract.extract(
        TENANT, [], subject="s", body="b")) == []


def _claim(**over):
    base = {"name": "John Smith", "email": "john@x.co", "phone": "",
            "label": "landlord", "booking_ref": "BK-77", "pays": True,
            "approves": False, "on_site": False, "informed": False,
            "evidence": "the landlord John Smith (john@x.co) pays for this",
            "confidence": 0.9}
    return {**base, **over}


def test_apply_lands_claims_as_inert_extracted_rows(monkeypatch):
    from app import party_extract
    saved = []
    monkeypatch.setattr(party_extract.db, "job_parties_for_bookings",
                        lambda t, ids, client=None: {})
    monkeypatch.setattr(party_extract.db, "upsert_job_party",
                        lambda t, b, party, client=None:
                        saved.append((b, party)) or {"id": "p1", **party})
    tally = party_extract.apply(TENANT, [BOOKING], [_claim()],
                                email_log_id="log-1")
    assert tally == {"suggested": 1, "money_claims": 1, "refs": ["BK-77"]}
    booking_id, party = saved[0]
    assert booking_id == BOOKING["id"]
    assert party["source"] == "extracted"          # inert until confirmed (D4)
    assert party["evidence"].startswith("the landlord")
    assert party["email_log_id"] == "log-1"
    # And the row still resolves NOTHING until a human confirms.
    r = job_parties.resolve(BOOKING, [{**party, "id": "p1"}])
    assert r["pays"]["is_fallback"]


def test_apply_skips_what_should_never_become_a_card(monkeypatch):
    from app import party_extract
    saved = []
    monkeypatch.setattr(party_extract.db, "job_parties_for_bookings",
                        lambda t, ids, client=None: {
                            BOOKING["id"]: [{"email": "already@x.co"}]})
    monkeypatch.setattr(party_extract.db, "upsert_job_party",
                        lambda t, b, party, client=None:
                        saved.append(party) or {"id": "px", **party})
    other = dict(BOOKING, id="bk2", booking_ref="BK-88")
    claims = [
        _claim(confidence=0.4),                          # speculation
        _claim(email="", phone=""),                      # unreachable
        _claim(email="booker@x.co"),                     # the booking's own contact
        _claim(email="already@x.co"),                    # already on the job
        _claim(email="who@x.co", booking_ref=""),        # ambiguous: 2 live jobs
    ]
    tally = party_extract.apply(TENANT, [BOOKING, other], claims)
    assert saved == [] and tally["suggested"] == 0
    # The same ambiguous claim WITH a stated ref lands.
    tally = party_extract.apply(TENANT, [BOOKING, other],
                                [_claim(email="who@x.co", booking_ref="BK-88")])
    assert tally["suggested"] == 1 and saved[0]["email"] == "who@x.co"


def test_an_unreferenced_claim_binds_to_the_email_own_job(monkeypatch):
    """Live 2026-09-04 (plumbing run): a CP12 email that HELD BK-1003 had its
    on-site tenant bound to BK-1002 — the customer's other confirmed job — because
    every live job the customer had looked equally like "the" job. The email's own
    job wins; the others still need a stated reference."""
    from app import party_extract
    saved = []
    monkeypatch.setattr(party_extract.db, "job_parties_for_bookings",
                        lambda t, ids, client=None: {})
    monkeypatch.setattr(party_extract.db, "upsert_job_party",
                        lambda t, b, party, client=None:
                        saved.append((b, party)) or {"id": "px", **party})
    older = dict(BOOKING, id="bk-older", booking_ref="BK-1002")
    mine = dict(BOOKING, id="bk-mine", booking_ref="BK-1003")
    tally = party_extract.apply(TENANT, [older, mine],
                                [_claim(email="tenant@x.co", booking_ref="")],
                                primary_ids=["bk-mine"])
    assert tally["refs"] == ["BK-1003"] and saved[0][0] == "bk-mine"
    # Without the email's own job it stays ambiguous — still no guess.
    saved.clear()
    assert party_extract.apply(TENANT, [older, mine],
                               [_claim(email="tenant@x.co", booking_ref="")],
                               primary_ids=[])["suggested"] == 0
    # A stated reference still wins over the email's own job.
    saved.clear()
    party_extract.apply(TENANT, [older, mine],
                        [_claim(email="tenant@x.co", booking_ref="BK-1002")],
                        primary_ids=["bk-mine"])
    assert saved[0][0] == "bk-older"


def test_the_prompt_marks_the_email_own_job():
    from app import party_extract
    older = dict(BOOKING, id="bk-older", booking_ref="BK-1002")
    mine = dict(BOOKING, id="bk-mine", booking_ref="BK-1003")
    prompt = party_extract.build_user_prompt([older, mine], "Re: CP12", "body",
                                             primary_ids=["bk-mine"])
    assert "BK-1003 — THIS EMAIL'S JOB" in prompt
    assert "BK-1002 — THIS EMAIL'S JOB" not in prompt
    assert "THIS EMAIL'S JOB" in party_extract.SYSTEM


def test_pipeline_reads_parties_only_on_booking_threads(monkeypatch):
    """The wiring itself (the _PipelineEnv pattern from test_crm_gating):
    an inbound on a thread with a live booking runs the extraction pass and
    logs the party_suggested event with the money warning; a thread with no
    booking never even builds the call."""
    import asyncio

    from app import agent as agent_mod
    from app import crm, db as real_db
    from app import knowledge as kbmod
    from app import party_extract, pipeline, usage

    async def fake_draft(cfg, email, **kw):
        return agent_mod.DraftResult(text="Reply.", usage={"input_tokens": 1},
                                     model="m")

    async def fake_retrieve(*a, **k):
        return []
    monkeypatch.setattr(pipeline.agent, "draft_reply", fake_draft)
    monkeypatch.setattr(pipeline.agent, "escalation_match", lambda *a, **k: None)
    monkeypatch.setattr(kbmod, "retrieve", fake_retrieve)
    monkeypatch.setattr(kbmod, "render_for_prompt", lambda *a, **k: None)
    for name, value in (("is_ignored", False), ("mailbox_self_addresses", set()),
                        ("own_mailbox_addresses", set()),
                        ("get_scheduling_config", None),
                        ("get_tenant", {"industry": "trades"}),
                        ("effective_config", {}), ("get_conversation", []),
                        ("active_modules", set()),
                        ("insert_email_log", ("log-1", True))):
        monkeypatch.setattr(real_db, name, lambda *a, _v=value, **k: _v)
    monkeypatch.setattr(real_db, "bookings_for_contact", lambda *a, **k: [])
    monkeypatch.setattr(usage, "record_anthropic", lambda *a, **k: None)
    monkeypatch.setattr(crm, "ingest_email_log_row",
                        lambda *a, **k: {"contact_id": None, "company_id": None})
    monkeypatch.setattr(real_db, "bookings_for_log",
                        lambda l, t: [dict(BOOKING)])
    events, calls = [], {}
    monkeypatch.setattr(real_db, "log_agent_event",
                        lambda t, kind, summary, **kw: events.append((kind, summary)))

    async def fake_extract(t, jobs, **kw):
        calls["extract"] = jobs
        calls["primary"] = list(kw.get("primary_ids") or [])
        return [_claim()]

    def fake_apply(t, jobs, claims, **kw):
        calls["apply"] = claims
        assert list(kw.get("primary_ids") or []) == calls["primary"]
        return {"suggested": 1, "money_claims": 1, "refs": ["BK-77"]}
    monkeypatch.setattr(party_extract, "extract", fake_extract)
    monkeypatch.setattr(party_extract, "apply", fake_apply)
    asyncio.run(pipeline.draft_for_email(
        TENANT, {"from_address": "agent@lettings.example", "subject": "Re: BK-77",
                 "body": "The landlord John Smith (john@x.co) pays."},
        source_message_id="m1"))
    assert calls["extract"][0]["id"] == BOOKING["id"]
    assert calls["apply"] == [_claim()]
    # …and both halves are told WHICH of those jobs this email itself placed.
    assert calls["primary"] == [BOOKING["id"]]
    suggested = [s for k, s in events if k == "party_suggested"]
    assert suggested and "confirm them on the job page" in suggested[0]
    assert "PAYS" in suggested[0]                     # the money warning rides
    # A thread with no booking at all: the pass never builds the call.
    calls.clear()
    monkeypatch.setattr(real_db, "bookings_for_log", lambda l, t: [])
    asyncio.run(pipeline.draft_for_email(
        TENANT, {"from_address": "a@x.co", "subject": "s", "body": "b"},
        source_message_id="m2"))
    assert "extract" not in calls


def test_confirm_route_makes_the_duty_real_and_links_the_crm(client, monkeypatch):
    events, contacts = [], []
    extracted = {"id": "p9", "email": "john@x.co", "name": "John Smith",
                 "label": "landlord", "pays": True, "approves": False,
                 "on_site": False, "informed": False, "source": "extracted"}
    monkeypatch.setattr(dashboard.db, "list_job_parties",
                        lambda *a, **k: [extracted])
    monkeypatch.setattr(dashboard.db, "get_booking",
                        lambda i, t, client=None: dict(BOOKING))
    monkeypatch.setattr(dashboard.db, "crm_get_or_create_contact",
                        lambda t, e, name=None, **k: contacts.append(e) or {"id": "c1"})
    confirmed = []
    monkeypatch.setattr(dashboard.db, "confirm_job_party",
                        lambda pid, t, contact_id=None, client=None:
                        confirmed.append((pid, contact_id)) or {"id": pid})
    monkeypatch.setattr(dashboard.db, "log_agent_event",
                        lambda t, kind, summary, **k: events.append((kind, summary)))
    r = client.post(f"/app/diary/{BOOKING['id']}/parties/p9/confirm")
    assert r.status_code == 303 and "msg=confirmed" in r.headers["location"]
    assert confirmed == [("p9", "c1")] and contacts == ["john@x.co"]
    assert events[0][0] == "party_confirmed"
    assert "money documents now route to them" in events[0][1]


def test_job_page_renders_the_claim_as_a_warned_card_not_a_row(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_booking",
                        lambda i, t, client=None: dict(BOOKING))
    monkeypatch.setattr(dashboard.db, "list_job_parties", lambda *a, **k: [
        {"id": "p9", "label": "landlord", "name": "John Smith",
         "email": "john@x.co", "pays": True, "approves": False,
         "on_site": False, "informed": False, "source": "extracted",
         "evidence": "invoices should go to the landlord John Smith"}])
    r = client.get(f"/app/diary/{BOOKING['id']}")
    assert r.status_code == 200
    assert "Read from the thread" in r.text
    assert "invoices should go to the landlord John Smith" in r.text  # verbatim
    assert "This changes who gets billed" in r.text                   # D4 warning
    assert "Confirm — I know them" in r.text and "Dismiss" in r.text
    # The receipt still routes to the booking's own contact — the claim is inert.
    assert "invoices to <span" in r.text and "booker@x.co" in r.text
    # And the settled-rows list doesn't double-render the claim.
    assert "No extra parties" in r.text


def test_first_invoice_to_a_confirmed_rerouted_payer_ccs_the_displaced_payer(monkeypatch):
    from app import db as real_db
    parties = [{"id": "p9", "email": "john@x.co", "pays": True,
                "source": "confirmed", "redirect_ccd": False, "informed": False}]
    marked = []
    monkeypatch.setattr(real_db, "list_job_parties",
                        lambda t, b, client=None: parties)
    monkeypatch.setattr(real_db, "get_booking",
                        lambda b, t, client=None: dict(BOOKING))
    monkeypatch.setattr(real_db, "mark_party_redirect_ccd",
                        lambda pid, t, client=None: marked.append(pid))
    cc = job_parties.cc_for_document(TENANT, BOOKING["id"], "john@x.co",
                                     redirect_guard=True)
    assert cc == ["booker@x.co"] and marked == ["p9"]   # the D4 receipt, once
    # Already receipted → no cc; owner-entered payers never trigger it; and
    # without the guard (quote sends) nothing changes.
    parties[0]["redirect_ccd"] = True
    assert job_parties.cc_for_document(TENANT, BOOKING["id"], "john@x.co",
                                       redirect_guard=True) == []
    parties[0].update(redirect_ccd=False, source="owner")
    assert job_parties.cc_for_document(TENANT, BOOKING["id"], "john@x.co",
                                       redirect_guard=True) == []
    parties[0]["source"] = "confirmed"
    assert job_parties.cc_for_document(TENANT, BOOKING["id"], "john@x.co") == []
    assert marked == ["p9"]


def test_pipeline_party_context_is_best_effort(monkeypatch):
    from app import pipeline
    monkeypatch.setattr(pipeline.db, "job_parties_for_bookings",
                        lambda *a, **k: {BOOKING["id"]: PARTIES})
    ctx = pipeline._party_context(TENANT, "agent@lettings.example", [BOOKING])
    assert "Sarah Vale" in ctx
    # No bookings → no read at all; a failing read → "" and the draft goes on.
    assert pipeline._party_context(TENANT, "a@x.co", []) == ""

    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(pipeline.db, "job_parties_for_bookings", boom)
    assert pipeline._party_context(TENANT, "a@x.co", [BOOKING]) == ""


def test_job_page_writes_the_dependant_out_and_shows_this_visits_day_notes(client, monkeypatch):
    """Kris 2026-09-23 (after K2): the dependant "could display a little better
    on mobile" — a record, label then value, not one wrapped line; and "the job
    page should show the 'On the day' promises" — only this visit's."""
    booking = dict(BOOKING, thread_id="th-1", email_log_id="log-2", booking_date="2026-09-30")
    monkeypatch.setattr(dashboard.db, "get_booking", lambda i, t, client=None: dict(booking))
    monkeypatch.setattr(dashboard.db, "list_job_parties", lambda *a, **k: [])
    group = {"id": "g1", "label": "Dog", "label_plural": "Dogs", "fields": [
        {"key": "breed", "label": "Breed", "type": "text", "durability": "once"},
        {"key": "vaccinations", "label": "Vaccinations", "type": "date", "durability": "expires", "gate": True},
        {"key": "feeding", "label": "Feeding instructions", "type": "text", "durability": "confirm"}]}
    monkeypatch.setattr(dashboard.db, "list_requirement_groups", lambda *a, **k: [group])
    monkeypatch.setattr(dashboard.db, "subjects_for_bookings", lambda *a, **k: {booking["id"]: [
        {"id": "i1", "group_id": "g1", "name": "Otis",
         "data": {"breed": "Cockapoo", "vaccinations": "2027-03-01"}}]})
    monkeypatch.setattr(dashboard.db, "crm_contact_by_email", lambda *a, **k: {"id": "c1"})
    monkeypatch.setattr(dashboard.db, "list_requirement_instances", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "email_log_ids_for_thread", lambda *a, **k: ["log-1", "log-2"])
    monkeypatch.setattr(dashboard.db, "crm_open_promises", lambda *a, **k: [
        {"id": "p1", "direction": "they_owe", "email_log_id": "log-2",
         "description": "Emma to bring enough Lily's Kitchen for two meals, portioned out.",
         "due_hint": "Wednesday 30 September at 1pm"},
        {"id": "p2", "direction": "they_owe", "email_log_id": "log-other",        # another visit
         "description": "Bring his coat for the December stay.", "due_hint": ""},
        {"id": "p3", "direction": "we_owe", "email_log_id": "log-2",              # an obligation, not a day note
         "description": "Once the trial night is complete, sort the Halloween stay.", "due_hint": ""}])
    html = client.get(f"/app/diary/{booking['id']}").text
    # the dependant as a record: label then value, a date read as a date, empty facts left out
    assert 'data-subject="i1"' in html and ">Breed</dt>" in html and ">Cockapoo" in html
    assert "1st Mar 27" in html and ">until</span>" in html
    assert "Feeding instructions</dt>" not in html
    # this visit's day notes, and only them
    assert "data-day-notes" in html and "Lily&#39;s Kitchen" in html
    assert "December stay" not in html and "Halloween stay" not in html
    assert '/app/crm/promises/p1/resolve' in html and f'value="/app/diary/{booking["id"]}"' in html


def test_job_page_with_no_thread_shows_no_day_notes(client, monkeypatch):
    monkeypatch.setattr(dashboard.db, "get_booking", lambda i, t, client=None: dict(BOOKING))
    monkeypatch.setattr(dashboard.db, "list_job_parties", lambda *a, **k: [])
    monkeypatch.setattr(dashboard.db, "crm_contact_by_email", lambda *a, **k: {"id": "c1"})
    monkeypatch.setattr(dashboard.db, "crm_open_promises", lambda *a, **k: [
        {"id": "p1", "direction": "they_owe", "email_log_id": "log-2",
         "description": "Bring the food portioned out.", "due_hint": "on the day"}])
    assert "data-day-notes" not in client.get(f"/app/diary/{BOOKING['id']}").text
