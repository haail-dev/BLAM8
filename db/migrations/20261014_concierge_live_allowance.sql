-- Per-user London-day allowance for Concierge transcription connections.
begin;

create table if not exists concierge_live_tier_limits (
  tier text primary key check (tier in ('starter','pro','core','enterprise')),
  minutes integer not null check (minutes between 0 and 1440),
  updated_at timestamptz not null default now(),
  updated_by uuid
);
insert into concierge_live_tier_limits (tier, minutes) values
  ('starter',10),('pro',10),('core',10),('enterprise',10)
on conflict (tier) do nothing;
alter table concierge_live_tier_limits enable row level security;

create table if not exists concierge_live_rollout (
  id boolean primary key default true check (id),
  enabled boolean not null default false,
  updated_at timestamptz not null default now(),
  updated_by uuid
);
insert into concierge_live_rollout(id,enabled) values(true,true) on conflict(id) do nothing;
alter table concierge_live_rollout enable row level security;

create table if not exists concierge_live_sessions (
  id uuid primary key,
  tenant_id uuid not null references tenants(id) on delete cascade,
  user_id uuid not null,
  london_day date not null,
  started_at timestamptz not null default now(),
  last_heartbeat timestamptz not null default now(),
  connected_at timestamptz,
  reserved_until timestamptz not null,
  closed_at timestamptz,
  charged_seconds integer,
  created_at timestamptz not null default now(),
  constraint concierge_live_closed_charge check
    ((closed_at is null and charged_seconds is null) or
     (closed_at is not null and charged_seconds >= 0))
);
create index if not exists concierge_live_sessions_user_day
  on concierge_live_sessions (tenant_id,user_id,london_day);
alter table concierge_live_sessions enable row level security;

create or replace function concierge_live_used_seconds(p_tenant_id uuid,p_user_id uuid,p_day date)
returns integer language sql stable security definer set search_path=public as $$
  select coalesce(sum(case when closed_at is not null then charged_seconds
    else case when connected_at is null then 0 else greatest(0,extract(epoch from
      (least(reserved_until,now())-connected_at))::integer) end end),0)::integer
  from concierge_live_sessions where tenant_id=p_tenant_id and user_id=p_user_id and london_day=p_day;
$$;

create or replace function concierge_live_reserve(p_id uuid,p_tenant_id uuid,p_user_id uuid,
  p_day date,p_limit_seconds integer,p_lease_seconds integer default 90)
returns jsonb language plpgsql volatile security definer set search_path=public as $$
declare used integer; row_limit integer := greatest(15,least(120,p_lease_seconds));
  lease_end timestamptz;
begin
  perform pg_advisory_xact_lock(hashtextextended(p_tenant_id::text||':'||p_user_id::text||':'||p_day::text,0));
  if exists(select 1 from concierge_live_sessions where id=p_id and tenant_id=p_tenant_id
      and user_id=p_user_id and london_day=p_day and closed_at is null) then
    used := concierge_live_used_seconds(p_tenant_id,p_user_id,p_day);
    return jsonb_build_object('ok',true,'used_seconds',used,'limit_seconds',p_limit_seconds,
      'lease_seconds',row_limit);
  end if;
  if exists(select 1 from concierge_live_sessions where id=p_id) then
    used := concierge_live_used_seconds(p_tenant_id,p_user_id,p_day);
    return jsonb_build_object('ok',false,'replayed',true,'used_seconds',used,
      'limit_seconds',p_limit_seconds);
  end if;
  update concierge_live_sessions set closed_at=reserved_until,
    charged_seconds=case when connected_at is null then 0 else
      greatest(0,extract(epoch from (reserved_until-connected_at))::integer) end
    where tenant_id=p_tenant_id and user_id=p_user_id and london_day=p_day
      and closed_at is null and reserved_until < now();
  used := concierge_live_used_seconds(p_tenant_id,p_user_id,p_day);
  if p_limit_seconds > 0 and used >= p_limit_seconds then
    return jsonb_build_object('ok',false,'used_seconds',used,'limit_seconds',p_limit_seconds);
  end if;
  lease_end := least(now()+make_interval(secs=>row_limit),((p_day+1)::timestamp at time zone 'Europe/London'));
  insert into concierge_live_sessions(id,tenant_id,user_id,london_day,reserved_until)
    values(p_id,p_tenant_id,p_user_id,p_day,lease_end);
  return jsonb_build_object('ok',true,'used_seconds',used,'limit_seconds',p_limit_seconds,
    'lease_seconds',row_limit);
end $$;

create or replace function concierge_live_heartbeat(p_id uuid,p_tenant_id uuid,p_user_id uuid,
  p_limit_seconds integer,p_lease_seconds integer default 90)
returns jsonb language plpgsql volatile security definer set search_path=public as $$
declare r concierge_live_sessions%rowtype; row_limit integer := greatest(15,least(120,p_lease_seconds));
  used_other integer; remaining integer; elapsed integer; settled_at timestamptz; day_end timestamptz;
