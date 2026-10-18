-- Shared Gmail/Microsoft import state. Apply before deploying the application.
alter table public.email_connections add column if not exists backlog_import jsonb;

-- Atomic across workers and entry points. Only the service role can claim work.
create or replace function public.claim_mailbox_import(p_tenant uuid, p_connection uuid)
returns jsonb language sql set search_path = public as $$
  update public.email_connections
  set backlog_import = jsonb_build_object(
    'state', 'running', 'run_id', gen_random_uuid(),
    'started_at', now(), 'expires_at', now() + interval '15 minutes')
  where tenant_id = p_tenant and id = p_connection
    and (coalesce(backlog_import->>'state', '') <> 'running'
         or (backlog_import->>'expires_at')::timestamptz <= now())
  returning backlog_import;
$$;
revoke all on function public.claim_mailbox_import(uuid, uuid) from public, anon, authenticated;
grant execute on function public.claim_mailbox_import(uuid, uuid) to service_role;
