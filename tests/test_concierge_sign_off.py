"""The sign-off record (R4b part 1): confirm, cancel, dismiss and expiry each stamp the card
(decided_at / decided_by / decided_via), once, best effort; the migration is additive and
survives forward / rollback / forward on a real PostgreSQL.

Run: python tests/run_offline.py tests/test_concierge_sign_off.py -q
"""
import os
import shutil
import socket
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import db
from tests.test_concierge_tap_only_delete import (  # noqa: F401
    DELETES, EDITS, T1, _post, _row, _waiting, world)
from tests.test_concierge_draft_edit_delete import rig  # noqa: F401
from tests.test_concierge_draft_edit_delete_quotes import qrig  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def stamps(world, monkeypatch):
    """db.stamp_concierge_decision applied to the in-memory proposals, once-only like the real one."""
    def fake(pid, tenant, via, *, by=None, reprepared_from=None):
        row = next((r for r in world.store.s["concierge_proposals"] if r["id"] == pid), None)
        if row is None or row.get("decided_at"):
            return False
        row.update(decided_at=datetime.now(timezone.utc).isoformat(), decided_by=by, decided_via=via)
        return True
    monkeypatch.setattr(db, "stamp_concierge_decision", fake)
    return world


@pytest.mark.parametrize("via", ["tap", "words"])
def test_confirm_stamps_who_and_how(stamps, via):
    card = _waiting(stamps, EDITS["invoice_edit_draft"])
    _post(stamps, card, confirm_via=via)
    row = _row(stamps, card)
    assert (row["decided_via"], row["decided_by"]) == (via, "u1") and row["decided_at"]


def test_a_confirm_with_no_route_is_stamped_unknown(stamps):
    card = _waiting(stamps, EDITS["invoice_edit_draft"])
    _post(stamps, card)
    assert _row(stamps, card)["decided_via"] == "unknown"


def test_cancel_and_dismiss_stamp_the_person(stamps):
    a = _waiting(stamps, EDITS["invoice_edit_draft"])
    stamps.http.post("/concierge/business/cancel", json={"proposal_id": a["proposal_id"]})
    assert (_row(stamps, a)["status"], _row(stamps, a)["decided_by"]) == ("cancelled", "u1")
    b = _waiting(stamps, EDITS["quote_edit_draft"])
    stamps.http.post("/concierge/business/dismiss", json={"proposal_id": b["proposal_id"]})
    assert (_row(stamps, b)["status"], _row(stamps, b)["decided_by"]) == ("dismissed", "u1")


def test_expiry_is_stamped_by_the_system_not_a_person(stamps):
    card = _waiting(stamps, EDITS["invoice_edit_draft"])
    _row(stamps, card)["created_at"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    _post(stamps, card, confirm_via="tap")
    row = _row(stamps, card)
    assert row["status"] == "expired" and (row["decided_via"], row["decided_by"]) == ("system", None)


def test_a_stamp_failure_never_changes_the_outcome(world, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("column does not exist")
    monkeypatch.setattr(db, "stamp_concierge_decision", boom)
    card = _waiting(world, EDITS["invoice_edit_draft"])
    assert _post(world, card, confirm_via="tap").status_code == 200
    assert _row(world, card)["status"] == "executed"


def test_the_real_stamp_is_once_only_service_side_and_swallows_errors(monkeypatch):
    seen = []

    class Chain:
        def __getattr__(self, name):
            def call(*a, **k):
                seen.append((name, a))
                return self
            return call

        def execute(self):
            return type("R", (), {"data": [{"id": "p"}]})()
    monkeypatch.setattr(db, "db", lambda: type("C", (), {"table": lambda s, n: Chain()})())
    assert db.stamp_concierge_decision("p", "t", "words", by="u1") is True
    assert seen[0][0] == "update" and seen[0][1][0]["decided_via"] == "words"
    assert ("is_", ("decided_at", "null")) in seen
    monkeypatch.setattr(db, "db", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    assert db.stamp_concierge_decision("p", "t", "tap") is False


# ── the migration on a throwaway PostgreSQL ───────────────────────────────────
def test_migration_forward_rollback_forward_keeps_the_stamps():
    psycopg = pytest.importorskip("psycopg")
    base = Path(os.environ["OUTREACH_TEST_POSTGRES_BIN"]) if os.environ.get("OUTREACH_TEST_POSTGRES_BIN") else None
    if base is None:
        binary = shutil.which("initdb")
        base = Path(binary).parent if binary else None
    suffix = ".exe" if os.name == "nt" else ""
    if base is None or not (base / ("initdb" + suffix)).exists():
        pytest.skip("PostgreSQL binaries are required for isolated database tests")
    cache = Path(os.environ.get("BLAM8_PG_TEST_TMP", ROOT / ".cache"))
    cache.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="sign-off-db-", dir=cache))
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.run([str(base / ("initdb" + suffix)), "-D", str(folder / "d"), "-A", "trust", "-U", "t",
                    "--no-locale", "-E", "UTF8"], check=True, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags, timeout=90)
    with (folder / "d" / "postgresql.conf").open("a") as f:
        f.write("\nunix_socket_directories = ''\n")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    log = (folder / "log").open("w")
    server = subprocess.Popen([str(base / ("postgres" + suffix)), "-D", str(folder / "d"), "-h", "127.0.0.1",
                               "-p", str(port), "-c", "fsync=off"], stdout=log, stderr=log, creationflags=flags)
    dsn = f"hostaddr=127.0.0.1 port={port} user=t dbname=postgres connect_timeout=5"
    try:
        for _ in range(100):
            try:
                psycopg.connect(dsn).close()
                break
            except psycopg.OperationalError:
                time.sleep(.1)
        fwd = (ROOT / "db/migrations/20261016_concierge_sign_off.sql").read_text(encoding="utf-8")
        back = (ROOT / "db/migrations/20261016_concierge_sign_off.rollback.sql").read_text(encoding="utf-8")
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("create table concierge_proposals(id uuid primary key default gen_random_uuid(), "
                      "status text, summary text)")
            c.execute("insert into concierge_proposals(status, summary) values ('executed','old row')")
            c.execute(fwd)
            c.execute(fwd)                                           # idempotent
            c.execute("update concierge_proposals set decided_via='tap', decided_at=now()")
            with pytest.raises(psycopg.errors.CheckViolation):
                c.execute("update concierge_proposals set decided_via='bogus'")
            c.execute(back)
            assert c.execute("select count(*) from concierge_proposals where decided_via='tap'").fetchone()[0] == 1
            c.execute(fwd)                                           # forward again
            cols = {r[0] for r in c.execute("select column_name from information_schema.columns "
                                            "where table_name='concierge_proposals'")}
            assert {"decided_at", "decided_by", "decided_via", "reprepared_from"} <= cols
            assert c.execute("select summary from concierge_proposals").fetchone()[0] == "old row"
    finally:
        server.terminate()
        server.wait(20)
        log.close()
        shutil.rmtree(folder, ignore_errors=True)
