-- Actual-provider extension; requires telephone pilot and existing CRM schema.
-- No credentials or activation. All access service-only through guarded routes.
begin;
alter table telephone_calls add column if not exists direction text not null default 'outbound' check(direction in ('inbound','outbound'));
alter table telephone_calls add column if not exists simulated boolean not null default true;
alter table telephone_calls add column if not exists message_erased boolean not null default false;
alter table telephone_calls add column if not exists request_payload jsonb;
alter table telephone_calls add column if not exists started_at timestamptz;
create table if not exists telephone_routes (
 id uuid primary key default gen_random_uuid(), tenant_id uuid not null unique references tenants(id) on delete cascade,
 account_id text not null, phone_number text not null,
 enabled boolean not null default false, provider_verified boolean not null default false,
 greeting text not null default '', instructions text not null default '',
 original_number text, transfer_number text, transfer_verified boolean not null default false,
 forwarding_state text not null default 'unknown' check(forwarding_state in ('unknown','active','restoration_required','restored')),
 transfer_timeout_seconds integer not null default 20 check(transfer_timeout_seconds between 5 and 60),
 configured_by uuid not null, updated_at timestamptz not null default now(), unique(account_id,phone_number)
);
create table if not exists telephone_inbound_outcomes (
 tenant_id uuid not null, call_id uuid not null, direction text not null,
 transport_status text not null, outcome text not null, objective_achieved boolean not null,
 usage_complete boolean not null default false, cost_minor integer, simulated boolean not null default false check(not simulated),
 created_at timestamptz not null default now(), updated_at timestamptz not null default now(), primary key(tenant_id,call_id),
 foreign key(tenant_id,call_id) references telephone_calls(tenant_id,id) on delete cascade
);
create table if not exists telephone_messages (
 tenant_id uuid not null, call_id uuid not null, revision integer not null check(revision>0),
 text text not null check(length(text) between 1 and 2000), confirmed boolean not null,
 updated_at timestamptz not null default now(), primary key(tenant_id,call_id),
 foreign key(tenant_id,call_id) references telephone_calls(tenant_id,id) on delete cascade
);
create table if not exists telephone_message_outbox (
 tenant_id uuid not null, call_id uuid not null, revision integer not null,
 status text not null default 'pending' check(status in ('pending','delivered','superseded')),
 delivered_at timestamptz, primary key(tenant_id,call_id,revision),
 foreign key(tenant_id,call_id) references telephone_calls(tenant_id,id) on delete cascade
);
create table if not exists telephone_transfers (
 id uuid primary key default gen_random_uuid(), tenant_id uuid not null, call_id uuid not null,
 target text not null, status text not null default 'requested' check(status in ('requested','accepted','answered','failed','timed_out')),
 provider_leg_id text, last_sequence integer not null default -1,
 timeout_seconds integer not null, expires_at timestamptz not null,
 created_at timestamptz not null default now(), updated_at timestamptz not null default now(), unique(tenant_id,call_id),
 foreign key(tenant_id,call_id) references telephone_calls(tenant_id,id) on delete cascade
);
alter table telephone_transfers add column if not exists network_answered boolean not null default false;
alter table telephone_transfers add column if not exists human_confirmed boolean not null default false;
-- Older candidate receipts established a network answer only, never human consent.
update telephone_transfers set network_answered=true,status='accepted' where status='answered' and not human_confirmed;
create table if not exists telephone_sip_sessions (
 tenant_id uuid not null, call_id uuid not null, token_hash text not null unique,
 expires_at timestamptz not null, consumed_at timestamptz, model_call_id text unique,
 state text not null default 'issued' check(state in ('issued','accepting','accepted','unknown','failed')),
 updated_at timestamptz not null default now(), primary key(tenant_id,call_id),
 foreign key(tenant_id,call_id) references telephone_calls(tenant_id,id) on delete cascade
);
create table if not exists telephone_tool_receipts (
 tenant_id uuid not null, call_id uuid not null, tool_call_id text not null,
 name text not null, args jsonb not null, result jsonb not null, created_at timestamptz not null default now(),
 primary key(tenant_id,call_id,tool_call_id),
 foreign key(tenant_id,call_id) references telephone_calls(tenant_id,id) on delete cascade
);
create table if not exists telephone_model_usage (
 tenant_id uuid not null, call_id uuid not null, response_id text not null,
 usage jsonb not null, created_at timestamptz not null default now(),
 primary key(tenant_id,call_id,response_id),
 foreign key(tenant_id,call_id) references telephone_calls(tenant_id,id) on delete cascade
);
alter table telephone_routes enable row level security;
alter table telephone_inbound_outcomes enable row level security;
alter table telephone_messages enable row level security;
alter table telephone_message_outbox enable row level security;
alter table telephone_transfers enable row level security;
alter table telephone_sip_sessions enable row level security;
alter table telephone_tool_receipts enable row level security;
alter table telephone_model_usage enable row level security;
revoke all on telephone_routes,telephone_inbound_outcomes,telephone_messages,telephone_message_outbox,telephone_transfers,telephone_sip_sessions,telephone_tool_receipts from public,anon,authenticated;
grant select,insert,update,delete on telephone_routes,telephone_inbound_outcomes,telephone_messages,telephone_message_outbox,telephone_transfers,telephone_sip_sessions,telephone_tool_receipts to service_role;
revoke all on telephone_model_usage from public,anon,authenticated;
grant select,insert,update,delete on telephone_model_usage to service_role;

create or replace function telephone_numeric_usage(p_usage jsonb,p_depth integer default 0)
returns boolean language plpgsql immutable set search_path=public,pg_temp as $$
declare k text; v jsonb;
begin
 if p_depth>3 or jsonb_typeof(p_usage) is distinct from 'object' then return false; end if;
 for k,v in select * from jsonb_each(p_usage) loop
  if k not in ('total_tokens','input_tokens','output_tokens','input_token_details','output_token_details','cached_tokens','text_tokens','audio_tokens','image_tokens','cached_tokens_details') then return false; end if;
  if jsonb_typeof(v)='object' then
   if k not in ('input_token_details','output_token_details','cached_tokens_details') or not telephone_numeric_usage(v,p_depth+1) then return false; end if;
  elsif jsonb_typeof(v)<>'number' or v::text !~ '^[0-9]{1,9}$' then return false;
  end if;
 end loop;
 return true;
