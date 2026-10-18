-- Historical import is separate from connecting and syncing new mail.
alter table public.email_connections add column if not exists backlog_import_choice jsonb;

create or replace function public.choose_mailbox_import(
  p_tenant uuid, p_connection uuid, p_actor uuid, p_decision text, p_expected_run text)
returns boolean language plpgsql set search_path = public as $$
declare box email_connections%rowtype;
begin
  if p_actor is null or p_decision not in ('import','skip','later') then
    raise exception 'Invalid import choice';
  end if;
  select * into box from email_connections where tenant_id=p_tenant and id=p_connection for update;
  if not found or coalesce(box.backlog_import->>'run_id','') is distinct from p_expected_run then
    return false;
  end if;
  if box.backlog_import->>'state'='running'
     and (box.backlog_import->>'expires_at')::timestamptz > now() then return false; end if;
  -- Completed runs cannot be restarted by a replayed initial-start request.
  if box.backlog_import->>'state'='complete' then return false; end if;
  -- Replays of the same choice cannot queue another worker after its lease expires.
  if box.backlog_import_choice->>'expected_run' = p_expected_run
     and box.backlog_import_choice->>'decision'='import'
     and coalesce((box.backlog_import_choice->>'claimed')::boolean,false) then return false; end if;
  update email_connections set backlog_import_choice=jsonb_build_object(
    'decision',p_decision,'actor',p_actor,'at',now(),'version',1,
    'expected_run',p_expected_run,'window_end',box.created_at,'days',14,'limit',100)
    where tenant_id=p_tenant and id=p_connection;
  return true;
end $$;

create or replace function public.claim_mailbox_import(p_tenant uuid, p_connection uuid)
returns jsonb language plpgsql set search_path = public as $$
declare claimed jsonb;
begin
  update email_connections
  set backlog_import=jsonb_build_object(
    'state','running','run_id',gen_random_uuid(),'started_at',now(),
    'expires_at',now()+interval '15 minutes','days',14,'limit',100,
    'window_end',backlog_import_choice->>'window_end','approval',backlog_import_choice),
    backlog_import_choice=backlog_import_choice || '{"claimed":true}'::jsonb
  where tenant_id=p_tenant and id=p_connection
    and backlog_import_choice->>'decision'='import'
    and coalesce((backlog_import_choice->>'claimed')::boolean,false)=false
    and backlog_import_choice->>'expected_run'=coalesce(backlog_import->>'run_id','')
    and (coalesce(backlog_import->>'state','') <> 'running'
         or (backlog_import->>'expires_at')::timestamptz <= now())
  returning backlog_import into claimed;
  return claimed;
end $$;
revoke all on function public.choose_mailbox_import(uuid,uuid,uuid,text,text) from public,anon,authenticated;
grant execute on function public.choose_mailbox_import(uuid,uuid,uuid,text,text) to service_role;
revoke all on function public.claim_mailbox_import(uuid,uuid) from public,anon,authenticated;
grant execute on function public.claim_mailbox_import(uuid,uuid) to service_role;
