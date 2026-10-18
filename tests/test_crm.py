"""CRM derivation logic (src/app/crm.py — docs/architecture/CRM_design.md).

Unit-level, db monkeypatched: domain → company inference (freemail never becomes
a firm), the ingest fold (upserts + activity feed, inbound always / outbound on
sent, own desks skipped), idempotent backfill tally, default-stage seeding, and
the pure view-model helpers (recency, pipeline grouping).

Run: python -m pytest tests/test_crm.py -q
"""
from datetime import datetime, timezone

import pytest

from app import crm


# ── Pure helpers ─────────────────────────────────────────────────────────────

def test_domain_of():
    assert crm.domain_of("Rob@Acme-Haulage.CO.UK") == "acme-haulage.co.uk"
    assert crm.domain_of("no-at-sign") is None
    assert crm.domain_of("") is None
    assert crm.domain_of(None) is None


def test_freemail_never_becomes_a_company():
    assert not crm.is_business_domain("gmail.com")
    assert not crm.is_business_domain("btinternet.com")   # UK ISP mail matters here
    assert not crm.is_business_domain(None)
    assert crm.is_business_domain("acme-haulage.co.uk")


def test_company_name_from_domain():
    assert crm.company_name_from_domain("acme-haulage.co.uk") == "Acme Haulage"
    assert crm.company_name_from_domain("dpd.co.uk") == "DPD"  # short labels read as initialisms
    assert crm.company_name_from_domain("koth.co.uk") == "Koth"
    assert crm.company_name_from_domain("smithlogistics.com") == "Smithlogistics"


def test_days_since_and_recency():
    now = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)
    assert crm.days_since("2026-07-01T09:00:00+00:00", now) == 4
    assert crm.days_since("2026-07-05T09:00:00Z", now) == 0
    assert crm.days_since(None, now) is None
    assert crm.days_since("not-a-date", now) is None
    rows = crm.with_recency([
        {"id": "a", "last_activity_at": "2026-06-01T09:00:00+00:00"},   # 34d — stale
        {"id": "b", "last_activity_at": "2026-07-04T09:00:00+00:00"},   # 1d — fresh
        {"id": "c", "last_activity_at": None},                          # never — not stale
    ], now)
    assert [r["stale"] for r in rows] == [True, False, False]
    assert rows[0]["days_since_last"] == 34


def test_newer_orders_z_against_offset_suffix():
    # String comparison would say Z > +00:00 for the SAME instant — parsed must not.
    assert not crm._newer("2026-07-05T09:00:00Z", "2026-07-05T09:00:00+00:00")
    assert crm._newer("2026-07-05T09:00:01Z", "2026-07-05T09:00:00+00:00")
    assert crm._newer("2026-07-05T09:00:00Z", None)
    assert not crm._newer(None, "2026-07-05T09:00:00Z")


def test_pipeline_view_groups_and_keeps_orphans_visible():
    stages = [{"id": "s1", "name": "Prospect"}, {"id": "s2", "name": "Won"}]
    deals = [{"id": "d1", "stage_id": "s1"}, {"id": "d2", "stage_id": "s1"},
             {"id": "d3", "stage_id": None}]
    cols = crm.pipeline_view(stages, deals)
    assert [len(c["deals"]) for c in cols] == [2, 0, 1]
    assert cols[-1]["stage"]["name"] == "Unstaged"
    assert crm.pipeline_view(stages, []) and len(crm.pipeline_view(stages, [])) == 2


# ── Ingest (db monkeypatched) ────────────────────────────────────────────────

