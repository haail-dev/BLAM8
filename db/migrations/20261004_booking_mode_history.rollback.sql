-- Roll back individual conversions first with rollback_booking_mode(receipt_id).
-- Archive remains private and intact when uninstalling these functions.
begin;
do $$ begin
  if exists(select 1 from booking_mode_migrations where rolled_back_at is null) then
    raise exception 'BLAM8_MODE_INVALID: roll back active conversions before uninstall';
  end if;
end $$;
drop function if exists rollback_booking_mode(uuid);
drop function if exists migrate_booking_mode(uuid,jsonb,jsonb,jsonb);
drop function if exists validate_booking_mode_capacity(bookings);
drop function if exists booking_mode_snapshot(uuid);
commit;
