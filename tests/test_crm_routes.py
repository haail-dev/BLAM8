"""CRM pages + JSON API (src/app/dashboard.py CRM section, src/app/crm_routes.py).

Route-level via TestClient (auth overridden, db monkeypatched — the house
pattern from test_home_dashboard.py): the contacts list, the contact-detail
killer view (recency badge, Draft-a-check-in into the existing conversation
flow, activity feed), companies, the stage-grouped pipeline, the nav entry,
and the /api/crm mirrors.

Run: python -m pytest tests/test_crm_routes.py -q
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.dashboard as dashboard
from app import crm
from app.auth import TenantCtx, require_onboarded
from app.main import app
from app.security import require_csrf


@pytest.fixture
def client():
    app.dependency_overrides[require_onboarded] = lambda: TenantCtx(tenant_id="t1", client=None)
    app.dependency_overrides[require_csrf] = lambda: None
    yield TestClient(app)   # no `with`: startup events must not run
    app.dependency_overrides.clear()


CONTACTS = [
    {"id": "ct1", "email": "rob@acme-haulage.co.uk", "name": "Rob Smith",
     "company_id": "co1", "first_seen_at": "2026-05-01T09:00:00+00:00",
     "last_activity_at": "2026-06-01T09:00:00+00:00",     # long quiet — stale
     "crm_companies": {"id": "co1", "name": "Acme Haulage", "domain": "acme-haulage.co.uk"}},
    # Fresh = relative to now, or the passage of real time flips her stale
    # (a fixed date here broke this suite 19 days after it was written).
    {"id": "ct2", "email": "sarah@gmail.com", "name": None, "company_id": None,
     "first_seen_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
     "last_activity_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
     "crm_companies": None},
]

COMPANIES = [
    {"id": "co1", "name": "Acme Haulage", "domain": "acme-haulage.co.uk",
     "last_activity_at": "2026-06-01T09:00:00+00:00", "crm_contacts": [{"count": 3}]},
]

STAGES = [
    {"id": "s1", "tenant_id": "t1", "name": "Prospect", "display_order": 0, "stage_type": "active"},
    {"id": "s2", "tenant_id": "t1", "name": "Won", "display_order": 30, "stage_type": "won"},
]

DEALS = [
    {"id": "d1", "title": "Weekly pallet run", "stage_id": "s1", "status": "open",
     "value_estimate": 1200, "opened_at": "2026-06-20T09:00:00+00:00",
     "primary_contact_id": "ct1", "company_id": "co1",
     "crm_stages": {"id": "s1", "name": "Prospect", "stage_type": "active"},
     "crm_companies": {"id": "co1", "name": "Acme Haulage", "domain": "acme-haulage.co.uk"},
     "crm_contacts": {"id": "ct1", "name": "Rob Smith", "email": "rob@acme-haulage.co.uk"}},
]

ACTIVITIES = [
    {"id": "a2", "activity_type": "email", "direction": "outbound",
     "subject": "3 pallets to Leeds", "summary": "Quoted £120",
     "occurred_at": "2026-06-01T09:05:00+00:00"},
    {"id": "a1", "activity_type": "email", "direction": "inbound",
     "subject": "3 pallets to Leeds", "summary": "Wants a quote",
     "occurred_at": "2026-06-01T09:00:00+00:00"},
]


class Env:
    """The nav ctx (_ctx) plus every crm_* read the pages/API touch."""

    def __init__(self, monkeypatch, *, contacts=CONTACTS, companies=COMPANIES,
                 stages=STAGES, deals=DEALS, activities=ACTIVITIES,
                 promises=(), cleared=()):
        monkeypatch.setattr(dashboard.db, "list_email_log", lambda *a, **k: [])
        monkeypatch.setattr(dashboard.db, "active_modules", lambda *a, **k: {"crm"})   # CRM unlocks at Core (2026-07-24)
        monkeypatch.setattr(dashboard.db, "get_tenant", lambda *a, **k: {"name": "Koth"})
        monkeypatch.setattr(dashboard.db, "is_enterprise", lambda *a, **k: False)
        monkeypatch.setattr(dashboard.billing, "has_active_subscription", lambda *a, **k: False)
        # The logbook's four reads (2026-09-21) — empty unless a test hands them in.
        monkeypatch.setattr(dashboard.db, "list_requirement_instances_all", lambda *a, **k: [])
        monkeypatch.setattr(dashboard.db, "list_bookings", lambda *a, **k: [])
        monkeypatch.setattr(dashboard.db, "list_invoices", lambda *a, **k: [])
        monkeypatch.setattr(dashboard.db, "crm_latest_activities", lambda *a, **k: [])
        monkeypatch.setattr(dashboard.db, "crm_list_contacts",
                            lambda tid, q=None, limit=100, offset=0, client=None:
                            [c for c in contacts if not q or q.lower() in
                             (c.get("name") or "").lower() + c["email"]])
        monkeypatch.setattr(dashboard.db, "crm_get_contact",
                            lambda cid, tid, client=None:
                            next((c for c in contacts if c["id"] == cid), None))
        monkeypatch.setattr(dashboard.db, "crm_list_companies",
                            lambda tid, limit=100, offset=0, client=None: list(companies))
        monkeypatch.setattr(dashboard.db, "crm_get_company",
                            lambda cid, tid, client=None:
                            next((c for c in companies if c["id"] == cid), None))
        monkeypatch.setattr(dashboard.db, "crm_contacts_for_company",
                            lambda cid, tid, client=None:
                            [c for c in contacts if c.get("company_id") == cid])
        monkeypatch.setattr(dashboard.db, "crm_list_stages",
                            lambda tid, client=None: list(stages))
        monkeypatch.setattr(dashboard.db, "crm_list_deals",
                            lambda tid, contact_id=None, company_id=None, client=None:
                            [d for d in deals
                             if (contact_id is None or d["primary_contact_id"] == contact_id)
                             and (company_id is None or d["company_id"] == company_id)])
        monkeypatch.setattr(dashboard.db, "crm_get_deal",
                            lambda did, tid, client=None:
                            next((d for d in deals if d["id"] == did), None))
        monkeypatch.setattr(dashboard.db, "crm_activities_for_contact",
                            lambda cid, tid, limit=20, client=None: list(activities))
        monkeypatch.setattr(dashboard.db, "crm_activities_for_deal",
                            lambda did, tid, limit=50, client=None: list(activities))
        monkeypatch.setattr(dashboard.db, "crm_list_field_definitions",
                            lambda tid, entity=None, client=None: [])
        monkeypatch.setattr(dashboard.db, "crm_pending_suggestions",
                            lambda tid, record_ids=None, client=None: [])
        monkeypatch.setattr(dashboard.db, "crm_open_promises",
                            lambda tid, contact_id=None, client=None: list(promises))
        monkeypatch.setattr(dashboard.db, "crm_recent_cleared_promises",
                            lambda tid, contact_id=None, days=30, limit=8, client=None:
                            list(cleared))
        self.new_stages = []
        def insert_stage(tid, name, display_order=0, stage_type="active", client=None):
            row = {"id": f"s-new-{len(self.new_stages)}", "tenant_id": tid, "name": name,
                   "display_order": display_order, "stage_type": stage_type}
            self.new_stages.append(row)
            return row
        monkeypatch.setattr(dashboard.db, "crm_insert_stage", insert_stage)


# ── Pages ────────────────────────────────────────────────────────────────────

def test_contacts_list_renders_and_flags_the_quiet_ones(client, monkeypatch):
    Env(monkeypatch)
    r = client.get("/app/crm")
    assert r.status_code == 200
    assert "Rob Smith" in r.text and "Acme Haulage" in r.text
    assert "sarah@gmail.com" in r.text
    assert "quiet" in r.text                       # Rob has gone stale
    assert "/app/crm/contacts/ct1" in r.text       # rows link to the killer view


def test_contact_detail_is_the_killer_view(client, monkeypatch):
    Env(monkeypatch)
    r = client.get("/app/crm/contacts/ct1")
    assert r.status_code == 200
    assert "Rob Smith" in r.text and "rob@acme-haulage.co.uk" in r.text
    assert "Acme Haulage" in r.text
    assert "last contact" in r.text                 # recency badge carries the stale nudge
    assert ">Conversation</button>" in r.text        # one word, top right (Kris 2026-09-20)
    assert "Draft a check-in" not in r.text
    assert "/app/conversations/view" in r.text      # into the EXISTING draft flow
    assert "Weekly pallet run" in r.text            # current deals
    assert "Wants a quote" in r.text and "Quoted £120" in r.text  # the feed
    # Promises fold to a face that STATES its count (the Settings idiom, 09-01);
    # the you-owe/they-owe wording lives on the rows inside.
    assert "Promises" in r.text and "open" in r.text


def test_contact_detail_party_to_lines(client, monkeypatch):
    """Job-parties Phase 1 (docs/modules/module-job-parties.md): the contact page lists
    every job this person is a party on, linking into the diary job page. The
    fold only exists when there ARE rows — 90% of contacts never see it."""
    Env(monkeypatch)
    monkeypatch.setattr(
        dashboard.db, "job_parties_for_contact",
        lambda tid, cid, email=None, client=None: [
            {"id": "p1", "booking_id": "bk1", "label": "landlord",
             "pays": True, "approves": True, "on_site": False, "informed": False}])
    monkeypatch.setattr(
        dashboard.db, "bookings_by_ids",
        lambda tid, ids, client=None: [
            {"id": "bk1", "booking_ref": "BK-1077", "booking_date": "2026-09-10",
             "summary": "Rewire kitchen"}])
    r = client.get("/app/crm/contacts/ct1")
    assert r.status_code == 200
    assert "Party to" in r.text and "1 job" in r.text
    assert "BK-1077" in r.text and "landlord" in r.text
    assert "pays, approves quotes" in r.text          # duties, legibly
    assert "/app/diary/bk1" in r.text                 # into the job page
    assert "10th Sep 26" in r.text                    # pretty dates, never [:10]
    # And without rows the fold is absent entirely.
    monkeypatch.setattr(dashboard.db, "job_parties_for_contact",
                        lambda *a, **k: [])
    assert "Party to" not in client.get("/app/crm/contacts/ct1").text


def test_contact_detail_conversation_button_sits_top_right_stale_or_not(client, monkeypatch):
    """Kris 2026-09-20: the button reads "Conversation" and pins to the box's
    top-right corner — it used to wrap under the name on a phone, and read
    "Draft a check-in" for a stale contact, which the amber badge already says."""
    Env(monkeypatch)
    for cid in ("ct1", "ct2"):                       # stale, and fresh
        r = client.get(f"/app/crm/contacts/{cid}")
        assert r.status_code == 200
        box = r.text[r.text.index("<!-- Who they are"):r.text.index("<!-- Everything known")]
        assert 'class="absolute top-4 right-4"' in box
        assert ">Conversation</button>" in box
        assert "Open conversation" not in box and "Draft a check-in" not in box
        assert 'p-5 relative' in box                   # the corner is the box's own


def test_contact_detail_404s_across_tenants(client, monkeypatch):
    Env(monkeypatch)
    assert client.get("/app/crm/contacts/other-tenants-id").status_code == 404


def test_companies_page_counts_contacts(client, monkeypatch):
    Env(monkeypatch)
    r = client.get("/app/crm/companies")
    assert r.status_code == 200
    assert "Acme Haulage" in r.text and "acme-haulage.co.uk" in r.text
    assert "3 contacts" in r.text


def test_pipeline_page_groups_deals_under_stages(client, monkeypatch):
    Env(monkeypatch)
    r = client.get("/app/crm/pipeline")
    assert r.status_code == 200
    assert "Prospect" in r.text and "Won" in r.text
    assert "Weekly pallet run" in r.text and "£1200" in r.text


def test_contact_page_shows_cleared_promises_with_their_link(client, monkeypatch):
    """Kris 2026-09-20: a cleared promise is shown cleared — green, ticked,
    linked to what cleared it — and a suggested one asks for a Confirm."""
    Env(monkeypatch,
        promises=[{"id": "p1", "direction": "we_owe", "description": "Send the quote",
                   "due_hint": "Friday", "resolution_type": "suggested",
                   "resolution_evidence": "QUO-1012 went out",
                   "resolved_by_kind": "quote", "resolved_by_id": "q1",
                   "resolved_by_label": "QUO-1012"}],
        cleared=[{"id": "p2", "direction": "they_owe", "description": "Confirm the pallet count",
                  "status": "resolved", "resolution_evidence": "18 pallets confirmed",
                  "resolved_by_kind": "email", "resolved_by_id": "log9",
                  "resolved_by_label": "their email",
                  "resolved_at": "2026-09-19T10:00:00+00:00"},
                 {"id": "p3", "direction": "we_owe", "description": "Ring back Tuesday",
                  "status": "dropped", "resolved_by_kind": "manual",
                  "resolved_at": "2026-09-18T10:00:00+00:00"}])
    r = client.get("/app/crm/contacts/ct1")
    assert r.status_code == 200
    body = r.text
    assert "1 open · 1 cleared" in body                 # dropped rows don't count
    assert "cleared by their email" in body
    assert 'href="/app/conversations/view?contact=rob%40acme-haulage.co.uk#log-log9"' in body
    assert "18 pallets confirmed" in body and "19th Sep 26" in body
    assert "line-through" in body and ">dropped · 18th Sep 26<" in body
    # the suggested row: amber, linked to the quote, Confirm / Still open
    assert "looks cleared" in body and 'href="/app/quotes/q1"' in body
    assert 'value="confirm"' in body and 'value="keep"' in body


def test_contact_page_without_promises_still_reads_empty(client, monkeypatch):
    Env(monkeypatch)
    body = client.get("/app/crm/contacts/ct1").text
    assert "none open" in body and "data-cleared-promise" not in body


def test_nav_carries_the_crm_entry(client, monkeypatch):
    Env(monkeypatch)
    r = client.get("/app/crm")
    assert '<span class="flex-1">CRM</span>' in r.text


def test_empty_crm_promises_it_fills_itself(client, monkeypatch):
    Env(monkeypatch, contacts=[])
    r = client.get("/app/crm")
    assert "fills itself in" in r.text


# ── JSON API ─────────────────────────────────────────────────────────────────

def test_api_contacts_carry_recency(client, monkeypatch):
    Env(monkeypatch)
    body = client.get("/api/crm/contacts").json()
    rob = body["contacts"][0]
    assert rob["email"] == "rob@acme-haulage.co.uk"
    assert rob["stale"] is True and rob["days_since_last"] > crm.STALE_AFTER_DAYS


def test_api_contact_detail_bundles_deals_and_feed(client, monkeypatch):
    Env(monkeypatch)
    body = client.get("/api/crm/contacts/ct1").json()
    assert body["contact"]["id"] == "ct1"
    assert [d["id"] for d in body["deals"]] == ["d1"]
    assert [a["direction"] for a in body["activities"]] == ["outbound", "inbound"]
    assert client.get("/api/crm/contacts/nope").status_code == 404


def test_api_company_detail_and_deals(client, monkeypatch):
    Env(monkeypatch)
    body = client.get("/api/crm/companies/co1").json()
    assert body["company"]["name"] == "Acme Haulage"
    assert [c["id"] for c in body["contacts"]] == ["ct1"]
    assert client.get("/api/crm/deals").json()["deals"][0]["id"] == "d1"
    assert client.get("/api/crm/deals/d1").json()["deal"]["title"] == "Weekly pallet run"


def test_api_pipeline_groups_by_stage(client, monkeypatch):
    Env(monkeypatch)
    cols = client.get("/api/crm/pipeline").json()["pipeline"]
    assert [c["stage"]["name"] for c in cols] == ["Prospect", "Won"]
    assert [len(c["deals"]) for c in cols] == [1, 0]


def test_api_add_stage_validates_and_inserts(client, monkeypatch):
    env = Env(monkeypatch)
    r = client.post("/api/crm/stages", data={"name": "On hold", "stage_type": "active",
                                             "display_order": 25})
    assert r.status_code == 200 and r.json()["stage"]["name"] == "On hold"
    assert env.new_stages[0]["display_order"] == 25
    assert client.post("/api/crm/stages", data={"name": "  ", "stage_type": "active"}).status_code == 422
    assert client.post("/api/crm/stages", data={"name": "X", "stage_type": "sideways"}).status_code == 422


def test_contacts_page_reads_as_a_logbook(client, monkeypatch):
    """Kris 2026-09-21: "more like a contact logbook". Grouped by when you last
    spoke; each name with its dependants, its last log line and the facts."""
    Env(monkeypatch, promises=[{"contact_id": "ct2", "direction": "they_owe",
                                "description": "Confirm Biscuit's dates"}])
    monkeypatch.setattr(dashboard.db, "list_requirement_instances_all", lambda *a, **k: [
        {"contact_id": "ct2", "name": "Biscuit", "data": {"breed": "Cockapoo"}, "group_label": "Dog"}])
    monkeypatch.setattr(dashboard.db, "list_bookings", lambda *a, **k: [
        {"contact": "sarah@gmail.com", "booking_date": "2099-10-07", "status": "confirmed"}])
    monkeypatch.setattr(dashboard.db, "list_invoices", lambda *a, **k: [
        {"customer_email": "sarah@gmail.com", "status": "sent", "total": 30.51,
         "amount_paid": 0, "due_date": "2099-10-05"}])
    monkeypatch.setattr(dashboard.db, "crm_latest_activities", lambda *a, **k: [
        {"contact_id": "ct2", "direction": "inbound", "summary": "Wants a November weekend too",
         "occurred_at": "2026-09-21T09:46:00+00:00"}])
    html = client.get("/app/crm?sort=recent").text
    assert 'data-logbook-group="Yesterday"' in html          # sarah spoke a day ago
    assert 'data-logbook-group="Earlier"' in html            # rob went quiet in June
    assert "Biscuit · Cockapoo" in html                      # the dog under her name
    assert "←</span> Wants a November weekend too" in html    # her last log line
    assert "Next 7th Oct 99" in html and "Owes £30.51" in html and "they owe 1" in html
    assert 'title="sarah@gmail.com"' in html                # the address under the pointer
    assert "quiet" in html                                   # rob's stale chip survives


def test_contacts_page_orders_recent_or_a_to_z(client, monkeypatch):
    Env(monkeypatch)
    az = client.get("/app/crm").text                          # A–Z is the default (Kris 2026-09-21)
    assert 'data-crm-order' in az
    assert 'data-logbook-group="R"' in az and 'data-logbook-group="S"' in az     # Rob Smith, sarah@…
    assert 'data-logbook-group="Yesterday"' not in az
    assert az.index('data-logbook-group="R"') < az.index('data-logbook-group="S"')
    recent = client.get("/app/crm?sort=recent").text
    assert 'data-logbook-group="Yesterday"' in recent and 'data-logbook-group="R"' not in recent
    # the switch keeps a search filter
    assert 'href="/app/crm?sort=recent&amp;q=rob"' in client.get("/app/crm?q=rob").text


def test_dependants_read_as_a_record_with_a_pencil_to_edit(client, monkeypatch):
    """Kris 2026-09-21: a form is not a record. What's known is written out; the
    pencil top-right opens the same fields to edit by hand."""
    Env(monkeypatch)
    group = {"id": "g1", "label": "Dog", "label_plural": "Dogs", "per_customer": "many",
             "fields": [{"key": "breed", "label": "Breed", "type": "text", "required": False, "durability": "once"},
                        {"key": "vaccinations", "label": "Vaccinations", "type": "date", "required": True,
                         "durability": "expires"},
                        {"key": "feeding", "label": "Feeding instructions", "type": "text", "required": False,
                         "durability": "once"}]}
    monkeypatch.setattr(dashboard.db, "list_requirement_groups", lambda *a, **k: [group])
    monkeypatch.setattr(dashboard.db, "list_requirement_instances", lambda *a, **k: [
        {"id": "i1", "group_id": "g1", "name": "Biscuit",
         "data": {"breed": "Cockapoo", "vaccinations": "2027-06-01"}}])
    monkeypatch.setattr(dashboard.db, "crm_pending_suggestions", lambda *a, **k: [])
    html = client.get("/app/crm/contacts/ct1").text
    assert 'data-dependant="i1"' in html
    assert ">Breed</dt>" in html and ">Cockapoo" in html                 # written out, label then value
    assert "1st Jun 27" in html and ">until</span>" in html             # a date reads as a date; an expiry says so
    assert "Feeding instructions</dt>" not in html                      # an empty field is not written out
    assert 'aria-label="Edit Biscuit"' in html and 'name="f_breed"' in html   # the pencil, the same form behind it


def test_a_new_card_asks_on_its_document_to_update_the_dependant(client, monkeypatch):
    """Kris 2026-09-22: the card arrives in Documents "asking for a confirmation
    to use it to update info" — on the document, marked as the new version of
    the one before, and not repeated under Dependants."""
    Env(monkeypatch)
    group = {"id": "g1", "label": "Dog", "label_plural": "Dogs",
             "fields": [{"key": "vaccinations", "label": "Vaccinations", "type": "date", "required": True,
                         "durability": "expires", "gate": True}]}
    monkeypatch.setattr(dashboard.db, "list_requirement_groups", lambda *a, **k: [group])
    monkeypatch.setattr(dashboard.db, "list_requirement_instances", lambda *a, **k: [
        {"id": "i1", "group_id": "g1", "name": "Alfie", "data": {"vaccinations": "2026-10-25"}}])
    monkeypatch.setattr(dashboard.db, "crm_pending_suggestions", lambda *a, **k: [
        {"id": "s1", "entity": "dependant", "record_id": "i1", "field_key": "vaccinations",
         "value": "2027-09-22", "email_log_id": "log-2", "status": "pending"}])
    monkeypatch.setattr(dashboard.db, "list_documents", lambda *a, **k: [
        {"id": "d-new", "filename": "k1-alfie.pdf", "label": "Vaccination record", "instance_id": "i1",
         "email_log_id": "log-2", "artifact_type": "attachment", "size_bytes": 1751,
         "created_at": "2026-09-22T21:40:00"},
        {"id": "d-old", "filename": "alfie-2025.pdf", "label": "Vaccination record", "instance_id": "i1",
         "email_log_id": "log-0", "artifact_type": "attachment", "size_bytes": 1700,
         "created_at": "2025-10-25T09:00:00"}])
    monkeypatch.setattr(dashboard.db, "get_scheduling_config", lambda *a, **k: {})
    monkeypatch.setattr(dashboard.db, "bookings_for_contact", lambda *a, **k: [])
    html = client.get("/app/crm/contacts/ct1").text
    assert "Use it to update <span class=\"font-semibold\">Alfie</span>?" in html
    assert "25th Oct 26" in html and "22nd Sep 27" in html                 # from → to, as dates
    assert 'action="/app/crm/documents/d-new/updates"' in html              # one decision for the card
    assert 'name="ids" value="s1"' in html and "✓ Use it" in html and "Don't use it" in html
    assert ">new version</span>" in html and "It replaces alfie-2025.pdf" in html
    assert "1 to confirm" in html
    assert "/app/crm/suggestions/s1/accept" not in html                     # not repeated under Dependants


def test_contact_card_keeps_bookings_and_payments_out_of_you_owe(client, monkeypatch):
    """Kris 2026-09-21: a booking waiting for its day is not pending work. Only
    obligations sit under You owe / They owe; the day's logistics sit under
    "On the day"; a held booking and a deposit have surfaces of their own."""
    Env(monkeypatch, promises=[
        {"id": "p1", "contact_id": "ct1", "direction": "we_owe",
         "description": "Host Biscuit for November boarding (BK-1024) with drop-off 20 November at 8.30am."},
        {"id": "p2", "contact_id": "ct1", "direction": "they_owe",
         "description": "Hannah will pay the deposit by bank transfer today.", "due_hint": "today"},
        {"id": "p3", "contact_id": "ct1", "direction": "they_owe",
         "description": "Bring Biscuit's food portioned out or with a scoop for his stay."},
        {"id": "p4", "contact_id": "ct1", "direction": "we_owe",
         "description": "Post daily photos on Facebook so Priya can keep an eye on Lola and Bean."}])
    html = client.get("/app/crm/contacts/ct1").text
    assert 'data-promise="p4"' in html and "Post daily photos" in html          # the obligation shows
    assert 'data-promise="p3"' in html and 'data-kind="on_the_day"' in html    # …the food under On the day
    assert 'data-promise="p1"' not in html and "Host Biscuit" not in html      # the diary's, not a debt
    assert 'data-promise="p2"' not in html and "pay the deposit" not in html   # Money's, not a debt
    assert "1 open · 1 on the day" in html                                      # the face counts what it shows
