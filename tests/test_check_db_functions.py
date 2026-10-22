"""The release check that compares installed database functions with the migrations."""
import importlib.util
import io
from pathlib import Path

import pytest

from tests.test_booking_capacity_database import ROOT, cluster as base_cluster

spec = importlib.util.spec_from_file_location("check_db_functions", ROOT / "scripts/ops/check_db_functions.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)

OLD = """begin;
create or replace function change_card(p uuid) returns int language plpgsql as $$
begin
  -- Allowed keys: a, b
  return 1;
end $$;
commit;
"""
NEW = OLD.replace("a, b", "a, b, c").replace("return 1", "return 2")


def write(folder, name, text):
    (folder / name).write_text(text, encoding="utf-8")


def test_a_migration_body_is_read_between_its_dollar_quotes_and_compared_without_spacing():
    sql = ("create or replace function public.\"Tidy\"(a int) returns int as $f$ select  a\r\n  + 1; $f$ language sql;\n"
           "create or replace function other.keep(b int) returns int as $$ select b $$ language sql;\n"
           "create or replace function nested() returns text as $f$ select 'create or replace function ghost() as $$ x $$' $f$ language sql;")
    found = dict(check.definitions(sql))
    assert found[("public", "tidy")] == "select a + 1;"
    assert found[("other", "keep")] == "select b"
    assert ("public", "ghost") not in found and ("public", "nested") in found


def test_latest_file_wins_rollbacks_are_ignored_and_the_report_names_the_file(tmp_path):
    write(tmp_path, "20261004_card.sql", OLD)
    write(tmp_path, "20261019_card.sql", NEW)
    write(tmp_path, "20261019_card.rollback.sql", OLD.replace("return 1", "return 0"))
    history, rewriters = check.load_migrations(tmp_path)
    assert rewriters == {}
    assert [name for name, _ in history[("public", "change_card")]] == ["20261004_card.sql", "20261019_card.sql"]
    old, new = (check.normalise(check.definitions(t).__next__()[1]) for t in (OLD, NEW))
    key = ("public", "change_card")
    assert check.report(history, {key: [new]}) == (["change_card: ok"], 0, 0)
    lines, failures, _ = check.report(history, {key: [old]})
    assert failures == 1 and lines == [
        "change_card: differs (latest definition: 20261019_card.sql; installed body matches the older definition in 20261004_card.sql)"]
    assert check.report(history, {key: ["something else"]})[0] == ["change_card: differs (latest definition: 20261019_card.sql)"]
    assert check.report(history, {}) == (["change_card: missing"], 1, 0)
    assert check.report(history, {key: ["something else", new]})[1] == 0  # any overload may match


def test_a_function_a_later_migration_rewrites_in_place_is_not_checked_and_does_not_fail_the_run(tmp_path):
    write(tmp_path, "20260927_guard.sql", OLD.replace("change_card", "guard"))
    write(tmp_path, "20261004_patch.sql", """do $$ declare body text; begin
      select prosrc into body from pg_proc where proname='guard';
      execute format('create or replace function %s returns int language plpgsql as $f$%s$f$', 'public.guard(uuid)', replace(body,'1','7'));
    end $$;""")
    write(tmp_path, "20261019_other.sql", NEW.replace("change_card", "other"))
    history, rewriters = check.load_migrations(tmp_path)
    assert rewriters == {("public", "guard"): ["20261004_patch.sql"]}
    installed = {("public", "guard"): ["not the first file's text"], ("public", "other"): [check.normalise(check.definitions(NEW).__next__()[1])]}
    lines, failures, unchecked = check.report(history, installed, rewriters)
    assert lines == ["guard: not checked (rewritten in place by 20261004_patch.sql)", "other: ok"]
    assert (failures, unchecked) == (0, 1)
    assert check.report(history, {}, rewriters)[0][0] == "guard: missing"  # an absent function is still a failure


class Connection:
    """Stands in for psycopg: records that the session was made read-only and answers the catalogue query."""

    def __init__(self, rows, log):
        self.rows, self.log, self.read_only = rows, log, False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.log.append(("read_only", self.read_only))

    def execute(self, sql, params):
        self.log.append(("sql", sql.split()[0].lower(), params))
        return type("Cursor", (), {"fetchall": lambda _self: self.rows})()


def test_main_runs_read_only_reports_and_sets_the_exit_status(tmp_path):
    write(tmp_path, "20261019_card.sql", NEW)
    log, out = [], io.StringIO()
    rows = [("public", "change_card", check.definitions(NEW).__next__()[1])]
    connect = lambda url, **kw: (log.append(("connect", url)), Connection(rows, log))[1]
    status = check.main(["--migrations", str(tmp_path)], {"SUPABASE_DB_URL": "postgresql://u:pw@h/db"}, connect, out)
    assert status == 0 and out.getvalue().splitlines()[0] == "change_card: ok"
    assert ("read_only", True) in log and [e[1] for e in log if e[0] == "sql"] == ["select"]
    out = io.StringIO()
    assert check.main(["--migrations", str(tmp_path)], {"SUPABASE_DB_URL": "x"},
                      lambda url, **kw: Connection([], log), out) == 1
    assert out.getvalue().splitlines()[0] == "change_card: missing"


def test_main_never_prints_the_connection_string(tmp_path, capsys):
    write(tmp_path, "20261019_card.sql", NEW)
    url = "postgresql://user:hunter2@db.example.com:5432/postgres"
    def refuse(value, **kw):
        raise RuntimeError(f"connection to {value} failed; also postgres://other:secret@elsewhere/db")
    assert check.main(["--migrations", str(tmp_path)], {"SUPABASE_DB_URL": url}, refuse) == 2
    captured = capsys.readouterr()
    assert "hunter2" not in captured.out + captured.err and "secret" not in captured.out + captured.err
    assert "RuntimeError" in captured.err
    assert check.main(["--migrations", str(tmp_path)], {}, refuse) == 2
    assert "SUPABASE_DB_URL is not set" in capsys.readouterr().err
    empty = tmp_path / "empty"
    empty.mkdir()
    assert check.main(["--migrations", str(empty)], {"SUPABASE_DB_URL": url}, refuse) == 2


def test_the_repository_migrations_parse_and_the_newest_card_function_is_the_diary_mode_one():
    history, rewriters = check.load_migrations(ROOT / "db/migrations")
    assert len(history) > 50
    # These two are rewritten by 20261004_booking_mode_history.sql from their installed bodies.
    assert {("public", "booking_span_bounds"), ("public", "enforce_booking_capacity")} <= set(rewriters)
    assert ("public", "change_setup_card") not in rewriters
    assert [n for n, _ in history[("public", "change_setup_card")]] == [
        "20261004_setup_cards.sql", "20261019_diary_booking_mode_setup_cards.sql"]


@pytest.fixture
def cluster():
    yield from base_cluster.__wrapped__()


def test_database_with_an_applied_migration_is_ok_and_a_missed_one_is_reported(cluster, tmp_path, capsys):
    driver, dsn = cluster
    earlier, later = ROOT / "db/migrations/20261004_setup_cards.sql", ROOT / "db/migrations/20261019_diary_booking_mode_setup_cards.sql"
    folder = tmp_path / "migrations"
    folder.mkdir()
    write(folder, later.name, later.read_text(encoding="utf-8"))
    with driver.connect(dsn, autocommit=True) as conn:
        conn.execute("create table tenants(id uuid primary key); create table knowledge_chunks(id uuid primary key, "
                     "tenant_id uuid, source text, mailbox_id uuid, content text, embedding text, source_chunk_index int); "
                     "create table invoice_settings(tenant_id uuid primary key, default_quote_valid_days int); "
                     "create table tenant_config(tenant_id uuid primary key, setup_cards jsonb not null default '{}'); "
                     "create function auth_tenant_ids() returns setof uuid language sql as 'select null::uuid'")
        conn.execute(earlier.read_text(encoding="utf-8"))
    # The 2026-10-04 function is installed but the 2026-10-19 migration was never applied: the incident.
    assert check.main(["--migrations", str(folder)], {"SUPABASE_DB_URL": dsn}, driver.connect) == 1
    assert ("change_setup_card: differs (latest definition: " + later.name) in capsys.readouterr().out
    with driver.connect(dsn, autocommit=True) as conn:
        conn.execute(later.read_text(encoding="utf-8"))
    assert check.main(["--migrations", str(folder)], {"SUPABASE_DB_URL": dsn}, driver.connect) == 0
    assert capsys.readouterr().out.splitlines()[0] == "change_setup_card: ok"
    write(folder, "20261020_extra.sql", NEW)
    assert check.main(["--migrations", str(folder)], {"SUPABASE_DB_URL": dsn}, driver.connect) == 1
    assert "change_card: missing" in capsys.readouterr().out
