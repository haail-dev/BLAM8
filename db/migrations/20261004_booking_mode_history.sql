-- Explicit operator-run booking-history conversion. Installation changes no bookings.
-- Apply after no_defaults and setup_cards. Functions and archive are DBA-only.
begin;
create table if not exists booking_mode_migrations (
  id uuid primary key default gen_random_uuid(), tenant_id uuid not null,
  before_state jsonb not null, after_state jsonb not null,
  mapping jsonb not null, created_at timestamptz not null default now(),
  rolled_back_at timestamptz
);
alter table booking_mode_migrations enable row level security;
revoke all on booking_mode_migrations from public;

create or replace function booking_mode_snapshot(p_tenant uuid)
returns jsonb language sql stable security invoker set search_path=public as $$
  select jsonb_build_object('config',(select to_jsonb(c) from scheduling_config c where tenant_id=p_tenant),
    'bookings',coalesce((select jsonb_agg(to_jsonb(b) order by id) from bookings b where tenant_id=p_tenant),'[]'::jsonb));
$$;
revoke all on function booking_mode_snapshot(uuid) from public;

-- Derive a final-state validator from the installed capacity guard. This keeps
-- the exact installed readiness, buffers, quantities and daily-limit rules.
-- Only the UPDATE shortcuts and legacy unknown-pool allowance are removed:
-- every reservation in a migration must pass the complete final-state check.
do $build$
declare body text; declarations text; core text; skip text; old_pool text;
begin
  select prosrc into body from pg_proc where oid='enforce_booking_capacity()'::regprocedure;
  if body is null or strpos(body,'  -- Non-reservations')=0
     or strpos(body,'  if tg_op = ''UPDATE'' and')=0
     or strpos(body,'  perform pg_advisory_xact_lock')=0 then
    raise exception 'BLAM8_FUNCTION_DRIFT: capacity validator anchors';
  end if;
  declarations := split_part(body,E'\nbegin',1);
  skip := split_part(split_part(body,'  -- Non-reservations',2),'  if tg_op = ''UPDATE'' and',1);
  core := '  perform pg_advisory_xact_lock' || split_part(body,'  perform pg_advisory_xact_lock',2);
  old_pool := $old$      if tg_op <> 'UPDATE' or new.resource_type is distinct from old.resource_type then
        raise exception 'BLAM8_CAPACITY_CONFLICT: unknown resource';
      end if;$old$;
  if strpos(core,old_pool)=0 then raise exception 'BLAM8_FUNCTION_DRIFT: capacity pool guard'; end if;
  core := replace(core,old_pool,'      raise exception ''BLAM8_CAPACITY_CONFLICT: unknown resource'';');
  body := declarations || E'\nbegin\n  -- Non-reservations' || skip || core;
  body := replace(replace(body,'new.','candidate.'),'return new;','return;');
  body := replace(body,'booking_span_bounds(new,','booking_span_bounds(candidate,');
  if regexp_replace(body,'--[^\n]*','','g') ~ '\m(tg_op|old|new)\M' then
    raise exception 'BLAM8_FUNCTION_DRIFT: unexpected trigger dependency';
  end if;
  execute 'create or replace function validate_booking_mode_capacity(candidate bookings) returns void '
    || 'language plpgsql volatile security invoker set search_path=public as ' || quote_literal(body);
end $build$;
revoke all on function validate_booking_mode_capacity(bookings) from public;

create or replace function migrate_booking_mode(p_tenant uuid,p_expected jsonb,p_patch jsonb,p_mapping jsonb)
returns uuid language plpgsql security invoker set search_path=public as $$
declare before_state jsonb; after_state jsonb; cfg scheduling_config; target scheduling_config;
  b bookings; item jsonb; span int8range; start integer; length integer;
  identity uuid; assignments text; resource text; is_external boolean;
