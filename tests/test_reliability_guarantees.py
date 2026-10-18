"""Failure outcomes: never turn an unavailable check into permission."""
from types import SimpleNamespace

import pytest

from app import db, ladders, pipeline, services
from app.booking_errors import VerificationUnavailable
from app import requirement_groups as rg
from tests.test_service_executor import _exec
from tests.test_diary_settings_save import make_saved, saved  # noqa: F401
from tests.test_documents_routes import client, DOC  # noqa: F401


def unavailable(*a, **k):
    raise RuntimeError("test storage unavailable")


def test_ladder_storage_failure_is_not_permission(monkeypatch):
    monkeypatch.setattr(db, "crm_list_field_definitions", unavailable)
    with pytest.raises(VerificationUnavailable, match="required level"):
        ladders.contact_prerequisite_refusal("t1", "customer@example.invalid", "Visit")


def test_unknown_level_holds_only_provisionally_and_requires_owner(monkeypatch):
    inserted = []
    execute, state = _exec(monkeypatch, [], inserted)
    monkeypatch.setattr(db, "crm_list_field_definitions", unavailable)
    out = execute("hold_booking", {"date": "2026-06-17", "window": "10:00", "service": "Cut"})
    assert out["error"] == "verification_unavailable"
    assert out["owner_review"] and out["reserved"] and not out["held"]
    assert inserted and all(row["status"] == "offered" for row in inserted)
    assert state["verification_unavailable"]


def test_pipeline_discards_a_model_confirmation_after_failed_check():
    result = SimpleNamespace(text="You're booked!", blocked="", deferred=True, missing="details")
    pipeline._verification_hold(result, {"verification_unavailable": "I couldn't check the required documents."})
    assert result.text == "" and result.blocked
    assert result.thread_state == "needs_attention" and not result.deferred


def test_database_capacity_conflict_never_returns_a_success(monkeypatch):
    execute, state = _exec(monkeypatch, [], [])
    def conflict(*a, **k):
        raise RuntimeError("BLAM8_CAPACITY_CONFLICT: private database diagnostics")
    monkeypatch.setattr(db, "insert_stay_booking", conflict)
    out = execute("hold_booking", {"date": "2026-06-17", "window": "10:00", "service": "Cut"})
    assert out["error"] == "full" and not out["held"]
    assert "private database" not in str(out)


def test_a_document_for_one_requirement_does_not_satisfy_another():
    group = rg._ensure({"label": "Participant", "fields": [
        {"label": label, "key": key, "type": "date", "durability": "expires", "gate": True, "proof": True}
        for label, key in [("Permission", "permission"), ("Certificate", "certificate")]
    ]})
    gaps = rg.missing_for(group, {"permission": "2027-01-01", "certificate": "2027-01-01"},
                          "2026-10-01", has_proof={"permission"})
    assert len(gaps) == 1 and "Certificate" in gaps[0]
    assert len(rg.missing_for(group, {"permission": "2027-01-01", "certificate": "2027-01-01"},
                              "2026-10-01", has_proof=set())) == 2


def test_settings_write_failure_does_not_redirect_to_saved(saved, monkeypatch):
    client, calls = saved
    monkeypatch.setattr(db, "upsert_scheduling_config", unavailable)
    monkeypatch.setattr(db, "apply_booking_settings", unavailable)
    out = client.post("/app/settings/scheduling", data={"windows": "slots"}, follow_redirects=False)
    assert out.status_code == 503 and "not saved" in out.json()["detail"]
    assert not calls


def test_row_only_diary_save_checks_revision_at_write_time(saved, monkeypatch):
    client, calls = saved
    attempted = []
    def raced(tid, patch, revision, **kw):
        attempted.append((patch, revision))
        raise RuntimeError("BLAM8_SETTINGS_STALE")
    monkeypatch.setattr(db, "apply_booking_settings", raced)
    out = client.post("/app/settings/scheduling", data={"service_name": "Review", "service_minutes": "45", "booking_revision": "0"}, follow_redirects=False)
    assert out.status_code == 409 and not calls
    assert attempted[0][1] == 0 and attempted[0][0]["booking_revision"] == 1


def test_out_of_hours_price_basis_and_terms_are_owned_by_business():
    policy = services.clean_out_of_hours({"until": "22:00", "price": 90,
                                         "price_basis": "booking", "terms": "Materials included."})
    assert services.ooh_price_label(policy) == "£90 per booking"
    assert policy["terms"] == "Materials included."
    fresh = services.clean_out_of_hours({"until": "22:00", "price": 90})
    assert "terms" not in fresh and services.ooh_price_label(fresh) == "£90"


def test_required_document_read_failure_is_distinct_from_no_document(monkeypatch):
    monkeypatch.setattr(db, "db", unavailable)
    with pytest.raises(VerificationUnavailable, match="required documents"):
        db.list_documents("t1", contact_id="c1", strict=True)


def test_daily_limit_read_failure_is_not_zero_bookings(monkeypatch):
    from tests.test_service_rules import _exec as ruled_executor
    execute = ruled_executor(monkeypatch)
    monkeypatch.setattr(db, "count_service_bookings_by_day", unavailable)
    out = execute("check_appointment_availability", {
        "service": "Meet & greet", "date_from": "2026-11-03", "date_to": "2026-11-03"})
    assert out["error"] == "verification_unavailable" and out["owner_review"]


@pytest.mark.parametrize("key,contact,status", [("certificate", "c1", 303),
                                                ("unrelated", "c1", 422),
                                                ("certificate", "c2", 422)])
def test_evidence_links_validate_requirement_and_customer(client, monkeypatch, key, contact, status):
    monkeypatch.setattr(db, "get_document", lambda *a, **k: {**DOC, "contact_id": "c1"})
    monkeypatch.setattr(db, "get_requirement_instance", lambda *a, **k:
                        {"id": "i1", "group_id": "g1", "contact_id": contact})
    monkeypatch.setattr(db, "list_requirement_groups", lambda *a, **k: [
        {"id": "g1", "label": "Participant", "fields": [
            {"label": "Certificate", "key": "certificate", "durability": "expires", "gate": True, "proof": True}]}])
    writes = []
    monkeypatch.setattr(db, "update_document", lambda *a, **k: writes.append(a[2]))
    result = client.post("/app/crm/documents/d1/update", data={
        "instance_id": "i1", "requirement_keys": [key]}, follow_redirects=False)
    assert result.status_code == status
    assert bool(writes) == (status == 303)
