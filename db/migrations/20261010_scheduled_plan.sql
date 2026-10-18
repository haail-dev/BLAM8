-- Phase 3: durable occurrences around existing application actions.
begin;
create table if not exists schedule_settings (
 tenant_id uuid primary key references tenants(id) on delete cascade,
 enabled boolean not null default false,
 config jsonb not null default '{"timezone":"Europe/London","working_days":[0,1,2,3,4],"holidays":[],"start_time":"09:00","end_time":"17:00","daily_cap":50,"min_gap_seconds":60}',
 last_attempt_at timestamptz
);
create table if not exists schedule_items (
 id uuid primary key default gen_random_uuid(), tenant_id uuid not null references tenants(id) on delete cascade,
 title text not null check(length(title) between 1 and 150),
 kind text not null check(kind in ('email','sms','quote','invoice','booking','task','followup','outreach')),
 mode text not null default 'draft' check(mode in ('draft','auto')),
 status text not null default 'active' check(status in ('active','paused','cancelled')),
 next_at timestamptz, timezone text not null default 'Europe/London',
 recurrence jsonb not null default '{"frequency":"once"}', catch_up text not null default 'run_late' check(catch_up in ('run_late','skip')),
 payload jsonb not null default '{}', dependency jsonb not null default '{}',
 revision integer not null default 1, created_by uuid not null,
 created_at timestamptz not null default now(), unique(tenant_id,id)
);
create index if not exists schedule_due on schedule_items(next_at) where status='active';
create table if not exists schedule_runs (
 id uuid primary key default gen_random_uuid(), tenant_id uuid not null references tenants(id) on delete cascade,
 item_id uuid not null, scheduled_at timestamptz not null, snapshot jsonb not null,
 status text not null default 'preparing' check(status in ('preparing','ready','executing','done','blocked','skipped','unknown')),
 token uuid, lease_until timestamptz, prepared jsonb not null default '{}',
 email_log_id uuid, result text, attempted_at timestamptz, finished_at timestamptz,
 created_at timestamptz not null default now(), last_checked_at timestamptz not null default now(),
 unique(item_id,scheduled_at), unique(tenant_id,id),
 foreign key(tenant_id,item_id) references schedule_items(tenant_id,id),
 foreign key(tenant_id,email_log_id) references email_log(tenant_id,id)
);
create table if not exists schedule_summaries (
 tenant_id uuid not null references tenants(id) on delete cascade, day date not null,
 counts jsonb not null, updated_at timestamptz not null default now(), primary key(tenant_id,day)
);
do $$ declare t text; begin
 foreach t in array array['schedule_settings','schedule_items','schedule_runs','schedule_summaries'] loop
  execute format('alter table %I enable row level security',t);
  execute format('drop policy if exists tenant_read on %I',t);
  execute format('create policy tenant_read on %I for select to authenticated using(tenant_id in (select auth_tenant_ids()))',t);
  execute format('revoke all on %I from anon, authenticated',t);
  execute format('grant select on %I to authenticated',t);
  execute format('grant all on %I to service_role',t);
 end loop;
end $$;

create or replace function schedule_reserve(p_tenant uuid,p_item uuid,p_due timestamptz,p_next timestamptz,p_token uuid)
returns setof schedule_runs language plpgsql security invoker set search_path=public as $$
declare i schedule_items;
begin
 perform 1 from schedule_settings where tenant_id=p_tenant and enabled for update;
 if not found then return; end if;
 select * into i from schedule_items where tenant_id=p_tenant and id=p_item for update;
 if not found or i.status<>'active' or i.next_at is distinct from p_due or i.next_at>now() then return; end if;
 if p_next is not null and p_next<=p_due then raise exception 'Recurrence must advance'; end if;
 return query insert into schedule_runs(tenant_id,item_id,scheduled_at,snapshot,token,lease_until)
 values(p_tenant,i.id,p_due,to_jsonb(i),p_token,now()+interval '5 minutes')
 on conflict(item_id,scheduled_at) do nothing returning *;
 update schedule_items set next_at=p_next where id=i.id;
end $$;

create or replace function schedule_prepare(p_tenant uuid,p_run uuid,p_token uuid,p_data jsonb)
returns boolean language plpgsql security invoker set search_path=public as $$
declare r schedule_runs; lid uuid;
begin
 select * into r from schedule_runs where tenant_id=p_tenant and id=p_run for update;
 if not found or r.status<>'preparing' or r.token<>p_token or r.lease_until<now() then return false; end if;
 if r.snapshot->>'kind' in ('email','sms') then
  insert into email_log(tenant_id,from_address,subject,body_in,draft_reply,status,kind,mailbox_id,source_message_id)
  values(p_tenant,p_data->>'to',p_data->>'subject','[scheduled plan]',p_data->>'body','pending','scheduled_plan',
    nullif(p_data->>'mailbox_id','')::uuid,'schedule:'||r.id::text) returning id into lid;
 end if;
 update schedule_runs set status='ready',prepared=p_data,email_log_id=lid,token=null,lease_until=null where id=r.id;
 return true;
