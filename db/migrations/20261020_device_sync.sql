-- Install before enabling DEVICE_SYNC_ENABLED. Browser roles cannot read sessions.
begin;
create table if not exists public.device_sync (
  tenant_id uuid not null references public.tenants(id) on delete cascade,
  user_id uuid not null,
  channel text not null check(channel in ('flow','assistant')),
  revision bigint not null default 0,
  payload jsonb not null default '{}',
  primary key(tenant_id,user_id,channel),
  check(octet_length(payload::text)<=1000000)
);
create table if not exists public.device_sync_operations (
  tenant_id uuid not null references public.tenants(id) on delete cascade,
  user_id uuid not null,
  id uuid not null,
  channel text not null,
  fingerprint text not null,
  response jsonb,
  started_at timestamptz not null default now(),
  primary key(tenant_id,user_id,id)
);
alter table public.device_sync enable row level security;
alter table public.device_sync_operations enable row level security;
revoke all on public.device_sync,public.device_sync_operations from public,anon,authenticated;
grant select,insert,update,delete on public.device_sync,public.device_sync_operations to service_role;

create or replace function public.claim_device_sync(p_tenant uuid,p_user uuid,p_channel text,
  p_id uuid,p_fingerprint text,p_revision bigint) returns jsonb
language plpgsql security invoker set search_path=pg_catalog,public,pg_temp as $$
declare s public.device_sync; op public.device_sync_operations;
begin
  insert into public.device_sync(tenant_id,user_id,channel) values(p_tenant,p_user,p_channel)
    on conflict do nothing;
  select * into s from public.device_sync where tenant_id=p_tenant and user_id=p_user
    and channel=p_channel for update;
  select * into op from public.device_sync_operations where tenant_id=p_tenant and user_id=p_user and id=p_id;
  if found then
    if op.fingerprint<>p_fingerprint or op.channel<>p_channel then raise exception 'SYNC_REUSED'; end if;
    if op.response is null then raise exception 'SYNC_RUNNING'; end if;
    return jsonb_build_object('replay',op.response);
  end if;
  if s.revision<>p_revision then raise exception 'SYNC_STALE'; end if;
  if exists(select 1 from public.device_sync_operations where tenant_id=p_tenant and user_id=p_user
    and channel=p_channel and response is null) then raise exception 'SYNC_RUNNING'; end if;
  insert into public.device_sync_operations(tenant_id,user_id,id,channel,fingerprint)
    values(p_tenant,p_user,p_id,p_channel,p_fingerprint);
  return jsonb_build_object('payload',s.payload,'revision',s.revision);
end $$;

create or replace function public.finish_device_sync(p_tenant uuid,p_user uuid,p_id uuid,
  p_patch jsonb,p_response jsonb) returns jsonb
language plpgsql security invoker set search_path=pg_catalog,public,pg_temp as $$
declare op public.device_sync_operations; s public.device_sync;
begin
  select * into op from public.device_sync_operations where tenant_id=p_tenant and user_id=p_user and id=p_id;
  if not found then raise exception 'SYNC_MISSING'; end if;
  select * into s from public.device_sync where tenant_id=p_tenant and user_id=p_user and channel=op.channel for update;
  select * into op from public.device_sync_operations where tenant_id=p_tenant and user_id=p_user and id=p_id for update;
  if op.response is not null then return jsonb_build_object('payload',s.payload,'revision',s.revision); end if;
  update public.device_sync set payload=payload||p_patch,revision=revision+1
    where tenant_id=p_tenant and user_id=p_user and channel=op.channel returning * into s;
  update public.device_sync_operations set response=p_response where tenant_id=p_tenant and user_id=p_user and id=p_id;
  return jsonb_build_object('payload',s.payload,'revision',s.revision);
end $$;
revoke all on function public.claim_device_sync(uuid,uuid,text,uuid,text,bigint),
  public.finish_device_sync(uuid,uuid,uuid,jsonb,jsonb) from public,anon,authenticated;
grant execute on function public.claim_device_sync(uuid,uuid,text,uuid,text,bigint),
  public.finish_device_sync(uuid,uuid,uuid,jsonb,jsonb) to service_role;
commit;
