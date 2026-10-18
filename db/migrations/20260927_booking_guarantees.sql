-- Apply before deploying the matching application changes. No provider calls.
-- All office booking writers share one tenant lock, including multi-day spans,
-- moves, direct/manual inserts, and expired offers becoming proposed bookings.
-- Existing over-capacity rows are not modified by this migration.
begin;

-- Persist the existing stay toggle once; capacity checks never inspect labels.
alter table scheduling_config add column if not exists booking_mode text;
update scheduling_config c set booking_mode = case when exists (
  select 1 from jsonb_array_elements(coalesce(c.intake_fields, '[]')) f
  where trim(regexp_replace(lower(f->>'label'), '[^a-z0-9]+', ' ', 'g')) =
        'departure collection date time') then 'stay' else 'appointment' end
where booking_mode is null;
alter table scheduling_config alter column booking_mode set default 'appointment';
alter table scheduling_config alter column booking_mode set not null;

create or replace function booking_span_bounds(b bookings, c scheduling_config)
returns int8range language plpgsql stable set search_path = public as $$
declare
  base bigint := (b.booking_date - date '2000-01-01')::bigint * 96;
  lo integer;
  hi integer;
begin
  if b.start_block is not null and b.length_blocks is not null then
    if b.length_blocks <= 0 then
      raise exception 'BLAM8_CAPACITY_CONFLICT: invalid reservation length';
    end if;
    return int8range(base + b.start_block, base + b.start_block + b.length_blocks, '[)');
  end if;
  lo := floor(extract(epoch from coalesce(c.open_time, time '09:00')) / 900)::int;
  hi := ceil(extract(epoch from coalesce(c.close_time, time '17:00')) / 900)::int;
  if hi <= lo then lo := 0; hi := 96; end if;
  if b."window" = 'am' and lo < 48 then hi := least(hi, 48);
  elsif b."window" = 'pm' and hi > 48 then lo := greatest(lo, 48);
  elsif b."window" in ('am', 'pm') then null;
  elsif b."window" ~ '^\d{2}:\d{2}$' then
    lo := floor(extract(epoch from b."window"::time) / 900)::int;
    hi := lo + ceil(coalesce(c.slot_minutes, 60) / 15.0)::int;
  elsif b."window" is null or b."window" <> 'day' then
    return null;
  end if;
  return int8range(base + lo, base + greatest(lo + 1, hi), '[)');
end $$;

create or replace function enforce_booking_capacity()
returns trigger language plpgsql volatile set search_path = public as $$
declare
  c scheduling_config;
  span int8range;
  cap integer;
  quantity integer := coalesce(new.quantity, 1);
  gap integer;
  peak bigint;
  svc jsonb;
  max_day integer;
  used_day integer;
  stays boolean;
