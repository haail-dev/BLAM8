"""The document hold: no wasted draft for an email that carries a card
(Kris 2026-09-22: "Could we do this in a manner that avoids a wasted draft?
Hold the draft until the right point.").

A vet card is read only once it is kept, and the pipeline kept files AFTER it
drafted — so the reply to "here's the new card" was written blind, then thrown
away the moment the card was read. Now, for a business that tracks dependants:

  * the email files with NO draft and no model call;
  * the card is kept and read;
  * no fact left for the owner  → the reply is written then, once;
  * a "You confirm" fact left   → the row waits, saying what it waits on, and
    the owner's confirm (or dismiss) writes the reply — once, even when two
    confirms land together.

Run: python -m pytest tests/test_document_hold.py -q
"""
import pytest

import app.pipeline as pipeline
from app.pipeline import DOC_HOLD_CONFIRM, DOC_HOLD_READING, DOC_HOLD_WRITING


# ── which attachments hold a reply ──────────────────────────────────────────
def test_a_pdf_holds_and_a_photo_or_an_inline_image_does_not():
    assert pipeline.card_names([
        {"name": "k1-alfie.pdf", "content_type": "application/pdf"},
        {"name": "Scan.PDF"},                                        # by extension
        {"name": "alfie.jpg", "content_type": "image/jpeg"},         # no text layer to read
        {"name": "logo.pdf", "content_type": "application/pdf", "inline": True},
        {"name": "", "content_type": "application/pdf"},
    ]) == ["k1-alfie.pdf", "Scan.PDF"]
    assert pipeline.card_names(None) == []


# ── draft_for_message decides ───────────────────────────────────────────────
def _message(monkeypatch, attachments, groups):
    seen = {}

    async def get_message(tenant_id, message_id, mailbox_id=None):
        return {"from": {"address": "claire@x.co", "name": "Claire"}, "subject": "Alfie",
                "body": "New card attached", "thread_id": "th1", "attachments": attachments}

    async def draft_for_email(tenant_id, email, **kw):
        seen["hold"] = kw.get("hold_for_documents")
        return "log-1"

    async def keep(tenant_id, message_id, log_id, msg, *, mailbox_id):
        seen["kept"] = log_id
        return {"labelled": 1, "written": 0, "suggested": 1, "skipped": 0}

    async def after(tenant_id, log_id, read):
        seen["after"] = (log_id, read)
    monkeypatch.setattr(pipeline.db, "get_email_log_by_message", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.mailbox, "get_message", get_message)
    monkeypatch.setattr(pipeline, "draft_for_email", draft_for_email)
    monkeypatch.setattr(pipeline, "keep_attachments", keep)
    monkeypatch.setattr(pipeline, "after_document_read", after)
    monkeypatch.setattr(pipeline.db, "list_requirement_groups", lambda *a, **k: groups)
    return seen


@pytest.mark.asyncio
async def test_a_card_for_a_business_with_dependants_is_read_before_the_reply(monkeypatch):
    seen = _message(monkeypatch, [{"name": "k1-alfie.pdf", "content_type": "application/pdf"}],
                    [{"id": "g-dog"}])
    await pipeline.draft_for_message("t1", "m1")
    assert seen["hold"] == ["k1-alfie.pdf"]
    assert seen["kept"] == "log-1"
    assert seen["after"][0] == "log-1" and seen["after"][1]["suggested"] == 1


@pytest.mark.asyncio
async def test_no_dependants_or_no_card_drafts_as_before(monkeypatch):
    seen = _message(monkeypatch, [{"name": "invoice.pdf", "content_type": "application/pdf"}], [])
    await pipeline.draft_for_message("t1", "m1")
    assert seen["hold"] is None and "after" not in seen          # files still kept
    assert seen["kept"] == "log-1"
    seen = _message(monkeypatch, [{"name": "alfie.jpg", "content_type": "image/jpeg"}], [{"id": "g"}])
    await pipeline.draft_for_message("t1", "m1")
    assert seen["hold"] is None and "after" not in seen


