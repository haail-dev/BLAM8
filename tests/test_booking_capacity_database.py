"""Real PostgreSQL races on a disposable local cluster, never a supplied DSN."""
from tests.booking_fixtures import carried_config
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "db/migrations/20260927_booking_guarantees.sql"


@pytest.fixture(scope="module")
def cluster():
    psycopg = pytest.importorskip("psycopg")
    binary = Path(os.environ["OUTREACH_TEST_POSTGRES_BIN"]) if os.environ.get("OUTREACH_TEST_POSTGRES_BIN") else None
    if binary is None:
        pg_config = shutil.which("pg_config")
        if pg_config:
            binary = Path(subprocess.check_output([pg_config, "--bindir"], text=True).strip())
    suffix = ".exe" if os.name == "nt" else ""
    if not binary or not (binary / ("initdb" + suffix)).exists():
        pytest.skip("PostgreSQL binaries are required for isolated database tests")
    cache = Path(os.environ.get("BLAM8_PG_TEST_TMP", ROOT / ".cache"))
    cache.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="booking-db-", dir=cache))
    data = folder / "data"
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    def run(tool, *args):
        # Windows server children inherit pipe handles; communicate() can wait
        # forever after pg_ctl exits. A file also preserves startup diagnostics.
        with (folder / "commands.log").open("a") as output:
            return subprocess.run([str(binary / (tool + suffix)), *map(str, args)],
                                  check=True, stdin=subprocess.DEVNULL, stdout=output,
                                  stderr=output, text=True, creationflags=flags, timeout=90)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    run("initdb", "-D", data, "-A", "trust", "-U", "blam8_test", "--no-locale", "-E", "UTF8")
    with (data / "postgresql.conf").open("a", encoding="utf-8") as config:
        config.write("\nunix_socket_directories = ''\n")
    # pg_ctl's Windows token reduction fails inside an already restricted sandbox.
    # Launch only this newly initialised cluster, hidden, under the current token.
    with (folder / "postgres.log").open("w") as output:
        server = subprocess.Popen([str(binary / ("postgres" + suffix)), "-D", str(data),
                                   "-h", "127.0.0.1", "-p", str(port), "-c", "fsync=off"],
                                  stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                  creationflags=flags)
    # libpq uses this freshly created loopback cluster only, not PG* variables.
    dsn = f"hostaddr=127.0.0.1 port={port} user=blam8_test dbname=postgres connect_timeout=5"
    try:
        for attempt in range(100):
            try:
                with psycopg.connect(dsn):
                    break
            except psycopg.OperationalError:
                if server.poll() is not None:
                    raise RuntimeError((folder / "postgres.log").read_text())
                time.sleep(0.1)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("""
                create table scheduling_config (
                  tenant_id uuid primary key, capacity_per_window int default 4,
                  open_time time default '09:00', close_time time default '17:00',
                  slot_minutes int default 60, buffer_minutes int default 0,
                  resource_types jsonb default '[]', service_types jsonb default '[]',
                  intake_fields jsonb default '[]');
                create table bookings (
                  id uuid primary key default gen_random_uuid(), tenant_id uuid not null,
                  booking_date date not null, "window" text default 'day',
                  start_block int, length_blocks int, quantity int, resource_type text,
                  status text default 'proposed', expires_at timestamptz,
                  spaces numeric, engineer_id uuid, source text default 'email',
                  details jsonb default '{}', updated_at timestamptz, notes text, calendar_event_id text);
                create table crm_artifacts (id uuid primary key, tenant_id uuid, instance_id uuid, contact_id uuid);
                create table requirement_groups (id uuid primary key, tenant_id uuid, fields jsonb);
                create table requirement_instances (id uuid primary key, tenant_id uuid, group_id uuid, contact_id uuid);
                create table asset_units (id uuid primary key, tenant_id uuid not null);
            """)
            conn.execute(MIGRATION.read_text(encoding="utf-8"))
        yield psycopg, dsn
    finally:
        if server.poll() is None:
            run("pg_ctl", "-D", data, "-m", "fast", "-w", "stop")
        server.wait(timeout=10)


