-- Disabled-by-default, simulated-only callback pilot. No provider credentials.
-- All tables/RPC service-role only; routes derive identity from authenticated
-- TenantCtx and prohibit view-as. SQL independently rechecks current owner.
begin;
create table if not exists telephone_settings (
 tenant_id uuid primary key references tenants(id) on delete cascade,
 enabled boolean not null default false,
 daily_budget_minor integer not null default 1000 check(daily_budget_minor between 0 and 100000),
 max_concurrent integer not null default 1 check(max_concurrent between 1 and 10),
 max_call_cost_minor integer not null default 100 check(max_call_cost_minor between 1 and 10000),
 max_duration_seconds integer not null default 300 check(max_duration_seconds between 1 and 600),
 updated_at timestamptz not null default now()
);
create table if not exists telephone_eligibility (
 tenant_id uuid not null references tenants(id) on delete cascade,
 phone text not null check(phone ~ '^\+[1-9][0-9]{7,14}$'),
 eligible boolean not null default false, suppressed boolean not null default false,
 expires_at timestamptz, basis text, callback_requested boolean not null default false,
 attested_by uuid, updated_at timestamptz not null default now(),
 primary key(tenant_id,phone)
);
create table if not exists telephone_calls (
 id uuid primary key default gen_random_uuid(), tenant_id uuid not null references tenants(id) on delete cascade,
 user_id uuid not null, idempotency_key text not null,
 snapshot jsonb not null, status text not null default 'pending_approval'
 check(status in ('pending_approval','approved','submitting','initiated','ringing','answered','unknown','completed','failed','busy','no_answer','cancelled')),
 approved_by uuid, approved_until timestamptz, attempt_id text,
 provider_call_id text unique, last_sequence integer not null default -1,
 reserved_minor integer not null default 0 check(reserved_minor>=0), cost_minor integer not null default 0 check(cost_minor>=0),
 reservation_day date, duration_seconds integer, outcome text not null default 'none',
 cancel_requested boolean not null default false, error_code text,
 business_recorded boolean not null default false,
 created_at timestamptz not null default now(), updated_at timestamptz not null default now(),
 unique(tenant_id,idempotency_key), unique(tenant_id,id)
);
create table if not exists telephone_provider_events (
 tenant_id uuid not null, call_id uuid not null, event_id text not null,
 sequence integer not null check(sequence>=0), payload jsonb not null,
 created_at timestamptz not null default now(), primary key(tenant_id,event_id),
 foreign key(tenant_id,call_id) references telephone_calls(tenant_id,id) on delete cascade
);
create table if not exists telephone_business_records (
 tenant_id uuid not null, call_id uuid not null, record_type text not null default 'simulated_callback_outcome',
 transport_status text not null, outcome text not null,
 objective_achieved boolean not null, simulated boolean not null default true check(simulated),
 created_at timestamptz not null default now(), primary key(tenant_id,call_id),
 foreign key(tenant_id,call_id) references telephone_calls(tenant_id,id) on delete cascade
);
-- No browser table access; authenticated actions use the guarded server route.
alter table telephone_settings enable row level security;
alter table telephone_eligibility enable row level security;
alter table telephone_calls enable row level security;
alter table telephone_provider_events enable row level security;
alter table telephone_business_records enable row level security;
revoke all on telephone_settings,telephone_eligibility,telephone_calls,telephone_provider_events,telephone_business_records from public,anon,authenticated;
grant select,insert,update,delete on telephone_settings,telephone_eligibility,telephone_calls,telephone_provider_events,telephone_business_records to service_role;

create or replace function telephone_command(p_tenant uuid,p_user uuid,p_action text,p_payload jsonb default '{}')
returns jsonb language plpgsql security invoker set search_path=public,pg_temp as $$
declare
 cfg telephone_settings; c telephone_calls; e telephone_eligibility;
 incoming jsonb; existing jsonb; terminal boolean; sequence_no integer;
 spent bigint; active_count bigint; phone_value text; id_value uuid;
 new_status text; new_outcome text; amount integer; duration integer;
