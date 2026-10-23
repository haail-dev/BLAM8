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


@pytest.mark.asyncio
async def test_excluded_stale_hours_never_reach_stored_chunks_or_prompt(source_db, monkeypatch):
    """Reproduce the saved 4 pm body / 5 pm evidence contradiction on real SQL."""
    from app import knowledge_review as admission, knowledge_fidelity, knowledge as kb, setup_card_knowledge
    old = 'Our office is open Monday to Friday, 9 am to 5 pm UK time, excluding public holidays in England.'
    rest = 'Routine messages are reviewed on the next working day. Email contact@example.test. Include your property address.'
    row, evidence = seeded()
    row['payload']['entries'] = [dict(row['payload']['entries'][0], label='Hours and Contact',
        content=old + ' ' + rest, provenance=[{'source':'https://example.test/hours', 'excerpt':old + ' ' + rest}])]
    row['payload']['source_units'] = knowledge_fidelity.inventory([{'url':'https://example.test/hours','text':old + ' ' + rest}])
    identity = row['payload']['entries'][0]['id']
    driver, dsn = source_db
    tenant = uuid.uuid4()
    with driver.connect(dsn, autocommit=True) as c:
        c.execute('insert into tenants values(%s)', (tenant,))
        c.execute("insert into tenant_config values(%s,'')", (tenant,))
        capture(c, tenant, row, evidence)
        ctx = c.execute('select knowledge_review_context(%s)', (tenant,)).fetchone()[0]
        form = {'version':ctx['version'], 'action_'+identity:'keep',
            'content_'+identity:(old + ' ' + rest).replace('5 pm','4 pm'),
            'source_'+row['payload']['source_units'][0]['id']:'exclude',
            'source_reason_'+row['payload']['source_units'][0]['id']:'Owner confirms closing time is now 4 pm.'}
        notes, payload = admission.plan(ctx, row, form)
        async def embed(texts): return [[0.1]*1024 for _ in texts], 0
        monkeypatch.setattr(kb, 'is_configured', lambda:True)
        monkeypatch.setattr(setup_card_knowledge.embeddings, 'embed_documents', embed)
        prepared, _ = await setup_card_knowledge.prepare({'notes':notes})
        assert c.execute('select admit_knowledge_review(%s,%s,%s,%s,%s)',
            (tenant,row['id'],ctx['version'],json.dumps(payload),json.dumps(prepared['notes']))).fetchone()[0]
        stored = c.execute('select source,content from knowledge_chunks where tenant_id=%s', (tenant,)).fetchall()
        async def embed_query(query):return [0.1]*1024, 0
        monkeypatch.setattr(kb.embeddings, 'embed_query', embed_query)
        monkeypatch.setattr(kb.usage, 'record_voyage', lambda *a,**k:None)
        monkeypatch.setattr(kb.db, 'match_knowledge_chunks', lambda *a,**k:[
            {'source':s,'content':t,'similarity':1.0} for s,t in c.execute(
                'select source,content from knowledge_chunks where tenant_id=%s',(tenant,)).fetchall()])
        prompt = kb.render_for_prompt(await kb.retrieve(str(tenant), 'office hours', whole_if_small=True))
        assert '4 pm' in prompt and '5 pm' not in prompt
        assert all(s in prompt for s in ('next working day','contact@example.test','property address'))
        audit = c.execute('select payload from knowledge_reviews where id=%s',(row['id'],)).fetchone()[0]
        assert '5 pm' in json.dumps(audit) and audit['source_decisions'][0]['status']=='excluded'


