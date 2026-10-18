-- Apply after booking_guarantees; never run by application startup.
begin;
-- Private rollback material. It is not accessible through tenant APIs.
create table if not exists booking_settings_rollback (
  key text primary key, value jsonb not null
);
alter table booking_settings_rollback enable row level security;
revoke all on booking_settings_rollback from public;
do $$ declare r text; begin
  foreach r in array array['anon','authenticated','service_role'] loop
    if exists(select 1 from pg_roles where rolname=r) then
      execute format('revoke all on booking_settings_rollback from %I', r);
    end if;
  end loop;
end $$;
-- Stop on reader disagreements: the old slot generator and grid interpret
-- inverted hours differently. NULL standard hours also disagree before this slice.
do $$ begin
  if to_regprocedure('enforce_booking_capacity()') is null then
    raise exception 'Apply booking_guarantees before no_defaults';
  end if;
  if not exists(select 1 from booking_settings_rollback where key='snapshot_complete')
     and exists(select 1 from scheduling_config c where exists(select 1 from subscriptions s where s.tenant_id=c.tenant_id and s.status='active' and s.module in ('scheduling','tms','fsm'))
            and (open_time is null or close_time is null or open_time >= close_time)) then
    raise exception 'BLAM8_SETTINGS_CONFLICT: existing hours disagree between readers; review before migration';
  end if;
end $$;
insert into booking_settings_rollback
select 'row:' || tenant_id, to_jsonb(c) from scheduling_config c
where exists(select 1 from subscriptions s where s.tenant_id=c.tenant_id and s.status='active' and s.module in ('scheduling','tms','fsm'))
  and not exists(select 1 from booking_settings_rollback where key='snapshot_complete')
on conflict do nothing;
insert into booking_settings_rollback
select 'function:' || oid::regprocedure::text, to_jsonb(pg_get_functiondef(oid))
from pg_proc where pronamespace='public'::regnamespace and proname in
 ('booking_span_bounds','enforce_booking_capacity','hold_booking_if_capacity','move_booking_if_capacity')
on conflict do nothing;
-- Snapshot only businesses with an active booking module; other missing rows stay missing.
insert into scheduling_config(tenant_id)
select id from tenants t where exists(select 1 from subscriptions s
  where s.tenant_id=t.id and s.status='active' and s.module in ('scheduling','tms','fsm'))
  and not exists(select 1 from booking_settings_rollback where key='snapshot_complete')
on conflict do nothing;
alter table scheduling_config add column if not exists timezone text;
alter table scheduling_config add column if not exists booking_setting_review jsonb not null default '{}';
alter table scheduling_config add column if not exists booking_revision bigint not null default 0;
update scheduling_config c set
 capacity_per_window=coalesce(c.capacity_per_window,4),
 working_days=coalesce(c.working_days,'mon,tue,wed,thu,fri'),
 open_time=coalesce(c.open_time,time '09:00'), close_time=coalesce(c.close_time,time '17:00'),
 timezone='Europe/London', booking_revision=1,
 booking_setting_review=(select jsonb_object_agg(k, jsonb_build_object(
   'state','set','review_status','carried_over_review','revision',1,
   'snapshot_version','20260927_no_defaults',
   'source',case when not exists(select 1 from booking_settings_rollback where key='row:'||c.tenant_id)
      then 'missing_row_fallback' when k='timezone' then 'engine_timezone'
      when (select value->>(case k when 'capacity' then 'capacity_per_window'
                                  when 'opening_hours' then 'open_time' else k end)
            from booking_settings_rollback where key='row:'||c.tenant_id) is null then 'runtime_fallback'
      else 'stored_value' end))
 from unnest(array['capacity','working_days','opening_hours','timezone']) k)
where exists(select 1 from subscriptions s where s.tenant_id=c.tenant_id and s.status='active' and s.module in ('scheduling','tms','fsm'))
  and booking_revision=0 and not exists(select 1 from booking_settings_rollback where key='snapshot_complete');