begin
 if p_action is null or p_action not in ('configure','eligibility','suppress','create','approve','claim','submitted','unknown','failed','event','cancel','read','list','config') then
  raise exception 'invalid_command';
 end if;
 if p_tenant is null or p_payload is null or jsonb_typeof(p_payload)<>'object' then raise exception 'invalid_payload'; end if;
 -- Internal completion/reconciliation must still work after an owner's demotion.
 if p_action not in ('submitted','unknown','failed','event') then
  if not exists(select 1 from tenant_members where tenant_id=p_tenant and user_id=p_user and role='owner' and active) then
   raise exception 'owner_required';
  end if;
 end if;
 insert into telephone_settings(tenant_id) values(p_tenant) on conflict do nothing;
 -- One tenant mutex: capacity/budget/eligibility/config and claims are atomic.
 select * into cfg from telephone_settings where tenant_id=p_tenant for update;
 if p_action='config' then return to_jsonb(cfg); end if;
 if p_action='configure' then
  update telephone_settings set enabled=coalesce((p_payload->>'enabled')::boolean,enabled),
   daily_budget_minor=coalesce((p_payload->>'daily_budget_minor')::integer,daily_budget_minor),
   max_concurrent=coalesce((p_payload->>'max_concurrent')::integer,max_concurrent),
   max_call_cost_minor=coalesce((p_payload->>'max_call_cost_minor')::integer,max_call_cost_minor),
   max_duration_seconds=coalesce((p_payload->>'max_duration_seconds')::integer,max_duration_seconds),updated_at=now()
   where tenant_id=p_tenant returning * into cfg;
  return to_jsonb(cfg);
 end if;
 if p_action in ('eligibility','suppress') then
  phone_value=p_payload->>'phone';
  if phone_value is null or phone_value !~ '^\+[1-9][0-9]{7,14}$' then raise exception 'invalid_payload'; end if;
  insert into telephone_eligibility(tenant_id,phone) values(p_tenant,phone_value) on conflict do nothing;
  if p_action='suppress' then
   if jsonb_typeof(p_payload->'suppressed')<>'boolean' then raise exception 'invalid_payload'; end if;
   update telephone_eligibility set suppressed=(p_payload->>'suppressed')::boolean,updated_at=now()
    where tenant_id=p_tenant and phone=phone_value returning * into e;
  else
   if p_payload->>'basis' is distinct from 'owner_attested_callback' or coalesce((p_payload->>'callback_requested')::boolean,false) is not true
    or p_payload->>'expires_at' is null or (p_payload->>'expires_at')::timestamptz <= now()
    or jsonb_typeof(p_payload->'eligible')<>'boolean' then raise exception 'invalid_payload'; end if;
   update telephone_eligibility set eligible=(p_payload->>'eligible')::boolean,expires_at=(p_payload->>'expires_at')::timestamptz,
    basis='owner_attested_callback',callback_requested=true,attested_by=p_user,updated_at=now()
    where tenant_id=p_tenant and phone=phone_value returning * into e;
  end if;
  return to_jsonb(e);
 end if;
 if p_action='list' then
  return jsonb_build_object('calls',coalesce((select jsonb_agg(to_jsonb(x)) from
   (select * from telephone_calls where tenant_id=p_tenant and snapshot->>'provider'='simulated' order by created_at desc limit 100) x),'[]'::jsonb));
 end if;
 if p_action='create' then
  incoming=p_payload - 'call_id';
  incoming=jsonb_set(incoming,'{provider}','"simulated"'::jsonb);
  if coalesce(length(incoming->>'idempotency_key'),0) not between 1 and 128
   or incoming->>'to_number' is null or incoming->>'to_number' !~ '^\+[1-9][0-9]{7,14}$'
   or incoming->>'from_number' is null or incoming->>'from_number' !~ '^\+[1-9][0-9]{7,14}$'
   or coalesce(length(incoming->>'objective'),0) not between 1 and 1000
   or coalesce(length(incoming->>'script'),0) not between 1 and 4000
   or incoming->>'scenario' is null or incoming->>'scenario' not in ('completed','failed','busy','no_answer','timeout','ambiguous')
   or coalesce((incoming->>'max_duration_seconds')::integer,0) not between 1 and cfg.max_duration_seconds then raise exception 'invalid_payload'; end if;
  select * into c from telephone_calls where tenant_id=p_tenant and idempotency_key=incoming->>'idempotency_key';
  if found then
   if (c.snapshot-'max_call_cost_minor')<>incoming or c.user_id<>p_user then raise exception 'idempotency_conflict'; end if;
   return to_jsonb(c);
  end if;
  if not cfg.enabled then raise exception 'disabled'; end if;
  select * into e from telephone_eligibility where tenant_id=p_tenant and phone=incoming->>'to_number';
  if coalesce(e.suppressed,false) then raise exception 'suppressed'; end if;
  if not coalesce(e.eligible and e.callback_requested and e.expires_at>now() and e.basis='owner_attested_callback',false) then raise exception 'ineligible'; end if;
  insert into telephone_calls(tenant_id,user_id,idempotency_key,snapshot)
   values(p_tenant,p_user,incoming->>'idempotency_key',incoming || jsonb_build_object('max_call_cost_minor',cfg.max_call_cost_minor)) returning * into c;
  return to_jsonb(c);
 end if;
 id_value=(p_payload->>'call_id')::uuid;
 select * into c from telephone_calls where tenant_id=p_tenant and id=id_value for update;
 if not found then raise exception 'not_found'; end if;
 if c.snapshot->>'provider' is distinct from 'simulated' then raise exception 'invalid_state'; end if;
 if p_action='read' then return to_jsonb(c); end if;
 if p_action='approve' then
  if not cfg.enabled then raise exception 'disabled'; end if;
  if c.status<>'pending_approval' then raise exception 'invalid_state'; end if;
  update telephone_calls set status='approved',approved_by=p_user,approved_until=now()+interval '15 minutes',updated_at=now()
   where id=c.id returning * into c;
 elsif p_action='claim' then
  -- Dispatch identity must be the immutable request actor, still owner now.
  if p_user<>c.user_id or not exists(select 1 from tenant_members where tenant_id=p_tenant and user_id=c.approved_by and role='owner' and active) then raise exception 'owner_required'; end if;
  if not cfg.enabled then raise exception 'disabled'; end if;
  if c.attempt_id is not null then raise exception 'already_dispatched'; end if;
  if c.status<>'approved' or c.cancel_requested then raise exception 'invalid_state'; end if;
  if c.approved_until is null or c.approved_until<=now() then raise exception 'approval_expired'; end if;
  if coalesce(length(p_payload->>'attempt_id'),0) not between 1 and 128 then raise exception 'invalid_payload'; end if;
  if (c.snapshot->>'max_duration_seconds')::integer>cfg.max_duration_seconds then raise exception 'invalid_payload'; end if;
  select * into e from telephone_eligibility where tenant_id=p_tenant and phone=c.snapshot->>'to_number';
  if coalesce(e.suppressed,false) then raise exception 'suppressed'; end if;
  if not coalesce(e.eligible and e.callback_requested and e.expires_at>now() and e.basis='owner_attested_callback',false) then raise exception 'ineligible'; end if;
  select count(*) into active_count from telephone_calls where tenant_id=p_tenant and status in ('submitting','initiated','ringing','answered','unknown');
  if active_count>=cfg.max_concurrent then raise exception 'capacity_exhausted'; end if;
  select coalesce(sum(greatest(reserved_minor,cost_minor)),0) into spent from telephone_calls where tenant_id=p_tenant and reservation_day=(now() at time zone 'UTC')::date;
  if spent+least(cfg.max_call_cost_minor,(c.snapshot->>'max_call_cost_minor')::integer)>cfg.daily_budget_minor then raise exception 'budget_exhausted'; end if;
  update telephone_calls set status='submitting',attempt_id=p_payload->>'attempt_id',reserved_minor=least(cfg.max_call_cost_minor,(c.snapshot->>'max_call_cost_minor')::integer),
   reservation_day=(now() at time zone 'UTC')::date,updated_at=now() where id=c.id returning * into c;
 elsif p_action in ('submitted','unknown','failed') then
  if c.attempt_id is null or c.attempt_id is distinct from p_payload->>'attempt_id' then raise exception 'attempt_mismatch'; end if;
  if p_action='failed' then
   if c.status<>'submitting' or c.provider_call_id is not null then raise exception 'invalid_state'; end if;
   update telephone_calls set status='failed',reserved_minor=0,error_code='provider_rejected',outcome='unknown',business_recorded=true,updated_at=now() where id=c.id returning * into c;
   insert into telephone_business_records(tenant_id,call_id,transport_status,outcome,objective_achieved) values(p_tenant,c.id,'failed','unknown',false);
  elsif p_action='unknown' then
   if c.status not in ('submitting','unknown') then raise exception 'invalid_state'; end if;
   update telephone_calls set status='unknown',error_code='submission_unknown',updated_at=now() where id=c.id returning * into c;
  else
   if coalesce(length(p_payload->>'provider_call_id'),0) not between 1 and 128 then raise exception 'invalid_payload'; end if;
   if c.provider_call_id is not null and c.provider_call_id<>p_payload->>'provider_call_id' then raise exception 'provider_mismatch'; end if;
   if c.status not in ('submitting','unknown') then
    if c.provider_call_id=p_payload->>'provider_call_id' then return to_jsonb(c); end if;
    raise exception 'invalid_state';
   end if;
   update telephone_calls set status='initiated',provider_call_id=p_payload->>'provider_call_id',error_code=null,updated_at=now() where id=c.id returning * into c;
  end if;
 elsif p_action='cancel' then
  if c.status in ('pending_approval','approved') then
   update telephone_calls set status='cancelled',cancel_requested=true,updated_at=now() where id=c.id returning * into c;
  elsif c.status in ('submitting','initiated','ringing','answered','unknown') then
   update telephone_calls set cancel_requested=true,updated_at=now() where id=c.id returning * into c;
  end if;
 elsif p_action='event' then
  if c.provider_call_id is null or c.provider_call_id is distinct from p_payload->>'provider_call_id' then raise exception 'provider_mismatch'; end if;
  sequence_no=(p_payload->>'sequence')::integer;
  new_status=p_payload->>'status'; new_outcome=coalesce(p_payload->>'outcome','none');
  amount=coalesce((p_payload->>'cost_minor')::integer,0); duration=coalesce((p_payload->>'duration_seconds')::integer,0);
  if coalesce(length(p_payload->>'event_id'),0) not between 1 and 128 or sequence_no is null or sequence_no<0
   or new_status is null or new_status not in ('initiated','ringing','answered','completed','failed','busy','no_answer','cancelled')
   or new_outcome not in ('none','message_taken','callback_requested','declined','unknown') or amount not between 0 and 10000000 or duration not between 0 and 7200 or sequence_no>1000000 then raise exception 'invalid_payload'; end if;
  select payload into existing from telephone_provider_events where tenant_id=p_tenant and event_id=p_payload->>'event_id';
  if found then
   if existing<>p_payload then raise exception 'event_conflict'; end if;
   return to_jsonb(c);
  end if;
  insert into telephone_provider_events(tenant_id,call_id,event_id,sequence,payload) values(p_tenant,c.id,p_payload->>'event_id',sequence_no,p_payload);
  -- Duplicate/out-of-order/late events cannot regress or rewrite a terminal outcome.
  if sequence_no<=c.last_sequence or c.status in ('completed','failed','busy','no_answer','cancelled') then return to_jsonb(c); end if;
  terminal=new_status in ('completed','failed','busy','no_answer','cancelled');
  if not terminal and (case new_status when 'initiated' then 1 when 'ringing' then 2 when 'answered' then 3 end)<
    (case c.status when 'initiated' then 1 when 'ringing' then 2 when 'answered' then 3 else 0 end) then return to_jsonb(c); end if;
  update telephone_calls set status=new_status,last_sequence=sequence_no,
   outcome=case when terminal then new_outcome else outcome end,
   cost_minor=case when terminal then amount else cost_minor end,
   duration_seconds=case when terminal then duration else duration_seconds end,
   reserved_minor=case when terminal then 0 else reserved_minor end,
   error_code=case when terminal and amount>c.reserved_minor then 'cost_overrun' else error_code end,
   updated_at=now() where id=c.id returning * into c;
  if terminal then
   insert into telephone_business_records(tenant_id,call_id,transport_status,outcome,objective_achieved)
    values(p_tenant,c.id,c.status,c.outcome,c.status='completed' and c.outcome in ('message_taken','callback_requested')) on conflict do nothing;
   update telephone_calls set business_recorded=true where id=c.id returning * into c;
   if c.error_code='cost_overrun' then update telephone_settings set enabled=false,updated_at=now() where tenant_id=p_tenant; end if;
  end if;
 end if;
 return to_jsonb(c);
exception when invalid_text_representation or numeric_value_out_of_range or check_violation or not_null_violation or invalid_datetime_format or datetime_field_overflow then
 raise exception 'invalid_payload';
end;
$$;
revoke all on function telephone_command(uuid,uuid,text,jsonb) from public,anon,authenticated;
grant execute on function telephone_command(uuid,uuid,text,jsonb) to service_role;
commit;
