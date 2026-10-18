"""Binning a draft invoice or quote never hands its number to another document.

The decision (15 October 2026): a draft nothing has ever happened to is HARD deleted, in
one statement, because refs come from a per-tenant counter that only goes up. This runs
the shipped next_invoice_ref / next_quote_ref (the text is read out of db/schema.sql)
against a disposable real PostgreSQL cluster and proves the three facts the choice rests
on: the number after a deleted draft's is the NEXT one, never the deleted one; the
guarded DELETE the app issues (db.delete_draft_invoice / delete_draft_quote, written here
as the SQL PostgREST turns them into) takes the document's lines with it by the schema's
ON DELETE CASCADE and touches nothing that was sent, paid or opened; and the unique ref
index the invoice table carries would refuse a duplicate if anything ever tried.
"""
from pathlib import Path
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = (ROOT / "db/schema.sql").read_text(encoding="utf-8")


def _function(name: str) -> str:
    match = re.search(r"create or replace function " + name + r" \(.*?end \$\$;", SCHEMA, re.S)
    assert match, f"{name} is not defined in db/schema.sql"
    return match.group(0)


# the columns the app's guarded DELETEs read, and the foreign keys the schema declares
TABLES = """
create table tenants(id uuid primary key);
create table tenant_counters(
  tenant_id uuid primary key references tenants(id) on delete cascade,
  invoice_seq integer not null default 0,
  quote_seq integer not null default 0);
create table invoices(
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete cascade,
  invoice_ref text, status text not null default 'draft',
  sent_at timestamptz, paid_at timestamptz, payment_method text,
  stripe_payment_intent_id text, payment_link text, viewed_at timestamptz,
  amount_paid numeric(12,2) not null default 0);
create unique index invoices_tenant_ref_uniq on invoices (tenant_id, invoice_ref) where invoice_ref is not null;
create table invoice_lines(
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete cascade,
  invoice_id uuid not null references invoices(id) on delete cascade,
  description text not null, amount numeric(12,2) not null default 0);
create table quotes(
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete cascade,
  quote_ref text, status text not null default 'draft',
  sent_at timestamptz, viewed_at timestamptz, accepted_at timestamptz, declined_at timestamptz,
  invoice_id uuid references invoices(id) on delete set null,
  deposit_invoice_id uuid references invoices(id) on delete set null,
  view_count integer not null default 0, reminders_sent integer not null default 0);
create table quote_lines(
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete cascade,
  quote_id uuid not null references quotes(id) on delete cascade,
  description text not null, amount numeric(12,2) not null default 0);
"""

# exactly what db.delete_draft_invoice / db.delete_draft_quote ask PostgREST for
DELETE_INVOICE = """
delete from invoices where id = %s and tenant_id = %s and status = 'draft' and amount_paid = 0
  and sent_at is null and paid_at is null and payment_method is null
  and stripe_payment_intent_id is null and payment_link is null and viewed_at is null
returning id"""
DELETE_QUOTE = """
delete from quotes where id = %s and tenant_id = %s and status = 'draft' and view_count = 0
  and reminders_sent = 0 and sent_at is null and viewed_at is null and accepted_at is null
  and declined_at is null and invoice_id is null and deposit_invoice_id is null
returning id"""


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
    folder = Path(tempfile.mkdtemp(prefix="draft-delete-db-", dir=cache))
    data = folder / "data"
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.run([str(binary / ("initdb" + suffix)), "-D", str(data), "-A", "trust",
                    "-U", "blam8_test", "--no-locale", "-E", "UTF8"], check=True,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   creationflags=flags, timeout=90)
    with (data / "postgresql.conf").open("a", encoding="utf-8") as config:
        config.write("\nunix_socket_directories = ''\n")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    log = (folder / "postgres.log").open("w")
    server = subprocess.Popen([str(binary / ("postgres" + suffix)), "-D", str(data),
                               "-h", "127.0.0.1", "-p", str(port), "-c", "fsync=off"],
                              stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=flags)
    dsn = f"hostaddr=127.0.0.1 port={port} user=blam8_test dbname=postgres connect_timeout=5"
    try:
        for _ in range(100):
            try:
                with psycopg.connect(dsn):
                    break
            except psycopg.OperationalError:
                if server.poll() is not None:
                    raise RuntimeError((folder / "postgres.log").read_text())
                time.sleep(.1)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(TABLES)
            conn.execute(_function("next_invoice_ref"))
            conn.execute(_function("next_quote_ref"))
        yield dsn
    finally:
        server.terminate()
        server.wait(timeout=15)
        log.close()
        shutil.rmtree(folder, ignore_errors=True)