insert into booking_settings_rollback values('snapshot_complete','true') on conflict do nothing;
alter table scheduling_config alter column capacity_per_window drop default;
alter table scheduling_config alter column capacity_per_window drop not null;
alter table scheduling_config alter column working_days drop default;
alter table scheduling_config alter column working_days drop not null;
alter table scheduling_config alter column open_time drop default;
alter table scheduling_config alter column open_time drop not null;
alter table scheduling_config alter column close_time drop default;
alter table scheduling_config alter column close_time drop not null;
-- Existing scheduling_config row policies also cover these columns.
-- No SECURITY DEFINER: the authenticated caller must own the row under RLS.
create or replace function apply_booking_settings(p_tenant uuid, p_revision bigint, p_patch jsonb)
returns void language plpgsql security invoker set search_path=public as $$
declare current_row scheduling_config; assignments text;
begin
  perform pg_advisory_xact_lock(hashtextextended('blam8:office:' || p_tenant::text, 0));
  insert into scheduling_config(tenant_id) values(p_tenant) on conflict do nothing;
  select * into current_row from scheduling_config where tenant_id=p_tenant for update;
  if not found or current_row.booking_revision <> p_revision then
    raise exception 'BLAM8_SETTINGS_STALE: reload settings';
  end if;
  if (p_patch->>'booking_revision')::bigint is distinct from p_revision+1
     or p_patch ? 'tenant_id' then raise exception 'Invalid settings revision'; end if;
  select string_agg(format('%I = r.%I', key, key), ',') into assignments
    from jsonb_object_keys(p_patch) key;
  execute 'update scheduling_config c set ' || assignments ||
    ' from jsonb_populate_record($1, $2) r where c.tenant_id=$3'
    using current_row, p_patch, p_tenant;
end $$;
revoke all on function apply_booking_settings(uuid,bigint,jsonb) from public;
do $$ declare r text; begin
 foreach r in array array['authenticated','service_role'] loop
   if exists(select 1 from pg_roles where rolname=r) then
     execute format('grant execute on function apply_booking_settings(uuid,bigint,jsonb) to %I',r);
   end if;
 end loop;
end $$;
-- Replace the existing functions in place; rollback restores their exact bodies.
do $$ declare f record; body text; patterns text[]; replacements text[]; i integer; begin
 for f in select oid, proname from pg_proc where pronamespace='public'::regnamespace
   and proname in ('enforce_booking_capacity','booking_span_bounds','hold_booking_if_capacity') loop
   select value #>> '{}' into body from booking_settings_rollback
     where key='function:' || f.oid::regprocedure::text;
   if body is null then raise exception 'Missing rollback function: %', f.proname; end if;
   if f.proname='enforce_booking_capacity' then
     patterns := array['coalesce(c.capacity_per_window, 4)',
       'select * into c from scheduling_config where tenant_id = new.tenant_id;', 'if quantity < 1 or cap < 1 then'];
     replacements := array['c.capacity_per_window',
       'select * into c from scheduling_config where tenant_id = new.tenant_id;
       if c.timezone is distinct from ''Europe/London'' or c.working_days is null or c.working_days = ''''
          or c.open_time is null or c.close_time is null or c.open_time >= c.close_time
          or exists(select 1 from jsonb_each(c.booking_setting_review) m
             where m.key in (''working_days'',''opening_hours'',''timezone'')
               and m.value->>''state'' is distinct from ''set'')
          or (c.booking_setting_review->''capacity''->>''state'') in (''unset'',''off'')
          or (c.capacity_per_window is null and
              (c.booking_setting_review->''capacity''->>''state'') is distinct from ''not_applicable'')
       then raise exception ''BLAM8_BOOKING_UNSUPPORTED: booking settings unresolved''; end if;',
       'if cap is null or cap < 1 then raise exception ''BLAM8_BOOKING_UNSUPPORTED: capacity is not set or off''; end if;
  if quantity < 1 or cap < 1 then'];
   elsif f.proname='booking_span_bounds' then
     patterns := array['coalesce(c.open_time, time ''09:00'')', 'coalesce(c.close_time, time ''17:00'')'];
     replacements := array['c.open_time', 'c.close_time'];
   elsif f.proname='hold_booking_if_capacity' then
     patterns := array['v_capacity := coalesce(v_capacity, 4);'];
     replacements := array['if v_capacity is null then raise exception ''BLAM8_BOOKING_UNSUPPORTED: capacity is not set''; end if;'];
   end if;
   for i in 1..array_length(patterns, 1) loop
     if strpos(body, patterns[i]) = 0 then
       raise exception 'BLAM8_FUNCTION_DRIFT: function %, missing pattern %', f.proname, patterns[i];
     end if;
     -- The readiness block belongs immediately after the single config read.
     if patterns[i] = 'select * into c from scheduling_config where tenant_id = new.tenant_id;'
        and strpos(substr(body, strpos(body, patterns[i]) + length(patterns[i])), patterns[i]) > 0 then
       raise exception 'BLAM8_FUNCTION_DRIFT: function %, repeated pattern %', f.proname, patterns[i];
     end if;
     body := replace(body, patterns[i], replacements[i]);
   end loop;
   execute body;
 end loop;
end $$;
commit;