@pytest.fixture
def db_cluster(cluster):
    driver, dsn = cluster
    tenant = uuid.uuid4()
    with driver.connect(dsn, autocommit=True) as conn:
        conn.execute("insert into scheduling_config(tenant_id,capacity_per_window) values (%s,1)", (tenant,))
    return driver, dsn, tenant


def insert(conn, tenant, start=40, length=4, day="2026-10-01", **kw):
    from psycopg import sql
    row = {"tenant_id": tenant, "booking_date": day, "start_block": start,
           "length_blocks": length, **kw}
    query = sql.SQL("insert into bookings ({}) values ({}) returning id").format(
        sql.SQL(",").join(map(sql.Identifier, row)),
        sql.SQL(",").join(sql.Placeholder() for _ in row))
    return conn.execute(query, list(row.values())).fetchone()[0]


def test_two_customers_cannot_both_take_the_last_place(db_cluster):
    driver, dsn, tenant = db_cluster
    ready = threading.Event()

    def contender():
        with driver.connect(dsn) as second:
            ready.set()
            try:
                insert(second, tenant)
                return "held"
            except driver.errors.RaiseException as exc:
                return str(exc)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with driver.connect(dsn) as first:
            insert(first, tenant)
            future = pool.submit(contender)
            assert ready.wait(5)
            time.sleep(0.1)
            assert not future.done()  # waits on first transaction, not a stale pre-check
        assert "BLAM8_CAPACITY_CONFLICT" in future.result(timeout=5)


def test_cross_day_stay_competes_with_next_days_appointment(db_cluster):
    driver, dsn, tenant = db_cluster
    with driver.connect(dsn, autocommit=True) as c:
        insert(c, tenant, start=40, length=100)
        with pytest.raises(driver.errors.RaiseException, match="BLAM8_CAPACITY_CONFLICT"):
            insert(c, tenant, start=40, day="2026-10-02")


def test_adjacent_bookings_and_peak_occupancy_are_not_summed(db_cluster):
    driver, dsn, tenant = db_cluster
    with driver.connect(dsn, autocommit=True) as c:
        c.execute("update scheduling_config set capacity_per_window=2 where tenant_id=%s", (tenant,))
        insert(c, tenant, start=40)
        insert(c, tenant, start=44)
        insert(c, tenant, start=40, length=8)  # peak = 2, not 3


def test_quantity_and_independent_resource_pools(db_cluster):
    driver, dsn, tenant = db_cluster
    with driver.connect(dsn, autocommit=True) as c:
        c.execute("update scheduling_config set resource_types=%s where tenant_id=%s",
                  ('[{"name":"A","count":2},{"name":"B","count":1}]', tenant))
        insert(c, tenant, resource_type="A", quantity=2)
        insert(c, tenant, resource_type="B")
        with pytest.raises(driver.errors.RaiseException, match="BLAM8_CAPACITY_CONFLICT"):
            insert(c, tenant, resource_type="A")


def test_move_is_atomic_and_leaves_original_row_on_conflict(db_cluster):
    driver, dsn, tenant = db_cluster
    with driver.connect(dsn, autocommit=True) as c:
        insert(c, tenant)
        other = insert(c, tenant, start=48)
        with pytest.raises(driver.errors.RaiseException, match="BLAM8_CAPACITY_CONFLICT"):
            c.execute("update bookings set start_block=40 where id=%s", (other,))
        assert c.execute("select start_block from bookings where id=%s", (other,)).fetchone() == (48,)


def test_expired_offer_cannot_steal_a_reallocated_place(db_cluster):
    driver, dsn, tenant = db_cluster
    with driver.connect(dsn, autocommit=True) as c:
        old = insert(c, tenant, status="offered", expires_at="2000-01-01T00:00Z")
        insert(c, tenant)
        with pytest.raises(driver.errors.RaiseException, match="BLAM8_CAPACITY_CONFLICT"):
            c.execute("update bookings set status='proposed',expires_at=null where id=%s", (old,))


