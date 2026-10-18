-- Rollback re-opens the hole the advisor flagged; only use it to diagnose a drafting fault.
drop policy if exists "tenant isolation - draft_claims" on draft_claims;
alter table draft_claims disable row level security;