class FakeDb:
    """Captures the CRM writes ingest makes. get-or-creates return existing rows
    when primed, else mint fresh ones — the unique-key behaviour in miniature."""

    def __init__(self, monkeypatch, companies=(), contacts=(), own=frozenset()):
        self.companies = {c["domain"] or c["name"]: c for c in companies}
        self.contacts = {c["email"]: c for c in contacts}
        self.company_updates, self.contact_updates, self.activities = [], [], []
        self.merged_custom = []
        self._seen_activity_keys = set()
        monkeypatch.setattr(crm.db, "crm_get_or_create_company", self._company)
        monkeypatch.setattr(crm.db, "crm_get_or_create_contact", self._contact)
        monkeypatch.setattr(crm.db, "crm_update_company",
                            lambda cid, tid, fields: self.company_updates.append((cid, fields)))
        monkeypatch.setattr(crm.db, "crm_update_contact",
                            lambda cid, tid, fields: self.contact_updates.append((cid, fields)))
        monkeypatch.setattr(crm.db, "crm_insert_activity", self._activity)
        monkeypatch.setattr(crm.db, "own_mailbox_addresses", lambda tid: set(own))
        monkeypatch.setattr(crm.db, "crm_list_companies",
                            lambda tid, limit=500: list(self.companies.values()))
        monkeypatch.setattr(crm.db, "crm_create_company_by_name", self._company_by_name)
        monkeypatch.setattr(crm.db, "crm_merge_custom_data",
                            lambda entity, rid, tid, values, client=None:
                            self.merged_custom.append((entity, rid, values)))

    def _company(self, tenant_id, domain, name=None):
        return self.companies.setdefault(domain, {"id": f"co-{domain}", "domain": domain,
                                                  "name": name, "last_activity_at": None})

    def _company_by_name(self, tenant_id, name):
        return self.companies.setdefault(name, {"id": f"co-{name}", "domain": None,
                                                "name": name, "last_activity_at": None})

    def _contact(self, tenant_id, email, name=None, company_id=None):
        return self.contacts.setdefault(email, {"id": f"ct-{email}", "email": email,
                                                "name": name, "company_id": company_id,
                                                "last_activity_at": None})

    def _activity(self, tenant_id, **kw):
        key = (kw.get("activity_type"), kw.get("source_id"), kw.get("direction"))
        if kw.get("source_id") and key in self._seen_activity_keys:
            return False   # crm_activities_source_uniq in miniature
        self._seen_activity_keys.add(key)
        self.activities.append(kw)
        return True


ROW = {"id": "log1", "from_address": "rob@acme-haulage.co.uk", "from_name": "Rob Smith",
       "contact_name": None, "contact_company": None, "subject": "3 pallets to Leeds",
       "status": "pending", "request_summary": "Wants a quote",
       "created_at": "2026-07-05T09:00:00+00:00"}


def test_ingest_business_email_builds_company_contact_activity(monkeypatch):
    fake = FakeDb(monkeypatch)
    tally = crm.ingest_email_log_row("t1", dict(ROW))
    assert tally["activities"] == 1
    assert tally["contact_id"] == "ct-rob@acme-haulage.co.uk"
    assert tally["company_id"] == "co-acme-haulage.co.uk"
    assert fake.companies["acme-haulage.co.uk"]["name"] == "Acme Haulage"
    contact = fake.contacts["rob@acme-haulage.co.uk"]
    assert contact["name"] == "Rob Smith" and contact["company_id"] == "co-acme-haulage.co.uk"
    (act,) = fake.activities
    assert act["direction"] == "inbound" and act["source_id"] == "log1"
    assert act["summary"] == "Wants a quote"


def test_ingest_freemail_contact_stands_alone(monkeypatch):
    fake = FakeDb(monkeypatch)
    crm.ingest_email_log_row("t1", {**ROW, "from_address": "sarah@gmail.com"})
    assert fake.companies == {}
    assert fake.contacts["sarah@gmail.com"]["company_id"] is None
    assert len(fake.activities) == 1


def test_ingest_sent_row_adds_the_outbound_reply(monkeypatch):
    fake = FakeDb(monkeypatch)
    tally = crm.ingest_email_log_row("t1", {**ROW, "status": "sent",
                                            "action_summary": "Quoted £120"})
    assert tally["activities"] == 2
    assert [a["direction"] for a in fake.activities] == ["inbound", "outbound"]
    assert fake.activities[1]["summary"] == "Quoted £120"


def test_ingest_skips_our_own_desks_and_junk_addresses(monkeypatch):
    fake = FakeDb(monkeypatch, own={"office@koth.co.uk"})
    nothing = {"activities": 0, "contact_id": None, "company_id": None}
    assert crm.ingest_email_log_row("t1", {**ROW, "from_address": "Office@koth.co.uk"}) \
        == nothing
    assert crm.ingest_email_log_row("t1", {**ROW, "from_address": "not-an-email"}) \
        == nothing
    # A robot sender is nobody's customer — the backfill met a real one of these.
    assert crm.ingest_email_log_row(
        "t1", {**ROW, "from_address": "account-security-noreply@accountprotection.microsoft.com"}) \
        == nothing
    assert fake.contacts == {} and fake.activities == []


