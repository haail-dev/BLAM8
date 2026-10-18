-- C3-01/04/07/09/10: durable revision/attempt/delivery ownership.
-- Additive; apply before enabling CONCIERGE_DELEGATION_ENABLED. Never replay effects.
begin;
alter table concierge_tasks add column if not exists revision integer not null default 0;
alter table concierge_tasks add column if not exists row_version bigint not null default 0;
alter table concierge_tasks add column if not exists attempt_id uuid;
alter table concierge_tasks add column if not exists source_turn text;
alter table concierge_tasks add column if not exists voice_session uuid;
alter table concierge_tasks add column if not exists commit_state text not null default 'preparation';
alter table concierge_tasks add column if not exists result jsonb not null default '{}';
alter table concierge_tasks add column if not exists event_id uuid;
alter table concierge_tasks add column if not exists delivery jsonb not null default '{}';
create unique index if not exists idx_concierge_task_source on concierge_tasks
 (tenant_id, coalesce(user_id,'00000000-0000-0000-0000-000000000000'::uuid),
  source_turn, coalesce(voice_session,'00000000-0000-0000-0000-000000000000'::uuid), position)
 where revision > 0;
-- Existing tenant-wide ALL policies must not permit forging/downgrading the
-- new authority. Legacy rows keep their old write behavior; new rows are
-- service-role-only writes, requesting-user-only reads.
drop policy if exists "tenant isolation - concierge_tasks" on concierge_tasks;
drop policy if exists "concierge tasks read" on concierge_tasks;
create policy "concierge tasks read" on concierge_tasks for select using
 (tenant_id in (select auth_tenant_ids()) and (revision=0 or user_id=auth.uid()));
drop policy if exists "concierge tasks legacy insert" on concierge_tasks;
create policy "concierge tasks legacy insert" on concierge_tasks for insert with check
 (tenant_id in (select auth_tenant_ids()) and revision=0);
drop policy if exists "concierge tasks legacy update" on concierge_tasks;
create policy "concierge tasks legacy update" on concierge_tasks for update using
 (tenant_id in (select auth_tenant_ids()) and revision=0) with check
 (tenant_id in (select auth_tenant_ids()) and revision=0);
drop policy if exists "concierge tasks legacy delete" on concierge_tasks;
create policy "concierge tasks legacy delete" on concierge_tasks for delete using
 (tenant_id in (select auth_tenant_ids()) and revision=0);
drop policy if exists "tenant isolation - concierge_proposals" on concierge_proposals;
drop policy if exists "concierge proposals read" on concierge_proposals;
create policy "concierge proposals read" on concierge_proposals for select using
 (tenant_id in (select auth_tenant_ids()) and (not (payload ? '_delegation') or user_id=auth.uid()));
drop policy if exists "concierge proposals legacy insert" on concierge_proposals;
create policy "concierge proposals legacy insert" on concierge_proposals for insert with check
 (tenant_id in (select auth_tenant_ids()) and not (payload ? '_delegation'));
drop policy if exists "concierge proposals legacy update" on concierge_proposals;
create policy "concierge proposals legacy update" on concierge_proposals for update using
 (tenant_id in (select auth_tenant_ids()) and not (payload ? '_delegation')) with check
 (tenant_id in (select auth_tenant_ids()) and not (payload ? '_delegation'));
drop policy if exists "concierge proposals legacy delete" on concierge_proposals;
create policy "concierge proposals legacy delete" on concierge_proposals for delete using
 (tenant_id in (select auth_tenant_ids()) and not (payload ? '_delegation'));
create table if not exists concierge_task_history (
 id bigint generated always as identity primary key,
 tenant_id uuid not null references tenants(id) on delete cascade,
 task_id uuid not null references concierge_tasks(id) on delete cascade,
 revision integer not null,
 recorded_at timestamptz not null default now(),
 reason text not null,
 snapshot jsonb not null
);
alter table concierge_task_history enable row level security;
drop policy if exists "tenant isolation - concierge_task_history" on concierge_task_history;
create policy "tenant isolation - concierge_task_history" on concierge_task_history
 for select using (tenant_id in (select auth_tenant_ids()) and exists
   (select 1 from concierge_tasks t where t.id=task_id and t.user_id=auth.uid()));
-- Writes are through an invoker function and existing task permissions.
drop policy if exists "tenant history insert" on concierge_task_history;
create policy "tenant history insert" on concierge_task_history
 for insert with check (tenant_id in (select auth_tenant_ids()) and exists
   (select 1 from concierge_tasks t where t.id=task_id and t.user_id=auth.uid()));

