"""S1f migration: disposable PostgreSQL, preserved evidence and tenant RLS."""
from pathlib import Path

from tests.test_booking_capacity_database import cluster  # isolated cluster fixture

ROOT = Path(__file__).resolve().parents[1]


def test_forward_rollback_forward_preserves_rows_and_rls(cluster):
    driver, dsn = cluster
    forward = (ROOT / "db/migrations/20260928_cost_receipts.sql").read_text()
    rollback = (ROOT / "db/migrations/20260928_cost_receipts.rollback.sql").read_text()
    with driver.connect(dsn) as conn:
        conn.execute("""
            CREATE TABLE usage_events (id int primary key, tenant_id text,
                cost_usd numeric(12,6) not null default 0);
            CREATE TABLE llm_calls (id int primary key, tenant_id text);
            ALTER TABLE usage_events ENABLE ROW LEVEL SECURITY;
            ALTER TABLE llm_calls ENABLE ROW LEVEL SECURITY;
            CREATE ROLE cost_receipt_reader;
            GRANT SELECT ON usage_events, llm_calls TO cost_receipt_reader;
            CREATE POLICY tenant_usage ON usage_events FOR SELECT
                USING (tenant_id = current_setting('test.tenant_id'));
            INSERT INTO usage_events VALUES (1, 'a', .125), (2, 'b', .25);
            INSERT INTO llm_calls VALUES (1, 'a');
        """)
        conn.execute(forward)
        assert conn.execute("SELECT raw_usage, cost_version FROM usage_events WHERE id=1").fetchone() == (None, None)
        conn.execute("""UPDATE usage_events SET raw_usage='{"complete":false,"attempts":[{"attempt_id":"retained"}]}',
                        cost_version='test-version' WHERE id=2""")
        before = conn.execute("SELECT * FROM usage_events ORDER BY id").fetchall()
        for sql in (rollback, forward):
            conn.execute(sql)
            assert conn.execute("SELECT * FROM usage_events ORDER BY id").fetchall() == before
            conn.execute("SET LOCAL ROLE cost_receipt_reader")
            conn.execute("SET LOCAL test.tenant_id = 'a'")
            assert conn.execute("SELECT id FROM usage_events").fetchall() == [(1,)]
            assert conn.execute("SELECT * FROM llm_calls").fetchall() == []
            conn.execute("RESET ROLE")
        columns = conn.execute("""SELECT is_nullable, column_default FROM information_schema.columns
            WHERE table_name IN ('usage_events','llm_calls') AND column_name IN ('raw_usage','cost_version')""").fetchall()
        assert columns == [('YES', None)] * 4
        conn.rollback()
