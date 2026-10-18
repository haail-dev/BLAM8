"""Atomic admission and stale-review protection on disposable PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import uuid
import pytest
from tests.test_booking_capacity_database import cluster

ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def review_db(cluster):
    driver,dsn=cluster
    with driver.connect(dsn,autocommit=True) as c:
        c.execute('''create table tenants(id uuid primary key);
          create table tenant_config(tenant_id uuid primary key,business_brief text);
          create table knowledge_chunks(id uuid primary key default gen_random_uuid(),tenant_id uuid,
            source text,mailbox_id uuid,content text not null,embedding text,source_chunk_index integer,
            created_at timestamptz default now());
          create role anon;create role authenticated;create role service_role bypassrls;
          grant select,insert,update,delete on tenants,tenant_config,knowledge_chunks to service_role;''')
        sql=(ROOT/'db/migrations/20261004_setup_cards.sql').read_text()
        c.execute(sql[sql.index('create or replace function replace_knowledge_source_atomic('):
                      sql.index('create or replace function change_setup_card(')])
        c.execute('grant execute on function replace_knowledge_source_atomic(uuid,text,uuid,jsonb) to service_role')
        migration=(ROOT/'db/migrations/20261018_knowledge_review.sql').read_text()
        c.execute(migration);c.execute(migration)
    return driver,dsn


@pytest.fixture
def review_case(review_db):
    driver,dsn=review_db;tenant,review=uuid.uuid4(),uuid.uuid4()
    with driver.connect(dsn,autocommit=True) as c:
        c.execute('insert into tenants values (%s)',(tenant,))
        c.execute("insert into tenant_config values (%s,'Original brief')",(tenant,))
        c.execute("insert into knowledge_reviews(id,tenant_id,payload) values (%s,%s,'{\"entries\":[]}')",(review,tenant))
        version=c.execute('select knowledge_review_context(%s)',(tenant,)).fetchone()[0]['version']
    return review_db,tenant,review,version


def admit(case,notes,tenant_override=None):
    (driver,dsn),tenant,review,version=case
    with driver.connect(dsn,autocommit=True) as c:
        c.execute('set role service_role')
        return c.execute("select admit_knowledge_review(%s,%s,%s,%s::jsonb,%s::jsonb,false)",
            (tenant_override or tenant,review,version,json.dumps({'entries':[],'decisions':'explicit'}),json.dumps(notes))).fetchone()[0]


def note(label='Reviewed',content='Facts from an approved source.'):
    return {'label':label,'mailbox_id':None,'chunks':[{'content':content,'embedding':'[0.1]'}]}


def test_concurrent_review_replay_admits_exactly_once(review_case):
    def attempt(_):
        try:return admit(review_case,[note()])
        except Exception:return False
    with ThreadPoolExecutor(max_workers=4) as pool:assert sum(pool.map(attempt,range(4)))==1
    (driver,dsn),tenant,review,_=review_case
    with driver.connect(dsn) as c:
        assert c.execute('select count(*) from knowledge_chunks where tenant_id=%s',(tenant,)).fetchone()[0]==1
        assert c.execute('select state from knowledge_reviews where id=%s',(review,)).fetchone()[0]=='saved'


@pytest.mark.parametrize('change',['brief','saved','pending'])
def test_stale_saved_brief_or_other_pending_evidence_saves_nothing(review_case,change):
    (driver,dsn),tenant,review,_=review_case
    with driver.connect(dsn,autocommit=True) as c:
        if change=='brief':c.execute("update tenant_config set business_brief='Changed brief' where tenant_id=%s",(tenant,))
        elif change=='saved':c.execute("insert into knowledge_chunks(tenant_id,source,content) values (%s,'New source','New facts')",(tenant,))
        else:c.execute("insert into knowledge_reviews(tenant_id,payload) values (%s,'{\"entries\":[]}')",(tenant,))
    with pytest.raises(Exception,match='BLAM8_KNOWLEDGE_REVIEW_STALE'):admit(review_case,[note()])
    with driver.connect(dsn) as c:
        assert c.execute("select count(*) from knowledge_chunks where tenant_id=%s and source='Reviewed'",(tenant,)).fetchone()[0]==0


def test_one_bad_note_rolls_back_all_notes_and_review_decision(review_case):
    (driver,dsn),tenant,review,_=review_case
    with pytest.raises(Exception):admit(review_case,[note(),note('Invalid',None)])
    with driver.connect(dsn) as c:
        assert c.execute('select count(*) from knowledge_chunks where tenant_id=%s',(tenant,)).fetchone()[0]==0
        assert c.execute('select state from knowledge_reviews where id=%s',(review,)).fetchone()[0]=='pending'


def test_cross_tenant_and_browser_role_cannot_admit(review_case):
    with pytest.raises(Exception,match='BLAM8_KNOWLEDGE_REVIEW_STALE'):admit(review_case,[note()],uuid.uuid4())
    (driver,dsn),tenant,review,version=review_case
    with driver.connect(dsn,autocommit=True) as c:
        c.execute('set role authenticated')
        with pytest.raises(driver.errors.InsufficientPrivilege):
            c.execute("select admit_knowledge_review(%s,%s,%s,'{}','[]',false)",(tenant,review,version))
