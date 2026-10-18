# AI-Generated | Claude (Anthropic) | BLAM8 | 2026-07-05
"""One-shot: apply the Practice OS schema — the additive recall + no-show columns on
bookings (the FSM service_* twin) plus the encrypted practice_medical_notes table.
Safe to re-run (ADD COLUMN IF NOT EXISTS / CREATE TABLE IF NOT EXISTS)."""
import re

import psycopg2

ENV_PATH = ".env"

MIGRATION_SQL = """
-- Practice OS §4.1: recall cadence (the FSM service_due_date twin) + no-show risk.
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS recall_due_date          date;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS recall_interval_months   integer;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS recall_reminder_queued_at timestamptz;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS recall_of_booking_id     uuid REFERENCES bookings(id) ON DELETE SET NULL;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS practitioner             text;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS no_show_score            integer;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS no_show_band             text;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS no_show_reason           text;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS no_show_scored_at        timestamptz;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS extra_reminder_queued_at timestamptz;
ALTER TABLE bookings ADD COLUMN IF NOT EXISTS reminder_stages_sent     jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_bookings_recall_due
  ON bookings (tenant_id, recall_due_date)
  WHERE recall_due_date IS NOT NULL AND recall_reminder_queued_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_bookings_noshow_scan
  ON bookings (tenant_id, booking_date, status);

-- Practice OS §4.3: encrypted medical notes (UK GDPR Art. 9 special-category data).
CREATE TABLE IF NOT EXISTS practice_medical_notes (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  contact_id      uuid NOT NULL REFERENCES crm_contacts(id) ON DELETE CASCADE,
  note_ciphertext bytea NOT NULL,
  note_kind       text NOT NULL DEFAULT 'note',   -- note | allergy | medication | condition
  source          text,                    -- manual | extracted
  source_email_id text,                    -- email_log id when auto-extracted
  created_by      text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_practice_med_notes_contact
  ON practice_medical_notes (tenant_id, contact_id);

ALTER TABLE practice_medical_notes ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "tenant isolation - practice_medical_notes" ON practice_medical_notes;
CREATE POLICY "tenant isolation - practice_medical_notes" ON practice_medical_notes
  FOR ALL USING (tenant_id IN (SELECT auth_tenant_ids()))
  WITH CHECK (tenant_id IN (SELECT auth_tenant_ids()));
"""


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


password = env_value("DB_PASSWORD")
ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)

print(f"Practice OS migration: {len(MIGRATION_SQL)} chars")

conn = psycopg2.connect(
    host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
    user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
)
conn.autocommit = True
cur = conn.cursor()
cur.execute(MIGRATION_SQL)
print("DDL applied")

# Verify — the additive bookings columns
for column in ("recall_due_date", "recall_interval_months", "recall_reminder_queued_at",
               "recall_of_booking_id", "practitioner", "no_show_score", "no_show_band",
               "no_show_reason", "no_show_scored_at", "extra_reminder_queued_at",
               "reminder_stages_sent"):
    cur.execute("""
      SELECT column_name FROM information_schema.columns
      WHERE table_name = 'bookings' AND column_name = %s;
    """, (column,))
    assert cur.fetchone(), f"{column} column missing on bookings"

# Verify — the encrypted-notes table
cur.execute(
    "SELECT table_name FROM information_schema.tables WHERE table_name = %s;",
    ("practice_medical_notes",))
assert cur.fetchone(), "practice_medical_notes table missing"

print("Practice OS schema verified OK")
cur.close()
conn.close()