def test_ingest_stated_name_and_company_win(monkeypatch):
    # The customer SAID who they are — that beats the header and the domain guess.
    fake = FakeDb(monkeypatch,
                  companies=[{"id": "co1", "domain": "acme-haulage.co.uk",
                              "name": "Acme Haulage", "last_activity_at": "2026-07-01T00:00:00+00:00"}],
                  contacts=[{"id": "ct1", "email": "rob@acme-haulage.co.uk", "name": None,
                             "company_id": "co1", "last_activity_at": "2026-07-01T00:00:00+00:00"}])
    crm.ingest_email_log_row("t1", {**ROW, "contact_name": "Robert Smith",
                                    "contact_company": "Acme Haulage Ltd"})
    assert ("co1", {"name": "Acme Haulage Ltd",
                    "last_activity_at": ROW["created_at"]}) in fake.company_updates
    (cid, fields), = [u for u in fake.contact_updates]
    assert cid == "ct1" and fields["name"] == "Robert Smith"
    assert fields["last_activity_at"] == ROW["created_at"]


def test_ingest_freemail_stated_company_links_with_provenance(monkeypatch):
    # First sight of a freemail sender who SAYS who they work for: a domain-less
    # company is minted, linked, and the link's origin is stamped.
    fake = FakeDb(monkeypatch)
    tally = crm.ingest_email_log_row(
        "t1", {**ROW, "from_address": "sarah@gmail.com",
               "contact_company": "Velcee Consulting Ltd"})
    company = fake.companies["Velcee Consulting Ltd"]
    assert company["domain"] is None
    assert tally["company_id"] == company["id"]
    (cid, fields), = fake.contact_updates
    assert fields["company_id"] == company["id"]
    (entity, rid, values), = fake.merged_custom
    assert (entity, rid) == ("contact", cid)
    prov = values["_company_provenance"]
    assert prov["source"] == "stated_company"
    assert prov["stated"] == "Velcee Consulting Ltd"
    assert prov["email_log_id"] == "log1"
    assert prov["previous_company_id"] is None


def test_ingest_freemail_changed_stated_company_relinks_promptly(monkeypatch):
    # THE bug this guards: sarah@gmail.com was at Pearson Plumbing, her latest
    # email signs off as Velcee Consulting — the zero-LLM ingest must move the
    # link there and then, not wait for a later intel pass.
    fake = FakeDb(
        monkeypatch,
        companies=[{"id": "co-pearson", "domain": None,
                    "name": "Pearson Plumbing & Heating",
                    "last_activity_at": "2026-07-20T00:00:00+00:00"}],
        contacts=[{"id": "ct1", "email": "sarah@gmail.com", "name": "Sarah",
                   "company_id": "co-pearson",
                   "last_activity_at": "2026-07-20T00:00:00+00:00"}])
    tally = crm.ingest_email_log_row(
        "t1", {**ROW, "from_address": "sarah@gmail.com",
               "contact_company": "Velcee Consulting Ltd",
               "created_at": "2026-07-26T17:05:00+00:00"})
    velcee = fake.companies["Velcee Consulting Ltd"]
    assert tally["company_id"] == velcee["id"]
    (cid, fields), = fake.contact_updates
    assert cid == "ct1" and fields["company_id"] == velcee["id"]
    (_, _, values), = fake.merged_custom
    prov = values["_company_provenance"]
    assert prov["previous_company_id"] == "co-pearson"
    assert prov["stated"] == "Velcee Consulting Ltd"
    assert prov["occurred_at"] == "2026-07-26T17:05:00+00:00"


def test_ingest_freemail_suffix_variant_neither_moves_nor_duplicates(monkeypatch):
    # "Velcee Consulting" is the SAME firm as the linked "Velcee Consulting Ltd"
    # (fuzzy resolution) — model wobble on the legal suffix must not churn the
    # link or mint a duplicate company.
    fake = FakeDb(
        monkeypatch,
        companies=[{"id": "co-velcee", "domain": None,
                    "name": "Velcee Consulting Ltd",
                    "last_activity_at": "2026-07-20T00:00:00+00:00"}],
        contacts=[{"id": "ct1", "email": "sarah@gmail.com", "name": "Sarah",
                   "company_id": "co-velcee",
                   "last_activity_at": "2026-07-20T00:00:00+00:00"}])
    crm.ingest_email_log_row(
        "t1", {**ROW, "from_address": "sarah@gmail.com",
               "contact_company": "Velcee Consulting",
               "created_at": "2026-07-26T17:05:00+00:00"})
    assert len(fake.companies) == 1
    assert all("company_id" not in f for _, f in fake.contact_updates)
    assert fake.merged_custom == []