create or replace function concierge_task_transition(
 p_operation text, p_tenant uuid, p_user uuid, p_task uuid default null,
 p_revision integer default null, p_attempt uuid default null, p_data jsonb default '{}'
) returns jsonb language plpgsql security invoker set search_path=public as $$
declare
 t concierge_tasks%rowtype;
 p concierge_proposals%rowtype;
 r jsonb;
 binding jsonb;
 candidate jsonb;
 new_id uuid;
 rows_out jsonb;
begin
 -- Authenticated callers cannot impersonate another requester; service workers
 -- have no auth.uid and are still constrained by all explicit row predicates.
 if auth.uid() is not null and auth.uid() is distinct from p_user then
   return jsonb_build_object('ok',false,'state','unavailable');
 end if;
 if auth.uid() is not null and p_operation in
   ('claim','recover','stage','finish','approval_claim','approval_finish') then
   return jsonb_build_object('ok',false,'state','worker_only');
 end if;
 if p_operation='create' then
   if nullif(p_data->>'source_turn','') is null then raise exception 'source turn required'; end if;
   -- Idempotency covers the WHOLE source turn, not individual positions: a
   -- retried model split must not append extra autonomous work to that turn.
   perform pg_advisory_xact_lock(hashtextextended(concat_ws(':','concierge-delegation',p_tenant::text,
      coalesce(p_user::text,''),p_data->>'source_turn',coalesce(p_data->>'voice_session','')),0));
   select jsonb_agg(to_jsonb(c) order by position) into rows_out from concierge_tasks c
    where tenant_id=p_tenant and user_id is not distinct from p_user
      and source_turn=p_data->>'source_turn'
      and voice_session is not distinct from nullif(p_data->>'voice_session','')::uuid;
   if rows_out is not null then return jsonb_build_object('ok',true,'tasks',rows_out); end if;
   for candidate in select value from jsonb_array_elements(p_data->'tasks') loop
     insert into concierge_tasks(tenant_id,user_id,batch_id,position,title,instruction,surface,
        revision,source_turn,voice_session,result)
       values(p_tenant,p_user,(p_data->>'batch_id')::uuid,(candidate->>'position')::integer,
        candidate->>'title',candidate->>'instruction',p_data->>'surface',1,
        p_data->>'source_turn',nullif(p_data->>'voice_session','')::uuid,jsonb_build_object('scope',p_data->>'scope'))
       on conflict do nothing;
   end loop;
   select jsonb_agg(to_jsonb(c) order by position) into rows_out from concierge_tasks c
    where tenant_id=p_tenant and user_id is not distinct from p_user
      and source_turn=p_data->>'source_turn'
      and voice_session is not distinct from nullif(p_data->>'voice_session','')::uuid;
   return jsonb_build_object('ok',rows_out is not null,'tasks',coalesce(rows_out,'[]'));
 end if;
 -- Linked proposal operations resolve the immutable task binding first, then
 -- lock TASK before PROPOSAL in every path, including revise and resurface.
 if p_operation in ('approval_claim','approval_finish','resurface') then
   select * into p from concierge_proposals where id=(p_data->>'proposal_id')::uuid and tenant_id=p_tenant;
   if not found then return jsonb_build_object('ok',false,'state','unavailable'); end if;
   binding=p.payload->'_delegation';
   if binding is null then return jsonb_build_object('ok',false,'state','unavailable'); end if;
   p_task=(binding->>'task_id')::uuid;
 end if;
 select * into t from concierge_tasks where id=p_task and tenant_id=p_tenant
   and user_id is not distinct from p_user and revision>0 for update;
 if not found then return jsonb_build_object('ok',false,'state','unavailable'); end if;
 if p_operation in ('approval_claim','approval_finish','resurface') then
   select * into p from concierge_proposals where id=p.id and tenant_id=p_tenant for update;
   if t.revision<>(binding->>'revision')::integer or t.proposal_id is distinct from p.id
      or binding->>'digest' is distinct from md5((p.payload-'_delegation')::text) then
     return jsonb_build_object('ok',false,'state','superseded','task',to_jsonb(t));
   end if;
 else
   if p_revision is distinct from t.revision then
     return jsonb_build_object('ok',false,'state','stale','task',to_jsonb(t));
   end if;
 end if;
 if p_operation in ('revise','cancel') then
   if t.commit_state in ('executing','committed','unknown') then
     return jsonb_build_object('ok',false,'state','too_late','task',to_jsonb(t));
   end if;
   if t.status not in ('queued','working','waiting','needs_you') then
     return jsonb_build_object('ok',false,'state','closed','task',to_jsonb(t));
   end if;
   if t.proposal_id is not null then
     select * into p from concierge_proposals where id=t.proposal_id and tenant_id=p_tenant for update;
     if found and p.status not in ('pending','superseded','expired') then
       return jsonb_build_object('ok',false,'state','too_late','task',to_jsonb(t));
     end if;
     update concierge_proposals set status='superseded' where id=t.proposal_id and tenant_id=p_tenant;
   end if;
   insert into concierge_task_history(tenant_id,task_id,revision,reason,snapshot)
     values(p_tenant,t.id,t.revision,p_operation,to_jsonb(t));
   update concierge_tasks set row_version=row_version+1, revision=revision+1, attempt_id=null, proposal_id=null,
      status=case when p_operation='cancel' then 'cancelled' else 'queued' end,
      instruction=case when p_operation='revise' then p_data->>'instruction' else instruction end,
      outcome=case when p_operation='cancel' then 'Task cancelled before execution.' else null end,
      result=case when p_operation='cancel' then '{"kind":"cancelled","summary":"Task cancelled before execution."}'::jsonb else jsonb_build_object('scope',result->>'scope') end,
      commit_state='preparation',started_at=null,finished_at=case when p_operation='cancel' then now() else null end,
      event_id=gen_random_uuid(),delivery='{}' where id=t.id returning * into t;
 elsif p_operation='claim' then
   if t.status<>'queued' then return jsonb_build_object('ok',false,'state','busy','task',to_jsonb(t)); end if;
   insert into concierge_task_history(tenant_id,task_id,revision,reason,snapshot)
     values(p_tenant,t.id,t.revision,'attempt',to_jsonb(t));
   update concierge_tasks set row_version=row_version+1, status='working',attempt_id=p_attempt,started_at=now()
     where id=t.id returning * into t;
 elsif p_operation='recover' then
   if t.status='working' and t.commit_state='preparation' then
     insert into concierge_task_history(tenant_id,task_id,revision,reason,snapshot)
       values(p_tenant,t.id,t.revision,'restart',to_jsonb(t));
     update concierge_tasks set row_version=row_version+1, status='queued',attempt_id=null where id=t.id returning * into t;
   elsif t.commit_state='executing' then
     -- The proposal receipt remains Cycle2-owned. Never re-enter an effect.
     update concierge_tasks set row_version=row_version+1, status='needs_you',commit_state='unknown',
       outcome='Outcome unconfirmed. Check the original action.',event_id=gen_random_uuid(),
       result='{"kind":"unknown","summary":"Outcome unconfirmed. Check the original action."}',delivery='{}'
       where id=t.id returning * into t;
   end if;
 elsif p_operation in ('finish','stage') then
   if t.status<>'working' or t.commit_state<>'preparation' or t.attempt_id is distinct from p_attempt then
     return jsonb_build_object('ok',false,'state','superseded','task',to_jsonb(t));
   end if;
   if p_operation='stage' then
     new_id=gen_random_uuid();
     r=coalesce(p_data->'payload','{}');
     binding=jsonb_build_object('task_id',t.id,'revision',t.revision,'digest',md5(r::text));
     insert into concierge_proposals(id,tenant_id,user_id,kind,summary,payload,status)
       values(new_id,p_tenant,p_user,p_data->>'kind',p_data->>'summary',
        r||jsonb_build_object('_delegation',binding),'pending');
     update concierge_tasks set row_version=row_version+1, status='waiting',proposal_id=new_id,outcome=p_data->>'summary',
       result=jsonb_build_object('kind','waiting','summary','Prepared for your approval: '||(p_data->>'summary'),'scope',t.result->>'scope'),
       finished_at=now(),event_id=gen_random_uuid(),delivery='{}' where id=t.id returning * into t;
   else
     update concierge_tasks set row_version=row_version+1, status=p_data->>'status',outcome=p_data->>'summary',
       result=p_data->'result',finished_at=now(),event_id=gen_random_uuid(),delivery='{}'
       where id=t.id returning * into t;
   end if;
 elsif p_operation in ('approval_claim','resurface') and p.status='pending' and t.status='waiting'
       and p.created_at < coalesce((p_data->>'cutoff')::timestamptz,now()-interval '5 minutes') then
   update concierge_proposals set status='expired' where id=p.id returning * into p;
   update concierge_tasks set row_version=row_version+1,status='needs_you',
     result=jsonb_build_object('kind','failed','summary','This approval expired. Revise the request to prepare a current card.','scope',t.result->>'scope'),
     outcome='This approval expired. Revise the request to prepare a current card.',
     event_id=gen_random_uuid(),delivery='{}',finished_at=now() where id=t.id returning * into t;
   return jsonb_build_object('ok',false,'state','expired','task',to_jsonb(t),'proposal',to_jsonb(p));
 elsif p_operation='approval_claim' then
   if p.status<>'pending' or t.status<>'waiting' or t.commit_state<>'preparation'
      or p.created_at < (p_data->>'cutoff')::timestamptz then
     return jsonb_build_object('ok',false,'state','unavailable','task',to_jsonb(t));
   end if;
   update concierge_proposals set status='executing',result=p_data->'receipt',executed_at=null
     where id=p.id returning * into p;
   update concierge_tasks set row_version=row_version+1, status='working',commit_state='executing' where id=t.id returning * into t;
   return jsonb_build_object('ok',true,'task',to_jsonb(t),'proposal',to_jsonb(p));
 elsif p_operation='approval_finish' then
   if p.status not in ('executing','unknown') or p.result->'execution'->>'id' is distinct from p_data->>'execution_id' then
     return jsonb_build_object('ok',false,'state','stale','task',to_jsonb(t));
   end if;
   update concierge_proposals set status=p_data->>'status',result=p_data->'receipt',
     executed_at=case when p_data->>'status'='executed' then now() else executed_at end
     where id=p.id returning * into p;
   update concierge_tasks set row_version=row_version+1, status=case when p.status='executed' then 'done' else 'needs_you' end,
     commit_state=case when p.status='executed' then 'committed' else 'unknown' end,
     outcome=p.result->>'summary',result=jsonb_build_object('kind','action_receipt','proposal_id',p.id),
     finished_at=now(),event_id=gen_random_uuid(),delivery='{}' where id=t.id returning * into t;
 elsif p_operation='resurface' then
   if p.status<>'pending' or t.status<>'waiting' or t.commit_state<>'preparation' then
     return jsonb_build_object('ok',false,'state','unavailable','task',to_jsonb(t));
   end if;
   -- Return the SAME immutable prepared card; never stage a second copy.
   return jsonb_build_object('ok',true,'task',to_jsonb(t),'proposal',to_jsonb(p));
 elsif p_operation='notice_claim' then
   if t.event_id is null or t.event_id is distinct from (p_data->>'event_id')::uuid
      or t.status not in ('waiting','done','needs_you','failed','cancelled') then
     return jsonb_build_object('ok',false,'state','stale');
   end if;
   if exists(select 1 from concierge_task_history where task_id=t.id and revision=t.revision
             and reason='notice:'||t.event_id::text) then
     return jsonb_build_object('ok',false,'state','claimed');
   end if;
   insert into concierge_task_history(tenant_id,task_id,revision,reason,snapshot)
     values(p_tenant,t.id,t.revision,'notice:'||t.event_id::text,to_jsonb(t));
 elsif p_operation in ('delivery_claim','delivery_start','delivery_ack','delivery_release') then
   if t.event_id is distinct from (p_data->>'event_id')::uuid or t.voice_session is distinct from (p_data->>'voice_session')::uuid
      or t.status not in ('waiting','done','needs_you','failed','cancelled') then
     return jsonb_build_object('ok',false,'state','stale','task',to_jsonb(t));
   end if;
   if p_operation='delivery_claim' then
     if t.delivery<>'{}'::jsonb then return jsonb_build_object('ok',false,'state','claimed','task',to_jsonb(t)); end if;
     update concierge_tasks set row_version=row_version+1, delivery=jsonb_build_object('id',p_data->>'delivery_id','state','claimed','at',now())
       where id=t.id returning * into t;
   else
     if t.delivery->>'id' is distinct from p_data->>'delivery_id' then
       return jsonb_build_object('ok',false,'state','unavailable');
     end if;
     if p_operation='delivery_release' then
       if t.delivery->>'state'<>'claimed' then return jsonb_build_object('ok',false,'state','stale'); end if;
       update concierge_tasks set row_version=row_version+1, delivery='{}' where id=t.id returning * into t;
     elsif p_operation='delivery_start' then
       if t.delivery->>'state'<>'claimed' then return jsonb_build_object('ok',false,'state','stale'); end if;
       update concierge_tasks set row_version=row_version+1, delivery=delivery||'{"state":"started"}'::jsonb where id=t.id returning * into t;
     else
       if t.delivery->>'state' not in ('claimed','started') then return jsonb_build_object('ok',false,'state','stale'); end if;
       update concierge_tasks set row_version=row_version+1, delivery=delivery||jsonb_build_object('state',
         case when p_data->>'outcome'='completed' and delivery->>'state'='started' then 'completed' else 'interrupted' end)
         where id=t.id returning * into t;
     end if;
   end if;
 else
   raise exception 'unsupported task transition';
 end if;
 return jsonb_build_object('ok',true,'task',to_jsonb(t));
end $$;
revoke all on function concierge_task_transition(text,uuid,uuid,uuid,integer,uuid,jsonb) from public;
revoke all on function concierge_task_transition(text,uuid,uuid,uuid,integer,uuid,jsonb) from authenticated;
grant execute on function concierge_task_transition(text,uuid,uuid,uuid,integer,uuid,jsonb) to service_role;
revoke all on concierge_task_history from authenticated;
grant select,insert on concierge_task_history to service_role;
grant usage,select on sequence concierge_task_history_id_seq to service_role;
commit;
