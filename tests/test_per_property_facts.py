"""A fact belongs to the thing it was said about (Kris 2026-09-04).

The plumbing run booked a boiler service at Sydney Buildings carrying the boiler
from the landlord's OTHER property at Holloway Road. Two separate defects, both
pinned here — and neither is a confidence problem: every value involved was true,
just true about something else.

1. **The record was corrupted first.** crm_fields autofill merged whatever it
   read into custom_data, last write wins, so the CP12 email's boiler replaced
   the customer's own. Extraction now fills an empty slot and OFFERS a
   contradiction (the same confirm chip a middling read gets), and the chip says
   what it would replace so the operator can actually decide.
2. **The portal was told to reuse everything.** Per-job facts rode in
   <customer_record> under "never re-ask — pass it into create_booking's
   details". They now travel in their own block with their own rule.

The full answer to "several properties" is requirement groups
(docs/modules/module-requirement-groups.md); these two are correct on their own terms
and stand whatever is decided there.

Run: python -m pytest tests/test_per_property_facts.py -q
"""
from unittest.mock import MagicMock

import pytest

from app import db, portal_chat, portal_data
from app.dashboard import _crm_annotate_suggestions


# ── 1. the record: fill an empty slot, offer a contradiction ────────────────
def _record(custom_data):
    """A db client whose crm_get_record returns one record, capturing updates."""
    updates = []
    client = MagicMock()
    table = client.table.return_value
    table.update.return_value.eq.return_value.eq.return_value.execute.side_effect = \
        lambda: updates.append(True)
    return client, updates, custom_data


def _merge(monkeypatch, stored, values, overwrite=True):
    client, updates, data = _record(stored)
    monkeypatch.setattr(db, "crm_get_record",
                        lambda entity, rid, tid, client=None: {"custom_data": data})
    conflicts = db.crm_merge_custom_data("contact", "c1", "t1", values,
                                         client=client, overwrite=overwrite)
    payload = client.table.return_value.update.call_args
    written = payload[0][0]["custom_data"] if payload else None
    return conflicts, written


def test_an_empty_slot_is_filled(monkeypatch):
    conflicts, written = _merge(monkeypatch, {}, {"boiler": "Worcester"}, overwrite=False)
    assert conflicts == {} and written["boiler"] == "Worcester"


def test_the_same_value_again_is_not_a_conflict(monkeypatch):
    conflicts, _ = _merge(monkeypatch, {"boiler": "Worcester"},
                          {"boiler": " Worcester "}, overwrite=False)
    assert conflicts == {}


def test_a_different_value_is_reported_and_not_written(monkeypatch):
    # The landlord's other property, arriving on a CP12 email.
    conflicts, written = _merge(monkeypatch, {"boiler": "Worcester Bosch"},
                                {"boiler": "Vaillant ecoTEC"}, overwrite=False)
    assert conflicts == {"boiler": "Worcester Bosch"}
    assert written is None                      # nothing to write → no update at all


def test_a_human_confirming_still_overwrites(monkeypatch):
    """accept_suggestion and the provenance stamp are deliberate writes — the
    guard is for what extraction does on its own."""
    conflicts, written = _merge(monkeypatch, {"boiler": "Worcester Bosch"},
                                {"boiler": "Vaillant ecoTEC"})
    assert conflicts == {} and written["boiler"] == "Vaillant ecoTEC"


def test_a_mixed_write_keeps_the_good_half(monkeypatch):
    conflicts, written = _merge(monkeypatch, {"boiler": "Worcester Bosch"},
                                {"boiler": "Vaillant", "access": "side gate"},
                                overwrite=False)
    assert conflicts == {"boiler": "Worcester Bosch"}
    assert written["access"] == "side gate" and written["boiler"] == "Worcester Bosch"


# ── the chip has to show what it would replace ──────────────────────────────
DEFS = [{"entity": "contact", "key": "boiler", "label": "Boiler make and model"}]


def test_a_conflicting_suggestion_says_what_it_replaces():
    out = _crm_annotate_suggestions(
        [{"entity": "contact", "field_key": "boiler", "value": "Vaillant ecoTEC"}],
        DEFS, {"contact": {"boiler": "Worcester Bosch"}})
    assert out[0]["replaces"] == "Worcester Bosch"