def test_ingest_freemail_empty_stated_company_keeps_the_link(monkeypatch):
    # An email whose extraction found no company says nothing — never unlink.
    fake = FakeDb(
        monkeypatch,
        companies=[{"id": "co-velcee", "domain": None,
                    "name": "Velcee Consulting Ltd", "last_activity_at": None}],
        contacts=[{"id": "ct1", "email": "sarah@gmail.com", "name": "Sarah",
                   "company_id": "co-velcee", "last_activity_at": None}])
    crm.ingest_email_log_row(
        "t1", {**ROW, "from_address": "sarah@gmail.com", "contact_company": "  "})
    assert len(fake.companies) == 1
    assert all("company_id" not in f for _, f in fake.contact_updates)
    assert fake.merged_custom == []


def test_ingest_freemail_old_email_cannot_clobber_a_newer_link(monkeypatch):
    # A replayed/out-of-order OLD email stating the previous employer must not
    # move the link back — nor even orphan-create its company.
    fake = FakeDb(
        monkeypatch,
        companies=[{"id": "co-velcee", "domain": None,
                    "name": "Velcee Consulting Ltd",
                    "last_activity_at": "2026-07-26T17:05:00+00:00"}],
        contacts=[{"id": "ct1", "email": "sarah@gmail.com", "name": "Sarah",
                   "company_id": "co-velcee",
                   "last_activity_at": "2026-07-26T17:05:00+00:00"}])
    crm.ingest_email_log_row(
        "t1", {**ROW, "from_address": "sarah@gmail.com",
               "contact_company": "Pearson Plumbing & Heating",
               "created_at": "2026-07-01T00:00:00+00:00"})
    assert "Pearson Plumbing & Heating" not in fake.companies
    assert all("company_id" not in f for _, f in fake.contact_updates)
    assert fake.merged_custom == []


def test_ingest_business_domain_never_repins_an_existing_link(monkeypatch):
    # A business-domain sender whose contact was (deliberately) linked elsewhere
    # keeps that link — the domain guess only fills a blank, it never overrides.
    fake = FakeDb(
        monkeypatch,
        companies=[{"id": "co-acme-haulage.co.uk", "domain": "acme-haulage.co.uk",
                    "name": "Acme Haulage", "last_activity_at": None}],
        contacts=[{"id": "ct1", "email": "rob@acme-haulage.co.uk", "name": "Rob",
                   "company_id": "co-other", "last_activity_at": None}])
    crm.ingest_email_log_row("t1", dict(ROW))
    assert all("company_id" not in f for _, f in fake.contact_updates)
    assert fake.merged_custom == []


def test_ingest_never_regresses_last_activity(monkeypatch):
    fake = FakeDb(monkeypatch,
                  contacts=[{"id": "ct1", "email": "sarah@gmail.com", "name": "Sarah",
                             "company_id": None, "last_activity_at": "2026-07-04T00:00:00+00:00"}])
    crm.ingest_email_log_row("t1", {**ROW, "from_address": "sarah@gmail.com",
                                    "created_at": "2026-06-01T00:00:00+00:00"})
    assert all("last_activity_at" not in f for _, f in fake.contact_updates)


def test_backfill_is_idempotent_and_tallies(monkeypatch):
    fake = FakeDb(monkeypatch)
    rows = [dict(ROW), {**ROW, "id": "log2", "status": "sent",
                        "created_at": "2026-07-05T10:00:00+00:00"}]
    monkeypatch.setattr(crm.db, "crm_email_log_rows", lambda tid: rows)
    monkeypatch.setattr(crm, "ensure_default_stages", lambda tid, client=None: [])
    first = crm.backfill_tenant("t1")
    assert first == {"rows": 2, "activities": 3}   # 2 inbound + 1 outbound
    again = crm.backfill_tenant("t1")
    assert again == {"rows": 2, "activities": 0}   # dedupe: nothing doubles
    assert len(fake.activities) == 3


def test_backfill_survives_a_poison_row(monkeypatch):
    FakeDb(monkeypatch)
    boom = {"id": "bad"}   # no from_address — plus one that raises
    def explode(tid, row, own_addresses=None):
        if row.get("id") == "bad2":
            raise RuntimeError("boom")
        return {"activities": 1}
    rows = [dict(ROW), {**boom, "id": "bad2"}, {**ROW, "id": "log3"}]
    monkeypatch.setattr(crm.db, "crm_email_log_rows", lambda tid: rows)
    monkeypatch.setattr(crm, "ensure_default_stages", lambda tid, client=None: [])
    monkeypatch.setattr(crm, "ingest_email_log_row", explode)
    assert crm.backfill_tenant("t1") == {"rows": 3, "activities": 2}


