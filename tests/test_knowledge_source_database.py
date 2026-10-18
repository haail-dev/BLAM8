"""Source immutability, tenant scope, atomic capture and admission races on local PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import uuid
import pytest
from tests.test_knowledge_review_database import review_db
from tests.test_booking_capacity_database import cluster
from tests.test_knowledge_source_review import seeded, corrections
from app import knowledge_source_review as review

ROOT = Path(__file__).resolve().parents[1]
FORWARD = ROOT/'db/migrations/20261018_knowledge_source_review.sql'
ROLLBACK = ROOT/'db/migrations/20261018_knowledge_source_review.rollback.sql'


@pytest.fixture(scope='module')
def source_db(review_db):
    driver, dsn = review_db
    with driver.connect(dsn, autocommit=True) as c:
        c.execute(FORWARD.read_text()); c.execute(FORWARD.read_text())
    return driver, dsn


def capture(c, tenant, row, evidence):
    return c.execute('select capture_knowledge_review_sources(%s,%s,%s,%s)',
        (tenant,row['id'],json.dumps(row['payload']),json.dumps(evidence))).fetchone()[0]


@pytest.fixture
def source_case(source_db):
    driver, dsn = source_db; tenant = uuid.uuid4(); row, evidence = seeded()
    with driver.connect(dsn, autocommit=True) as c:
        c.execute('insert into tenants values(%s)', (tenant,))
        c.execute("insert into tenant_config values(%s,'Owner brief')", (tenant,))
        capture(c, tenant, row, evidence)
        version = c.execute('select knowledge_review_context(%s)', (tenant,)).fetchone()[0]['version']
    return source_db, tenant, row, evidence, version


def test_failed_capture_leaves_no_partial_review_and_raw_evidence_cannot_be_replaced(source_case):
    (driver, dsn), tenant, row, evidence, version = source_case
    with driver.connect(dsn, autocommit=True) as c:
        other = dict(row, id=str(uuid.uuid4()))
        with pytest.raises(driver.errors.UniqueViolation): capture(c, tenant, other, evidence)
        assert c.execute('select count(*) from knowledge_reviews where id=%s', (other['id'],)).fetchone()[0] == 0
        with pytest.raises(driver.errors.RaiseException, match='IMMUTABLE'):
            c.execute("update knowledge_review_sources set evidence='{}' where tenant_id=%s", (tenant,))
        assert c.execute('select evidence from knowledge_review_sources where tenant_id=%s', (tenant,)).fetchone()[0] == evidence
        assert c.execute('select count(*) from knowledge_chunks where tenant_id=%s', (tenant,)).fetchone()[0] == 0


def test_browser_roles_cannot_read_or_write_other_tenants_and_purged_ids_cannot_be_reused(source_case):
    (driver, dsn), tenant, row, evidence, version = source_case
    with driver.connect(dsn, autocommit=True) as c:
        c.execute('set role authenticated')
        with pytest.raises(driver.errors.InsufficientPrivilege): c.execute('select * from knowledge_review_sources')
        with pytest.raises(driver.errors.InsufficientPrivilege): capture(c, tenant, row, evidence)
        c.execute('reset role'); c.execute('set role service_role')
        c.execute('delete from knowledge_review_sources where tenant_id=%s', (tenant,))
        c.execute('create temporary table knowledge_review_source_ids(tenant_id uuid,id uuid)')
        other = dict(row, id=str(uuid.uuid4()))
        with pytest.raises(driver.errors.UniqueViolation): capture(c, tenant, other, evidence)
        c.execute('reset role')
        assert c.execute('select count(*) from public.knowledge_review_source_ids where tenant_id=%s', (tenant,)).fetchone()[0] == 1


def test_concurrent_corrections_apply_once_and_invalidate_final_admission_preview(source_case):
    (driver, dsn), tenant, row, evidence, version = source_case
    payload = review.corrected_payload(row, evidence, corrections(review.draft_card(row,evidence), evidence))
    def attempt(_):
        with driver.connect(dsn, autocommit=True) as c:
            try:
                return c.execute('select revise_knowledge_review_sources(%s,%s,%s,%s)',
                    (tenant,row['id'],version,json.dumps(payload))).fetchone()[0]
            except driver.errors.RaiseException as exc:
                assert 'STALE' in str(exc); return False
    with ThreadPoolExecutor(max_workers=2) as pool: assert sum(pool.map(attempt,range(2))) == 1
    with driver.connect(dsn, autocommit=True) as c:
        with pytest.raises(driver.errors.RaiseException, match='STALE'):
            c.execute("select admit_knowledge_review(%s,%s,%s,%s,'[]')", (tenant,row['id'],version,json.dumps(payload)))
        assert c.execute('select state,payload from knowledge_reviews where id=%s', (row['id'],)).fetchone() == ('pending',payload)
        assert c.execute('select count(*) from knowledge_chunks where tenant_id=%s', (tenant,)).fetchone()[0] == 0
        # The normal atomic admission path remains available after a fresh review.
        fresh = c.execute('select knowledge_review_context(%s)', (tenant,)).fetchone()[0]['version']
        assert c.execute("select admit_knowledge_review(%s,%s,%s,%s,'[]')", (tenant,row['id'],fresh,json.dumps(payload))).fetchone()[0] is True


def test_cross_tenant_deleted_or_rebound_source_cannot_be_applied(source_case):
    (driver, dsn), tenant, row, evidence, version = source_case
    payload = review.corrected_payload(row,evidence,corrections(review.draft_card(row,evidence),evidence))
    with driver.connect(dsn, autocommit=True) as c:
        with pytest.raises(driver.errors.RaiseException, match='STALE'):
            c.execute('select revise_knowledge_review_sources(%s,%s,%s,%s)', (uuid.uuid4(),row['id'],version,json.dumps(payload)))
        changed = dict(payload, source_snapshot=str(uuid.uuid4()))
        with pytest.raises(driver.errors.RaiseException, match='SOURCE_INVALID'):
            c.execute('select revise_knowledge_review_sources(%s,%s,%s,%s)', (tenant,row['id'],version,json.dumps(changed)))
        c.execute('delete from knowledge_review_sources where tenant_id=%s', (tenant,))
        with pytest.raises(driver.errors.RaiseException, match='SOURCE_INVALID'):
            c.execute('select revise_knowledge_review_sources(%s,%s,%s,%s)', (tenant,row['id'],version,json.dumps(payload)))


def test_rollback_preserves_admission_drafts_and_can_reapply(source_db):
    driver, dsn = source_db
    with driver.connect(dsn, autocommit=True) as c:
        # Transactional rollback verification does not affect other cases.
        c.execute('begin')
        count = c.execute('select count(*) from knowledge_reviews').fetchone()[0]
        c.execute(ROLLBACK.read_text().replace('begin;', '').replace('commit;', ''))
        assert c.execute("select to_regclass('knowledge_review_sources')").fetchone()[0] is None
        assert c.execute('select count(*) from knowledge_reviews').fetchone()[0] == count
        c.execute(FORWARD.read_text().replace('begin;', '').replace('commit;', ''))
        assert c.execute("select to_regclass('knowledge_review_sources')").fetchone()[0]
        c.execute('rollback')



def test_final_approval_and_source_correction_cannot_both_win(source_case):
    from threading import Barrier
    (driver, dsn), tenant, row, evidence, version = source_case
    payload = review.corrected_payload(row,evidence,corrections(review.draft_card(row,evidence),evidence))
    barrier = Barrier(2)
    def attempt(operation):
        with driver.connect(dsn, autocommit=True) as c:
            barrier.wait(timeout=10)
            try:
                if operation == 'correct':
                    c.execute('select revise_knowledge_review_sources(%s,%s,%s,%s)',
                        (tenant,row['id'],version,json.dumps(payload)))
                else:
                    c.execute("select admit_knowledge_review(%s,%s,%s,%s,'[]')",
                        (tenant,row['id'],version,json.dumps(row['payload'])))
                return operation
            except driver.errors.RaiseException as exc:
                assert 'STALE' in str(exc)
                return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        winners = [value for value in pool.map(attempt,['correct','approve']) if value]
    assert len(winners) == 1
    with driver.connect(dsn) as c:
        state, stored = c.execute('select state,payload from knowledge_reviews where id=%s',(row['id'],)).fetchone()
        assert state == ('pending' if winners[0] == 'correct' else 'saved')
        assert stored == (payload if winners[0] == 'correct' else row['payload'])