# ── draft_for_email files the held row and spends nothing ───────────────────
def _gates_open(monkeypatch, created=True):
    filed = []
    monkeypatch.setattr(pipeline, "_inbox_rules", lambda t: [])
    monkeypatch.setattr(pipeline.db, "is_ignored", lambda *a, **k: False)
    monkeypatch.setattr(pipeline.db, "mailbox_self_addresses", lambda *a, **k: set())
    monkeypatch.setattr(pipeline, "_cfg_draft_hold", lambda t: False)
    monkeypatch.setattr(pipeline.suppression, "check_inbound", lambda *a, **k: {"action": "draft"})
    monkeypatch.setattr(pipeline.db, "get_tenant", lambda *a, **k: {})
    monkeypatch.setattr(pipeline.trial, "gate_allows", lambda t: True)
    monkeypatch.setattr(pipeline.db, "insert_email_log",
                        lambda *a, **k: filed.append((a, k)) or ("log-1", created))
    monkeypatch.setattr(pipeline.crm, "ingest_email_log_row", lambda *a, **k: None)

    async def no_model(*a, **k):
        raise AssertionError("a held email must not spend a token")
    monkeypatch.setattr(pipeline.agent, "draft_reply", no_model)
    return filed


EMAIL = {"from_address": "claire@x.co", "from_name": "Claire", "subject": "Alfie",
         "body": "New card attached", "thread_id": "th1"}


@pytest.mark.asyncio
async def test_the_held_row_files_with_no_draft_and_no_model_call(monkeypatch):
    filed = _gates_open(monkeypatch)
    published = []
    monkeypatch.setattr(pipeline.events, "publish", lambda t, ev: published.append(ev))
    out = await pipeline.draft_for_email("t1", EMAIL, source_message_id="m1",
                                         hold_for_documents=["k1-alfie.pdf"])
    assert out == "log-1"
    # the open pages hear about it at once (K1 take: it sat unseen until a reload)
    assert [(e["type"], e["log_id"]) for e in published] == [("draft_ready", "log-1")]
    (args, kw), = filed
    assert args[4] == ""                                           # no draft
    assert kw["status"] == "pending" and kw["action_summary"] == DOC_HOLD_READING
    assert "k1-alfie.pdf" in kw["category_reason"]


@pytest.mark.asyncio
async def test_a_twin_delivery_never_reads_the_card_twice(monkeypatch):
    _gates_open(monkeypatch, created=False)
    assert await pipeline.draft_for_email("t1", EMAIL, source_message_id="m1",
                                          hold_for_documents=["k1-alfie.pdf"]) is None


# ── after the read, and the release ─────────────────────────────────────────
def _row(monkeypatch, summary, chips):
    state = {"summary": summary, "writes": [], "redrafts": 0, "published": []}
    monkeypatch.setattr(pipeline.db, "get_email_log_entry",
                        lambda log_id, t, client=None: {"id": log_id, "from_name": "Claire",
                                                        "from_address": "claire@x.co",
                                                        "action_summary": state["summary"]})
    monkeypatch.setattr(pipeline.db, "pending_chips_for_email", lambda t, log_id: list(chips))

    def claim(log_id, t, expect, to, client=None):
        if state["summary"] != expect:
            return False
        state["summary"] = to
        return True
    monkeypatch.setattr(pipeline.db, "claim_email_log_summary", claim)
    monkeypatch.setattr(pipeline.db, "update_email_log_draft",
                        lambda log_id, draft, t, **kw: state["writes"].append(kw) or
                        state.update(summary=kw.get("action_summary", state["summary"])))

    async def redraft(t, log_id, client=None):
        state["redrafts"] += 1
        return "Booked in."
    monkeypatch.setattr(pipeline, "redraft_email_log", redraft)
    monkeypatch.setattr(pipeline, "_chip_words", lambda t, c: "Alfie's Vaccinations (22nd Sep 27)")
    return state


