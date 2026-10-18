-- S1f: additive receipts only. No historical backfill or tenant-policy change.
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS raw_usage jsonb;
ALTER TABLE usage_events ADD COLUMN IF NOT EXISTS cost_version text;
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS raw_usage jsonb;
ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS cost_version text;