begin
  if jsonb_typeof(p_expected) is distinct from 'object' or jsonb_typeof(p_patch) is distinct from 'object'
     or jsonb_typeof(p_mapping) is distinct from 'array' then raise exception 'BLAM8_MODE_INVALID'; end if;
  if exists(select 1 from jsonb_object_keys(p_patch) k where k not in
      ('booking_mode','intake_fields','resource_types','service_types','capacity_per_window'))
     or not(p_patch ?& array['booking_mode','intake_fields','resource_types','service_types','capacity_per_window'])
     or p_patch->>'booking_mode' not in ('stay','appointment') or p_patch->>'booking_mode' is null
     or jsonb_typeof(p_patch->'intake_fields') is distinct from 'array'
     or jsonb_typeof(p_patch->'resource_types') is distinct from 'array'
     or jsonb_typeof(p_patch->'service_types') is distinct from 'array'
     or jsonb_array_length(p_patch->'intake_fields')>20 then raise exception 'BLAM8_MODE_INVALID'; end if;
  perform pg_advisory_xact_lock(hashtextextended('blam8:office:' || p_tenant::text,0));
  select * into cfg from scheduling_config where tenant_id=p_tenant for update;
  if not found then raise exception 'BLAM8_MODE_INVALID: missing settings'; end if;
  lock table bookings in share row exclusive mode;
  before_state := booking_mode_snapshot(p_tenant);
  if before_state is distinct from p_expected then raise exception 'BLAM8_SETTINGS_STALE'; end if;
  if cfg.booking_mode=p_patch->>'booking_mode' then raise exception 'BLAM8_MODE_INVALID: mode is unchanged'; end if;
  select * into target from jsonb_populate_record(cfg,p_patch);
  if target.capacity_per_window is not null and target.capacity_per_window<1 then raise exception 'BLAM8_MODE_INVALID: capacity'; end if;
  if exists(select 1 from jsonb_array_elements(target.resource_types) r where nullif(trim(r->>'name'),'') is null
       or (r->>'count')::int is null or (r->>'count')::int<1)
     or (select count(*)<>count(distinct lower(r->>'name')) from jsonb_array_elements(target.resource_types) r)
     then raise exception 'BLAM8_MODE_INVALID: pools'; end if;
  if exists(select 1 from jsonb_array_elements(target.service_types) s where nullif(trim(s->>'name'),'') is null)
     or (select count(*)<>count(distinct lower(s->>'name')) from jsonb_array_elements(target.service_types) s)
     then raise exception 'BLAM8_MODE_INVALID: services'; end if;
  if target.booking_mode='stay' and exists(select 1 from jsonb_array_elements(target.service_types) s
      where coalesce(s->>'timing','time')<>'time' or s->>'resource' is distinct from s->>'name'
        or not exists(select 1 from jsonb_array_elements(target.resource_types) r where r->>'name'=s->>'name'))
     then raise exception 'BLAM8_MODE_INVALID: stay appointment pools'; end if;
  if (select count(*) from jsonb_array_elements(target.intake_fields) f where
      trim(regexp_replace(lower(f->>'label'),'[^a-z0-9]+',' ','g'))='departure collection date time')
      <> (case when target.booking_mode='stay' then 1 else 0 end)
     then raise exception 'BLAM8_MODE_INVALID: departure capture'; end if;
  if target.booking_mode='stay' and exists(select 1 from jsonb_array_elements(target.intake_fields) f where
      trim(regexp_replace(lower(f->>'label'),'[^a-z0-9]+',' ','g'))='departure collection date time'
      and f->'required' is distinct from 'true'::jsonb)
     then raise exception 'BLAM8_MODE_INVALID: departure must be required'; end if;
  if target.booking_mode='appointment' and exists(select 1 from jsonb_array_elements(target.intake_fields) f,
      lateral jsonb_array_elements_text(coalesce(f->'services','[]'::jsonb)) scope where lower(trim(scope))='stays')
     then raise exception 'BLAM8_MODE_INVALID: remove Stays-only field scopes'; end if;
  if jsonb_array_length(p_mapping)<>jsonb_array_length(before_state->'bookings')
     or (select count(*)<>count(distinct m->>'id') from jsonb_array_elements(p_mapping) m)
     or exists(select 1 from jsonb_array_elements(p_mapping) m where
       jsonb_typeof(m) is distinct from 'object' or not(m ?& array['id','resource_type'])
       or exists(select 1 from jsonb_object_keys(m) k where k not in ('id','resource_type'))
       or not exists(select 1 from bookings where tenant_id=p_tenant and id=(m->>'id')::uuid))
     then raise exception 'BLAM8_MODE_INVALID: map every booking exactly once'; end if;
  -- Record times before changing settings, including legacy AM/PM/day windows.
  -- A mapping changes capacity identity only. Dates, times, quantities, statuses,
  -- details, prices and linked records are never inferred or rewritten.
  p_mapping := (select coalesce(jsonb_agg(m || jsonb_build_object('span',
      case when mapped.spaces is not null or mapped.engineer_id is not null or mapped.source='calendar'
        or exists(select 1 from asset_units u where u.tenant_id=p_tenant and u.id::text=mapped.resource_type)
      then null else booking_span_bounds(mapped,cfg)::text end) order by mapped.id),'[]'::jsonb)
      from jsonb_array_elements(p_mapping) m join bookings mapped on mapped.id=(m->>'id')::uuid and mapped.tenant_id=p_tenant);
  update scheduling_config set booking_mode=target.booking_mode,intake_fields=target.intake_fields,
    resource_types=target.resource_types,service_types=target.service_types,capacity_per_window=target.capacity_per_window,
    booking_revision=booking_revision+1,updated_at=now() where tenant_id=p_tenant;
  for item in select * from jsonb_array_elements(p_mapping) loop
    select * into b from bookings where tenant_id=p_tenant and id=(item->>'id')::uuid;
    resource := nullif(item->>'resource_type','');
    is_external := b.spaces is not null or b.engineer_id is not null or b.source='calendar'
      or exists(select 1 from asset_units u where u.tenant_id=p_tenant and u.id::text=b.resource_type);
    if is_external then
      if resource is distinct from nullif(b.resource_type,'') then raise exception 'BLAM8_MODE_INVALID: external reservation cannot change pool'; end if;
      continue;
    end if;
    span := (item->>'span')::int8range;
    if span is null or isempty(span) or lower_inf(span) or upper_inf(span) then raise exception 'BLAM8_MODE_INVALID: unknown booking span'; end if;
    if resource is not null and not exists(select 1 from jsonb_array_elements(target.resource_types) r where r->>'name'=resource)
       then raise exception 'BLAM8_MODE_INVALID: unknown mapped pool'; end if;
    if target.booking_mode='stay' and exists(select 1 from jsonb_array_elements(target.service_types) s
       where s->>'name'=b.details->>'Service' and s->>'resource' is distinct from resource)
       then raise exception 'BLAM8_MODE_INVALID: appointment mapping'; end if;
    start := (lower(span)-(b.booking_date-date '2000-01-01')::bigint*96)::int;
    length := (upper(span)-lower(span))::int;
    update bookings set start_block=start,length_blocks=length,resource_type=resource
      where tenant_id=p_tenant and id=b.id;
  end loop;
  for b in select * from bookings where tenant_id=p_tenant loop
    perform validate_booking_mode_capacity(b);
  end loop;
  after_state := booking_mode_snapshot(p_tenant);
  insert into booking_mode_migrations(tenant_id,before_state,after_state,mapping)
    values(p_tenant,before_state,after_state,p_mapping) returning id into identity;
  -- Prove this exact mapping can reverse with ordinary triggers still enabled.
  -- Discard the rehearsal with a private subtransaction; a real rollback error
  -- escapes and aborts the entire conversion. No irreversible plan is accepted.
  begin
    perform rollback_booking_mode(identity);
    raise sqlstate 'BLM01' using message='discard successful rollback rehearsal';
  exception when sqlstate 'BLM01' then
    null;
  end;
  return identity;
