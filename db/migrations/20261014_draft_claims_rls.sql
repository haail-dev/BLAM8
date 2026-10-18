-- Supabase Security Advisor, 14 Oct 2026: public.draft_claims had RLS disabled while exposed to
-- PostgREST, so the public anon key could read and write it. Same tenant-isolation policy as every
-- other table; the app writes it with the service role, which bypasses RLS, so drafting is unaffected.
alter table draft_claims enable row level security;
drop policy if exists "tenant isolation - draft_claims" on draft_claims;
create policy "tenant isolation - draft_claims" on draft_claims
  for all using (tenant_id in (select auth_tenant_ids()))
  with check (tenant_id in (select auth_tenant_ids()));