begin
  if tg_op = 'UPDATE' and new.tenant_id <> old.tenant_id then
    raise exception 'A booking cannot change business';
  end if;
  -- Non-reservations and metadata updates must never queue behind a reservation.
  if new.spaces is not null or new.engineer_id is not null or new.source = 'calendar'
     or exists (select 1 from asset_units u where u.tenant_id = new.tenant_id
                and u.id::text = new.resource_type)
     or new.status not in ('offered', 'proposed', 'confirmed')
     or (new.status = 'offered' and new.expires_at <= clock_timestamp()) then
    return new;
  end if;
  if tg_op = 'UPDATE' and
     row(new.booking_date, new."window", new.start_block, new.length_blocks,
         new.quantity, new.resource_type, new.status, new.expires_at,
         new.spaces, new.engineer_id, new.source,
         new.details->>'Service') is not distinct from
     row(old.booking_date, old."window", old.start_block, old.length_blocks,
         old.quantity, old.resource_type, old.status, old.expires_at,
         old.spaces, old.engineer_id, old.source,
         old.details->>'Service') then
    return new;
  end if;
  -- Live status transitions do not acquire capacity again. Expired offers do.
  if tg_op = 'UPDATE' and old.status in ('offered', 'proposed', 'confirmed')
     and (old.status <> 'offered' or old.expires_at is null or old.expires_at > clock_timestamp())
     and row(new.booking_date, new."window", new.start_block, new.length_blocks,
             new.quantity, new.resource_type, new.spaces, new.engineer_id, new.source,
             new.details->>'Service') is not distinct from
         row(old.booking_date, old."window", old.start_block, old.length_blocks,
             old.quantity, old.resource_type, old.spaces, old.engineer_id, old.source,
             old.details->>'Service') then
    return new;
  end if;
  perform pg_advisory_xact_lock(hashtextextended('blam8:office:' || new.tenant_id::text, 0));
  select * into c from scheduling_config where tenant_id = new.tenant_id;
  cap := coalesce(c.capacity_per_window, 4);
  if nullif(new.resource_type, '') is not null then
    select greatest(1, coalesce((r->>'count')::integer, 1)) into cap
    from jsonb_array_elements(coalesce(c.resource_types, '[]')) r
    where lower(r->>'name') = lower(new.resource_type);
    if cap is null then
      -- A new request must choose a configured pool. An existing booking may
      -- retain its old label when that pool is renamed/removed.
      if tg_op <> 'UPDATE' or new.resource_type is distinct from old.resource_type then
        raise exception 'BLAM8_CAPACITY_CONFLICT: unknown resource';
      end if;
      cap := coalesce(c.capacity_per_window, 4);
    end if;
  end if;
  if quantity < 1 or cap < 1 then
    raise exception 'BLAM8_CAPACITY_CONFLICT: invalid capacity';
  end if;
  select s into svc from jsonb_array_elements(coalesce(c.service_types, '[]')) s
  where lower(s->>'name') = lower(new.details->>'Service') limit 1;
  gap := ceil(coalesce(nullif((svc->>'buffer')::integer, 0), c.buffer_minutes, 0) / 15.0)::int;
  max_day := coalesce((svc->>'max_per_day')::integer, 0);
  if max_day > 0 then
    select count(*) into used_day from bookings b
    where b.tenant_id = new.tenant_id and b.id <> new.id
      and b.booking_date = new.booking_date
      and lower(b.details->>'Service') = lower(new.details->>'Service')
      and (b.status in ('proposed', 'confirmed') or
           (b.status = 'offered' and (b.expires_at is null or b.expires_at > clock_timestamp())));
    if used_day >= max_day then
      raise exception 'BLAM8_CAPACITY_CONFLICT: daily service limit';
    end if;
  end if;
  stays := c.booking_mode = 'stay';
  span := booking_span_bounds(new, c);
  if span is null then
    raise exception 'BLAM8_CAPACITY_CONFLICT: unknown reservation window';
  end if;
  -- Sweep interval endpoints, summing quantities rather than counting every
  -- overlapping booking as concurrent. Endpoints are grouped before summing:
  -- a departure and arrival at the same time do not overlap.
  with busy as (
    select booking_span_bounds(b, c) as r, coalesce(b.quantity, 1) as q
    from bookings b
    where b.tenant_id = new.tenant_id and b.id <> new.id
      and b.spaces is null and b.engineer_id is null
      and (b.status in ('proposed', 'confirmed') or
           (b.status = 'offered' and (b.expires_at is null or b.expires_at > clock_timestamp())))
      and (case when nullif(new.resource_type, '') is not null
                then b.resource_type = new.resource_type
                when stays then nullif(b.resource_type, '') is null
                else true end)
  ), clipped as (
    select greatest(lower(span), lower(r) - gap) as a,
           least(upper(span), upper(r) + gap) as z, q
    from busy where lower(r) - gap < upper(span) and upper(r) + gap > lower(span)
  ), events as (
    select a as t, q as delta from clipped
    union all select z, -q from clipped
  ), totals as (
    select t, sum(delta) as delta from events group by t
  ), running as (
    select sum(delta) over (order by t) as used from totals
  ) select case when new.start_block is null or new.length_blocks is null
                then (select count(*) from clipped)
                else coalesce(max(used), 0) end into peak from running;
  if peak + quantity > cap then
    raise exception 'BLAM8_CAPACITY_CONFLICT: no capacity for this reservation';
  end if;
  return new;