begin
  perform pg_advisory_xact_lock(hashtextextended(p_tenant_id::text||':'||p_user_id::text||':'||
    coalesce((select london_day::text from concierge_live_sessions where id=p_id
      and tenant_id=p_tenant_id and user_id=p_user_id),''),0));
  select * into r from concierge_live_sessions where id=p_id and tenant_id=p_tenant_id
    and user_id=p_user_id and closed_at is null for update;
  if not found then return jsonb_build_object('ok',false); end if;
  if r.reserved_until < now() then
    update concierge_live_sessions set closed_at=reserved_until,
      charged_seconds=case when connected_at is null then 0 else
        greatest(0,extract(epoch from (reserved_until-connected_at))::integer) end where id=p_id;
    return jsonb_build_object('ok',false,'ended',true,'reason','lease_expired');
  end if;
  day_end := ((r.london_day+1)::timestamp at time zone 'Europe/London');
  if (now() at time zone 'Europe/London')::date > r.london_day then
    settled_at := least(day_end,r.reserved_until,now());
    update concierge_live_sessions set closed_at=settled_at,
      charged_seconds=case when connected_at is null then 0 else
        greatest(0,extract(epoch from (settled_at-connected_at))::integer) end where id=p_id;
    return jsonb_build_object('ok',false,'used_seconds',concierge_live_used_seconds(r.tenant_id,r.user_id,r.london_day),
      'ended',true,'reason','day_rollover');
  end if;
  select coalesce(sum(case when closed_at is not null then charged_seconds else
    case when connected_at is null then 0 else greatest(0,extract(epoch from
      (least(reserved_until,now())-connected_at))::integer) end end),0)::integer into used_other
    from concierge_live_sessions where tenant_id=p_tenant_id and user_id=p_user_id
      and london_day=r.london_day and id<>p_id;
  if p_limit_seconds > 0 then
    remaining := greatest(0,p_limit_seconds-used_other);
    elapsed := case when r.connected_at is null then 0 else
      greatest(0,extract(epoch from(now()-r.connected_at))::integer) end;
    if elapsed >= remaining then
      settled_at := coalesce(r.connected_at,now())+make_interval(secs=>remaining);
      update concierge_live_sessions set closed_at=settled_at,charged_seconds=remaining where id=p_id;
      return jsonb_build_object('ok',false,'used_seconds',p_limit_seconds,'ended',true,'reason','limit_reached');
    end if;
  end if;
  update concierge_live_sessions set last_heartbeat=now(),connected_at=coalesce(connected_at,now()),
    reserved_until=least(now()+make_interval(secs=>row_limit),day_end)
   where id=p_id and tenant_id=p_tenant_id and user_id=p_user_id and closed_at is null
   returning * into r;
  if not found then return jsonb_build_object('ok',false); end if;
  return jsonb_build_object('ok',true,'used_seconds',concierge_live_used_seconds(r.tenant_id,r.user_id,r.london_day));
end $$;

create or replace function concierge_live_close(p_id uuid,p_tenant_id uuid,p_user_id uuid)
returns jsonb language plpgsql volatile security definer set search_path=public as $$
declare r concierge_live_sessions%rowtype; stamp timestamptz := now();
begin
  select * into r from concierge_live_sessions where id=p_id and tenant_id=p_tenant_id and user_id=p_user_id for update;
  if not found then return jsonb_build_object('ok',false); end if;
  if r.closed_at is null then
  stamp := least(stamp,r.reserved_until);
  update concierge_live_sessions set closed_at=stamp,
      charged_seconds=case when r.connected_at is null then 0 else
        greatest(0,extract(epoch from (stamp-r.connected_at))::integer) end
      where id=p_id returning * into r;
  end if;
  return jsonb_build_object('ok',true,'charged_seconds',r.charged_seconds,
    'used_seconds',concierge_live_used_seconds(r.tenant_id,r.user_id,r.london_day));
end $$;

create or replace function concierge_live_status(p_tenant_id uuid,p_user_id uuid,p_day date,p_limit_seconds integer)
returns jsonb language sql stable security definer set search_path=public as $$
  select jsonb_build_object('used_seconds',concierge_live_used_seconds(p_tenant_id,p_user_id,p_day),
    'limit_seconds',p_limit_seconds,'remaining_seconds',case when p_limit_seconds=0 then null else
      greatest(0,p_limit_seconds-concierge_live_used_seconds(p_tenant_id,p_user_id,p_day)) end);
$$;

revoke all on function concierge_live_used_seconds(uuid,uuid,date) from public, anon, authenticated;
revoke all on function concierge_live_reserve(uuid,uuid,uuid,date,integer,integer) from public, anon, authenticated;
revoke all on function concierge_live_heartbeat(uuid,uuid,uuid,integer,integer) from public, anon, authenticated;
revoke all on function concierge_live_close(uuid,uuid,uuid) from public, anon, authenticated;
revoke all on function concierge_live_status(uuid,uuid,date,integer) from public, anon, authenticated;
grant execute on function concierge_live_used_seconds(uuid,uuid,date) to service_role;
grant execute on function concierge_live_reserve(uuid,uuid,uuid,date,integer,integer) to service_role;
grant execute on function concierge_live_heartbeat(uuid,uuid,uuid,integer,integer) to service_role;
grant execute on function concierge_live_close(uuid,uuid,uuid) to service_role;
grant execute on function concierge_live_status(uuid,uuid,date,integer) to service_role;
commit;