@pytest.mark.asyncio
async def test_owner_replacement_survives_atomic_apply_save_reload_and_stale_reread(source_db, monkeypatch):
    from tests.test_knowledge_owner_corrections import owner_case, REQUEST, BEFORE, REST
    from app import knowledge_review as admission, knowledge as kb, setup_card_knowledge
    row, evidence, card, raw = owner_case()
    driver, dsn = source_db
    tenant = uuid.uuid4()
    with driver.connect(dsn, autocommit=True) as c:
        c.execute('insert into tenants values(%s)',(tenant,))
        c.execute("insert into tenant_config values(%s,'')",(tenant,))
        capture(c,tenant,row,evidence)
        ctx = c.execute('select knowledge_review_context(%s)',(tenant,)).fetchone()[0]
        payload = review.corrected_payload(row,evidence,raw,card=card,owner_request=REQUEST,reviewer='owner')
        assert c.execute('select revise_knowledge_review_sources(%s,%s,%s,%s)',
            (tenant,row['id'],ctx['version'],json.dumps(payload))).fetchone()[0]
        with pytest.raises(driver.errors.RaiseException,match='STALE'):
            c.execute('select revise_knowledge_review_sources(%s,%s,%s,%s)',
                (tenant,row['id'],ctx['version'],json.dumps(payload)))
        ctx = c.execute('select knowledge_review_context(%s)',(tenant,)).fetchone()[0]
        row = next(r for r in ctx['reviews'] if r['id']==row['id'])
        identity = row['payload']['entries'][0]['id']
        form = {'version':ctx['version'],'action_'+identity:'keep'}
        notes, final = admission.plan(ctx,row,form)
        async def embed(texts):return [[0.1]*1024 for _ in texts],0
        monkeypatch.setattr(kb,'is_configured',lambda:True)
        monkeypatch.setattr(kb.embeddings,'embed_documents',embed)
        prepared,_ = await setup_card_knowledge.prepare({'notes':notes})
        assert c.execute('select admit_knowledge_review(%s,%s,%s,%s,%s)',
            (tenant,row['id'],ctx['version'],json.dumps(final),json.dumps(prepared['notes']))).fetchone()[0]
        saved = c.execute('select knowledge_review_context(%s)',(tenant,)).fetchone()[0]
        assert len(saved['sources'])==1 and '5 pm' not in saved['sources'][0]['content']
        assert '4 pm' in saved['sources'][0]['content'] and REST in saved['sources'][0]['content']
        with pytest.raises(driver.errors.RaiseException,match='STALE'):
            c.execute('select admit_knowledge_review(%s,%s,%s,%s,%s)',
                (tenant,row['id'],ctx['version'],json.dumps(final),json.dumps(prepared['notes'])))
        # A deliberate reread is another pending batch, requiring an explicit
        # overlap decision against the corrected saved fact. It cannot overwrite it.
        reread, _, _, _ = owner_case()
        saved['reviews'].append(reread)
        with pytest.raises(Exception,match='overlap'):
            admission.plan(saved,reread,{'version':saved['version'],
                'action_'+reread['payload']['entries'][0]['id']:'keep'})
        assert c.execute('select count(*) from knowledge_chunks where tenant_id=%s',(tenant,)).fetchone()[0]==1


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



def test_applied_paragraph_removal_recovers_explicit_choices_after_lost_response(source_db):
    from app import knowledge_note_edits, knowledge_source_evidence, knowledge_fidelity, knowledge_review
    driver, dsn = source_db
    tenant = uuid.uuid4(); row, _ = seeded()
    text = 'We review incoming requests.\n\nMention the earlier reference for a repeated request.'
    evidence = knowledge_source_evidence.build([{'url':'https://example.test/advice','text':text}])
    row['payload']['entries'] = [dict(row['payload']['entries'][0],content=text)]
    row['payload'].update(source_snapshot=evidence['id'],source_units=knowledge_fidelity.inventory(evidence['pages']))
    identity = row['payload']['entries'][0]['id']
    request = 'Please remove the final repeated paragraph and keep the rest.'
    with driver.connect(dsn,autocommit=True) as c:
        c.execute('insert into tenants values(%s)', (tenant,))
        c.execute("insert into tenant_config values(%s,'Owner brief')", (tenant,))
        capture(c,tenant,row,evidence)
        version=c.execute('select knowledge_review_context(%s)',(tenant,)).fetchone()[0]['version']
        context={'version':version,'reviews':[row],'sources':[],'brief':'Owner brief'}
        form={'version':version}
        card=knowledge_note_edits.scope(review.draft_card(row,evidence),context,row,form,identity,request)
        raw={'revision':1,'changes':[{'id':identity,'content_hash':knowledge_source_evidence.digest(text),
             'insertions':[],'citations':[],'removals':[card['knowledge_edit_scope']['paragraphs'][-1]['reference']]}],
             'overlaps':[],'questions':[],'edit_scope':knowledge_note_edits.binding(card)}
        payload=review.corrected_payload(row,evidence,raw,card=card,owner_request=request,reviewer='owner')
        assert c.execute('select revise_knowledge_review_sources(%s,%s,%s,%s)',(tenant,row['id'],version,json.dumps(payload))).fetchone()[0] is True
        # Simulate losing the HTTP acknowledgement: only committed SQL is read.
        state,stored=c.execute('select state,payload from knowledge_reviews where id=%s',(row['id'],)).fetchone()
        fresh=c.execute('select knowledge_review_context(%s)',(tenant,)).fetchone()[0]['version']
        assert fresh!=version and state=='pending'
        recovered=knowledge_review.review_view({**context,'version':fresh},{**row,'payload':stored})
        unit=row['payload']['source_units'][-1]
        assert recovered['form']['source_'+unit['id']]=='exclude'
        assert recovered['form']['source_reason_'+unit['id']]==request
        assert recovered['form']['action_'+identity]==''
        assert stored['source_units']==row['payload']['source_units']
        assert c.execute('select evidence from knowledge_review_sources where tenant_id=%s',(tenant,)).fetchone()[0]==evidence
        assert c.execute('select count(*) from knowledge_chunks where tenant_id=%s',(tenant,)).fetchone()[0]==0
        with pytest.raises(driver.errors.RaiseException,match='STALE'):
            c.execute("select admit_knowledge_review(%s,%s,%s,%s,'[]')",(tenant,row['id'],version,json.dumps(payload)))