def _tenant(conn):
    tenant = uuid.uuid4()
    conn.execute("insert into tenants(id) values (%s)", (tenant,))
    return tenant


def test_a_deleted_invoice_number_is_never_issued_again(cluster):
    import psycopg
    with psycopg.connect(cluster, autocommit=True) as conn:
        tenant = _tenant(conn)
        ids = {}
        for _ in range(4):
            ref = conn.execute("select next_invoice_ref(%s)", (tenant,)).fetchone()[0]
            ids[ref] = conn.execute("insert into invoices(tenant_id, invoice_ref) values (%s,%s) returning id",
                                    (tenant, ref)).fetchone()[0]
            conn.execute("insert into invoice_lines(tenant_id, invoice_id, description, amount) values (%s,%s,'Work',10)",
                         (tenant, ids[ref]))
        assert list(ids) == ["INV-1001", "INV-1002", "INV-1003", "INV-1004"]
        # bin the LAST one (where max(ref)+1 would reuse its number) and a middle one
        for ref in ("INV-1004", "INV-1002"):
            assert conn.execute(DELETE_INVOICE, (ids[ref], tenant)).fetchone() is not None
        assert conn.execute("select count(*) from invoice_lines where invoice_id = any(%s)",
                            ([ids["INV-1004"], ids["INV-1002"]],)).fetchone()[0] == 0      # the cascade
        assert conn.execute("select count(*) from invoice_lines where tenant_id=%s", (tenant,)).fetchone()[0] == 2
        assert conn.execute("select next_invoice_ref(%s)", (tenant,)).fetchone()[0] == "INV-1005"
        # and were a duplicate ever attempted, the unique index refuses it
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute("insert into invoices(tenant_id, invoice_ref) values (%s,'INV-1003')", (tenant,))


def test_a_deleted_quote_number_is_never_issued_again(cluster):
    import psycopg
    with psycopg.connect(cluster, autocommit=True) as conn:
        tenant = _tenant(conn)
        quotes = {}
        for _ in range(3):
            ref = conn.execute("select next_quote_ref(%s)", (tenant,)).fetchone()[0]
            quotes[ref] = conn.execute("insert into quotes(tenant_id, quote_ref) values (%s,%s) returning id",
                                       (tenant, ref)).fetchone()[0]
            conn.execute("insert into quote_lines(tenant_id, quote_id, description, amount) values (%s,%s,'Work',10)",
                         (tenant, quotes[ref]))
        assert list(quotes) == ["QUO-1001", "QUO-1002", "QUO-1003"]
        assert conn.execute(DELETE_QUOTE, (quotes["QUO-1003"], tenant)).fetchone() is not None
        assert conn.execute("select count(*) from quote_lines where quote_id=%s", (quotes["QUO-1003"],)).fetchone()[0] == 0
        assert conn.execute("select next_quote_ref(%s)", (tenant,)).fetchone()[0] == "QUO-1004"
    # counters are per business: another tenant starts its own run
    with psycopg.connect(cluster, autocommit=True) as conn:
        assert conn.execute("select next_quote_ref(%s)", (_tenant(conn),)).fetchone()[0] == "QUO-1001"


INVOICE_TOUCHES = ["status = 'sent'", "sent_at = now()", "paid_at = now()", "payment_method = 'cash'",
                   "stripe_payment_intent_id = 'pi_1'", "payment_link = 'https://example.test/pay'",
                   "viewed_at = now()", "amount_paid = 5"]
QUOTE_TOUCHES = ["status = 'sent'", "sent_at = now()", "viewed_at = now()", "accepted_at = now()",
                 "declined_at = now()", "view_count = 2", "reminders_sent = 1"]


