"""Run #5 (hot tubs, 2026-09-04) P2 items 12, 16, 18."""
from app import agent, crm_intel
import app.dashboard as dashboard


def test_a_refused_request_is_not_a_promise():
    assert "refuses or redirects" in crm_intel.INTEL_SYSTEM if hasattr(crm_intel, "INTEL_SYSTEM") else True
    import inspect
    src = inspect.getsource(crm_intel)
    assert "is NOT a commitment on either side" in src


def test_one_diary_check_per_visit_rule_is_in_the_drafter():
    import inspect
    src = agent.build_system_prompt({}, scheduling=True)
    assert "ONE check_availability with `services` as a list" in src


def test_manual_add_links_named_dependants(monkeypatch):
    linked, written = {}, {}
    row = {"id": "b-9", "details": {"Service": "Routine service"}}
    monkeypatch.setattr(dashboard.db, "list_requirement_groups",
                        lambda t, client=None: [{"id": "g1", "label": "Hot tub", "label_plural": "Hot tubs",
                                                 "cardinality": "many",
                                                 "fields": [{"key": "make_and_model", "label": "Make and model",
                                                             "type": "text", "durability": "once"}]}])
    monkeypatch.setattr(dashboard.db, "crm_contact_by_email", lambda t, e: {"id": "c1"})
    monkeypatch.setattr(dashboard.db, "list_requirement_instances",
                        lambda t, c: [{"id": "i1", "group_id": "g1", "name": "The Byre"},
                                      {"id": "i2", "group_id": "g1", "name": "The Dairy"},
                                      {"id": "i3", "group_id": "g1", "name": "The Granary"}])
    monkeypatch.setattr(dashboard.db, "link_booking_subjects", lambda t, bid, ids: linked.update(bid=bid, ids=ids))
    monkeypatch.setattr(dashboard.db, "update_booking_details",
                        lambda bid, t, details, summary=None: written.update(details))
    names = dashboard._link_manual_subjects("t1", row, "kris@x.co", "the byre, Dairy and something else")
    assert names == ["The Byre", "The Dairy"]
    assert linked == {"bid": "b-9", "ids": ["i1", "i2"]}
    assert written["Hot tubs"] == "The Byre, The Dairy"


def test_manual_add_without_names_or_email_links_nothing():
    assert dashboard._link_manual_subjects("t1", {"id": "b-9"}, None, "The Byre") == []
    assert dashboard._link_manual_subjects("t1", {"id": "b-9"}, "kris@x.co", "") == []
