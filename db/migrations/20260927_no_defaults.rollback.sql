-- Deploy the previous application first. Preserve owner edits made after migration.
begin;
drop function if exists apply_booking_settings(uuid,bigint,jsonb);
do $$ declare f record; begin
 if to_regclass('public.booking_settings_rollback') is not null then
   for f in select value from booking_settings_rollback where key like 'function:%' loop
     execute f.value #>> '{}';
   end loop;
 end if;
end $$;
-- Unset values must remain NULL on rollback: the previous app supplies its old
-- runtime defaults. Reinstating NOT NULL would invent owner values or fail.
alter table scheduling_config alter column capacity_per_window set default 4;
alter table scheduling_config alter column working_days set default 'mon,tue,wed,thu,fri';
alter table scheduling_config alter column open_time set default '09:00';
alter table scheduling_config alter column close_time set default '17:00';
-- Retain inert provenance and private rollback material so re-application never
-- marks newly created/unset businesses as carried over, or discards confirmations.
commit;