@pytest.mark.asyncio
async def test_nothing_to_confirm_writes_the_reply_once_straight_away(monkeypatch):
    state = _row(monkeypatch, DOC_HOLD_READING, chips=[])
    await pipeline.after_document_read("t1", "log-1", {"written": 1, "suggested": 0})
    assert state["redrafts"] == 1 and state["summary"] == DOC_HOLD_WRITING


@pytest.mark.asyncio
async def test_the_thread_shows_the_held_reply_being_written(monkeypatch):
    """K1 take: "it did draft, it just didn't show any progress". The typing
    bubble opens and closes around the write, with no provisional copy of the
    customer's email (it is already on the thread)."""
    state = _row(monkeypatch, DOC_HOLD_CONFIRM, chips=[])
    seen = []
    monkeypatch.setattr(pipeline.events, "begin_drafting", lambda t, k, info: seen.append(("begin", k, info)))
    monkeypatch.setattr(pipeline.events, "end_drafting", lambda t, k: seen.append(("end", k)))
    monkeypatch.setattr(pipeline.events, "drafting_step", lambda t, c, label: seen.append(("step", label)))
    monkeypatch.setattr(pipeline.events, "publish", lambda t, ev: seen.append(("pub", ev["type"])))
    await pipeline.release_document_hold("t1", "log-1")
    kinds = [s[0] for s in seen]
    assert kinds[0] == "begin" and "end" in kinds and kinds[-1] == "pub"
    assert seen[0][1] == "held:log-1" and seen[0][2]["subject"] == "" and seen[0][2]["body"] == ""
    assert kinds.index("end") > kinds.index("step")
    assert state["redrafts"] == 1


@pytest.mark.asyncio
async def test_a_read_that_failed_still_writes_the_reply(monkeypatch):
    state = _row(monkeypatch, DOC_HOLD_READING, chips=[])
    await pipeline.after_document_read("t1", "log-1", None)
    assert state["redrafts"] == 1


@pytest.mark.asyncio
async def test_a_fact_for_the_owner_holds_and_says_what_it_waits_on(monkeypatch):
    state = _row(monkeypatch, DOC_HOLD_READING, chips=[{"id": "s1"}])
    await pipeline.after_document_read("t1", "log-1", {"written": 0, "suggested": 1})
    assert state["redrafts"] == 0
    assert state["summary"] == DOC_HOLD_CONFIRM
    reason = state["writes"][0]["category_reason"]
    assert reason.startswith("Waiting on you: confirm Alfie's Vaccinations (22nd Sep 27)")
    assert "Claire" in reason


@pytest.mark.asyncio
async def test_a_row_that_is_not_held_is_left_alone(monkeypatch):
    state = _row(monkeypatch, "Booked BK-1060", chips=[])
    await pipeline.after_document_read("t1", "log-1", {"written": 1})
    assert state["redrafts"] == 0 and state["writes"] == []


@pytest.mark.asyncio
async def test_the_confirm_writes_the_reply_once_even_twice_pressed(monkeypatch):
    state = _row(monkeypatch, DOC_HOLD_CONFIRM, chips=[])
    await pipeline.release_document_hold("t1", "log-1")
    await pipeline.release_document_hold("t1", "log-1")        # the second click / chip
    assert state["redrafts"] == 1


@pytest.mark.asyncio
async def test_the_reply_waits_while_any_chip_from_that_email_is_pending(monkeypatch):
    state = _row(monkeypatch, DOC_HOLD_CONFIRM, chips=[{"id": "s2"}])
    await pipeline.release_document_hold("t1", "log-1")
    assert state["redrafts"] == 0 and state["summary"] == DOC_HOLD_CONFIRM