end $$;
revoke all on function migrate_booking_mode(uuid,jsonb,jsonb,jsonb) from public;

create or replace function rollback_booking_mode(p_id uuid)
returns void language plpgsql security invoker set search_path=public as $$
declare receipt booking_mode_migrations; cfg scheduling_config; b bookings; previous bookings;
begin
  select * into receipt from booking_mode_migrations where id=p_id;
  if not found or receipt.rolled_back_at is not null then raise exception 'BLAM8_MODE_INVALID: receipt unavailable'; end if;
  perform pg_advisory_xact_lock(hashtextextended('blam8:office:' || receipt.tenant_id::text,0));
  perform 1 from scheduling_config where tenant_id=receipt.tenant_id for update;
  lock table bookings in share row exclusive mode;
  if booking_mode_snapshot(receipt.tenant_id) is distinct from receipt.after_state then
    raise exception 'BLAM8_SETTINGS_STALE: changes since migration require a new reviewed plan'; end if;
  select * into cfg from jsonb_populate_record(null::scheduling_config,receipt.before_state->'config');
  update scheduling_config set booking_mode=cfg.booking_mode,intake_fields=cfg.intake_fields,
    resource_types=cfg.resource_types,service_types=cfg.service_types,capacity_per_window=cfg.capacity_per_window,
    booking_revision=cfg.booking_revision,updated_at=cfg.updated_at where tenant_id=receipt.tenant_id;
  for previous in select * from jsonb_populate_recordset(null::bookings,receipt.before_state->'bookings') loop
    update bookings set start_block=previous.start_block,length_blocks=previous.length_blocks,
      resource_type=previous.resource_type,updated_at=previous.updated_at
      where tenant_id=receipt.tenant_id and id=previous.id;
  end loop;
  if booking_mode_snapshot(receipt.tenant_id) is distinct from receipt.before_state then
    raise exception 'BLAM8_MODE_INVALID: rollback did not restore snapshot'; end if;
  update booking_mode_migrations set rolled_back_at=now() where id=p_id;
end $$;
revoke all on function rollback_booking_mode(uuid) from public;

do $$ declare r text; begin
  foreach r in array array['anon','authenticated','service_role'] loop
    if exists(select 1 from pg_roles where rolname=r) then
      execute format('revoke all on booking_mode_migrations from %I',r);
      execute format('revoke all on function booking_mode_snapshot(uuid), validate_booking_mode_capacity(bookings), migrate_booking_mode(uuid,jsonb,jsonb,jsonb), rollback_booking_mode(uuid) from %I',r);
    end if;
  end loop;
end $$;
commit;
