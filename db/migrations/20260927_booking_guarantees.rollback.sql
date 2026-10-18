-- ROLLBACK for 20260927_booking_guarantees.sql (Astra-assisted, see docs/astra/deploys.md).
-- Run only AFTER the app has been rolled back to the pre-Astra release: the old
-- app does not translate BLAM8_CAPACITY_CONFLICT, so the guard must go with it.
-- Before the forward migration runs on production, save the live definition:
--   select pg_get_functiondef('move_booking_if_capacity'::regproc);
-- and check it matches the copy restored below (taken from main's db/schema.sql).
--
-- Kept on purpose (additive, harmless to the old app, holds no data it needs):
--   scheduling_config.booking_mode, crm_artifacts.requirement_keys.
-- Out-of-hours: removes only the terms the migration copied in AND the owner
-- has not yet reviewed (still marked carried_over_review). Reviewed terms stay:
-- the owner confirmed them.
begin;

drop trigger if exists bookings_capacity_guard on bookings;
drop function if exists enforce_booking_capacity();
drop function if exists booking_span_bounds(bookings, scheduling_config);

drop trigger if exists artifacts_single_proof on crm_artifacts;
drop function if exists classify_single_proof();

update scheduling_config c set service_types = (
  select jsonb_agg(case
    when jsonb_typeof(s->'out_of_hours') = 'object'
         and jsonb_typeof(s->'out_of_hours'->'carried_over_review') = 'array'
    then jsonb_set(s, '{out_of_hours}',
      ((s->'out_of_hours') - array(select jsonb_array_elements_text(
                                      s->'out_of_hours'->'carried_over_review')))
      - 'carried_over_review')
    else s end order by n)
  from jsonb_array_elements(c.service_types) with ordinality as items(s, n)
)
where jsonb_typeof(c.service_types) = 'array' and jsonb_array_length(c.service_types) > 0;

-- The pre-Astra move function, verbatim from main's db/schema.sql.
create or replace function move_booking_if_capacity (
  p_tenant_id  uuid,
  p_booking_id uuid,
  p_new_date   date,
  p_new_window text
)
returns setof bookings
language plpgsql volatile
set search_path = public
as $$
declare
  v_capacity int;
  v_used     int;
begin
  if p_new_window not in ('day', 'am', 'pm') and p_new_window !~ '^\d{2}:\d{2}$' then
    raise exception 'invalid window: %', p_new_window;
  end if;

  -- Only a live booking the tenant owns can move.
  if not exists (
    select 1 from bookings
    where id = p_booking_id and tenant_id = p_tenant_id
      and status in ('proposed', 'confirmed')
  ) then
    return;
  end if;

  perform pg_advisory_xact_lock(
    hashtextextended(p_tenant_id::text || ':' || p_new_date::text, 0)
  );

  select capacity_per_window into v_capacity
  from scheduling_config where tenant_id = p_tenant_id;
  v_capacity := coalesce(v_capacity, 4);

  select count(*) into v_used
  from bookings b
  where b.tenant_id = p_tenant_id
    and b.booking_date = p_new_date
    and (b.status in ('proposed', 'confirmed')
         or (b.status = 'offered' and (b.expires_at is null or b.expires_at > now())))
    and b.spaces is null
    and b.id <> p_booking_id
    and (
      p_new_window = 'day' or b."window" = 'day' or b."window" = p_new_window
      or (b."window" = 'am' and p_new_window ~ '^\d{2}:\d{2}$' and p_new_window <  '12:00')
      or (b."window" = 'pm' and p_new_window ~ '^\d{2}:\d{2}$' and p_new_window >= '12:00')
      or (p_new_window = 'am' and b."window" ~ '^\d{2}:\d{2}$' and b."window" <  '12:00')
      or (p_new_window = 'pm' and b."window" ~ '^\d{2}:\d{2}$' and b."window" >= '12:00')
    );

  if v_used >= v_capacity then
    return;
  end if;

  return query
    update bookings
       set booking_date = p_new_date, "window" = p_new_window, updated_at = now()
     where id = p_booking_id and tenant_id = p_tenant_id
       and status in ('proposed', 'confirmed')
     returning *;
end
$$;

commit;