end $$;

create or replace function schedule_skip_future(p_tenant uuid,p_item uuid,p_due timestamptz,p_next timestamptz)
returns boolean language plpgsql security invoker set search_path=public as $$
declare i schedule_items;
begin
 select * into i from schedule_items where tenant_id=p_tenant and id=p_item for update;
 if not found or i.next_at is distinct from p_due or p_due is null then return false; end if;
 if p_next is not null and p_next<=p_due then raise exception 'Recurrence must advance'; end if;
 insert into schedule_runs(tenant_id,item_id,scheduled_at,snapshot,status,result,finished_at)
 values(p_tenant,i.id,p_due,to_jsonb(i),'skipped','Skipped by the owner',now()) on conflict(item_id,scheduled_at) do nothing;
 update schedule_items set next_at=p_next where id=i.id;
 return true;
end $$;

create or replace function schedule_claim(p_tenant uuid,p_run uuid,p_auto boolean)
returns text language plpgsql security invoker set search_path=public as $$
declare s schedule_settings; r schedule_runs; i schedule_items; local_now timestamp; cap integer;
begin
 select * into s from schedule_settings where tenant_id=p_tenant for update;
 if not found or not s.enabled then return 'Scheduled plan is paused'; end if;
 select * into r from schedule_runs where tenant_id=p_tenant and id=p_run;
 if not found then return 'Run not found'; end if;
 select * into i from schedule_items where tenant_id=p_tenant and id=r.item_id for update;
 select * into r from schedule_runs where tenant_id=p_tenant and id=p_run for update;
 if r.status<>'ready' then return 'This occurrence is not ready'; end if;
 if i.status<>'active' or i.revision<>(r.snapshot->>'revision')::integer then return 'Item paused or changed'; end if;
 if not exists(select 1 from tenant_members where tenant_id=p_tenant and user_id=i.created_by
    and coalesce(active,true) and role in ('owner','manager','member')) then return 'Planner owner no longer has permission'; end if;
 if i.kind in ('task','followup','outreach') and not exists(select 1 from subscriptions
    where tenant_id=p_tenant and module='crm' and status='active') then return 'CRM is not active'; end if;
 if i.kind='booking' and not exists(select 1 from subscriptions where tenant_id=p_tenant
    and module='scheduling' and status='active') then return 'Scheduling is not active'; end if;
 if i.dependency->>'kind'='after_payment' and not exists(select 1 from invoices
    where tenant_id=p_tenant and id::text=i.dependency->>'invoice_id' and status='paid') then return 'Waiting for payment.'; end if;
 if i.dependency->>'kind'='if_no_reply' and exists(select 1 from email_log l join crm_contacts c
    on c.tenant_id=p_tenant and c.id::text=i.payload->>'contact_id'
    where l.tenant_id=p_tenant and l.kind='inbound' and lower(l.from_address)=lower(c.email)
    and l.created_at>=(i.dependency->>'since')::timestamptz) then return 'A reply has arrived; review this occurrence.'; end if;
 if p_auto and (i.mode<>'auto' or i.kind not in ('email','sms')) then return 'Approval is required'; end if;
 if r.email_log_id is not null and not exists(select 1 from email_log where id=r.email_log_id and tenant_id=p_tenant and status='pending') then return 'Draft is no longer pending'; end if;
 if p_auto and not coalesce((select coalesce(mc.auto_send,tc.auto_send,false) from tenant_config tc
    left join mailbox_config mc on mc.tenant_id=tc.tenant_id and mc.mailbox_id=nullif(r.prepared->>'mailbox_id','')::uuid
    where tc.tenant_id=p_tenant),false) then return 'Mailbox requires approval'; end if;
 if i.kind='email' and not exists(select 1 from email_connections where tenant_id=p_tenant
    and id=nullif(r.prepared->>'mailbox_id','')::uuid and sync_failing_since is null
    and lower(coalesce(nullif(send_as_address,''),address))=r.prepared->>'sender') then return 'Mailbox changed or needs attention'; end if;
 if exists(select 1 from crm_contacts where tenant_id=p_tenant and do_not_sequence
    and (lower(email)=lower(r.prepared->>'to') or lower(email)=lower(r.prepared->'payload'->>'to')
         or id::text=i.payload->>'contact_id')) then return 'Contact is stopped'; end if;
 if exists(select 1 from outreach_suppressions where tenant_id=p_tenant
    and (email=lower(r.prepared->>'to') or email=lower(r.prepared->'payload'->>'to'))) then return 'Contact is stopped'; end if;
 if exists(select 1 from ignored_contacts where tenant_id=p_tenant
    and (lower(address)=lower(r.prepared->>'to') or lower(address)=lower(r.prepared->'payload'->>'to'))) then return 'Contact is ignored'; end if;
 if exists(select 1 from schedule_runs where tenant_id=p_tenant and status in ('executing','unknown')) then return 'Another outcome needs review'; end if;
 local_now:=now() at time zone (s.config->>'timezone');
 if not (s.config->'working_days' @> to_jsonb(array[(extract(isodow from local_now)::integer-1)]))
  or s.config->'holidays' ? to_char(local_now,'YYYY-MM-DD')
  or local_now::time<(s.config->>'start_time')::time or local_now::time>=(s.config->>'end_time')::time then return 'Outside working window'; end if;
 cap:=(s.config->>'daily_cap')::integer;
 if (select count(*) from schedule_runs where tenant_id=p_tenant and attempted_at>now()-interval '24 hours')>=cap then return 'Daily cap reached'; end if;
 if s.last_attempt_at>now()-make_interval(secs=>(s.config->>'min_gap_seconds')::integer) then return 'Minimum gap'; end if;
 update schedule_runs set status='executing',attempted_at=now(),result=null where id=r.id;
 update schedule_settings set last_attempt_at=now() where tenant_id=p_tenant;
 return 'claimed';
