# AI-Generated | Claude (Anthropic) | ForgeGuard | 2026-09-04
"""One-shot: Documents on the CRM record (Kris 2026-09-04). The CRM's
`crm_artifacts` seam (schema: "the table exists so the seam is real; nothing
writes it yet") becomes the documents table: one row per file kept — from an
inbound email's attachments or an operator upload — on the customer's
record, optionally pinned to the thread row, a booking, a dependant, and
labelled as the checklist document it satisfies. The bytes live in the
private `documents` Storage bucket (created on first use); `url_or_path`
holds the object key. Safe to re-run."""
import re

import psycopg2

ENV_PATH = ".env"

MIGRATION_SQL = """
ALTER TABLE crm_artifacts
  ADD COLUMN IF NOT EXISTS contact_email text,
  ADD COLUMN IF NOT EXISTS email_log_id  uuid REFERENCES email_log(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS booking_id    uuid REFERENCES bookings(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS instance_id   uuid REFERENCES requirement_instances(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS size_bytes    int NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS label         text;
CREATE INDEX IF NOT EXISTS crm_artifacts_contact_email_idx
  ON crm_artifacts (tenant_id, contact_email, created_at DESC);
CREATE INDEX IF NOT EXISTS crm_artifacts_booking_idx
  ON crm_artifacts (tenant_id, booking_id);
"""


def env_value(key: str) -> str:
    for line in open(ENV_PATH, encoding="utf-8"):
        line = line.strip().lstrip("#").strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{key} not found in {ENV_PATH}")


if __name__ == "__main__":
    password = env_value("DB_PASSWORD")
    ref = re.search(r"https://([a-z0-9]+)\.supabase\.co", env_value("SUPABASE_URL")).group(1)
    print(f"documents migration: {len(MIGRATION_SQL)} chars")
    conn = psycopg2.connect(
        host="aws-1-eu-west-2.pooler.supabase.com", port=5432,
        user=f"postgres.{ref}", password=password, dbname="postgres", sslmode="require",
    )
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(MIGRATION_SQL)
    print("DDL applied")
    for col in ("contact_email", "email_log_id", "booking_id", "instance_id", "size_bytes", "label"):
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'crm_artifacts' AND column_name = %s;", (col,))
        assert cur.fetchone(), f"{col} missing on crm_artifacts"
    print("crm_artifacts documents columns verified OK")
    cur.close()
    conn.close()