@pytest.mark.parametrize("assignment", INVOICE_TOUCHES)
def test_the_guarded_invoice_delete_touches_nothing_that_left_the_building(cluster, assignment):
    import psycopg
    with psycopg.connect(cluster, autocommit=True) as conn:
        tenant = _tenant(conn)
        inv = conn.execute("insert into invoices(tenant_id, invoice_ref) values (%s,'X-1') returning id",
                           (tenant,)).fetchone()[0]
        conn.execute("insert into invoice_lines(tenant_id, invoice_id, description) values (%s,%s,'Work')", (tenant, inv))
        conn.execute(f"update invoices set {assignment} where id = %s", (inv,))
        assert conn.execute(DELETE_INVOICE, (inv, tenant)).fetchone() is None
        assert conn.execute("select count(*) from invoices where id=%s", (inv,)).fetchone()[0] == 1
        assert conn.execute("select count(*) from invoice_lines where invoice_id=%s", (inv,)).fetchone()[0] == 1


@pytest.mark.parametrize("assignment", QUOTE_TOUCHES)
def test_the_guarded_quote_delete_touches_nothing_that_left_the_building(cluster, assignment):
    import psycopg
    with psycopg.connect(cluster, autocommit=True) as conn:
        tenant = _tenant(conn)
        quote = conn.execute("insert into quotes(tenant_id, quote_ref) values (%s,'Q-1') returning id",
                             (tenant,)).fetchone()[0]
        conn.execute("insert into quote_lines(tenant_id, quote_id, description) values (%s,%s,'Work')", (tenant, quote))
        conn.execute(f"update quotes set {assignment} where id = %s", (quote,))
        assert conn.execute(DELETE_QUOTE, (quote, tenant)).fetchone() is None
        assert conn.execute("select count(*) from quote_lines where quote_id=%s", (quote,)).fetchone()[0] == 1


def test_a_quote_built_on_a_draft_invoice_keeps_the_guard_honest(cluster):
    """A quote's link to its invoice is ON DELETE SET NULL: the database would let the
    invoice go and silently drop the link. That is why the Concierge checks the link
    itself (db.invoice_linked_quote_ref) and refuses, instead of relying on the delete."""
    import psycopg
    with psycopg.connect(cluster, autocommit=True) as conn:
        tenant = _tenant(conn)
        inv = conn.execute("insert into invoices(tenant_id, invoice_ref) values (%s,'Y-1') returning id",
                           (tenant,)).fetchone()[0]
        quote = conn.execute("insert into quotes(tenant_id, quote_ref, invoice_id) values (%s,'Q-2',%s) returning id",
                             (tenant, inv)).fetchone()[0]
        assert conn.execute(DELETE_INVOICE, (inv, tenant)).fetchone() is not None
        assert conn.execute("select invoice_id from quotes where id=%s", (quote,)).fetchone()[0] is None


def test_the_guarded_deletes_are_tenant_scoped(cluster):
    import psycopg
    with psycopg.connect(cluster, autocommit=True) as conn:
        tenant, other = _tenant(conn), _tenant(conn)
        inv = conn.execute("insert into invoices(tenant_id, invoice_ref) values (%s,'Z-1') returning id",
                           (tenant,)).fetchone()[0]
        quote = conn.execute("insert into quotes(tenant_id, quote_ref) values (%s,'Z-2') returning id",
                             (tenant,)).fetchone()[0]
        assert conn.execute(DELETE_INVOICE, (inv, other)).fetchone() is None
        assert conn.execute(DELETE_QUOTE, (quote, other)).fetchone() is None
        assert conn.execute("select count(*) from invoices where id=%s", (inv,)).fetchone()[0] == 1
        assert conn.execute("select count(*) from quotes where id=%s", (quote,)).fetchone()[0] == 1


def test_the_sql_above_guards_exactly_the_columns_the_app_guards():
    """DELETE_INVOICE / DELETE_QUOTE are the app's guarded deletes written as SQL; if the
    app starts or stops guarding a column, this fails until the SQL (and so the proof
    above) is brought into line."""
    from app import db
    for col in db.UNTOUCHED_INVOICE_NULLS:
        assert f"{col} is null" in DELETE_INVOICE, col
    for col in db.UNTOUCHED_QUOTE_NULLS:
        assert f"{col} is null" in DELETE_QUOTE, col
    for fragment in ("status = 'draft'", "amount_paid = 0", "tenant_id = %s"):
        assert fragment in DELETE_INVOICE
    for fragment in ("status = 'draft'", "view_count = 0", "reminders_sent = 0", "tenant_id = %s"):
        assert fragment in DELETE_QUOTE