def test_note_outbound_sent_reingests_as_sent(monkeypatch):
    fake = FakeDb(monkeypatch)
    monkeypatch.setattr(crm.db, "get_email_log_entry",
                        lambda log_id, tid, client=None: dict(ROW))
    crm.note_outbound_sent("t1", "log1")
    assert [a["direction"] for a in fake.activities] == ["inbound", "outbound"]
    monkeypatch.setattr(crm.db, "get_email_log_entry", lambda log_id, tid, client=None: None)
    crm.note_outbound_sent("t1", "gone")   # vanished row: a quiet no-op
    assert len(fake.activities) == 2


def test_stages_are_never_seeded(monkeypatch):
    """Julius 2026-09-02: no default pipeline. A tenant starts with ZERO
    stages — only the assistant's applied recommendations and manual adds
    create them — and an empty list is a real state, never 'fixed' by a read."""
    inserted = []
    monkeypatch.setattr(crm.db, "crm_list_stages", lambda tid, client=None: [])
    monkeypatch.setattr(crm.db, "crm_insert_stage",
                        lambda *a, **k: inserted.append(a))
    assert crm.ensure_default_stages("t1") == []
    assert crm.ensure_default_stages("t1") == []   # still empty, still no writes
    assert inserted == []
    assert not hasattr(crm, "DEFAULT_STAGES")      # the seed list itself is gone


# ── Non-email activity helpers (portal + diary → timeline, 2026-07-31) ───────

def test_note_contact_activity_appends_and_bumps_recency(monkeypatch):
    inserted, updated = [], []
    monkeypatch.setattr(crm.db, "crm_insert_activity",
                        lambda tid, **kw: inserted.append(kw) or True)
    monkeypatch.setattr(crm.db, "crm_update_contact",
                        lambda cid, tid, fields: updated.append(fields))
    contact = {"id": "c1", "company_id": "co1",
               "last_activity_at": "2020-01-01T00:00:00+00:00"}
    assert crm.note_contact_activity(
        "t1", contact, "portal_chat", summary="Asked on the portal: prices?")
    assert inserted[0]["contact_id"] == "c1"
    assert inserted[0]["company_id"] == "co1"
    assert inserted[0]["activity_type"] == "portal_chat"
    assert updated and "last_activity_at" in updated[0]
    # no contact / no id → a quiet no-op, never a raise
    assert crm.note_contact_activity("t1", None, "portal_chat") is False
    assert crm.note_contact_activity("t1", {}, "portal_chat") is False


def test_note_booking_activity_skips_non_email_contacts(monkeypatch):
    called = []
    monkeypatch.setattr(crm.db, "crm_get_or_create_contact",
                        lambda *a, **k: called.append(a) or {"id": "c1"})
    assert crm.note_booking_activity("t1", {"contact": "07700 900123"}) is False
    assert crm.note_booking_activity("t1", {"contact": None}) is False
    assert crm.note_booking_activity("t1", None) is False
    assert not called


def test_note_booking_activity_writes_calendar_event(monkeypatch):
    inserted = []
    monkeypatch.setattr(crm.db, "crm_get_or_create_contact",
                        lambda tid, email, **k: {"id": "c1", "company_id": None,
                                                 "last_activity_at": None})
    monkeypatch.setattr(crm.db, "crm_insert_activity",
                        lambda tid, **kw: inserted.append(kw) or True)
    monkeypatch.setattr(crm.db, "crm_update_contact", lambda *a, **k: None)
    booking = {"id": "b1", "contact": "S@X.com", "booking_date": "2026-08-01",
               "window": "am", "summary": "Boiler service",
               "booking_ref": "BK-1001"}
    assert crm.note_booking_activity("t1", booking)
    assert inserted[0]["activity_type"] == "calendar_event"
    assert inserted[0]["source_id"] == "b1"          # id-keyed: re-confirm dedupes
    assert "Booked for 2026-08-01 (am)" in inserted[0]["summary"]
    assert "BK-1001" in inserted[0]["summary"]
    # a move keys its own row via the slot suffix
    inserted.clear()
    crm.note_booking_activity("t1", booking, verb="Rescheduled",
                              source_suffix="2026-08-02:pm")
    assert inserted[0]["source_id"] == "b1:2026-08-02:pm"
