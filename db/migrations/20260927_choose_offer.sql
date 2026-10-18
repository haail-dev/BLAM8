-- Astra: atomic choice. Two taps (including different siblings) have one winner.
create or replace function public.choose_offered_booking(
    p_booking_id uuid, p_tenant_id uuid, p_details jsonb default null,
    p_exclude_ids uuid[] default array[]::uuid[]
) returns setof public.bookings
language plpgsql security invoker set search_path = public as $$
declare chosen bookings%rowtype; c scheduling_config;
begin
    select * into chosen from bookings
      where id = p_booking_id and tenant_id = p_tenant_id;
    if not found or chosen.contact is null then return; end if;
    perform pg_advisory_xact_lock(hashtextextended(
      p_tenant_id::text || ':offer:' || chosen.contact, 0));
    -- Same day lock as the existing capacity-checked office hold RPC. Recheck
    -- the expiry after waiting: a queue must not extend the customer's offer.
    perform pg_advisory_xact_lock(hashtextextended(
      p_tenant_id::text || ':' || chosen.booking_date::text, 0));
    -- Share the settings/capacity lock before checking readiness and expiry.
    -- The capacity trigger keeps an unchanged live hold's existing reservation.
    perform pg_advisory_xact_lock(hashtextextended('blam8:office:' || p_tenant_id::text, 0));
    select * into c from scheduling_config where tenant_id = p_tenant_id;
    if not found or c.timezone is distinct from 'Europe/London'
       or c.working_days is null or c.working_days = ''
       or c.open_time is null or c.close_time is null or c.open_time >= c.close_time
       or exists(select 1 from jsonb_each(c.booking_setting_review) m
          where m.key in ('working_days','opening_hours','timezone')
            and m.value->>'state' is distinct from 'set')
       or c.capacity_per_window <= 0
       or (c.booking_setting_review->'capacity'->>'state') in ('unset','off')
       or (c.capacity_per_window is null and
           (c.booking_setting_review->'capacity'->>'state') is distinct from 'not_applicable')
    then raise exception 'BLAM8_BOOKING_UNSUPPORTED: booking settings unresolved'; end if;
    select * into chosen from bookings
      where id = p_booking_id and tenant_id = p_tenant_id for update;
    if chosen.status <> 'offered' or chosen.expires_at is null
       or chosen.expires_at <= clock_timestamp() then return; end if;
    update bookings set status = 'confirmed', expires_at = null,
      details = coalesce(p_details, details), updated_at = clock_timestamp()
      where id = p_booking_id and tenant_id = p_tenant_id
      returning * into chosen;
    update bookings set status = 'cancelled', expires_at = null,
      updated_at = clock_timestamp()
      where tenant_id = p_tenant_id and contact = chosen.contact
        and status = 'offered' and id <> p_booking_id
        and not (id = any(p_exclude_ids));
    return next chosen;
end $$;
revoke all on function public.choose_offered_booking(uuid, uuid, jsonb, uuid[]) from public;
grant execute on function public.choose_offered_booking(uuid, uuid, jsonb, uuid[]) to service_role;
