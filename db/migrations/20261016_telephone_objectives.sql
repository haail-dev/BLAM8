-- Versioned business forms and caller-statement evidence; service role only.
begin;
create table if not exists telephone_templates (
 tenant_id uuid not null references tenants(id) on delete cascade,
 id uuid not null, version integer not null check(version>0), spec jsonb not null,
 created_by uuid not null,created_at timestamptz not null default now(),
 primary key(tenant_id,id,version)
);
create table if not exists telephone_objectives (
 tenant_id uuid not null,call_id uuid not null,template_id uuid not null,template_version integer not null,
 template jsonb not null,facts jsonb not null default '{}',evidence jsonb not null default '{}',
 revision integer not null default 0,status text not null default 'incomplete',reason text,
 primary key(tenant_id,call_id),foreign key(tenant_id,call_id) references telephone_calls(tenant_id,id) on delete cascade,
 foreign key(tenant_id,template_id,template_version) references telephone_templates(tenant_id,id,version)
);
alter table telephone_templates enable row level security;
alter table telephone_objectives enable row level security;
create table if not exists telephone_fact_receipts (
 tenant_id uuid not null,call_id uuid not null,source_id text not null,delta jsonb not null,result jsonb not null,
 primary key(tenant_id,call_id,source_id),foreign key(tenant_id,call_id) references telephone_calls(tenant_id,id) on delete cascade
);
alter table telephone_fact_receipts enable row level security;
revoke all on telephone_templates,telephone_objectives from public,anon,authenticated;
grant select,insert,update,delete on telephone_templates,telephone_objectives to service_role;
revoke all on telephone_fact_receipts from public,anon,authenticated;
grant select,insert,update,delete on telephone_fact_receipts to service_role;
create or replace function telephone_objective_command(p_tenant uuid,p_user uuid,p_action text,p_payload jsonb default '{}')
returns jsonb language plpgsql security invoker set search_path=public,pg_temp as $$
declare c telephone_calls;o telephone_objectives;t telephone_templates;tid uuid;v integer;k text;ev jsonb;receipt telephone_fact_receipts;delta_value jsonb;
begin
 if p_tenant is null or jsonb_typeof(p_payload)<>'object' then raise exception 'invalid_payload'; end if;
 if p_action in ('save','list','read','bind','erase','states') then
  if not exists(select 1 from tenant_members where tenant_id=p_tenant and user_id=p_user and role='owner' and active) then raise exception 'owner_required'; end if;
 end if;
 if p_action='states' then
  return jsonb_build_object('states',coalesce((select jsonb_object_agg(calls.id::text,
   coalesce(to_jsonb(goals),'{"bound":false}'::jsonb)||jsonb_build_object('message_delivery',
    case when calls.message_erased then 'erased' when messages.call_id is null then 'not_captured'
     when not messages.confirmed then 'awaiting_confirmation' else coalesce(deliveries.status,'pending') end))
   from telephone_calls calls left join telephone_objectives goals on goals.tenant_id=calls.tenant_id and goals.call_id=calls.id
   left join telephone_messages messages on messages.tenant_id=calls.tenant_id and messages.call_id=calls.id
   left join telephone_message_outbox deliveries on deliveries.tenant_id=calls.tenant_id and deliveries.call_id=calls.id and deliveries.revision=messages.revision
   where calls.tenant_id=p_tenant and calls.id::text in(select jsonb_array_elements_text(p_payload->'call_ids'))),'{}'));
 elsif p_action='save' then
  insert into telephone_settings(tenant_id) values(p_tenant) on conflict do nothing;
  perform 1 from telephone_settings where tenant_id=p_tenant for update;
  tid=coalesce((p_payload->>'id')::uuid,gen_random_uuid());
  select coalesce(max(version),0)+1 into v from telephone_templates where tenant_id=p_tenant and id=tid;
  if jsonb_typeof(p_payload->'spec')<>'object' or octet_length((p_payload->'spec')::text)>32768 then raise exception 'invalid_payload'; end if;
  insert into telephone_templates values(p_tenant,tid,v,p_payload->'spec',p_user,now()) returning * into t;
  return to_jsonb(t);
 elsif p_action='list' then
  return jsonb_build_object('templates',coalesce((select jsonb_agg(to_jsonb(x)) from
   (select distinct on(id) * from telephone_templates where tenant_id=p_tenant order by id,version desc) x),'[]'));
 elsif p_action='read' then
  select * into t from telephone_templates where tenant_id=p_tenant and id=(p_payload->>'id')::uuid and version=(p_payload->>'version')::integer;
  if not found then raise exception 'not_found'; end if;return to_jsonb(t);
 end if;
 select * into c from telephone_calls where tenant_id=p_tenant and id=(p_payload->>'call_id')::uuid for update;
 if not found then raise exception 'not_found'; end if;
 if p_action='bind' then
  if c.status<>'pending_approval' then raise exception 'invalid_state'; end if;
  select * into t from telephone_templates where tenant_id=p_tenant and id=(p_payload->>'id')::uuid and version=(p_payload->>'version')::integer;
  if not found then raise exception 'not_found'; end if;
  select * into o from telephone_objectives where tenant_id=p_tenant and call_id=c.id;
  if found then
   if o.template_id=t.id and o.template_version=t.version then return to_jsonb(o)||'{"bound":true}'::jsonb; end if;
   raise exception 'invalid_state';
  end if;
  insert into telephone_objectives(tenant_id,call_id,template_id,template_version,template) values(p_tenant,c.id,t.id,t.version,t.spec);
  update telephone_calls set snapshot=snapshot||jsonb_build_object('template_id',t.id,'template_version',t.version,'template',t.spec) where id=c.id;
 end if;
 select * into o from telephone_objectives where tenant_id=p_tenant and call_id=c.id for update;
 if not found then return jsonb_build_object('bound',false); end if;
 if p_action in ('state','bind') then return to_jsonb(o)||'{"bound":true}'::jsonb; end if;
 if p_action='facts' then
  if length(coalesce(p_payload->>'source_id','')) not between 1 and 128 then raise exception 'invalid_payload'; end if;
  delta_value=coalesce(p_payload->'delta',(select coalesce(jsonb_object_agg(items.field_key,p_payload->'facts'->items.field_key),'{}')
    from jsonb_array_elements_text(p_payload->'provided_keys') as items(field_key)));
  select * into receipt from telephone_fact_receipts where tenant_id=p_tenant and call_id=c.id and source_id=p_payload->>'source_id';
  if found then
   if receipt.delta<>delta_value then raise exception 'event_conflict'; end if;
   return receipt.result;
  end if;
  if c.status not in ('initiated','ringing','answered') or o.status<>'incomplete' or o.revision<>(p_payload->>'expected_revision')::integer then raise exception 'invalid_state'; end if;
  if jsonb_typeof(p_payload->'facts')<>'object' or octet_length((p_payload->'facts')::text)>16384 then raise exception 'invalid_payload'; end if;
  ev=o.evidence;
  for k in select jsonb_array_elements_text(p_payload->'provided_keys') loop
   ev=ev||jsonb_build_object(k,jsonb_build_object('source','caller_statement','source_id',left(p_payload->>'source_id',128),'verified_external_action',false));
  end loop;
  update telephone_objectives set facts=p_payload->'facts',evidence=ev,revision=revision+1 where tenant_id=p_tenant and call_id=c.id returning * into o;
  insert into telephone_fact_receipts values(p_tenant,c.id,p_payload->>'source_id',delta_value,to_jsonb(o)||'{"bound":true}'::jsonb);
 elsif p_action='erase' then
  delete from telephone_fact_receipts where tenant_id=p_tenant and call_id=c.id;
  update telephone_objectives set facts='{}',evidence='{}',status='erased',reason='owner_erased',revision=revision+1
   where tenant_id=p_tenant and call_id=c.id returning * into o;
 elsif p_action='finish' then
  if o.status in ('erased','refused','opt_out','no_answer','voicemail','handoff') then return to_jsonb(o)||'{"bound":true}'::jsonb; end if;
  if o.revision<>(p_payload->>'expected_revision')::integer then raise exception 'invalid_state'; end if;
  if p_payload->>'reason' not in ('caller_finished','refused','opt_out','no_answer','voicemail','handoff','timeout','disconnected') then raise exception 'invalid_payload'; end if;
  -- External effects are proved by their own durable receipts, never caller facts.
  v=0;
  for k in select jsonb_array_elements_text(coalesce(o.template->'required_receipts','[]')) loop
   if k='message_delivered' and not exists(select 1 from telephone_messages m join telephone_message_outbox b
     on b.tenant_id=m.tenant_id and b.call_id=m.call_id and b.revision=m.revision
     where m.tenant_id=p_tenant and m.call_id=c.id and m.confirmed and b.status='delivered'
     and exists(select 1 from crm_activities where tenant_id=p_tenant and source_id='telephone:'||c.id::text)) then v=v+1; end if;
   if k='handover_answered' and not exists(select 1 from telephone_transfers where tenant_id=p_tenant and call_id=c.id and status='answered' and human_confirmed) then v=v+1; end if;
  end loop;
  update telephone_objectives set status=case when p_payload->>'reason' in ('refused','opt_out','no_answer','voicemail','handoff') then p_payload->>'reason'
   when coalesce((p_payload->>'criteria_valid')::boolean,false) and v=0 then 'completed' else 'incomplete' end,
   reason=case when v>0 then 'external_action_unconfirmed' when not coalesce((p_payload->>'criteria_valid')::boolean,false) then 'required_criteria_unmet' else p_payload->>'reason' end
   where tenant_id=p_tenant and call_id=c.id returning * into o;
 else raise exception 'invalid_command'; end if;
 return to_jsonb(o)||'{"bound":true}'::jsonb;
end;$$;
revoke all on function telephone_objective_command(uuid,uuid,text,jsonb) from public,anon,authenticated;
grant execute on function telephone_objective_command(uuid,uuid,text,jsonb) to service_role;
commit;