def test_buffer_and_daily_limit_are_database_constraints(db_cluster):
    driver, dsn, tenant = db_cluster
    with driver.connect(dsn, autocommit=True) as c:
        c.execute("update scheduling_config set buffer_minutes=15,service_types=%s where tenant_id=%s",
                  ('[{"name":"Visit","max_per_day":1}]', tenant))
        insert(c, tenant, details='{"Service":"Visit"}')
        with pytest.raises(driver.errors.RaiseException, match="BLAM8_CAPACITY_CONFLICT"):
            insert(c, tenant, start=44)
        with pytest.raises(driver.errors.RaiseException, match="daily service limit"):
            insert(c, tenant, start=56, details='{"Service":"Visit"}')


def test_live_offer_confirms_in_place_and_other_tenants_do_not_conflict(db_cluster):
    driver, dsn, tenant = db_cluster
    with driver.connect(dsn, autocommit=True) as c:
        bid = insert(c, tenant, status="offered", expires_at="2099-01-01T00:00Z")
        c.execute("update bookings set status='confirmed',expires_at=null where id=%s", (bid,))
        insert(c, uuid.uuid4())


def test_migration_preserves_legacy_terms_and_does_not_rewrite_new_policies(db_cluster):
    driver, dsn, tenant = db_cluster
    with driver.connect(dsn, autocommit=True) as c:
        c.execute("update scheduling_config set service_types=%s where tenant_id=%s", (
            '[{"name":"Legacy","out_of_hours":{"until":"22:00","price":90}},'
            '{"name":"New","out_of_hours":{"until":"22:00","price":80,"price_basis":"unspecified"}},'
            '{"name":"Owned","out_of_hours":{"until":"22:00","price":70,"terms":"Materials included."}}]', tenant))
        c.execute(MIGRATION.read_text(encoding="utf-8"))
        policies = c.execute("select service_types from scheduling_config where tenant_id=%s", (tenant,)).fetchone()[0]
        assert policies[0]["out_of_hours"]["price_basis"] == "first_hour"
        assert "Parts are extra" in policies[0]["out_of_hours"]["terms"]
        assert policies[0]["out_of_hours"]["carried_over_review"] == ["price_basis", "terms"]
        assert policies[2]["out_of_hours"]["carried_over_review"] == ["price_basis"]
        assert "carried_over_review" not in policies[1]["out_of_hours"]
        assert "terms" not in policies[1]["out_of_hours"]
        assert policies[2]["out_of_hours"]["terms"] == "Materials included."
        c.execute(MIGRATION.read_text(encoding="utf-8"))
        assert c.execute("select service_types from scheduling_config where tenant_id=%s", (tenant,)).fetchone()[0] == policies


def test_schema_contains_the_same_guard_as_the_standalone_migration():
    assert MIGRATION.read_text(encoding="utf-8").strip() in (ROOT / "db/schema.sql").read_text(encoding="utf-8")


@pytest.mark.parametrize("column,value", [("calendar_event_id", "synced"), ("notes", "owner note"), ("status", "confirmed")])
def test_non_capacity_updates_do_not_take_the_business_lock(db_cluster, column, value):
    from psycopg import sql
    driver, dsn, tenant = db_cluster
    with driver.connect(dsn, autocommit=True) as setup:
        bid = insert(setup, tenant)
    with driver.connect(dsn) as locker, driver.connect(dsn) as writer:
        locker.execute("select pg_advisory_xact_lock(hashtextextended('blam8:office:' || %s,0))", (str(tenant),))
        writer.execute("set local lock_timeout='200ms'")
        writer.execute(sql.SQL("update bookings set {}=%s where id=%s").format(sql.Identifier(column)), (value, bid))
        assert writer.execute("select count(*) from pg_locks where pid=pg_backend_pid() and locktype='advisory'").fetchone() == (0,)


def test_calendar_import_and_fleet_rows_do_not_take_the_lock(db_cluster):
    driver, dsn, tenant = db_cluster
    with driver.connect(dsn) as locker, driver.connect(dsn) as writer:
        locker.execute("select pg_advisory_xact_lock(hashtextextended('blam8:office:' || %s,0))", (str(tenant),))
        writer.execute("set local lock_timeout='200ms'")
        bid = insert(writer, tenant, source="calendar")
        writer.execute("update bookings set start_block=48 where id=%s", (bid,))
        insert(writer, tenant, spaces=3)
        assert writer.execute("select count(*) from pg_locks where pid=pg_backend_pid() and locktype='advisory'").fetchone() == (0,)