end;
$$;
revoke all on function telephone_numeric_usage(jsonb,integer) from public,anon,authenticated;
grant execute on function telephone_numeric_usage(jsonb,integer) to service_role;

create or replace function telephone_inbound_view(p_tenant uuid,p_call uuid)
returns jsonb language sql security invoker set search_path=public,pg_temp as $$
 select to_jsonb(c) || jsonb_build_object(
  'usage_complete',coalesce(o.usage_complete,false),'cost_minor',case when o.usage_complete then o.cost_minor else null end,
  'message_revision',coalesce(m.revision,0),'message_text',m.text,'message_confirmed',coalesce(m.confirmed,false),
  'transfer',(select to_jsonb(t) || jsonb_build_object('status',case when t.status in ('requested','accepted') and t.expires_at<=now() then 'timed_out' else t.status end)
    from telephone_transfers t where t.tenant_id=c.tenant_id and t.call_id=c.id),
  'session',(select (to_jsonb(s)-'token_hash') || jsonb_build_object('consumed',s.consumed_at is not null)
    from telephone_sip_sessions s where s.tenant_id=c.tenant_id and s.call_id=c.id),
  'model_usage',coalesce((select jsonb_agg(jsonb_build_object('response_id',u.response_id,'usage',u.usage))
    from(select response_id,usage from telephone_model_usage where tenant_id=c.tenant_id and call_id=c.id order by created_at limit 100)u),'[]'::jsonb),
  'remaining_seconds',case when c.started_at is null then (c.snapshot->>'max_duration_seconds')::integer
    else greatest(0,(c.snapshot->>'max_duration_seconds')::integer-extract(epoch from(now()-c.started_at))::integer) end)
 from telephone_calls c left join telephone_messages m on m.tenant_id=c.tenant_id and m.call_id=c.id
 left join telephone_inbound_outcomes o on o.tenant_id=c.tenant_id and o.call_id=c.id
 where c.tenant_id=p_tenant and c.id=p_call and not c.simulated;
$$;
revoke all on function telephone_inbound_view(uuid,uuid) from public,anon,authenticated;
grant execute on function telephone_inbound_view(uuid,uuid) to service_role;

create or replace function telephone_inbound_command(p_tenant uuid,p_user uuid,p_action text,p_payload jsonb default '{}')
returns jsonb language plpgsql security invoker set search_path=public,pg_temp as $$
declare
 cfg telephone_settings; r telephone_routes; c telephone_calls; e telephone_eligibility;
 m telephone_messages; tr telephone_transfers; sess telephone_sip_sessions; receipt telephone_tool_receipts;
 incoming jsonb; old_event jsonb; result_value jsonb; id_value uuid;
 phone_value text; caller_value text; account_value text; sid_value text; token_value text;
 active_count bigint; spent bigint; new_status text; new_outcome text;
 sequence_no integer; amount integer; duration integer; terminal boolean; usage_done boolean; was_terminal boolean;
 owner_action boolean; expected integer; tool_name text; tool_args jsonb;
