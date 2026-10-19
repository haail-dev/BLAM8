"""Real transactions: tenant isolation, card races and atomic apply receipts."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import uuid
import pytest
from tests.test_booking_capacity_database import ROOT, cluster as base_cluster

FORWARD = ROOT / "db/migrations/20261004_setup_cards.sql"
ROLLBACK = ROOT / "db/migrations/20261004_setup_cards.rollback.sql"
DIARY_MODE_FORWARD = ROOT / "db/migrations/20261019_diary_booking_mode_setup_cards.sql"
DIARY_MODE_ROLLBACK = ROOT / "db/migrations/20261019_diary_booking_mode_setup_cards.rollback.sql"


@pytest.fixture
def cluster():
    yield from base_cluster.__wrapped__()


def initialise(conn):
    conn.execute("""
        create table tenants(id uuid primary key);
        create table knowledge_chunks(id uuid primary key default gen_random_uuid(),
          tenant_id uuid references tenants(id), source text, mailbox_id uuid,
          content text, embedding text, created_at timestamptz default now());
        create table tenant_config(tenant_id uuid primary key references tenants(id), unrelated text, crm_quiet_days integer, quote_auto_draft text not null default 'propose');
        create table crm_stages(id uuid primary key, tenant_id uuid references tenants(id), name text,
          display_order integer not null default 0, stage_type text not null default 'active', created_at timestamptz default now());
        alter table scheduling_config add column booking_revision bigint not null default 0;
        alter table scheduling_config add column updated_at timestamptz;
        alter table scheduling_config add column intake_configured_at timestamptz;
        alter table scheduling_config add column dependants_configured_at timestamptz;
        alter table scheduling_config add column booking_setting_review jsonb not null default '{}';
        alter table scheduling_config add column timezone text;
        alter table scheduling_config add column working_days text;
        alter table scheduling_config add column windows text;
        alter table scheduling_config add column min_notice_hours int;
        alter table scheduling_config add column horizon_days int;
        alter table scheduling_config add column blackout_dates date[];
        alter table scheduling_config add column bank_holiday_nation text;
        alter table scheduling_config add column job_done_when text;
        alter table scheduling_config add column stay_pricing jsonb;
        alter table scheduling_config add column day_hours jsonb;
        alter table scheduling_config add column party_size jsonb;
        alter table scheduling_config add column recurring jsonb;
        alter table scheduling_config add column delivery jsonb;
        alter table scheduling_config add column credits jsonb;
        alter table scheduling_config add column courses jsonb;
        alter table scheduling_config add column makeups jsonb;
        alter table scheduling_config add column nouns jsonb;
        alter table requirement_groups add column label text;
        alter table requirement_groups add column label_plural text default '';
        alter table requirement_groups add column cardinality text default 'many';
        alter table requirement_groups add column display_order int default 0;
        alter table requirement_groups add column created_at timestamptz default now();
        alter table requirement_groups add column updated_at timestamptz default now();
        alter table scheduling_config alter column capacity_per_window drop default;
        alter table scheduling_config alter column open_time drop default;
        alter table scheduling_config alter column close_time drop default;
        create function auth_tenant_ids() returns setof uuid language sql stable as
          'select nullif(current_setting(''test.tenant'', true), '''')::uuid';
    """)
    schema = (ROOT / "db/schema.sql").read_text(encoding="utf-8")
    conn.execute(schema[schema.index("create table if not exists invoice_settings ("):
                        schema.index("alter table invoice_settings enable row level security;")])
    conn.execute('alter table invoice_settings add column default_quote_valid_months integer not null default 30; '
                 'alter table invoice_settings add column default_deposit_percent numeric(5,2); '
                 'alter table invoice_settings add column bank_account_name text; '
                 'alter table invoice_settings add column bank_sort_code text; '
                 'alter table invoice_settings add column bank_account_number text;')
    conn.execute(schema[schema.index("create table if not exists crm_field_definitions ("):
                        schema.index("-- Packages and the wallet")])
    conn.execute(FORWARD.read_text(encoding="utf-8"))


def card(revision, page="diary_settings"):
    return {"page": page, "revision": revision, "lines": []}


def test_service_exceptions_and_task_rules_share_one_atomic_receipt(cluster):
    from tests.test_setup_card_service_rules import apply, rule, ooh
    from tests.test_setup_card_scalars import setting
    driver, dsn = cluster
    tenant = uuid.uuid4()
    with driver.connect(dsn, autocommit=True) as conn:
        initialise(conn)
        conn.execute('insert into tenants values(%s)', (tenant,))
        conn.execute('insert into scheduling_config(tenant_id,service_types) values(%s,%s)',
                     (tenant, json.dumps([{'name': 'Review', 'minutes': 30, 'price': 60, 'future': {'keep': 1}}])))
        conn.execute("create table existing_completion_tasks(id integer primary key, title text, due_date date)")
        conn.execute("insert into existing_completion_tasks values(1,'Existing',current_date)")
        save(conn, tenant, 0)
        def snapshot(): return conn.execute('select to_jsonb(s) from scheduling_config s').fetchone()[0]
        before = snapshot()
        _, patch = apply(rule(enabled=True, same_day_until='14:00', notice_hours=0), ooh(),
                         rule('service_after_job', tasks=['Send report — 2 working days']), setting('job_done_when', 'marked'), cfg=before)
        conn.execute("insert into setup_card_revisions values(%s,'diary_settings',2,'{}',now())", (tenant,))
        with pytest.raises(driver.errors.UniqueViolation): save(conn, tenant, 1, expected=before, patch=patch)
        assert snapshot() == before
        conn.execute('delete from setup_card_revisions where revision=2')
        save(conn, tenant, 1, expected=before, patch=patch)
        saved = snapshot()
        service = saved['service_types'][0]
        assert service['future'] == {'keep': 1} and service['notice_hours'] == 0
        assert service['out_of_hours']['price_basis'] == 'booking'
        assert service['after_job'][0]['unit'] == 'working'
        assert saved['booking_revision'] == before['booking_revision'] + 1
        assert conn.execute('select count(*) from existing_completion_tasks').fetchone() == (1,)
        with pytest.raises(driver.errors.RaiseException, match='SETTINGS_STALE'):
            save(conn, tenant, 2, expected=before, patch=patch)


def test_progress_ladders_atomic_contact_and_subject_updates_preserve_values(cluster):
    from tests.test_setup_card_ladders import apply, row
    from tests.test_setup_cards import append
    driver, dsn = cluster
    tenant = uuid.uuid4()
    with driver.connect(dsn, autocommit=True) as conn:
        initialise(conn)
        conn.execute('insert into tenants values(%s)', (tenant,))
        conn.execute('insert into scheduling_config(tenant_id) values(%s)', (tenant,))
        save(conn, tenant, 0, page='dependants')
        def snapshot():
            return {'config': conn.execute('select to_jsonb(s) from scheduling_config s').fetchone()[0],
                    'groups': conn.execute("select coalesce(jsonb_agg(to_jsonb(g) order by id),'[]') from requirement_groups g").fetchone()[0],
                    'definitions': conn.execute("select coalesce(jsonb_agg(to_jsonb(d) order by id),'[]') from crm_field_definitions d").fetchone()[0]}
        before = snapshot()
        _, patch = apply(append('group', label='Asset', plural='Assets', cardinality='many'), row('Asset'), row(), saved=before)
        conn.execute("insert into setup_card_revisions values(%s,'dependants',2,'{}',now())", (tenant,))
        with pytest.raises(driver.errors.UniqueViolation):
            save(conn, tenant, 1, page='dependants', expected=before, patch=patch)
        assert snapshot() == before
        conn.execute('delete from setup_card_revisions where revision=2')
        save(conn, tenant, 1, page='dependants', expected=before, patch=patch)
        saved = snapshot()
        conn.execute('create table crm_contacts(id uuid primary key, custom_data jsonb)')
        conn.execute("insert into crm_contacts values(%s,'{\"progress\":\"Stage 1\"}')", (uuid.uuid4(),))
        conn.execute('alter table requirement_instances add column data jsonb')
        conn.execute("insert into requirement_instances(id,tenant_id,group_id,data) values(%s,%s,%s,'{\"progress\":\"Stage 2\"}')",
                     (uuid.uuid4(), tenant, saved['groups'][0]['id']))
        _, correction = apply(row('Asset', levels=['Stage 1','Stage 2','Stage 3']), row(notify='off'), saved=saved)
        conn.execute("update crm_field_definitions set extraction_hint='Concurrent'")
        with pytest.raises(driver.errors.RaiseException, match='SETTINGS_STALE'):
            save(conn, tenant, 2, page='dependants', expected=saved, patch=correction)
        save(conn, tenant, 2, page='dependants', expected=snapshot(), patch=correction)
        final = snapshot()
        assert final['definitions'][0]['id'] == saved['definitions'][0]['id']
        assert final['definitions'][0]['extraction_hint'] == 'Concurrent'
        assert final['definitions'][0]['ladder']['notify'] == 'off'
        assert final['groups'][0]['fields'][0]['options'] == ['Stage 1','Stage 2','Stage 3']
        assert conn.execute('select custom_data from crm_contacts').fetchone()[0] == {'progress':'Stage 1'}
        assert conn.execute('select data from requirement_instances').fetchone()[0] == {'progress':'Stage 2'}
        conn.execute(ROLLBACK.read_text(encoding='utf-8'))
        conn.execute(FORWARD.read_text(encoding='utf-8'))
        assert snapshot() == final


def test_knowledge_receipt_batch_atomicity_scopes_and_migration_cycle(cluster):
    driver, dsn = cluster
    tenant, other, mailbox = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with driver.connect(dsn, autocommit=True) as conn:
        initialise(conn)
        conn.execute('insert into tenants values(%s),(%s)', (tenant, other))
        conn.execute("insert into knowledge_chunks(tenant_id,source,mailbox_id,content) values"
                     "(%s,'Hours',null,'Old'),(%s,'Hours',%s,'Mailbox'),(%s,'Hours',null,'Other')",
                     (tenant, tenant, mailbox, other))
        save(conn, tenant, 0, page='knowledge')
        def snapshot():
            chunks = conn.execute("select coalesce(jsonb_agg(jsonb_build_object('id',id,'content',content) order by id),'[]') "
                                  "from knowledge_chunks where tenant_id=%s and source='Hours' and mailbox_id is null", (tenant,)).fetchone()[0]
            return {'sources': [{'label': 'Hours', 'chunks': chunks}, {'label': 'Terms', 'chunks': []}]}
        before = snapshot()
        patch = {'notes': [{'label': label, 'content': 'New', 'chunks': [{'content': 'New', 'embedding': '[0.1]'}]}
                           for label in ['Hours', 'Terms']]}
        conn.execute("insert into setup_card_revisions values(%s,'knowledge',2,'{}',now())", (tenant,))
        with pytest.raises(driver.errors.UniqueViolation):
            save(conn, tenant, 1, page='knowledge', expected=before, patch=patch)
        assert snapshot() == before
        assert conn.execute("select count(*) from knowledge_chunks where source='Terms'").fetchone() == (0,)
        conn.execute('delete from setup_card_revisions where revision=2')
        conn.execute("update knowledge_chunks set content='Concurrent' where tenant_id=%s and mailbox_id is null", (tenant,))
        with pytest.raises(driver.errors.RaiseException, match='SETTINGS_STALE'):
            save(conn, tenant, 1, page='knowledge', expected=before, patch=patch)
        save(conn, tenant, 1, page='knowledge', expected=snapshot(), patch=patch)
        assert conn.execute('select content from knowledge_chunks where mailbox_id=%s', (mailbox,)).fetchone() == ('Mailbox',)
        assert conn.execute('select content from knowledge_chunks where tenant_id=%s', (other,)).fetchone() == ('Other',)
        conn.execute("alter table knowledge_chunks add constraint no_invalid_content check(content <> 'Invalid')")
        def replace(rows):
            return conn.execute('select replace_knowledge_source_atomic(%s,%s,%s,%s)',
                                (tenant, 'Hours', None, json.dumps(rows))).fetchone()[0]
        row = {'tenant_id': str(tenant), 'source': 'Hours', 'mailbox_id': None, 'content': 'Invalid', 'embedding': '[0.2]'}
        with pytest.raises(driver.errors.CheckViolation): replace([row])
        assert snapshot()['sources'][0]['chunks'][0]['content'] == 'New'
        with pytest.raises(driver.errors.RaiseException, match='INVALID'): replace([row | {'tenant_id': str(other)}])
        assert replace([row | {'content': 'Imported'}]) == 1
        retained = conn.execute('select jsonb_agg(to_jsonb(k) order by id) from knowledge_chunks k').fetchone()[0]
        conn.execute(ROLLBACK.read_text(encoding='utf-8'))
        conn.execute(FORWARD.read_text(encoding='utf-8'))
        assert conn.execute('select jsonb_agg(to_jsonb(k) order by id) from knowledge_chunks k').fetchone()[0] == retained


def test_booking_mode_atomic_receipt_late_history_and_migration_cycle(cluster):
    from tests.test_setup_card_mode import apply
    driver, dsn = cluster
    tenant = uuid.uuid4()
    with driver.connect(dsn, autocommit=True) as conn:
        initialise(conn)
        conn.execute('insert into tenants values(%s)', (tenant,))
        conn.execute('insert into scheduling_config(tenant_id) values(%s)', (tenant,))
        save(conn, tenant, 0, page='booking_details')
        def snapshot(history=False):
            return {'config': conn.execute('select to_jsonb(s) from scheduling_config s').fetchone()[0],
                    'definitions': [], 'has_bookings': history}
        before = snapshot()
        _, patch = apply(True, before['config'])
        conn.execute("insert into setup_card_revisions values(%s,'booking_details',2,'{}',now())", (tenant,))
        with pytest.raises(driver.errors.UniqueViolation):
            save(conn, tenant, 1, page='booking_details', expected=before, patch=patch)
        assert snapshot() == before
        conn.execute('delete from setup_card_revisions where revision=2')
        save(conn, tenant, 1, page='booking_details', expected=before, patch=patch)
        current = snapshot()
        assert current['config']['booking_mode'] == 'stay'
        assert current['config']['booking_revision'] == before['config']['booking_revision'] + 1
        _, off = apply(False, current['config'])
        conn.execute("insert into bookings(tenant_id,booking_date,status) values(%s,current_date,'completed')", (tenant,))
        with pytest.raises(driver.errors.RaiseException, match='booking history'):
            save(conn, tenant, 2, page='booking_details', expected=current, patch=off)
        assert snapshot() == current
        _, unchanged = apply(True, current['config'], True)
        save(conn, tenant, 2, page='booking_details', expected=snapshot(True), patch=unchanged)
        retained = snapshot(True)
        conn.execute(ROLLBACK.read_text(encoding='utf-8'))
        conn.execute(FORWARD.read_text(encoding='utf-8'))
        assert snapshot(True) == retained


def test_diary_owned_booking_mode_migration_keeps_requirements_step_open(cluster):
    from app import setup_card_mode
    driver, dsn = cluster
    tenant = uuid.uuid4()
    with driver.connect(dsn, autocommit=True) as conn:
        initialise(conn)
        conn.execute(DIARY_MODE_FORWARD.read_text(encoding="utf-8"))
        conn.execute("insert into tenants values(%s)", (tenant,))
        conn.execute("insert into scheduling_config(tenant_id) values(%s)", (tenant,))
        save(conn, tenant, 0, page="diary_settings")
        before = conn.execute("select to_jsonb(s) from scheduling_config s where tenant_id=%s", (tenant,)).fetchone()[0]
        expected = before | {"_setup_has_bookings": False}
        patch, _ = setup_card_mode.prepare(
            {"id": "mode", "fields": {"key": "stay_mode", "value": True}},
            {"config": before, "has_bookings": False})
        save(conn, tenant, 1, page="diary_settings", expected=expected, patch=patch)
        after = conn.execute("select to_jsonb(s) from scheduling_config s where tenant_id=%s", (tenant,)).fetchone()[0]
        assert after["booking_mode"] == "stay"
        assert after["intake_fields"] == [{"label": "Departure / collection date & time", "required": True,
                                            "hint": "When the booking ends — pick-up / check-out / drop-back."}]
        assert after["intake_configured_at"] is None

        conn.execute("insert into bookings(tenant_id,booking_date,status) values(%s,current_date,'completed')", (tenant,))
        with pytest.raises(driver.errors.RaiseException, match="booking history"):
            save(conn, tenant, 2, page="diary_settings",
                 expected=after | {"_setup_has_bookings": False},
                 patch={"booking_mode": "appointment", "intake_fields": []})
        current = conn.execute("select to_jsonb(s) from scheduling_config s where tenant_id=%s", (tenant,)).fetchone()[0]
        assert current == after
        conn.execute(DIARY_MODE_ROLLBACK.read_text(encoding="utf-8"))
        with pytest.raises(driver.errors.RaiseException, match="BLAM8_SETUP_INVALID"):
            save(conn, tenant, 2, page="diary_settings",
                 expected=after | {"_setup_has_bookings": False},
                 patch={"booking_mode": "appointment", "intake_fields": []})
        conn.execute(DIARY_MODE_FORWARD.read_text(encoding="utf-8"))


def test_customer_fields_atomic_identity_saved_values_and_migration_cycle(cluster):
    from tests.test_setup_card_fields import field, apply
    driver, dsn = cluster
    tenant = uuid.uuid4()
    with driver.connect(dsn, autocommit=True) as conn:
        initialise(conn)
        conn.execute('insert into tenants values(%s)', (tenant,))
        save(conn, tenant, 0, page='crm_fields')
        _, patch = apply(field(field_type='dropdown', options=['A', 'B']))
        before = {'definitions': []}
        conn.execute("insert into setup_card_revisions values(%s,'crm_fields',2,'{}',now())", (tenant,))
        with pytest.raises(driver.errors.UniqueViolation):
            save(conn, tenant, 1, page='crm_fields', expected=before, patch=patch)
        assert conn.execute('select count(*) from crm_field_definitions').fetchone() == (0,)
        conn.execute('delete from setup_card_revisions where revision=2')
        save(conn, tenant, 1, page='crm_fields', expected=before, patch=patch)
        def snapshot():
            return {'definitions': conn.execute("select jsonb_agg(to_jsonb(d) order by id) from crm_field_definitions d where entity='contact'").fetchone()[0]}
        current = snapshot()
        old = current['definitions'][0]
        conn.execute('create table crm_contacts(id uuid primary key, custom_data jsonb)')
        conn.execute('insert into crm_contacts values(%s,%s)', (uuid.uuid4(), json.dumps({old['key']: 'A'})))
        _, correction = apply({'label': 'Reference', 'extraction_hint': 'Revised'}, current['definitions'])
        with pytest.raises(driver.errors.RaiseException, match='SETTINGS_STALE'):
            save(conn, tenant, 2, page='crm_fields', expected=before, patch=correction)
        forged = {'definitions': [correction['definitions'][0] | {'key': 'different'}]}
        with pytest.raises(driver.errors.RaiseException, match='identity and meaning'):
            save(conn, tenant, 2, page='crm_fields', expected=current, patch=forged)
        with pytest.raises(driver.errors.RaiseException, match='dropdown options'):
            save(conn, tenant, 2, page='crm_fields', expected=current,
                 patch={'definitions': [correction['definitions'][0] | {'options': ['B', 'C']}]})
        save(conn, tenant, 2, page='crm_fields', expected=current, patch=correction)
        after = snapshot()['definitions'][0]
        assert after['id'] == old['id'] and after['key'] == old['key']
        assert after['created_at'] == old['created_at'] and after['extraction_hint'] == 'Revised'
        assert conn.execute('select custom_data from crm_contacts').fetchone() == ({old['key']: 'A'},)
        conn.execute(ROLLBACK.read_text(encoding='utf-8'))
        conn.execute(FORWARD.read_text(encoding='utf-8'))
        assert snapshot()['definitions'][0] == after
        assert conn.execute('select count(*) from scheduling_config').fetchone() == (0,)


def test_crm_stages_and_threshold_share_atomic_receipt_and_collection_snapshot(cluster):
    driver, dsn = cluster
    tenant, existing, added = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with driver.connect(dsn, autocommit=True) as conn:
        initialise(conn)
        conn.execute('insert into tenants values(%s)', (tenant,))
        conn.execute("insert into crm_stages(id,tenant_id,name) values(%s,%s,'Waiting')", (existing, tenant))
        conn.execute('create table crm_deals(id uuid primary key, stage_id uuid references crm_stages(id), status text)')
        conn.execute("insert into crm_deals values(%s,%s,'open')", (uuid.uuid4(), existing))
        save(conn, tenant, 0, page='crm_settings')
        def snapshot():
            return {'quiet_days': conn.execute('select crm_quiet_days from tenant_config').fetchone()[0],
                    'stages': conn.execute('select jsonb_agg(to_jsonb(s) order by id) from crm_stages s').fetchone()[0]}
        before = snapshot()
        patch = {'crm_quiet_days': 7, 'stages': [dict(id=str(existing), name='Waiting', stage_type='active', display_order=2),
                                               dict(id=str(added), name='Complete', stage_type='won', display_order=3)]}
        conn.execute("insert into setup_card_revisions values(%s,'crm_settings',2,'{}',now())", (tenant,))
        with pytest.raises(driver.errors.UniqueViolation):
            save(conn, tenant, 1, page='crm_settings', expected=before, patch=patch)
        assert snapshot() == before
        conn.execute('delete from setup_card_revisions where revision=2')
        save(conn, tenant, 1, page='crm_settings', expected=before, patch=patch)
        after = snapshot()
        assert after['quiet_days'] == 7 and len(after['stages']) == 2
        saved = next(s for s in after['stages'] if s['id'] == str(existing))
        assert saved['display_order'] == 2 and saved['created_at'] == before['stages'][0]['created_at']
        assert conn.execute('select stage_id,status from crm_deals').fetchone() == (existing, 'open')
        with pytest.raises(driver.errors.RaiseException, match='identity and type'):
            save(conn, tenant, 2, page='crm_settings', expected=after,
                 patch={'stages': [dict(id=str(existing), name='Waiting', stage_type='won', display_order=2)]})
        assert conn.execute('select count(*) from scheduling_config').fetchone() == (0,)
        with pytest.raises(driver.errors.RaiseException, match='SETTINGS_STALE'):
            save(conn, tenant, 2, page='crm_settings', expected=before, patch=patch)
        conn.execute(ROLLBACK.read_text(encoding='utf-8'))
        conn.execute(FORWARD.read_text(encoding='utf-8'))
        assert snapshot() == after
        conn.execute('update tenant_config set crm_quiet_days=30')
        with pytest.raises(driver.errors.RaiseException, match='SETTINGS_STALE'):
            save(conn, tenant, 2, page='crm_settings', expected=after, patch={'stages': [], 'crm_quiet_days': 10})
        with pytest.raises(driver.errors.RaiseException, match='SETUP_INVALID'):
            save(conn, tenant, 2, page='crm_settings', expected=snapshot(), patch={'stages': [], 'unrelated': 'bad'})


def test_package_services_snapshot_is_locked_with_invoice_and_receipt(cluster):
    driver, dsn = cluster
    tenant = uuid.uuid4()
    with driver.connect(dsn, autocommit=True) as conn:
        initialise(conn)
        conn.execute('insert into tenants values(%s)', (tenant,))
        conn.execute('insert into invoice_settings(tenant_id) values(%s)', (tenant,))
        conn.execute("insert into scheduling_config(tenant_id,service_types) values(%s,'[{\"name\":\"Review\"}]')", (tenant,))
        save(conn, tenant, 0, page='packages')
        before = conn.execute('select to_jsonb(i) from invoice_settings i').fetchone()[0]
        before['_setup_scheduling'] = conn.execute('select to_jsonb(s) from scheduling_config s').fetchone()[0]
        patch = {'extras': {'packages': [{'key': 'pass', 'name': 'Pass', 'services': ['Review']}]}}
        conn.execute("update scheduling_config set service_types='[]'")
        with pytest.raises(driver.errors.RaiseException, match='SETTINGS_STALE'):
            save(conn, tenant, 1, page='packages', expected=before, patch=patch)
        conn.execute("update scheduling_config set service_types='[{\"name\":\"Review\"}]'")
        conn.execute("insert into setup_card_revisions values(%s,'packages',2,'{}',now())", (tenant,))
        with pytest.raises(driver.errors.UniqueViolation):
            save(conn, tenant, 1, page='packages', expected=before, patch=patch)
        assert conn.execute('select extras from invoice_settings').fetchone() == ({},)
        conn.execute("delete from setup_card_revisions where revision=2")
        save(conn, tenant, 1, page='packages', expected=before, patch=patch)
        assert conn.execute('select extras from invoice_settings').fetchone() == (patch['extras'],)
        assert conn.execute('select booking_revision from scheduling_config').fetchone() == (0,)
        conn.execute(ROLLBACK.read_text(encoding='utf-8'))
        conn.execute(FORWARD.read_text(encoding='utf-8'))
        assert conn.execute('select extras from invoice_settings').fetchone() == (patch['extras'],)
        assert conn.execute('select count(*) from setup_card_revisions').fetchone() == (2,)


def test_invoice_and_package_atomic_receipts_shared_snapshot_and_migration_cycle(cluster):
    driver, dsn = cluster
    tenant = uuid.uuid4()
    with driver.connect(dsn, autocommit=True) as conn:
        initialise(conn)
        legacy = uuid.uuid4()
        conn.execute('insert into tenants values(%s)', (legacy,))
        conn.execute('alter table invoice_settings drop column default_quote_valid_days')
        conn.execute('insert into invoice_settings(tenant_id,default_quote_valid_months) values(%s,3)', (legacy,))
        conn.execute(FORWARD.read_text(encoding='utf-8'))
        assert conn.execute('select default_quote_valid_months,default_quote_valid_days from invoice_settings where tenant_id=%s', (legacy,)).fetchone() == (3, None)
        conn.execute('insert into tenants values(%s)', (tenant,))
        save(conn, tenant, 0, page='invoice_settings')
        choices = dict(show_vat=False, vat_rate=0, show_business_details=True,
                       show_payment_terms=False, payment_terms='', default_quote_valid_days=14)
        with pytest.raises(driver.errors.RaiseException, match='explicit initial'):
            save(conn, tenant, 1, page='invoice_settings', expected={}, patch={'footer_note': 'Text'})
        save(conn, tenant, 1, page='invoice_settings', expected={}, patch=choices)
        def snapshot():
            return conn.execute('select to_jsonb(i) from invoice_settings i where tenant_id=%s', (tenant,)).fetchone()[0]
        before = snapshot()
        assert all(before[k] == v for k, v in choices.items())
        assert before['default_deposit_percent'] is None and before['extras'] == {}
        assert conn.execute('select count(*) from scheduling_config').fetchone() == (0,)
        conn.execute("insert into setup_card_revisions values(%s,'invoice_settings',3,'{}',now())", (tenant,))
        with pytest.raises(driver.errors.UniqueViolation):
            save(conn, tenant, 2, page='invoice_settings', expected=before, patch={'footer_note': 'Text'})
        assert snapshot() == before
        conn.execute("delete from setup_card_revisions where page='invoice_settings' and revision=3")
        save(conn, tenant, 0, page='packages')
        extras = {'packages': [{'name': 'Existing'}], 'package_prefs': {'paid_only': False, 'low_at': 0, 'expiry_days': 0, 'portal': True}}
        save(conn, tenant, 1, page='packages', expected=before, patch={'extras': extras})
        with pytest.raises(driver.errors.RaiseException, match='SETTINGS_STALE'):
            save(conn, tenant, 2, page='invoice_settings', expected=before, patch={'footer_note': 'Text'})
        before = snapshot()
        with pytest.raises(driver.errors.RaiseException, match='SETUP_INVALID'):
            save(conn, tenant, 2, page='packages', expected=before, patch={'vat_rate': 5})
        conn.execute(ROLLBACK.read_text(encoding='utf-8'))
        conn.execute(FORWARD.read_text(encoding='utf-8'))
        save(conn, tenant, 2, page='invoice_settings', expected=before, patch={'footer_note': 'Text'})
        after = snapshot()
        assert after['extras'] == extras and after['footer_note'] == 'Text'
        assert conn.execute('select count(*) from setup_card_revisions').fetchone() == (5,)


def test_linked_stages_snapshot_receipt_rollback_and_migration_cycle(cluster):
    driver, dsn = cluster
    tenant, stage = uuid.uuid4(), uuid.uuid4()
    with driver.connect(dsn, autocommit=True) as conn:
        initialise(conn)
        conn.execute("insert into tenants values(%s)", (tenant,))
        conn.execute("insert into scheduling_config(tenant_id) values(%s)", (tenant,))
        conn.execute("insert into crm_stages values(%s,%s,'Waiting')", (stage, tenant))
        save(conn, tenant, 0)
        def snapshot():
            cfg = conn.execute("select to_jsonb(c) from scheduling_config c where tenant_id=%s", (tenant,)).fetchone()[0]
            cfg["_setup_stages"] = conn.execute("select jsonb_agg(to_jsonb(s) order by id) from crm_stages s where tenant_id=%s", (tenant,)).fetchone()[0]
            return cfg
        before = snapshot()
        patch = {"courses": {"waitlist_stage": "Waiting"}}
        conn.execute("update crm_stages set name='Queued' where id=%s", (stage,))
        with pytest.raises(driver.errors.RaiseException, match="SETTINGS_STALE"):
            save(conn, tenant, 1, expected=before, patch=patch)
        conn.execute("update crm_stages set name='Waiting' where id=%s", (stage,))
        conn.execute("insert into setup_card_revisions values(%s,'diary_settings',2,'{}',now())", (tenant,))
        with pytest.raises(driver.errors.UniqueViolation):
            save(conn, tenant, 1, expected=before, patch=patch)
        assert conn.execute("select courses,booking_revision from scheduling_config").fetchone() == (None, 0)
        conn.execute("delete from setup_card_revisions where revision=2")
        save(conn, tenant, 1, expected=before, patch=patch)
        assert conn.execute("select courses,booking_revision from scheduling_config").fetchone() == (patch["courses"], 1)
        conn.execute(ROLLBACK.read_text(encoding="utf-8"))
        conn.execute(FORWARD.read_text(encoding="utf-8"))
        before = snapshot()
        conn.execute("delete from crm_stages where id=%s", (stage,))
        with pytest.raises(driver.errors.RaiseException, match="SETTINGS_STALE"):
            save(conn, tenant, 2, expected=before, patch={"courses": {"waitlist_stage": ""}})
        before["_setup_stages"] = []
        save(conn, tenant, 2, expected=before, patch={"courses": {"waitlist_stage": ""}})
        assert conn.execute("select courses,booking_revision from scheduling_config").fetchone() == ({"waitlist_stage": ""}, 2)


def save(conn, tenant, revision, *, expected=None, patch=None, page="diary_settings"):
    return conn.execute("select change_setup_card(%s,%s,%s,%s,%s,%s)",
                        (tenant, page, revision, json.dumps(card(revision+1, page)),
                         json.dumps(expected) if expected is not None else None,
                         json.dumps(patch) if patch is not None else None)).fetchone()[0]


def test_forward_rollback_forward_preserves_history_and_atomic_settings(cluster):
    driver, dsn = cluster
    tenant = uuid.uuid4()
    with driver.connect(dsn, autocommit=True) as conn:
        initialise(conn)
        conn.execute("insert into tenants values(%s)", (tenant,))
        conn.execute("insert into scheduling_config(tenant_id) values(%s)", (tenant,))
        save(conn, tenant, 0)
        before = conn.execute("select to_jsonb(c) from scheduling_config c where tenant_id=%s", (tenant,)).fetchone()[0]
        patch = {"service_types": [{"name": "Review", "minutes": 45}]}
        save(conn, tenant, 1, expected=before, patch=patch)
        after = conn.execute("select to_jsonb(c) from scheduling_config c where tenant_id=%s", (tenant,)).fetchone()[0]
        assert after["service_types"] == patch["service_types"]
        assert after["booking_revision"] == 1
        assert after["capacity_per_window"] == before["capacity_per_window"]
        with pytest.raises(driver.errors.RaiseException, match="SETTINGS_STALE"):
            save(conn, tenant, 2, expected=before, patch=patch)
        assert conn.execute("select count(*) from setup_card_revisions").fetchone()[0] == 2
        # A receipt failure rolls the settings change and current card back too.
        conn.execute("insert into setup_card_revisions values(%s,'diary_settings',3,'{}',now())", (tenant,))
        with pytest.raises(driver.errors.UniqueViolation):
            save(conn, tenant, 2, expected=after, patch={"service_types": []})
        assert conn.execute("select service_types,booking_revision from scheduling_config").fetchone() == (patch["service_types"], 1)
        assert conn.execute("select setup_cards->'diary_settings'->>'revision' from tenant_config").fetchone() == ("2",)
        conn.execute("delete from setup_card_revisions where revision=3")
        conn.execute(ROLLBACK.read_text(encoding="utf-8"))
        conn.execute(ROLLBACK.read_text(encoding="utf-8"))
        assert conn.execute("select to_regprocedure('change_setup_card(uuid,text,bigint,jsonb,jsonb,jsonb)')").fetchone() == (None,)
        assert conn.execute("select count(*) from setup_card_revisions").fetchone() == (2,)
        conn.execute(FORWARD.read_text(encoding="utf-8"))
        conn.execute(FORWARD.read_text(encoding="utf-8"))
        save(conn, tenant, 2, expected=after, patch={"resource_types": []})
        assert conn.execute("select count(*) from setup_card_revisions").fetchone() == (3,)
        fresh = uuid.uuid4()
        conn.execute("insert into tenants values(%s)", (fresh,))
        save(conn, fresh, 0)
        save(conn, fresh, 1, expected={}, patch=patch)
        assert conn.execute("select capacity_per_window,open_time,close_time,booking_revision from scheduling_config where tenant_id=%s", (fresh,)).fetchone() == (None, None, None, 1)
        with pytest.raises(driver.errors.RaiseException, match="SETTINGS_STALE"):
            save(conn, fresh, 2, expected={}, patch=patch)


def test_rls_and_same_revision_competition(cluster):
    driver, dsn = cluster
    tenant, other = uuid.uuid4(), uuid.uuid4()
    with driver.connect(dsn, autocommit=True) as conn:
        initialise(conn)
        conn.execute("insert into tenants values(%s),(%s)", (tenant, other))
        conn.execute("insert into tenant_config(tenant_id,unrelated) values(%s,'kept'),(%s,'private')", (tenant, other))
        conn.execute("create role setup_owner nologin")
        conn.execute("grant select,insert,update on tenant_config,scheduling_config,invoice_settings to setup_owner")
        conn.execute("grant execute on function save_invoice_settings_checked(uuid,jsonb,jsonb,text) to setup_owner")
        conn.execute("grant select,insert on setup_card_revisions to setup_owner")
        conn.execute("grant execute on function change_setup_card(uuid,text,bigint,jsonb,jsonb,jsonb) to setup_owner")
        conn.execute("alter table tenant_config enable row level security")
        conn.execute("alter table scheduling_config enable row level security")
        for table in ("tenant_config", "scheduling_config"):
            conn.execute(f"create policy owner on {table} using(tenant_id in(select auth_tenant_ids())) with check(tenant_id in(select auth_tenant_ids()))")
        save(conn, other, 0)
        conn.execute("select set_config('test.tenant',%s,false)", (str(tenant),))
        conn.execute("set role setup_owner")
        assert conn.execute("select * from setup_card_revisions").fetchall() == []
        with pytest.raises(driver.errors.InsufficientPrivilege):
            save(conn, other, 1)
        save(conn, tenant, 0)
        with pytest.raises(driver.errors.InsufficientPrivilege):
            conn.execute("select save_invoice_settings_checked(%s,%s,%s,%s)",
                (other, json.dumps({"invoice": {}, "quote_auto_draft": "propose"}), json.dumps({"payment_terms": "No"}), "off"))
        assert conn.execute("select unrelated from tenant_config").fetchone() == ("kept",)
        with pytest.raises(driver.errors.InsufficientPrivilege):
            conn.execute("delete from setup_card_revisions")

    started = threading.Event()
    def second_save():
        with driver.connect(dsn, autocommit=True) as conn:
            started.set()
            try:
                save(conn, tenant, 1)
                return "saved"
            except driver.errors.RaiseException as exc:
                return str(exc)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with driver.connect(dsn) as first:
            save(first, tenant, 1)
            second = pool.submit(second_save)
            assert started.wait(5)
        assert "SETUP_STALE" in second.result(timeout=5)
    with driver.connect(dsn) as conn:
        assert conn.execute("select count(*) from setup_card_revisions where tenant_id=%s", (tenant,)).fetchone() == (2,)


def test_page_save_during_accept_is_detected_under_row_lock(cluster):
    driver, dsn = cluster
    tenant = uuid.uuid4()
    with driver.connect(dsn, autocommit=True) as conn:
        initialise(conn)
        conn.execute("insert into tenants values(%s)", (tenant,))
        conn.execute("insert into scheduling_config(tenant_id) values(%s)", (tenant,))
        save(conn, tenant, 0)
        snapshot = conn.execute("select to_jsonb(c) from scheduling_config c").fetchone()[0]
    attempted = threading.Event()
    def accept():
        with driver.connect(dsn, autocommit=True) as conn:
            attempted.set()
            try:
                save(conn, tenant, 1, expected=snapshot, patch={"service_types": []})
                return "saved"
            except driver.errors.RaiseException as exc:
                return str(exc)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with driver.connect(dsn) as first:
            first.execute("update scheduling_config set buffer_minutes=15 where tenant_id=%s", (tenant,))
            pending = pool.submit(accept)
            assert attempted.wait(5)
        assert "SETTINGS_STALE" in pending.result(timeout=5)


def snapshot(conn, tenant, page):
    config = conn.execute("select to_jsonb(c) from scheduling_config c where tenant_id=%s", (tenant,)).fetchone()
    table, suffix = ("crm_field_definitions", " and entity='contact'") if page == "booking_details" else ("requirement_groups", "")
    related = conn.execute(f"select coalesce(jsonb_agg(to_jsonb(r) order by id),'[]') from {table} r where tenant_id=%s{suffix}", (tenant,)).fetchone()[0]
    return {"config": config[0] if config else {}, "definitions" if page == "booking_details" else "groups": related}


def test_detail_saves_are_atomic_preserve_instances_and_survive_rollback(cluster):
    from app import setup_cards
    from tests.test_setup_cards import start, append
    from tests.test_setup_card_details import subject
    driver, dsn = cluster
    tenant, gid, instance = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with driver.connect(dsn, autocommit=True) as conn:
        initialise(conn)
        conn.execute("insert into tenants values(%s)", (tenant,))
        page = "booking_details"
        proposed = start(append("field", label="Reference", required=False, remember="once"), page=page)
        before = snapshot(conn, tenant, page)
        _, patch = setup_cards.accept(proposed, 1, "accept", [r["id"] for r in proposed["lines"]], before)
        save(conn, tenant, 0, page=page)
        # Failure to write the receipt must undo the new config AND customer field.
        conn.execute("insert into setup_card_revisions values(%s,%s,2,'{}',now())", (tenant, page))
        with pytest.raises(driver.errors.UniqueViolation):
            save(conn, tenant, 1, page=page, expected=before, patch=patch)
        assert conn.execute("select count(*) from crm_field_definitions").fetchone() == (0,)
        assert conn.execute("select count(*) from scheduling_config").fetchone() == (0,)
        conn.execute("delete from setup_card_revisions where revision=2")
        save(conn, tenant, 1, page=page, expected=before, patch=patch)
        assert conn.execute("select key from crm_field_definitions").fetchone() == ("reference",)
        assert conn.execute("select intake_fields->0->>'crm_key',intake_configured_at is not null from scheduling_config").fetchone() == ("reference", True)
        conn.execute("insert into requirement_groups(id,tenant_id,label,label_plural,fields) values(%s,%s,'Asset','Assets',%s)",
                     (gid, tenant, json.dumps([{"key": "stable", "label": "Code", "type": "text", "required": False, "durability": "once", "gate": False}])))
        conn.execute("insert into requirement_instances(id,tenant_id,group_id) values(%s,%s,%s)", (instance, tenant, gid))
        page = "dependants"
        proposed = start(subject(hint="Updated"), page=page)
        before = snapshot(conn, tenant, page)
        _, patch = setup_cards.accept(proposed, 1, "accept", [r["id"] for r in proposed["lines"]], before)
        save(conn, tenant, 0, page=page)
        save(conn, tenant, 1, page=page, expected=before, patch=patch)
        assert conn.execute("select id,fields->0->>'key',fields->0->>'hint' from requirement_groups").fetchone() == (gid, "stable", "Updated")
        assert conn.execute("select group_id from requirement_instances where id=%s", (instance,)).fetchone() == (gid,)
        conn.execute(ROLLBACK.read_text(encoding="utf-8"))
        conn.execute(FORWARD.read_text(encoding="utf-8"))
        assert conn.execute("select count(*) from setup_card_revisions").fetchone() == (4,)
        assert conn.execute("select key from crm_field_definitions").fetchone() == ("reference",)
        fresh = snapshot(conn, tenant, page)
        conn.execute("insert into setup_card_revisions values(%s,%s,3,'{}',now())", (tenant, page))
        patch["groups"][0]["fields"][0]["hint"] = "Must roll back"
        with pytest.raises(driver.errors.UniqueViolation):
            save(conn, tenant, 2, page=page, expected=fresh, patch=patch)
        assert conn.execute("select fields->0->>'hint' from requirement_groups").fetchone() == ("Updated",)


@pytest.mark.parametrize("page", ["booking_details", "dependants"])
def test_concurrent_related_insert_invalidates_detail_preview(cluster, page):
    driver, dsn = cluster
    tenant = uuid.uuid4()
    with driver.connect(dsn, autocommit=True) as conn:
        initialise(conn)
        conn.execute("insert into tenants values(%s)", (tenant,))
        save(conn, tenant, 0, page=page)
        before = snapshot(conn, tenant, page)
    started = threading.Event()
    def accept():
        with driver.connect(dsn, autocommit=True) as conn:
            started.set()
            try:
                save(conn, tenant, 1, page=page, expected=before,
                     patch={"intake_fields": [], "definitions": []} if page == "booking_details" else {"groups": []})
                return "saved"
            except driver.errors.RaiseException as exc:
                return str(exc)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with driver.connect(dsn) as first:
            if page == "booking_details":
                first.execute("insert into crm_field_definitions(tenant_id,entity,key,label) values(%s,'contact','code','Code')", (tenant,))
            else:
                first.execute("insert into requirement_groups(id,tenant_id,label) values(%s,%s,'Asset')", (uuid.uuid4(), tenant))
            pending = pool.submit(accept)
            assert started.wait(5)
        assert "SETTINGS_STALE" in pending.result(timeout=5)
    with driver.connect(dsn) as conn:
        assert conn.execute("select count(*) from setup_card_revisions").fetchone() == (1,)
        assert conn.execute("select count(*) from scheduling_config").fetchone() == (0,)


def test_stay_appointment_and_pool_commit_together_with_rollback_and_stale_checks(cluster):
    from app import setup_cards, intake
    from tests.test_setup_cards import start, service
    driver, dsn = cluster
    tenant = uuid.uuid4()
    with driver.connect(dsn, autocommit=True) as conn:
        initialise(conn)
        conn.execute("insert into tenants values(%s)", (tenant,))
        conn.execute("insert into scheduling_config(tenant_id,intake_fields,capacity_per_window,resource_types,booking_mode) values(%s,%s,20,%s,'stay')",
                     (tenant, json.dumps([intake.STAY_FIELD]), json.dumps([{"name": "Space", "count": 8}])))
        save(conn, tenant, 0)
        before = conn.execute("select to_jsonb(c) from scheduling_config c").fetchone()[0]
        proposal = start(service(at_once=3))
        applied, patch = setup_cards.accept(proposal, 1, "accept", [proposal["lines"][0]["id"]], before)
        conn.execute("insert into setup_card_revisions values(%s,'diary_settings',2,'{}',now())", (tenant,))
        args = (tenant, "diary_settings", 1, json.dumps(applied), json.dumps(before), json.dumps(patch))
        with pytest.raises(driver.errors.UniqueViolation):
            conn.execute("select change_setup_card(%s,%s,%s,%s,%s,%s)", args)
        assert conn.execute("select to_jsonb(c) from scheduling_config c").fetchone()[0] == before
        conn.execute("delete from setup_card_revisions where revision=2")
        conn.execute("select change_setup_card(%s,%s,%s,%s,%s,%s)", args)
        saved = conn.execute("select to_jsonb(c) from scheduling_config c").fetchone()[0]
        assert saved["resource_types"] == [{"name": "Space", "count": 8}, {"name": "Review", "count": 3}]
        assert saved["service_types"][0]["resource"] == "Review"
        assert saved["capacity_per_window"] == 20 and saved["booking_revision"] == 1
        assert conn.execute("select card->'last_action'->'changes'->0->'resource_after'->>'count' from setup_card_revisions where revision=2").fetchone() == ("3",)
        # A new pool count between preview and commit invalidates the complete snapshot.
        conn.execute("update scheduling_config set resource_types=%s", (json.dumps([{"name": "Space", "count": 8}, {"name": "Review", "count": 4}]),))
        with pytest.raises(driver.errors.RaiseException, match="SETTINGS_STALE"):
            save(conn, tenant, 2, expected=saved, patch=patch)
        assert conn.execute("select resource_types->1->>'count' from scheduling_config").fetchone() == ("4",)
        conn.execute(ROLLBACK.read_text(encoding="utf-8"))
        conn.execute(FORWARD.read_text(encoding="utf-8"))
        assert conn.execute("select count(*) from setup_card_revisions").fetchone() == (2,)
        assert conn.execute("select service_types->0->>'resource' from scheduling_config").fetchone() == ("Review",)


def test_scalar_types_confirmation_and_receipt_share_one_transaction(cluster):
    from tests.test_setup_card_scalars import apply, setting
    driver, dsn = cluster
    tenant = uuid.uuid4()
    with driver.connect(dsn, autocommit=True) as conn:
        initialise(conn)
        conn.execute("insert into tenants values(%s)", (tenant,))
        conn.execute("insert into scheduling_config(tenant_id,buffer_minutes,recurring) values(%s,15,%s)",
                     (tenant, json.dumps({"enabled": True, "frequencies": ["weekly"], "max_occurrences": 8, "future": 7})))
        save(conn, tenant, 0)
        before = conn.execute("select to_jsonb(c) from scheduling_config c").fetchone()[0]
        card, patch = apply(setting("open_time", "08:00"), setting("close_time", "17:00"),
            setting("working_days", ["mon", "tue"]), setting("capacity_per_window", 4), setting("timezone", "Europe/London"),
            setting("buffer_minutes", 0), setting("recurring_enabled", False), setting("blackout_dates", ["2026-12-25"]), cfg=before)
        conn.execute("insert into setup_card_revisions values(%s,'diary_settings',2,'{}',now())", (tenant,))
        args = (tenant, "diary_settings", 1, json.dumps(card), json.dumps(before), json.dumps(patch))
        with pytest.raises(driver.errors.UniqueViolation):
            conn.execute("select change_setup_card(%s,%s,%s,%s,%s,%s)", args)
        assert conn.execute("select to_jsonb(c) from scheduling_config c").fetchone()[0] == before
        conn.execute("delete from setup_card_revisions where revision=2")
        conn.execute("select change_setup_card(%s,%s,%s,%s,%s,%s)", args)
        saved = conn.execute("select to_jsonb(c) from scheduling_config c").fetchone()[0]
        assert saved["buffer_minutes"] == 0 and saved["recurring"]["enabled"] is False
        assert saved["recurring"]["future"] == 7 and saved["blackout_dates"] == ["2026-12-25"]
        assert saved["working_days"] == "mon,tue" and saved["open_time"] == "08:00:00"
        assert saved["booking_revision"] == 1
        assert saved["booking_setting_review"]["opening_hours"]["confirmed_by"] == "owner"
        with pytest.raises(driver.errors.RaiseException, match="SETTINGS_STALE"):
            save(conn, tenant, 2, expected=before, patch={"buffer_minutes": 10})
        with pytest.raises(driver.errors.RaiseException, match="SETUP_INVALID"):
            save(conn, tenant, 2, expected=saved, patch={"tenant_id": str(uuid.uuid4())})
        conn.execute(ROLLBACK.read_text(encoding="utf-8"))
        conn.execute(FORWARD.read_text(encoding="utf-8"))
        save(conn, tenant, 2, expected=saved, patch={"open_time": None, "close_time": None})
        assert conn.execute("select open_time,close_time,booking_revision from scheduling_config").fetchone() == (None, None, 2)


def test_considered_only_revision_is_persisted_without_settings_and_rejects_stale(cluster):
    from app import setup_cards
    driver, dsn = cluster
    tenant = uuid.uuid4()
    proposed = setup_cards.change("diary_settings", None, 0, "discussion", [],
        [{"key": "diary_settings/buffer_minutes", "state": "needs_answer"}])
    with driver.connect(dsn, autocommit=True) as conn:
        initialise(conn)
        conn.execute("insert into tenants values(%s)", (tenant,))
        args = (tenant, "diary_settings", 0, json.dumps(proposed), None, None)
        conn.execute("select change_setup_card(%s,%s,%s,%s,%s,%s)", args)
        assert conn.execute("select card from setup_card_revisions").fetchone()[0] == proposed
        assert conn.execute("select count(*) from scheduling_config").fetchone() == (0,)
        with pytest.raises(driver.errors.RaiseException, match="SETUP_STALE"):
            conn.execute("select change_setup_card(%s,%s,%s,%s,%s,%s)", args)
        assert conn.execute("select count(*) from setup_card_revisions").fetchone() == (1,)


def test_invoice_form_snapshot_atomicity_card_race_and_rollback(cluster):
    from app import invoice_config
    driver, dsn = cluster
    tenant = uuid.uuid4()
    initial = {"invoice": {}, "quote_auto_draft": "propose"}
    fields = invoice_config.clean_form({"payment_terms": "On receipt"})
    def expected(conn):
        return {"invoice": conn.execute("select to_jsonb(i) from invoice_settings i where tenant_id=%s", (tenant,)).fetchone()[0],
                "quote_auto_draft": conn.execute("select quote_auto_draft from tenant_config where tenant_id=%s", (tenant,)).fetchone()[0]}
    def write(conn, version, patch, mode):
        conn.execute("select save_invoice_settings_checked(%s,%s,%s,%s)", (tenant, json.dumps(version), json.dumps(patch), mode))
    with driver.connect(dsn, autocommit=True) as conn:
        initialise(conn)
        conn.execute("insert into tenants values(%s)", (tenant,))
        write(conn, initial, fields, "off")
        current = expected(conn)
        assert current["quote_auto_draft"] == "off"
        assert current["invoice"]["payment_terms"] == "On receipt"
        with pytest.raises(driver.errors.RaiseException, match="SETTINGS_STALE"):
            write(conn, initial, {"payment_terms": "Old form"}, "auto")
        assert expected(conn) == current
        conn.execute("""create function reject_quote_mode() returns trigger language plpgsql as $$
            begin if new.quote_auto_draft='auto' then raise exception 'injected failure'; end if; return new; end $$;
            create trigger reject_quote before update on tenant_config for each row execute function reject_quote_mode();""")
        with pytest.raises(driver.errors.RaiseException, match="injected failure"):
            write(conn, current, {"payment_terms": "Must roll back"}, "auto")
        assert expected(conn) == current
        conn.execute("drop trigger reject_quote on tenant_config")
        # Card acceptance and the ordinary form share the same row snapshot.
        save(conn, tenant, 0, page="invoice_settings", expected=current["invoice"], patch={"payment_terms": "From card"})
        with pytest.raises(driver.errors.RaiseException, match="SETTINGS_STALE"):
            write(conn, current, {"payment_terms": "Older form"}, "auto")
        current = expected(conn)
        conn.execute(ROLLBACK.read_text(encoding="utf-8"))
        assert conn.execute("select to_regprocedure('save_invoice_settings_checked(uuid,jsonb,jsonb,text)')").fetchone() == (None,)
        assert expected(conn) == current
        conn.execute(FORWARD.read_text(encoding="utf-8"))
        write(conn, current, {"footer_note": "After reapply"}, "propose")
        assert expected(conn)["invoice"]["footer_note"] == "After reapply"


def test_competing_invoice_forms_allow_only_one_winner(cluster):
    from app import invoice_config
    driver, dsn = cluster
    tenant = uuid.uuid4()
    with driver.connect(dsn, autocommit=True) as conn:
        initialise(conn)
        conn.execute("insert into tenants values(%s)", (tenant,))
    ready = threading.Barrier(2)
    def write(label):
        with driver.connect(dsn, autocommit=True) as conn:
            ready.wait(timeout=10)
            try:
                conn.execute("select save_invoice_settings_checked(%s,%s,%s,%s)",
                    (tenant, json.dumps({"invoice": {}, "quote_auto_draft": "propose"}),
                     json.dumps(invoice_config.clean_form({"payment_terms": label})), "off"))
                return "saved"
            except driver.errors.RaiseException as exc:
                assert "SETTINGS_STALE" in str(exc)
                return "stale"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(write, ["First", "Second"])) == ["saved", "stale"]
    with driver.connect(dsn) as conn:
        assert conn.execute("select count(*) from invoice_settings").fetchone() == (1,)