def test_removed_pool_can_confirm_and_move_but_still_counts(db_cluster):
    driver, dsn, tenant = db_cluster
    with driver.connect(dsn, autocommit=True) as c:
        c.execute("update scheduling_config set resource_types=%s where tenant_id=%s", ('[{"name":"Old","count":1}]', tenant))
        bid = insert(c, tenant, resource_type="Old", status="offered")
        c.execute("update scheduling_config set resource_types=%s where tenant_id=%s", ('[{"name":"Renamed","count":1}]', tenant))
        c.execute("update bookings set status='confirmed' where id=%s", (bid,))
        c.execute("update bookings set start_block=48 where id=%s", (bid,))
        with pytest.raises(driver.errors.RaiseException, match="BLAM8_CAPACITY_CONFLICT"):
            insert(c, tenant, start=48, resource_type="Old")
        with pytest.raises(driver.errors.RaiseException, match="BLAM8_CAPACITY_CONFLICT"):
            insert(c, tenant, start=48)


def test_explicit_stay_mode_survives_a_renamed_field(db_cluster):
    driver, dsn, tenant = db_cluster
    with driver.connect(dsn, autocommit=True) as c:
        c.execute("update scheduling_config set booking_mode='stay', intake_fields=%s, resource_types=%s where tenant_id=%s",
                  ('[{"label":"End"}]', '[{"name":"Visit","count":1}]', tenant))
        insert(c, tenant, resource_type="Visit")
        insert(c, tenant, length=100)
        with pytest.raises(driver.errors.RaiseException, match="BLAM8_CAPACITY_CONFLICT"):
            insert(c, tenant, day="2026-10-02")


@pytest.mark.parametrize("proof_count", [1, 2])
def test_migration_and_new_documents_classify_only_single_proof_groups(db_cluster, proof_count):
    import json
    driver, dsn, tenant = db_cluster
    group, instance, contact, doc = [uuid.uuid4() for _ in range(4)]
    with driver.connect(dsn, autocommit=True) as c:
        fields = [{"key": f"proof_{i}", "label": f"Proof {i}", "proof": True,
                   "gate": True, "type": "date", "durability": "expires"} for i in range(proof_count)]
        c.execute("insert into requirement_groups values (%s,%s,%s)", (group, tenant, json.dumps(fields)))
        c.execute("insert into requirement_instances values (%s,%s,%s,%s)", (instance, tenant, group, contact))
        c.execute("insert into crm_artifacts(id,tenant_id,instance_id,contact_id) values (%s,%s,%s,%s)", (doc, tenant, instance, contact))
        expected = ["proof_0"] if proof_count == 1 else []
        assert c.execute("select requirement_keys from crm_artifacts where id=%s", (doc,)).fetchone()[0] == expected
        from app import requirement_groups as rg
        group_cfg = rg._ensure({"label": "Participant", "fields": fields})
        data = {f["key"]: "2027-01-01" for f in fields}
        gaps = rg.missing_for(group_cfg,data,"2026-10-01",has_proof=set(expected))
        assert len(gaps) == (0 if proof_count == 1 else 2)
        # A file linked to its subject later is classified by the same rule.
        late_doc = uuid.uuid4()
        c.execute("insert into crm_artifacts(id,tenant_id,contact_id) values (%s,%s,%s)", (late_doc,tenant,contact))
        c.execute("update crm_artifacts set instance_id=%s where id=%s", (instance,late_doc))
        assert c.execute("select requirement_keys from crm_artifacts where id=%s", (late_doc,)).fetchone()[0] == expected
        # An existing pre-migration file follows the identical path.
        c.execute("alter table crm_artifacts disable trigger artifacts_single_proof")
        try:
            c.execute("update crm_artifacts set requirement_keys='{}' where id=%s", (doc,))
        finally:
            c.execute("alter table crm_artifacts enable trigger artifacts_single_proof")
        c.execute(MIGRATION.read_text(encoding="utf-8"))
        assert c.execute("select requirement_keys from crm_artifacts where id=%s", (doc,)).fetchone()[0] == expected