def test_a_plain_suggestion_has_nothing_to_replace():
    out = _crm_annotate_suggestions(
        [{"entity": "contact", "field_key": "boiler", "value": "Vaillant ecoTEC"}],
        DEFS, {"contact": {}})
    assert out[0]["replaces"] == ""
    # …and a suggestion echoing the stored value is not a change either
    same = _crm_annotate_suggestions(
        [{"entity": "contact", "field_key": "boiler", "value": "Vaillant ecoTEC"}],
        DEFS, {"contact": {"boiler": "Vaillant ecoTEC"}})
    assert same[0]["replaces"] == ""


def test_the_annotator_still_works_with_no_records():
    out = _crm_annotate_suggestions(
        [{"entity": "contact", "field_key": "boiler", "value": "x"}], DEFS)
    assert out[0]["label"] == "Boiler make and model" and out[0]["replaces"] == ""


# ── 2. the portal: job facts are not record facts ───────────────────────────
class _Session:
    tenant_id = "t1"
    profile = None

    def __init__(self, contact):
        self.contact = contact


@pytest.fixture
def portal_env(monkeypatch):
    monkeypatch.setattr(portal_data.db, "crm_list_field_definitions",
                        lambda tid, client=None: [
                            {"entity": "contact", "key": "boiler",
                             "label": "Boiler make and model"}])
    monkeypatch.setattr(portal_data.db, "crm_open_promises", lambda *a, **k: [])
    monkeypatch.setattr(portal_data.db, "bookings_for_contact", lambda tid, email: [
        {"booking_ref": "BK-1003", "booking_date": "2026-09-10",
         "details": {"Service": "Landlord gas safety certificate",
                     "Property address": "12 Holloway Road",
                     "Landlord or tenant contact details": "Sam Okafor, 07700 900111"}},
    ])


def test_record_facts_and_job_facts_come_back_apart(portal_env):
    session = _Session({"email": "k@x.co", "id": "c1", "phone": "07495 111222",
                        "custom_data": {"boiler": "Worcester Bosch"}})
    record, jobs = portal_data._profile_sections(session)
    assert "Phone: 07495 111222" in record
    assert "Boiler make and model: Worcester Bosch" in record
    assert all("Holloway" not in line for line in record)
    assert any("BK-1003" in line and "Holloway" in line for line in jobs)
    # _profile_lines keeps its old flat contract for callers that want one list
    assert portal_data._profile_lines(session) == record + jobs


def test_the_identity_tool_result_keeps_them_apart_too(portal_env):
    session = _Session({"email": "k@x.co", "id": "c1", "name": "Kris",
                        "reference_code": "HARTWE-375R", "custom_data": {}})
    view = portal_data._contact_view(session)
    assert "from_earlier_jobs" in view          # a key that says what it is
    assert any("Holloway" in ln for ln in view["from_earlier_jobs"])
    assert all("Holloway" not in ln for ln in view.get("on_file", []))


def test_the_prompt_gives_each_half_its_own_rule():
    p = portal_chat.build_portal_prompt(
        business_name="Hartwell",
        customer_profile=["Boiler make and model: Worcester Bosch"],
        customer_job_facts=["From their booking BK-1003 (CP12, 10th Sep 26): "
                            "Property address: 12 Holloway Road"])
    record_rule = p.split("<earlier_job_facts>")[0]
    job_rule = p.split("</earlier_job_facts>")[1]
    # The record keeps "never re-ask"; the job facts explicitly do not.
    assert "Never re-ask for anything listed above" in record_rule
    assert "These describe THAT JOB, not this customer" in job_rule
    assert "plainly the same place and the same work" in job_rule
    assert "never copy one into create_booking's `details` on a guess" in job_rule


def test_no_job_facts_means_no_block():
    p = portal_chat.build_portal_prompt(
        business_name="Hartwell", customer_profile=["Phone: 07495 111222"])
    assert "<earlier_job_facts>" not in p
    assert "<customer_record>" in p