# ── the buttons release it ──────────────────────────────────────────────────
def test_confirming_a_chip_from_a_held_email_releases_it_detached(monkeypatch):
    from fastapi.testclient import TestClient
    import app.dashboard as dashboard
    from app.auth import TenantCtx, require_onboarded
    from app.main import app
    from app.security import require_csrf

    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[dashboard.require_crm] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    detached = []
    try:
        monkeypatch.setattr(dashboard.db, "crm_get_suggestion",
                            lambda sid, t, client=None: {"id": sid, "entity": "dependant",
                                                         "email_log_id": "log-1"})
        monkeypatch.setattr(dashboard.crm_fields, "accept_suggestion", lambda *a, **k: True)
        monkeypatch.setattr(dashboard.crm_fields, "dismiss_suggestion", lambda *a, **k: True)
        monkeypatch.setattr(dashboard, "_detach", lambda coro: detached.append(coro))
        c = TestClient(app)
        for verb in ("accept", "dismiss"):
            r = c.post(f"/app/crm/suggestions/s1/{verb}", data={"return_to": "/app"},
                       follow_redirects=False)
            assert r.status_code == 303
        assert [d.cr_code.co_name for d in detached] == ["release_document_hold"] * 2
        for d in detached:
            d.close()
    finally:
        app.dependency_overrides.clear()


def test_one_tap_uses_everything_the_card_read_and_writes_the_reply_once(monkeypatch):
    """K1 rehearsal 2026-09-22: a vet card read three facts, and three taps to
    release one reply is not a flow. One decision per document."""
    from fastapi.testclient import TestClient
    import app.dashboard as dashboard
    from app.auth import TenantCtx, require_onboarded
    from app.main import app
    from app.security import require_csrf

    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[dashboard.require_crm] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    accepted, dismissed, detached = [], [], []
    try:
        monkeypatch.setattr(dashboard.db, "crm_get_suggestion",
                            lambda sid, t, client=None: {"id": sid, "entity": "dependant",
                                                         "email_log_id": "log-1"})
        monkeypatch.setattr(dashboard.crm_fields, "accept_suggestion",
                            lambda t, sid, client=None: accepted.append(sid) or True)
        monkeypatch.setattr(dashboard.crm_fields, "dismiss_suggestion",
                            lambda t, sid, client=None: dismissed.append(sid) or True)
        monkeypatch.setattr(dashboard, "_detach", lambda coro: detached.append(coro))
        c = TestClient(app)
        r = c.post("/app/crm/documents/d1/updates",
                   data={"ids": ["s1", "s2", "s3"], "use": "1", "return_to": "/app/crm/contacts/c1"},
                   follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/app/crm/contacts/c1"
        assert accepted == ["s1", "s2", "s3"] and dismissed == []
        assert len(detached) == 1                                 # one release for one held reply
        r = c.post("/app/crm/documents/d1/updates", data={"ids": ["s4"], "use": "0"},
                   follow_redirects=False)
        assert dismissed == ["s4"] and len(detached) == 2
        for d in detached:
            d.close()
    finally:
        app.dependency_overrides.clear()


def test_a_read_that_matches_the_record_asks_nothing():
    from app import dependants_extract as dx, requirement_groups as rg
    dog = rg.clean_group({"id": "g", "label": "Dog", "fields": [
        {"label": "Breed", "type": "text", "durability": "once"},
        {"label": "Vaccinations", "type": "date", "durability": "expires", "gate": True}]})
    alfie = {"id": "i", "group_id": "g", "name": "Alfie",
             "data": {"breed": "Labradoodle", "vaccinations": "2026-10-25"}}
    steps = dx.plan([dog], [alfie], [{"group": "Dog", "name": "Alfie", "confidence": 0.95, "fields": [
        {"key": "breed", "value": "labradoodle", "confidence": 0.95},
        {"key": "vaccinations", "value": "2027-09-22", "confidence": 0.95}]}],
        text="labradoodle 22 September 2027", confirm_all=True)
    assert [(s["action"], s["data"]) for s in steps] == [("suggest", {"vaccinations": "2027-09-22"})]


def test_a_chip_that_is_not_a_dependants_releases_nothing(monkeypatch):
    import app.dashboard as dashboard
    monkeypatch.setattr(dashboard.db, "crm_get_suggestion",
                        lambda sid, t, client=None: {"id": sid, "entity": "contact", "email_log_id": "l"})
    assert dashboard._held_reply_for("t1", "s1", None) is None