def test_migration_backfills_stay_toggle_only_once(db_cluster):
    driver, dsn, tenant = db_cluster
    with driver.connect(dsn, autocommit=True) as c:
        c.execute("alter table scheduling_config alter column booking_mode drop not null")
        c.execute("update scheduling_config set booking_mode=null,intake_fields=%s where tenant_id=%s", ('[{"label":"Departure / collection date & time"}]', tenant))
        c.execute(MIGRATION.read_text(encoding="utf-8"))
        assert c.execute("select booking_mode from scheduling_config where tenant_id=%s", (tenant,)).fetchone() == ("stay",)
        c.execute("update scheduling_config set intake_fields='[]' where tenant_id=%s", (tenant,))
        c.execute(MIGRATION.read_text(encoding="utf-8"))
        assert c.execute("select booking_mode from scheduling_config where tenant_id=%s", (tenant,)).fetchone() == ("stay",)


def _database_verdict(conn, tenant, cfg, booked, requested):
    """Load the SAME application fixture, including authoritative historic rows."""
    import json
    from psycopg import sql
    conn.execute("delete from bookings where tenant_id=%s", (tenant,))
    keys = ("capacity_per_window", "open_time", "close_time", "slot_minutes", "buffer_minutes", "resource_types", "service_types", "booking_mode")
    defaults = dict(capacity_per_window=4, open_time="09:00", close_time="17:00", slot_minutes=60,
                    buffer_minutes=0, resource_types=[], service_types=[], booking_mode="appointment")
    data = {**defaults, **{k: v for k,v in (cfg or {}).items() if k in keys}}
    conn.execute(sql.SQL("update scheduling_config set {} where tenant_id=%s").format(
        sql.SQL(',').join(sql.SQL('{}=%s').format(sql.Identifier(k)) for k in keys)),
        [json.dumps(data[k]) if isinstance(data[k],list) else data[k] for k in keys] + [tenant])
    # Bypass only while installing existing reality, which may exceed current capacity.
    conn.execute("alter table bookings disable trigger bookings_capacity_guard")
    try:
        for b in booked:
            row = {k:v for k,v in b.items() if k in {"window","quantity","resource_type","status","spaces","engineer_id","expires_at","details"}}
            if row.get("engineer_id"): row["engineer_id"] = uuid.uuid4()
            if isinstance(row.get("details"),dict): row["details"] = json.dumps(row["details"])
            insert(conn, tenant, start=b.get("start_block"), length=b.get("length_blocks"), day=b["booking_date"], **row)
    finally:
        conn.execute("alter table bookings enable trigger bookings_capacity_guard")
    row = dict(requested)
    day = row.pop("booking_date")
    start, length = row.pop("start_block",None), row.pop("length_blocks",None)
    if isinstance(row.get("details"),dict): row["details"] = json.dumps(row["details"])
    try:
        insert(conn, tenant, start=start, length=length, day=day, **row)
        return True
    except Exception as exc:
        if "BLAM8_CAPACITY_CONFLICT" in str(exc): return False
        raise


def test_database_parity_with_all_existing_office_capacity_fixtures(db_cluster):
    from tests.test_slotgrid_parity import CONFIGS, _aligned_booking_sets, WED, NOW
    from app import scheduling, slotgrid
    driver, dsn, tenant = db_cluster
    with driver.connect(dsn, autocommit=True) as c:
        for cfg in CONFIGS:
            for booked in _aligned_booking_sets(cfg):
                # Capacity parity only: notice/closed/horizon are separate policy gates.
                norm = scheduling._norm(cfg)
                available = {w for w in slotgrid.office_windows(cfg)
                             if slotgrid.window_used(booked,WED,w,cfg,norm["slot_minutes"]) < norm["capacity"]}
                for window in slotgrid.office_windows(cfg):
                    assert _database_verdict(c, tenant, cfg, booked, {"booking_date": WED, "window": window}) == (window in available), (cfg, booked, window)