end $$;

drop trigger if exists bookings_capacity_guard on bookings;
create trigger bookings_capacity_guard before insert or update on bookings
for each row execute function enforce_booking_capacity();

-- Evidence identifies the exact requirement on its already-linked subject.
-- One possible proof field preserves the existing proof behaviour. Several require classification.
alter table crm_artifacts add column if not exists requirement_keys text[] not null default '{}';

create or replace function classify_single_proof()
returns trigger language plpgsql set search_path = public as $$
declare keys text[];
begin
  if cardinality(new.requirement_keys) = 0 and new.instance_id is not null then
    select array_agg(f->>'key') into keys
    from requirement_instances i
    join requirement_groups g on g.id = i.group_id and g.tenant_id = i.tenant_id
    cross join lateral jsonb_array_elements(g.fields) f
    where i.id = new.instance_id and i.tenant_id = new.tenant_id
      and (new.contact_id is null or new.contact_id = i.contact_id)
      and f->>'proof' = 'true' and nullif(f->>'key', '') is not null;
    if cardinality(keys) = 1 then new.requirement_keys := keys; end if;
  end if;
  return new;
end $$;
drop trigger if exists artifacts_single_proof on crm_artifacts;
create trigger artifacts_single_proof before insert or update of instance_id, requirement_keys
on crm_artifacts for each row execute function classify_single_proof();
update crm_artifacts set requirement_keys = requirement_keys
where cardinality(requirement_keys) = 0 and instance_id is not null;


-- Snapshot the previous shared out-of-hours assumptions into existing tenant
-- data before removing them from prompts. New policies inherit none of these
-- terms. Presence of price_basis makes this step safe to re-run.
update scheduling_config c set service_types = (
  select jsonb_agg(case
    when jsonb_typeof(s->'out_of_hours') = 'object'
         and not (s->'out_of_hours' ? 'price_basis')
    then jsonb_set(s, '{out_of_hours}',
      '{"price_basis":"first_hour","terms":"Parts are extra. The charge applies even if the work cannot be finished during the visit."}'::jsonb
      || (s->'out_of_hours')
      || jsonb_build_object('carried_over_review', case
           when s->'out_of_hours' ? 'terms' then '["price_basis"]'::jsonb
           else '["price_basis","terms"]'::jsonb end))
    else s end order by n)
  from jsonb_array_elements(c.service_types) with ordinality as items(s,n)
)
where jsonb_typeof(c.service_types) = 'array' and jsonb_array_length(c.service_types) > 0;

-- Moves reserve their final span in one write through the same guard.
create or replace function move_booking_if_capacity(
  p_tenant_id uuid, p_booking_id uuid, p_new_date date, p_new_window text)
returns setof bookings language plpgsql volatile set search_path = public as $$
declare b bookings; c scheduling_config; bounds int8range;
begin
  select * into b from bookings where id=p_booking_id and tenant_id=p_tenant_id
    and status in ('proposed','confirmed');
  if not found then return; end if;
  select * into c from scheduling_config where tenant_id=p_tenant_id;
  b.booking_date := p_new_date; b."window" := p_new_window;
  b.start_block := null; b.length_blocks := null;
  bounds := booking_span_bounds(b,c);
  if bounds is null then raise exception 'BLAM8_CAPACITY_CONFLICT: invalid window'; end if;
  return query update bookings set booking_date=p_new_date, "window"=p_new_window,
    start_block=(lower(bounds) - (p_new_date-date '2000-01-01')::bigint*96)::int,
    length_blocks=(upper(bounds)-lower(bounds))::int, updated_at=now()
    where id=p_booking_id and tenant_id=p_tenant_id and status in ('proposed','confirmed')
    returning *;
end $$;

commit;
