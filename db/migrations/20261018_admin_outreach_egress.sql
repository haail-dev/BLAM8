-- Apply before the outreach egress fix. Return identities, never repeated
-- copies of message templates/settings. POST avoids long configuration URLs.
begin;
create or replace function public.admin_outreach_stale_drafts(p_config jsonb)
returns table(id uuid)
language sql stable security invoker set search_path = public
as $$
  select m.id from public.admin_outreach_messages m
  where m.status in ('draft', 'approved') and m.config_snapshot <> p_config;
$$;
revoke all on function public.admin_outreach_stale_drafts(jsonb) from public, anon, authenticated;
grant execute on function public.admin_outreach_stale_drafts(jsonb) to service_role;
notify pgrst, 'reload schema';
commit;