begin
 if p_action is null or p_action not in ('configure_route','config_route','offboard','read','list','cancel','erase_message',
  'create_outbound','approve_outbound','claim_outbound','submitted_outbound','unknown_outbound','failed_outbound',
  'admit','resolve','resolve_attempt','bind_sip','claim_sip','bind_model','resolve_model','model_usage','message','transfer_request','transfer_update','human_accept','finish_handover','event','outbox_deliver','tool','opt_out') then raise exception 'invalid_command'; end if;
 if p_payload is null or jsonb_typeof(p_payload)<>'object' then raise exception 'invalid_payload'; end if;
 owner_action=p_action in ('configure_route','config_route','offboard','read','list','cancel','erase_message','create_outbound','approve_outbound','claim_outbound');
 if owner_action and (p_tenant is null or p_user is null or not exists(
  select 1 from tenant_members where tenant_id=p_tenant and user_id=p_user and role='owner' and active)) then raise exception 'owner_required'; end if;

 if p_action='admit' then
  account_value=p_payload->>'account_id'; phone_value=p_payload->>'to_number'; sid_value=p_payload->>'provider_call_id';
  caller_value=p_payload->>'from_number';
  if lower(coalesce(caller_value,'')) in ('','anonymous','restricted','unknown','withheld') then caller_value=null; end if;
  if coalesce(length(account_value),0) not between 1 and 128 or coalesce(length(sid_value),0) not between 1 and 128
   or phone_value is null or phone_value !~ '^\+[1-9][0-9]{7,14}$'
   or (caller_value is not null and caller_value !~ '^\+[1-9][0-9]{7,14}$') then raise exception 'invalid_payload'; end if;
  select * into r from telephone_routes where account_id=account_value and phone_number=phone_value;
  if not found then raise exception 'not_found'; end if;
  if p_tenant is not null and p_tenant<>r.tenant_id then raise exception 'provider_mismatch'; end if;
  p_tenant=r.tenant_id;
 elsif p_action in ('resolve','resolve_attempt','resolve_model','claim_sip','bind_model') then
  if p_action='resolve' then
   select * into c from telephone_calls where provider_call_id=p_payload->>'provider_call_id' and snapshot->>'account_id'=p_payload->>'account_id' and not simulated;
  elsif p_action='resolve_attempt' then
   select * into c from telephone_calls where id=(p_payload->>'call_id')::uuid and not simulated and direction='outbound'
    and snapshot->>'account_id'=p_payload->>'account_id' and attempt_id=p_payload->>'attempt_id'
    and snapshot->>'to_number'=p_payload->>'to_number' and snapshot->>'from_number'=p_payload->>'from_number'
    and status in ('submitting','unknown','initiated','ringing','answered','completed','failed','busy','no_answer','cancelled');
  elsif p_action='claim_sip' then
   token_value=p_payload->>'token';
   if token_value is null or token_value !~ '^[a-fA-F0-9]{64}$' then raise exception 'correlation_invalid'; end if;
   select * into sess from telephone_sip_sessions where token_hash=encode(sha256(convert_to(token_value,'UTF8')),'hex');
   if not found then raise exception 'correlation_invalid'; end if;
   select * into c from telephone_calls where id=sess.call_id and tenant_id=sess.tenant_id and not simulated;
  else
   select * into sess from telephone_sip_sessions where model_call_id=p_payload->>'model_call_id';
   if not found then raise exception 'not_found'; end if;
   select * into c from telephone_calls where id=sess.call_id and tenant_id=sess.tenant_id and not simulated;
  end if;
  if c.id is null then raise exception 'not_found'; end if;
  if p_tenant is not null and p_tenant<>c.tenant_id then raise exception 'provider_mismatch'; end if;
  p_tenant=c.tenant_id; id_value=c.id;
 end if;
 if p_tenant is null then raise exception 'invalid_payload'; end if;
 insert into telephone_settings(tenant_id) values(p_tenant) on conflict do nothing;
 select * into cfg from telephone_settings where tenant_id=p_tenant for update;
 -- One tenant mutex serializes real and simulated capacity/budget reservations.
 select * into r from telephone_routes where tenant_id=p_tenant;
 if p_action='admit' and (r.account_id is distinct from account_value or r.phone_number is distinct from phone_value) then raise exception 'provider_mismatch'; end if;

 if p_action='config_route' then return coalesce(to_jsonb(r),jsonb_build_object('enabled',false,'provider_verified',false)); end if;
 if p_action='configure_route' then
  if coalesce(length(p_payload->>'account_id'),0) not between 1 and 128
   or p_payload->>'phone_number' is null or p_payload->>'phone_number' !~ '^\+[1-9][0-9]{7,14}$'
   or (p_payload->>'original_number' is not null and p_payload->>'original_number' !~ '^\+[1-9][0-9]{7,14}$')
   or (p_payload->>'transfer_number' is not null and p_payload->>'transfer_number' !~ '^\+[1-9][0-9]{7,14}$')
   or coalesce(length(p_payload->>'greeting'),0)>300 or coalesce(length(p_payload->>'instructions'),0)>2000
   or coalesce(p_payload->>'forwarding_state','unknown') not in ('unknown','active','restoration_required','restored')
   or coalesce((p_payload->>'transfer_timeout_seconds')::integer,20) not between 5 and 60 then raise exception 'invalid_payload'; end if;
  if coalesce((p_payload->>'enabled')::boolean,false) then
   if not coalesce((p_payload->>'provider_verified')::boolean,false) then raise exception 'provider_unverified'; end if;
   if coalesce(length(trim(p_payload->>'greeting')),0)=0 or coalesce(length(trim(p_payload->>'instructions')),0)=0 then raise exception 'invalid_payload'; end if;
  end if;
  if p_payload->>'transfer_number' is not null and (p_payload->>'transfer_number'=p_payload->>'phone_number'
   or p_payload->>'transfer_number'=p_payload->>'original_number'
   or exists(select 1 from telephone_routes where enabled and phone_number=p_payload->>'transfer_number')) then raise exception 'handover_unavailable'; end if;
  insert into telephone_routes(tenant_id,account_id,phone_number,enabled,provider_verified,greeting,instructions,original_number,transfer_number,transfer_verified,forwarding_state,transfer_timeout_seconds,configured_by)
   values(p_tenant,p_payload->>'account_id',p_payload->>'phone_number',coalesce((p_payload->>'enabled')::boolean,false),
    coalesce((p_payload->>'provider_verified')::boolean,false),coalesce(p_payload->>'greeting',''),coalesce(p_payload->>'instructions',''),p_payload->>'original_number',p_payload->>'transfer_number',
    coalesce((p_payload->>'transfer_verified')::boolean,false),coalesce(p_payload->>'forwarding_state','unknown'),coalesce((p_payload->>'transfer_timeout_seconds')::integer,20),p_user)
   on conflict(tenant_id) do update set account_id=excluded.account_id,phone_number=excluded.phone_number,enabled=excluded.enabled,
    provider_verified=excluded.provider_verified,greeting=excluded.greeting,instructions=excluded.instructions,original_number=excluded.original_number,transfer_number=excluded.transfer_number,
    transfer_verified=excluded.transfer_verified,forwarding_state=excluded.forwarding_state,transfer_timeout_seconds=excluded.transfer_timeout_seconds,
    configured_by=excluded.configured_by,updated_at=now() returning * into r;
  return to_jsonb(r);
 end if;
 if p_action='offboard' then
  if r.id is null then raise exception 'not_found'; end if;
  update telephone_routes set enabled=false,
   forwarding_state=case when coalesce((p_payload->>'restored')::boolean,false) then 'restored' else 'restoration_required' end,
   updated_at=now() where tenant_id=p_tenant returning * into r;
  return to_jsonb(r) || jsonb_build_object('carrier_changed',false,'restoration_owner_attested',coalesce((p_payload->>'restored')::boolean,false));
 end if;
 if p_action='list' then return jsonb_build_object('calls',coalesce((select jsonb_agg(telephone_inbound_view(p_tenant,x.id))
  from(select id from telephone_calls where tenant_id=p_tenant and not simulated order by created_at desc limit 100)x),'[]'::jsonb)); end if;
 if p_action in ('admit','create_outbound') then
  if p_action='create_outbound' then
   incoming=p_payload || jsonb_build_object('provider','twilio');
   if coalesce(length(incoming->>'idempotency_key'),0) not between 1 and 128
    or incoming->>'to_number' is null or incoming->>'to_number' !~ '^\+[1-9][0-9]{7,14}$'
    or incoming->>'from_number' is null or incoming->>'from_number' !~ '^\+[1-9][0-9]{7,14}$'
    or coalesce(length(incoming->>'objective'),0) not between 1 and 1000 or coalesce(length(incoming->>'script'),0) not between 1 and 4000
    or coalesce((incoming->>'max_duration_seconds')::integer,0) not between 1 and cfg.max_duration_seconds then raise exception 'invalid_payload'; end if;
   select * into c from telephone_calls where tenant_id=p_tenant and idempotency_key=incoming->>'idempotency_key';
   if found then
    if c.simulated or c.user_id<>p_user or c.request_payload is distinct from incoming then raise exception 'idempotency_conflict'; end if;
    return telephone_inbound_view(p_tenant,c.id);
   end if;
  else
   select * into c from telephone_calls where provider_call_id=sid_value;
   if found then
    if c.simulated or c.tenant_id<>p_tenant or c.direction<>'inbound' or c.snapshot->>'account_id' is distinct from account_value
     or c.snapshot->>'to_number' is distinct from phone_value or c.snapshot->>'from_number' is distinct from caller_value then raise exception 'provider_mismatch'; end if;
    return telephone_inbound_view(p_tenant,c.id);
   end if;
  end if;
  if not cfg.enabled or r.id is null or not r.enabled then raise exception 'disabled'; end if;
  if not r.provider_verified then raise exception 'provider_unverified'; end if;
  if p_action='create_outbound' then
   if incoming->>'from_number'<>r.phone_number then raise exception 'provider_mismatch'; end if;
   select * into e from telephone_eligibility where tenant_id=p_tenant and phone=incoming->>'to_number';
   if coalesce(e.suppressed,false) then raise exception 'suppressed'; end if;
   if not coalesce(e.eligible and e.callback_requested and e.expires_at>now() and e.basis='owner_attested_callback',false) then raise exception 'ineligible'; end if;
   result_value=incoming;
   incoming=incoming || jsonb_build_object('account_id',r.account_id,'route_id',r.id,'max_call_cost_minor',cfg.max_call_cost_minor,
    'transfer_number',r.transfer_number,'transfer_verified',r.transfer_verified,'original_number',r.original_number,'transfer_timeout_seconds',r.transfer_timeout_seconds,
    'greeting',r.greeting,'instructions',r.instructions);
   insert into telephone_calls(tenant_id,user_id,idempotency_key,snapshot,direction,simulated,request_payload)
    values(p_tenant,p_user,incoming->>'idempotency_key',incoming,'outbound',false,result_value) returning * into c;
  else
   select count(*) into active_count from telephone_calls where tenant_id=p_tenant and status in ('submitting','initiated','ringing','answered','unknown');
   if active_count>=cfg.max_concurrent then raise exception 'capacity_exhausted'; end if;
   select coalesce(sum(greatest(reserved_minor,cost_minor)),0) into spent from telephone_calls where tenant_id=p_tenant and reservation_day=(now() at time zone 'UTC')::date;
   if spent+cfg.max_call_cost_minor>cfg.daily_budget_minor then raise exception 'budget_exhausted'; end if;
   incoming=jsonb_build_object('provider','twilio','account_id',r.account_id,'route_id',r.id,'to_number',phone_value,'from_number',caller_value,'caller_id_available',caller_value is not null,
    'objective','Answer, take a caller-confirmed message or request human handover; no account access.',
    'max_duration_seconds',cfg.max_duration_seconds,'max_call_cost_minor',cfg.max_call_cost_minor,
    'transfer_number',r.transfer_number,'transfer_verified',r.transfer_verified,'original_number',r.original_number,'transfer_timeout_seconds',r.transfer_timeout_seconds,
    'greeting',r.greeting,'instructions',r.instructions);
   insert into telephone_calls(tenant_id,user_id,idempotency_key,snapshot,status,provider_call_id,reserved_minor,reservation_day,direction,simulated,started_at)
    values(p_tenant,r.configured_by,'inbound:'||sid_value,incoming,'initiated',sid_value,cfg.max_call_cost_minor,(now() at time zone 'UTC')::date,'inbound',false,now()) returning * into c;
  end if;
  return telephone_inbound_view(p_tenant,c.id);
 end if;

 if id_value is null then id_value=(p_payload->>'call_id')::uuid; end if;
 select * into c from telephone_calls where tenant_id=p_tenant and id=id_value and not simulated for update;
 if not found then raise exception 'not_found'; end if;
 if p_action in ('read','resolve','resolve_attempt','resolve_model') then return telephone_inbound_view(p_tenant,c.id); end if;
 if p_action in ('approve_outbound','claim_outbound','submitted_outbound','unknown_outbound','failed_outbound') and c.direction<>'outbound' then raise exception 'invalid_state'; end if;
 if p_action='approve_outbound' then
  if not cfg.enabled or not coalesce(r.enabled and r.provider_verified,false) then raise exception 'disabled'; end if;
  if c.status<>'pending_approval' then raise exception 'invalid_state'; end if;
  update telephone_calls set status='approved',approved_by=p_user,approved_until=now()+interval '15 minutes',updated_at=now() where id=c.id;
 elsif p_action='claim_outbound' then
  if p_user<>c.user_id or not exists(select 1 from tenant_members where tenant_id=p_tenant and user_id=c.approved_by and role='owner' and active) then raise exception 'owner_required'; end if;
  if not cfg.enabled or not coalesce(r.enabled and r.provider_verified,false) then raise exception 'disabled'; end if;
  if c.snapshot->>'account_id'<>r.account_id or c.snapshot->>'from_number'<>r.phone_number then raise exception 'provider_mismatch'; end if;
  if c.attempt_id is not null then raise exception 'already_dispatched'; end if;
  if c.status<>'approved' or c.cancel_requested then raise exception 'invalid_state'; end if;
  if c.approved_until is null or c.approved_until<=now() then raise exception 'approval_expired'; end if;
  if coalesce(length(p_payload->>'attempt_id'),0) not between 1 and 128 or (c.snapshot->>'max_duration_seconds')::integer>cfg.max_duration_seconds then raise exception 'invalid_payload'; end if;
  select * into e from telephone_eligibility where tenant_id=p_tenant and phone=c.snapshot->>'to_number';
  if coalesce(e.suppressed,false) then raise exception 'suppressed'; end if;
  if not coalesce(e.eligible and e.callback_requested and e.expires_at>now() and e.basis='owner_attested_callback',false) then raise exception 'ineligible'; end if;
  select count(*) into active_count from telephone_calls where tenant_id=p_tenant and status in ('submitting','initiated','ringing','answered','unknown');
  if active_count>=cfg.max_concurrent then raise exception 'capacity_exhausted'; end if;
  select coalesce(sum(greatest(reserved_minor,cost_minor)),0) into spent from telephone_calls where tenant_id=p_tenant and reservation_day=(now() at time zone 'UTC')::date;
  amount=least(cfg.max_call_cost_minor,(c.snapshot->>'max_call_cost_minor')::integer);
  if spent+amount>cfg.daily_budget_minor then raise exception 'budget_exhausted'; end if;
  update telephone_calls set status='submitting',attempt_id=p_payload->>'attempt_id',reserved_minor=amount,reservation_day=(now() at time zone 'UTC')::date,started_at=now(),updated_at=now() where id=c.id;
 elsif p_action in ('submitted_outbound','unknown_outbound','failed_outbound') then
  if c.attempt_id is null or c.attempt_id is distinct from p_payload->>'attempt_id' then raise exception 'attempt_mismatch'; end if;
  if p_action='unknown_outbound' then
   -- A signed callback may have proved acceptance before HTTP timed out.
   if c.provider_call_id is not null then return telephone_inbound_view(p_tenant,c.id); end if;
   if c.status not in ('submitting','unknown') then raise exception 'invalid_state'; end if;
   update telephone_calls set status='unknown',error_code='submission_unknown',updated_at=now() where id=c.id;
  elsif p_action='failed_outbound' then
   if c.status<>'submitting' or c.provider_call_id is not null then raise exception 'invalid_state'; end if;
   update telephone_calls set status='failed',reserved_minor=0,error_code='provider_rejected',outcome='unknown',business_recorded=true,updated_at=now() where id=c.id;
   insert into telephone_inbound_outcomes(tenant_id,call_id,direction,transport_status,outcome,objective_achieved,usage_complete,cost_minor)
    values(p_tenant,c.id,'outbound','failed','unknown',false,true,0);
  else
   if coalesce(length(p_payload->>'provider_call_id'),0) not between 1 and 128 then raise exception 'invalid_payload'; end if;
   if c.provider_call_id is not null and c.provider_call_id<>p_payload->>'provider_call_id' then raise exception 'provider_mismatch'; end if;
   if c.status not in ('submitting','unknown') then
    if c.provider_call_id=p_payload->>'provider_call_id' then return telephone_inbound_view(p_tenant,c.id); end if;
    raise exception 'invalid_state';
   end if;
   update telephone_calls set status='initiated',provider_call_id=p_payload->>'provider_call_id',error_code=null,updated_at=now() where id=c.id;
  end if;
 elsif p_action='cancel' then
  update telephone_calls set cancel_requested=true,status=case when status in ('pending_approval','approved') then 'cancelled' else status end,updated_at=now() where id=c.id;
 elsif p_action='bind_sip' then
  token_value=p_payload->>'token';
  if token_value is null or token_value !~ '^[a-fA-F0-9]{64}$' then raise exception 'invalid_payload'; end if;
  if c.status not in ('initiated','ringing','answered') or c.cancel_requested then raise exception 'invalid_state'; end if;
  if c.started_at is null or c.started_at+make_interval(secs=>(c.snapshot->>'max_duration_seconds')::integer)<=now() then raise exception 'invalid_state'; end if;
  select * into sess from telephone_sip_sessions where tenant_id=p_tenant and call_id=c.id;
  if found then
   if sess.token_hash<>encode(sha256(convert_to(token_value,'UTF8')),'hex') then raise exception 'correlation_invalid'; end if;
   if sess.consumed_at is not null then raise exception 'correlation_consumed'; end if;
   if sess.expires_at<=now() then raise exception 'correlation_invalid'; end if;
   -- Idempotent answer-webhook retry; never renew the original expiry.
   return telephone_inbound_view(p_tenant,c.id);
  end if;
  insert into telephone_sip_sessions(tenant_id,call_id,token_hash,expires_at) values(p_tenant,c.id,encode(sha256(convert_to(token_value,'UTF8')),'hex'),now()+interval '90 seconds');
 elsif p_action='claim_sip' then
  select * into sess from telephone_sip_sessions where tenant_id=p_tenant and call_id=c.id for update;
  if sess.consumed_at is not null then raise exception 'correlation_consumed'; end if;
  if sess.expires_at<=now() then raise exception 'correlation_invalid'; end if;
  if c.status not in ('initiated','ringing','answered') or c.cancel_requested then raise exception 'invalid_state'; end if;
  if c.started_at is null or c.started_at+make_interval(secs=>(c.snapshot->>'max_duration_seconds')::integer)<=now() then raise exception 'invalid_state'; end if;
  if not cfg.enabled or not coalesce(r.enabled and r.provider_verified,false) then raise exception 'disabled'; end if;
  if coalesce(length(p_payload->>'model_call_id'),0) not between 1 and 128 then raise exception 'invalid_payload'; end if;
  update telephone_sip_sessions set consumed_at=now(),model_call_id=p_payload->>'model_call_id',state='accepting',updated_at=now() where tenant_id=p_tenant and call_id=c.id;
 elsif p_action='bind_model' then
  select * into sess from telephone_sip_sessions where tenant_id=p_tenant and call_id=c.id for update;
  new_status=p_payload->>'state';
  if new_status is null or new_status not in ('accepted','unknown','failed') then raise exception 'invalid_payload'; end if;
  if sess.state not in ('accepting','unknown') then
   if sess.state=new_status then return telephone_inbound_view(p_tenant,c.id); end if;
   raise exception 'invalid_state';
  end if;
  update telephone_sip_sessions set state=new_status,updated_at=now() where tenant_id=p_tenant and call_id=c.id;
 elsif p_action='model_usage' then
  if coalesce(length(p_payload->>'response_id'),0) not between 1 and 128
   or length(coalesce((p_payload->'usage')::text,''))>4096 or not telephone_numeric_usage(p_payload->'usage') then raise exception 'invalid_payload'; end if;
  if not exists(select 1 from telephone_sip_sessions where tenant_id=p_tenant and call_id=c.id and consumed_at is not null) then raise exception 'invalid_state'; end if;
  select usage into old_event from telephone_model_usage where tenant_id=p_tenant and call_id=c.id and response_id=p_payload->>'response_id';
  if found then
   if old_event<>p_payload->'usage' then raise exception 'event_conflict'; end if;
   return jsonb_build_object('recorded',true,'response_id',p_payload->>'response_id');
  end if;
  insert into telephone_model_usage(tenant_id,call_id,response_id,usage) values(p_tenant,c.id,p_payload->>'response_id',p_payload->'usage');
  return jsonb_build_object('recorded',true,'response_id',p_payload->>'response_id');
 elsif p_action='erase_message' then
  -- Non-content tombstone prevents old tool/event replay from restoring text.
  update telephone_calls set message_erased=true,updated_at=now() where id=c.id;
  delete from telephone_messages where tenant_id=p_tenant and call_id=c.id;
  update telephone_message_outbox set status='superseded' where tenant_id=p_tenant and call_id=c.id;
  update telephone_tool_receipts set args='{}',result=jsonb_build_object('erased',true) where tenant_id=p_tenant and call_id=c.id and name='save_message';
  update crm_activities set summary=null,subject='Telephone message erased' where tenant_id=p_tenant and activity_type='voice_call' and source_id='telephone:'||c.id::text;
 elsif p_action='opt_out' then
  phone_value=case when c.direction='inbound' then c.snapshot->>'from_number' else c.snapshot->>'to_number' end;
  if phone_value is not null and phone_value ~ '^\+[1-9][0-9]{7,14}$' then
   insert into telephone_eligibility(tenant_id,phone,suppressed) values(p_tenant,phone_value,true)
    on conflict(tenant_id,phone) do update set suppressed=true,updated_at=now();
  end if;
  -- This suppresses the bound phone only; it grants no caller identity.
  update telephone_calls set cancel_requested=true,updated_at=now() where id=c.id;
  return telephone_inbound_view(p_tenant,c.id) || jsonb_build_object('suppression_status',case when phone_value is not null and phone_value ~ '^\+[1-9][0-9]{7,14}$' then 'suppressed' else 'caller_number_unavailable' end);
 elsif p_action='message' then
  if c.message_erased then raise exception 'message_erased'; end if;
  if c.status not in ('initiated','ringing','answered') or c.cancel_requested then raise exception 'invalid_state'; end if;
  if c.started_at is null or c.started_at+make_interval(secs=>(c.snapshot->>'max_duration_seconds')::integer)<now() then raise exception 'invalid_state'; end if;
  expected=(p_payload->>'expected_revision')::integer;
  if expected is null or expected<0 or coalesce(length(p_payload->>'text'),0) not between 1 and 2000 or jsonb_typeof(p_payload->'confirmed') is distinct from 'boolean' then raise exception 'invalid_payload'; end if;
  select * into m from telephone_messages where tenant_id=p_tenant and call_id=c.id;
  if expected<>coalesce(m.revision,0) then raise exception 'revision_conflict'; end if;
  insert into telephone_messages(tenant_id,call_id,revision,text,confirmed)
   values(p_tenant,c.id,expected+1,p_payload->>'text',(p_payload->>'confirmed')::boolean)
   on conflict(tenant_id,call_id) do update set revision=excluded.revision,text=excluded.text,confirmed=excluded.confirmed,updated_at=now();
  update telephone_message_outbox set status='superseded' where tenant_id=p_tenant and call_id=c.id and status='pending';
  if (p_payload->>'confirmed')::boolean then insert into telephone_message_outbox(tenant_id,call_id,revision) values(p_tenant,c.id,expected+1); end if;
 elsif p_action='outbox_deliver' then
  if c.message_erased then raise exception 'message_erased'; end if;
  select * into m from telephone_messages where tenant_id=p_tenant and call_id=c.id;
  if not coalesce(m.confirmed,false) then raise exception 'invalid_state'; end if;
  if not exists(select 1 from telephone_message_outbox where tenant_id=p_tenant and call_id=c.id and revision=m.revision and status='pending') then
   return jsonb_build_object('call_id',c.id,'revision',m.revision,'delivered',exists(select 1 from telephone_message_outbox where tenant_id=p_tenant and call_id=c.id and revision=m.revision and status='delivered'));
  end if;
  insert into crm_activities(tenant_id,contact_id,company_id,activity_type,source_id,direction,subject,summary,occurred_at)
   values(p_tenant,null,null,'voice_call','telephone:'||c.id::text,c.direction,'Telephone message (caller supplied)',m.text,coalesce(c.started_at,c.created_at))
   on conflict(tenant_id,activity_type,source_id,(coalesce(direction,''))) where source_id is not null
   do update set summary=excluded.summary,subject=excluded.subject;
  update telephone_message_outbox set status='delivered',delivered_at=now() where tenant_id=p_tenant and call_id=c.id and revision=m.revision;
  return jsonb_build_object('call_id',c.id,'revision',m.revision,'delivered',true);
 elsif p_action='transfer_request' then
  if c.status not in ('initiated','ringing','answered') or c.cancel_requested then raise exception 'invalid_state'; end if;
  if c.started_at is null or c.started_at+make_interval(secs=>(c.snapshot->>'max_duration_seconds')::integer)<now() then raise exception 'invalid_state'; end if;
  if not coalesce(r.enabled and r.provider_verified,false) or not coalesce((c.snapshot->>'transfer_verified')::boolean,false)
   or c.snapshot->>'transfer_number' is null or c.snapshot->>'transfer_number'=c.snapshot->>'to_number'
   or c.snapshot->>'transfer_number'=c.snapshot->>'from_number' or c.snapshot->>'transfer_number'=c.snapshot->>'original_number'
   or exists(select 1 from telephone_routes where enabled and phone_number=c.snapshot->>'transfer_number') then raise exception 'handover_unavailable'; end if;
  if exists(select 1 from telephone_transfers where tenant_id=p_tenant and call_id=c.id) then raise exception 'handover_already_requested'; end if;
  insert into telephone_transfers(tenant_id,call_id,target,timeout_seconds,expires_at)
   values(p_tenant,c.id,c.snapshot->>'transfer_number',(c.snapshot->>'transfer_timeout_seconds')::integer,
    least(c.started_at+make_interval(secs=>(c.snapshot->>'max_duration_seconds')::integer),
     now()+make_interval(secs=>(c.snapshot->>'transfer_timeout_seconds')::integer+15)));
 elsif p_action='transfer_update' then
  select * into tr from telephone_transfers where tenant_id=p_tenant and call_id=c.id and id=(p_payload->>'transfer_id')::uuid for update;
  if not found then raise exception 'not_found'; end if;
  new_status=p_payload->>'status'; sequence_no=(p_payload->>'sequence')::integer;
  if sequence_no is null or sequence_no not between 0 and 1000000 or new_status is null or new_status not in ('accepted','answered','failed','timed_out') then raise exception 'invalid_payload'; end if;
  -- A local REST ACK uses sequence zero without a leg. A genuine first answered
  -- callback may also be zero; bound network evidence is stronger than that ACK.
  if sequence_no<tr.last_sequence or (sequence_no=tr.last_sequence and not(new_status='answered' and tr.status='accepted' and tr.provider_leg_id is null))
   or tr.status in ('answered','failed','timed_out') then return telephone_inbound_view(p_tenant,c.id); end if;
  if new_status in ('accepted','answered') then
   -- REST acknowledgement can precede creation of the child carrier leg.
   if new_status='answered' and coalesce(length(p_payload->>'provider_leg_id'),0) not between 1 and 128 then raise exception 'invalid_payload'; end if;
   if p_payload->>'provider_leg_id' is not null and length(p_payload->>'provider_leg_id') not between 1 and 128 then raise exception 'invalid_payload'; end if;
   if tr.provider_leg_id is not null and p_payload->>'provider_leg_id' is not null and tr.provider_leg_id<>p_payload->>'provider_leg_id' then raise exception 'provider_mismatch'; end if;
   -- An answered network leg is not proof that a human answered. Root validates
   -- the signed carrier callback's parent leg and actual target before this RPC.
   if new_status='answered' and (p_payload->>'parent_call_id' is distinct from c.provider_call_id or p_payload->>'target_number' is distinct from tr.target) then raise exception 'provider_mismatch'; end if;
  end if;
  if tr.expires_at<=now() and new_status in ('accepted','answered') then new_status='timed_out'; end if;
  update telephone_transfers set network_answered=network_answered or new_status='answered',
   status=case when new_status='answered' then 'accepted' else new_status end,
   provider_leg_id=coalesce(provider_leg_id,p_payload->>'provider_leg_id'),last_sequence=sequence_no,updated_at=now() where id=tr.id;
 elsif p_action in ('human_accept','finish_handover') then
  select * into tr from telephone_transfers where tenant_id=p_tenant and call_id=c.id and id=(p_payload->>'transfer_id')::uuid for update;
  if not found then raise exception 'not_found'; end if;
  if p_payload->>'parent_call_id' is distinct from c.provider_call_id or p_payload->>'target_number' is distinct from tr.target then raise exception 'provider_mismatch'; end if;
  if p_payload->>'provider_leg_id' is not null and (length(p_payload->>'provider_leg_id') not between 1 and 128
   or (tr.provider_leg_id is not null and tr.provider_leg_id<>p_payload->>'provider_leg_id')) then raise exception 'provider_mismatch'; end if;
  if p_action='finish_handover' then
   -- A network answer or provider acknowledgement cannot prove human acceptance.
   if not tr.human_confirmed then
    update telephone_transfers set status='failed',provider_leg_id=coalesce(provider_leg_id,p_payload->>'provider_leg_id'),updated_at=now() where id=tr.id;
   end if;
  else
   if coalesce(length(p_payload->>'provider_leg_id'),0) not between 1 and 128 then raise exception 'invalid_payload'; end if;
   if tr.human_confirmed then return telephone_inbound_view(p_tenant,c.id); end if;
   if tr.status in ('failed','timed_out') or tr.expires_at<=now() or c.cancel_requested
    or c.status not in ('initiated','ringing','answered') or c.started_at is null
    or c.started_at+make_interval(secs=>(c.snapshot->>'max_duration_seconds')::integer)<=now() then raise exception 'invalid_state'; end if;
   -- Only the signed callee Gather Digits=1 handler may invoke this service RPC.
   update telephone_transfers set status='answered',network_answered=true,human_confirmed=true,
    provider_leg_id=p_payload->>'provider_leg_id',updated_at=now() where id=tr.id;
  end if;
 elsif p_action='event' then
  if c.provider_call_id is null or c.provider_call_id is distinct from p_payload->>'provider_call_id' or c.snapshot->>'account_id' is distinct from p_payload->>'account_id' then raise exception 'provider_mismatch'; end if;
  new_status=p_payload->>'status'; new_outcome=coalesce(p_payload->>'outcome','none'); sequence_no=(p_payload->>'sequence')::integer;
  duration=coalesce((p_payload->>'duration_seconds')::integer,0); usage_done=coalesce((p_payload->>'usage_complete')::boolean,false);
  amount=(p_payload->>'cost_minor')::integer;
  if coalesce(length(p_payload->>'event_id'),0) not between 1 and 128 or sequence_no is null or sequence_no not between 0 and 1000000
   or new_status is null or new_status not in ('initiated','ringing','answered','completed','failed','busy','no_answer','cancelled')
   or new_outcome not in ('none','message_taken','callback_requested','declined','unknown') or duration not between 0 and 7200
   or (amount is not null and amount not between 0 and 10000000) or (usage_done and amount is null) then raise exception 'invalid_payload'; end if;
  select payload into old_event from telephone_provider_events where tenant_id=p_tenant and event_id=p_payload->>'event_id';
  if found then
   if old_event<>p_payload then raise exception 'event_conflict'; end if;
   return telephone_inbound_view(p_tenant,c.id);
  end if;
  insert into telephone_provider_events(tenant_id,call_id,event_id,sequence,payload) values(p_tenant,c.id,p_payload->>'event_id',sequence_no,p_payload);
  if sequence_no<=c.last_sequence then return telephone_inbound_view(p_tenant,c.id); end if;
  terminal=new_status in ('completed','failed','busy','no_answer','cancelled');
  was_terminal=c.status in ('completed','failed','busy','no_answer','cancelled');
  if was_terminal then
   if not terminal or not usage_done or not exists(select 1 from telephone_inbound_outcomes where tenant_id=p_tenant and call_id=c.id and not usage_complete) then return telephone_inbound_view(p_tenant,c.id); end if;
   -- Late authoritative usage may settle cost only; the outcome is immutable.
   update telephone_calls set cost_minor=amount,reserved_minor=0,last_sequence=sequence_no,
    error_code=case when amount>c.reserved_minor then 'cost_overrun' else error_code end,updated_at=now() where id=c.id;
   update telephone_inbound_outcomes set usage_complete=true,cost_minor=amount,updated_at=now() where tenant_id=p_tenant and call_id=c.id;
  else
   if not terminal and (case new_status when 'initiated' then 1 when 'ringing' then 2 when 'answered' then 3 end)<
    (case c.status when 'initiated' then 1 when 'ringing' then 2 when 'answered' then 3 else 0 end) then return telephone_inbound_view(p_tenant,c.id); end if;
   update telephone_calls set status=new_status,last_sequence=sequence_no,outcome=case when terminal then new_outcome else outcome end,
    duration_seconds=case when terminal then duration else duration_seconds end,
    cost_minor=case when terminal and usage_done then amount else cost_minor end,
    reserved_minor=case when terminal and usage_done then 0 else reserved_minor end,
    error_code=case when terminal and usage_done and amount>c.reserved_minor then 'cost_overrun' else error_code end,
    business_recorded=terminal or business_recorded,updated_at=now() where id=c.id;
   if terminal then
    insert into telephone_inbound_outcomes(tenant_id,call_id,direction,transport_status,outcome,objective_achieved,usage_complete,cost_minor)
     values(p_tenant,c.id,c.direction,new_status,new_outcome,new_status='completed' and new_outcome in ('message_taken','callback_requested'),usage_done,case when usage_done then amount else null end);
   end if;
  end if;
  if terminal and usage_done and amount>c.reserved_minor then update telephone_settings set enabled=false,updated_at=now() where tenant_id=p_tenant; end if;
 elsif p_action='tool' then
  if coalesce(length(p_payload->>'tool_call_id'),0) not between 1 and 128 then raise exception 'invalid_payload'; end if;
  tool_name=p_payload->>'name'; tool_args=p_payload->'args';
  if tool_name is null or tool_name not in ('save_message','request_handover') or jsonb_typeof(tool_args) is distinct from 'object' then raise exception 'invalid_payload'; end if;
  if tool_name='save_message' and c.message_erased then raise exception 'message_erased'; end if;
  select * into receipt from telephone_tool_receipts where tenant_id=p_tenant and call_id=c.id and tool_call_id=p_payload->>'tool_call_id';
  if found then
   if receipt.name<>tool_name or receipt.args<>tool_args then raise exception 'tool_conflict'; end if;
   return jsonb_build_object('result',receipt.result,'replayed',true);
  end if;
  if tool_name='save_message' then
   if tool_args - array['expected_revision','text','confirmed']::text[] <> '{}'::jsonb then raise exception 'invalid_payload'; end if;
   result_value=telephone_inbound_command(p_tenant,null,'message',tool_args || jsonb_build_object('call_id',c.id));
   result_value=jsonb_build_object('saved',true,'revision',result_value->'message_revision','confirmed',result_value->'message_confirmed');
  else
   if tool_args<>'{}'::jsonb then raise exception 'invalid_payload'; end if;
   result_value=telephone_inbound_command(p_tenant,null,'transfer_request',jsonb_build_object('call_id',c.id));
   result_value=jsonb_build_object('requested',true,'transfer_id',result_value->'transfer'->'id','status',result_value->'transfer'->'status');
  end if;
  insert into telephone_tool_receipts(tenant_id,call_id,tool_call_id,name,args,result) values(p_tenant,c.id,p_payload->>'tool_call_id',tool_name,tool_args,result_value);
  return jsonb_build_object('result',result_value,'replayed',false);
 end if;
 return telephone_inbound_view(p_tenant,c.id);
exception when invalid_text_representation or numeric_value_out_of_range or check_violation or not_null_violation or invalid_datetime_format or datetime_field_overflow then raise exception 'invalid_payload';
 when unique_violation then raise exception 'idempotency_conflict';
end;
$$;
revoke all on function telephone_inbound_command(uuid,uuid,text,jsonb) from public,anon,authenticated;
grant execute on function telephone_inbound_command(uuid,uuid,text,jsonb) to service_role;
commit;