end $$;

-- Only preparation that has never created a draft/action may be reclaimed.
create or replace function schedule_retry_prepare(p_tenant uuid,p_run uuid,p_token uuid)
returns setof schedule_runs language plpgsql security invoker set search_path=public as $$
begin
 perform 1 from schedule_settings where tenant_id=p_tenant and enabled for update;
 if not found then return; end if;
 return query update schedule_runs r set status='preparing',token=p_token,lease_until=now()+interval '5 minutes',result=null,finished_at=null
 from schedule_items i where r.tenant_id=p_tenant and r.id=p_run and r.status='blocked' and r.email_log_id is null
 and i.id=r.item_id and i.tenant_id=p_tenant and i.status='active' and i.revision=(r.snapshot->>'revision')::integer
 returning r.*;
end $$;

create or replace function schedule_complete(p_tenant uuid,p_run uuid,p_result text,p_auto boolean)
returns boolean language plpgsql security invoker set search_path=public as $$
declare r schedule_runs;
begin
 select * into r from schedule_runs where tenant_id=p_tenant and id=p_run for update;
 if not found or r.status<>'executing' then return false; end if;
 update schedule_runs set status='done',result=p_result,finished_at=now() where id=r.id;
 if r.email_log_id is not null then
  update email_log set status='sent',sent_at=now(),sent_via=case when p_auto then 'auto' else 'human' end
  where id=r.email_log_id and tenant_id=p_tenant;
 end if;
 return true;
end $$;
create or replace function schedule_reconcile(p_tenant uuid,p_run uuid,p_done boolean)
returns boolean language plpgsql security invoker set search_path=public as $$
declare r schedule_runs;
begin
 perform 1 from schedule_settings where tenant_id=p_tenant and not enabled for update;
 if not found then return false; end if;
 select * into r from schedule_runs where tenant_id=p_tenant and id=p_run for update;
 if not found or r.status not in ('executing','unknown') or r.attempted_at is null
    or r.attempted_at>now()-interval '5 minutes' then return false; end if;
 update schedule_runs set status=case when p_done then 'done' else 'skipped' end,finished_at=now(),
 result=case when p_done then 'Owner verified completion' else 'Closed without retry after review' end where id=r.id;
 if r.email_log_id is not null then
  update email_log set status=case when p_done then 'sent' else 'skipped' end,
   sent_at=case when p_done then r.attempted_at else sent_at end,
   sent_via=case when p_done then 'human' else sent_via end
  where tenant_id=p_tenant and id=r.email_log_id and status='pending';
 end if;
 return true;
end $$;
do $$ declare f regprocedure; begin
 for f in select p.oid::regprocedure from pg_proc p join pg_namespace n on n.oid=p.pronamespace
 where n.nspname='public' and p.proname in ('schedule_reserve','schedule_prepare','schedule_claim','schedule_complete','schedule_retry_prepare','schedule_skip_future','schedule_reconcile') loop
  execute format('revoke all on function %s from public,anon,authenticated',f);
  execute format('grant execute on function %s to service_role',f);
 end loop;
end $$;
commit;