def test_database_parity_with_existing_stay_capacity_tests(db_cluster, monkeypatch):
    from tests import test_slotgrid_stays as cases
    from app import slotgrid
    driver, dsn, tenant = db_cluster
    original = slotgrid.stay_fits
    seen = set()
    with driver.connect(dsn, autocommit=True) as c:
        def parity(spans, start, length, capacity, qty=1, buffer_blocks=0):
            spans = list(spans)
            expected = original(spans,start,length,capacity,qty,buffer_blocks)
            key = (tuple(spans),start,length,capacity,qty,buffer_blocks)
            if key not in seen:
                seen.add(key)
                def row(s,n,q=1):
                    dt = slotgrid.abs_to_datetime(s)
                    return dict(booking_date=dt.date().isoformat(),start_block=s%96,length_blocks=n,quantity=q)
                assert _database_verdict(c,tenant,dict(capacity_per_window=capacity,buffer_minutes=buffer_blocks*15),
                                         [row(*s) for s in spans],row(start,length,qty)) == expected
            return expected
        monkeypatch.setattr(slotgrid,"stay_fits",parity)
        for name, case in vars(cases).items():
            if name.startswith("test_"): case()
        # Weighted quantities, buffers, independent spans and adjacent endpoints.
        start = slotgrid.abs_block("2026-10-01",40)
        for cap in (1,2,4):
            for qty in (1,2):
                for gap in (0,1,4):
                    for offset in (0,3,4,5,8):
                        parity([(start,4,2),(start+4,4,1)],start+offset,4,cap,qty,gap)
    assert seen



def test_legacy_move_reserves_its_final_coordinates_in_one_write(db_cluster):
    driver, dsn, tenant = db_cluster
    with driver.connect(dsn,autocommit=True) as c:
        bid = insert(c,tenant)
        taken = insert(c,tenant,day="2026-10-02",start=48)
        with pytest.raises(driver.errors.RaiseException, match="BLAM8_CAPACITY_CONFLICT"):
            c.execute("select * from move_booking_if_capacity(%s,%s,'2026-10-02','pm')",(tenant,bid))
        assert str(c.execute("select booking_date from bookings where id=%s",(bid,)).fetchone()[0]) == "2026-10-01"
        c.execute("delete from bookings where id=%s",(taken,))
        c.execute("select * from move_booking_if_capacity(%s,%s,'2026-10-02','pm')",(tenant,bid))
        assert c.execute('select start_block,length_blocks,"window" from bookings where id=%s',(bid,)).fetchone() == (48,20,"pm")


@pytest.mark.parametrize("resource", [None, "Meet & greet", "Removed"])
@pytest.mark.parametrize("buffer", [0,15])
def test_database_parity_with_appointment_beside_stay_fixtures(db_cluster,resource,buffer):
    from tests.test_appointments_on_stays import _cfg, _rows
    from app import scheduling, slotgrid, diary_config
    driver,dsn,tenant = db_cluster
    cfg = _cfg(booking_mode="stay",capacity_per_window=1,buffer_minutes=buffer)
    booked = list(_rows("2026-10-01"))
    booked.append({**booked[1],"resource_type":"Removed"})
    cap = (diary_config.find_resource(cfg["resource_types"],resource) or {}).get("count",cfg["capacity_per_window"])
    spans = scheduling._pool_spans(booked,cfg,resource,exclude_typed=resource is None)
    with driver.connect(dsn,autocommit=True) as c:
        for start in (28,40,42,44,52):
            expected = (resource != "Removed" and slotgrid.stay_fits(
                spans,slotgrid.abs_block("2026-10-01",start),2,cap,buffer_blocks=buffer//15))
            assert _database_verdict(c,tenant,cfg,booked,dict(booking_date="2026-10-01",start_block=start,length_blocks=2,resource_type=resource)) == expected


def test_legacy_day_capacity_counts_both_halves_like_the_app(db_cluster):
    from app import scheduling, slotgrid
    driver,dsn,tenant = db_cluster
    cfg = carried_config({"capacity_per_window":3})
    booked = [dict(booking_date="2026-10-01",window=w,status="confirmed") for w in ("am","pm","day")]
    expected = slotgrid.window_used(booked,"2026-10-01","day",cfg,60) < scheduling._norm(cfg)["capacity"]
    with driver.connect(dsn,autocommit=True) as c:
        assert _database_verdict(c,tenant,cfg,booked,dict(booking_date="2026-10-01",window="day")) == expected == False
