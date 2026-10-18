"""The booking-guarantees migration can be undone and re-applied on a disposable cluster."""
import json
import uuid

from tests.test_booking_capacity_database import MIGRATION, ROOT, cluster  # noqa: F401 — own module-scoped cluster

ROLLBACK = ROOT / "db/migrations/20260927_booking_guarantees.rollback.sql"

OWN_TERMS = {"name": "Own terms", "out_of_hours": {"price": 90, "terms": "The owner's words."}}
NO_TERMS = {"name": "Copied terms", "out_of_hours": {"price": 140}}
NO_POLICY = {"name": "No out of hours"}


def _objects(conn):
    q = "select count(*) from pg_proc where proname = %s"
    return {name: conn.execute(q, (name,)).fetchone()[0]
            for name in ("enforce_booking_capacity", "booking_span_bounds",
                         "classify_single_proof", "move_booking_if_capacity")}


def test_rollback_removes_the_guard_and_returns_the_owners_settings(cluster):  # noqa: F811
    driver, dsn = cluster
    tenant = uuid.uuid4()
    original = [OWN_TERMS, NO_TERMS, NO_POLICY]
    with driver.connect(dsn, autocommit=True) as conn:
        conn.execute("insert into scheduling_config(tenant_id, capacity_per_window, service_types) "
                     "values (%s, 1, %s)", (tenant, json.dumps(original)))
        # Forward again so the out-of-hours carry-over sees this business.
        conn.execute(MIGRATION.read_text(encoding="utf-8"))
        carried = conn.execute("select service_types from scheduling_config where tenant_id=%s",
                               (tenant,)).fetchone()[0]
        assert carried[1]["out_of_hours"]["carried_over_review"] == ["price_basis", "terms"]
        assert carried[0]["out_of_hours"]["carried_over_review"] == ["price_basis"]
        assert carried[0]["out_of_hours"]["terms"] == "The owner's words."

        conn.execute(ROLLBACK.read_text(encoding="utf-8"))
        conn.execute(ROLLBACK.read_text(encoding="utf-8"))   # safe to run twice

        assert _objects(conn) == {"enforce_booking_capacity": 0, "booking_span_bounds": 0,
                                  "classify_single_proof": 0, "move_booking_if_capacity": 1}
        body = conn.execute("select prosrc from pg_proc where proname='move_booking_if_capacity'"
                            ).fetchone()[0]
        assert "v_capacity := coalesce(v_capacity, 4)" in body   # the pre-Astra function
        restored = conn.execute("select service_types from scheduling_config where tenant_id=%s",
                                (tenant,)).fetchone()[0]
        assert restored == original
        # No guard: the old app's own checks decide again, so a second row is not refused.
        for _ in range(2):
            conn.execute("insert into bookings(tenant_id, booking_date, start_block, length_blocks) "
                         "values (%s, '2026-10-01', 40, 4)", (tenant,))

        # Re-deploying after a rollback works.
        conn.execute(MIGRATION.read_text(encoding="utf-8"))
        assert _objects(conn)["enforce_booking_capacity"] == 1


def test_rollback_keeps_terms_the_owner_already_reviewed(cluster):  # noqa: F811
    driver, dsn = cluster
    tenant = uuid.uuid4()
    reviewed = [{"name": "Reviewed", "out_of_hours": {"price": 140, "price_basis": "first_hour",
                                                       "terms": "Confirmed by the owner."}}]
    with driver.connect(dsn, autocommit=True) as conn:
        conn.execute("insert into scheduling_config(tenant_id, capacity_per_window, service_types) "
                     "values (%s, 1, %s)", (tenant, json.dumps(reviewed)))
        conn.execute(ROLLBACK.read_text(encoding="utf-8"))
        kept = conn.execute("select service_types from scheduling_config where tenant_id=%s",
                            (tenant,)).fetchone()[0]
        assert kept == reviewed
